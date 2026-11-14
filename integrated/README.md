# LovestDNS 集成版开发说明

当前应用版本：`0.1.0-dev.lovestdns-native.20261114.query1.2`，包含[真实查询观测 MVP](../docs/query-recording-mvp-2026-11-12.md)、采集时钟修复与六项交互修复；同源原生预发布包已公开，固定源码、归档摘要和同包验证见[query1.2 发行记录](../docs/native-query12-release-2026-11-14.md)。此前[主线记录](../docs/query11-mainline-2026-11-14.md)保留 query1.1 阶段的验证范围。默认安装入口仍固定 `.11`，选择本预发布需显式传入版本。支持 [Debian 原生 systemd 部署](../docs/lovestdns-native-deployment.md) 与源码构建的 Docker 集成版。固定契约、样例、摘要和复核命令见 [v1 提供方基线](../docs/contracts/v1/README.md)，源码分区见 [结构说明](../docs/project-layout.md)。

历史版本号、镜像和容器标识用于对应已有契约和验收证据。API `kind`、报告类型、数据文件名和内部运行路径沿用原值，保持兼容。

独立 WebUI 已固定为 `standalone-20261024` 源码检查点。集成版通过独立后端和前端覆盖层复用该检查点；构建在临时目录中组装，不修改 `web/`。

## 已实现

- 固定上游镜像 digest，构建时校验关键脚本和配置模板，防止补丁应用到不匹配的核心。
- 将核心组件交给进程管理器监督，WebUI 与 DNS 分别管理；有界启动等待、停止顺序和日志轮转。
- 独立采集进程通过 Unix socket 提供只读快照；递归/转发 Unbound 使用不同控制 socket。
- 组件状态、两路 Unbound 的查询增量与命中率、Redis 内存/DB/持久化信息。缺失数据明确显示不可用，组件重启后不跨计数周期计算增量。
- 集成版 DNS 查询支持 A、AAAA、CNAME、NS、MX、TXT、SRV、PTR、SVCB、HTTPS，读取 PaoPaoDNS Additional TXT 诊断标记。SVCB/HTTPS 参数当前按编号和十六进制展示。
- 保存配置时记录历史，保留最多 20 个版本/文件，并设置总内容容量上限。支持预览与单文件恢复，恢复仍执行内容校验。
- 核心日志限量读取及 Token 脱敏；真实入口查询记录默认关闭，开启后按容量和时间保留，不保证全量。
- Web 以管理员密码登录并可记住会话；首页展示核心进程、监听、缓存与通道统计，配置页提供原位应用和样本查询。初始密码及重置方式见原生部署说明。
- `/api/v1/` 提供版本化集成接口，支持实例识别、能力发现、只读观测 Token、请求编号、OpenAPI 契约和外部 DNS 联调；接口说明见 [集成 API](../docs/integration-api.md)。
- 首次接入保留已知用户输入的副本，不执行上游删除底层配置的初始化步骤；保持 `redis_dns_v2.rdb` 和逻辑 DB 0/1。
- 缓存预算考虑 cgroup 内存上限并预留面板运行空间。

`config_apply` 与 `redis_snapshot` 按实际运行模式和维护服务就绪状态返回；配置中心可预览并应用 custom_mod，系统页可提交受控 Redis 后台快照。初始化环境变量和 Unbound 模板修改仍需按提示重启服务。保存、恢复和任务完成不声称已经验证每条规则生效。能力接口新增 `deployment=native/container` 和实际 DNS 查询端口，原生版 Web 使用同一套管理接口。

本项目提供 DNS 专用管理面板与 API：配置中心提供历史/差异/恢复，诊断页提供最近入口查询、主动查询、最多四条链路对照及 JSON 报告导入导出。`cache_flush`、`client_policies` 仍未开放；被动查询记录不提供完整流量计数、缓存命中或分流路径推断。

## 源码固定与验证

```sh
python scripts/freeze-standalone.py
```

检查点清单位于 `packaging/standalone.freeze.json`，原始源码 ZIP 位于 `releases/standalone-20261024-source.zip`。清单对文本按 LF 计算哈希，允许 Windows/Linux 的换行差异；ZIP 另有原始字节 SHA-256。旧 WebUI 工作流原样归档到 `packaging/legacy/web-docker-ghcr.yml.disabled`，冻结校验仍使用原逻辑路径；新仓库不会执行或发布旧 WebUI 镜像。

## 构建与启动

在具备 Docker 的 Linux 主机上，从仓库根目录执行：

