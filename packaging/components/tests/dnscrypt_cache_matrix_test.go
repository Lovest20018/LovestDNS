// Extended offline contracts and fixed-work workloads for the locked 2.1.20 cache.
package main

import (
	"encoding/binary"
	"fmt"
	"runtime"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"codeberg.org/miekg/dns"
)

func matrixReset(capacity int) {
	cachedResponses = nil
	if err := initCachedResponses(capacity); err != nil {
		panic(err)
	}
}

func matrixClear(state *PluginsState) {
	state.cacheHit, state.synthResponse, state.action = false, nil, 0
	delete(state.sessionData, "stale")
}

func matrixKey(index int, forceType uint16) (string, uint16) {
	kind := forceType
	if kind == 0 {
		kind = 1
		if index%2 != 0 {
			kind = 28
		}
	}
	return fmt.Sprintf("matrix-%d.cache.test", index), kind
}

func matrixValidate(msg, query *dns.Msg, maxTTL uint32) error {
	if msg == nil || !msg.Response || msg.ID != query.ID || msg.Rcode != 0 || len(msg.Answer) != 1 {
		return fmt.Errorf("invalid cached response")
	}
	answer, question := msg.Answer[0], query.Question[0]
	if dns.RRToType(answer) != dns.RRToType(question) || answer.Header().Class != question.Header().Class ||
		!strings.EqualFold(string(answer.Header().Name), string(question.Header().Name)) ||
		answer.Header().TTL == 0 || answer.Header().TTL > maxTTL {
		return fmt.Errorf("wrong cached question, type, class or TTL")
	}
	return nil
}

func matrixRead(state *PluginsState, query *dns.Msg) error {
	matrixClear(state)
	return new(PluginCache).Eval(state, query)
}

func TestCostMatrixMissAndFlags(t *testing.T) {
	matrixReset(17)
	state := costTTLState()
	q := costUnpack(costWire("flags.cache.test", 1, false))
	if err := matrixRead(state, q); err != nil || state.cacheHit || state.synthResponse != nil || state.action != 0 {
		t.Fatal("empty cache is not a miss", err)
	}
	if err := new(PluginCacheResponse).Eval(state, costUnpack(costWire("flags.cache.test", 1, true))); err != nil {
		t.Fatal(err)
	}
	for _, variant := range []string{"other-name", "other-type", "cd", "dnssec"} {
		s := costTTLState()
		wire := costWire("flags.cache.test", 1, false)
		if variant == "other-name" {
			wire = costWire("absent.cache.test", 1, false)
		}
		if variant == "other-type" {
			wire = costWire("flags.cache.test", 28, false)
		}
		msg := costUnpack(wire)
		if variant == "cd" {
			msg.CheckingDisabled = true
		}
		if variant == "dnssec" {
			s.dnssec = true
		}
		if err := matrixRead(s, msg); err != nil || s.cacheHit || s.synthResponse != nil || s.sessionData["stale"] != nil {
			t.Fatal("isolated key unexpectedly hit", variant, err)
		}
	}
	if err := matrixRead(state, q); err != nil || !state.cacheHit {
		t.Fatal("original entry lost", err)
	}
}

func TestCostMatrixCapacityAndEviction(t *testing.T) {
	for _, capacity := range []int{1, 2, 15, 16, 17, 511, 512, 513, 4096} {
		matrixReset(capacity)
		if cachedResponses.Capacity() != capacity || cachedResponses.NumShards() != min(capacity, 16) {
			t.Fatal("wrong initialized capacity", capacity)
		}
		state := costTTLState()
		for i := 0; i < capacity*3+17; i++ {
			name, kind := matrixKey(i, 0)
			if err := new(PluginCacheResponse).Eval(state, costUnpack(costWire(name, kind, true))); err != nil {
				t.Fatal(err)
			}
			if i%7 == 0 {
				query := costUnpack(costWire(name, kind, false))
				if err := matrixRead(state, query); err != nil || !state.cacheHit {
					t.Fatal("just-inserted entry missing", capacity, i, err)
				}
				if err := matrixValidate(state.synthResponse, query, 600); err != nil {
					t.Fatal(err)
				}
			}
			if cachedResponses.Len() > capacity {
				t.Fatal("capacity exceeded", capacity)
			}
		}
		for _, entry := range cachedResponses.GetShardByIndex(0).Items() {
			if entry.Value.msg == nil {
				t.Fatal("nil cache payload")
			}
		}
		cachedResponses.Clear()
		if cachedResponses.Len() != 0 {
			t.Fatal("clear retained entries")
		}
	}
}

