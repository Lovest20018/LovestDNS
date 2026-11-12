/* Benchmark-only recorder for one dnsperf UDP sender/receiver pair. */
#include <sched.h>

static struct benchmark_trace_sample {
    uint64_t sent, received;
    uint16_t id, rcode;
} *benchmark_trace;
static size_t benchmark_trace_used, benchmark_trace_capacity;
static const char* benchmark_trace_path;
static bool benchmark_trace_deferred;
static const char* benchmark_affinity_path;
static int benchmark_thread_cpus[2]; /* receiver, sender */
static uint64_t benchmark_thread_verified[2], benchmark_start;

static int
benchmark_cpu(const char* text)
{
    char* end;
    if (!text || !*text || strspn(text, "0123456789") != strlen(text))
        perf_log_fatal("invalid benchmark thread CPU");
    unsigned long value = strtoul(text, &end, 10);
    cpu_set_t allowed;
    if (*end || value >= CPU_SETSIZE || sched_getaffinity(0, sizeof(allowed), &allowed) || !CPU_ISSET(value, &allowed))
        perf_log_fatal("benchmark thread CPU is outside the allowed affinity");
    return (int)value;
}

static void
benchmark_trace_init(const config_t* config)
{
    const char* mode = getenv("DNSPERF_TRACE_MODE");
    const char* capacity = getenv("DNSPERF_TRACE_CAPACITY");
    const char* receiver = getenv("DNSPERF_RECV_CPU");
    const char* sender = getenv("DNSPERF_SEND_CPU");
    benchmark_affinity_path = getenv("DNSPERF_AFFINITY_FILE");
    benchmark_trace_path = getenv("DNSPERF_TRACE_FILE");
    if (!mode && !capacity && !benchmark_trace_path) {
        if (receiver || sender || benchmark_affinity_path)
            perf_log_fatal("benchmark thread placement requires the timestamp recorder");
        return;
    }
    if (!mode || !capacity || !benchmark_trace_path || !*benchmark_trace_path ||
        config->mode != sock_udp || config->threads != 1 || config->clients != 1 || !config->verbose)
        perf_log_fatal("the benchmark trace requires one UDP client with verbose mode enabled");
    if (receiver || sender || benchmark_affinity_path) {
        if (!receiver || !sender || !benchmark_affinity_path || !*benchmark_affinity_path)
            perf_log_fatal("benchmark thread placement requires both CPUs and an audit path");
        benchmark_thread_cpus[0] = benchmark_cpu(receiver);
        benchmark_thread_cpus[1] = benchmark_cpu(sender);
        if (benchmark_thread_cpus[0] == benchmark_thread_cpus[1])
            perf_log_fatal("benchmark sender and receiver require different CPUs");
    }
    benchmark_trace_deferred = strcmp(mode, "memory") == 0;
    if (!benchmark_trace_deferred && strcmp(mode, "stream") != 0)
        perf_log_fatal("invalid benchmark trace mode");
    char* end;
    unsigned long limit = strtoul(capacity, &end, 10);
    if (*end || !limit || limit > 1000000)
        perf_log_fatal("invalid benchmark trace capacity");
    benchmark_trace_capacity = limit;
    benchmark_trace = calloc(limit, sizeof(*benchmark_trace));
    if (!benchmark_trace)
        perf_log_fatal("cannot allocate benchmark trace");
    /* Touch the bounded buffer before the send/receive threads start. */
    volatile unsigned char* pages = (volatile unsigned char*)benchmark_trace;
    for (size_t i = 0; i < limit * sizeof(*benchmark_trace); i += 4096)
        pages[i] = 0;
}

static void
benchmark_trace_place_threads(threadinfo_t* tinfo)
{
    if (!benchmark_affinity_path)
        return;
    pthread_t workers[] = { tinfo->receiver, tinfo->sender };
    for (int i = 0; i < 2; i++) {
        cpu_set_t target, actual;
        CPU_ZERO(&target);
        CPU_SET(benchmark_thread_cpus[i], &target);
        if (pthread_setaffinity_np(workers[i], sizeof(target), &target) ||
            pthread_getaffinity_np(workers[i], sizeof(actual), &actual) ||
            CPU_COUNT(&actual) != 1 || !CPU_ISSET(benchmark_thread_cpus[i], &actual))
            perf_log_fatal("cannot apply and verify benchmark thread placement");
        benchmark_thread_verified[i] = perf_get_time();
    }
}

static void
benchmark_trace_add(const received_query_t* sample)
{
    if (benchmark_trace_used == benchmark_trace_capacity || sample->when < sample->sent)
        perf_log_fatal("benchmark trace overflow or client clock reversal");
    benchmark_trace[benchmark_trace_used++] = (struct benchmark_trace_sample){
        .sent = sample->sent, .received = sample->when, .id = sample->qid, .rcode = sample->rcode
    };
}

static void
benchmark_trace_write(void)
{
    if (!benchmark_trace)
        return;
    FILE* out = fopen(benchmark_trace_path, "wx");
    if (!out)
        perf_log_fatal("cannot open benchmark trace output");
    if (fputs("id sent_us read_us rcode\n", out) < 0)
        perf_log_fatal("cannot write benchmark trace header");
    for (size_t i = 0; i < benchmark_trace_used; i++) {
        const struct benchmark_trace_sample* row = &benchmark_trace[i];
        if (fprintf(out, "%u %" PRIu64 " %" PRIu64 " %u\n", (unsigned int)row->id,
                row->sent, row->received, (unsigned int)row->rcode) < 0)
            perf_log_fatal("cannot write benchmark trace row");
    }
    if (fclose(out) != 0)
        perf_log_fatal("cannot close benchmark trace output");
    if (benchmark_affinity_path) {
        out = fopen(benchmark_affinity_path, "wx");
        if (!out || fputs("role cpu verified_us start_us\n", out) < 0)
            perf_log_fatal("cannot open benchmark thread affinity audit");
        for (int i = 0; i < 2; i++) {
            if (!benchmark_thread_verified[i] || benchmark_thread_verified[i] > benchmark_start ||
                fprintf(out, "%s %d %" PRIu64 " %" PRIu64 "\n", i ? "sender" : "receiver",
                    benchmark_thread_cpus[i], benchmark_thread_verified[i], benchmark_start) < 0)
                perf_log_fatal("invalid benchmark thread affinity audit");
        }
        if (fclose(out) != 0)
            perf_log_fatal("cannot close benchmark thread affinity audit");
    }
    free(benchmark_trace);
}
