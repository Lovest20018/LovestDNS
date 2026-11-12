// Standalone contract for the actual udpListener function extracted from proxy.go.
// Hold all asynchronous processors until the scratch buffer has been overwritten.
package main

import (
	"bytes"
	"fmt"
	"net"
	"testing"
	"time"
)

const MaxDNSPacketSize = 4096

type XTransport struct{ mainProto string }
type Proxy struct {
	xTransport *XTransport
	maxClients int
	arrived    chan struct{}
	release    chan struct{}
	packets    chan []byte
	benchmark  bool
}

var dlog = struct{ Warnf, Debugf func(string, ...any) }{
	func(string, ...any) {}, func(string, ...any) {},
}

func (*Proxy) clientsCountInc() bool { return true }
func (*Proxy) clientsCountDec()      {}
func (p *Proxy) processIncomingQuery(_, _ string, packet []byte, _ *net.Addr, _ *net.UDPConn, _ time.Time, _ bool) {
	if p.benchmark {
		p.packets <- packet
		return
	}
	p.arrived <- struct{}{}
	<-p.release
	p.packets <- append([]byte(nil), packet...)
}

// Exercise the extracted listener rather than a second implementation of its
// allocation logic. Includes loopback and goroutine costs; not a DNS CPU claim.
func BenchmarkUDPListenerAllocations(b *testing.B) {
	for _, size := range []int{32, 63, 255, 512, MaxDNSPacketSize - 1} {
		b.Run(fmt.Sprint(size), func(b *testing.B) {
			listener, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
			if err != nil {
				b.Fatal(err)
			}
			sender, err := net.DialUDP("udp4", nil, listener.LocalAddr().(*net.UDPAddr))
			if err != nil {
				listener.Close()
				b.Fatal(err)
			}
			defer sender.Close()
			p := &Proxy{xTransport: &XTransport{}, maxClients: 250,
				benchmark: true, packets: make(chan []byte, 1)}
			done := make(chan struct{})
			go func() { p.udpListener(listener); close(done) }()
			defer func() { listener.Close(); <-done }()
			packet := bytes.Repeat([]byte{0x55}, size)
			b.ReportAllocs()
			b.ResetTimer()
			for i := 0; i < b.N; i++ {
				if _, err := sender.Write(packet); err != nil {
					b.Fatal(err)
				}
				if got := <-p.packets; !bytes.Equal(got, packet) {
					b.Fatal("corrupted packet")
				}
			}
			b.StopTimer()
		})
	}
}

func TestAsynchronousQueriesOwnTheirPacketBytes(t *testing.T) {
	const count = 32
	listener, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	sender, err := net.DialUDP("udp4", nil, listener.LocalAddr().(*net.UDPAddr))
	if err != nil {
		t.Fatal(err)
	}
	defer sender.Close()
	p := &Proxy{xTransport: &XTransport{}, maxClients: 250, arrived: make(chan struct{}, count),
		release: make(chan struct{}), packets: make(chan []byte, count)}
	defer func() {
		select {
		case <-p.release:
		default:
			close(p.release)
		}
	}()
	done := make(chan struct{})
	go func() { p.udpListener(listener); close(done) }()
	expected := make(map[byte][]byte)
	for i := 0; i < count; i++ {
		size := []int{32, 63, 255, 512, MaxDNSPacketSize - 1}[i%5]
		packet := bytes.Repeat([]byte{byte(i + 1)}, size)
		expected[byte(i+1)] = packet
		if _, err := sender.Write(packet); err != nil {
			t.Fatal(err)
		}
		// Ensure this packet has been read before sending the next. Processors
		// still cannot inspect their packet until every receive has completed.
		select {
		case <-p.arrived:
		case <-time.After(2 * time.Second):
			t.Fatal("reader did not accept packet")
		}
	}
	close(p.release)
	for i := 0; i < count; i++ {
		select {
		case packet := <-p.packets:
			if len(packet) == 0 || !bytes.Equal(packet, expected[packet[0]]) {
				t.Fatal("packet bytes were reused or truncated")
			}
			delete(expected, packet[0])
		case <-time.After(2 * time.Second):
			t.Fatal("asynchronous processor did not finish")
		}
	}
	if len(expected) != 0 {
		t.Fatal("missing or duplicated query")
	}
	listener.Close()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("reader did not stop")
	}
}
