// Stream a private cachedb catalog through ordinary old/new Unbound instances.
// Build offline using the DNSCrypt source lock's vendor tree; never query public DNS.
package main

import (
	"bufio"
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"os"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/miekg/dns"
)

type record struct {
	DB   byte
	Kind byte
	Blob []byte
}

type totals struct {
	Checked           uint64            `json:"checked"`
	Matched           uint64            `json:"matched"`
	RetainedPayload   uint64            `json:"retained_payload"`
	SharedPolicy      uint64            `json:"shared_policy_or_partial_response"`
	MissingTarget     uint64            `json:"missing_target_refresh_required"`
	TCP               uint64            `json:"tcp_fallbacks"`
	Expired           uint64            `json:"expired_payloads"`
	Rcodes            map[string]uint64 `json:"rcodes"`
	Types             map[string]uint64 `json:"question_types"`
	TTLPositive       uint64            `json:"positive_ttl_responses"`
	StaleTTLZero      uint64            `json:"zero_ttl_responses"`
	PolicyDifferences map[string]uint64 `json:"classified_policy_differences"`
	Unexplained       uint64            `json:"unexplained_differences"`
	OpaquePayload     uint64            `json:"typed_payload_decode_failures"`
}

type result struct {
	Passed         bool                     `json:"passed"`
	Complete       bool                     `json:"complete"`
	Input          uint64                   `json:"input_records"`
	Totals         totals                   `json:"totals"`
	DB             map[string]*totals       `json:"databases"`
	Elapsed        float64                  `json:"elapsed_seconds"`
	Error          string                   `json:"error,omitempty"`
	QuestionHash   string                   `json:"failed_question_sha256,omitempty"`
	Differences    []map[string]interface{} `json:"unexplained_examples,omitempty"`
	Transport      string                   `json:"transport"`
	Scope          string                   `json:"scope"`
	OpaqueExamples []map[string]interface{} `json:"typed_invalid_wire_examples,omitempty"`
}

func freshTotals() totals {
	return totals{Rcodes: map[string]uint64{}, Types: map[string]uint64{}, PolicyDifferences: map[string]uint64{}}
}

// Some historical values contain a complete question and structurally framed
// records whose typed RDATA cannot be unpacked. Keep those bytes untouched and
// compare the two real resolvers, without claiming an expected-answer match or
// applying a policy exemption to an undecodable reference message.
func decodeBlob(blob []byte) (*dns.Msg, dns.Question, string, error) {
	var empty dns.Question
	if len(blob) < 33 {
		return nil, empty, "", errors.New("Truncated cachedb catalog record")
	}
	packet := blob[:len(blob)-16]
	if binary.BigEndian.Uint16(packet[4:6]) != 1 || binary.BigEndian.Uint16(packet[2:4])&0x8000 == 0 {
		return nil, empty, "", errors.New("Expected one question in a cached response")
	}
	name, end, e := dns.UnpackDomainName(packet, 12)
	if e != nil || end+4 > len(packet) {
		return nil, empty, "", errors.New("Cannot derive a complete catalog question")
	}
	q := dns.Question{Name: name, Qtype: binary.BigEndian.Uint16(packet[end : end+2]), Qclass: binary.BigEndian.Uint16(packet[end+2 : end+4])}
	var expected dns.Msg
	if e = expected.Unpack(packet); e != nil {
		return nil, q, e.Error(), nil
	}
	if len(expected.Question) != 1 {
		return nil, empty, "", errors.New("Expected one catalog question")
	}
	return &expected, q, "", nil
}

func readRecord(reader io.Reader) (record, error) {
	var size [4]byte
	if _, e := io.ReadFull(reader, size[:]); e != nil {
		return record{}, e
	}
	n := binary.BigEndian.Uint32(size[:])
	if n < 35 || n > 65553 {
		return record{}, errors.New("Invalid catalog frame length")
	}
	payload := make([]byte, n)
	if _, e := io.ReadFull(reader, payload); e != nil {
		if e == io.EOF {
			e = io.ErrUnexpectedEOF
		}
		return record{}, e
	}
	if payload[0] > 1 || payload[1] < 1 || payload[1] > 2 {
		return record{}, errors.New("Invalid catalog DB or kind")
	}
	return record{payload[0], payload[1], payload[2:]}, nil
}

