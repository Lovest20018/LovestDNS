# 源码分区与发布

LovestDNS 从原 `Lovest20018/PaoPaoDNS` fork 继续发布，更名后的地址为 `https://github.com/Lovest20018/LovestDNS`。保留原 fork 的 Git 历史和上游归属；`Lovest20018/PaoPaoDNS-WebUI` 是独立项目，本次不向它推送。上游版本固定在 `packaging/upstream.lock.json`，旧提交仍可用于核对原始源码。

## 产品与运行代码

`integrated/backend/` 负责 Web/API、浏览器认证、配置版本与历史。`integrated/runtime/` 负责观测、维护任务、共享配置生成和应用记录。`integrated/frontend/` 是当前 LovestDNS 界面，在构建时覆盖冻结 WebUI 的对应文件。

`web/` 保留 `standalone-20261024` 原始检查点，继续提供部分通用源代码；LovestDNS 的前端依赖清单和锁文件由 `integrated/frontend/` 单独维护。新增功能写入 `integrated/`。冻结清单和原 ZIP 不改写，`scripts/freeze-standalone.py` 校验 39 个逻辑文件。唯一移动的旧 CI 文件原样归档到 `packaging/legacy/`，不再作为活动工作流运行。

## 部署与构建

`install.py` 是部署者使用的公开入口，仅下载、核验并调用安装包。`packaging/native/install.py` 是包内的 systemd 安装/升级/回退实现，`packaging/native/runtime/` 是原生进程管理适配器。原生运行不使用 Docker；构建机通过 Docker 提取固定上游二进制及依赖。

`packaging/integrated/` 和 `docker-compose.integrated.yaml` 保留 Docker 兼容部署。根目录的 `docker-compose-web.yaml`、`docker-compose.sidecar.yaml` 只用于旧独立 WebUI，不能当作 LovestDNS 的生产安装入口。

`scripts/` 存放前端组装、原生打包、契约导出及隔离验收；测试代码分别位于 `integrated/tests/`、`integrated/frontend/tests/`、`scripts/tests/`，冻结版回归位于 `web/` 对应测试目录。

## 发布产物与工作流

- `.github/workflows/integrated-check.yml`：推送 main、PR 或手工触发时验证；构建容器用于隔离验收，不发布镜像。
- `.github/workflows/native-package.yml`：手工构建原生包并保存 Actions artifact；不会自动安装到服务器或发布到 Releases。
- GitHub Release 使用 `v<版本>` 标签，包含 `lovestdns-native-debian-amd64.tar.gz` 和同名 `.sha256`。候选版本标记为 prerelease；不得把未验收的新构建覆盖为旧候选。
- `.build/` 和本地 `releases/` 产物不推送 Git，例外仅为原有冻结源码 ZIP。真实配置、密码、Token、数据、RDB、状态、历史、日志和截图不纳入源代码发布。

安装包内 `manifest.json` 记录载荷和原始上游二进制摘要。安装入口默认下载已发布的 `.11`；历史控制台候选 `20261109.console4.1` 尚未公开发布，由明确的本地提交构建，使用本地包和摘要部署。其默认包沿用固定上游核心，DNSCrypt 三补丁和 Redis/Unbound 更新继续独立验收。前端构建清单记录该提交、干净工作区状态和源码摘要，包内源码摘要与 API 契约基线一致；原生打包入口拒绝开发依赖复用和来源不符的前端。当前源码候选为 `20261111.home1`，尚未打包或部署；后续变更使用新候选版本，保留既有候选及其验收记录。详见 [源码与版本收口](source-version-closure-2026-11-06.md)。
