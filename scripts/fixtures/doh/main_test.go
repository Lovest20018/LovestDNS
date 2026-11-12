package main

import (
	"bytes"
	"crypto/tls"
	"encoding/binary"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func question(name string, kind uint16) []byte {
	packet := make([]byte, 12)
	binary.BigEndian.PutUint16(packet[0:2], 0x1234)
	binary.BigEndian.PutUint16(packet[2:4], 0x0100)
	binary.BigEndian.PutUint16(packet[4:6], 1)
	if name != "" {
		for _, label := range strings.Split(name, ".") {
			packet = append(packet, byte(len(label)))
			packet = append(packet, label...)
		}
	}
	packet = append(packet, 0, 0, 0, 0, 1)
	binary.BigEndian.PutUint16(packet[len(packet)-4:len(packet)-2], kind)
	return packet
}

func TestAnswerAndInitializationProbes(t *testing.T) {
	for _, kind := range []uint16{1, 28} {
		q := question("MiXeD.cache.test", kind)
		r, err := answer(q)
		if err != nil || binary.BigEndian.Uint16(r[6:8]) != 1 || !bytes.Equal(q[12:], r[12:len(q)]) {
			t.Fatalf("question or answer count changed: %x %v", r, err)
		}
		if kind == 1 && !bytes.Equal(r[len(r)-4:], []byte{192, 0, 2, 10}) {
			t.Fatal("wrong A")
		}
		if kind == 28 && len(r)-len(q) != 28 {
			t.Fatal("wrong AAAA length")
		}
	}
	for _, name := range []string{"", "probe.test.dnscrypt"} {
		r, err := answer(question(name, 2))
		if err != nil || binary.BigEndian.Uint16(r[6:8]) != 0 {
			t.Fatal("invalid initialization probe")
		}
		if name != "" && r[3]&15 != 3 {
			t.Fatal("probe requires NXDOMAIN")
		}
	}
}

func TestMalformedOrUncontrolledQuestionsAreRejected(t *testing.T) {
	q := question("a.cache.test", 1)
	for _, packet := range [][]byte{q[:13], question("example.com", 1), question("a.cache.test", 15),
		append(append([]byte(nil), q[:12]...), 0xc0, 12, 0, 1, 0, 1)} {
		if _, err := answer(packet); err == nil {
			t.Fatalf("accepted uncontrolled query: %x", packet)
		}
	}
}

func TestNegotiatedProtocolAndSourceAreEnforced(t *testing.T) {
	for _, protocol := range []string{"h1", "h2"} {
		f := newFixture(protocol, "direct")
		server := httptest.NewUnstartedServer(f)
		server.EnableHTTP2 = protocol == "h2"
		server.StartTLS()
		client := server.Client()
		if protocol == "h1" {
			client.Transport.(*http.Transport).TLSClientConfig.NextProtos = []string{"http/1.1"}
		}
		// Verify the actual server and client negotiate the required protocol.
		response, err := client.Post(server.URL+"/dns-query", "application/dns-message",
			bytes.NewReader(question("a.cache.test", 1)))
		if err != nil {
			server.Close()
			t.Fatal(err)
		}
		io.Copy(io.Discard, response.Body)
		response.Body.Close()
		f.mu.Lock()
		requests := f.stats.Requests
		f.mu.Unlock()
		if response.StatusCode != 200 || requests != 1 {
			server.Close()
			t.Fatal("TLS protocol was not accepted")
		}
		f.mu.Lock()
		f.route = "proxy"
		f.mu.Unlock()
		// Then prove that a real TLS request on the wrong source is rejected.
		response, err = client.Post(server.URL+"/dns-query", "application/dns-message",
			bytes.NewReader(question("a.cache.test", 1)))
		if err != nil {
			server.Close()
			t.Fatal(err)
		}
		io.Copy(io.Discard, response.Body)
		response.Body.Close()
		f.mu.Lock()
		rejected := f.stats.Rejected
		f.mu.Unlock()
		if response.StatusCode != 403 || rejected != 1 {
			t.Fatal("direct request bypassed proxy route")
		}
		server.Close()
		// Exercise the exact wrong-ALPN rejection independently of HTTP negotiation.
		r := httptest.NewRequest(http.MethodPost, "/dns-query", bytes.NewReader(question("a.cache.test", 1)))
		r.RemoteAddr, r.Proto = "127.0.0.2:1234", "HTTP/2.0"
		r.TLS = &tls.ConnectionState{NegotiatedProtocol: "http/1.1"}
		w := httptest.NewRecorder()
		f.ServeHTTP(w, r)
		if w.Code != 403 {
			t.Fatal("protocol fallback accepted")
		}
	}
}
