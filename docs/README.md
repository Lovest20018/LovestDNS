# LovestDNS 文档

## 安装与维护

- [Debian 原生安装、升级、备份与回退](lovestdns-native-deployment.md)：推荐的独立部署入口。
- [Docker / PVE CT 兼容部署](lovestdns-pve-deployment.md)：源码构建与旧数据接入边界。
- [DNS 诊断](dns-diagnostics.md)：查询、链路比较、报告与宿主机统计。

## 开发与接口

- [项目分区与发布](project-layout.md)：各目录职责、构建依赖、GitHub 目标和发布产物。
- [开发、构建与回归](../integrated/README.md)。
- [DNS 管理 集成 API](integration-api.md)。
- [v1 契约、确定性样例与摘要](contracts/v1/README.md)。

## 验证与历史

- [Debian 原生验收](verification.md)。
- [Linux 容器历史验收](verification.md)。
- [操作与 UI 优化实施记录](verification.md)。
- [旧独立 WebUI](standalone-webui.md)：单独仓库继续保留，本仓库只复用冻结代码。
