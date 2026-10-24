"""Clash / mihomo 外部控制接口客户端与出口线路池。

为什么走 Clash API 而不是给浏览器换代理
----------------------------------------
查询浏览器启动后固定连本地代理端口（如 `127.0.0.1:7890`），节点切换发生在
mihomo 内部。这样换节点不用重启浏览器，Apple 会话（cookie、verify.js 校验）
全部保留——这正是原项目的架构。

Shadowrocket 没有这类控制接口，所以用了轮换就必须跑一个 mihomo。订阅可以
和 Shadowrocket 用同一个机场链接，两者互不干扰。

切换节点后必须断开经过该策略组的旧连接：Chrome 会复用 HTTP/2 连接，不断开
的话后续请求仍然从旧节点出去，切换等于没发生。

冷却按 Apple 站点主机名记账：港澳日新澳马共用 `www.apple.com`，同一出口 IP
在这些地区之间的请求很可能被合并计数。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import aiohttp

log = logging.getLogger(__name__)

# 测速地址与 Clash 默认一致。不用 Apple 的地址，避免测速本身消耗 Apple 额度。
DELAY_TEST_URL = "https://www.gstatic.com/generate_204"
DELAY_TIMEOUT_MS = 3000

# 策略组里不能当作出口的条目类型：嵌套策略组、拒绝和兼容占位。
NON_ROUTE_TYPES = frozenset({
    "Selector", "URLTest", "Fallback", "LoadBalance", "Relay",
    "Reject", "RejectDrop", "Pass", "Compatible",
})

# 机场常用来展示套餐信息的假节点关键词。
INFO_NODE_KEYWORDS = ("剩余", "到期", "过期", "官网", "重置", "套餐", "网址", "expire", "traffic")

# 冷却档位：被 Apple 拒绝后逐级延长，成功一次即恢复。与原项目一致。
COOLDOWN_STEPS = (300, 600, 1200, 1800)
# 节点连不通（测速失败）：节点自身的问题，与 Apple 无关，短暂跳过即可。
UNREACHABLE_COOLDOWN = 60
# 连续多少个不同节点都被拒绝后停止切换——通常说明被拦的不只是 IP。
MAX_SWITCHES_PER_CYCLE = 10
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=4)


@dataclass
class RouteInfo:
    name: str
    delay_ms: int | None = None  # None = 未测速或测速失败
    cooling_until: float = 0.0


class ClashError(Exception):
    pass


class ClashClient:
    """mihomo / ClashVerge 外部控制接口的最小客户端。

    只做四件事：读策略组节点、测速、切换节点、断开经过该组的连接。

    支持两种接入方式：
    - `http://127.0.0.1:9090`：ClashX、原版 Clash 的 TCP 控制端口
    - `unix:/path/to/verge-mihomo.sock`：ClashVerge 默认只开 Unix socket，
      TCP 端口留空。用 socket 路径直连即可，不必去改它的配置。
    """

    def __init__(self, api: str, secret: str = "", group: str = "") -> None:
        raw = api.strip()
        self.socket_path: str | None = None
        if raw.startswith("unix:"):
            self.socket_path = raw[5:]
            self.base = "http://localhost"
        else:
            self.base = raw.rstrip("/")
        self.secret = secret
        self.group = group

    def _headers(self) -> dict[str, str]:
        if self.secret:
            return {"Authorization": f"Bearer {self.secret}"}
        return {}

    def _connector(self) -> aiohttp.BaseConnector:
        if self.socket_path:
            return aiohttp.UnixConnector(path=self.socket_path)
        return aiohttp.TCPConnector()

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        url = f"{self.base}{path}"
        try:
            async with aiohttp.ClientSession(
                timeout=REQUEST_TIMEOUT,
                headers=self._headers(),
                connector=self._connector(),
            ) as session:
                async with session.request(method, url, **kwargs) as resp:
                    if resp.status >= 300:
                        text = await resp.text()
                        raise ClashError(f"Clash API {path} 返回 HTTP {resp.status}：{text[:120]}")
                    if resp.content_type == "application/json":
                        return await resp.json()
                    return {}
        except aiohttp.ClientError as exc:
            raise ClashError(f"Clash API {path} 连接失败：{exc}") from exc

    def describe(self) -> str:
        return f"unix:{self.socket_path}" if self.socket_path else self.base

    async def ping(self) -> bool:
        """控制接口是否可达。"""

        try:
            await self._request("GET", "/version")
            return True
        except ClashError:
            return False

    async def group_nodes(self) -> list[str]:
        """列出策略组里可用的出口节点，剔除嵌套组和假节点。"""

        data = await self._request("GET", "/proxies")
        proxies = data.get("proxies") or {}
        entry = proxies.get(self.group)
        if entry is None:
            raise ClashError(f"策略组 {self.group!r} 不存在。可用组：{sorted(proxies)[:10]}…")
        members = entry.get("all") or []
        out: list[str] = []
        for name in members:
            info = proxies.get(name)
            if info and str(info.get("type", "")).lower() in {t.lower() for t in NON_ROUTE_TYPES}:
                continue  # 嵌套策略组、拒绝占位等不能当出口
            if any(keyword in name for keyword in INFO_NODE_KEYWORDS):
                continue  # 机场的套餐信息假节点
            out.append(name)
        return out

    async def current(self) -> str | None:
        """当前实际生效的出口节点。

        策略组可能指向另一个策略组（例如 `Apple` → `机场总组` → `自动选择`），
        这种情况下 `now` 返回的是组名，而不是真正出网的节点。这里递归往下追，
        直到拿到叶子节点——否则冷却会记在组名上，真实节点永远不被冷却。
        """

        data = await self._request("GET", "/proxies")
        proxies = data.get("proxies") or {}
        name = (proxies.get(self.group) or {}).get("now")
        seen: set[str] = set()
        while name and name not in seen:
            seen.add(name)
            info = proxies.get(name) or {}
            kind = str(info.get("type", "")).lower()
            if kind not in {t.lower() for t in NON_ROUTE_TYPES}:
                return name  # 叶子节点
            name = info.get("now")
        return name

    async def select(self, node: str) -> None:
        await self._request(
            "PUT", f"/proxies/{self.group}", json={"name": node}
        )

    async def delay(self, node: str) -> int | None:
        """测单个节点延迟，毫秒；不通返回 None。不消耗 Apple 额度。"""

        try:
            data = await self._request(
                "GET",
                f"/proxies/{node}/delay",
                params={"url": DELAY_TEST_URL, "timeout": DELAY_TIMEOUT_MS},
            )
            return int(data.get("delay") or 0) or None
        except ClashError:
            return None

    async def close_group_connections(self) -> int:
        """断开链路里包含该策略组的连接，返回断开的数量。

        Chrome 复用 HTTP/2 连接；切换节点后不断开旧连接，请求仍从旧节点出去。
        只断链路里包含本策略组的连接，不影响其他软件。
        """

        data = await self._request("GET", "/connections")
        closed = 0
        for conn in data.get("connections") or []:
            chains = conn.get("chains") or []
            if self.group in chains:
                conn_id = conn.get("id")
                if conn_id:
                    await self._request("DELETE", f"/connections/{conn_id}")
                    closed += 1
        return closed


@dataclass
class RoutePool:
    """出口线路池：轮转使用节点，并按站点记录冷却。

    两种使用方式：
    - `rotate=True`（默认）：**每轮换一个节点**。20 个节点 × 10 秒间隔 =
      每个节点平均 200 秒才被用一次，单节点的请求密度降到原来的 1/20，
      这是抗风控最有效的方式。
    - `rotate=False`：只在当前节点被拒时才换，适合节点很少或很贵的情况。

    冷却档位按「连续被拒次数」计：第 1 次被拒冷却 5 分钟，探测仍被拒依次
    10 → 20 → 30 分钟；成功一次即恢复到 0 档。
    """

    nodes: list[str] = field(default_factory=list)
    active: str | None = None
    # (节点, 站点) → [档位, 冷却截止时间戳]
    cooldowns: dict[tuple[str, str], list] = field(default_factory=dict)
    unreachable: dict[str, float] = field(default_factory=dict)
    # 本轮已切换次数；begin_cycle 清零
    switches_this_cycle: int = 0
    # 轮转游标：每次轮换前进一格
    cursor: int = 0
    rotate: bool = True
    # 每轮是否把 DIRECT（家宽）也当成一个出口参与轮转
    use_direct: bool = False

    def set_nodes(self, nodes: list[str]) -> None:
        """用最新节点列表替换旧列表，仍存在的节点保留冷却记录。"""

        keep = set(nodes)
        self.nodes = list(nodes)
        self.cooldowns = {k: v for k, v in self.cooldowns.items() if k[0] in keep}
        self.unreachable = {k: v for k, v in self.unreachable.items() if k in keep}
        if self.active and self.active not in keep:
            self.active = None
        self.cursor = 0

    def begin_cycle(self) -> None:
        self.switches_this_cycle = 0

    def candidates(self) -> list[str]:
        """本轮可轮转的出口列表。

        `DIRECT` 是 mihomo 的内置直连出口，把它加进列表就等于把家宽也算
        一个节点——既能摊薄每个节点的请求密度，又能在所有节点都失效时兜底。
        """

        out = list(self.nodes)
        if self.use_direct and "DIRECT" not in out:
            out.append("DIRECT")
        return out

    def next_rotation(self, site: str, now: float) -> str | None:
        """轮转：按游标取下一个健康节点。

        跳过冷却中的节点；全部在冷却则返回 None，由调用方决定等待。
        """

        pool = self.candidates()
        if not pool:
            return None
        total = len(pool)
        for offset in range(total):
            index = (self.cursor + offset) % total
            node = pool[index]
            if self.cooling_remaining(node, site, now) > 0:
                continue
            if self.unreachable.get(node, 0) > now:
                continue
            self.cursor = (index + 1) % total
            return node
        return None

    @staticmethod
    def site_key(node: str, site: str) -> tuple[str, str]:
        return (node, site)

    def cooling_remaining(self, node: str, site: str, now: float) -> float:
        entry = self.cooldowns.get(self.site_key(node, site))
        if not entry:
            return 0.0
        return max(0.0, entry[1] - now)

    def block(self, node: str, site: str, now: float) -> float:
        """节点被 Apple 拒绝：冷却档位 +1，返回本次冷却时长（秒）。

        第 1 次被拒 5 分钟；冷却结束后的第一次请求仍被拒，则依次延长到
        10、20、30 分钟。成功一次即恢复（见 `mark_ok`）。
        """

        key = self.site_key(node, site)
        entry = self.cooldowns.get(key)
        if entry and entry[1] > now:
            # 仍在冷却中又被拒：说明是下一轮的探测，升档重新计时
            level = min(entry[0] + 1, len(COOLDOWN_STEPS) - 1)
        elif entry:
            level = min(entry[0] + 1, len(COOLDOWN_STEPS) - 1)
        else:
            level = 0
        duration = COOLDOWN_STEPS[level]
        self.cooldowns[key] = [level, now + duration]
        return duration

    def mark_ok(self, node: str, site: str) -> None:
        """节点成功一次：清除冷却记录。"""

        self.cooldowns.pop(self.site_key(node, site), None)

    def mark_unreachable(self, node: str, now: float) -> None:
        self.unreachable[node] = now + UNREACHABLE_COOLDOWN

    def next_node(self, site: str, now: float, exclude: set[str] | None = None) -> str | None:
        """挑下一个可用节点；全部在冷却则返回 None。

        排除当前节点、冷却中的节点、最近连不通的节点，以及调用方指定跳过的
        节点（通常是本轮已经试过的）。`use_direct` 打开时 DIRECT 也参与，
        作为最后一道兜底。
        """

        exclude = set(exclude or ())
        if self.active:
            exclude.add(self.active)
        # 节点优先，DIRECT 放最后：家宽是兜底，不是首选。
        pool = self.candidates()
        if "DIRECT" in pool:
            pool = [n for n in pool if n != "DIRECT"] + ["DIRECT"]
        for node in pool:
            if node in exclude:
                continue
            if self.cooling_remaining(node, site, now) > 0:
                continue
            if self.unreachable.get(node, 0) > now:
                continue
            return node
        return None

    def earliest_recovery(self, site: str, now: float) -> float | None:
        """所有节点都不可用时，最早什么时候能再试（秒数）。"""

        times = [
            entry[1] - now
            for node in self.nodes
            if (entry := self.cooldowns.get(self.site_key(node, site)))
        ]
        times += [t - now for t in self.unreachable.values() if t > now]
        return max(0.0, min(times)) if times else None


class RouteManager:
    """把 ClashClient 和 RoutePool 串成轮换流程，供监控循环调用。

    两种模式：

    **轮转（rotate=True，默认）**：每轮开始前主动换一个节点。20 个节点 ×
    10 秒间隔，每个节点平均 200 秒才用一次——单节点请求密度降到 1/20，
    这是抗风控最有效的方式，也避免了「一个节点被往死里用」。

    **被动（rotate=False）**：只在被 Apple 拒绝时才换节点。

    `excluded_this_round` 记录本轮已被拒的节点：连续 10 个不同节点都被拒时
    停止切换——通常说明被拦的不只是 IP（可能是指纹或会话），换节点没有意义。
    """

    def __init__(self, client: ClashClient, pool: RoutePool) -> None:
        self.client = client
        self.pool = pool
        self.excluded_this_round: set[str] = set()
        self._nodes_loaded = False
        self._nodes_at = 0.0

    # 节点列表刷新间隔（秒）。订阅更新、隐藏节点后无需重启进程。
    NODES_REFRESH_S = 600

    async def ensure_nodes(self) -> None:
        """拉取节点列表。首次必拉，之后每 10 分钟刷新一次。

        用户在 ClashVerge 里隐藏/新增节点后，不必重启监控进程——下次刷新
        就会跟上。仍存在的节点保留冷却记录，不会因为刷新把冷却清掉。
        """

        now = time.time()
        if self._nodes_loaded and now - self._nodes_at < self.NODES_REFRESH_S:
            return
        try:
            nodes = await self.client.group_nodes()
        except ClashError as exc:
            # 刷新失败不影响已有节点列表，继续用旧的
            log.warning("刷新节点列表失败，沿用上次的 %d 个：%s", len(self.pool.nodes), exc)
            return
        if not nodes:
            return
        previous = set(self.pool.nodes)
        self.pool.set_nodes(nodes)
        self._nodes_loaded = True
        self._nodes_at = now
        if previous and previous != set(nodes):
            added = sorted(set(nodes) - previous)
            removed = sorted(previous - set(nodes))
            log.info(
                "节点列表已更新：%d 个（新增 %d，移除 %d）",
                len(nodes),
                len(added),
                len(removed),
            )
            if removed:
                log.info("已移除：%s", "、".join(removed[:5]) + ("…" if len(removed) > 5 else ""))
        else:
            log.info(
                "出口轮换就绪：%d 个节点（策略组 %s）%s",
                len(nodes),
                self.client.group,
                "，每轮轮转" if self.pool.rotate else "，仅在失败时切换",
            )

    async def rotate_node(self, site: str) -> str | None:
        """轮转模式下每轮开始时调用：切到下一个节点。

        返回新节点名；没有可用节点时返回 None（调用方继续用当前出口）。
        如果已经是该节点则不重复切换，避免无谓地断开连接。
        """

        if not self.pool.rotate:
            return None
        await self.ensure_nodes()
        node = self.pool.next_rotation(site, time.time())
        if node is None or node == self.pool.active:
            return None
        try:
            await self.client.select(node)
        except ClashError as exc:
            log.warning("轮转到 %s 失败：%s", node, exc)
            return None
        self.pool.active = node
        # 这里**不**主动断开旧连接。
        #
        # 轮转是「预先换节点」，此刻浏览器空闲，没有在途请求；旧连接会自然
        # 过期。反过来，如果在这里断连接，Chrome 下一次 fetch 可能正好撞上
        # 连接被拔，报 `TypeError: Failed to fetch`——实测每轮都失败。
        #
        # 真正需要断连接的是「被 Apple 拒绝后切换」：那时必须让后续请求立刻
        # 走新节点，不能等旧连接自然过期（见 switch()）。
        log.info("本轮出口：%s", node)
        return node

    async def switch(self, site: str) -> str | None:
        """切换到下一个可用节点；返回新节点名，无可用节点返回 None。"""

        await self.ensure_nodes()
        now = time.time()
        if self.pool.switches_this_cycle >= MAX_SWITCHES_PER_CYCLE:
            log.warning(
                "本轮已有 %d 个不同节点被拒，停止切换（被拦的可能不只是 IP）",
                self.pool.switches_this_cycle,
            )
            return None
        node = self.pool.next_node(site, now, exclude=self.excluded_this_round)
        if node is None:
            return None
        await self.client.select(node)
        closed = await self.client.close_group_connections()
        self.pool.active = node
        self.pool.switches_this_cycle += 1
        self.excluded_this_round.add(node)
        log.info(
            "出口已切换：%s（本轮第 %d 次切换，断开 %d 条旧连接）",
            node,
            self.pool.switches_this_cycle,
            closed,
        )
        return node

    async def on_blocked(self, site: str) -> float:
        """当前节点被 Apple 拒绝：冷却它，尝试切换，返回建议等待秒数。

        返回值含义：
        - 0.0：切换成功，可以立即重试
        - >0：全部节点都在冷却，等到最早恢复的节点
        """

        await self.ensure_nodes()
        now = time.time()

        # 首次被拒时 pool.active 还是空的（进程刚启动、没切过节点），
        # 要从 Clash 问出当前真正在用的节点。绝不能退化成占位字符串——
        # 那会把冷却记在一个不存在的名字上，真实节点永远不会被冷却。
        current = self.pool.active or await self.client.current()
        if not current:
            log.warning("无法确定当前出口节点，跳过冷却，直接尝试切换")
            new_node = await self.switch(site)
            if new_node is not None:
                return 0.0
            return float(COOLDOWN_STEPS[0])

        cooldown = self.pool.block(current, site, now)
        log.info(
            "节点 %s 在 %s 被 Apple 拒绝，冷却 %.0f 分钟",
            current,
            site,
            cooldown / 60,
        )
        self.excluded_this_round.add(current)
        new_node = await self.switch(site)
        if new_node is not None:
            return 0.0
        recovery = self.pool.earliest_recovery(site, now)
        return recovery if recovery is not None else cooldown

    async def on_success(self, site: str) -> None:
        """查询成功：当前节点恢复满血，清空本轮排除名单。"""

        current = self.pool.active
        if current:
            self.pool.mark_ok(current, site)

    def begin_cycle(self) -> None:
        self.pool.begin_cycle()
        self.excluded_this_round.clear()
