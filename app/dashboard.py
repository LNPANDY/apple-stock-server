"""本地 Web 面板：每轮监控结果 + 监控设置。

前端在 `app/static/` 下（index.html / app.css / app.js），不内嵌进 Python
字符串——那样既没法用编辑器的语法高亮，也没法单独格式化或加测试。

只监听 127.0.0.1 是默认值。要暴露到公网必须同时设置 `panel_token`，
否则任何人都能看到监控目标和通知密钥。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from aiohttp import web

from . import configwrite
from .config import AutoFamily, Config, ConfigError, load_config
from .monitor import Monitor
from .notify import Notifier
from .stores import StoreCatalog, StoreInfo, fetch_stores

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


def _read_static(name: str) -> str:
    """读取前端文件。启动时读一次，改动需重启进程。"""

    return (STATIC_DIR / name).read_text(encoding="utf-8")


class Dashboard:
    """面板后端：只读状态 + 设置读写。"""

    def __init__(
        self,
        monitor: Monitor,
        *,
        config_path: Any = None,
        host: str = "127.0.0.1",
        port: int = 8787,
        browser: Any = None,
        token: str = "",
    ) -> None:
        self.monitor = monitor
        self.host = host
        self.port = port
        self.browser = browser
        self.token = token
        self.config_path = config_path or monitor.config.config_path
        self._runner: web.AppRunner | None = None
        self._error = ""
        self.last_cycle_at = 0.0
        self.next_check_at = 0.0
        self.running = True
        self._catalog: StoreCatalog | None = None
        self._catalog_at = 0.0
        self._families: list[dict[str, Any]] | None = None
        self._lock = asyncio.Lock()
        self.last_round: Any = None
        self.rounds_seen = 0

    # -- 生命周期 ---------------------------------------------------------

    def record_error(self, message: str) -> None:
        self._error = message

    def record_cycle(self, *, finished_at: float, next_check_at: float) -> None:
        self.last_cycle_at = finished_at
        self.next_check_at = next_check_at
        self._error = ""

    def record_round(self, record: Any) -> None:
        """每轮结束时调用，用于在面板上即时反映最新一轮。"""

        self.last_round = record
        self.rounds_seen += 1

    @web.middleware
    async def _auth(self, request: web.Request, handler: Any) -> web.StreamResponse:
        """可选的访问令牌。

        面板没有用户体系，一旦监听公网地址，任何人都能看到监控目标和
        Bark key。设了 token 后，必须带 `?token=xxx` 或 `X-Auth-Token` 头。
        token 会写进 cookie，之后正常点页面就不用每次带参数。
        """

        if not self.token:
            return await handler(request)

        supplied = (
            request.query.get("token")
            or request.headers.get("X-Auth-Token")
            or request.cookies.get("apple_monitor_token")
        )
        if supplied != self.token:
            return web.Response(status=401, text="需要访问令牌：在地址后加 ?token=你的令牌")

        response = await handler(request)
        if request.query.get("token") == self.token and isinstance(response, web.Response):
            response.set_cookie("apple_monitor_token", self.token, max_age=30 * 86400, httponly=True)
        return response

    async def start(self) -> None:
        app = web.Application(middlewares=[self._auth])
        app.router.add_get("/", self._index)
        app.router.add_get("/static/{name}", self._static)
        app.router.add_get("/api/status", self._status)
        app.router.add_get("/api/logs", self._logs)
        app.router.add_get("/favicon.ico", self._favicon)
        app.router.add_get("/api/settings", self._get_settings)
        app.router.add_post("/api/settings", self._post_settings)
        app.router.add_get("/api/parts", self._get_parts)
        app.router.add_post("/api/notify/test", self._post_notify_test)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        log.info("状态面板：http://%s:%d", self.host, self.port)

    async def stop(self) -> None:
        self.running = False
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

    # -- 只读状态 ---------------------------------------------------------

    async def _index(self, _request: web.Request) -> web.Response:
        return web.Response(text=_read_static("index.html"), content_type="text/html", charset="utf-8")

    async def _static(self, request: web.Request) -> web.Response:
        """只允许读取 static 目录下的已知文件，不做路径拼接。"""

        name = request.match_info["name"]
        allowed = {"app.css": "text/css", "app.js": "application/javascript"}
        if name not in allowed:
            raise web.HTTPNotFound()
        return web.Response(
            text=_read_static(name), content_type=allowed[name], charset="utf-8"
        )

    async def _status(self, _request: web.Request) -> web.Response:
        return _json(self.snapshot())

    async def _favicon(self, _request: web.Request) -> web.Response:
        # 没有图标就别让浏览器一直 404，返回 204 即可。
        return web.Response(status=204)

    async def _logs(self, request: web.Request) -> web.Response:
        """每轮监控日志，新的在前。`?limit=` 控制条数。"""

        try:
            limit = min(200, max(1, int(request.query.get("limit") or 100)))
        except ValueError:
            limit = 100

        rounds = self.monitor.state.rounds[:limit]
        return _json(
            {
                "rounds": [
                    {
                        "at": r.at,
                        "node": r.node,
                        "ok": r.ok,
                        "error_kind": r.error_kind,
                        "error_detail": r.error_detail,
                        "store_count": r.store_count,
                        "part_count": r.part_count,
                        "hit_count": len(r.hits),
                        "hits": [{"store": h[0], "title": h[1], "quote": h[2]} for h in r.hits],
                    }
                    for r in rounds
                ],
                "total": len(self.monitor.state.rounds),
            }
        )

    def snapshot(self) -> dict[str, Any]:
        monitor = self.monitor
        config = monitor.config
        state = monitor.state

        rows: list[dict[str, Any]] = []
        hits: list[dict[str, Any]] = []
        for key, part_state in state.parts.items():
            store_number, _, part = key.partition("|")
            info = monitor.store_index.get(store_number)
            row = {
                "store_number": store_number,
                "store_name": info[0] if info else store_number,
                "city": info[1] if info else "",
                "part": part,
                "title": part_state.last_title or monitor.part_names.get(part, part),
                "available": part_state.available,
                "quote": part_state.last_quote,
            }
            rows.append(row)
            if part_state.available is True:
                hits.append(row)

        return {
            "running": self.running,
            "region": config.region.title,
            "scope": monitor.describe_scope(),
            "interval": config.interval,
            "jitter": config.jitter,
            "store_count": len(monitor.store_index),
            "part_count": len(monitor.part_names),
            "consecutive_blocks": state.consecutive_blocks,
            "degraded_alert_after": config.degraded_alert_after,
            "last_success_at": state.last_success_at,
            "last_cycle_at": self.last_cycle_at,
            "next_check_at": self.next_check_at,
            "quiet_active": config.quiet_hours.covers(datetime.now()),
            "error": self._error,
            "hits": hits,
            "history": [
                {
                    "at": record.at,
                    "store_count": record.store_count,
                    "part_count": record.part_count,
                    "count": len(record.details),
                    "summary": ", ".join(sorted({name for _, name in record.details})),
                }
                for record in state.history
            ],
            "rows": rows,
            "server_time": time.time(),
        }

    # -- 设置 -------------------------------------------------------------

    async def _get_settings(self, _request: web.Request) -> web.Response:
        async with self._lock:
            catalog = await self._ensure_catalog()
            families = await self._ensure_families()
        config = self.monitor.config
        return _json(
            {
                "settings": {
                    "interval": config.interval,
                    "jitter": config.jitter,
                    "provinces": [
                        catalog.province_of_location(loc) for loc in config.pickup.cities
                    ],
                    "family": config.panel_family
                    or (config.auto[0].family if config.auto else ""),
                    "color": "",
                    "capacity": "",
                    "parts": [spec.part for spec in config.products],
                    "backoff": list(config.backoff_ladder),
                    "degraded_alert_after": config.degraded_alert_after,
                    "clash_api": config.clash_api,
                    "rotate_routes": config.rotate_routes,
                    "repeat_when_in_stock": config.repeat_when_in_stock,
                    "repeat_interval": config.repeat_interval,
                    "quiet_hours": {
                        "enabled": config.quiet_hours.enabled,
                        "start": config.quiet_hours.start,
                        "end": config.quiet_hours.end,
                        "mode": config.quiet_hours.mode,
                    },
                    "notify": _notify_for_panel(config),
                },
                "current_parts": [
                    {"part": spec.part, "name": spec.display, "color": "", "capacity": ""}
                    for spec in config.products
                ],
                "stores": {
                    "provinces": catalog.provinces(),
                    "counts": _count_by_province(catalog),
                    "source": catalog.source,
                },
                "families": families,
            }
        )

    async def _post_settings(self, request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return _json({"error": "请求体不是 JSON"}, status=400)

        async with self._lock:
            catalog = await self._ensure_catalog()
            try:
                provinces = [str(p) for p in (payload.get("provinces") or [])]
                cities = catalog.resolve_locations(provinces)
                parts = payload.get("parts") or []
                if not parts:
                    return _json({"error": "至少需要一个型号"}, status=400)

                notify_raw = payload.get("notify")
                notify = None
                if notify_raw is not None:
                    notify = _notify_for_save(notify_raw, self.monitor.config.notify)

                configwrite.save_settings(
                    self.config_path,
                    interval=int(payload["interval"]),
                    jitter=float(payload["jitter"]),
                    cities=cities,
                    stores=[],
                    products=[
                        {"part": str(item["part"]), "name": str(item.get("name") or "")}
                        for item in parts
                    ],
                    # 面板选的是具体型号，因此清掉 auto：否则重启后又会被
                    # 自动发现扩回整个机型。机型只作为界面记忆另存。
                    auto=[],
                    panel_family=str(payload.get("family") or ""),
                    backoff=[int(v) for v in payload.get("backoff") or []],
                    degraded_alert_after=int(payload.get("degraded_alert_after") or 4),
                    repeat_when_in_stock=bool(payload.get("repeat_when_in_stock")),
                    repeat_interval=int(payload.get("repeat_interval") or 1800),
                    quiet_hours=payload.get("quiet_hours") or {},
                    notify=notify,
                )
                new_config = load_config(self.config_path)
            except (ConfigError, configwrite.ConfigWriteError, KeyError, TypeError, ValueError) as exc:
                log.warning("设置保存失败：%s", exc)
                return _json({"error": str(exc)}, status=400)

            restart_required = (
                new_config.interval != self.monitor.config.interval
                or new_config.jitter != self.monitor.config.jitter
            )
            self._apply_runtime(new_config)

        message = "已保存；间隔/抖动将在重启后生效" if restart_required else "已保存"
        log.info("面板更新配置：%s", message)
        return _json({"ok": True, "message": message, "restart_required": restart_required})

    def _apply_runtime(self, new_config: Config) -> None:
        """把新配置挂到运行中的 monitor 上，下一轮直接采用。

        `Monitor.config` 是冻结 dataclass 的引用，整体替换即可；正在跑的循环
        持有的是 self.config 的读取，不缓存旧值。

        通知渠道也一起换掉：改完 Bark 不用重启，下一轮提醒就用新配置。
        """

        from .notify import NotifyError, build_channels

        monitor = self.monitor
        object.__setattr__(monitor, "config", new_config)
        monitor.part_names = {spec.part: spec.display for spec in new_config.products}
        try:
            monitor.notifier = Notifier(build_channels(new_config.notify))
        except NotifyError as exc:
            log.warning("通知渠道重建失败，沿用旧配置：%s", exc)

    async def _post_notify_test(self, request: web.Request) -> web.Response:
        """发一条测试推送。

        优先用请求体里带上来的渠道配置（面板当前值），这样还没保存也能测。
        没带就用磁盘上的配置。
        """

        from .config import NotifyChannel
        from .notify import NotifyError, build_channels

        try:
            payload = await request.json() if request.can_read_body else {}
        except json.JSONDecodeError:
            payload = {}

        raw_channels = payload.get("notify") if isinstance(payload, dict) else None
        try:
            if isinstance(raw_channels, list) and raw_channels:
                # 面板传来的可能是打码值，先还原成真实密钥
                restored = _notify_for_save(raw_channels, self.monitor.config.notify)
                channels_spec = tuple(
                    NotifyChannel(type=item["type"], options={k: v for k, v in item.items() if k != "type"})
                    for item in restored
                )
            else:
                config = load_config(self.config_path)
                channels_spec = config.notify
            channels = build_channels(channels_spec)
        except (ConfigError, NotifyError, ValueError) as exc:
            return _json({"error": str(exc)}, status=400)

        from . import tz

        title = "测试推送（不代表有货）"
        body = (
            "这是一条来自 Apple 补货监控的测试消息。\n"
            f"时间：{tz.current():%Y-%m-%d %H:%M:%S}\n"
            "收到说明通知渠道配置正确。"
        )

        results: list[str] = []
        failures: list[str] = []
        for channel in channels:
            try:
                await channel.send(title, body)
                results.append(channel.describe())
            except Exception as exc:
                failures.append(f"{channel.describe()}：{exc}")

        if failures and not results:
            return _json({"error": "；".join(failures)}, status=502)
        if failures:
            return _json(
                {
                    "ok": True,
                    "message": f"部分成功（{len(results)} 个成功）：" + "；".join(failures),
                }
            )
        return _json({"ok": True, "message": f"测试推送已发送（{len(results)} 个渠道）"})

    async def _get_parts(self, request: web.Request) -> web.Response:
        family = str(request.query.get("family") or "").strip()
        if not family:
            return _json({"error": "缺少 family 参数"}, status=400)
        if self.browser is None:
            return _json({"error": "浏览器未就绪"}, status=503)

        category = "iphone"
        for item in await self._ensure_families():
            if item["slug"] == family:
                category = item["category"]
                break

        try:
            found = await self.browser.discover_products(
                AutoFamily(category=category, family=family)
            )
        except Exception as exc:
            log.warning("读取型号失败：%s", exc)
            return _json({"error": str(exc)}, status=502)

        parts = _split_names(found)
        return _json(
            {
                "parts": parts,
                "colors": _unique([p["color"] for p in parts]),
                "capacities": _unique([p["capacity"] for p in parts]),
            }
        )

    # -- 目录缓存 ---------------------------------------------------------

    async def _ensure_catalog(self) -> StoreCatalog:
        if self._catalog and time.time() - self._catalog_at < 6 * 3600:
            return self._catalog
        catalog = await fetch_stores(self.monitor.config.region)
        if not catalog.stores:
            catalog = _fallback_catalog()
        self._catalog = catalog
        self._catalog_at = time.time()
        return catalog

    async def _ensure_families(self) -> list[dict[str, Any]]:
        if self._families is not None:
            return self._families
        # 内置机型表：面板打开就能选，不依赖一次网络请求。
        self._families = [
            {"slug": slug, "label": label, "category": "iphone", "colors": [], "capacities": []}
            for slug, label in (
                ("iphone-18-pro", "iPhone 18 Pro"),
                ("iphone-18", "iPhone 18"),
                ("iphone-air", "iPhone Air"),
                ("iphone-17", "iPhone 17"),
                ("iphone-17e", "iPhone 17e"),
                ("iphone-duo", "iPhone Duo"),
            )
        ]
        return self._families


# -- 辅助 -----------------------------------------------------------------


def _json(payload: Any, *, status: int = 200) -> web.Response:
    return web.json_response(
        payload, status=status, dumps=lambda value: json.dumps(value, ensure_ascii=False)
    )


def _notify_for_panel(config: Config) -> list[dict[str, Any]]:
    """把通知渠道转成面板能编辑的结构。

    Bark 的 key 是密钥，回填时打码：面板只需要知道「配过了」，不需要显示
    完整 key。用户想换就整条重填。
    """

    out: list[dict[str, Any]] = []
    for channel in config.notify:
        item: dict[str, Any] = {"type": channel.type}
        for key, value in channel.options.items():
            if key in ("url", "token", "password") and isinstance(value, str):
                item[key] = _mask_secret(value)
                item[f"{key}_masked"] = True
            else:
                item[key] = value
        out.append(item)
    return out


def _mask_secret(value: str) -> str:
    """只保留尾部若干字符，用于回填已配置的密钥。"""

    text = value.rstrip("/")
    if "://" in text:
        head, _, tail = text.rpartition("/")
        return f"{head}/{_mask_tail(tail)}"
    return _mask_tail(text)


def _mask_tail(text: str) -> str:
    if len(text) <= 6:
        return "****"
    return f"****{text[-4:]}"


def _notify_for_save(
    raw: Any, existing: tuple[Any, ...]
) -> list[dict[str, Any]]:
    """把面板提交的通知配置转成写盘结构。

    密钥字段可能仍是打码值（用户没改）。这时必须沿用原配置里的真实密钥，
    否则一保存就把 key 写成了 `****abcd`，推送从此失效——而且用户不会
    收到任何报错，只会「突然收不到通知」。
    """

    if not isinstance(raw, list):
        raise ValueError("notify 应当是列表")

    # 按 type 建索引，用于取回被打了码的原始密钥
    originals: dict[str, dict[str, Any]] = {}
    for channel in existing:
        originals.setdefault(channel.type, dict(channel.options))

    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "").strip().lower()
        if not kind:
            continue
        options: dict[str, Any] = {}
        for key, value in item.items():
            if key in ("type", "url_masked", "token_masked", "password_masked"):
                continue
            if value is None or value == "":
                continue
            # 打码值：还原成配置里的真实值
            if isinstance(value, str) and value.startswith("****"):
                original = originals.get(kind, {}).get(key)
                if original:
                    options[key] = original
                continue
            if isinstance(value, str) and "****" in value and key in ("url", "token", "password"):
                original = originals.get(kind, {}).get(key)
                if original and value.endswith(_mask_tail(str(original).rstrip("/").rsplit("/", 1)[-1])):
                    options[key] = original
                    continue
            options[key] = value
        out.append({"type": kind, **options})
    return out


def _count_by_province(catalog: StoreCatalog) -> dict[str, int]:
    counts: dict[str, int] = {}
    for store in catalog.stores:
        counts[store.province] = counts.get(store.province, 0) + 1
    return counts


def _fallback_catalog() -> StoreCatalog:
    """内置兜底：常用城市的核心门店，保证面板在断网时也能选。"""

    raw = [
        ("R683", "环球港", "上海", "上海"),
        ("R390", "香港广场", "上海", "上海"),
        ("R359", "南京东路", "上海", "上海"),
        ("R320", "三里屯", "北京", "北京"),
        ("R388", "西单大悦城", "北京", "北京"),
        ("R448", "王府井", "北京", "北京"),
        ("R637", "天津大悦城", "天津", "天津"),
        ("R579", "天津恒隆广场", "天津", "天津"),
        ("R638", "天津万象城", "天津", "天津"),
        ("R471", "西湖", "浙江", "杭州"),
        ("R532", "杭州万象城", "浙江", "杭州"),
        ("R688", "苏州", "江苏", "苏州"),
    ]
    return StoreCatalog(
        stores=[
            StoreInfo(number=number, name=name, province=province, city=city)
            for number, name, province, city in raw
        ],
        source="内置兜底",
    )


_CAPACITY_RE = re.compile(r"(\d+)\s?(GB|TB)", re.IGNORECASE)


def _split_names(found: dict[str, str]) -> list[dict[str, str]]:
    """把「iPhone 18 Pro Max 6.9 英寸显示屏 勃艮第酒红色 1 TB」拆成结构化字段。

    展示名是购买页拼出来的，没有结构化字段可用。这里按容量形态做一次拆分，
    拆不出来就整串当名字，不影响监控本身。
    """

    out: list[dict[str, str]] = []
    for part, label in found.items():
        match = _CAPACITY_RE.search(label)
        capacity = f"{match.group(1)}{match.group(2).upper()}" if match else ""
        color = ""
        if match:
            head = label[: match.start()].strip()
            tokens = head.split()
            if tokens:
                color = tokens[-1]
        out.append({"part": part, "name": label, "color": color, "capacity": capacity})
    return out


def _unique(values: list[str]) -> list[str]:
    seen: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.append(value)
    return seen


def print_banner(host: str, port: int) -> None:
    print(f"\n  状态面板  http://{host}:{port}\n", flush=True)
