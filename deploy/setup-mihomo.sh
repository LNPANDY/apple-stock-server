#!/usr/bin/env bash
# 一键装好 mihomo + 订阅 + 监控专用端口。
#
#   sudo bash deploy/setup-mihomo.sh "你的订阅链接"
#
# 做的事：
#   1. 下载 mihomo（自动识别 amd64 / arm64）
#   2. 用订阅生成配置，加两个监听端口：
#        7899 → 走 Apple 策略组（监控轮转用）
#        7898 → 直连（家宽兜底）
#   3. 注册 systemd 服务并启动
#   4. 打印需要填进 config.yaml 的参数
#
# 只影响本机 mihomo，不碰任何已有代理软件。
set -euo pipefail

APP_DIR=/opt/apple-stock
MIHOMO_DIR=/opt/mihomo
MIHOMO_USER=mihomo
SOURCE="${1:-}"   # 订阅链接，或本地 Clash YAML 文件路径
API_PORT=9090
PROXY_PORT=7899
DIRECT_PORT=7898

die() { echo "错误：$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "请用 root 运行：sudo bash $0 \"订阅链接\""
[[ -n "$SOURCE" ]] || die "请提供订阅链接或 Clash YAML 文件路径：
  sudo bash $0 \"https://example.com/api/v1/client/subscribe?token=xxx\"
  sudo bash $0 /path/to/config.yaml"

command -v curl >/dev/null || die "缺少 curl：apt install -y curl"
command -v python3 >/dev/null || die "缺少 python3：apt install -y python3"

# ---- 1. 架构识别 ----
case "$(uname -m)" in
  x86_64)  ARCH=amd64 ;;
  aarch64) ARCH=arm64 ;;
  armv7l)  ARCH=armv7 ;;
  *) die "不支持的架构：$(uname -m)" ;;
esac
echo "==> 架构：$ARCH"

# ---- 2. 下载 mihomo ----
mkdir -p "$MIHOMO_DIR"
if [[ -x "$MIHOMO_DIR/mihomo" ]]; then
  echo "==> mihomo 已存在，跳过下载（想升级就删掉 $MIHOMO_DIR/mihomo）"
else
  echo "==> 下载 mihomo"
  URL=$(curl -s --max-time 20 https://api.github.com/repos/MetaCubeX/mihomo/releases/latest \
    | grep -o "https://[^\"]*mihomo-linux-${ARCH}-v1[^\"]*\.gz" | head -1)
  [[ -n "$URL" ]] || die "找不到 $ARCH 的 mihomo 下载地址（GitHub API 可能被限流，稍后重试）"
  curl -L --max-time 180 -o /tmp/mihomo.gz "$URL" || die "下载失败：$URL"
  gunzip -f /tmp/mihomo.gz
  mv /tmp/mihomo "$MIHOMO_DIR/mihomo"
  chmod +x "$MIHOMO_DIR/mihomo"
fi
"$MIHOMO_DIR/mihomo" -v || die "mihomo 无法运行"

