# Private historical cachedb replay

This helper reads the framed catalog produced by
`scripts/test-cachedb-history-replay.py`. It only accepts explicit IPv4 loopback
resolver endpoints. The catalog, query fingerprints and raw results are private
cache evidence and must stay out of a public report.

Build with the toolchain and vendored `github.com/miekg/dns` dependency from the
source archive pinned by `packaging/dnscrypt-source.lock.json`. From a fresh
directory containing that archive's `go.mod`, `go.sum` and `vendor` tree:

```sh
export GOTOOLCHAIN=local GOWORK=off GOPROXY=off GOSUMDB=off CGO_ENABLED=0
go test -mod=vendor /path/to/repo/scripts/fixtures/history-replay/main.go \
  /path/to/repo/scripts/fixtures/history-replay/main_test.go
go build -mod=vendor -trimpath -buildvcs=false '-ldflags=-s -w -buildid=' \
  -o /private/new-history-replay /path/to/repo/scripts/fixtures/history-replay/main.go
```

Record the source, tests, vendored files, toolchain and binary hashes. Offline
classification opens no DNS sockets:

```sh
/private/new-history-replay --catalog-only \
  --input /private/private-catalog.bin --output /private/new-classification.json
```

The classification reports whether every question can be derived and whether
every full typed DNS packet decodes. A typed decode failure remains explicit.
During replay, only those inputs use raw old/new response comparison, requiring
complete transaction and question checks followed by exact byte equality. This
does not make an invalid historical packet valid or count it as a retained
expected answer.

Normal packets compare RR content and multiplicity across answer, authority and
additional sections, preserving opaque data and the old/new AD flag. TTL and DNS
name presentation case are ignored for content comparison; zero and positive
reply TTL counts are recorded separately. An exact old historical answer followed
by a new empty SERVFAIL may be classified only when the original record has zero
lifetime or is a CNAME intermediate without the queried terminal type. Other
differences remain unexplained.

Replay requires a separate loopback-only network namespace and bounded service
CPU, memory and time. Use ordinary binaries, immutable historical bytes and an
explicit stale/no-write policy; the Python entry point checks the input and helper
hashes. UDP truncation falls back to TCP. TCP connections open when a database is
first used, and transport errors are retained without silent retries. A result
passes only after every input is checked and no unexplained difference remains.
This is a compatibility tool, with no performance, DNSSEC or deployment approval.
