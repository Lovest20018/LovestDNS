# 旧独立 WebUI 构建材料

`web-docker-ghcr.yml.disabled` 是原独立 WebUI 的 GitHub Actions 文件，内容保持冻结，仅从活动工作流目录移出。LovestDNS 仓库不再执行它，不会发布旧 WebUI 镜像。冻结校验仍按原逻辑路径核对这份文件。

`web/`、根目录两个 sidecar Compose 文件及 `releases/standalone-20261024-source.zip` 保留为兼容和构建依赖。独立 WebUI 的实际发布仓库仍为 [PaoPaoDNS-WebUI](https://github.com/Lovest20018/PaoPaoDNS-WebUI)。
