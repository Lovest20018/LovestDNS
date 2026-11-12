# 独立组件补丁候选

本目录不被默认组件构建或安装流程自动应用。`components.lock.json` 仍指向官方 DNSCrypt 二进制；添加补丁不代表可部署组合已经更新。

## 普通源码候选

`packaging/dnscrypt-source.lock.json` 独立固定源码、工具链和补丁。显式构建命令：

```sh
python3 scripts/build-dnscrypt-source.py \
  --source-archive /path/to/dnscrypt-2.1.20.tar.gz \
  --toolchain-archive /path/to/go1.27.3.linux-amd64.tar.gz \
  --work /new/disposable/candidates
```

产生 `upstream`、`buffer`、`udp` 三个普通二进制及各自的 `source-manifest.json`，依次为原实现、仅接收队列补丁、双补丁。构建使用校验后的全新源码、同一编译参数和离线 vendor，没有采样入口；清单仍标记 `candidate_only=true`、`release_approved=false`。默认打包不读取这份锁。

`--variants upstream,combined` 显式构建原实现和三补丁组合，combined 按接收队列、UDP 分配、缓存分片的顺序应用现有补丁；`--variants cache` 则只应用缓存分片补丁。默认构建组合保持原样。增加组合后源码锁摘要发生变化，旧候选清单不得修改后复用，应重新构建；历史结果仍按各自归档中的旧锁解释。

不依赖定速计时的功能入口为 `scripts/test-dnscrypt-source-function.py`：

```sh
unshare --net sh -c 'ip link set lo up && exec python3 scripts/test-dnscrypt-source-function.py \
  --candidates /inputs/normal --baseline-home /opt/lovestdns-validation/current \
  --work /tmp/source-function --cases upstream,combined'
```

默认覆盖 HTTP/1.1/HTTP/2、直连/实际 SOCKS、三个缓存容量、短 TTL 和冻结 MosDNS 的受控分流；baseline-home 只提供只读组件，测试数据全部新建。该工具不生成 CPU、吞吐或延迟验收结论，详见 [组合验证方案](../../../docs/dnscrypt-combined-plan-2026-11-09.md)。UDP 权限入口可显式选择 `--cases upstream,combined`；没有选择 cache 时不要求 `--cache-candidate`。

组合已完成两次可复现构建、30 组功能、八组 UDP 权限及三组源码 race 检查；结论和未覆盖范围见 [组合验证结果](../../../docs/dnscrypt-combined-results-2026-11-09.md)。功能通过不等于默认安装或性能验收通过。

## 原生整包隔离候选

`scripts/build-dnscrypt-native-trial.py` 从摘要已确认、全部文件校验通过的旧原生归档生成匹配的 control/combined 两包。两包同样刷新当前 backend/runtime/installer；前端、wheels、模板和原核心/依赖保持原归档字节，combined 另有 DNSCrypt 普通源码制品、锁和清单。旧制品保留用于追溯；启动包装器指向新静态制品，不修改默认组件锁。

```sh
python3 scripts/build-dnscrypt-native-trial.py \
  --baseline-archive /inputs/original-native.tar.gz --baseline-sha256 EXPECTED_SHA256 \
  --candidate /inputs/normal/combined --output /new/native-trials --version trial.VERSION
```

`scripts/test-dnscrypt-native-trial.py` 在调用者已创建的独立 network namespace 内，用真实安装器完成新装、升级、RDB 复用/新写入、显式回退、坏包拒绝和启动失败自动恢复。它只接受全新 `dnstrial-*` 实例，自建 systemd drop-in，结束会停止并禁用该实例、保留备份。它不替代持续观察，不能用于在用实例。

```sh
ip netns add lovestdns-dnstrial
ip -n lovestdns-dnstrial link set lo up
ip netns exec lovestdns-dnstrial python3 scripts/test-dnscrypt-native-trial.py \
  --packages /new/native-trials --fixtures /inputs/normal --work /new/native-run \
  --instance dnstrial-example --netns-path /run/netns/lovestdns-dnstrial
ip netns delete lovestdns-dnstrial
```

当前工具按 41 的 Debian 13 amd64、CPU 4–7、768 MiB 实例上限编排，主机依赖必须预先齐全；它不会主动安装系统依赖。夹具须由当前源码重建，显式 `--route both` 支持同一原生模板启动直连/SOCKS 两路，并继续按来源地址分别计数；夹具默认仍只接受 direct。实际 DoH/ALPN、源路径和答案都会核对。范围及隔离要求见 [原生验收方案](../../../docs/dnscrypt-native-trial-plan-2026-11-09.md)。

