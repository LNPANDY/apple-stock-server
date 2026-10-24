"""命令行入口。

用法：
    python -m app run  -c config.yaml     # 前台运行（调试用）
    python -m app check -c config.yaml    # 只跑一轮并打印结果，不发通知
    python -m app parts -c config.yaml    # 列出解析到的零件号，便于核对
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import shutil
import signal
import sys
from pathlib import Path
from typing import Any

# 依赖缺失时给出可操作的提示，而不是抛一段 traceback。这在多 Python 版本的
# 机器上很常见：`python3` 指向的版本未必装了这个项目的依赖。
_MISSING_HINT = """
缺少依赖：{missing}

如果你用系统里的 python3，可能是它指向的版本没装依赖（机器上装了多个
Python 时很容易踩到）。最省事的做法是用项目自带的启动脚本，它会建一个
独立的虚拟环境：

    ./run.sh {command}

或者手动装到当前解释器：

    {python} -m pip install -r requirements.txt
""".strip()

try:
    from .browser import AppleBrowser, FetchError, find_browser
    from .config import Config, ConfigError, load_config
    from .dashboard import Dashboard, print_banner
    from .monitor import Monitor, _chunk
    from .notify import Notifier, NotifyError, build_channels
except ModuleNotFoundError as exc:  # pragma: no cover - 只在环境缺依赖时触发
    _missing = exc.name or "未知模块"
    print(
        _MISSING_HINT.format(
            missing=_missing,
            command="run -c config.yaml --web",
            python=sys.executable,
        ),
        file=sys.stderr,
    )
    raise SystemExit(3) from exc

log = logging.getLogger("apple-stock")

PROFILE_DIR = Path("data/browser-profile")


def apply_timezone(config: Any) -> None:
    """把配置里的时区设为全局默认。必须在监控启动前调用。"""

    from . import tz

    tz.configure(config.timezone)


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def _load(path: str) -> Config:
    try:
        return load_config(path)
    except ConfigError as exc:
        log.error("配置错误：%s", exc)
        raise SystemExit(2) from exc


def cmd_run(args: argparse.Namespace) -> int:
    config = _load(args.config)
    setup_logging(config.log_level)
    apply_timezone(config)

    if not find_browser():
        log.error(
            "找不到 Chrome/Chromium。请在 Ubuntu 上执行：\n"
            "  sudo apt install -y chromium-browser\n"
            "并确认 `chromium-browser --version` 可用。"
        )
        return 3

    try:
        notifier = Notifier(build_channels(config.notify))
    except NotifyError as exc:
        log.error("通知配置错误：%s", exc)
        return 2

    async def main() -> int:
        # 出口轮换模式：浏览器固定走 mihomo 的代理端口，节点切换通过其 API 完成。
        route_manager = None
        if config.clash_api:
            from .routes import ClashClient, RouteManager, RoutePool

            client = ClashClient(config.clash_api, config.clash_secret, config.clash_group)
            if await client.ping():
                group = config.clash_group or await _detect_group(client)
                if group:
                    client.group = group
                    route_manager = RouteManager(
                        client,
                        RoutePool(
                            rotate=config.rotate_routes,
                            use_direct=config.use_direct_route,
                        ),
                    )
                    log.info("出口轮换已启用：api=%s 策略组=%s", config.clash_api, group)
                    if not (args.proxy or config.proxy):
                        log.warning(
                            "已启用出口轮换但未配置 proxy。浏览器应当指向 mihomo 的代理端口"
                            "（例如 proxy: http://127.0.0.1:7890），否则切换节点不会生效。"
                        )
                else:
                    log.warning("Clash API 可达但没有找到可用的策略组，退回单出口模式")
            else:
                log.warning("Clash API（%s）不可达，退回单出口模式", config.clash_api)

        browser = AppleBrowser(
            config.region,
            PROFILE_DIR,
            headless=not args.headful,
            no_sandbox=args.no_sandbox or _running_as_root(),
            proxy=args.proxy or config.proxy or None,
            proxy_bypass=config.proxy_bypass or None,
        )
        try:
            await browser.start()
        except FetchError as exc:
            log.error("浏览器启动失败：%s", exc)
            return 3

        monitor = Monitor(config, browser, notifier, route_manager=route_manager)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                # Windows 的事件循环不支持信号处理
                loop.add_signal_handler(sig, monitor.request_stop)

        dashboard = None
        if args.web:
            dashboard = Dashboard(
                monitor,
                config_path=Path(args.config),
                host=args.host,
                port=args.port,
                browser=browser,
                token=args.token or config.panel_token or "",
            )
            monitor.dashboard = dashboard
            try:
                await dashboard.start()
                print_banner(args.host, args.port)
            except OSError as exc:
                log.error("状态面板启动失败（端口 %d 可能被占用）：%s", args.port, exc)
                return 5
        try:
            await monitor.run()
        finally:
            if dashboard is not None:
                await dashboard.stop()
            await browser.close()
        return 0

    return asyncio.run(main())


def cmd_check(args: argparse.Namespace) -> int:
    """跑一轮并打印明细，不发任何通知。用于部署后自检。"""

    config = _load(args.config)
    setup_logging("DEBUG" if args.verbose else "INFO")
    print(f"地区：{config.region.title}（{config.region.base_url}）")
    print(f"范围：城市={config.pickup.cities or '无'} 门店={config.pickup.stores or '无'}")
    print(f"通知渠道：{Notifier(build_channels(config.notify)).describe()}")
    print()

    async def main() -> int:
        browser = AppleBrowser(
            config.region,
            PROFILE_DIR,
            headless=not args.headful,
            no_sandbox=args.no_sandbox or _running_as_root(),
            proxy=args.proxy or config.proxy or None,
            proxy_bypass=config.proxy_bypass or None,
        )
        try:
            await browser.start()
            monitor = Monitor(config, browser, notifier=Notifier([]))
            parts = await monitor._resolve_parts()
            print(f"解析到 {len(parts)} 个零件号")
            if args.list_parts:
                for part, name in parts.items():
                    print(f"  {part}  {name}")
                return 0

            batches = _chunk(list(parts), 20)
            total_available = 0
            for kind, value in monitor._build_scopes():
                print(f"\n=== {kind}={value} ===")
                for index, batch in enumerate(batches):
                    if index:
                        await asyncio.sleep(5)
                    try:
                        stores = await browser.query(parts=batch, **{kind: value})
                    except FetchError as exc:
                        print(f"  查询失败：{exc}")
                        if exc.blocked or exc.rate_limited:
                            return 4
                        continue
                    for store in stores:
                        hits = [
                            (part, status)
                            for part, status in store.parts.items()
                            if status.available and part in parts
                        ]
                        total_available += len(hits)
                        marker = "有货" if hits else "无货"
                        print(f"  {store.store_number} {store.store_name}（{store.city}）{marker}")
                        for part, status in hits:
                            print(f"      ✔ {status.title or parts[part]}  {part}  {status.quote}")
            print(f"\n共检测到 {total_available} 个「有货」组合")
            return 0
        except FetchError as exc:
            log.error("查询失败：%s", exc)
            return 4
        finally:
            await browser.close()

    return asyncio.run(main())


def cmd_parts(args: argparse.Namespace) -> int:
    config = _load(args.config)
    setup_logging(config.log_level)

    async def main() -> int:
        browser = AppleBrowser(
            config.region,
            PROFILE_DIR,
            headless=not args.headful,
            no_sandbox=args.no_sandbox or _running_as_root(),
            proxy=args.proxy or config.proxy or None,
            proxy_bypass=config.proxy_bypass or None,
        )
        try:
            await browser.start()
            monitor = Monitor(config, browser, notifier=Notifier([]))
            parts = await monitor._resolve_parts()
            for part, name in parts.items():
                print(f"{part}\t{name}")
            print(f"\n共 {len(parts)} 个零件号", file=sys.stderr)
            return 0
        except FetchError as exc:
            log.error("%s", exc)
            return 4
        finally:
            await browser.close()

    return asyncio.run(main())


def _running_as_root() -> bool:
    try:
        return os_geteuid() == 0
    except AttributeError:
        return False


async def _detect_group(client: Any) -> str:
    """没在配置里写策略组名时，自动挑一个像「手动选择」的组。"""

    try:
        import aiohttp

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=4), headers=client._headers()
        ) as session, session.get(f"{client.base}/proxies") as resp:
            data = await resp.json()
    except Exception:
        return ""
    proxies = data.get("proxies") or {}
    # 优先级：GLOBAL 之后，找名字里带「选择/Select/手动」的组；都没有就用第一个 Selector
    candidates = [
        name
        for name, info in proxies.items()
        if str(info.get("type", "")) == "Selector"
        and info.get("all")
        and name != "GLOBAL"
    ]
    for keyword in ("选择", "Select", "select", "手动", "手动切换"):
        for name in candidates:
            if keyword in name:
                return name
    return candidates[0] if candidates else ""


def os_geteuid() -> int:
    import os

    return os.geteuid()


def cmd_netcheck(args: argparse.Namespace) -> int:
    """检查出口 IP 与 DNS 行为——专门用来验证「VPS 经代理走家宽」是否成立。

    看两件事：
    1. 出口 IP 是不是你家宽的公网 IP（不是 VPS 的）；
    2. DNS 是在哪端解析的。走 SOCKS5 时 Chrome 会把域名交给代理解析，
       所以 DNS 也发生在代理端，Apple 看到的来源是自洽的。
    """

    config = _load(args.config)
    setup_logging(config.log_level)
    proxy = args.proxy or config.proxy or None

    async def main() -> int:
        browser = AppleBrowser(
            config.region,
            PROFILE_DIR,
            headless=not args.headful,
            no_sandbox=args.no_sandbox or _running_as_root(),
            proxy=proxy,
            proxy_bypass=config.proxy_bypass or None,
        )
        try:
            await browser.start()
        except FetchError as exc:
            log.error("浏览器启动失败：%s", exc)
            return 3

        print(f"代理：{proxy or '未使用（直连）'}")
        print()
        try:
            egress = await browser.probe_egress()
        except FetchError as exc:
            log.error("出口探测失败：%s", exc)
            return 4
        finally:
            await browser.close()

        print("出口 IP：", egress.get("ip") or "未知")
        for key, label in (("country", "国家"), ("region", "地区"), ("city", "城市"),
                           ("org", "运营商")):
            value = egress.get(key)
            if value:
                print(f"{label}：{value}")
        print()
        print("DNS：Chrome 走 SOCKS5/HTTP 代理时把域名交给代理解析（实测为 ATYP=3），")
        print("     因此 DNS 查询也发生在代理端，不会从本机泄漏。")
        if not proxy:
            print()
            print("提示：当前未配置代理，查询直接从本机出口发出。")
        return 0

    return asyncio.run(main())


def cmd_doctor(args: argparse.Namespace) -> int:
    """不连 Apple，只检查本机环境是否具备运行条件。"""

    okay = True
    print(f"Python：{sys.version.split()[0]}")

    found = find_browser()
    if found:
        print(f"浏览器：{found[0]}（{found[1]}）")
    else:
        print("浏览器：未找到 ✘  请安装 chromium 或 google-chrome")
        okay = False

    for module in ("playwright", "yaml", "aiohttp"):
        try:
            __import__(module)
            print(f"依赖 {module}：OK")
        except ImportError:
            print(f"依赖 {module}：缺失 ✘  请执行 pip install -r requirements.txt")
            okay = False

    if shutil.which("systemctl"):
        print("systemd：可用")
    else:
        print("systemd：不可用（容器环境可忽略）")

    if Path("config.yaml").is_file():
        try:
            config = load_config("config.yaml")
            print(f"配置文件：OK（{config.region.title}）")
        except ConfigError as exc:
            print(f"配置文件：有问题 ✘  {exc}")
            okay = False
    else:
        print("配置文件：未找到 config.yaml（复制 config.example.yaml 后修改）")

    return 0 if okay else 1


def build_parser() -> argparse.ArgumentParser:
    # 全局参数同时挂到每个子命令上，这样 `-c` 放在子命令前后都成立。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", default="config.yaml", help="配置文件路径")
    common.add_argument("--headful", action="store_true", help="显示浏览器窗口（仅调试用）")
    common.add_argument("--no-sandbox", action="store_true", help="容器/root 环境需要")
    common.add_argument("--web", action="store_true", help="同时启动本地状态面板")
    common.add_argument("--host", default="127.0.0.1", help="面板监听地址（默认仅本机）")
    common.add_argument("--port", type=int, default=8787, help="面板端口，默认 8787")
    common.add_argument(
        "--token", default=None,
        help="面板访问令牌。监听公网地址时强烈建议设置，否则任何人都能打开",
    )
    common.add_argument(
        "--proxy", default=None, help="给查询浏览器指定代理，如 http://127.0.0.1:7890"
    )

    parser = argparse.ArgumentParser(
        prog="apple-stock",
        description="Apple 直营店取货补货监控（仅提醒，不下单）",
        parents=[common],
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="持续监控（生产环境用这个）", parents=[common])
    run.set_defaults(func=cmd_run)

    check = sub.add_parser("check", help="只跑一轮并打印明细，不发通知", parents=[common])
    check.add_argument("--list-parts", action="store_true", help="只列出解析到的零件号")
    check.add_argument("-v", "--verbose", action="store_true")
    check.set_defaults(func=cmd_check)

    parts = sub.add_parser("parts", help="列出自动发现的零件号", parents=[common])
    parts.set_defaults(func=cmd_parts)

    doctor = sub.add_parser("doctor", help="检查本机环境", parents=[common])
    doctor.set_defaults(func=cmd_doctor)

    net = sub.add_parser(
        "netcheck", help="检查出口 IP 与代理是否生效（不消耗 Apple 额度）", parents=[common]
    )
    net.set_defaults(func=cmd_netcheck)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
