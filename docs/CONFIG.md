# 配置说明

完整示例见 [`config.example.yaml`](../config.example.yaml)，每项都有注释。
最小可用配置只有 `region`、`pickup`、`products`、`notify` 四项。

## 地区与范围

`region` 可选：`cn`（中国大陆）、`hk`、`mo`、`tw`、`jp`、`sg`、`au`、`my`。

### pickup

```yaml
pickup:
  cities: ["上海 上海"]     # 走 location= 查询，一次返回该地点周边全部门店
  stores: ["R683"]          # 走 store= 查询，只查指定门店
```

**`cities` 是压降请求量的关键**：上海 12 家店只发 1 次请求，而不是 12 次。

大陆站必须写「省 市」——只写城市或只写省份都会被 Apple 拒绝，返回空门店。
直辖市写成「北京 北京」。

多个城市可能互相覆盖：实测 `location=北京 北京` 会同时返回天津 3 家店，
配两个城市反而重复请求。用 `./run.sh check` 能看到实际覆盖了哪些门店。

## 型号

### 手写零件号

```yaml
products:
  - part: MJT74CH/A
    name: iPhone 18 Pro 256GB
```

零件号在 Apple 购买页 URL 上：`https://www.apple.com.cn/shop/product/MJT74CH/A`。

### 自动发现

```yaml
auto:
  - category: iphone
    family: iphone-18-pro      # 留空 = 该品类全部机型
    filters:
      color: [黑色, 银色]       # 包含匹配，不区分大小写
      capacity: [256GB, 512GB]
```

从购买页实时读取，新型号上市当天就能覆盖。先用 `./run.sh parts` 核对。

## 时间策略

```yaml
interval: 300       # 查询间隔（秒）
jitter: 0.2         # ±20% 随机浮动，固定节奏本身是机器人特征
```

**间隔下限**：单出口 120 秒；启用轮换（`clash_api` + `rotate_routes: true`）
后降到 5 秒。5 秒是物理下限——单轮查询本身要 2–6 秒，再短会请求重叠。

### 退避与失效告警

```yaml
backoff_ladder: [600, 1200, 2400, 3600, 7200]   # 被拦后逐级等待（秒）
degraded_alert_after: 4                          # 连续多少轮失败后推送告警
```

`degraded_alert_after` 很重要：监控静默失效比漏掉一次补货更糟，用户会以为
「一直没货」，实际上程序早就查不动了。

### 静默时段

```yaml
quiet_hours: "23:00-08:00"     # 简写形式
# 或
quiet_hours:
  enabled: true
  start: "23:00"
  end: "08:00"
  mode: defer                  # silent = 只记日志；defer = 结束后合并补发
```

### 重复提醒

```yaml
repeat_when_in_stock: true
repeat_interval: 1800          # 有货时每 30 分钟再提醒一次
```

## 通知

多个渠道可同时启用，任一失败不影响其他。

### Bark

```yaml
notify:
  - type: bark
    url: https://api.day.app/你的设备Key
    group: Apple
    level: timeSensitive       # 可穿透专注模式
    sound: bell                # 可选
    volume: 7                  # 0–10，仅 critical 生效
```

`level` 建议保留 `timeSensitive`——补货提醒属于「稍后看到就晚了」的类型。

### Telegram / Webhook / 邮件

```yaml
  - type: telegram
    token: env:TG_TOKEN
    chat_id: env:TG_CHAT_ID

  - type: webhook
    url: env:FEISHU_WEBHOOK
    template: feishu           # feishu / dingtalk / wecom / text

  - type: smtp
    host: smtp.example.com
    port: 465
    security: ssl              # ssl / starttls / none
    username: me@example.com
    password: env:SMTP_PASSWORD
    to: me@example.com
```

### 密钥管理

任何值都可以写 `env:变量名`，从环境变量读取。配合 `.env` 或 systemd 的
`EnvironmentFile` 使用，避免密钥进仓库：

```bash
# .env
BARK_URL=https://api.day.app/xxx
```

## 网络

```yaml
proxy: http://127.0.0.1:7899    # 查询浏览器的出口
proxy_bypass: "127.0.0.1,localhost"
```

轮换相关配置见 [ROTATION.md](ROTATION.md)。

## 网页面板

```yaml
panel_token: ""    # 空 = 不需要令牌。监听公网地址时必须设置
```

面板默认只监听 `127.0.0.1`，不需要令牌。要用 `--host 0.0.0.0` 暴露到公网，
**必须**同时设 `panel_token`——面板没有用户体系，裸奔等于把监控目标和
通知密钥全公开。

## 其他

```yaml
state_file: data/state.json   # 状态与历史，重启不丢
log_level: INFO               # DEBUG 会打印每次请求的细节
```