# ---- 3. 拉订阅并生成配置 ----
echo "==> 读取配置来源"
SOURCE_MODE=file
if [[ "$SOURCE" == http://* || "$SOURCE" == https://* ]]; then
  SOURCE_MODE=url
  # URL 也要下载一次做格式校验；真正给 mihomo 的还是 URL 本身
  # （proxy-providers 自动更新），这里只验证它可用。
  curl -L --max-time 60 -o /tmp/sub.yaml "$SOURCE" || die "订阅下载失败"
  [[ -s /tmp/sub.yaml ]] || die "订阅内容为空"
  echo "   使用订阅链接（mihomo 将每 12 小时自动更新）"
else
  [[ -f "$SOURCE" ]] || die "文件不存在：$SOURCE"
  cp "$SOURCE" /tmp/sub.yaml || die "无法读取 $SOURCE"
  echo "   使用本地文件：$SOURCE（注意：文件不会自动更新，订阅过期需重新提供）"
fi

# 订阅可能返回 base64 的节点列表（v2ray 格式）而不是 Clash YAML。
# 那种格式 mihomo 读不了，需要先判断。
# 用 YAML 解析判断格式，而不是 grep 头部若干行——proxies 段可能出现在
# 任意位置（这份机场配置就在第 18 行，而 proxy-groups 在第 56 行）。
python3 - <<'CHECKEOF'
import sys
try:
    import yaml
except ImportError:
    sys.exit(0)   # 没装 PyYAML 就跳过检查，交给后面的生成步骤报错

with open("/tmp/sub.yaml", encoding="utf-8", errors="ignore") as fh:
    head = fh.read(4096)

if head.lstrip().startswith(("ss://", "vmess://", "trojan://", "vless://", "hysteria")):
    sys.exit("这个文件是节点链接列表，不是 Clash YAML。")
try:
    data = yaml.safe_load(open("/tmp/sub.yaml", encoding="utf-8", errors="ignore"))
except Exception as exc:
    sys.exit(f"YAML 解析失败：{exc}")
if not isinstance(data, dict) or not data.get("proxies"):
    sys.exit("配置里没有 proxies 段。确认这是 Clash / mihomo 格式的配置文件。")
CHECKEOF
check_status=$?
if [[ $check_status -ne 0 ]]; then
  die "配置格式不对（见上面的提示）。"
fi

echo "==> 生成配置"
python3 - "$MIHOMO_DIR/config.yaml" "$API_PORT" "$PROXY_PORT" "$DIRECT_PORT" <<'PYEOF'
import sys

try:
    import yaml
except ImportError:
    sys.exit("需要 PyYAML：pip3 install pyyaml 或 apt install -y python3-yaml")

out_path, api_port, proxy_port, direct_port, source_mode, source = (
    sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]),
    sys.argv[5], sys.argv[6],
)
with open("/tmp/sub.yaml", encoding="utf-8", errors="ignore") as fh:
    sub = yaml.safe_load(fh)

if not isinstance(sub, dict) or not sub.get("proxies"):
    sys.exit("订阅里没有 proxies 段，确认这是 Clash 格式的订阅")

