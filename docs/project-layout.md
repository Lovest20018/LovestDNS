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

安装包内 `manifest.json` 记录载荷与组件来源。当前正式版为 `0.1.0`；前端构建清单绑定提交、干净工作区、固定依赖和源码摘要，原生打包器拒绝开发依赖复用或来源不一致的界面。使用已校验基础包刷新时保持组件及依赖字节和权限，并保留基础包与当前源码的两组来源记录。

当前使用文档留在 `docs/`，发行说明位于 `docs/releases/`；历史计划、候选记录和实验结果位于 `docs/archive/`，不再作为当前版本说明。旧 WebUI 冻结源码和原 ZIP 继续保留以支持兼容与来源核对。
