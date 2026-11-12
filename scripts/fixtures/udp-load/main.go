// A bounded loopback DNS load generator with no catch-up after a late slot.
// Its text sample format is consumed by the existing DNS benchmark parser.
package main

import (
	"bufio"
	"encoding/binary"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log"
	"net"
	"os"
	"runtime"
	"strings"
	"sync"
	"time"
)

type pendingQuery struct {
	name string
	sent time.Time
}

type result struct {
	name    string
	code    int
	latency time.Duration
}

// Trace rows stay in bounded memory until the measured work has finished.
// No per-packet disk I/O is inserted in the sending path.
type sendTrace struct {
	Index      int   `json:"index"`
	Planned    int64 `json:"planned_ns"`
	Woke       int64 `json:"woke_ns"`
	Locked     int64 `json:"lock_acquired_ns"`
	WriteStart int64 `json:"write_start_ns"`
	WriteEnd   int64 `json:"write_end_ns"`
	Pending    int   `json:"pending"`
}

func saveTrace(file *os.File, rows []sendTrace, started, ended time.Time, before, after runtime.MemStats) error {
	pauses := []map[string]uint64{}
	for i, end := range after.PauseEnd {
		if end >= uint64(started.UnixNano()) && end <= uint64(ended.UnixNano()) {
			pauses = append(pauses, map[string]uint64{"end_unix_ns": end, "duration_ns": after.PauseNs[i]})
		}
	}
	return json.NewEncoder(file).Encode(map[string]any{
		"method": "monotonic-send-stages-v1", "diagnostic_only": true,
		"started_unix_ns": started.UnixNano(), "ended_unix_ns": ended.UnixNano(),
		"elapsed_ns": ended.Sub(started).Nanoseconds(), "rows": rows,
		"gc_cycles": after.NumGC - before.NumGC, "gc_pause_total_ns": after.PauseTotalNs - before.PauseTotalNs,
		"gc_recent_pauses": pauses, "gc_pause_history_capacity": len(after.PauseNs),
	})
}

func question(line string, id uint16) ([]byte, string, error) {
	fields := strings.Fields(line)
	if len(fields) != 2 || !strings.HasSuffix(strings.ToLower(fields[0]), ".cache.test") || len(fields[0]) > 253 {
		return nil, "", errors.New("controlled cache.test question required")
	}
	kind := uint16(1)
	if fields[1] == "AAAA" {
		kind = 28
	} else if fields[1] != "A" {
		return nil, "", errors.New("only A and AAAA are supported")
	}
	packet := make([]byte, 12)
	binary.BigEndian.PutUint16(packet[:2], id)
	binary.BigEndian.PutUint16(packet[2:4], 0x0100)
	binary.BigEndian.PutUint16(packet[4:6], 1)
	for _, label := range strings.Split(fields[0], ".") {
		if len(label) < 1 || len(label) > 63 {
			return nil, "", errors.New("invalid DNS label")
		}
		packet = append(packet, byte(len(label)))
		packet = append(packet, label...)
	}
	packet = append(packet, 0, 0, 0, 0, 1)
	binary.BigEndian.PutUint16(packet[len(packet)-4:len(packet)-2], kind)
	return packet, fields[0], nil
}

// Small timer jitter can use the next planned slot. A delay larger than half
// a period starts a new phase, so missed slots are never emitted as a burst.
func nextDeadline(planned, actual time.Time, period time.Duration) time.Time {
	if actual.Sub(planned) > period/2 {
		return actual.Add(period)
	}
	return planned.Add(period)
}

