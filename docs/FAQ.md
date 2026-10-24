# 常见问题

## 出现 HTTP 541

`541` 是 Apple 自定义的状态码（不是标准 HTTP 状态码），表示「这次请求不像来自
浏览器」，**不代表无货**。

按顺序排查：

1. **确认用的是真实 Chromium**。裸 HTTP 客户端即使补齐 UA/Cookie/Referer
   也会被拒。本程序用 Playwright 启动的浏览器在页面内发 fetch。
2. **看是不是单个节点的问题**。配了轮换的话，日志会显示哪个节点被拒。
   实测 33 个节点里通常只有个别节点被标记。
3. **调大间隔**。单出口时尤其重要。
4. **不要反复重启**。同一 IP 的持续请求会延长封禁。

## 报 CERTIFICATE_VERIFY_FAILED

本机 Python 缺根证书，不是网络问题。典型出现在 python.org 的 macOS 安装包：
系统没给这个 Python 装 CA 包，所有 HTTPS 请求都验不过。

修复（运行官方脚本）：

```bash
/Applications/Python\ 3.12/Install\ Certificates.command
```

验证：

```bash
python3 -c "import ssl; print(ssl.create_default_context().cert_store_stats()['x509_ca'])"
# 应大于 0；0 表示缺证书
```

本项目内置了兜底（`app/tls.py` 显式指向 certifi），装了依赖就不会遇到。
**不要**用 `verify_ssl=False` 绕过——那等于关掉证书校验。

## 报「浏览器资料目录被占用」

同一目录不能同时开两个实例。先停掉另一个：

```bash
pgrep -fl 'apple-stock|browser-profile'
```

确认没有实例在跑，说明上次异常退出留了残留锁：

```bash
rm -f data/browser-profile/Singleton*
```

## 报「浏览器未找到」

按优先级找：PATH 里的 chrome/chromium → 常见安装路径 → Playwright 自带的。

VPS 上最省事的是让 Playwright 下载：

```bash
python -m playwright install --with-deps chromium
```

## 配了 Bark 但收不到推送

1. 面板点「发送测试推送」，看具体报错
2. 确认 key 正确：Bark App 首页的地址形如 `https://api.day.app/xxxxxxxx`
3. 检查通知等级：`passive` 只进通知列表不亮屏，容易以为没收到
4. 检查 iOS 是否允许 Bark 通知
5. 如果开了静默时段，提醒不会推送——面板顶部会有提示条

## 界面显示「已保存」但配置没变

确认 `config.yaml` 的修改时间和内容：

```bash
ls -la config.yaml
```

也检查进程是否有写权限。

## 节点全部超时 / 连不上

如果同时开着别的 TUN 型代理（Shadowrocket、Surge 等），它们会劫持整机流量，
连 Clash 连接自己节点服务器的连接也被抓走，表现为「所有节点突然全部超时」。

解决：给 Clash 绑定物理网卡，或让监控走独立端口。详见 [ROTATION.md](ROTATION.md)。

## 补货了但没收到提醒

先看面板的「监控日志」，确认那一轮的状态：

- `Apple 拦截` / `节点不通` → 查询失败，不是没货，看上面的排查
- `无补货` → 确实没货。核对门店、颜色、容量、零件号是否和官网一致
- `N 个补货` → 检测到了，检查通知渠道

## 其他注意事项

- **不保证能一直查到。** Apple 的风控策略会变，任何基于网页接口的监控都可能失效。
- **提醒不代表能买到。** 库存随时变化，最终以下单页结果为准。
- **没有下单功能。** 这是刻意的：自动下单属于高强度请求，会显著提高风控概率。
- 本项目与 Apple 无关联。请自行控制请求频率，遵守 Apple 的服务条款。