func matrixOPT(packet []byte) []byte {
	binary.BigEndian.PutUint16(packet[10:12], 1)
	// DO + UDP size 4096, with one EDE option carrying "test".
	return append(packet, 0, 0, 41, 16, 0, 0, 0, 128, 0, 0, 10, 0, 15, 0, 6, 0, 0, 't', 'e', 's', 't')
}

func matrixEDNS(msg *dns.Msg) bool {
	// This library exposes OPT flags on MsgHeader and EDNS options in Pseudo.
	return msg.Security && msg.UDPSize == 4096 && msg.Version == 0 && len(msg.Pseudo) == 1 && msg.Pseudo[0] != nil
}

func TestCostMatrixExpiryAndResponseOwnership(t *testing.T) {
	matrixReset(17)
	state := costTTLState()
	state.cacheMaxTTL = 40
	query := costUnpack(costWire("expiry.cache.test", 1, false))
	response := costUnpack(matrixOPT(costWire("expiry.cache.test", 1, true)))
	if !matrixEDNS(response) {
		t.Fatal("fixture did not decode its EDNS header and option")
	}
	if err := new(PluginCacheResponse).Eval(state, response); err != nil {
		t.Fatal(err)
	}
	key := computeCacheKey(state, query)
	entry, ok := cachedResponses.Get(key)
	if !ok || !matrixEDNS(entry.msg) || entry.msg.Answer[0] == response.Answer[0] || entry.msg.Pseudo[0] == response.Pseudo[0] || entry.msg.Question != nil {
		t.Fatal("cache writer does not own response records")
	}
	costChangeTTL(response, 1)
	response.Answer[0].Header().Name = "changed.cache.test."
	response.Security = false
	response.Pseudo[0] = nil
	if err := matrixRead(state, query); err != nil || !state.cacheHit {
		t.Fatal("fresh entry missing", err)
	}
	if err := matrixValidate(state.synthResponse, query, 40); err != nil {
		t.Fatal(err)
	}
	if !matrixEDNS(state.synthResponse) {
		t.Fatal("EDNS flags were treated as TTL")
	}
	entry.expiration = time.Now().Add(-time.Hour)
	cachedResponses.Insert(key, entry)
	for i := 0; i < 2; i++ {
		if err := matrixRead(state, query); err != nil || state.cacheHit || state.synthResponse != nil || state.action != 0 {
			t.Fatal("expired entry became a fresh hit", err)
		}
		stale, ok := state.sessionData["stale"].(*dns.Msg)
		if !ok {
			t.Fatal("expired entry did not offer a stale hint")
		}
		if err := matrixValidate(stale, query, 30); err != nil {
			t.Fatal(err)
		}
		if !matrixEDNS(stale) {
			t.Fatal("stale update changed EDNS flags")
		}
		stale.Answer[0].Header().Name = "caller-change.cache.test."
		costChangeTTL(stale, 1)
		stale.Security = false
		stale.Pseudo[0] = nil
	}
	state.cacheMaxTTL = 20
	if err := new(PluginCacheResponse).Eval(state, costUnpack(costWire("expiry.cache.test", 1, true))); err != nil {
		t.Fatal(err)
	}
	if err := matrixRead(state, query); err != nil || !state.cacheHit || state.sessionData["stale"] != nil {
		t.Fatal("refresh did not replace stale entry", err)
	}
	if err := matrixValidate(state.synthResponse, query, 20); err != nil {
		t.Fatal(err)
	}
}

func matrixNegative() []byte {
	packet := costWire("negative.cache.test", 1, false)
	binary.BigEndian.PutUint16(packet[2:4], 0x8183)
	binary.BigEndian.PutUint16(packet[8:10], 1)
	rdata := []byte{2, 'n', 's', 5, 'c', 'a', 'c', 'h', 'e', 4, 't', 'e', 's', 't', 0, 10, 'h', 'o', 's', 't', 'm', 'a', 's', 't', 'e', 'r', 5, 'c', 'a', 'c', 'h', 'e', 4, 't', 'e', 's', 't', 0}
	for _, v := range []uint32{1, 3600, 600, 86400, 60} {
		var word [4]byte
		binary.BigEndian.PutUint32(word[:], v)
		rdata = append(rdata, word[:]...)
	}
	rr := []byte{0xc0, 12, 0, 6, 0, 1, 0, 0, 0, 120, 0, byte(len(rdata))}
	return matrixOPT(append(append(packet, rr...), rdata...))
}

