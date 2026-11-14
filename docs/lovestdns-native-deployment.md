# LovestDNS Debian 原生部署

当前原生候选面向 **Debian 12/13、Linux amd64、systemd**。DNS 核心、Web、采集器和维护服务由同一个 systemd 服务管理，运行时不需要 Docker。构建机仍使用 Docker 从固定 digest 的上游镜像中提取已校验的核心和模板；安装包携带原始核心二进制、专用 musl 加载器/依赖库及 Python 离线 wheel，不覆盖 Debian 的 libc，也不使用 chroot 或 Alpine 根文件系统。

## 发布包安装入口

仓库为 `Lovest20018/LovestDNS`，由原 `PaoPaoDNS` fork 更名。独立 `PaoPaoDNS-WebUI` 仓库不参与本次发布。

```sh
curl -fL https://raw.githubusercontent.com/Lovest20018/LovestDNS/main/install.py -o install.py
curl -fL https://raw.githubusercontent.com/Lovest20018/LovestDNS/main/.env.native.example -o lovestdns-install.env
chmod 600 lovestdns-install.env
nano lovestdns-install.env
sudo python3 install.py --config ./lovestdns-install.env
sudo lovestdnsctl health
sudo lovestdnsctl password
```

根目录 `install.py` 默认选择 `0.1.0`，按精确标签下载 GitHub Release 中的 `lovestdns-native-debian-amd64.tar.gz` 及同名 `.sha256`，核对归档、路径、清单和载荷后再执行包内安装器。摘要与归档来自同一发布渠道，可检测传输损坏，不代替对发布账号的信任。可使用 `--version <不含v的版本>` 选择其他已发布版本；发行状态以项目首页及对应 Release 为准，首次部署应先验收自己的环境。

只下载且验证、不安装：

```sh
python3 install.py --download-only ./verified-release
```

离线包安装（SHA-256 应通过可信渠道核对）：

```sh
sudo python3 install.py --package ./lovestdns-native-debian-amd64.tar.gz --sha256 <已核对的SHA-256> --config ./lovestdns-install.env
```

`--instance validation` 创建命名实例，`--import-data /path/to/offline-copy` 只用于首次导入一致化的独立副本。安装入口不会替你停止其他 DNS 或 Docker，也不会为迁移读取旧生产目录。已有 LovestDNS 的同实例升级会执行下文的备份和短暂重启流程。

## 开发者构建安装包

在 Linux amd64 构建机，准备 Docker、Python venv/pip、Node/npm 和源码：

```sh
python3 scripts/build-integrated-ui.py
python3 -m venv .build/native-tools
.build/native-tools/bin/python scripts/build-components.py --output .build/components-bundle
.build/native-tools/bin/python scripts/build-native-package.py \
  --components-bundle .build/components-bundle \
  --frontend-dist .build/integrated-web/dist \
  --output .build/lovestdns-native.tar.gz
```

构建校验 `packaging/upstream.lock.json` 中的上游脚本 SHA-256，独立组件来源与构建依赖由 `packaging/components.lock.json` 固定。两种发行方式复用同一组件包，定制 MosDNS 及原运行库保留；新版组件的私有依赖独立打包。构建还为 Debian 12 的 Python 3.11、Debian 13 的 Python 3.13 下载固定版本的 wheel。输出安装包与同名 `.sha256` 文件；`manifest.json` 列出全部载荷摘要、上游和实际组件来源。`--version` 可覆盖包及 API 的版本号。单组件对照与缓存验收见 [组件构建说明](../packaging/components/README.md)。

需要将已验收的 DNSCrypt `combined` 源码候选加入独立试用包时，在上述原生打包命令中显式增加 `--dnscrypt-source-candidate /path/to/normal/combined`。该目录必须包含正常静态 amd64 二进制与对应 `source-manifest.json`。打包器核对源码锁、三个补丁、工具链声明和二进制摘要，拒绝诊断构建及其他变体；保留输入组件清单，另写实际载荷清单和补丁来源。该参数不更改默认组件来源，新包保留候选、未批准发布及性能未验收标记。

