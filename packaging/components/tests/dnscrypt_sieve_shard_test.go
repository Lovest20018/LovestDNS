// Copy into the locked vendor package to verify the optional shard digest patch.
package sievecache

import (
	"crypto/sha256"
	"fmt"
	"hash/maphash"
	"sync"
	"testing"
)

var shardSink int

func TestShardDigestHashesAllRawBytes(t *testing.T) {
	for _, shards := range []int{1, 3, 16, 31} {
		cache, err := NewShardedWithShards[[32]byte, int](1024, shards)
		if err != nil {
			t.Fatal(err)
		}
		for i := 0; i < 256; i++ {
			key := sha256.Sum256([]byte(fmt.Sprint(i)))
			var hash maphash.Hash
			hash.SetSeed(hashSeed)
			hash.Write(key[:])
			if got, want := cache.getShardIndex(key), int(hash.Sum64()%uint64(shards)); got != want {
				t.Fatalf("digest %d, shards %d: index %d != raw-byte hash %d", i, shards, got, want)
			}
		}
	}
}

func TestShardDigestHashDoesNotAllocate(t *testing.T) {
	cache, err := NewSharded[[32]byte, int](512)
	if err != nil {
		t.Fatal(err)
	}
	key := sha256.Sum256([]byte("full-length cache digest"))
	if allocations := testing.AllocsPerRun(1000, func() { shardSink = cache.getShardIndex(key) }); allocations != 0 {
		t.Fatalf("shard hashing allocates %v times per key", allocations)
	}
}

func shardRoundTrip[K comparable](t *testing.T, keys []K) {
	t.Helper()
	cache, err := NewShardedWithShards[K, int](512, 3)
	if err != nil {
		t.Fatal(err)
	}
	for i, key := range keys {
		cache.Insert(key, i+1)
		if got, ok := cache.Get(key); !ok || got != i+1 {
			t.Fatal("wrong cached value", key, got, ok)
		}
		cache.Insert(key, i+100)
		if got, ok := cache.Get(key); !ok || got != i+100 {
			t.Fatal("replace failed", key, got, ok)
		}
		if got, ok := cache.Remove(key); !ok || got != i+100 {
			t.Fatal("remove failed", key, got, ok)
		}
		if _, ok := cache.Get(key); ok {
			t.Fatal("removed key found", key)
		}
	}
	if cache.Capacity() != 512 || !cache.IsEmpty() {
		t.Fatal("capacity/empty contract changed")
	}
}

type shardNamedDigest [32]byte
type shardStringer int

func (key shardStringer) String() string { return fmt.Sprintf("named-%d", int(key)) }

func TestShardOtherKeyTypesStillWork(t *testing.T) {
	shardRoundTrip(t, []string{"", "a", "b"})
	shardRoundTrip(t, []int{0, -1, 128})
	shardRoundTrip(t, []int64{0, -1, 1 << 50})
	shardRoundTrip(t, []shardNamedDigest{{}, {1}, {31: 255}})
	shardRoundTrip(t, []shardStringer{0, 1, -1})
}

func TestShardDigestParallelOwnershipAndCapacity(t *testing.T) {
	cache, err := NewShardedWithShards[[32]byte, int](32, 16)
	if err != nil {
		t.Fatal(err)
	}
	var workers sync.WaitGroup
	for worker := 0; worker < 8; worker++ {
		workers.Add(1)
		go func(worker int) {
			defer workers.Done()
			for i := 0; i < 2000; i++ {
				key := sha256.Sum256([]byte(fmt.Sprintf("%d/%d", worker, i%128)))
				cache.Insert(key, worker)
				// Concurrent eviction is allowed; a hit must retain the key's value.
				if got, ok := cache.Get(key); ok && got != worker {
					t.Errorf("key/value mismatch: %d != %d", got, worker)
					return
				}
				if i%3 == 0 {
					cache.Remove(key)
				}
			}
		}(worker)
	}
	workers.Wait()
	if cache.Len() > cache.Capacity() || cache.Capacity() != 32 {
		t.Fatal("capacity exceeded")
	}
	cache.Clear()
	if !cache.IsEmpty() {
		t.Fatal("clear retained entries")
	}
}

func TestShardOddCapacitiesCanFillAndEvict(t *testing.T) {
	for _, capacity := range []int{1, 2, 15, 16, 17, 33, 512, 4096} {
		cache, err := NewSharded[[32]byte, int](capacity)
		if err != nil {
			t.Fatal(err)
		}
		chosen := make([]int, cache.NumShards())
		keys := make(map[[32]byte]int, capacity)
		for candidate := 0; len(keys) < capacity && candidate < capacity*256; candidate++ {
			key := sha256.Sum256([]byte(fmt.Sprint(candidate)))
			shard := cache.getShardIndex(key)
			if chosen[shard] >= cache.GetShardByIndex(shard).Capacity() {
				continue
			}
			chosen[shard]++
			keys[key] = candidate
			cache.Insert(key, candidate)
		}
		if len(keys) != capacity || cache.Len() != capacity || cache.Capacity() != capacity {
			t.Fatal("could not use every configured slot", capacity)
		}
		for key, want := range keys {
			if got, ok := cache.Get(key); !ok || got != want {
				t.Fatal("full cache returned a wrong value", capacity)
			}
		}
		// All slots are now visited. Inserts must still evict before growing.
		for candidate := 0; candidate < 64; candidate++ {
			key := sha256.Sum256([]byte(fmt.Sprintf("replacement-%d", candidate)))
			cache.Insert(key, -candidate)
			if got, ok := cache.Get(key); !ok || got != -candidate || cache.Len() != capacity {
				t.Fatal("full/visited eviction failed", capacity)
			}
		}
	}
}