type catalogAudit struct {
	Complete            bool                     `json:"complete"`
	QuestionsDerivable  bool                     `json:"questions_derivable"`
	TypedDecodeComplete bool                     `json:"typed_decode_complete"`
	Records             uint64                   `json:"records"`
	TypedFailures       uint64                   `json:"typed_payload_decode_failures"`
	Issues              map[string]uint64        `json:"decode_issues"`
	Databases           map[string]uint64        `json:"records_by_db"`
	Examples            []map[string]interface{} `json:"decode_examples,omitempty"`
	Error               string                   `json:"error,omitempty"`
	Scope               string                   `json:"scope"`
}

func auditCatalog(reader io.Reader, limit uint64) catalogAudit {
	r := catalogAudit{Issues: map[string]uint64{}, Databases: map[string]uint64{},
		Scope: "Offline complete-question and typed DNS packet decoding only; no resolver queries or compatibility approval."}
	for {
		item, e := readRecord(reader)
		if e == io.EOF {
			r.Complete = true
			break
		}
		if e != nil {
			r.Error = e.Error()
			break
		}
		r.Records++
		if r.Records > limit {
			r.Error = "Input record limit exceeded"
			break
		}
		_, q, issue, e := decodeBlob(item.Blob)
		if e != nil {
			r.Error = e.Error()
			break
		}
		r.Databases[strconv.Itoa(int(item.DB))]++
		if issue != "" {
			r.TypedFailures++
			r.Issues[issue]++
			if len(r.Examples) < 32 {
				hash := sha256.Sum256(item.Blob)
				r.Examples = append(r.Examples, map[string]interface{}{
					"index_zero_based": r.Records - 1, "db": item.DB, "kind": item.Kind, "qtype": q.Qtype, "qclass": q.Qclass, "blob_sha256": hex.EncodeToString(hash[:]), "decode_issue": issue})
			}
		}
	}
	r.QuestionsDerivable = r.Complete && r.Error == "" && r.Records > 0
	r.TypedDecodeComplete = r.QuestionsDerivable && r.TypedFailures == 0
	return r
}

// A zero-lifetime value cannot become a reusable stale cache entry in the new
// cache insertion path. A CNAME-only intermediate message is also insufficient
// to answer an address query. The loopback upstream deliberately returns
// SERVFAIL: account only for these exact, observed refresh outcomes.
func policyDifference(expected, old, new *dns.Msg, blob []byte) string {
	if new.Rcode != dns.RcodeServerFailure || len(new.Answer) != 0 || len(new.Ns) != 0 || !sameContent(old, expected) {
		return ""
	}
	if len(blob) >= 16 && binary.BigEndian.Uint64(blob[len(blob)-16:]) == binary.BigEndian.Uint64(blob[len(blob)-8:]) {
		return "zero_lifetime_requires_refresh"
	}
	if len(expected.Question) != 1 || expected.Rcode != dns.RcodeSuccess {
		return ""
	}
	qtype := expected.Question[0].Qtype
	if qtype == dns.TypeCNAME || qtype == dns.TypeANY {
		return ""
	}
	cname, terminal := false, false
	for _, rr := range expected.Answer {
		cname = cname || rr.Header().Rrtype == dns.TypeCNAME
		terminal = terminal || rr.Header().Rrtype == qtype
	}
	if cname && !terminal {
		return "cname_intermediate_requires_refresh"
	}
	return ""
}

func dataRRs(rrs []dns.RR) []dns.RR {
	out := make([]dns.RR, 0, len(rrs))
	for _, rr := range rrs {
		if rr.Header().Rrtype != dns.TypeOPT {
			out = append(out, rr)
		}
	}
	return out
}

// IsDuplicate compares owner, type, class and typed RDATA, ignoring TTL and
// presentation case of DNS names. TXT and other opaque strings remain exact.
func sameRRs(left, right []dns.RR) bool {
	left, right = dataRRs(left), dataRRs(right)
	if len(left) != len(right) {
		return false
	}
	used := make([]bool, len(right))
	for _, a := range left {
		found := false
		for j, b := range right {
			if !used[j] && dns.IsDuplicate(a, b) {
				used[j] = true
				found = true
				break
			}
		}
		if !found {
			return false
		}
	}
	return true
}