Debian 13 amd64 实测完成 17 个检查点、434 次查询，包括旧 RDB/新写入复用和两类恢复。输入准备失败记录、包摘要与未完成关卡见 [原生验收结果](../../../docs/dnscrypt-native-trial-results-2026-11-09.md)。默认升级及性能验收仍未放行。

## Debian 12 用户态与容器候选

普通功能工具支持 `--host-netns-path`：在 `network=none` 容器内，将宿主机 `/proc/1/ns/net` 只读挂载为参照文件后传入。工具要求自身 namespace 与参照不同；默认仍比较 `/proc/1/ns/net`，适用于原先的 `unshare` 用法。容器用户态测试共享宿主内核，不能代替 Debian 12 原生 systemd 安装或多内核验收。

`build-dnscrypt-container-trial.py` 只接受已存在的完整本地 image ID、普通 combined 源码候选和全新输出目录。它从旧组件 Alpine 镜像生成同样更新 app/runtime/backend 的两份匹配镜像，保留旧前端和依赖，combined 加入 DNSCrypt、锁及来源清单。RUN 步骤关闭网络；基础镜像通过经 image ID 核验的全新本地标签引用，使用 `--pull=false`。构建核对冻结核心及实际候选摘要，不改变默认 Dockerfile 或组件锁。

```sh
python3 scripts/build-dnscrypt-container-trial.py \
  --base-image sha256:FULL_LOCAL_BASE_IMAGE_ID --candidate /inputs/normal/combined \
  --output /new/container-trials --tag-prefix lovestdns:dnstrial-example
python3 scripts/test-integrated-container.py \
  --image CONTROL_IMAGE_ID --upgrade-image COMBINED_IMAGE_ID --offline \
  --fixture-container DISPOSABLE_FIXTURE --fixture-dir /new/fixture \
  --evidence-dir /new/private-evidence
```

离线分流模式须另传 `--fixture-container NAME --fixture-dir /new/fixture`；专用夹具容器必须已按 `network=none` 启动，使用 `scripts/fixtures/dnscrypt-container.py`、当前 DoH 夹具与一次性证书生成该目录。项目测试容器共享此隔离 namespace，HTTP 经 `docker exec` 访问，不发布端口；该脚本不会自行拉起或清理调用者提供的夹具。可用 `--cpuset-cpus` 和 `--cpus` 指定试验资源。测试只创建带标记的新 volumes/容器，校验 control 到 combined 及返回 control；RDB 标记保留不是完整 DNS 缓存命中证明。`--evidence-dir` 要求全新私有目录，会在收尾时复制本轮合成数据、配置和日志，再清理测试容器及 volumes；该目录不应公开发布。

本轮范围见 [容器兼容方案](../../../docs/dnscrypt-container-compat-plan-2026-11-09.md)，不能据此批准性能或默认升级。

实测 Debian 12 用户态 30 组、13,574 次查询及 Alpine 完整容器升级/显式切回均通过，期间修复了管理面自动重载争锁；原失败、工具修正及未完成关卡见 [兼容结果](../../../docs/dnscrypt-container-compat-results-2026-11-09.md)。

同目录的 `doh-fixture` 与 `fixture-manifest.json` 用于验证实际 HTTP/1.1 / HTTP/2 和 ALPN；`paced-udp` 与 `generator-manifest.json` 固定发送器。发送器用单调时钟安排请求，明显落后时顺延，不追发错过的时隙。

`benchmark-dnscrypt-source.py --help` 列出独立网络命名空间中的运行参数，默认使用新发送器；`--load-generator dnsperf --dnsperf /path/to/dnsperf` 仅用于显式对照原发送器。每组同时检查答案、TLS、实际 SOCKS 路由、协议及内核逐包发送节奏。验证次序和预先固定的门槛见 [普通候选方案](../../../docs/dnscrypt-source-candidate-plan-2026-11-09.md)。

## DNSCrypt 2.1.20 Linux UDP 接收队列

`dnscrypt-2.1.20-udp-receive-buffer.patch` 只将接收端 `SO_RCVBUFFORCE=4096` 改为 `SO_RCVBUF=256*1024`。普通申请受 `net.core.rmem_max` 约束，不修改全局 sysctl，也不要求 `CAP_NET_ADMIN`。发送队列、报文处理、TLS、安全校验、缓存和协议选择保持原实现。