已有核验过的新组件原生包时，也可用 `scripts/refresh-native-package.py --base <原包> --base-sha256 <可信摘要> --frontend .build/integrated-web/dist --output <新包>` 更新应用与界面。脚本要求当前源码干净且已提交，前端为同提交的 `npm ci` 构建；上游锁、模板适配脚本、组件锁和 Python 依赖改变时拒绝刷新。所有核心、私有库、组件与 wheels 保持原字节，Linux 执行权限从原归档保留；新包分别记录继承来源与当前源码，不声称重新编译了组件。正式发行时可显式增加 `--release`，仅接受不带开发后缀的 `主.次.修订` 版本，并记录功能发行批准；性能验收标记仍为 false，不由该开关推导性能结论。默认省略该参数保持候选标记。

## 首次安装

将安装包与通过可信渠道取得的 SHA-256 一起传到目标测试机；校验摘要后再解压。以下命令应在包目录执行：

```sh
sha256sum -c lovestdns-native.tar.gz.sha256
tar -xzf lovestdns-native.tar.gz
cd lovestdns-0.1.0
cp native.env.example /root/lovestdns-install.env
chmod 600 /root/lovestdns-install.env
nano /root/lovestdns-install.env
sudo python3 install.py install --config /root/lovestdns-install.env
```

默认 DNS 为 `0.0.0.0:53/UDP+TCP`，Web 为 `127.0.0.1:8123`。需要局域网访问时，将 `WEB_BIND_ADDRESS` 改为主机内网 IP。新装若端口被占用，会退出并保留现有服务；不会自动停止占用端口的程序。首次安装会通过 apt 补齐 Python venv、inotify、iproute2、procps、xz、CA、bash 等依赖，因此系统依赖安装仍需 Debian 软件源。Python 应用依赖从包内 wheel 离线安装。

勾选“记住登录状态”最多保留 7 天。网页凭据与运维平台 Token 独立：`WEB_UI_TOKEN` 未指定时仍生成随机值，供需要配置/维护权限的 API 客户端使用；另设不同的 `SUITE_READ_TOKEN` 供观测接入。安装输出只提示配置路径，不输出凭据。

命名测试实例使用：

```sh
sudo python3 install.py --instance validation install --config /path/to/private-test.env
sudo lovestdns-validationctl health
```

命名实例分别使用 `/opt/lovestdns-validation`、`/etc/lovestdns-validation`、`/var/lib/lovestdns-validation` 和对应控制命令。多个原生实例仍需为 DNS/Web 分配不同端口；智能分流的内部 `5301–5304` 端口目前固定，同一宿主机只适合运行一个开启智能分流的原生实例。

## 持久化与旧数据副本

默认实例路径：

| 路径 | 内容 |
| --- | --- |
| `/opt/lovestdns/releases/` | 版本载荷和每版 Python venv |
| `/opt/lovestdns/current` | 当前版本链接 |
| `/etc/lovestdns/lovestdns.env` | 受限权限的实例参数与 Token |
| `/var/lib/lovestdns/data` | 完整 DNS 配置、规则、Redis RDB |
| `/var/lib/lovestdns/state` | 面板历史、实例身份、任务及轮转日志 |
| `/var/lib/lovestdns/backups` | 升级前的一致备份和回退时保留的被替换数据 |
| `/var/lib/lovestdns/updates` | 网页更新任务、专用代理设置、结果与最多五份私有安装日志；下载文件临时保存 |

systemd 仅在服务自己的挂载命名空间内将持久化数据映射到 `/data`，并隔离 `/tmp`。不会修改宿主机 `/etc/resolv.conf`；内部 DNSCrypt 与 Unbound 监听回环地址，DNS 对外地址由实例配置决定。Web 不挂 Docker socket。

首次导入使用 `install --import-data /path/to/offline-data-copy`，目标数据目录必须为空。必须是完整、离线或已经一致化的独立副本，不能直接共享旧服务仍在写入的目录。保留原实例的环境变量、完整 `/data`、镜像及回退材料；在新实例中逐项还原业务参数。`redis_dns_v2.rdb` 及 DB 0/1 沿用原上游核心格式，Unbound/MosDNS 的纯内存缓存不会随文件迁移。

