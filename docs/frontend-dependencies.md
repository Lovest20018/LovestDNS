# 集成版前端依赖

LovestDNS 的依赖清单及锁文件位于 `integrated/frontend/package.json` 和 `integrated/frontend/package-lock.json`。冻结的 `web/` 继续提供通用源文件，构建覆盖层携带自己的依赖，不再沿用独立版锁文件。

`scripts/build-integrated-ui.py` 将两个源码目录组装到 `.build/integrated-web`，执行 `npm ci`、依赖审计、构建及 lint。Docker 前端阶段和 CI 使用同一套集成版依赖。任何审计条目都会阻止这一构建路径通过；审计服务失败也会保留失败状态。`--reuse-dependencies` 仅用于开发，不执行依赖安装和审计，不作为发行验收。

本轮删除未引用的直接依赖 `js-yaml` 及其类型声明，更新锁文件中的兼容版本。开发工具依赖的 minimatch 暂时固定覆盖到 **11.0.5**：上游审计公告指出 7.x 至 11.0.0 受影响，11.0.1 起有修复；覆盖项用于避免将 ESLint 与 TypeScript 工具降回较早的大版本。完整源码 lint、TypeScript 构建及前端测试共同验证兼容性。后续上游工具自身依赖安全版本后可移除此覆盖。[上游公告](https://github.com/advisories/GHSA-g3hv-75wg-m249)。

构建清单同时记录锁文件摘要、Node 版本、源码提交/工作区状态、源文件摘要集合和前端产物摘要。原生打包入口在启动 Docker 前检查该清单：仅接受干净提交上通过 `npm ci` 构建的前端，并要求源码、锁文件及完整产物列表和摘要一致；开发依赖复用、过期或修改过的文件均拒绝。原生包另带源码摘要清单，打包时对测试版本的版本号替换由包内逐文件 manifest 核对。
