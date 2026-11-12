// Disposable, loopback-only DoH fixture. It never forwards a DNS question.
package main

import (
	"crypto/tls"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"strings"
	"sync"
	"time"
)

type counts struct {
	Requests    int            `json:"requests"`
	Connections int            `json:"connections"`
	Rejected    int            `json:"rejected"`
	DNSErrors   int            `json:"dns_errors"`
	Routes      map[string]int `json:"routes"`
	Protocols   map[string]int `json:"protocols"`
	ALPN        map[string]int `json:"alpn"`
}

type fixture struct {
	mu       sync.Mutex
	protocol string
	route    string
	stats    counts
}

func newFixture(protocol, route string) *fixture {
	return &fixture{protocol: protocol, route: route, stats: counts{
		Routes: map[string]int{}, Protocols: map[string]int{}, ALPN: map[string]int{},
	}}
}

func answer(packet []byte) ([]byte, error) {
	if len(packet) < 17 || binary.BigEndian.Uint16(packet[2:4])&0xf800 != 0 ||
		binary.BigEndian.Uint16(packet[4:6]) != 1 {
		return nil, errors.New("expected one ordinary DNS question")
	}
	end := 12
	labels := []string{}
	for {
		if end >= len(packet) || end-12 > 254 {
			return nil, errors.New("truncated or oversized name")
		}
		size := int(packet[end])
		end++
		if size == 0 {
			break
		}
		if size > 63 || end+size > len(packet) {
			return nil, errors.New("invalid or compressed fixture question")
		}
		labels = append(labels, string(packet[end:end+size]))
		end += size
	}
	if end+4 > len(packet) {
		return nil, errors.New("truncated question type")
	}
	kind, class := binary.BigEndian.Uint16(packet[end:end+2]), binary.BigEndian.Uint16(packet[end+2:end+4])
	name := strings.ToLower(strings.Join(labels, "."))
	probe := class == 1 && kind == 2 && (name == "" || strings.HasSuffix(name, ".test.dnscrypt"))
	if !probe && (class != 1 || (kind != 1 && kind != 28) || !strings.HasSuffix(name, ".cache.test")) {
		return nil, errors.New("only cache.test A/AAAA and DNSCrypt initialization probes are supported")
	}
	value := net.ParseIP("192.0.2.10").To4()
	if kind == 28 {
		value = net.ParseIP("2001:db8::10").To16()
	}
	response := make([]byte, 12, end+4+12+len(value))
	copy(response[:2], packet[:2])
	binary.BigEndian.PutUint16(response[2:4], 0x8080|(binary.BigEndian.Uint16(packet[2:4])&0x0100))
	binary.BigEndian.PutUint16(response[4:6], 1)
	if probe {
		if name != "" {
			response[3] |= 3
		}
		return append(response, packet[12:end+4]...), nil
	}
	binary.BigEndian.PutUint16(response[6:8], 1)
	response = append(response, packet[12:end+4]...)
	rr := []byte{0xc0, 0x0c, 0, 0, 0, 1, 0, 0, 2, 88, 0, byte(len(value))}
	binary.BigEndian.PutUint16(rr[2:4], kind)
	response = append(response, rr...)
	return append(response, value...), nil
}

func (f *fixture) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	source, _, _ := net.SplitHostPort(r.RemoteAddr)
	route := "direct"
	if source == "127.0.0.2" {
		route = "proxy"
	}
	alpn := ""
	if r.TLS != nil {
		alpn = r.TLS.NegotiatedProtocol
	}
	expectedProto, expectedALPN := "HTTP/1.1", "http/1.1"
	if f.protocol == "h2" {
		expectedProto, expectedALPN = "HTTP/2.0", "h2"
	}
	f.mu.Lock()
	f.stats.Routes[route]++
	f.stats.Protocols[r.Proto]++
	f.stats.ALPN[alpn]++
	allowed := source == "127.0.0.1" || source == "127.0.0.2"
	allowed = allowed && (route == f.route || f.route == "both") && r.Proto == expectedProto && alpn == expectedALPN
	if !allowed {
		f.stats.Rejected++
	}
	f.mu.Unlock()
	if !allowed {
		http.Error(w, "unexpected route or negotiated protocol", http.StatusForbidden)
		return
	}
	if r.URL.Path != "/dns-query" {
		http.NotFound(w, r)
		return
	}
	var packet []byte
	var err error
	switch r.Method {
	case http.MethodGet:
		packet, err = base64.RawURLEncoding.DecodeString(r.URL.Query().Get("dns"))
	case http.MethodPost:
		if r.Header.Get("Content-Type") != "application/dns-message" {
			http.Error(w, "DNS message required", http.StatusUnsupportedMediaType)
			return
		}
		packet, err = io.ReadAll(io.LimitReader(r.Body, 65536))
	default:
		http.Error(w, "GET or POST required", http.StatusMethodNotAllowed)
		return
	}
	var response []byte
	if err == nil {
		response, err = answer(packet)
	}
	if err != nil {
		f.mu.Lock()
		f.stats.DNSErrors++
		f.mu.Unlock()
		http.Error(w, "invalid fixture question", http.StatusBadRequest)
		return
	}
	f.mu.Lock()
	f.stats.Requests++
	f.mu.Unlock()
	w.Header().Set("Content-Type", "application/dns-message")
	w.Header().Set("Content-Length", fmt.Sprint(len(response)))
	w.Write(response)
}

func main() {
	cert, key := flag.String("cert", "", "disposable certificate"), flag.String("key", "", "disposable key")
	protocol := flag.String("protocol", "h2", "h1 or h2; fallback is rejected")
	route := flag.String("route", "direct", "direct, proxy or both source addresses")
	flag.Parse()
	if (*protocol != "h1" && *protocol != "h2") || (*route != "direct" && *route != "proxy" && *route != "both") {
		log.Fatal("invalid protocol or route")
	}
	f := newFixture(*protocol, *route)
	listener, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		log.Fatal(err)
	}
	control, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		log.Fatal(err)
	}
	server := &http.Server{Handler: f, ReadHeaderTimeout: 5 * time.Second, IdleTimeout: 30 * time.Second,
		TLSConfig: &tls.Config{MinVersion: tls.VersionTLS12},
		ConnState: func(_ net.Conn, state http.ConnState) {
			if state == http.StateNew {
				f.mu.Lock()
				f.stats.Connections++
				f.mu.Unlock()
			}
		}}
	if *protocol == "h1" {
		server.TLSConfig.NextProtos = []string{"http/1.1"}
		server.TLSNextProto = map[string]func(*http.Server, *tls.Conn, http.Handler){}
	}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /state", func(w http.ResponseWriter, r *http.Request) {
		f.mu.Lock()
		defer f.mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(f.stats)
	})
	go func() {
		if err := (&http.Server{Handler: mux, ReadHeaderTimeout: 5 * time.Second}).Serve(control); err != nil {
			log.Fatal(err)
		}
	}()
	json.NewEncoder(log.Writer()).Encode(map[string]string{
		"https": listener.Addr().String(), "state": control.Addr().String(), "protocol": *protocol, "route": *route})
	log.Fatal(server.ServeTLS(listener, *cert, *key))
}