Web 编辑的 `custom_env.ini` 用于 DNS 参数与规则变量。原生版禁止其覆盖 PATH、Python/动态加载器变量及服务运行目录、绑定端口、Web 认证等保留变量；这些参数应在 `/etc/lovestdns/lovestdns.env` 中修改，再执行控制命令重启。普通自定义规则变量仍可使用。

## 规则数据更新

原生版的规则更新与网页软件升级是两项独立任务。`UPDATE=daily/weekly/monthly` 控制根提示、GeoIP、tracker 和全局标记数据的更新周期；`UPDATE=no` 关闭规则自动更新。程序版本目录保持只读，压缩输入和根提示保存到 `/data/rule-assets`，解压后的活动规则仍使用既有 `/data` 路径。用户 tracker 增补按原有合并方式保留。

每轮先获取并校验所有启用的数据。根提示沿用 Internic 发布的 MD5，其他三项使用上游 SHA-256；另验证 XZ 解压限制、GeoIP 格式和全局标记的内嵌 SHA-512。下载仅使用固定官方上游及 CDN，GitHub 官方内容 API 为回退入口，不接受客户端任意 URL。单次请求和整轮有时限，文件及解压体积有上限。配置了 SOCKS5 时先使用既有 MosDNS 下载器，再尝试直连；故障信息不输出代理凭据。

下载或校验失败保留活动规则。替换前取得维护锁并备份运行文件，暂停监听器，以同目录原子替换写入数据，调用既有加载流程后重新启动监听器，避免替换 inode 后丢失监视。加载或健康检查失败时尝试恢复旧文件及运行配置；不停止 Redis，也不清缓存。规则变化可能短暂重启相关 DNS 组件。成功后记录本轮时间，失败按 1、2、4 分钟递增重试，最长间隔一小时；结果保存在 `state/rule-update.json`，详细过程在“维护与日志 → updater”。状态为成功只证明下载、格式校验和本机服务响应，不证明每条规则符合实际业务。

## 日常维护、升级与回退

从 query1.3 起，Debian 原生安装包含网页更新助手。在 **设置 → 服务信息 → 软件更新** 点击“检查更新”，核对版本及发行说明，再点击“更新到此版本 → 确认更新并重启”。页面会区分下载、校验、备份安装和最终结果；重启期间继续重连，关闭网页不会取消主机任务，重新进入此页可查看结果。没有后台自动升级。

默认从官方 `Lovest20018/LovestDNS` 最新正式发行版中检查更新，要求同时包含 amd64 原生包和 SHA-256 文件。主动选择预览通道后，会检查最近 20 个完整发行版，包含明确标注的预发布版。只接受固定仓库和精确标签；确认时绑定已检查的归档摘要，安装前重新核对发行信息、外层归档及包内每个文件。版本列表或下载失败会如实报错，尚未安装的失败不停止 DNS。

### 更新网络与通道

v0.1.0 在“软件更新 → 更新网络与通道”提供正式版/预览通道，以及直连、HTTP CONNECT 和 SOCKS5。填写代理 IPv4/IPv6 地址、端口及可选认证，点击“保存并检查更新”，以真实官方发行版检查结果判断连通性。SOCKS5 将 GitHub 域名交给代理解析；HTTP 使用 CONNECT 后校验目标站点 TLS。版本检查、SHA-256 文件、安装包及允许的 GitHub 重定向全部使用同一配置，代理失败不会转为直连。直连模式不读取主机代理环境变量；命令行独立安装入口仍沿用自己的网络环境。

这项配置只供软件更新助手使用，不修改主机默认网关、DNS 分流、规则数据更新器或其他组件的代理。规则更新继续采用上文的既有下载机制。代理端点仅接受 IP 地址；账号密码通过代理协议交给指定服务器，应选择受信任的代理及网络。HTTPS 内容仍端到端验证，认证握手本身不提供额外链路加密。

设置由已修改初始密码的管理员会话和 CSRF 校验写入私有 `updates/network.json`，文件权限为 `0600`，接口不回传密码。相同协议、IP、端口和账号下密码留空保留；修改其中任一项须重新填写认证信息。清除认证时，SOCKS5 同时清除账号；选择直连清除全部代理信息。任务运行期间不可改设置；保存会清除旧的版本检查结果，安装前须重新检查。设置保留在 `updates`，不随 `state` 数据回退恢复旧值。

