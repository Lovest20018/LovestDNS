# DNS 诊断与宿主机快照

「概览 → DNS 诊断」支持以下操作：

诊断区按「域名查询 / 链路对照 / 健康检查」切换，只显示当前任务所需的字段。点击当前模式的主按钮执行；传输协议在「查询选项」中调整。切换模式会保留已填写的域名和目标地址。

- **查询**：查询一个域名的 A / AAAA / CNAME 记录，显示返回码、地址、TTL、耗时和实际协议。
- **健康检查**：查询 `www.baidu.com`、`www.google.com` 的 A 记录。必须拿到查询名称或其 CNAME 链上对应的 IN 类型目标记录才算已解析。
- **解析链路对照**：对同一个域名和记录类型，比较最多三条页面预设链路。默认端口为分流入口 `53`、原生递归 `5301`、加密转发路径 `5304`；每条链路的地址和端口可修改，也可取消选择。
- **导出报告**：下载当前一次成功操作的完整 JSON 结果。后续成功操作会替换当前报告，输入或导入失败时保留上一份结果，请及时导出。
- **导入报告**：读取 WebUI 导出的报告或本仓库宿主机采集脚本生成的 JSON。仅在浏览器内解析，不上传后端，不执行文件内容，不写入 `/data`；大小上限为 2 MB。导出已导入的文件时保留其原始内容。

结果区先显示取得目标记录的样本数量、需检查项和采集时间。单次查询与异常结果默认展开；多链路的成功结果默认折叠，可点击查看完整域名、解析地址和 TTL。导入的报告明确标记为「离线记录」，不代表当前状态。原有报告会在新操作成功后替换，输入或导入失败时仍可查看和导出上一份报告。

UDP 应答设置了截断标志时，WebUI 自动使用 TCP 获取完整响应；也可选择直接使用 TCP。健康检查固定使用 UDP 并自动回退 TCP。状态区分已解析、无目标记录、DNS 错误、应答不完整、超时、网络错误和无效应答。

TTL 为 0 的目标记录仍可解析成功。单次查询不会判断缓存是否刷新；DNS 成功也不证明网站、视频下载或 TLS 连接可用。探测使用普通 DNS 请求，不会清空缓存，可能预热少量缓存条目。

## sidecar 的网络目标

请求从 **WebUI 后端所在网络** 发出。`127.0.0.1` 通常指 WebUI 容器自身，不是 PaoPaoDNS 容器或宿主机。

本仓库同时部署的 compose 默认可用 `paopaodns` 服务名。给已有容器单独添加 sidecar 时，需要让两个容器处于可互通的网络，或填写 WebUI 能访问的 PaoPaoDNS 地址。地址留空的对照行沿用上方「DNS 服务」。

`5301` / `5304` 是否能访问取决于实际监听、功能开关、网络和端口映射；对照失败本身不能判断 DNS 核心故障。端口不适用时可取消选择。`5304` 包含 Unbound 等缓存层，其耗时不能直接当作远端加密上游耗时。无需为诊断将内部端口映射到公网。

后端仍支持环境变量：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DNS_TEST_SERVER` | `paopaodns` | API 未指定目标时使用的服务地址 |
| `DNS_TEST_PORT` | `53` | API 未指定目标时使用的端口 |
| `DNS_TEST_TIMEOUT` | `3` | 每个传输阶段的超时秒数，范围大于 0 且不超过 10；无效值回退到 3 |

页面字段显式指定目标，会覆盖 API 的默认值。UDP 转 TCP 时会获得一个新的传输超时；服务名解析仍依赖系统解析器。链路对照并行执行，每条链路都保留各自的结果，失败的链路不会隐藏其他结果。

## 采集宿主机内部统计

可选脚本位于 `scripts/paopaodns-health-snapshot.py`，需在 **Linux Docker 宿主机**运行，依赖 Python 3、Docker CLI，以及 PaoPaoDNS 容器内的 `dig`。它不在 WebUI 镜像里运行，也不要求 WebUI 挂载 Docker socket。

```sh
python3 scripts/paopaodns-health-snapshot.py \
  --container paopaodns \
  --domain www.qq.com \
  --domain www.google.com \
  --output dns-snapshot.json
```

若当前账号没有 Docker 或容器 `/proc/<pid>/root` 的读取权限，请使用已有授权的宿主机管理账号执行。脚本使用 `stats_noreset` 读取 Unbound 累计统计，不重置计数，不修改配置，不重启服务，不清空缓存，不常驻。

常用参数：

```sh
# 仅探测分流入口，跳过未启用的内部链路
python3 scripts/paopaodns-health-snapshot.py \
  --recursive-port 0 --forward-port 0 --output frontend-only.json

# 自定义容器内部端口、控制套接字和最短统计观察窗口
python3 scripts/paopaodns-health-snapshot.py \
  --recursive-port 5301 --forward-port 5304 \
  --control-socket /tmp/uc_raw.sock --interval 10 --timeout 6 \
  --output another-snapshot.json
```

脚本内部固定向容器自身 `127.0.0.1` 查询，参数填写**容器内部端口**。`--interval` 是最短统计观察窗口，不是 TTL 重复测试间隔。输出文件不可覆盖，需选择新文件名。退出码：`0` 表示探测和元数据采集完成，`1` 表示无法检查运行容器，`2` 表示只得到部分结果；部分报告仍可导入查看。缺少控制套接字或配置读取权限不会阻止保存其余查询结果。退出码不代表应用连接可用。

将 JSON 文件复制到本地后，在页面点击「导入报告」。页面显示查询结果、递归统计增量、结束时队列和运行配置指纹比较结果：

- 统计包含观察窗口内其他设备和后台请求，递归队列丢弃不能直接换算成终端解析失败。
- 检测到计数器下降或重置时，增量标记为不可比较；未采集字段显示「未采集」。
- 配置指纹一致只表示被读取文件在采样前后一致，不证明配置已加载或期间没有过短暂变化。
- 报告含域名、IP、容器和端口等信息，分享前请检查内容。

## API 与本地验证

原有 `POST /api/dns-test` 和 `GET /api/health-check` 继续保留；`available` 现在表示取得完整的目标类型记录，`responded` 表示收到通过校验的 DNS 应答。`/api/dns-test` 新增 `transport: "udp" | "tcp"`（默认 UDP）。

新增 `POST /api/dns-compare`，受相同 Bearer Token 鉴权保护。请求示例：

```json
{
  "domain": "www.qq.com",
  "record_type": "A",
  "targets": [
    {"label": "分流入口", "server": "paopaodns", "port": 53, "transport": "udp"},
    {"label": "原生递归", "server": "paopaodns", "port": 5301, "transport": "udp"}
  ]
}
```

API 每次接受 1–4 个目标，页面提供三条可编辑预设。所有目标校验通过后才开始查询，返回带版本号的 `paopaodns-dns-report` JSON。

```sh
python -m pytest web/backend/tests scripts/tests -q
npm --prefix web test
npm --prefix web run lint
npm --prefix web run build
```

前端解析测试使用 Node 内置测试运行器和 TypeScript 类型剥离，需要支持 `--experimental-strip-types` 的 Node 22.6+（或更新版）。后端测试使用模拟 DNS 报文和本地 TCP 服务，不访问公共解析器。
