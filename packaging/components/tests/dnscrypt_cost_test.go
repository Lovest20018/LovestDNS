// Named-file benchmark against the extracted, unmodified upstream cache code.
// Read process CPU separately from wall time: VM pauses still affect ns/op.
package main

import (
	"encoding/binary"
	"fmt"
	"strings"
	"syscall"
	"testing"
	"time"
)

var costSinkKey [32]byte
var costSinkPacket []byte
var costSinkMsg *costMsg

func costWire(name string, kind uint16, response bool) []byte {
	packet := make([]byte, 12)
	binary.BigEndian.PutUint16(packet[:2], 42)
	binary.BigEndian.PutUint16(packet[2:4], 0x0100)
	binary.BigEndian.PutUint16(packet[4:6], 1)
	for _, label := range strings.Split(name, ".") {
		packet = append(packet, byte(len(label)))
		packet = append(packet, label...)
	}
	packet = append(packet, 0, 0, 0, 0, 1)
	binary.BigEndian.PutUint16(packet[len(packet)-4:], kind)
	if response {
		binary.BigEndian.PutUint16(packet[2:4], 0x8180)
		binary.BigEndian.PutUint16(packet[6:8], 1)
		value := []byte{192, 0, 2, 10}
		if kind == 28 {
			value = []byte{0x20, 1, 0xd, 0xb8, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0x10}
		}
		rr := []byte{0xc0, 12, 0, 0, 0, 1, 0, 0, 2, 88, 0, byte(len(value))}
		binary.BigEndian.PutUint16(rr[2:4], kind)
		packet = append(packet, rr...)
		packet = append(packet, value...)
	}
	return packet
}

func costCPU() int64 {
	var usage syscall.Rusage
	if err := syscall.Getrusage(syscall.RUSAGE_SELF, &usage); err != nil {
		panic(err)
	}
	return usage.Utime.Nano() + usage.Stime.Nano()
}

func costMeasure(b *testing.B, run func()) {
	b.ReportAllocs()
	b.ResetTimer()
	start := costCPU()
	for i := 0; i < b.N; i++ {
		run()
	}
	end := costCPU()
	b.StopTimer()
	b.ReportMetric(float64(end-start)/float64(b.N), "cpu-ns/op")
}

func TestCostRoundTripAndClone(t *testing.T) {
	for _, kind := range []uint16{1, 28} {
		original := costUnpack(costWire("MiXeD.cache.test", kind, true))
		copy := costClone(original)
		costChangeTTL(copy, 7)
		if costTTL(original) != 600 || costTTL(copy) != 7 {
			t.Fatal("clone aliases its source RR")
		}
		packet := costPack(copy)
		decoded := costUnpack(packet)
		if costTTL(decoded) != 7 || len(decoded.Answer) != 1 {
			t.Fatal("invalid wire roundtrip")
		}
		if binary.BigEndian.Uint16(packet[:2]) != 42 {
			t.Fatal("transaction ID changed")
		}
	}
}

func TestCostCacheKeyIsolation(t *testing.T) {
	state := costTTLState()
	a := costUnpack(costWire("MiXeD.cache.test", 1, false))
	lower := costUnpack(costWire("mixed.cache.test", 1, false))
	key := computeCacheKey(state, a)
	if key != computeCacheKey(state, lower) {
		t.Fatal("case normalization changed")
	}
	state.dnssec = true
	if key == computeCacheKey(state, a) {
		t.Fatal("DNSSEC bit is not isolated")
	}
	state.dnssec = false
	if key == computeCacheKey(state, costUnpack(costWire("mixed.cache.test", 28, false))) {
		t.Fatal("type collision")
	}
	classWire := costWire("mixed.cache.test", 1, false)
	binary.BigEndian.PutUint16(classWire[len(classWire)-2:], 3)
	if key == computeCacheKey(state, costUnpack(classWire)) {
		t.Fatal("class collision")
	}
	costSetCD(a, true)
	isIsolated := key != computeCacheKey(state, a)
	if isIsolated != costCDIsolated {
		t.Fatal("unexpected source version CD behavior")
	}
	t.Logf("CheckingDisabled isolated: %v (observed baseline behavior; no correctness rollback)", isIsolated)
}

