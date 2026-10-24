#!/bin/sh
# 梅林固件（Asuswrt-Merlin）反向隧道脚本
#
# 作用：让路由器把自己的网络作为出口，供 VPS 上的监控程序使用。
# 路由器只做转发，不跑 Chrome（AC86U 只有 512MB 内存，跑不动）。
#
# 部署步骤见 deploy/README-merlin.md
#
# 用法：
#   ./merlin-tunnel.sh start    启动
#   ./merlin-tunnel.sh stop     停止
#   ./merlin-tunnel.sh status   查看状态
#   ./merlin-tunnel.sh test     检查隧道是否可用
#
# 放在 /jffs/scripts/ 下，并在 services-start 里调用 start。

set -u

# ===== 需要你改的几项 =====
VPS_HOST="your.vps.ip"
VPS_PORT="22"
VPS_USER="applemon"
KEY="/jffs/.ssh/id_ed25519"
# VPS 上监听的端口。监控程序连 127.0.0.1:REMOTE_PORT
REMOTE_PORT="1080"
# 路由器本地 SOCKS5 端口。梅林自带 ss-server 或你装的 Clash 都可以。
LOCAL_SOCKS="127.0.0.1:7890"
# =========================

NAME="apple-tunnel"
PIDFILE="/var/run/${NAME}.pid"
LOGFILE="/tmp/${NAME}.log"

log() {
  echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOGFILE"
}

is_running() {
  [ -f "$PIDFILE" ] || return 1
  pid=$(cat "$PIDFILE" 2>/dev/null)
  [ -n "${pid:-}" ] || return 1
  kill -0 "$pid" 2>/dev/null
}

start() {
  if is_running; then
    echo "已在运行（pid $(cat "$PIDFILE")）"
    return 0
  fi

  if [ ! -f "$KEY" ]; then
    echo "找不到密钥：$KEY" >&2
    echo "请先按 README 生成并上传公钥到 VPS。" >&2
    return 1
  fi

  # 梅林自带的 dropbear 客户端不支持 -R 的可靠保活，优先用 entware 的 openssh-client
  SSH_BIN="$(command -v ssh || echo /usr/bin/ssh)"

  # -N 不执行远程命令；-R 反向转发；两个 ServerAlive 让断线能被及时发现
  # ExitOnForwardFailure 保证端口占用时立刻失败而不是静默跑着
  "$SSH_BIN" -N \
    -o StrictHostKeyChecking=accept-new \
    -o UserKnownHostsFile=/jffs/.ssh/known_hosts \
    -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=30 \
    -o ServerAliveCountMax=3 \
    -o TCPKeepAlive=yes \
    -o ConnectTimeout=15 \
    -i "$KEY" \
    -p "$VPS_PORT" \
    -R "127.0.0.1:${REMOTE_PORT}:${LOCAL_SOCKS}" \
    "${VPS_USER}@${VPS_HOST}" \
    >> "$LOGFILE" 2>&1 &

  pid=$!
  echo "$pid" > "$PIDFILE"
  sleep 4

  if kill -0 "$pid" 2>/dev/null; then
    log "隧道已启动 pid=$pid → ${VPS_HOST}:${REMOTE_PORT} → ${LOCAL_SOCKS}"
    echo "隧道已启动（pid $pid）"
    return 0
  fi

  log "隧道启动失败，见 $LOGFILE"
  echo "启动失败，最后几行日志：" >&2
  tail -5 "$LOGFILE" >&2
  rm -f "$PIDFILE"
  return 1
}

stop() {
  if ! is_running; then
    echo "未在运行"
    rm -f "$PIDFILE"
    return 0
  fi
  pid=$(cat "$PIDFILE")
  kill "$pid" 2>/dev/null
  sleep 2
  kill -9 "$pid" 2>/dev/null
  rm -f "$PIDFILE"
  log "隧道已停止"
  echo "已停止"
}

status() {
  if is_running; then
    echo "运行中（pid $(cat "$PIDFILE")）"
    echo "转发：VPS 127.0.0.1:${REMOTE_PORT} → 本机 ${LOCAL_SOCKS}"
    return 0
  fi
  echo "未运行"
  return 1
}

# 用本地 SOCKS5 拉一次回显服务，确认出口是本机网络
test_tunnel() {
  if ! is_running; then
    echo "隧道未运行，先执行 start" >&2
    return 1
  fi
  if command -v curl >/dev/null 2>&1; then
    echo "本机出口 IP："
    curl -s --max-time 10 -x "socks5h://${LOCAL_SOCKS}" https://ipinfo.io/json \
      | grep -E '"(ip|city|org)"' || echo "（curl 探测失败，检查 LOCAL_SOCKS 是否正确）"
  else
    echo "路由器上没有 curl，请在 VPS 上执行："
    echo "  ./run.sh netcheck -c config.yaml"
  fi
}

case "${1:-}" in
  start)  start ;;
  stop)   stop ;;
  restart) stop; sleep 1; start ;;
  status) status ;;
  test)   test_tunnel ;;
  *)
    echo "用法：$0 {start|stop|restart|status|test}" >&2
    exit 1
    ;;
esac
