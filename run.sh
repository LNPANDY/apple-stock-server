#!/usr/bin/env bash
# 用项目自己的虚拟环境启动，避免系统里多个 python3 互相干扰。
#
#   ./run.sh run -c config.yaml --web
#   ./run.sh check -c config.yaml
#
# 首次运行会自动建 venv 并装依赖。
set -euo pipefail

cd "$(dirname "$0")"

VENV=".venv"
STAMP="$VENV/.deps-ok"

# 挑一个可用的 Python：3.10+ 即可（代码用到了 X | Y 的类型写法）。
pick_python() {
  for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
        echo "$candidate"
        return 0
      fi
    fi
  done
  return 1
}

if [[ ! -x "$VENV/bin/python" ]]; then
  PY="$(pick_python)" || {
    echo "找不到 Python 3.10+。请先安装：brew install python@3.12" >&2
    exit 1
  }
  # 变量名紧贴中文时，shell 会把中文首字节当成变量名的一部分，必须用 ${}。
  echo "创建虚拟环境（使用 ${PY}）…"
  "$PY" -m venv "$VENV"
fi

# 依赖只在 requirements.txt 变化时重装，避免每次启动都跑 pip。
if [[ ! -f "$STAMP" || requirements.txt -nt "$STAMP" ]]; then
  echo "安装依赖…"
  "$VENV/bin/python" -m pip install --quiet --upgrade pip
  "$VENV/bin/python" -m pip install --quiet -r requirements.txt
  touch "$STAMP"
fi

exec "$VENV/bin/python" -m app "$@"