func TestCostCacheReadOwnsItsResponse(t *testing.T) {
	costReset()
	state := costTTLState()
	writer, reader := new(PluginCacheResponse), new(PluginCache)
	response := costUnpack(costWire("answer.cache.test", 1, true))
	if err := writer.Eval(state, response); err != nil {
		t.Fatal(err)
	}
	q := costUnpack(costWire("AnSwEr.cache.test", 1, false))
	if err := reader.Eval(state, q); err != nil {
		t.Fatal(err)
	}
	if !state.cacheHit || state.action != PluginsActionSynth || state.synthResponse == nil {
		t.Fatal("expected real cache hit")
	}
	first := state.synthResponse
	costChangeTTL(first, 1)
	state.cacheHit, state.synthResponse = false, nil
	if err := reader.Eval(state, q); err != nil {
		t.Fatal(err)
	}
	if !state.cacheHit || state.synthResponse == first || costTTL(state.synthResponse) < 590 {
		t.Fatal("cached answer aliases caller")
	}
	other := costTTLState()
	if err := reader.Eval(other, costUnpack(costWire("answer.cache.test", 28, false))); err != nil {
		t.Fatal(err)
	}
	if other.cacheHit {
		t.Fatal("A answer certified an AAAA hit")
	}
}

func BenchmarkCost(b *testing.B) {
	for _, kind := range []uint16{1, 28} {
		name := fmt.Sprint(kind)
		queryWire := costWire("MiXeD.cache.test", kind, false)
		answerWire := costWire("MiXeD.cache.test", kind, true)
		b.Run("DecodeQuery/"+name, func(b *testing.B) {
			costMeasure(b, func() { costSinkMsg = costUnpack(queryWire) })
		})
		b.Run("AnswerRoundTrip/"+name, func(b *testing.B) {
			costMeasure(b, func() { costSinkPacket = costPack(costUnpack(answerWire)) })
		})
		b.Run("Key/"+name, func(b *testing.B) {
			state, msg := costTTLState(), costUnpack(queryWire)
			costMeasure(b, func() { costSinkKey = computeCacheKey(state, msg) })
		})
		b.Run("Clone/"+name, func(b *testing.B) {
			msg := costUnpack(answerWire)
			costMeasure(b, func() { costSinkMsg = costClone(msg) })
		})
		b.Run("CacheHit/"+name, func(b *testing.B) {
			costReset()
			state, response := costTTLState(), costUnpack(answerWire)
			if err := new(PluginCacheResponse).Eval(state, response); err != nil {
				b.Fatal(err)
			}
			reader, query := new(PluginCache), costUnpack(queryWire)
			costMeasure(b, func() {
				state.cacheHit, state.synthResponse = false, nil
				if err := reader.Eval(state, query); err != nil || !state.cacheHit {
					b.Fatal("missing cache hit", err)
				}
				costSinkMsg = state.synthResponse
			})
		})
		b.Run("CacheReplace/"+name, func(b *testing.B) {
			costReset()
			state, response := costTTLState(), costUnpack(answerWire)
			writer := new(PluginCacheResponse)
			if err := writer.Eval(state, response); err != nil {
				b.Fatal(err)
			}
			costMeasure(b, func() {
				if err := writer.Eval(state, response); err != nil {
					b.Fatal(err)
				}
			})
		})
	}
}

func TestCostCPUTimeExcludesSleeping(t *testing.T) {
	start, wall := costCPU(), time.Now()
	time.Sleep(150 * time.Millisecond)
	elapsed := time.Since(wall).Nanoseconds()
	used := costCPU() - start
	if used < 0 || used > elapsed/2 {
		t.Fatalf("process CPU counter did not distinguish sleep: %d/%d", used, elapsed)
	}
}
