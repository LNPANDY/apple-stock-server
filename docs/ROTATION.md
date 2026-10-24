# 出口轮换（Clash / mihomo）

被 Apple 拦截时自动切换节点，是最有效的抗风控手段之一。

**前提**：需要一个支持外部控制接口的 Clash 内核——ClashX、ClashVerge、
mihomo 都可以。**Shadowrocket 没有这个接口**，用不了。

## VPS 部署

最省事的是在**本机**跑仓库根目录的一键脚本（含 mihomo + systemd + 防火墙）：

```bash
python3 deploy_to_vps.py 你的VPS_IP --clash-yaml /path/to/clash-config.yaml
```

只想在**服务器上**装 mihomo 的话，用单独的脚本：

```bash
sudo bash deploy/setup-mihomo.sh /path/to/clash-config.yaml
# 或直接给订阅链接
sudo bash deploy/setup-mihomo.sh "https://example.com/api/v1/client/subscribe?token=xxx"
```

脚本会：

1. 按架构下载 mihomo（amd64 / arm64 / armv7）
2. 读取订阅或本地 YAML，**清掉机场信息假节点**（「剩余流量」「套餐到期」这类，
   它们连不通且会被自动测速选中）
3. 建 `AppleMonitor` 策略组：全部真实节点 + DIRECT
4. 开两个监听端口：
   - `7899` → 固定走该组（监控轮转用）
   - `7898` → 固定 DIRECT（家宽兜底）
5. 注册 systemd、验证 API 和两个端口的连通性
6. 打印要填进 `config.yaml` 的配置

跑完把输出里的五行贴进配置即可。

## 桌面端接入

### ClashVerge（默认只开 Unix socket）

```bash
grep external-controller-unix \
  ~/Library/Application\ Support/io.github.clash-verge-rev.clash-verge-rev/config.yaml
```

```yaml
clash_api: unix:/var/folders/.../T/verge-mihomo.sock
clash_group: Apple
proxy: http://127.0.0.1:7897
```

ClashVerge 的 `Merge.yaml`（订阅更新不会覆盖）里加：

```yaml
# 与 Shadowrocket 共存的关键：绑定物理网卡。
# Shadowrocket 开 TUN 时会劫持整机流量，连 mihomo 连接自己节点服务器的那条
# 连接也会被抓走，表现为「所有节点突然全部超时」。绑网卡后出站直接走物理网卡。
# 注意 en0 在多数 Mac 上是 Wi-Fi；改用有线/USB 网卡时要相应改接口名。
interface-name: en0

listeners:
  - name: apple-monitor
    type: mixed
    port: 7899
    listen: 127.0.0.1
    proxy: Apple
  - name: apple-monitor-direct
    type: mixed
    port: 7898
    listen: 127.0.0.1
    proxy: DIRECT
```

### ClashX

默认开 TCP 9090：

```yaml
clash_api: http://127.0.0.1:9090
clash_group: 手动切换
proxy: http://127.0.0.1:7890
```

## 怎么选策略组

| 组类型 | 能不能用 | 原因 |
| --- | --- | --- |
| 名字带 Apple 的专用组 | **最理想** | 只影响 Apple 域名，不动其他流量 |
| 「手动切换」 | 可以 | 静态组，切了就是切了 |
| 「自动选择」/ URLTest | **不要用** | 它自己会切，和本程序打架 |
| 「故障转移」/ Fallback | 不要用 | 同上 |

程序会跳过组里的嵌套组和假节点，但**嵌套组指向谁管不了**——如果组里嵌了
「自动选择」，而它选中了一个假节点，出口照样不通。所以优先选扁平的手动组。

## 轮换规则

- **每轮轮转**：默认开启。33 个节点 × 5 秒 = 每个节点 165 秒才用一次
- **被拦才切**：403/541/429 触发，冷却当前节点并换下一个
- **冷却阶梯**：5 → 10 → 20 → 30 分钟，成功一次恢复
- **按站点记账**：大陆 `www.apple.com.cn` 与海外 `www.apple.com` 分开
- **连续 10 个节点都被拒就停**：说明被拦的不只是 IP
- **全员冷却时**等最早恢复的节点，不空等退避阶梯

## 一个坑：别用 gstatic 测速判断节点能不能打 Apple

部分机场屏蔽 `gstatic.com` 这类探测域名，但 Apple 是通的。实测环境里
`gstatic` 全超时、`apple.com.cn` 走代理 0.1 秒返回 200。

所以程序**不依赖测速结果剔除节点**，只按真实的 541/403 冷却。手动验证：

```bash
curl -x http://127.0.0.1:7899 -o /dev/null -w "%{http_code}\n" https://www.apple.com.cn/
```

## VPS + 家宽出口（另一种思路）

如果程序必须跑在 VPS 上，但希望查询从家里宽带出去，可以让 VPS 通过家里的
机器做 SOCKS5 代理。

如果程序必须跑在 VPS 上，但你又希望查询从家里的宽带出去，可以让 VPS 通过
家里的机器做 SOCKS5 代理。这样 Apple 看到的是你家宽的 IP。

**这个方案成立的关键是 DNS。** 实测确认：Chrome 走 SOCKS5 时发给代理的是
**域名**（SOCKS5 的 `ATYP=3`），不是解析后的 IP。所以 DNS 也在家宽那端解析，
VPS 不会通过 DNS 查询泄漏位置。Apple 看到的来源是自洽的。

### 家里的机器：开一个 SOCKS5

最简单的是 SSH 反向隧道，不需要额外软件，也不需要公网 IP：

```bash
# 在家里那台常开的机器上执行
ssh -R 1080:127.0.0.1:1080 vps用户@你的VPS
# 前提是那台机器自己已经有 SOCKS5（例如 Clash 的混合端口 7890）
```

如果家里跑的是 Clash / mihomo，它本身就提供 SOCKS5（混合端口），
更直接的做法是把那个端口反向转发到 VPS：

```bash
# 家里执行：把本机 7890 暴露成 VPS 的 1080
ssh -R 1080:127.0.0.1:7890 vps用户@你的VPS
```

想让隧道自动重连，用 `autossh`：

```bash
autossh -M 0 -N -o "ServerAliveInterval 30" -o "ServerAliveCountMax 3" \
  -R 1080:127.0.0.1:7890 vps用户@你的VPS
```

在 VPS 的 `/etc/ssh/sshd_config` 里建议加（防止别人蹭你的隧道）：

```
AllowTcpForwarding remote
GatewayPorts no
```

`GatewayPorts no` 让转发只绑定在 127.0.0.1，不对外暴露。

### VPS 上：指向这个端口

```yaml
# config.yaml
proxy: socks5://127.0.0.1:1080
proxy_bypass: "127.0.0.1,localhost"
```

先验证出口真的变成了家宽：

```bash
./run.sh netcheck -c config.yaml
# 出口 IP 应该是你家宽的公网 IP，不是 VPS 的
```

### 注意事项

- **家宽需要能主动外连 VPS。** 反向隧道由家里发起，所以家里有公网 IP 与否
  都不影响；NAT 后面也能用。
- **VPS 会看到你的家宽 IP。** 用自己可控的 VPS，别用共享机器。
- **延迟会升高**：查询链路变成 家里 → VPS → 家里出口 → Apple。对 300 秒
  一次的场景完全无所谓。
- **隧道断了程序会失败，但不会误报无货。** 代理不通属于查询失败，状态记为
  「未知」，并进入退避；恢复后自动继续。
- **家宽 IP 也可能被拦**，只是概率比机房 IP 低。真被拦了就调大间隔。
