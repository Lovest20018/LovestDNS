package main

import (
	"encoding/binary"
	"testing"
	"time"
)

func TestDelayedSlotStartsANewPhase(t *testing.T) {
	start := time.Unix(100, 0)
	period := 10 * time.Millisecond
	for _, delay := range []time.Duration{0, time.Millisecond, 4 * time.Millisecond, 6 * time.Millisecond, 600 * time.Millisecond} {
		actual := start.Add(delay)
		next := nextDeadline(start, actual, period)
		if next.Sub(actual) < period/2 {
			t.Fatal("missed slot would be caught up too quickly")
		}
		if delay > period/2 && next != actual.Add(period) {
			t.Fatal("late sender did not start a new phase")
		}
	}
}

func TestQuestionEncodingAndScope(t *testing.T) {
	for _, kind := range []string{"A", "AAAA"} {
		packet, name, err := question("abc.cache.test "+kind, 42)
		if err != nil || name != "abc.cache.test" || binary.BigEndian.Uint16(packet[:2]) != 42 {
			t.Fatal("invalid question")
		}
		want := uint16(1)
		if kind == "AAAA" {
			want = 28
		}
		if binary.BigEndian.Uint16(packet[len(packet)-4:]) != want {
			t.Fatal("wrong question type")
		}
	}
	for _, line := range []string{"example.com A", "a..cache.test A", "a.cache.test TXT", "a.cache.test"} {
		if _, _, err := question(line, 0); err == nil {
			t.Fatal("uncontrolled question accepted")
		}
	}
}
