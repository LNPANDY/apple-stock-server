"""监控主循环。

设计上有三条硬约束，都来自原项目踩过的坑：

1. **失败绝不当成无货。** 一次请求失败只更新「未知」，不改动库存结论。
   把未知降级成无货，用户就会在真的补货时收不到提醒。
2. **只在状态变化时提醒。** 每轮都推一次「无货」会让提醒失去意义。
3. **被拦之后要退避，而不是按原间隔重试。** 541/403 之后继续按 5 分钟一次
   敲门，只会让封禁时间不断延长。这里的退避是跨轮持续的，并且有上限。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import tz
from .browser import MAX_PARTS_PER_REQUEST, AppleBrowser, FetchError, StoreAvailability
from .config import Config
from .notify import Notifier

log = logging.getLogger(__name__)

# 退避阶梯（秒）。被拦后按这个节奏重新试探，而不是继续按 interval 敲门。
BACKOFF_LADDER = (600, 1200, 2400, 3600, 7200)
# 单轮内多批次之间的间隔，避免一次性形成突发。
BATCH_GAP_S = 5.0
# 同一零件号在同一门店的有货提醒冷却：库存抖动时不至于反复刷屏。
SAME_STOCK_COOLDOWN_S = 900
# 补货历史保留条数。只存「有补货」的扫描，10 条足够看出最近的补货节奏。
HISTORY_LIMIT = 10
# 日志面板保留的轮次数。10 秒一轮时 200 条约 33 分钟，够看出趋势。
ROUND_LOG_LIMIT = 200
# 单批次被拦后最多换几次节点重试。2 次足够——连续被拒通常说明
# 被拦的不只是当前 IP，继续换只会拖长本轮。
MAX_SWITCH_RETRY = 2


@dataclass
class PartState:
    available: bool | None = None  # None = 未知（查询失败）
    last_changed_at: float = 0.0
    last_alert_at: float = 0.0
    last_title: str = ""
    last_quote: str = ""

    def to_json(self) -> dict:
        return {
            "available": self.available,
            "last_changed_at": self.last_changed_at,
            "last_alert_at": self.last_alert_at,
            "last_title": self.last_title,
            "last_quote": self.last_quote,
        }

    @classmethod
    def from_json(cls, raw: dict) -> PartState:
        return cls(
            available=raw.get("available"),
            last_changed_at=float(raw.get("last_changed_at") or 0),
            last_alert_at=float(raw.get("last_alert_at") or 0),
            last_title=str(raw.get("last_title") or ""),
            last_quote=str(raw.get("last_quote") or ""),
        )


@dataclass(frozen=True)
class RoundLog:
    """一轮扫描的完整记录，供面板日志页展示。

    与 `ScanRecord` 的分工：ScanRecord 只留「有补货」的轮次作为补货历史；
    RoundLog 记录**每一轮**（含无货、失败），用于排查「为什么没查到」。
    """

    at: float
    node: str = ""  # 本轮使用的出口节点
    ok: bool = False
    # 失败类型：blocked(541/403) / rate_limited(429) / node_error(节点不通) /
    #          other(其他错误) / ""
    error_kind: str = ""
    error_detail: str = ""
    store_count: int = 0
    part_count: int = 0
    hits: tuple[tuple[str, str, str], ...] = ()  # (门店, 型号, 取货文案)
    elapsed_ms: int = 0

    def to_json(self) -> dict:
        return {
            "at": self.at,
            "node": self.node,
            "ok": self.ok,
            "error_kind": self.error_kind,
            "error_detail": self.error_detail,
            "store_count": self.store_count,
            "part_count": self.part_count,
            "hits": [list(h) for h in self.hits],
            "elapsed_ms": self.elapsed_ms,
        }

    @classmethod
    def from_json(cls, raw: dict) -> RoundLog | None:
        if not isinstance(raw, dict):
            return None
        try:
            at = float(raw.get("at") or 0)
        except (TypeError, ValueError):
            return None
        if at <= 0:
            return None
        hits_raw = raw.get("hits") or []
        return cls(
            at=at,
            node=str(raw.get("node") or ""),
            ok=bool(raw.get("ok")),
            error_kind=str(raw.get("error_kind") or ""),
            error_detail=str(raw.get("error_detail") or ""),
            store_count=int(raw.get("store_count") or 0),
            part_count=int(raw.get("part_count") or 0),
            hits=tuple(
                (str(h[0]), str(h[1]), str(h[2]) if len(h) > 2 else "")
                for h in hits_raw
                if isinstance(h, (list, tuple)) and len(h) >= 2
            ),
            elapsed_ms=int(raw.get("elapsed_ms") or 0),
        )


@dataclass(frozen=True)
class ScanRecord:
    """一次扫描的补货概况。

    只记录「有补货」的扫描——无货的轮次没有任何信息量，记下来只会让历史
    变成一串「0 台」，真正想回看的补货记录反而被淹没。
    """

    at: float
    store_count: int
    part_count: int
    details: tuple[tuple[str, str], ...] = ()  # (门店名, 型号名)

    def to_json(self) -> dict:
        return {
            "at": self.at,
            "store_count": self.store_count,
            "part_count": self.part_count,
            "details": [list(item) for item in self.details],
        }

    @classmethod
    def from_json(cls, raw: dict) -> ScanRecord | None:
        """从落盘数据恢复一条记录；坏数据返回 None 由调用方丢弃。

        状态文件可能被手动改坏。历史只是回看用的，不是库存判定的一部分，
        丢掉一条比让整个监控起不来更合理。
        """

        if not isinstance(raw, dict):
            return None
        details = raw.get("details") or []
        try:
            at = float(raw.get("at") or 0)
            store_count = int(raw.get("store_count") or 0)
            part_count = int(raw.get("part_count") or 0)
        except (TypeError, ValueError):
            return None
        if at <= 0:
            return None
        return cls(
            at=at,
            store_count=store_count,
            part_count=part_count,
            details=tuple(
                (str(item[0]), str(item[1]))
                for item in details
                if isinstance(item, (list, tuple)) and len(item) >= 2
            ),
        )


@dataclass
class MonitorState:
    """落盘的状态。键是 `store|part`。"""

    parts: dict[str, PartState] = field(default_factory=dict)
    consecutive_blocks: int = 0
    last_success_at: float = 0.0
    last_alert_summary: str = ""
    degraded_alerted_at: float = 0.0
    # 最近若干次「有补货」的扫描，新的在前
    history: list[ScanRecord] = field(default_factory=list)
    # 每一轮的完整记录（含无货/失败），新的在前，供日志面板
    rounds: list[RoundLog] = field(default_factory=list)
    # 上一轮检测到的有货组合（门店, 型号），用于判断补货是否连续
    last_hits: set = field(default_factory=set)

    def key(self, store: str, part: str) -> str:
        return f"{store}|{part}"

    def get(self, store: str, part: str) -> PartState:
        key = self.key(store, part)
        state = self.parts.get(key)
        if state is None:
            state = PartState()
            self.parts[key] = state
        return state

    def load(self, path: Path) -> None:
        if not path.is_file():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("状态文件无法读取，将重新开始：%s", exc)
            return
        self.parts = {
            key: PartState.from_json(value)
            for key, value in (raw.get("parts") or {}).items()
            if isinstance(value, dict)
        }
        self.consecutive_blocks = int(raw.get("consecutive_blocks") or 0)
        self.last_success_at = float(raw.get("last_success_at") or 0)
        self.degraded_alerted_at = float(raw.get("degraded_alerted_at") or 0)
        self.history = [
            record
            for item in (raw.get("history") or [])
            if (record := ScanRecord.from_json(item)) is not None
        ][:HISTORY_LIMIT]
        self.rounds = [
            record
            for item in (raw.get("rounds") or [])
            if (record := RoundLog.from_json(item)) is not None
        ][:ROUND_LOG_LIMIT]
        self.last_hits = {
            (str(item[0]), str(item[1]))
            for item in (raw.get("last_hits") or [])
            if isinstance(item, (list, tuple)) and len(item) >= 2
        }
        log.info(
            "已载入上次状态：%d 个门店/型号组合，%d 条补货历史",
            len(self.parts),
            len(self.history),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "parts": {key: value.to_json() for key, value in self.parts.items()},
            "consecutive_blocks": self.consecutive_blocks,
            "last_success_at": self.last_success_at,
            "degraded_alerted_at": self.degraded_alerted_at,
            "history": [record.to_json() for record in self.history],
            "rounds": [record.to_json() for record in self.rounds],
            "last_hits": [list(h) for h in self.last_hits],
            "saved_at": time.time(),
        }
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def record_round(self, record: RoundLog) -> None:
        """追加一轮日志，只保留最近若干条。"""

        self.rounds.insert(0, record)
        del self.rounds[ROUND_LOG_LIMIT:]

    def record_scan(self, record: ScanRecord, *, continuous: bool) -> None:
        """追加一条补货记录，只保留最近若干条。

        `continuous` 表示这批货在上一轮扫描里也存在（即中间没有断过）。

        去重只在「持续有货」时生效。最初的实现只看「和上一条内容是否相同」，
        结果 18:25 和 19:10 两次独立的补货（同门店、同型号、中间断过货）被
        当成同一条，第二次直接消失。真正的补货事件必须每次都在历史里出现。
        """

        if continuous and self.history:
            last = self.history[0]
            if (
                last.store_count == record.store_count
                and last.part_count == record.part_count
                and set(last.details) == set(record.details)
            ):
                return
        self.history.insert(0, record)
        del self.history[HISTORY_LIMIT:]


@dataclass(frozen=True)
class Alert:
    store_number: str
    store_name: str
    city: str
    part: str
    title: str
    quote: str


class Monitor:
    def __init__(
        self,
        config: Config,
        browser: AppleBrowser,
        notifier: Notifier,
        *,
        route_manager: Any = None,
    ) -> None:
        self.config = config
        self.browser = browser
        self.notifier = notifier
        self.state = MonitorState()
        self.state_file = config.state_file
        self._stop = asyncio.Event()
        # 面板需要的只读视图：门店号 → (店名, 城市)，以及零件号 → 显示名。
        self.store_index: dict[str, tuple[str, str]] = {}
        self.part_names: dict[str, str] = {}
        self.dashboard: Any = None
        self._pending_alerts: list[tuple[str, str]] = []
        # 出口轮换（Clash/mihomo 模式）。None = 单出口直连。
        self.routes = route_manager
        # 全部节点进冷却时记录的最早恢复时间，供 _next_delay 采用。
        self._route_recovery_wait: float = 0.0

    # -- 生命周期 ---------------------------------------------------------

    def request_stop(self) -> None:
        self._stop.set()

    def describe_scope(self) -> str:
        return self._describe_scope()

    async def run(self) -> None:
        self.state.load(self.state_file)
        log.info(
            "开始监控 %s｜间隔 %ds（±%d%%）｜范围：%s",
            self.config.region.title,
            self.config.interval,
            int(self.config.jitter * 100),
            self._describe_scope(),
        )
        log.info("通知渠道：%s", self.notifier.describe())

        while not self._stop.is_set():
            blocked = False
            error = ""
            await self._flush_pending()
            try:
                blocked = await self._run_cycle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("本轮查询出现未预期错误")
                error = str(exc)

            try:
                self.state.save(self.state_file)
            except OSError as exc:
                log.error("状态文件写入失败：%s", exc)

            delay = self._next_delay(blocked)
            log.info("下一轮将在 %s 后开始（%s）", _fmt_duration(delay), _clock(delay))
            if self.dashboard is not None:
                self.dashboard.record_cycle(
                    finished_at=time.time(), next_check_at=time.time() + delay
                )
                if error:
                    self.dashboard.record_error(error)
            # 等待下一轮；stop 事件被设置时会提前返回。
            # Python 3.11+ 里 asyncio.TimeoutError 就是内置 TimeoutError，
            # 但 3.10（Ubuntu 22.04 自带）两者是不同的类——只写一个就会
            # 漏接，实测表现为服务每个轮询周期崩溃一次。
            with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
        log.info("监控已停止")

    async def _run_cycle(self) -> bool:
        """跑一轮，返回本轮是否被 Apple 拒绝。"""

        if self.routes:
            self.routes.begin_cycle()

        part_names = await self._resolve_parts()
        if not part_names:
            log.error("没有解析到任何零件号，本轮跳过")
            return False
        self.part_names = part_names

        # 轮转模式：每轮换一个出口。20 个节点 × 10 秒间隔时，每个节点平均
        # 200 秒才被用一次，单节点密度降到 1/20，这是抗风控最有效的方式。
        if self.routes:
            await self.routes.rotate_node(self._site_host())

        batches = _chunk(list(part_names), MAX_PARTS_PER_REQUEST)
        scopes = self._build_scopes()
        site = self._site_host()

        all_stores: dict[str, StoreAvailability] = {}
        blocked = False
        failures: list[str] = []
        # 记录本轮失败的性质，供日志面板区分「节点不通」和「Apple 拦截」
        error_kind = ""
        error_detail = ""

        request_count = 0
        for scope_kind, scope_value in scopes:
            for index, batch in enumerate(batches):
                if index or request_count:
                    await asyncio.sleep(BATCH_GAP_S)
                request_count += 1

                # 被拦时换节点后重试**同一批次**。之前只 continue 到下一批，
                # 在「只有一批」的常见场景下等于本轮直接作废，切换白做。
                stores = None
                for attempt in range(MAX_SWITCH_RETRY + 1):
                    try:
                        stores = await self.browser.query(
                            parts=batch,
                            **({scope_kind: scope_value}),
                        )
                        break
                    except FetchError as exc:
                        log.warning("查询失败（%s=%s）：%s", scope_kind, scope_value, exc)
                        failures.append(f"{scope_kind}={scope_value}: {exc}")
                        error_kind, error_detail = _classify_error(exc)

                        can_retry = (
                            (exc.blocked or exc.rate_limited)
                            and self.routes
                            and attempt < MAX_SWITCH_RETRY
                        )
                        if not can_retry:
                            break

                        # 冷却当前节点并切换；切换成功就用新节点重试本批次
                        wait = await self.routes.on_blocked(site)
                        if wait > 0:
                            # 全部节点都在冷却：本轮作废，下一轮按等待时间调度
                            blocked = True
                            self._route_recovery_wait = wait
                            break
                        log.info("已切换出口，重试本批次（第 %d 次）", attempt + 1)
                        error_kind = ""
                        error_detail = ""

                if blocked:
                    break
                if stores is None:
                    # 非拦截类失败（如节点不通）：记下并继续下一批次
                    if error_kind in ("blocked", "rate_limited"):
                        blocked = True
                        break
                    continue

                for store in stores:
                    existing = all_stores.get(store.store_number)
                    if existing is None:
                        all_stores[store.store_number] = store
                    else:
                        existing.parts.update(store.parts)
            if blocked:
                break

        if blocked:
            self.state.consecutive_blocks += 1
            log.warning(
                "本轮被 Apple 拒绝（连续第 %d 次）。已停止本轮后续请求，稍后自动重试。",
                self.state.consecutive_blocks,
            )
            self._log_round(
                node=self._active_node(),
                ok=False,
                error_kind=error_kind or "blocked",
                error_detail=error_detail or "全部出口被拒或冷却中",
                store_count=0,
                part_count=0,
                hits=(),
            )
            await self._maybe_warn_degraded()
            return True

        if not all_stores:
            log.warning("本轮没有拿到任何门店数据：%s", "；".join(failures) or "无详情")
            self._log_round(
                node=self._active_node(),
                ok=False,
                error_kind=error_kind or "other",
                error_detail=error_detail or ("；".join(failures) or "无详情"),
                store_count=0,
                part_count=0,
                hits=(),
            )
            return False

        recovered = self.state.consecutive_blocks > 0
        self.state.consecutive_blocks = 0
        self.state.last_success_at = time.time()
        if recovered:
            log.info("已从被拦截状态恢复，库存结论重新可信")
            self.state.degraded_alerted_at = 0.0
        if self.routes:
            await self.routes.on_success(site)

        self.store_index = {
            number: (store.store_name, store.city) for number, store in all_stores.items()
        }

        alerts = self._reconcile(all_stores, part_names)
        await self._emit(alerts, all_stores, part_names)

        in_stock = sum(
            1
            for store in all_stores.values()
            for part, status in store.parts.items()
            if status.available and part in part_names
        )

        # 本轮有货明细，供日志面板逐条展示（门店 / 型号 / 取货文案）
        hit_details = tuple(
            (store.store_name, status.title or part_names.get(part, part), status.quote)
            for store in all_stores.values()
            for part, status in store.parts.items()
            if status.available and part in part_names
        )

        # 只把「有补货」的扫描记进历史。无货轮次没有信息量，记下来只会把
        # 真正想回看的补货记录冲掉。
        if in_stock:
            current_hits = {(h[0], h[1]) for h in hit_details}
            # 上一轮也有这批货 → 视为同一段持续有货，可合并；否则是新的补货事件
            continuous = bool(current_hits & self.state.last_hits) and (
                current_hits == self.state.last_hits
            )
            self.state.record_scan(
                ScanRecord(
                    at=time.time(),
                    store_count=len({h[0] for h in hit_details}),
                    part_count=len({h[1] for h in hit_details}),
                    details=tuple((h[0], h[1]) for h in hit_details),
                ),
                continuous=continuous,
            )
            self.state.last_hits = current_hits
        else:
            # 本轮无货：清空，下一批货算新的补货事件
            self.state.last_hits = set()

        # 每一轮都记日志（含无货），供面板排查「为什么没查到」
        self._log_round(
            node=self._active_node(),
            ok=True,
            error_kind="",
            error_detail="",
            store_count=len(all_stores),
            part_count=len(part_names),
            hits=hit_details,
        )
        log.info(
            "本轮完成：%d 家门店，%d 个型号，%d 个「有货」组合",
            len(all_stores),
            len(part_names),
            in_stock,
        )
        if failures:
            log.info("本轮有 %d 处查询失败，相关型号状态保持未知", len(failures))
        return False

    # -- 范围与型号 -------------------------------------------------------

    def _describe_scope(self) -> str:
        pieces = []
        if self.config.pickup.cities:
            pieces.append("城市：" + "、".join(self.config.pickup.cities))
        if self.config.pickup.stores:
            pieces.append("门店：" + "、".join(self.config.pickup.stores))
        return "；".join(pieces)

    def _build_scopes(self) -> list[tuple[str, str]]:
        scopes: list[tuple[str, str]] = [("location", city) for city in self.config.pickup.cities]
        scopes.extend(("store", store) for store in self.config.pickup.stores)
        return scopes

    async def _resolve_parts(self) -> dict[str, str]:
        """返回 {零件号: 显示名}。

        显式配置的 `products` 直接采用；`auto` 则从官网购买页现抓，这样新型号
        上市当天就能盯上，不必等本项目更新。
        """

        resolved: dict[str, str] = {spec.part: spec.display for spec in self.config.products}
        if not self.config.auto:
            return resolved

        for family in self.config.auto:
            try:
                discovered = await self.browser.discover_products(family)
            except FetchError as exc:
                log.error("自动发现型号失败（%s/%s）：%s", family.category, family.family or "*", exc)
                continue
            log.info(
                "自动发现 %s/%s：%d 个型号",
                family.category,
                family.family or "*",
                len(discovered),
            )
            for part, name in discovered.items():
                resolved.setdefault(part, name)
        return resolved

    # -- 状态比对与提醒 ---------------------------------------------------

    def _reconcile(
        self, stores: dict[str, StoreAvailability], part_names: dict[str, str]
    ) -> list[Alert]:
        now = time.time()
        alerts: list[Alert] = []
        for store in stores.values():
            for part, status in store.parts.items():
                if part not in part_names:
                    continue
                state = self.state.get(store.store_number, part)
                previous = state.available
                if status.available:
                    state.available = True
                    if previous is not True:
                        state.last_changed_at = now
                else:
                    state.available = False
                    if previous is not False:
                        state.last_changed_at = now

                if not status.available:
                    continue
                title = status.title or part_names.get(part, part)
                state.last_title = title
                state.last_quote = status.quote
                changed = previous is not True
                cooldown_passed = (now - state.last_alert_at) >= SAME_STOCK_COOLDOWN_S
                repeat_due = (
                    self.config.repeat_when_in_stock
                    and (now - state.last_alert_at) >= self.config.repeat_interval
                )
                if not self.config.repeat_when_in_stock and not changed:
                    continue
                if not (changed or (cooldown_passed and repeat_due)):
                    continue
                state.last_alert_at = now
                alerts.append(
                    Alert(
                        store_number=store.store_number,
                        store_name=store.store_name,
                        city=store.city,
                        part=part,
                        title=title,
                        quote=status.quote,
                    )
                )
        return alerts

    async def _emit(
        self,
        alerts: list[Alert],
        stores: dict[str, StoreAvailability],
        part_names: dict[str, str],
    ) -> None:
        if not alerts:
            return
        log.info("检测到 %d 条补货提醒", len(alerts))
        title, body = format_alert(self.config, alerts)
        self.state.last_alert_summary = body

        quiet = self.config.quiet_hours
        if quiet.covers(tz.current()):
            if quiet.mode == "defer":
                # 推迟到静默结束后补发：这里先攒起来，下一轮开始前统一发出。
                self._pending_alerts.append((title, body))
                log.info("当前处于静默时段，提醒已推迟（累计 %d 条）", len(self._pending_alerts))
            else:
                log.info("当前处于静默时段（%s–%s），本次提醒只写日志", quiet.start, quiet.end)
            return

        await self.notifier.send(title, body)

    async def _flush_pending(self) -> None:
        """把静默时段攒下的提醒一次性发出去。"""

        if not self._pending_alerts:
            return
        if self.config.quiet_hours.covers(tz.current()):
            return
        pending = self._pending_alerts
        self._pending_alerts = []
        title = f"补货提醒（{len(pending)} 条，静默时段积压）"
        body = "\n\n".join(text for _, text in pending)
        log.info("静默结束，补发 %d 条积压提醒", len(pending))
        await self.notifier.send(title, body)

    async def _maybe_warn_degraded(self) -> None:
        """连续被拦到一定次数时提醒一次。

        监控静默失效比漏掉一次补货更糟：用户会以为「一直没货」，实际上程序
        早就查不动了。这里只在越过阈值时提醒，避免每轮刷屏；恢复后重置。
        """

        threshold = self.config.degraded_alert_after
        if self.state.consecutive_blocks != threshold:
            return
        now = time.time()
        if now - self.state.degraded_alerted_at < 6 * 3600:
            return
        self.state.degraded_alerted_at = now
        title = "监控已被 Apple 限制（查询失败）"
        body = (
            f"已连续 {self.state.consecutive_blocks} 轮无法从 Apple 取得库存数据，"
            "当前监控处于失效状态，请勿据此判断「无货」。\n\n"
            "建议：\n"
            "· 调大 interval（例如 600 秒），给出口 IP 留出恢复时间\n"
            "· 不要反复重启程序——同一 IP 的持续请求会延长封禁\n"
            "· 若使用代理，考虑更换出口\n\n"
            "程序会继续按退避节奏自动重试，恢复后会再通知你。"
        )
        await self.notifier.send(title, body)

    # -- 节奏 -------------------------------------------------------------

    def _next_delay(self, blocked: bool) -> float:
        if blocked:
            ladder = self.config.backoff_ladder or BACKOFF_LADDER
            step = min(self.state.consecutive_blocks - 1, len(ladder) - 1)
            base = ladder[max(0, step)]
            # 轮换模式下所有节点都进了冷却时，按最早恢复时间等待，
            # 而不是按退避阶梯——节点恢复就能立即用，不必空等阶梯走完。
            recovery = getattr(self, "_route_recovery_wait", 0.0)
            if self.routes and recovery > 0:
                self._route_recovery_wait = 0.0
                return recovery
            # 退避也加抖动，避免多个实例在同一秒一起试探。
            return base * random.uniform(0.85, 1.15)
        base = float(self.config.interval)
        if self.config.jitter <= 0:
            return base
        return base * (1 + random.uniform(-self.config.jitter, self.config.jitter))

    def _active_node(self) -> str:
        """当前使用的出口，用于日志展示。"""

        if self.routes and self.routes.pool.active:
            return self.routes.pool.active
        return "直连" if not self.config.proxy else "（未轮转）"

    def _log_round(
        self,
        *,
        node: str,
        ok: bool,
        error_kind: str,
        error_detail: str,
        store_count: int,
        part_count: int,
        hits: tuple[tuple[str, str, str], ...],
    ) -> None:
        self.state.record_round(
            RoundLog(
                at=time.time(),
                node=node,
                ok=ok,
                error_kind=error_kind,
                error_detail=error_detail,
                store_count=store_count,
                part_count=part_count,
                hits=hits,
            )
        )
        if self.dashboard is not None:
            self.dashboard.record_round(self.state.rounds[0])

    def _site_host(self) -> str:
        """当前地区的 Apple 站点主机名，冷却按它记账。

        港澳日新澳马共用 `www.apple.com`，同一出口 IP 在这些地区之间的请求
        很可能被合并计数，所以冷却要合并；大陆是独立域名。
        """

        from urllib.parse import urlsplit

        return urlsplit(self.config.region.base_url).hostname or self.config.region.base_url


# -- 工具函数 -------------------------------------------------------------


def _chunk(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        return f"{seconds // 60} 分 {seconds % 60} 秒"
    return f"{seconds // 3600} 小时 {seconds % 3600 // 60} 分"


def _clock(seconds: float) -> str:
    at = datetime.fromtimestamp(time.time() + seconds, tz.zone())
    return at.strftime("%H:%M:%S")


def format_alert(config: Config, alerts: Iterable[Alert]) -> tuple[str, str]:
    alerts = list(alerts)
    parts = {alert.part for alert in alerts}
    stores = {alert.store_number for alert in alerts}
    if len(alerts) == 1:
        one = alerts[0]
        title = f"有货：{one.store_name}｜{_short(one.title)}"
    else:
        title = f"补货提醒：{len(stores)} 家门店 × {len(parts)} 个型号"

    by_store: dict[str, list[Alert]] = {}
    for alert in alerts:
        by_store.setdefault(alert.store_number, []).append(alert)

    lines: list[str] = []
    for store_alerts in by_store.values():
        head = store_alerts[0]
        city = f"（{head.city}）" if head.city else ""
        lines.append(f"{head.store_name}{city}")
        for alert in store_alerts:
            quote = f" · {alert.quote}" if alert.quote else ""
            lines.append(f"  · {_short(alert.title)}  {alert.part}{quote}")
    lines.append("")
    lines.append(f"地区：{config.region.title}　时间：{tz.current():%m-%d %H:%M}")
    lines.append(f"下单：{config.region.base_url}/shop/buy-iphone")
    return title, "\n".join(lines)


def _short(text: str, limit: int = 40) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _classify_error(exc: FetchError) -> tuple[str, str]:
    """把查询异常归类，供日志面板区分失败原因。

    用户最关心的是「是节点本身不通，还是被 Apple 拦了」——这两者的处置完全
    不同：节点不通要换节点，被拦要降频或换 IP。
    """

    detail = str(exc)
    if exc.blocked:
        return "blocked", detail
    if exc.rate_limited:
        return "rate_limited", detail
    lowered = detail.lower()
    if any(
        token in lowered
        for token in ("page.evaluate", "failed to fetch", "net::err", "timeout", "超时", "连接")
    ):
        return "node_error", detail
    return "other", detail
