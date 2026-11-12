package main

import (
	"bytes"
	"encoding/binary"
	"github.com/miekg/dns"
	"net"
	"testing"
)

func syntheticBlob(t *testing.T, malformed bool) []byte {
	t.Helper()
	m := &dns.Msg{MsgHdr: dns.MsgHdr{Response: true, Rcode: dns.RcodeNameError},
		Question: []dns.Question{{Name: "missing.cache.test.", Qtype: dns.TypeAAAA, Qclass: dns.ClassINET}},
		Ns:       []dns.RR{rr(t, "cache.test. 1 IN SOA ns.cache.test. host.cache.test. 1 2 3 4 5")}}
	packet, e := m.Pack()
	if e != nil {
		t.Fatal(e)
	}
	if malformed {
		_, end, e := dns.UnpackDomainName(packet, 12)
		if e != nil {
			t.Fatal(e)
		}
		_, end, e = dns.UnpackDomainName(packet, end+4)
		if e != nil {
			t.Fatal(e)
		}
		rdlength := binary.BigEndian.Uint16(packet[end+8 : end+10])
		binary.BigEndian.PutUint16(packet[end+8:end+10], rdlength+2)
		packet = append(packet, 0, 0)
	}
	return append(packet, make([]byte, 16)...)
}

func frame(item record) []byte {
	data := make([]byte, 6)
	binary.BigEndian.PutUint32(data, uint32(len(item.Blob)+2))
	data[4], data[5] = item.DB, item.Kind
	return append(data, item.Blob...)
}

func TestTypedDecodeFailurePreservesQuestionWithoutInventingExpectedAnswer(t *testing.T) {
	for _, malformed := range []bool{false, true} {
		expected, q, issue, e := decodeBlob(syntheticBlob(t, malformed))
		if e != nil || q.Name != "missing.cache.test." || q.Qtype != dns.TypeAAAA || q.Qclass != dns.ClassINET {
			t.Fatal("Question derivation failed", q, e)
		}
		if malformed {
			if expected != nil || issue == "" {
				t.Fatal("An undecodable SOA cannot supply a semantic reference answer")
			}
		} else if expected == nil || issue != "" {
			t.Fatal("Valid SOA should decode", issue)
		}
	}
}

func TestOfflineAuditAccountsForOpaquePayloadsAndBothDatabases(t *testing.T) {
	data := append(frame(record{0, 1, syntheticBlob(t, false)}), frame(record{1, 1, syntheticBlob(t, true)})...)
	r := auditCatalog(bytes.NewReader(data), 10)
	if !r.Complete || !r.QuestionsDerivable || r.TypedDecodeComplete || r.Records != 2 || r.TypedFailures != 1 || r.Databases["0"] != 1 || r.Databases["1"] != 1 {
		t.Fatal(r)
	}
	if len(r.Examples) != 1 || r.Examples[0]["index_zero_based"] != uint64(1) {
		t.Fatal("Malformed input must retain a stable private catalog index", r.Examples)
	}
}

func TestAuditRejectsTruncatedFramesAndIncompleteQuestions(t *testing.T) {
	valid := frame(record{0, 1, syntheticBlob(t, false)})
	for _, data := range [][]byte{valid[:4], valid[:len(valid)-1], valid[:3]} {
		r := auditCatalog(bytes.NewReader(data), 10)
		if r.Complete || r.QuestionsDerivable || r.Error == "" {
			t.Fatal("Truncated catalog cannot complete", r)
		}
	}
	bad := syntheticBlob(t, false)
	binary.BigEndian.PutUint16(bad[4:6], 2)
	if _, _, _, e := decodeBlob(bad); e == nil {
		t.Fatal("A cached message with another question count cannot be queried")
	}
	r := auditCatalog(bytes.NewReader(valid), 0)
	if r.QuestionsDerivable || r.Error == "" {
		t.Fatal("Input limit must apply during preflight", r)
	}
}

func TestRawTCPPathRetainsKnownInvalidRDATAAndChecksTransaction(t *testing.T) {
	blob := syntheticBlob(t, true)
	packet := append([]byte(nil), blob[:len(blob)-16]...)
	q := &dns.Msg{MsgHdr: dns.MsgHdr{Id: 42, RecursionDesired: true}, Question: []dns.Question{{Name: "missing.cache.test.", Qtype: dns.TypeAAAA, Qclass: dns.ClassINET}}}
	binary.BigEndian.PutUint16(packet, 42)
	var parsed dns.Msg
	if parsed.Unpack(packet) == nil {
		t.Fatal("Fixture must retain invalid SOA RDATA")
	}
	left, right := net.Pipe()
	defer left.Close()
	done := make(chan error, 1)
	go func() {
		defer right.Close()
		conn := &dns.Conn{Conn: right}
		if _, e := conn.ReadMsg(); e != nil {
			done <- e
			return
		}
		reply := make([]byte, 2)
		binary.BigEndian.PutUint16(reply, uint16(len(packet)))
		_, e := right.Write(append(reply, packet...))
		done <- e
	}()
	c := &client{conn: &dns.Conn{Conn: left}, address: "127.0.0.1:5301"}
	got, fallback, e := c.exchangeWire(q)
	if e != nil || fallback || !bytes.Equal(got, packet) {
		t.Fatal("Raw input was lost or changed", e, fallback)
	}
	if e := <-done; e != nil {
		t.Fatal(e)
	}
	for _, fault := range []string{"id", "question", "truncated"} {
		changed := append([]byte(nil), packet...)
		switch fault {
		case "id":
			changed[0] ^= 1
		case "question":
			changed[13] = 'z'
		case "truncated":
			changed = changed[:16]
		}
		if validateWireResponse(changed, q) == nil {
			t.Fatal("Opaque responses still require exact transaction/question framing", fault)
		}
	}
}