func TestCostMatrixNegativeAndUncacheableResponses(t *testing.T) {
	matrixReset(17)
	state := costTTLState()
	state.cacheNegMinTTL = 5
	state.cacheNegMaxTTL = 20
	query := costUnpack(costWire("negative.cache.test", 1, false))
	response := costUnpack(matrixNegative())
	if err := new(PluginCacheResponse).Eval(state, response); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 2; i++ {
		if err := matrixRead(state, query); err != nil || !state.cacheHit {
			t.Fatal("negative cache miss", err)
		}
		msg := state.synthResponse
		if msg.Rcode != 3 || len(msg.Answer) != 0 || len(msg.Ns) != 1 || msg.Ns[0].Header().TTL == 0 || msg.Ns[0].Header().TTL > 20 || !matrixEDNS(msg) {
			t.Fatal("negative TTL/EDNS changed")
		}
		msg.Ns[0].Header().Name = "caller-change.cache.test."
		if !strings.EqualFold(string(msg.Question[0].Header().Name), string(query.Question[0].Header().Name)) {
			t.Fatal("negative question changed")
		}
	}
	entry, _ := cachedResponses.Get(computeCacheKey(state, query))
	if entry.msg.Ns[0].Header().Name == "caller-change.cache.test." {
		t.Fatal("negative authority aliases caller")
	}
	for _, mode := range []string{"truncated", "servfail"} {
		matrixReset(17)
		msg := costUnpack(costWire("negative.cache.test", 1, true))
		if mode == "truncated" {
			msg.Truncated = true
		} else {
			msg.Rcode = 2
		}
		if err := new(PluginCacheResponse).Eval(state, msg); err != nil {
			t.Fatal(err)
		}
		if err := matrixRead(state, query); err != nil || state.cacheHit || cachedResponses.Len() != 0 {
			t.Fatal("uncacheable response stored", mode, err)
		}
	}
}

func TestCostMatrixParallelResponseOwnership(t *testing.T) {
	matrixReset(17)
	var workers sync.WaitGroup
	errors := make(chan error, 8)
	for worker := 0; worker < 8; worker++ {
		workers.Add(1)
		go func(worker int) {
			defer workers.Done()
			state := costTTLState()
			for i := 0; i < 300; i++ {
				name, kind := matrixKey(worker*128+i%128, 0)
				query := costUnpack(costWire(name, kind, false))
				if err := new(PluginCacheResponse).Eval(state, costUnpack(costWire(name, kind, true))); err != nil {
					errors <- err
					return
				}
				if err := matrixRead(state, query); err != nil {
					errors <- err
					return
				}
				if state.cacheHit {
					if err := matrixValidate(state.synthResponse, query, 600); err != nil {
						errors <- err
						return
					}
					state.synthResponse.Answer[0].Header().Name = "caller-change.cache.test."
					costChangeTTL(state.synthResponse, 1)
				}
			}
		}(worker)
	}
	workers.Wait()
	close(errors)
	for err := range errors {
		t.Fatal(err)
	}
	if cachedResponses.Len() > 17 {
		t.Fatal("parallel cache exceeded capacity")
	}
}

type matrixCase struct {
	name                    string
	capacity, keys, prefill int
	kind                    uint16
	fill, hotMix, stale     bool
}

type matrixWorker struct {
	state                        *PluginsState
	queries, answers             []*dns.Msg
	hits, misses, stales, writes int64
}