func run() error {
	server := flag.String("s", "127.0.0.1", "loopback resolver")
	port := flag.Int("p", 0, "resolver port")
	input := flag.String("d", "", "controlled questions")
	local := flag.String("a", "127.0.0.1", "loopback source")
	localPort := flag.Int("x", 0, "source port")
	mode := flag.String("m", "udp", "UDP only")
	duration := flag.Int("l", 0, "duration in seconds")
	rate := flag.Int("Q", 100, "questions per second")
	limit := flag.Int("q", 64, "maximum outstanding")
	timeout := flag.Int("t", 2, "final response timeout in seconds")
	passes := flag.Int("n", 1, "one input pass")
	tracePath := flag.String("trace", "", "diagnostic-only send stages, written after measurement to a new file")
	flag.Bool("v", false, "emit per-answer samples")
	flag.Bool("W", false, "accepted compatibility flag")
	flag.Parse()
	if *server != "127.0.0.1" || *local != "127.0.0.1" || *port < 1 || *port > 65535 ||
		*localPort < 1 || *localPort > 65535 || *mode != "udp" || *passes != 1 ||
		*duration < 1 || *duration > 120 || *rate < 1 || *rate > 1000 || *limit < 1 || *limit > 256 || *timeout != 2 {
		return errors.New("requires bounded loopback UDP arguments")
	}
	selfNS, err := os.Readlink("/proc/self/ns/net")
	if err != nil {
		return err
	}
	hostNS, err := os.Readlink("/proc/1/ns/net")
	if err != nil || selfNS == hostNS {
		return errors.New("disposable network namespace required")
	}
	data, err := os.ReadFile(*input)
	if err != nil {
		return err
	}
	lines := strings.Split(strings.TrimSpace(string(data)), "\n")
	packets, names := make([][]byte, 0, len(lines)), make([]string, 0, len(lines))
	for i, line := range lines {
		packet, name, err := question(line, uint16(i))
		if err != nil {
			return err
		}
		packets, names = append(packets, packet), append(names, name)
	}
	conn, err := net.DialUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: *localPort},
		&net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: *port})
	if err != nil {
		return err
	}
	defer conn.Close()
	var mu sync.Mutex
	pending := map[uint16]pendingQuery{}
	capacity := (*rate) * (*duration)
	results := make([]result, 0, capacity)
	var traceFile *os.File
	var traces []sendTrace
	var gcBefore runtime.MemStats
	if *tracePath != "" {
		traceFile, err = os.OpenFile(*tracePath, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
		if err != nil {
			return err
		}
		defer traceFile.Close()
		traces = make([]sendTrace, 0, capacity+16)
		runtime.ReadMemStats(&gcBefore)
	}
	sending, malformed := true, 0
	started := time.Now()
	end := started.Add(time.Duration(*duration) * time.Second)
	final := end.Add(time.Duration(*timeout) * time.Second)
	done := make(chan struct{})
	go func() {
		defer close(done)
		buffer := make([]byte, 65535)
		for {
			conn.SetReadDeadline(time.Now().Add(100 * time.Millisecond))
			mu.Lock()
			finished := !sending && len(pending) == 0
			mu.Unlock()
			if finished || time.Now().After(final) {
				return
			}
			n, err := conn.Read(buffer)
			if err != nil {
				if e, ok := err.(net.Error); ok && e.Timeout() {
					continue
				}
				return
			}
			now := time.Now()
			mu.Lock()
			if n < 12 || buffer[2]&0x80 == 0 {
				malformed++
				mu.Unlock()
				continue
			}
			id := binary.BigEndian.Uint16(buffer[:2])
			query, ok := pending[id]
			if !ok {
				malformed++
				mu.Unlock()
				continue
			}
			delete(pending, id)
			results = append(results, result{name: query.name, code: int(buffer[3] & 15), latency: now.Sub(query.sent)})
			mu.Unlock()
		}
	}()
	period, planned := time.Second/time.Duration(*rate), started
	sent, resets, full, clamps := 0, 0, 0, 0
	var previous time.Time
	var minGap, maxGap time.Duration
	for i := 0; i < len(packets); {
		slot := planned
		if wait := time.Until(planned); wait > 0 {
			time.Sleep(wait)
		}
		now := time.Now()
		if !now.Before(end) {
			break
		}
		next := nextDeadline(planned, now, period)
		if now.Sub(planned) > period/2 {
			resets++
		}
		planned = next
		id := uint16(i)
		mu.Lock()
		var locked time.Time
		if traceFile != nil {
			locked = time.Now()
		}
		_, collision := pending[id]
		if len(pending) >= *limit || collision {
			full++
			mu.Unlock()
			continue
		}
		pending[id] = pendingQuery{name: names[i], sent: now}
		pendingCount := len(pending)
		mu.Unlock()
		var writeStart time.Time
		if traceFile != nil {
			writeStart = time.Now()
		}
		if _, err := conn.Write(packets[i]); err != nil {
			return err
		}
		if traceFile != nil {
			if len(traces) == cap(traces) {
				return errors.New("diagnostic trace budget exceeded")
			}
			traces = append(traces, sendTrace{Index: i, Planned: slot.Sub(started).Nanoseconds(),
				Woke: now.Sub(started).Nanoseconds(), Locked: locked.Sub(started).Nanoseconds(),
				WriteStart: writeStart.Sub(started).Nanoseconds(), WriteEnd: time.Since(started).Nanoseconds(), Pending: pendingCount})
		}
		if earliest := time.Now().Add(period / 2); planned.Before(earliest) {
			planned = earliest
			clamps++
		}
		if !previous.IsZero() {
			gap := now.Sub(previous)
			if minGap == 0 || gap < minGap {
				minGap = gap
			}
			if gap > maxGap {
				maxGap = gap
			}
		}
		previous = now
		sent++
		i++
	}
	if wait := time.Until(end); wait > 0 {
		time.Sleep(wait)
	}
	mu.Lock()
	sending = false
	mu.Unlock()
	conn.SetReadDeadline(time.Now())
	<-done
	ended := time.Now()
	if traceFile != nil {
		var gcAfter runtime.MemStats
		runtime.ReadMemStats(&gcAfter)
		if err := saveTrace(traceFile, traces, started, ended, gcBefore, gcAfter); err != nil {
			return err
		}
	}
	elapsed := ended.Sub(started).Seconds()
	stream := bufio.NewWriter(os.Stdout)
	fmt.Fprintln(stream, "LovestDNS paced UDP load v1")
	codes := map[int]string{0: "NOERROR", 1: "FORMERR", 2: "SERVFAIL", 3: "NXDOMAIN", 4: "NOTIMP", 5: "REFUSED"}
	for i, row := range results {
		code, ok := codes[row.code]
		if !ok {
			code = fmt.Sprint("RCODE", row.code)
		}
		fmt.Fprintf(stream, "> %s %d %s %.9f\n", code, i, row.name, row.latency.Seconds())
	}
	fmt.Fprintf(stream, "Queries sent: %d\nQueries completed: %d\nQueries lost: %d\n", sent, len(results), sent-len(results))
	fmt.Fprintf(stream, "Queries per second: %.6f\nRun time (s): %.6f\n", float64(len(results))/elapsed, elapsed)
	evidence := map[string]any{"method": "monotonic-deadlines-no-late-catchup-v1", "phase_resets": resets,
		"spacing_clamps":          clamps,
		"outstanding_limit_skips": full, "malformed_or_unexpected": malformed,
		"min_send_gap_ns": minGap.Nanoseconds(), "max_send_gap_ns": maxGap.Nanoseconds()}
	value, _ := json.Marshal(evidence)
	fmt.Fprintf(stream, "Paced UDP evidence: %s\n", value)
	if err := stream.Flush(); err != nil {
		return err
	}
	return nil
}

func main() {
	if err := run(); err != nil {
		log.Fatal(err)
	}
}