func rr(t *testing.T, s string) dns.RR {
	t.Helper()
	r, e := dns.NewRR(s)
	if e != nil {
		t.Fatal(e)
	}
	return r
}

func TestSemanticRecordsIgnoreTTLAndDNSNameCaseButPreserveOpaqueData(t *testing.T) {
	a := rr(t, "Owner.example. 600 IN MX 10 MAIL.example.")
	b := rr(t, "owner.EXAMPLE. 0 IN MX 10 mail.EXAMPLE.")
	if !sameRRs([]dns.RR{a}, []dns.RR{b}) {
		t.Fatal("DNS name case and TTL should not alter content")
	}
	c := rr(t, "owner.example. 0 IN MX 20 mail.example.")
	if sameRRs([]dns.RR{a}, []dns.RR{c}) {
		t.Fatal("MX preference must remain exact")
	}
	if sameRRs([]dns.RR{rr(t, `x.example. 0 IN TXT "Case"`)}, []dns.RR{rr(t, `x.example. 0 IN TXT "case"`)}) {
		t.Fatal("TXT bytes must remain exact")
	}
}

func TestRefreshClassificationRequiresExactObservedConditions(t *testing.T) {
	expected := &dns.Msg{MsgHdr: dns.MsgHdr{Response: true}, Question: []dns.Question{{Name: "x.example.", Qtype: dns.TypeA, Qclass: dns.ClassINET}}, Answer: []dns.RR{rr(t, "x.example. 0 IN A 192.0.2.1")}}
	failed := &dns.Msg{MsgHdr: dns.MsgHdr{Response: true, Rcode: dns.RcodeServerFailure}}
	blob := make([]byte, 16)
	binary.BigEndian.PutUint64(blob[:8], 100)
	binary.BigEndian.PutUint64(blob[8:], 100)
	if policyDifference(expected, expected, failed, blob) != "zero_lifetime_requires_refresh" {
		t.Fatal("Zero lifetime must be classified")
	}
	binary.BigEndian.PutUint64(blob[8:], 120)
	if policyDifference(expected, expected, failed, blob) != "" {
		t.Fatal("Positive original lifetime cannot be waived")
	}
	expected.Answer = []dns.RR{rr(t, "x.example. 0 IN CNAME target.example.")}
	if policyDifference(expected, expected, failed, blob) != "cname_intermediate_requires_refresh" {
		t.Fatal("CNAME-only address lookup requires completion")
	}
	expected.Question[0].Qtype = dns.TypeCNAME
	if policyDifference(expected, expected, failed, blob) != "" {
		t.Fatal("CNAME query has a terminal answer")
	}
	expected.Question[0].Qtype = dns.TypeA
	changed := expected.Copy()
	changed.Answer = []dns.RR{rr(t, "x.example. 0 IN CNAME another.example.")}
	if policyDifference(expected, changed, failed, blob) != "" {
		t.Fatal("An old answer with different data cannot be waived")
	}
}

func TestUnorderedSetsKeepMultiplicityAndNegativeAuthority(t *testing.T) {
	a := rr(t, "x.example. 60 IN A 192.0.2.1")
	b := rr(t, "x.example. 60 IN A 192.0.2.2")
	if !sameRRs([]dns.RR{a, b}, []dns.RR{b, a}) {
		t.Fatal("RR ordering is immaterial")
	}
	if sameRRs([]dns.RR{a, b}, []dns.RR{a, a}) {
		t.Fatal("Cannot replace a record with a duplicate")
	}
	x := &dns.Msg{MsgHdr: dns.MsgHdr{Response: true, Rcode: dns.RcodeNameError}, Ns: []dns.RR{rr(t, "example. 60 IN SOA ns.example. host.example. 1 2 3 4 5")}}
	y := x.Copy()
	y.Ns[0] = rr(t, "example. 0 IN SOA ns.example. host.example. 2 2 3 4 5")
	if sameContent(x, y) {
		t.Fatal("Negative authority SOA data must remain exact")
	}
	y = x.Copy()
	y.Rcode = dns.RcodeSuccess
	if sameContent(x, y) {
		t.Fatal("NXDOMAIN and NODATA are distinct")
	}
}

func TestLoopbackBoundaryAndStaleTTL(t *testing.T) {
	for _, s := range []string{"example.com:53", "192.0.2.1:53", "127.0.0.1:0", "[::1]:53"} {
		if _, e := endpoint(s); e == nil {
			t.Fatal("Unexpected endpoint accepted", s)
		}
	}
	if _, e := endpoint("127.0.0.1:5301"); e != nil {
		t.Fatal(e)
	}
	x := &dns.Msg{Answer: []dns.RR{rr(t, "x.example. 0 IN A 192.0.2.1")}}
	if !ttlZero(x) {
		t.Fatal("Expired response TTL should be zero")
	}
	x.Answer[0].Header().Ttl = 60
	if ttlZero(x) {
		t.Fatal("Fresh TTL should be reported")
	}
}
