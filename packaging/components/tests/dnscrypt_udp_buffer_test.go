// Standalone Linux socket contract. Copy beside the selected upstream
// setsockopts_linux.go and run: go test -v setsockopts_linux.go dnscrypt_udp_buffer_test.go
// The stub supplies only the receiver type; listener code comes from upstream.
package main

import (
	"context"
	"net"
	"syscall"
	"testing"
	"time"
)

type Proxy struct{}

func TestUDPListenerRetainsBoundedBurst(t *testing.T) {
	config, err := (&Proxy{}).udpListenerConfig()
	if err != nil {
		t.Fatal(err)
	}
	listener, err := config.ListenPacket(context.Background(), "udp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	udp := listener.(*net.UDPConn)
	control, err := udp.SyscallConn()
	if err != nil {
		t.Fatal(err)
	}
	var buffer int
	var optionErr error
	if err := control.Control(func(fd uintptr) {
		buffer, optionErr = syscall.GetsockoptInt(int(fd), syscall.SOL_SOCKET, syscall.SO_RCVBUF)
	}); err != nil {
		t.Fatal(err)
	}
	if optionErr != nil {
		t.Fatal(optionErr)
	}
	sender, err := net.DialUDP("udp4", nil, udp.LocalAddr().(*net.UDPAddr))
	if err != nil {
		t.Fatal(err)
	}
	defer sender.Close()
	const packets = 128
	payload := make([]byte, 256)
	// Deliberately do not drain the receiver until the whole finite burst is
	// queued. This reproduces a paused worker without depending on VM timing.
	for i := 0; i < packets; i++ {
		payload[0] = byte(i)
		if _, err := sender.Write(payload); err != nil {
			t.Fatal(err)
		}
	}
	if err := udp.SetReadDeadline(time.Now().Add(time.Second)); err != nil {
		t.Fatal(err)
	}
	received, seen := 0, make(map[byte]bool)
	for received < packets {
		packet := make([]byte, 512)
		n, _, err := udp.ReadFromUDP(packet)
		if timeout, ok := err.(net.Error); ok && timeout.Timeout() {
			break
		}
		if err != nil {
			t.Fatal(err)
		}
		if n != len(payload) || seen[packet[0]] {
			t.Fatalf("invalid or duplicated packet %d", received)
		}
		seen[packet[0]] = true
		received++
	}
	t.Logf("SO_RCVBUF=%d burst=%d received=%d lost=%d", buffer, packets, received, packets-received)
	if received != packets {
		t.Fatalf("receive queue discarded %d of %d packets", packets-received, packets)
	}
}