func BenchmarkCacheMatrix(b *testing.B) {
	cases := []matrixCase{
		{name: "empty-c1", capacity: 1, keys: 32},
		{name: "empty-c17", capacity: 17, keys: 32},
		{name: "empty-c512", capacity: 512, keys: 32},
		{name: "empty-c4096", capacity: 4096, keys: 32},
		{name: "hot-a", capacity: 512, keys: 1, prefill: 1, kind: 1},
		{name: "hot-aaaa", capacity: 512, keys: 1, prefill: 1, kind: 28},
		{name: "quarter-c512", capacity: 512, keys: 128, prefill: 128},
		{name: "full-c512", capacity: 512, keys: 512, prefill: 512},
		{name: "churn-c17", capacity: 17, keys: 68, prefill: 17, fill: true},
		{name: "mixed-c512", capacity: 512, keys: 2048, prefill: 512, fill: true, hotMix: true},
		{name: "stale-c512", capacity: 512, keys: 2, prefill: 2, stale: true},
	}
	for _, scenario := range cases {
		for _, parallel := range []int{1, 2, 8} {
			b.Run(fmt.Sprintf("%s/p%d", scenario.name, parallel), func(b *testing.B) {
				matrixReset(scenario.capacity)
				seedState := costTTLState()
				for i := 0; i < scenario.prefill; i++ {
					name, kind := matrixKey(i, scenario.kind)
					response := costUnpack(costWire(name, kind, true))
					if err := new(PluginCacheResponse).Eval(seedState, response); err != nil {
						b.Fatal(err)
					}
					if scenario.stale {
						key := computeCacheKey(seedState, response)
						entry, _ := cachedResponses.Get(key)
						entry.expiration = time.Now().Add(-time.Hour)
						cachedResponses.Insert(key, entry)
					}
				}
				initialEntries := cachedResponses.Len()
				workers := make([]matrixWorker, parallel)
				for i := range workers {
					worker := &workers[i]
					worker.state = costTTLState()
					for j := 0; j < scenario.keys; j++ {
						name, kind := matrixKey(j, scenario.kind)
						worker.queries = append(worker.queries, costUnpack(costWire(name, kind, false)))
						if scenario.fill {
							worker.answers = append(worker.answers, costUnpack(costWire(name, kind, true)))
						}
					}
				}
				run := func(worker *matrixWorker, index int64) {
					key := int(index) % scenario.keys
					if scenario.hotMix {
						key = int(index) % 16
						if index%10 == 0 {
							key = 16 + int(index/10)%(scenario.keys-16)
						}
					}
					query := worker.queries[key]
					if err := matrixRead(worker.state, query); err != nil {
						b.Fatal(err)
					}
					if worker.state.cacheHit {
						if err := matrixValidate(worker.state.synthResponse, query, 600); err != nil {
							b.Fatal(err)
						}
						worker.hits++
					} else if stale, ok := worker.state.sessionData["stale"].(*dns.Msg); ok {
						if !scenario.stale {
							b.Fatal("unexpected expiry during a fresh workload")
						}
						if err := matrixValidate(stale, query, 30); err != nil {
							b.Fatal(err)
						}
						worker.stales++
					} else {
						worker.misses++
						if scenario.fill {
							response := worker.answers[key]
							costChangeTTL(response, 600)
							if err := new(PluginCacheResponse).Eval(worker.state, response); err != nil {
								b.Fatal(err)
							}
							worker.writes++
						}
					}
				}
				b.ReportAllocs()
				b.ResetTimer()
				start := costCPU()
				if parallel == 1 {
					for i := 0; i < b.N; i++ {
						run(&workers[0], int64(i))
					}
				} else {
					if runtime.GOMAXPROCS(0) != 2 {
						b.Fatal("parallel matrix requires GOMAXPROCS=2")
					}
					b.SetParallelism(parallel / 2)
					var next atomic.Int32
					b.RunParallel(func(pb *testing.PB) {
						id := int(next.Add(1)) - 1
						worker := &workers[id]
						i := int64(id)
						for pb.Next() {
							run(worker, i)
							i++
						}
					})
				}
				used := costCPU() - start
				b.StopTimer()
				var hits, misses, stales, writes int64
				for _, worker := range workers {
					hits += worker.hits
					misses += worker.misses
					stales += worker.stales
					writes += worker.writes
				}
				if hits+misses+stales != int64(b.N) || cachedResponses.Len() > scenario.capacity {
					b.Fatal("incomplete operations or capacity overflow")
				}
				if scenario.prefill == 0 && misses != int64(b.N) || scenario.stale && stales != int64(b.N) {
					b.Fatal("workload did not follow its declared path")
				}
				if strings.HasPrefix(scenario.name, "hot-") && hits != int64(b.N) {
					b.Fatal("hot workload did not hit on every operation")
				}
				b.ReportMetric(float64(used)/float64(b.N), "cpu-ns/op")
				b.ReportMetric(float64(hits)/float64(b.N), "hit/op")
				b.ReportMetric(float64(misses)/float64(b.N), "miss/op")
				b.ReportMetric(float64(stales)/float64(b.N), "stale/op")
				b.ReportMetric(float64(writes)/float64(b.N), "write/op")
				b.ReportMetric(float64(initialEntries), "initial-entries")
				b.ReportMetric(float64(cachedResponses.Len()), "final-entries")
			})
		}
	}
}