func sameContent(left, right *dns.Msg) bool {
	return left.Response && right.Response && left.Rcode == right.Rcode &&
		sameRRs(left.Answer, right.Answer) && sameRRs(left.Ns, right.Ns) && sameRRs(left.Extra, right.Extra)
}

func ttlZero(m *dns.Msg) bool {
	for _, rr := range append(append([]dns.RR{}, m.Answer...), m.Ns...) {
		if rr.Header().Rrtype != dns.TypeOPT && rr.Header().Ttl != 0 {
			return false
		}
	}
	return true
}

func endpoint(s string) (string, error) {
	h, p, e := net.SplitHostPort(s)
	if e != nil {
		return "", e
	}
	if h != "127.0.0.1" {
		return "", errors.New("Only an explicit IPv4 loopback endpoint is allowed")
	}
	n, e := strconv.Atoi(p)
	if e != nil || n < 1 || n > 65535 {
		return "", errors.New("Invalid endpoint port")
	}
	return s, nil
}

type client struct {
	conn    *dns.Conn
	address string
}

func newClient(address, transport string) (*client, error) {
	c, e := net.DialTimeout(transport+"4", address, time.Second)
	if e != nil {
		return nil, e
	}
	return &client{conn: &dns.Conn{Conn: c, UDPSize: 65535}, address: address}, nil
}
func (c *client) exchange(q *dns.Msg) (*dns.Msg, bool, error) {
	c.conn.SetDeadline(time.Now().Add(3 * time.Second))
	if e := c.conn.WriteMsg(q); e != nil {
		return nil, false, e
	}
	a, e := c.conn.ReadMsg()
	if e != nil {
		return nil, false, e
	}
	fallback := a.Truncated
	if fallback {
		a, _, e = (&dns.Client{Net: "tcp4", Timeout: 3 * time.Second}).Exchange(q, c.address)
		if e != nil {
			return nil, true, e
		}
	}
	if a.Id != q.Id || !a.Response || len(a.Question) != 1 || !strings.EqualFold(a.Question[0].Name, q.Question[0].Name) || a.Question[0].Qtype != q.Question[0].Qtype || a.Question[0].Qclass != q.Question[0].Qclass || a.Truncated {
		return nil, fallback, errors.New("DNS transaction/question mismatch")
	}
	return a, fallback, nil
}

// Only an input already classified as typed-invalid may use a raw comparison.
// The DNS transaction and complete question still have to match. Byte equality
// is deliberately stricter than semantic RR equality; no TTL, flag, authority,
// name case or opaque data differences are discarded on this path.
func validateWireResponse(packet []byte, q *dns.Msg) error {
	if len(packet) < 17 || len(q.Question) != 1 || binary.BigEndian.Uint16(packet) != q.Id || binary.BigEndian.Uint16(packet[2:4])&0x8000 == 0 || binary.BigEndian.Uint16(packet[4:6]) != 1 {
		return errors.New("Opaque DNS transaction/header mismatch")
	}
	name, end, e := dns.UnpackDomainName(packet, 12)
	if e != nil || end+4 > len(packet) || !strings.EqualFold(name, q.Question[0].Name) || binary.BigEndian.Uint16(packet[end:end+2]) != q.Question[0].Qtype || binary.BigEndian.Uint16(packet[end+2:end+4]) != q.Question[0].Qclass {
		return errors.New("Opaque DNS question mismatch")
	}
	return nil
}

