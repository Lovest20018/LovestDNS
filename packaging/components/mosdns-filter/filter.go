// Package dnsfilter adds an instance-local filtering snapshot at the DNS entry.
package dnsfilter

import (
	"bufio"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/miekg/dns"
)

const maxPolicy = 64 << 20

var (
	revisionPattern = regexp.MustCompile("^[a-f0-9]{64}$")
	tokenPattern    = regexp.MustCompile("^[a-f0-9]{32}$")
	once            sync.Once
	core            *Filter
)

type Rule struct {
	ID       string `json:"id"`
	Source   string `json:"source"`
	SourceID string `json:"source_id,omitempty"`
	Rule     string `json:"rule"`
	Until    int64  `json:"until,omitempty"`
}

type policy struct {
	Fragments []struct {
		Text string `json:"text"`
		Rule
	} `json:"fragments"`
	Format      int             `json:"version"`
	Enabled     bool            `json:"enabled"`
	Exact       map[string]Rule `json:"exact"`
	Suffix      map[string]Rule `json:"suffix"`
	Except      map[string]Rule `json:"except"`
	Allow       map[string]Rule `json:"allow"`
	AllowExact  map[string]Rule `json:"allow_exact"`
	AllowSuffix map[string]Rule `json:"allow_suffix"`

	revision string
	probes   [2]string
	probeEnd time.Time
}

type decision struct {
	Action   string `json:"action"`
	Revision string `json:"revision"`
	*Rule
}

func suffixMatch(terms map[string]Rule, name string) (Rule, bool) {
	for {
		if rule, ok := terms[name]; ok {
			return rule, true
		}
		_, rest, ok := strings.Cut(name, ".")
		if !ok {
			return Rule{}, false
		}
		name = rest
	}
}

func (p *policy) match(name string, now int64) decision {
	if !p.Enabled {
		return decision{Action: "disabled", Revision: p.revision}
	}
	d := decision{Action: "pass", Revision: p.revision}
	if rule, ok := p.Allow[name]; ok && (rule.Until == 0 || rule.Until > now) {
		d.Action, d.Rule = "allow", &rule
	} else if rule, ok := p.AllowExact[name]; ok {
		d.Action, d.Rule = "allow", &rule
	} else if rule, ok := suffixMatch(p.AllowSuffix, name); ok {
		d.Action, d.Rule = "allow", &rule
	} else if rule, ok := suffixMatch(p.Except, name); ok {
		d.Action, d.Rule = "allow", &rule
	} else if rule, ok := p.Exact[name]; ok {
		d.Action, d.Rule = "block", &rule
	} else if rule, ok := suffixMatch(p.Suffix, name); ok {
		d.Action, d.Rule = "block", &rule
	} else {
		for i := range p.Fragments {
			fragment := &p.Fragments[i]
			if strings.Contains(name, fragment.Text) {
				d.Action, d.Rule = "block", &fragment.Rule
				break
			}
		}
	}
	return d
}

type endpoint struct {
	Transport string `json:"transport"`
	Address   string `json:"address"`
}

type Filter struct {
	directory string
	epoch     string
	active    atomic.Pointer[policy]
	grant     atomic.Pointer[grant]
	events    chan event
	sequence  atomic.Uint64
	drops     atomic.Uint64

	endpointsMu sync.Mutex
	endpoints   []endpoint
}

// ForListener initializes the control endpoint once, before serving requests.
// An absent environment leaves a baseline MosDNS invocation unmodified.
func ForListener(transport string, address net.Addr) *Filter {
	once.Do(func() {
		state, run := os.Getenv("LOVESTDNS_FILTER_STATE"), os.Getenv("LOVESTDNS_FILTER_RUN")
		if state == "" || run == "" {
			return
		}
		f := &Filter{directory: state, epoch: randomToken(), events: make(chan event, 256)}
		if f.epoch == "" {
			return
		}
		f.active.Store(&policy{})
		if data, err := os.ReadFile(filepath.Join(state, "active")); err == nil {
			p, loadErr := f.read(strings.TrimSpace(string(data)))
			if loadErr != nil {
				// Preserve a working DNS entry and report the revision mismatch.
				data, _ = os.ReadFile(filepath.Join(state, "previous"))
				p, loadErr = f.read(strings.TrimSpace(string(data)))
			}
			if loadErr == nil {
				f.active.Store(p)
			}
		}
		path := filepath.Join(run, "dns-filter.sock")
		_ = os.Remove(path)
		listener, err := net.Listen("unix", path)
		if err != nil {
			// DNS still uses the verified snapshot if the management socket fails.
			core = f
			return
		}
		if err = os.Chmod(path, 0600); err != nil {
			_ = listener.Close()
			core = f
			return
		}
		go f.control(listener)
		go f.sendEvents(filepath.Join(run, "filter-events.sock"))
		core = f
	})
	if core != nil {
		core.endpointsMu.Lock()
		core.endpoints = append(core.endpoints, endpoint{transport, address.String()})
		core.endpointsMu.Unlock()
	}
	return core
}

func randomToken() string {
	var token [16]byte
	if _, err := rand.Read(token[:]); err != nil {
		return ""
	}
	return hex.EncodeToString(token[:])
}