上游在具备 `CAP_NET_ADMIN` 时会把接收队列强制缩小，Linux 的 `getsockopt` 报告值是请求值的两倍。无此能力时，原强制调用失败且错误被忽略，通常继续使用内核默认队列，因此测试需要同时核对实际能力与 `SO_RCVBUF`，不能只比较配置或源码常量。

诊断构建方法见 `scripts/build-dnscrypt-diagnostic.py --help`，使用 `--version 2.1.20 --buffer-patch`。该产物包含采样入口，只能用于诊断，不能复制到正式服务或作为发布组件。

确定性 socket 回归使用 `packaging/components/tests/dnscrypt_udp_buffer_test.go`，测试实际的上游 `udpListenerConfig()`，并非重新实现监听代码。把选定源码中的 `setsockopts_linux.go` 和测试复制到同一临时目录，在启用 loopback 的独立 Linux 网络命名空间执行：

```sh
go test -v -count=1 setsockopts_linux.go dnscrypt_udp_buffer_test.go
```

接收端暂不读取、连续发送 128 个 256 字节 UDP 包，再核对每个包和实际缓冲区。在 41 的当前内核限制下，原实现（有能力）实际 8192 字节，收到 9 / 128；补丁为 425984 字节，收到 128 / 128；原实现去掉 NET_ADMIN 后为 212992 字节，也收到 128 / 128。前者的失败是反例证据，不能删除或计作测试通过。

这证明了短时积压承受能力，不证明长期吞吐、DoH 延迟、CPU 或 RSS 已达标。发布前仍需将补丁纳入可追溯的正式源码构建，补齐权限组合、资源边界、实际 DNS 协议与原性能验收。

普通完整制品的权限检查使用 `scripts/test-dnscrypt-udp-runtime.py`：要求独立网络命名空间与 NET_ADMIN，以显式覆盖有/无该能力和 IPv4/IPv6。它验证实际能力、kernel socket 队列/丢包、缓存命中和最大接收长度问题，不是性能测试。用新的 work 运行：

```sh
unshare --net sh -c 'ip link set lo up && exec python3 scripts/test-dnscrypt-udp-runtime.py \
  --baseline-home /opt/lovestdns-validation/current \
  --candidates /inputs/normal --cache-candidate /inputs/cache \
  --work /tmp/udp-runtime'
```

候选目录应有当前源码锁对应的 upstream/buffer/udp、DoH 夹具及清单，cache 单独指定；缓存候选按同锁 `--variants cache` 显式构建。旧强制小队列组预计会丢包，`passed` 表示复现预定契约，`lossless` 才表示该组没有丢包。20 组普通制品的实际结果和单内核边界见 [权限验证报告](../../../docs/dnscrypt-udp-runtime-results-2026-11-09.md)。没有修改全局 sysctl，也未批准默认安装。

## DNSCrypt 2.1.20 UDP 报文分配

`dnscrypt-2.1.20-udp-packet-allocation.patch` 把约 4KiB 的接收缓冲区移到监听循环之外，收到问题后复制实际报文字节，交给原有同步 / 异步处理路径。每个请求仍拥有独立字节，最大接收长度保持 4095 字节。没有增加对象池，也没有让异步处理器引用可被下次接收覆盖的缓冲区。

对应诊断构建选项为 `--packet-patch`，可与 `--buffer-patch` 分别使用或组合。堆采样先只改变分配补丁；组合只做功能 smoke，不混作 CPU 提速比较。

`dnscrypt_udp_ownership_test.go` 让 32 个异步处理器等待全部接收结束后再访问各自报文，覆盖 32、63、255、512、4095 字节，校验独立内容、缺失和重复。测试使用从选定 `proxy.go` 原样提取的 `udpListener()`，其余依赖由最小处理器替身提供。准备临时目录的提取方式：

```python
from pathlib import Path
source = Path('/path/to/verified-source/dnscrypt-proxy/proxy.go').read_text()
start = source.index('func (proxy *Proxy) udpListener(')
end = source.index('\nfunc ', start + 1)
Path('listener.go').write_text('package main\nimport ("net";"runtime";"time")\n' + source[start:end])
```

把所有权测试复制到同一目录并命名为 `listener_test.go`，在独立网络命名空间执行 `go test -v -count=1 listener.go listener_test.go`。原实现与补丁均须通过；在另一个临时副本中把复制故意改回 `buffer[:length]` 应失败，证明测试能检出缓冲区别名。该替身测试不覆盖完整 DNS 协议；完整程序还需验证答案、路由、TLS、缓存和丢包。