func (c *client) exchangeWire(q *dns.Msg) ([]byte, bool, error) {
	c.conn.SetDeadline(time.Now().Add(3 * time.Second))
	if e := c.conn.WriteMsg(q); e != nil {
		return nil, false, e
	}
	var packet []byte
	if _, udp := c.conn.Conn.(*net.UDPConn); udp {
		packet = make([]byte, 65535)
		n, e := c.conn.Conn.Read(packet)
		if e != nil {
			return nil, false, e
		}
		packet = packet[:n]
	} else {
		var size [2]byte
		if _, e := io.ReadFull(c.conn.Conn, size[:]); e != nil {
			return nil, false, e
		}
		n := binary.BigEndian.Uint16(size[:])
		if n < 17 {
			return nil, false, errors.New("Truncated opaque TCP response")
		}
		packet = make([]byte, n)
		if _, e := io.ReadFull(c.conn.Conn, packet); e != nil {
			return nil, false, e
		}
	}
	if e := validateWireResponse(packet, q); e != nil {
		return nil, false, e
	}
	if binary.BigEndian.Uint16(packet[2:4])&0x0200 != 0 {
		next, e := newClient(c.address, "tcp")
		if e != nil {
			return nil, true, e
		}
		defer next.conn.Close()
		answer, _, e := next.exchangeWireNoFallback(q)
		return answer, true, e
	}
	return packet, false, nil
}

func (c *client) exchangeWireNoFallback(q *dns.Msg) ([]byte, bool, error) {
	// A TCP truncation is never a reason for unbounded recursive fallback.
	c.conn.SetDeadline(time.Now().Add(3 * time.Second))
	if e := c.conn.WriteMsg(q); e != nil {
		return nil, false, e
	}
	var size [2]byte
	if _, e := io.ReadFull(c.conn.Conn, size[:]); e != nil {
		return nil, false, e
	}
	n := binary.BigEndian.Uint16(size[:])
	if n < 17 {
		return nil, false, errors.New("Truncated opaque TCP response")
	}
	packet := make([]byte, n)
	if _, e := io.ReadFull(c.conn.Conn, packet); e != nil {
		return nil, false, e
	}
	if e := validateWireResponse(packet, q); e != nil {
		return nil, false, e
	}
	if binary.BigEndian.Uint16(packet[2:4])&0x0200 != 0 {
		return nil, false, errors.New("Opaque TCP response remains truncated")
	}
	return packet, false, nil
}

