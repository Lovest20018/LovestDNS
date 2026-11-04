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

根目录 `install.py` 默认选择已经验收的 `.11`，按精确标签 `v0.1.0-dev.lovestdns-native.20261104.11` 下载 GitHub Release 中的 `lovestdns-native-debian-amd64.tar.gz` 及同名 `.sha256`，核对归档、路径、清单和载荷后再执行包内安装器。摘要与归档来自同一发布渠道，可检测传输损坏，不代替对发布账号的信任。可使用 `--version <不含v的版本>` 选择其他已发布版本；预发布包仍需先验收自己的环境。

只下载且验证、不安装：

```sh
python3 install.py --download-only ./verified-release
```

离线包安装（SHA-256 应通过可信渠道核对）：

```sh
sudo python3 install.py --package ./lovestdns-native-debian-amd64.tar.gz --sha256 <已核对的SHA-256> --config ./lovestdns-install.env
```

`--instance validation` 创建命名实例，`--import-data /path/to/offline-copy` 只用于首次导入一致化的独立副本。安装入口不会替你停止旧 DNS 或 Docker，也不会为迁移读取旧生产目录。现有 LovestDNS 仍建议使用下文的控制命令升级和回退。

## 开发者构建安装包

在 Linux amd64 构建机，准备 Docker、Python venv/pip、Node/npm 和源码：

```sh
python3 scripts/build-integrated-ui.py
python3 -m venv .build/native-tools
.build/native-tools/bin/python scripts/build-native-package.py \
  --frontend-dist .build/integrated-web/dist \
  --output .build/lovestdns-native.tar.gz
```

构建校验 `packaging/upstream.lock.json` 中的上游脚本 SHA-256，并为 Debian 12 的 Python 3.11、Debian 13 的 Python 3.13 下载固定版本的 wheel。输出安装包与同名 `.sha256` 文件。包内 `manifest.json` 列出全部载荷文件摘要和上游来源；`--version` 可覆盖包及 API 的版本号，用于独立测试版本。

## 首次安装

将安装包与通过可信渠道取得的 SHA-256 一起传到目标测试机；校验摘要后再解压。以下命令应在包目录执行：

```sh
sha256sum -c lovestdns-native.tar.gz.sha256
tar -xzf lovestdns-native.tar.gz
cd lovestdns-0.1.0-dev.lovestdns-native.20261104.11
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

systemd 仅在服务自己的挂载命名空间内将持久化数据映射到 `/data`，并隔离 `/tmp`。不会修改宿主机 `/etc/resolv.conf`；内部 DNSCrypt 与 Unbound 监听回环地址，DNS 对外地址由实例配置决定。Web 不挂 Docker socket。

首次导入使用 `install --import-data /path/to/offline-data-copy`，目标数据目录必须为空。必须是完整、离线或已经一致化的独立副本，不能直接共享旧服务仍在写入的目录。保留原实例的环境变量、完整 `/data`、镜像及回退材料；在新实例中逐项还原业务参数。`redis_dns_v2.rdb` 及 DB 0/1 沿用原上游核心格式，Unbound/MosDNS 的纯内存缓存不会随文件迁移。

Web 编辑的 `custom_env.ini` 用于 DNS 参数与规则变量。原生版禁止其覆盖 PATH、Python/动态加载器变量及服务运行目录、绑定端口、Web 认证等保留变量；这些参数应在 `/etc/lovestdns/lovestdns.env` 中修改，再执行控制命令重启。普通自定义规则变量仍可使用。

## 日常维护、升级与回退

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

`health` 检查组件与采样新鲜度，并确认 DNS 协议有应答；它不证明所有公网域名、规则、代理或 IPv6 都正常。改变初始化环境、Unbound 模板后按 Web 提示重启服务；规则更新与核心升级分开处理。

## 跟进上游更新

`UPDATE` 支持 `no/daily/weekly/monthly`；规则库由原生调度组件调用适配后的上游更新脚本，不改宿主机 cron 或软件源。核心版本固定在 `packaging/upstream.lock.json`：上游发布新版本后，更新来源、校验脚本与依赖，再重新构建 LovestDNS 包并在测试机跑验收，最后通过 `upgrade` 发布。不会直接用上游容器入口脚本覆盖原生服务。

原生验收脚本只接受带 `.lovestdns-test-instance` 标记的命名测试数据目录，会切换运行模式、写入人工规则、注入一次 MosDNS 启动失败、检查恢复，并保留测试预览。**仅用于测试实例**：

```sh
sudo python3 scripts/test-native-instance.py --instance validation
```


## 网页配置与生效

日常配置直接在“基础设置、域名分流、TTL 规则、高级配置”中修改和保存，无需重新部署软件。基础设置只更新 `custom_env.ini` 中对应变量，保留其他自定义变量与注释；页面分别显示目标覆盖值和核心启动时采集的运行值。

- 基础设置、`custom_env.ini`、`unbound_custom.conf` 保存后，在配置中心执行“重启 DNS 核心”。确认后生成后台任务，先确认 Redis 快照，再按固定顺序停止和重建 DNS 组件，Web、采集器与登录会话继续运行。
- `custom_mod.yaml` 可在配置中心预览并单独应用；域名列表和 TTL 的自动监听依赖当前运行参数。保存提示只说明监听条件，最终请使用查询测试验证规则。
- 新配置初始化失败时尝试恢复旧运行文件、旧环境和先前运行的 DNS 组件；保存的目标文件保留，修正后可再次执行。查看任务中的回退结果及维护页 `bootstrap` 初始化日志。回退失败时请使用主机控制命令处理。
- 配置编辑后，切换页面、刷新或关闭浏览器会提醒未保存修改。不要将“文件已保存”当作规则验证结果。
- 网页保存和历史恢复携带读取时的文件版本；其他页面已改动同一文件时会阻止覆盖并保留草稿，请重新读取后合并。外部工具直接写磁盘不参与 API 的锁。
- 配置中心展示服务器保存的应用记录，其他浏览器也可读取。记录比较成功任务输入与当前文件，并绑定相关 DNS 进程；进程改变、记录不可用时显示待确认。文件监听自动加载与域名效果仍需查询验证。

网页重启不会改变服务绑定地址、DNS/Web 端口、数据路径、systemd 资源限制或软件版本。这些主机参数仍在实例 `.env` 中修改，并通过对应 `lovestdnsctl` 控制命令重启整项服务；软件更新使用已验证的安装包。
