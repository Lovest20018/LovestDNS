# 家用单设备试用

完成[半小时受控验收](home-use-results-2026-11-11.md)后，下一步是让一台实际设备使用候选 DNS，检查常用网站与所需分流。先确认服务端，再切换这一台设备；路由器的 DHCP 下发保持，便于逐步接入和恢复。

## 接入顺序

1. 在目标主机核对活动版本、四个组件摘要、配置、Redis 保存状态和磁盘余量，使用已核对的候选包及[原生升级入口](lovestdns-native-deployment.md)。保存一致的旧版本、数据与配置备份，保留现有回退点。
2. 确认该主机的局域网地址和 **UDP／TCP 53 端口**可用，再修改命名实例的服务参数并重启。普通设备 DNS 设置只填写 IP，不能直接填入测试端口；不要把高位端口查询成功当成系统接入完成。
3. 从试用设备直接查询该 IP 的 53 端口，检查常用域名的 A 和需要的 AAAA，核对预期分流。完成后记录该设备原来的 IPv4／IPv6 DNS、是否由 DHCP 提供，以及正在使用的网络接口，再切换该接口。
4. 切换后核对系统实际 DNS，使用普通浏览器和常用应用。独立配置的浏览器加密 DNS、VPN 或其他接口仍可能选择别的解析器，需确认实际路径；页面能打开不等于经过了候选服务。IPv6 DNS 下发也需一起核对，无需因此关闭 IPv6 协议。
5. 遇到实际问题，先恢复该设备原来的 DNS 设置；服务端需要回退时，使用已有备份和原生控制命令，保留候选期间产生的数据。原来使用 DHCP 与静态 DNS 的设备应分别恢复对应方式。

Windows 可用 `Get-DnsClientServerAddress` 查看指定接口；设置静态 DNS 会覆盖该接口的 DHCP DNS，`-ResetServerAddresses` 恢复 DHCP 提供的 DNS。恢复命令应以接入前记录的方式为准。[Microsoft 查看说明](https://learn.microsoft.com/en-us/powershell/module/dnsclient/get-dnsclientserveraddress)、[设置与恢复说明](https://learn.microsoft.com/en-us/powershell/module/dnsclient/set-dnsclientserveraddress)

## 从设备进行只读检查

[`scripts/check-home-dns.py`](../scripts/check-home-dns.py)只发出少量普通 DNS 查询，不修改客户端 DNS、服务配置或缓存策略。它使用标准库和仓库中的报文解析模块，不需要 Web Token 或管理员密码。在 Python 3.11+ 的仓库根目录运行，下面的 `192.0.2.10` 是保留示例地址，须换成实际主机 IP：

```sh
python scripts/check-home-dns.py --server 192.0.2.10 --port 53 \
  --domain www.qq.com --domain www.baidu.com --domain example.com \
  --output .build/home-client-a.json
```

默认两轮、A 查询、每个域名分别走 UDP／TCP；可用 `--record-type A --record-type AAAA` 检查两种记录。传输地址族由 `--server` 的 IP 决定，AAAA 查询本身不表示使用 IPv6 传输。报告目录需要存在，输出文件必须是新文件。

工具核对事务与问题、应答格式、CNAME 路径和目标类型记录，UDP 截断时要求 TCP 回退成功。TTL 为零可以是有效应答。`no_records` 表示取得 NOERROR 但没有目标记录，单列记录可用性，不冒充超时，也不自动认定为配置错误；它可能来自站点本身或当前解析策略。

退出码 0 表示所有查询均取得目标记录；2 表示至少一条查询缺少目标记录或失败。中断时保留已经完成的结果，`state=running` 且 `passed=false`。报告包含域名、IP 和记录，应保存在私有目录；工具不核对服务端实际组件版本，不证明网站 HTTP 可用、缓存命中或具体分流路径。

## 本轮准备状态

2026-11-11，在现有测试服务的高位 DNS 端口完成三个样本域名、两轮 UDP／TCP 的 **12 次 A 查询，全部取得目标记录**。随后增加 AAAA，共 **24 次合法 NOERROR 应答**，其中 20 次取得目标记录，`example.com` 的四次 AAAA 应答无目标记录；未据此认定 IPv6 故障。该轮报告按要求保留为 `passed=false`，不将“有应答”改记为“所有记录可用”。

对标准 53 端口的两次 UDP／TCP 请求均取得结构有效、类型匹配的 A 答案，但同一域名的结果与高位测试端口明显不同。传输与报文检查通过不等于已确认服务身份或解析语义正确；端口归属、DNS 接管与活动配置仍待主机侧核对，不能直接覆盖该端口或据此切换客户端。

这些是现有服务的接入预检，尚未重新核对其活动组件版本，不能归为新版组合的局域网验证。目标服务的候选升级、53 端口归属确认、设备 DNS 切换和实际应用试用尚未执行。客户端工具的 12 项定向测试通过，涵盖不相关答案、错误事务／问题／类型、畸形报文、TCP 回退失败、单次读取截止时刻及中断保留。

后续同日只读主机检查已确认：53 端口由 `sniffbox` 持有，旧 console4.1 测试实例监听 UDP/TCP 15453。尚未核对 sniffbox 的完整转发策略或改变端口归属；随后新版组件与 home1 已完成候选升级及短时观察，见[验收记录](home1-validation-results-2026-11-11.md)；设备切换仍未执行。详见[home1 实现与接入状态](home-console-implementation-2026-11-11.md)。

## 独立 Linux 进程试用补充

随后按用户要求改用另一台可远程操作的 Linux 主机，在每次查询的私有挂载空间中使用候选 DNS，完成五分钟 21 次 libc 解析及 UDP/TCP 受控路径配对，见[本轮结果](home1-linux-client-results-2026-11-11.md)。服务端只对该来源暂时重定向标准 53，结束后规则和私有挂载均已清理；宿主机与 Windows 的持久 DNS 设置没有切换。这补充了跨主机系统解析器路径证据，尚不等于浏览器、手机或完整终端日常使用验收。