# ---- URL 模式：用 mihomo 原生 proxy-providers，让它自己定期拉订阅 ----
# 这样订阅过期/机场换服务器时无需重新部署；我们的程序每 10 分钟刷新
# 节点列表，新节点自动进入轮转池。
# 机场的「剩余流量/套餐到期」假节点无法在这一层剔除（内容由 mihomo 拉取），
# 由监控程序按节点名过滤（routes.py 的 INFO_NODE_KEYWORDS）。
if source_mode == "url":
    provider_name = "monitor-sub"
    apple_group = "AppleMonitor"
    config = {
        "mixed-port": 7897,
        "external-controller": f"127.0.0.1:{api_port}",
        "allow-lan": False,
        "mode": "rule",
        "log-level": "warning",
        "find-process-mode": "off",
        # 健康检查用 Apple 域名：部分机场屏蔽 gstatic 这类探测域名，
        # 用它会把所有节点误判为不可用。
        "proxy-providers": {
            provider_name: {
                "type": "http",
                "url": source,
                "interval": 43200,          # 12 小时自动更新订阅
                "path": "./providers/monitor-sub.yaml",
                "health-check": {
                    "enable": True,
                    "url": "https://www.apple.com.cn/",
                    "interval": 600,
                },
            }
        },
        "proxy-groups": [
            {
                "name": apple_group,
                "type": "select",
                "use": [provider_name],
                "proxies": ["DIRECT"],      # 兜底：全部节点失效时直连
            }
        ],
        "rules": [f"MATCH,{apple_group}"],
        "listeners": [
            {
                "name": "apple-monitor",
                "type": "mixed",
                "port": proxy_port,
                "listen": "127.0.0.1",
                "proxy": apple_group,
            },
            {
                "name": "apple-monitor-direct",
                "type": "mixed",
                "port": direct_port,
                "listen": "127.0.0.1",
                "proxy": "DIRECT",
            },
        ],
    }
    import os
    os.makedirs(f"{out_path.rsplit('/', 1)[0]}/providers", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(config, fh, allow_unicode=True, sort_keys=False, width=4096)
    print("   已生成 proxy-providers 配置（12 小时自动更新订阅）", file=sys.stderr)
    sys.exit(0)

# 订阅里的端口、控制接口、DNS、TUN 都是给桌面端用的，这里全部覆盖。
for key in (
    "port", "socks-port", "redir-port", "tproxy-port", "mixed-port",
    "external-controller", "external-controller-unix", "external-controller-cors",
    "secret", "allow-lan", "bind-address", "mode", "log-level", "ipv6",
    "tun", "dns", "hosts", "profile", "listeners",
):
    sub.pop(key, None)

# mixed-port 走规则模式（普通代理端口），与监控专用端口区分开：
#   7897 → 按订阅规则分流（一般没人用，留着方便排障）
#   7899 → 固定 Apple 组（监控轮转）
#   7898 → 固定 DIRECT（家宽兜底）
# 监控只关心「能不能出网」，不需要订阅那套精细分流规则。
# 订阅的 rule-providers 常指向 cdn.jsdelivr.net，在某些网络/VPS 上拉不到，
# 加载失败会让所有走规则的流量卡死（表现为「节点全部超时」）。
# 这里改成极简规则：其余流量一律走第一个可用组。
sub["rule-providers"] = {}

sub["mixed-port"] = 7897
sub["external-controller"] = f"127.0.0.1:{api_port}"
sub["allow-lan"] = False
sub["mode"] = "rule"
sub["log-level"] = "warning"
# 按进程分流在这里没用（浏览器和日常浏览器同名），关掉省资源
sub["find-process-mode"] = "off"

groups = sub.get("proxy-groups") or []
all_proxies = [p.get("name") for p in sub["proxies"] if p.get("name")]

# 找一个「只包含真实节点」的组给监控用。优先级：
#   1. 名字带 apple 的组
#   2. 名字带「手动/select」的组
#   3. 自己建一个
# 机场常用「剩余流量」「套餐到期」这类假节点来展示套餐信息。
# 它们连不通，而且会被自动测速选中，必须排除。
FAKE_KEYWORDS = ("剩余", "到期", "过期", "官网", "重置", "套餐", "网址", "expire", "traffic")

def is_fake(name):
    return any(k in str(name) for k in FAKE_KEYWORDS)

def is_flat(group):
    """组里只有真实节点（不嵌策略组、不含假节点）。

    嵌了策略组的话，切过去等于没切（实际出口由内层组决定）；
    含假节点的话，自动测速可能选中它们导致出口不通。
    """
    members = group.get("proxies") or []
    group_names = {g.get("name") for g in groups}
    return all(m not in group_names and not is_fake(m) for m in members)

candidates = [g for g in groups if g.get("type") == "select" and is_flat(g)]
# 节点越多轮转效果越好：每个节点的请求密度是 interval × 节点数。
# 所以按节点数排序，优先选大组。
candidates.sort(key=lambda g: len(g.get("proxies") or []), reverse=True)

apple_group = None
# 1. 名字带 apple 的优先（只影响 Apple 流量，最干净）
for g in candidates:
    if "apple" in str(g.get("name", "")).lower():
        apple_group = g["name"]
        break
# 2. 否则取节点最多的组（不限定名字，避免选到只有几个节点的地区组）
if apple_group is None and candidates:
    best = candidates[0]
    if len(best.get("proxies") or []) >= 10:
        apple_group = best["name"]

# 顺手把订阅里所有策略组的假节点清掉。它们连不通，而且会被自动测速选中，
# 是「明明有节点却连不上」的常见原因。
removed_fake = 0
for g in groups:
    members = g.get("proxies")
    if isinstance(members, list):
        kept = [m for m in members if not is_fake(m)]
        removed_fake += len(members) - len(kept)
        g["proxies"] = kept
# proxies 段本身也去掉假节点，免得出现在组外
sub["proxies"] = [p for p in sub["proxies"] if not is_fake(p.get("name"))]
if removed_fake:
    print(f"   已清理 {removed_fake} 个机场信息假节点（剩余流量/套餐到期等）", file=sys.stderr)

if apple_group is None:
    apple_group = "AppleMonitor"
    sub.setdefault("proxy-groups", []).insert(
        0,
        {
            "name": apple_group,
            "type": "select",
            "proxies": [p for p in all_proxies if not is_fake(p)] + ["DIRECT"],
        },
    )
    print(f"   订阅里没有合适的扁平组，已新建 {apple_group}（{len(all_proxies)} 个节点 + DIRECT）", file=sys.stderr)
else:
    print(f"   使用策略组：{apple_group}", file=sys.stderr)

# 极简规则：除监控专用端口外，其余流量统一走监控组。
# 不保留订阅的精细分流，避免 rule-provider 拉取失败导致整体卡死。
sub["rules"] = [f"MATCH,{apple_group}"]

# 监控专用监听端口：7899 走 Apple 组做轮转，7898 直连兜底
sub["listeners"] = [
    {
        "name": "apple-monitor",
        "type": "mixed",
        "port": proxy_port,
        "listen": "127.0.0.1",
        "proxy": apple_group,
    },
    {
        "name": "apple-monitor-direct",
        "type": "mixed",
        "port": direct_port,
        "listen": "127.0.0.1",
        "proxy": "DIRECT",
    },
]

with open(out_path, "w", encoding="utf-8") as fh:
    yaml.safe_dump(sub, fh, allow_unicode=True, sort_keys=False, width=4096)
print(f"   配置已写入 {out_path}", file=sys.stderr)
PYEOF

# ---- 4. 建用户 + systemd ----
id -u "$MIHOMO_USER" >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin "$MIHOMO_USER"
chown -R "$MIHOMO_USER:$MIHOMO_USER" "$MIHOMO_DIR"

cat > /etc/systemd/system/mihomo.service <<EOF
[Unit]
Description=mihomo (Clash Meta) for Apple stock monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$MIHOMO_USER
ExecStart=$MIHOMO_DIR/mihomo -d $MIHOMO_DIR
Restart=on-failure
RestartSec=10
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now mihomo
sleep 3

# ---- 5. 验证 ----
echo "==> 验证"
if ! systemctl is-active --quiet mihomo; then
  echo "!! mihomo 启动失败，最近日志：" >&2
  journalctl -u mihomo -n 20 --no-pager >&2
  exit 1
fi

if curl -s --max-time 5 "http://127.0.0.1:$API_PORT/version" | grep -q version; then
  echo "   API 正常：http://127.0.0.1:$API_PORT"
else
  echo "!! API 无响应，检查 journalctl -u mihomo" >&2
  exit 1
fi

# 从生成的配置里读回策略组名（它在上面的 python 子进程里，shell 拿不到）
APPLE_GROUP=$(python3 -c "
import yaml
cfg = yaml.safe_load(open('$MIHOMO_DIR/config.yaml'))
for item in cfg.get('listeners') or []:
    if item.get('name') == 'apple-monitor':
        print(item.get('proxy', ''))
        break
" 2>/dev/null)
[[ -n "$APPLE_GROUP" ]] || APPLE_GROUP=AppleMonitor

for port in "$PROXY_PORT" "$DIRECT_PORT"; do
  if curl -s --max-time 15 -x "http://127.0.0.1:$port" -o /dev/null -w "%{http_code}" https://www.apple.com.cn/ | grep -q 200; then
    echo "   端口 $port 可用"
  else
    echo "!! 端口 $port 无法访问 Apple（可能是节点不通，先跑 app netcheck 确认）" >&2
  fi
done

cat <<EOF

完成。在 config.yaml 里填这几项：

clash_api: http://127.0.0.1:$API_PORT
clash_group: $APPLE_GROUP
proxy: http://127.0.0.1:$PROXY_PORT
fallback_proxy: http://127.0.0.1:$DIRECT_PORT
rotate_routes: true

然后验证出口：

  cd $APP_DIR && ./run.sh netcheck -c config.yaml

日常维护：

  systemctl status mihomo          # 状态
  journalctl -u mihomo -f          # 日志
  systemctl restart mihomo         # 改订阅后重启
EOF
