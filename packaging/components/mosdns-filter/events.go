package dnsfilter

import (
	"encoding/json"
	"net"
	"strconv"
	"strings"
	"time"

	"github.com/miekg/dns"
)

type grant struct {
	token string
	until time.Time
}

type event struct {
	Token     string    `json:"token"`
	Epoch     string    `json:"epoch"`
	Sequence  uint64    `json:"sequence"`
	Started   int64     `json:"started_us"`
	Finished  int64     `json:"finished_us"`
	Elapsed   float64   `json:"elapsed_ms"`
	Transport string    `json:"transport"`
	Client    string    `json:"client"`
	Entry     string    `json:"entry"`
	Name      string    `json:"name"`
	Type      uint16    `json:"qtype"`
	Class     uint16    `json:"qclass"`
	Rcode     *int      `json:"rcode"`
	Answers   int       `json:"answer_count"`
	Addresses []string  `json:"addresses"`
	Complete  bool      `json:"addresses_complete"`
	Truncated bool      `json:"truncated"`
	Decision  *decision `json:"filter,omitempty"`
}

// Record publishes the decision and the response of this very request. No DNS
// ID or timestamp pairing with another stream is required.
func (f *Filter) Record(e evaluation, transport string, client, listener net.Addr, local net.IP, q, r *dns.Msg) {
	if e.grant == nil {
		return
	}
	now := time.Now()
	g := f.grant.Load()
	if g == nil || g.token != e.grant.token || !now.Before(g.until) {
		return
	}
	question := q.Question[0]
	name := canonical(question.Name)
	entry := listener.String()
	if local != nil && !local.IsUnspecified() {
		if _, port, err := net.SplitHostPort(entry); err == nil {
			entry = net.JoinHostPort(local.String(), port)
		}
	}
	value := event{
		Token: g.token, Epoch: f.epoch, Sequence: f.sequence.Add(1),
		Started: e.start.UnixMicro(), Finished: now.UnixMicro(), Elapsed: float64(now.Sub(e.start).Microseconds()) / 1000,
		Transport: transport, Client: client.String(), Entry: entry,
		Name: name, Type: question.Qtype, Class: question.Qclass, Addresses: []string{}, Complete: r != nil,
	}
	if e.decision.Action != "" {
		value.Decision = &e.decision
	}
	if r != nil {
		code := r.Rcode
		value.Rcode, value.Answers, value.Truncated = &code, len(r.Answer), r.Truncated
		if r.Truncated {
			value.Complete = false
		}
		for _, answer := range r.Answer {
			var ip net.IP
			switch a := answer.(type) {
			case *dns.A:
				ip = a.A
			case *dns.AAAA:
				ip = a.AAAA
			default:
				continue
			}
			if len(value.Addresses) == 4 {
				value.Complete = false
				break
			}
			value.Addresses = append(value.Addresses, ip.String())
		}
	}
	select {
	case f.events <- value:
	default:
		f.drops.Add(1)
	}
}

// Match the passive recorder's display representation without changing DNS
// packets. DNS label bytes outside the printable range remain escaped.
func canonical(name string) string {
	if !strings.Contains(name, "\\") {
		return strings.ToLower(name)
	}
	// miekg/dns escapes printable bytes as \X; expand that representation
	// to the recorder's numeric format for a literal label dot or backslash.
	out := make([]byte, 0, len(name))
	for i := 0; i < len(name); i++ {
		ch := name[i]
		if ch == '\\' && i+1 < len(name) {
			i++
			ch = name[i]
			if i+2 < len(name) {
				if value, err := strconv.ParseUint(name[i:i+3], 10, 8); err == nil {
					ch = byte(value)
					i += 2
				}
			}
			if ch < 33 || ch > 126 || ch == '.' || ch == '\\' {
				out = append(out, '\\', '0'+ch/100, '0'+ch/10%10, '0'+ch%10)
				continue
			}
		}
		if ch >= 'A' && ch <= 'Z' {
			ch += 'a' - 'A'
		}
		out = append(out, ch)
	}
	return string(out)
}

func (f *Filter) sendEvents(path string) {
	var conn net.Conn
	for value := range f.events {
		g := f.grant.Load()
		if g == nil || g.token != value.Token || !time.Now().Before(g.until) {
			continue
		}
		if conn == nil {
			var err error
			conn, err = net.DialTimeout("unix", path, 100*time.Millisecond)
			if err != nil {
				f.drops.Add(1)
				continue
			}
		}
		_ = conn.SetWriteDeadline(time.Now().Add(100 * time.Millisecond))
		if err := json.NewEncoder(conn).Encode(value); err != nil {
			f.drops.Add(1)
			_ = conn.Close()
			conn = nil
		}
	}
}
