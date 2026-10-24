#!/usr/bin/env python3
"""把项目完整部署到一台 Ubuntu VPS（一条命令）。

用法：
    python3 deploy_to_vps.py <IP> [选项]

    --clash-yaml PATH     Clash 订阅配置文件（mihomo 用）。不提供则跳过 mihomo，
                          监控走直连
    --bark-url URL        Bark 推送地址。不提供则只写日志渠道
    --port N              面板端口（默认 8788）
    --interval N          查询间隔秒（默认 300）

示例：
    python3 deploy_to_vps.py 1.2.3.4 \\
        --clash-yaml ~/Downloads/airport.yaml \\
        --bark-url https://api.day.app/你的Key

SSH 认证用 ~/.ssh/vps_deploy_key（公钥需已在服务器上）。

重复部署是安全的：已存在的 config.yaml 会保留（含面板令牌），
只补全缺少的字段。
"""

from __future__ import annotations

import argparse
import secrets
import subprocess
import time
from pathlib import Path

import paramiko
import yaml

PROJECT = Path(__file__).resolve().parent
KEY = str(Path.home() / ".ssh" / "vps_deploy_key")
REMOTE_DIR = "/opt/apple-stock"
STEP_DONE = "__STEP_DONE__"


def connect(host: str) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, port=22, username="root", key_filename=KEY, timeout=30)
    return client


def run(client: paramiko.SSHClient, cmd: str, timeout: int = 120) -> str:
    _, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    stdout.channel.recv_exit_status()
    return stdout.read().decode() + stderr.read().decode()


def run_bg(client: paramiko.SSHClient, cmd: str, log: str) -> None:
    """后台执行并立即返回。长操作走这里，避免撞 SSH 超时。

    完成后往日志追加标记。不能用 pgrep 判断是否还在跑：pgrep -f 的模式
    会匹配到自己所在的命令行，永远返回「在跑」（实测卡满超时）。
    """

    wrapped = f"{cmd}; echo {STEP_DONE}"
    _, stdout, _ = client.exec_command(
        f"nohup bash -c {wrapped!r} >> {log} 2>&1 & echo $!", timeout=15
    )
    print(f"    后台 pid={stdout.read().decode().strip()}", flush=True)


def wait_done(client: paramiko.SSHClient, log: str, timeout: int) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        out = run(client, f"grep -c {STEP_DONE} {log} 2>/dev/null || true", 15)
        if out.strip() not in ("", "0"):
            return True
        time.sleep(10)
    return False


