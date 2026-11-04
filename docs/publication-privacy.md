# 公开材料与本地记录

公开文档只使用示例主机和保留地址。现场验收、迁移过程、实例标识、缓存统计、连接信息和凭据保存在仓库外或被忽略的本地目录；不要把这些记录粘贴进提交说明、Issue、Release 或 CI 日志。

提交前运行 `python scripts/check-public-privacy.py --staged`，发布前运行 `python scripts/check-public-privacy.py --history --ref HEAD`。在当前克隆启用 `git config core.hooksPath .githooks`，推送钩子会检查待推送提交的完整历史，并拒绝备份和编辑器引用。CI 还会检查公开分支、标签及可达历史。扫描规则有识别范围，人工审阅仍需确认新截图、附件和自定义凭据格式。

历史清理后请从 GitHub 重新克隆。旧克隆应先备份未提交工作，再迁移到干净基线；不要合并旧分支、恢复旧标签或使用 `git push --mirror`。旧备份仅供本地恢复，不能再次发布。作者邮箱使用 GitHub 的 `users.noreply.github.com` 地址。

清理分支和标签不会自动清除已下载副本、GitHub 缓存、旧提交视图或 fork 网络中的不可达对象。平台缓存清理由 GitHub 支持处理，处理边界见[官方说明](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository)。