该功能包含在 v0.1.0；旧 query1.4 及更早版本需要先升级才能使用。正式版通道默认开启，预览通道需显式选择；变更通道会使旧检查失效，重新检查后才能确认安装。验证范围与未完成项见[发行说明](releases/v0.1.0.md)及[已知限制](roadmap.md)。

首次从不含更新助手的版本迁移，需要通过当前安装入口升级一次，并保留实际实例名；之后可在前端完成同实例软件升级：

```sh
sudo python3 install.py --version 0.1.0 --instance <实例名>
```

默认实例省略 `--instance`。Docker 部署继续由宿主机 Compose 管理。网页更新仅允许已修改初始密码的管理员会话与 CSRF 校验，不接受外部管理/只读 Token、任意 URL、主机命令或其他实例参数。更新开始后暂停配置、认证设置和维护任务写入，DNS 查询与状态读取仍可用；有未保存草稿时请先保存或还原。

原生安装器为当前实例安装独立的 `lovestdns[-实例名]-update.path` 和 `-update.service`：前者监听私有更新请求，后者运行包校验与原生安装器。助手独立于 DNS/Web 服务，按实例限制可写目录；任务与结果位于 `updates`，不随 `state` 回退而丢失。正常结束时清除临时下载，保留最近五份安装日志和原有升级备份。意外中断不会自动重放安装；结果恢复会重新核对当前版本、摘要收据与健康状态。磁盘不足会在停止 DNS 前拒绝安装，备份仍需由主机按保留策略管理。

控制命令与显式回退继续可用：

```sh
sudo lovestdnsctl status
sudo lovestdnsctl health
sudo lovestdnsctl restart
sudo lovestdnsctl logs
sudo lovestdnsctl password
sudo lovestdnsctl upgrade /path/to/lovestdns-native.tar.gz --sha256 <已核对的SHA-256>
sudo lovestdnsctl rollback
```

升级先核对包摘要、文件清单与 Python/二进制依赖，再停止当前实例，将 DNS 数据、面板状态、服务参数和 unit 保存为一致备份。新版本启动与真实 DNS 协议探测通过后才记为成功。启动失败时恢复先前版本及其数据/配置，并尝试启动、检查原服务。显式回退同样恢复当时备份；回退后将失去升级后新增的活动配置和缓存，但被替换的数据仍单独保存在 `backups/pre-rollback-*`，便于复核。

升级和回退都可能短暂中断 DNS。备份保留在本机，需要给版本、数据和多份备份留足磁盘空间；本机一致备份不能替代异机备份。重新安装已经回退过的候选时会生成独立 release 目录，不覆盖先前载荷。可将已校验的新包解压后用该包的 `install.py install` 恢复升级路径。

从 `.15` 及更早版本升级到外部 API 默认关闭的版本时，请使用仓库根目录的安装入口执行 `sudo python3 install.py --package /path/to/lovestdns-native.tar.gz --sha256 <已核对的SHA-256>`。它会核对新包并使用包内的新安装器，保留已有配置并生成升级备份。旧版 `lovestdnsctl upgrade` 的健康检查依赖 Token API，会在 API 关闭后误判失败并回退；完成这次升级后可继续使用新版控制命令。

`health` 检查组件与采样新鲜度，并确认 DNS 协议有应答；它不证明所有公网域名、规则、代理或 IPv6 都正常。改变初始化环境、Unbound 模板后按 Web 提示重启服务；规则更新与核心升级分开处理。

## 跟进上游更新

`UPDATE` 支持 `no/daily/weekly/monthly`；规则库由原生调度组件调用适配后的上游更新脚本，不改宿主机 cron 或软件源。原上游来源由 `packaging/upstream.lock.json` 固定，独立组件由 `packaging/components.lock.json` 固定。组件更新需要重建组件产物和 LovestDNS 包，完成历史缓存兼容、功能及性能对照后通过 `upgrade` 发布。网页“设置”显示包内声明的独立组件版本与清单摘要。不会直接用上游容器入口脚本覆盖原生服务。