func (f *Filter) read(revision string) (*policy, error) {
	if revision == "" {
		return &policy{}, nil
	}
	if !revisionPattern.MatchString(revision) {
		return nil, errors.New("invalid revision")
	}
	file, err := os.Open(filepath.Join(f.directory, revision+".policy"))
	if err != nil {
		return nil, err
	}
	defer file.Close()
	data, err := io.ReadAll(io.LimitReader(file, maxPolicy+1))
	if err != nil || len(data) > maxPolicy {
		return nil, errors.New("policy size or read error")
	}
	sum := sha256.Sum256(data)
	if hex.EncodeToString(sum[:]) != revision {
		return nil, errors.New("policy checksum mismatch")
	}
	p := &policy{revision: revision}
	if err := json.Unmarshal(data, p); err != nil || p.Format < 1 || p.Format > 3 || len(p.Fragments) > 128 ||
		(p.Format < 3 && (len(p.AllowExact) != 0 || len(p.AllowSuffix) != 0)) {
		return nil, errors.New("invalid policy format")
	}
	return p, nil
}

type command struct {
	Op       string `json:"op"`
	Revision string `json:"revision"`
	Domain   string `json:"domain"`
	Token    string `json:"token"`
}

func (f *Filter) control(listener net.Listener) {
	slots := make(chan struct{}, 4)
	for {
		conn, err := listener.Accept()
		if err != nil {
			return
		}
		select {
		case slots <- struct{}{}:
			go func() {
				defer func() { <-slots }()
				f.serveControl(conn)
			}()
		default:
			_ = conn.Close()
		}
	}
}

func (f *Filter) serveControl(conn net.Conn) {
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(4 * time.Second))
	data, err := bufio.NewReaderSize(conn, 2048).ReadSlice('\n')
	var c command
	if err != nil || json.Unmarshal(data, &c) != nil {
		return
	}
	response := map[string]any{"ok": true}
	switch c.Op {
	case "load":
		p, err := f.read(c.Revision)
		token := randomToken()
		if err != nil || token == "" {
			response = map[string]any{"ok": false, "error": "policy_load_failed"}
			break
		}
		p.probes = [2]string{token + ".filter-block.invalid", token + ".filter-allow.invalid"}
		p.probeEnd = time.Now().Add(15 * time.Second)
		if p.Exact == nil {
			p.Exact = make(map[string]Rule)
		}
		if p.Allow == nil {
			p.Allow = make(map[string]Rule)
		}
		p.Exact[p.probes[0]] = Rule{}
		p.Exact[p.probes[1]] = Rule{}
		p.Allow[p.probes[1]] = Rule{}
		f.active.Store(p)
		response["probes"] = p.probes
	case "check":
		response["decision"] = f.active.Load().match(c.Domain, time.Now().Unix())
	case "record":
		if !tokenPattern.MatchString(c.Token) {
			return
		}
		f.grant.Store(&grant{c.Token, time.Now().Add(4 * time.Second)})
	case "status":
	default:
		return
	}
	p := f.active.Load()
	response["revision"], response["enabled"] = p.revision, p.Enabled
	response["pid"], response["epoch"], response["drops"] = os.Getpid(), f.epoch, f.drops.Load()
	f.endpointsMu.Lock()
	response["endpoints"] = append([]endpoint(nil), f.endpoints...)
	f.endpointsMu.Unlock()
	_ = json.NewEncoder(conn).Encode(response)
}

type evaluation struct {
	decision decision
	grant    *grant
	start    time.Time
}

// Begin runs before MosDNS executes its entry sequence (including cache).
// A synthetic response skips the entire sequence and its cache writes.
func (f *Filter) Begin(q *dns.Msg) (evaluation, *dns.Msg) {
	if f == nil || q.Response || q.Opcode != dns.OpcodeQuery || len(q.Question) != 1 {
		return evaluation{}, nil
	}
	p := f.active.Load()
	g := f.grant.Load()
	if !p.Enabled && g == nil && p.probeEnd.IsZero() {
		return evaluation{}, nil
	}
	now := time.Now()
	if g != nil && !now.Before(g.until) {
		f.grant.CompareAndSwap(g, nil)
		g = nil
	}
	if !p.Enabled && g == nil && !now.Before(p.probeEnd) {
		return evaluation{}, nil
	}
	name := strings.TrimSuffix(canonical(q.Question[0].Name), ".")
	e := evaluation{start: now, grant: g}
	if p.probes[0] != "" && (name == p.probes[0] || name == p.probes[1]) {
		if q.Question[0].Qclass != dns.ClassINET || !now.Before(p.probeEnd) {
			return e, nil
		}
		code := dns.RcodeSuccess
		if p.match(name, now.Unix()).Action == "block" {
			code = dns.RcodeRefused
		}
		return evaluation{}, reply(q, code)
	}
	if q.Question[0].Qclass == dns.ClassINET && p.Enabled {
		e.decision = p.match(name, now.Unix())
		if e.decision.Action == "block" {
			return e, reply(q, dns.RcodeRefused)
		}
	}
	return e, nil
}

func reply(q *dns.Msg, code int) *dns.Msg {
	r := new(dns.Msg)
	r.SetRcode(q, code)
	r.RecursionAvailable = true
	return r
}