def build_config(args, existing: dict | None) -> dict:
    """生成监控配置。已存在的配置只补缺，不覆盖——重复部署时保住
    用户改过的字段和面板令牌。"""

    config: dict = {
        "region": "cn",
        "interval": args.interval,
        "jitter": 0.3,
        "timezone": "Asia/Shanghai",
        "pickup": {"cities": ["北京 北京"], "stores": []},
        "products": [{"part": "MK2M4CH/A", "name": "iPhone Duo 星光白 256GB"}],
        "notify": [{"type": "console"}],
        "state_file": "data/state.json",
        "log_level": "INFO",
    }
    if args.bark_url:
        config["notify"].append(
            {"type": "bark", "url": args.bark_url, "group": "Apple", "level": "timeSensitive"}
        )
    if args.clash_yaml:
        config.update(
            {
                "clash_api": "http://127.0.0.1:9090",
                "clash_group": "AppleMonitor",
                "proxy": "http://127.0.0.1:7899",
                "fallback_proxy": "http://127.0.0.1:7898",
                "rotate_routes": True,
            }
        )

    if existing:
        # 已有配置优先；只补上缺失的键
        merged = dict(existing)
        for key, value in config.items():
            merged.setdefault(key, value)
        return merged
    return config


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("host", help="VPS IP")
    parser.add_argument("--clash-yaml", default=None, help="Clash 订阅配置（本地路径）")
    parser.add_argument("--bark-url", default=None, help="Bark 推送地址")
    parser.add_argument("--port", type=int, default=8788, help="面板端口")
    parser.add_argument("--interval", type=int, default=300, help="查询间隔秒")
    args = parser.parse_args()

    print(f"==> 连接 {args.host}", flush=True)
    client = connect(args.host)

    print("==> 上传项目", flush=True)
    tar = Path("/tmp/apple-stock-deploy.tar.gz")
    subprocess.run(
        ["tar", "czf", str(tar),
         "--exclude=.venv", "--exclude=venv", "--exclude=data", "--exclude=__pycache__",
         "--exclude=.pytest_cache", "--exclude=.ruff_cache", "--exclude=config.yaml",
         "--exclude=.git", "--exclude=.env",
         "-C", str(PROJECT), "."],
        check=True,
    )
    sftp = client.open_sftp()
    sftp.put(str(tar), "/tmp/app.tar.gz")
    if args.clash_yaml:
        sftp.put(args.clash_yaml, "/tmp/clash.yaml")
        print(f"    订阅配置已上传（{Path(args.clash_yaml).name}）", flush=True)
    sftp.close()

    # 已有配置先拉下来（保留面板令牌）
    existing: dict | None = None
    try:
        with client.open_sftp().open(f"{REMOTE_DIR}/config.yaml") as fh:
            existing = yaml.safe_load(fh)
        if not isinstance(existing, dict):
            existing = None
    except FileNotFoundError:
        pass
    except OSError:
        pass

    print("==> 系统依赖 + 程序", flush=True)
    log = "/tmp/step_install.log"
    run_bg(
        client,
        f"mkdir -p {REMOTE_DIR} && cd {REMOTE_DIR} && tar xzf /tmp/app.tar.gz "
        f"&& bash deploy/install.sh",
        log,
    )
    if not wait_done(client, log, 1200):
        print(f"    !! 安装超时，查看 {log}", flush=True)
        return 1
    # install.sh 失败时日志末尾不会有「安装完成」
    if "安装完成" not in run(client, f"tail -20 {log}"):
        print(f"    !! install.sh 失败：\n{run(client, f'tail -30 {log}')}", flush=True)
        return 1
    print("    完成", flush=True)

    if args.clash_yaml:
        print("==> mihomo", flush=True)
        log = "/tmp/step_mihomo.log"
        run_bg(client, f"cd {REMOTE_DIR} && bash deploy/setup-mihomo.sh /tmp/clash.yaml", log)
        if not wait_done(client, log, 900):
            print(f"    !! mihomo 超时，查看 {log}", flush=True)
            return 1
        if "日常维护" not in run(client, f"tail -5 {log}"):
            print(f"    !! setup-mihomo.sh 失败：\n{run(client, f'tail -20 {log}')}", flush=True)
            return 1
        print("    完成", flush=True)

    print("==> 写配置", flush=True)
    config = build_config(args, existing)
    token = str((existing or {}).get("panel_token") or secrets.token_urlsafe(16))
    config["panel_token"] = token
    sftp = client.open_sftp()
    with sftp.open(f"{REMOTE_DIR}/config.yaml", "w") as fh:
        yaml.safe_dump(config, fh, allow_unicode=True, sort_keys=False, width=4096)
    sftp.close()
    run(client, f"chown -R applemon:applemon {REMOTE_DIR}")

    print("==> systemd 服务", flush=True)
    run(
        client,
        f"""cat > /etc/systemd/system/apple-stock.service <<'EOF'
[Unit]
Description=Apple stock monitor
After=network-online.target mihomo.service
Wants=network-online.target

[Service]
Type=simple
User=applemon
WorkingDirectory={REMOTE_DIR}
EnvironmentFile=-{REMOTE_DIR}/.env
ExecStart={REMOTE_DIR}/.venv/bin/python -m app run -c {REMOTE_DIR}/config.yaml --web --host 0.0.0.0 --port {args.port}
Restart=on-failure
RestartSec=30
Environment=PYTHONUNBUFFERED=1
# 机器上可能还有其他服务。给本程序设上限，就算浏览器内存膨胀
# 也只影响自己，不会挤压它们；CPU 竞争时让现有服务优先。
MemoryMax=900M
MemoryHigh=700M
CPUWeight=50
Nice=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload && systemctl enable --now apple-stock
# mihomo 同样约束（轻量，纯保险）
mkdir -p /etc/systemd/system/mihomo.service.d
cat > /etc/systemd/system/mihomo.service.d/limits.conf <<'EOF'
[Service]
MemoryMax=200M
CPUWeight=50
Nice=5
EOF
systemctl daemon-reload && systemctl restart mihomo""",
        timeout=60,
    )

    # 防火墙：开着才放行（RackNerd 等默认开，DigitalOcean 等默认关）
    if "Status: active" in run(client, "ufw status 2>/dev/null"):
        run(client, f"ufw allow {args.port}/tcp")
        print(f"    已放行防火墙端口 {args.port}", flush=True)

    print("==> 验证", flush=True)
    time.sleep(25)
    states = run(client, "systemctl is-active apple-stock mihomo 2>/dev/null").split()
    print(f"    apple-stock: {states[0] if states else '?'}"
          + (f"  mihomo: {states[1]}" if len(states) > 1 else ""))
    tail = run(client, "journalctl -u apple-stock -n 5 --no-pager 2>/dev/null | grep -E '本轮完成|出口轮换' | tail -2")
    print(f"    {tail.strip()}")

    uninstall = f"{REMOTE_DIR}/deploy/uninstall.sh"
    print(
        f"""
================ 部署完成 ================

面板（浏览器打开）：
  http://{args.host}:{args.port}/?token={token}

不带 token 返回 401——面板没有用户体系，令牌是唯一屏障。
第一次打开后 token 存在 cookie，之后直接访问根地址。

服务管理：
  systemctl status apple-stock mihomo
  journalctl -u apple-stock -f

卸载（删除本程序的全部内容，不影响机器上其他服务）：
  bash {uninstall}          # 服务 + 程序 + 防火墙规则
  bash {uninstall} --full   # 再清 Chromium 缓存和 apt 库
==========================================""",
        flush=True,
    )
    client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