原生验收脚本只接受带 `.lovestdns-test-instance` 标记的命名测试数据目录，会切换运行模式、写入人工规则、注入一次 MosDNS 启动失败、检查恢复，并保留测试预览。**仅用于测试实例**：

```sh
sudo python3 scripts/test-native-instance.py --instance validation
```


## 网页配置与生效

外部 Token API 默认关闭。需要接入时在“服务信息 → API 与 Token”开启，或使用主机命令 `sudo lovestdnsctl api on`；`api off` 关闭，`api status` 查看。设置持久保存且立即生效，关闭时网页配置与 DNS 解析继续使用。`lovestdnsctl health` 直接检查本机采集器、Web 页面和 DNS 协议，不依赖外部 API。升级前已设置 Token 也不会自动开启接入。

日常配置直接在“基础设置、域名分流、TTL 规则、高级配置”中修改和保存，无需重新部署软件。基础设置只更新 `custom_env.ini` 中对应变量，保留其他自定义变量与注释；页面分别显示目标覆盖值和核心启动时采集的运行值。

- 基础设置、`custom_env.ini`、`unbound_custom.conf` 保存后，在配置中心执行“重启 DNS 核心”。确认后生成后台任务，先确认 Redis 快照，再按固定顺序停止和重建 DNS 组件，Web、采集器与登录会话继续运行。
- `custom_mod.yaml` 可在配置中心预览并单独应用；域名列表和 TTL 的自动监听依赖当前运行参数。保存提示只说明监听条件，最终请使用查询测试验证规则。
- 新配置初始化失败时尝试恢复旧运行文件、旧环境和先前运行的 DNS 组件；保存的目标文件保留，修正后可再次执行。查看任务中的回退结果及维护页 `bootstrap` 初始化日志。回退失败时请使用主机控制命令处理。
- 配置编辑后，切换页面、刷新或关闭浏览器会提醒未保存修改。不要将“文件已保存”当作规则验证结果。
- 网页保存和历史恢复携带读取时的文件版本；其他页面已改动同一文件时会阻止覆盖并保留草稿，请重新读取后合并。外部工具直接写磁盘不参与 API 的锁。
- 配置中心展示服务器保存的应用记录，其他浏览器也可读取。记录比较成功任务输入与当前文件，并绑定相关 DNS 进程；进程改变、记录不可用时显示待确认。文件监听自动加载与域名效果仍需查询验证。

网页重启不会改变服务绑定地址、DNS/Web 端口、数据路径、systemd 资源限制或软件版本。这些主机参数仍在实例 `.env` 中修改，并通过对应 `lovestdnsctl` 控制命令重启整项服务；软件更新使用已验证的安装包。

## 安装身份与来源记录

`20261112.home2` 起，设置页分开展示持久实例 ID、命名服务和维护命令、应用提交、前端构建来源、继承基础包与组件版本声明。实例目录和命令只取原生安装器约定的服务环境；旧包或非标准路径缺失时显示“未记录”，不会据显示名称生成默认实例命令。当前页面不核对运行进程的实际可执行文件。

新安装器在 `/opt/lovestdns[-实例名]/receipts/<发布目录名>.json` 保存安装收据，绑定服务名、发布路径、版本和安装清单 SHA-256。经过摘要校验的 `upgrade` 还记录外层归档摘要；直接运行解压后的 `install.py install` 没有归档输入，因此该项保持未记录。收据不加入归档，避免循环自哈希；选择旧 release 回退时读取该 release 对应的收据，不沿用新版摘要。收据不匹配时隐藏归档摘要并提示。

从 home1 或更早版本安装此候选时，使用更新后的仓库根目录 `install.py --package <包路径> --sha256 <已核对摘要> --instance <实例名>`。入口核对新包后调用包内支持收据的新安装器；兼容旧包的原安装参数。该路径会临时解压两份载荷，空间预算需计入两份目录。旧控制命令升级到新包可能仍使用旧安装逻辑，收据缺失会如实显示，不能事后填入未经核对的历史归档摘要。