func main() {
	input := flag.String("input", "", "Private framed cachedb catalog")
	output := flag.String("output", "", "New result JSON path")
	oldText := flag.String("old", "", "Comma-separated old Unbound loopback endpoints for DB 0,1")
	newText := flag.String("new", "", "Comma-separated new Unbound loopback endpoints for DB 0,1")
	workers := flag.Int("workers", 8, "Bounded independent workers")
	transport := flag.String("transport", "udp", "Explicit DNS transport: udp or tcp")
	auditOnly := flag.Bool("catalog-only", false, "Decode the whole private catalog offline without opening DNS sockets")
	limit := flag.Uint64("max-records", 3000000, "Hard input limit")
	flag.Parse()
	if *input == "" || *output == "" || *workers < 1 || *workers > 32 {
		panic("Explicit input/output and 1..32 workers required")
	}
	if *transport != "udp" && *transport != "tcp" {
		panic("Transport must be udp or tcp")
	}
	if *auditOnly {
		f, e := os.Open(*input)
		if e != nil {
			panic(e)
		}
		defer f.Close()
		out, e := os.OpenFile(*output, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
		if e != nil {
			panic(e)
		}
		defer out.Close()
		r := auditCatalog(bufio.NewReaderSize(f, 1<<20), *limit)
		if e := json.NewEncoder(out).Encode(r); e != nil {
			panic(e)
		}
		fmt.Printf("{\"complete\":%t,\"questions_derivable\":%t,\"typed_decode_complete\":%t,\"records\":%d,\"typed_payload_decode_failures\":%d}\n", r.Complete, r.QuestionsDerivable, r.TypedDecodeComplete, r.Records, r.TypedFailures)
		if !r.QuestionsDerivable {
			os.Exit(1)
		}
		return
	}
	var ports [2][2]string
	for v, text := range []string{*oldText, *newText} {
		parts := strings.Split(text, ",")
		if len(parts) != 2 {
			panic("Exactly two DB endpoints required")
		}
		for db, p := range parts {
			s, e := endpoint(p)
			if e != nil {
				panic(e)
			}
			ports[v][db] = s
		}
	}
	f, e := os.Open(*input)
	if e != nil {
		panic(e)
	}
	defer f.Close()
	out, e := os.OpenFile(*output, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if e != nil {
		panic(e)
	}
	defer out.Close()
	started := time.Now()
	r := result{Totals: freshTotals(), DB: map[string]*totals{}, Transport: *transport,
		Scope: "Old/new real response comparison for every derivable question; a known typed-invalid input uses exact raw response equality after transaction/question checks, with no expected-payload or policy exemption. Historical bytes and lifetimes are unchanged."}
	for db := 0; db < 2; db++ {
		t := freshTotals()
		r.DB[strconv.Itoa(db)] = &t
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Hour)
	defer cancel()
	jobs := make(chan record, *workers*4)
	var wg sync.WaitGroup
	var lock sync.Mutex
	var issued atomic.Uint64
	fail := func(err error, q string) {
		lock.Lock()
		defer lock.Unlock()
		if r.Error == "" {
			r.Error = err.Error()
			s := sha256.Sum256([]byte(q))
			r.QuestionHash = hex.EncodeToString(s[:])
			cancel()
		}
	}
	for w := 0; w < *workers; w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			var clients [2][2]*client
			defer func() {
				for v := 0; v < 2; v++ {
					for db := 0; db < 2; db++ {
						if clients[v][db] != nil {
							clients[v][db].conn.Close()
						}
					}
				}
			}()
			for {
				select {
				case <-ctx.Done():
					return
				case item, ok := <-jobs:
					if !ok {
						return
					}
					expected, expectedQuestion, decodeIssue, e := decodeBlob(item.Blob)
					if e != nil {
						fail(e, "")
						return
					}
					q := &dns.Msg{MsgHdr: dns.MsgHdr{Id: uint16(issued.Add(1)), RecursionDesired: true}}
					q.Question = []dns.Question{expectedQuestion}
					q.Question[0].Name = dns.CanonicalName(q.Question[0].Name)
					q.SetEdns0(65535, true)
					// DB 1 may remain unused for minutes while DB 0 is replayed. Open
					// its connections on first actual use, avoiding idle TCP expiry.
					for v := 0; v < 2; v++ {
						if clients[v][item.DB] == nil {
							c, e := newClient(ports[v][item.DB], *transport)
							if e != nil {
								fail(e, q.Question[0].Name)
								return
							}
							clients[v][item.DB] = c
						}
					}
					if decodeIssue != "" {
						a, at, e := clients[0][item.DB].exchangeWire(q)
						if e != nil {
							fail(fmt.Errorf("Old opaque response: %w", e), q.Question[0].Name)
							return
						}
						b, bt, e := clients[1][item.DB].exchangeWire(q)
						if e != nil {
							fail(fmt.Errorf("New opaque response: %w", e), q.Question[0].Name)
							return
						}
						matched := bytes.Equal(a, b)
						lock.Lock()
						for _, t := range []*totals{&r.Totals, r.DB[strconv.Itoa(int(item.DB))]} {
							t.Checked++
							t.OpaquePayload++
							t.SharedPolicy++
							if at {
								t.TCP++
							}
							if bt {
								t.TCP++
							}
							t.Types[strconv.Itoa(int(q.Question[0].Qtype))]++
							t.Rcodes[strconv.Itoa(int(binary.BigEndian.Uint16(a[2:4])&15))]++
							if binary.BigEndian.Uint64(item.Blob[len(item.Blob)-8:]) <= uint64(time.Now().Unix()) {
								t.Expired++
							}
							if item.Kind == 2 {
								t.MissingTarget++
							}
							if matched {
								t.Matched++
							} else {
								t.Unexplained++
							}
						}
						if len(r.OpaqueExamples) < 32 {
							ha, hb := sha256.Sum256(a), sha256.Sum256(b)
							r.OpaqueExamples = append(r.OpaqueExamples, map[string]interface{}{"db": item.DB, "qtype": q.Question[0].Qtype, "input_decode_issue": decodeIssue, "old_sha256": hex.EncodeToString(ha[:]), "new_sha256": hex.EncodeToString(hb[:]), "old_bytes": len(a), "new_bytes": len(b), "exactly_equal": matched})
						}
						count := r.Totals.Checked
						lock.Unlock()
						if count%10000 == 0 {
							fmt.Printf("{\"checked\":%d}\n", count)
						}
						continue
					}
					a, at, e := clients[0][item.DB].exchange(q)
					if e != nil {
						fail(fmt.Errorf("Old Unbound: %w", e), q.Question[0].Name)
						return
					}
					b, bt, e := clients[1][item.DB].exchange(q)
					if e != nil {
						fail(fmt.Errorf("New Unbound: %w", e), q.Question[0].Name)
						return
					}
					matched := sameContent(a, b) && a.AuthenticatedData == b.AuthenticatedData
					policy := ""
					if !matched && expected != nil {
						policy = policyDifference(expected, a, b, item.Blob)
					}
					lock.Lock()
					for _, t := range []*totals{&r.Totals, r.DB[strconv.Itoa(int(item.DB))]} {
						t.Checked++
						if at {
							t.TCP++
						}
						if bt {
							t.TCP++
						}
						t.Types[strconv.Itoa(int(q.Question[0].Qtype))]++
						t.Rcodes[strconv.Itoa(a.Rcode)]++
						if binary.BigEndian.Uint64(item.Blob[len(item.Blob)-8:]) <= uint64(time.Now().Unix()) {
							t.Expired++
						}
						if matched {
							t.Matched++
						} else if policy != "" {
							t.PolicyDifferences[policy]++
						} else {
							t.Unexplained++
						}
						if decodeIssue != "" {
							t.OpaquePayload++
						}
						if item.Kind == 2 {
							t.MissingTarget++
						} else if expected != nil && sameContent(a, expected) && sameContent(b, expected) {
							t.RetainedPayload++
						} else {
							t.SharedPolicy++
						}
						if ttlZero(b) {
							t.StaleTTLZero++
						} else {
							t.TTLPositive++
						}
					}
					if !matched && policy == "" && len(r.Differences) < 32 {
						hash := sha256.Sum256([]byte(q.Question[0].Name))
						expectedRcode, expectedAnswers := -1, -1
						if expected != nil {
							expectedRcode = expected.Rcode
							expectedAnswers = len(expected.Answer)
						}
						r.Differences = append(r.Differences, map[string]interface{}{"question_sha256": hex.EncodeToString(hash[:]), "db": item.DB, "kind": item.Kind, "qtype": q.Question[0].Qtype, "old_rcode": a.Rcode, "new_rcode": b.Rcode, "expected_rcode": expectedRcode, "old_answers": len(a.Answer), "new_answers": len(b.Answer), "expected_answers": expectedAnswers, "typed_payload_decode_failed": expected == nil, "original_lifetime": binary.BigEndian.Uint64(item.Blob[len(item.Blob)-8:]) - binary.BigEndian.Uint64(item.Blob[len(item.Blob)-16:])})
					}
					count := r.Totals.Checked
					lock.Unlock()
					if count%10000 == 0 {
						fmt.Printf("{\"checked\":%d}\n", count)
					}
				}
			}
		}()
	}
	reader := bufio.NewReaderSize(f, 1<<20)
readLoop:
	for {
		item, e := readRecord(reader)
		if e == io.EOF {
			r.Complete = true
			break
		}
		if e != nil {
			fail(e, "")
			break
		}
		r.Input++
		if r.Input > *limit {
			fail(errors.New("Input record limit exceeded"), "")
			break
		}
		select {
		case jobs <- item:
		case <-ctx.Done():
			break readLoop
		}
	}
	close(jobs)
	wg.Wait()
	if ctx.Err() != nil && r.Error == "" {
		r.Error = ctx.Err().Error()
	}
	r.Elapsed = time.Since(started).Seconds()
	r.Passed = r.Complete && r.Error == "" && r.Input > 0 && r.Input == r.Totals.Checked && r.Totals.Unexplained == 0
	json.NewEncoder(out).Encode(r)
	fmt.Printf("{\"passed\":%t,\"checked\":%d}\n", r.Passed, r.Totals.Checked)
	if !r.Passed {
		os.Exit(1)
	}
}