```sh
cp .env.integrated.example .env
# 生成随机 Token 并填入 .env 的 WEB_UI_TOKEN；按需修改端口和数据目录
openssl rand -hex 32
nano .env
python3 scripts/build-components.py --output .build/components-bundle
docker compose -f docker-compose.integrated.yaml build
docker compose -f docker-compose.integrated.yaml up -d
```

访问 `http://127.0.0.1:8080`。默认新建独立 DNS 数据卷和面板状态卷，不自动接管已有 PaoPaoDNS 数据。DNS 使用宿主机 53/TCP、53/UDP；Web 默认仅绑定回环地址，均可通过 Compose 变量调整。`DNS_DATA_DIR` 和 `DNS_STATE_DIR` 可改为宿主机绝对路径；已有 Compose 的项目名和卷名保持不变，避免意外改用空卷。

开发验证可先设置 `DNS_HOST_PORT=1053`、`WEB_HOST_PORT=18080`，避免占用现有端口。容器健康状态目前检查核心进程及采集结果，不能替代真实域名解析验证。

上游基镜像已锁定。新增 Python 依赖版本已固定；安装 Alpine 系统依赖仍使用上游配置的 edge 软件源，因此当前构建尚不具备所有系统包的完全可复现性。候选发布需要记录实际组件版本，并完成运行验证后再稳定化系统基础层。

## 历史数据接入

先停止旧核心并完整备份原数据和镜像/环境变量，使用克隆的数据卷测试迁移。不要把仍在运行的旧核心和集成版挂到同一份可写数据。

本版会在独立状态卷中保存一份已知配置输入的初始副本。它不是完整数据备份：不复制 Redis RDB、大型数据文件或任意引用目录。原有底层模板原样保留；仅在生成的运行配置中增加本地统计接口。

RDB 文件保持原位；本版没有改动 Redis/Unbound 二进制，但依旧需要针对旧部署的实际版本验证读取与缓存命中。迁移兼容性必须按旧实例的实际版本单独验证。内存缓存无法完整迁移，过去未记录的查询历史也无法从 RDB 恢复。升级失败时使用原镜像和原始备份回退。

## 本地开发与检查

```sh
pip install -r web/backend/requirements.txt -r integrated/tests/requirements.txt
python -m pytest integrated/tests scripts/tests -q
python -m pytest web/backend/tests -q
node --experimental-strip-types --test integrated/frontend/tests/*.test.ts
python scripts/build-integrated-ui.py --reuse-dependencies
```

无现成依赖时省略 `--reuse-dependencies`，脚本将在 `.build/integrated-web` 安装依赖。后端本地开发需将 `web/backend`、`integrated/backend`、`integrated` 加入 `PYTHONPATH`，并设置 `DATA_DIR`、`SUITE_STATE_DIR`、`SUITE_RUN_DIR` 到测试目录。`SUITE_DIST_DIR` 可指向构建产物的绝对目录。使用 Flask 启动 `server:app`；没有容器采集器时运行信息显示不可用。

有 Docker 时执行隔离验收：

```sh
docker build -f packaging/integrated/Dockerfile -t lovestdns:test .
python scripts/test-integrated-container.py --image lovestdns:test
```

测试只创建随机命名的临时容器/卷，不映射宿主机 DNS 端口；覆盖两种 CNAUTO 模式、v1 管理/只读权限、实例身份保留、采样新鲜度、UDP/TCP 链路对照、配置历史与恢复、幂等配置应用、停止并恢复 MosDNS、Redis RDB 重建容器后保留，以及 Web 进程重启。RDB 标记保留不等于证明所有历史 DNS 缓存跨版本可命中。

`.github/workflows/integrated-check.yml` 提供相同构建和运行验收，只验证，不推送镜像。Windows 开发电脑没有 Docker；2026-10-26 已通过 Xterminal 在 validation-host 完成 Linux amd64 隔离构建/运行验收并保留开发预览。地址、测试路由修正、失败回退证据及尚未验收范围见 [validation-host 验收记录](../docs/verification.md)，仍不表示稳定发布就绪。

2026-10-25 本地收尾验证：151 项 Python 测试（分进程运行集成版与独立版套件）、14 项前端测试、TypeScript/构建/ESLint 和独立版冻结校验通过。浏览器实测配置历史、差异、预览文件恢复及链路错误展示。运行验收脚本已补充上述容器场景，但尚未运行；未验证真实核心下的应用/恢复或发布就绪性。

开发预览可在 `.build/integrated-web` 设置 `SUITE_DEV_API_URL=http://127.0.0.1:8081` 后运行 `npm run dev -- --host 127.0.0.1 --port 5174`。此变量只控制 Vite 开发代理，生产接口仍走同源 `/api/v1/`。
