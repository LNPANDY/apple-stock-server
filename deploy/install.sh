#!/usr/bin/env bash
# Ubuntu 20.04 / 22.04 / 24.04 一键安装。
#
#   sudo bash deploy/install.sh
#
# 浏览器用 Playwright 自带的 Chromium，不用 apt 的 chromium-browser：
# Ubuntu 22.04+ 里那是 snap 包装器，实测在无桌面的服务器上会卡在
# snap store 连接上 30 分钟以上。
set -euo pipefail

APP_DIR=/opt/apple-stock
APP_USER=applemon

if [[ $EUID -ne 0 ]]; then
  echo "请用 root 运行：sudo bash $0" >&2
  exit 1
fi

if ! command -v apt-get >/dev/null; then
  echo "这个脚本只支持 Debian/Ubuntu。" >&2
  exit 1
fi

echo "==> 安装系统依赖"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq

# Ubuntu 24.04 把一批库改名成 *t64（64 位 time_t 迁移），22.04 没有这些名字。
# 按发行版版本选对后缀，否则整个 apt install 因找不到包而失败。
V=$(. /etc/os-release && echo "$VERSION_ID")
case "$V" in
  24.*) S=t64 ;;
  *)    S="" ;;
esac

# 只装运行库，不装 chromium-browser（snap 包装器，见文件头注释）。
apt-get install -y -qq --no-install-recommends \
  python3 python3-venv python3-pip python3-yaml \
  curl ca-certificates fonts-liberation fonts-noto-cjk \
  libnss3 libnspr4 \
  "libatk1.0-0$S" "libatk-bridge2.0-0$S" "libcups2$S" "libasound2$S" \
  libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
  libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libcairo2

echo "==> 创建用户 $APP_USER"
id -u "$APP_USER" >/dev/null 2>&1 || \
  useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"

echo "==> 部署到 $APP_DIR"
mkdir -p "$APP_DIR"
if [[ "$(pwd)" != "$APP_DIR" ]]; then
  rsync -a --delete \
    --exclude 'venv' --exclude '.venv' --exclude 'data' --exclude '__pycache__' \
    --exclude '.pytest_cache' --exclude '.ruff_cache' --exclude 'config.yaml' \
    --exclude '.git' --exclude '.env' \
    ./ "$APP_DIR"/
fi

echo "==> 准备 Python 环境"
[[ -x "$APP_DIR/.venv/bin/python" ]] || python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

echo "==> 下载 Chromium（Playwright 自带，约 190MB）"
# Playwright 会把浏览器装到 $HOME/.cache/ms-playwright，之后由 find_browser() 找到。
# 以 APP_USER 跑，缓存落在它自己的家目录。
sudo -u "$APP_USER" -H "$APP_DIR/.venv/bin/python" -m playwright install chromium \
  || echo "!! 下载失败（可能是网络问题）。手动重试：
     sudo -u $APP_USER -H $APP_DIR/.venv/bin/python -m playwright install chromium" >&2

mkdir -p "$APP_DIR/data"
if [[ ! -f "$APP_DIR/config.yaml" ]]; then
  cp "$APP_DIR/config.example.yaml" "$APP_DIR/config.yaml"
  echo "!! 已生成 $APP_DIR/config.yaml，请修改后再启动"
fi
if [[ ! -f "$APP_DIR/.env" ]]; then
  cat > "$APP_DIR/.env" <<'EOF'
# 通知密钥放这里，权限 600。配置里用 env:NAME 引用。
# BARK_URL=https://api.day.app/你的Key
EOF
fi
chmod 600 "$APP_DIR/.env" || true
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

echo "==> 安装 systemd 服务"
cp "$APP_DIR/deploy/apple-stock.service" /etc/systemd/system/apple-stock.service
systemctl daemon-reload

cat <<EOF

安装完成。接下来：

  1. 编辑配置（region / pickup / products / notify）
       sudoedit $APP_DIR/config.yaml
       sudoedit $APP_DIR/.env          # 如果用到 Bark / Telegram 等

  2. 自检：确认能连上 Apple 并拿到库存
       cd $APP_DIR && sudo -u $APP_USER .venv/bin/python -m app check -c config.yaml

  3. 启动并设置开机自启
       systemctl enable --now apple-stock
       journalctl -u apple-stock -f

提示：被 Apple 拦截时程序会自动退避，不要用 systemctl restart 来「救」。
反复重启只会延长封禁。
EOF
