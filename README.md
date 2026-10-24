# Apple 直营店补货监控

盯 Apple 直营店的到店取货库存，**有货就推送提醒，不做任何下单操作**。
为长期跑在服务器上设计，实测可在 1 核 1G 的 VPS 上运行。

改造自 [`suversal/apple-store-inventory-monitor`](https://github.com/suversal/apple-store-inventory-monitor)
（Rust + Tauri 桌面应用），去掉图形界面和自动结账，换成 Python + 网页面板，
以便在无桌面环境的服务器上跑。

> 与 Apple 无关联。请自行控制请求频率，遵守 Apple 的服务条款。

## 它解决什么问题

原项目能跑，但只能在有桌面的机器上跑。想在 VPS 上长期监控，会遇到三件事：

1. **Apple 会拦「不像浏览器」的请求**。裸 HTTP 客户端会被返回自定义状态码
   `541`，哪怕 UA、Cookie、Referer 全都补齐。
2. **请求太多会被风控**。按出口 IP 计额度，同一 IP 连续查几十次就会被拒。
3. **判断错了比不报更糟**。把「查询失败」当成「无货」，用户会在真的补货时
   收不到提醒。

这三点各有对应处理，每条的实测依据见 [设计说明](docs/DESIGN.md)。

## 快速开始

### 本机（Mac / Linux 桌面）

```bash
git clone <repo> && cd apple-stock
cp config.example.yaml config.yaml
$EDITOR config.yaml                # 至少填 region、pickup、products、notify

./run.sh check -c config.yaml      # 自检：确认能拿到库存
./run.sh run   -c config.yaml --web # 面板 http://127.0.0.1:8787
```

`run.sh` 会自动建虚拟环境，不受系统 `python3` 版本影响。

### VPS（Ubuntu 20.04+，手动）

```bash
git clone <repo> && cd apple-stock
sudo bash deploy/install.sh

sudoedit /opt/apple-stock/config.yaml
sudo -u applemon /opt/apple-stock/.venv/bin/python -m app check -c /opt/apple-stock/config.yaml
sudo systemctl enable --now apple-stock
```

浏览器用 Playwright 自带的 Chromium（`install.sh` 会下载）。不用 apt 的
`chromium-browser`——Ubuntu 22.04+ 里那是 snap 包装器，在无桌面的服务器上
会卡在 snap store 连接上。

### VPS（一条命令，推荐）

本机执行，自动完成上面全部步骤 + mihomo + systemd + 防火墙：

```bash
python3 deploy_to_vps.py 你的VPS_IP
```

在终端里运行时会**逐步询问**：要不要启用多节点轮换（需 Clash 订阅，
确认后输入订阅链接或本地配置文件路径）、要不要配 Bark 推送。

给订阅**链接**时 mihomo 会每 12 小时自动更新节点（推荐，订阅过期换新
链接后无需重新部署）；给本地 YAML 文件则节点固定，换订阅需重跑部署。
全参数模式见 `python3 deploy_to_vps.py --help`。

要求本机 `~/.ssh/vps_deploy_key` 能免密登录该 VPS。重复部署安全：
已有配置（含面板令牌）会被保留。结束时打印面板地址和卸载命令。

## 最小配置

```yaml
region: cn                    # cn / hk / mo / tw / jp / sg / au / my

pickup:
  cities: ["上海 上海"]        # 大陆站必须写「省 市」，只写城市会被拒绝

products:
  - part: MJT74CH/A           # 零件号见购买页 URL：/shop/product/MJT74CH/A
    name: iPhone 18 Pro 256GB # 通知里的显示名，可省略

notify:
  - type: bark
    url: env:BARK_URL         # 密钥放 .env，别写进仓库
```

这四项就能跑。其余都是可选项，见 [配置说明](docs/CONFIG.md)。

## 关键能力

| 能力 | 说明 |
| --- | --- |
| **一次查全城** | 用 `location=` 而非 `store=`，上海 12 家店只发 1 次请求 |
| **失败不等于无货** | 541/403/429/解析失败一律记为「未知」，不覆盖上次结论 |
| **网页面板** | 本轮结果、补货历史、每轮日志（含失败原因）、可视化改配置 |
| **出口轮换** | 接入 Clash/mihomo，每轮换节点；被拦时自动切换并冷却 |
| **监控失效告警** | 连续 N 轮查不到会推送「监控已失效」，避免静默失败 |
| **静默时段** | 深夜不推送，可选「结束后补发」；按配置时区判断，不吃服务器 UTC 的亏 |
| **完整卸载** | 一条命令删干净：服务、程序、防火墙规则、Chromium、apt 库 |

## 文档

- [配置说明](docs/CONFIG.md) —— 所有配置项与取值建议
- [设计说明](docs/DESIGN.md) —— 为什么这么做，含踩过的坑
- [出口轮换](docs/ROTATION.md) —— Clash/mihomo 接入，含 VPS 一键脚本
- [常见问题](docs/FAQ.md) —— 541、证书错误、节点不通、收不到推送

## 命令

```bash
./run.sh doctor    # 检查本机环境（不需要网络）
./run.sh netcheck  # 查出口 IP，确认代理是否生效（不消耗 Apple 额度）
./run.sh parts     # 列出自动发现的零件号
./run.sh check     # 跑一轮并打印每家店的结果，不发通知
./run.sh run       # 持续监控；加 --web 开面板
```

## 开发

```bash
python -m pip install -r requirements.txt pytest
python -m pytest tests/ -q       # 148 项，全部不需要网络
```

前端在 `app/static/`（原生 HTML/CSS/JS，无构建步骤）。

## 许可

GPL-3.0-or-later。本项目是
[`suversal/apple-store-inventory-monitor`](https://github.com/suversal/apple-store-inventory-monitor)
的衍生作品，必须沿用同一许可。
