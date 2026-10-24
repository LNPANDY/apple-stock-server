"""配置加载与校验。

配置用 YAML 写，字段全部带默认值：最小可用配置只有 `region` 一项。
所有错误都在启动时一次性抛出，避免跑到半夜才发现某个字段名写错。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .regions import Region, resolve_region

DEFAULT_INTERVAL = 300
DEFAULT_JITTER = 0.2
# 单出口时的下限。
MIN_INTERVAL = 120
# 多节点轮转时的下限。轮转让每个节点承受的请求密度是 interval × 节点数：
# 33 个节点 × 5 秒 = 每个节点 165 秒才被用一次，仍然很安全。
# 节点越多这个值可以越小；保留 5 秒是为了避免单轮查询（约 3 秒）还没跑完
# 就开始下一轮，造成请求重叠。
MIN_INTERVAL_ROTATE = 5


class ConfigError(Exception):
    """配置文件本身有问题，重试没有意义。"""


@dataclass(frozen=True)
class ProductSpec:
    """一个要盯的零件号。"""

    part: str
    name: str = ""

    @property
    def display(self) -> str:
        return self.name or self.part


@dataclass(frozen=True)
class AutoFamily:
    """从官网购买页自动发现型号。"""

    category: str
    family: str = ""
    color: tuple[str, ...] = ()
    capacity: tuple[str, ...] = ()
    screen: tuple[str, ...] = ()


@dataclass(frozen=True)
class PickupScope:
    """取货查询的范围。

    `cities` 走 `location=` 查询，一次请求就能拿到该城市及周边全部门店的
    库存，是压降请求量最关键的一环。`stores` 走 `store=` 查询，用于城市
    查询覆盖不到的门店，或只需要盯一家店的情况。
    """

    cities: tuple[str, ...] = ()
    stores: tuple[str, ...] = ()


@dataclass(frozen=True)
class NotifyChannel:
    type: str
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class QuietHours:
    """静默时段：这段时间内只记录日志，不推送。

    补货提醒的价值在于「立刻看到」，但凌晨三点的推送通常只换来关掉通知。
    默认关闭，避免替用户做决定。
    """

    enabled: bool = False
    start: str = "23:00"
    end: str = "08:00"
    mode: str = "silent"  # silent = 不推送；defer = 推迟到结束后补发

    def covers(self, moment: Any) -> bool:
        if not self.enabled:
            return False
        start = _parse_hhmm(self.start)
        end = _parse_hhmm(self.end)
        if start is None or end is None:
            return False
        current = moment.hour * 60 + moment.minute
        if start == end:
            return False
        if start < end:
            return start <= current < end
        # 跨午夜，例如 23:00 → 08:00
        return current >= start or current < end


def _parse_hhmm(value: Any) -> int | None:
    """解析 `HH:MM`，返回当天的分钟数。

    也接受 YAML 1.1 把 `23:30` 读成的六十进制整数 1410——旧配置可能已经这么
    写下去了，不能因此让程序起不来。
    """

    if isinstance(value, int):
        if 0 <= value < 24 * 60:
            return value
        return None
    try:
        hour, minute = str(value).strip().split(":")
        hour_i, minute_i = int(hour), int(minute)
    except (ValueError, AttributeError):
        return None
    if not (0 <= hour_i < 24 and 0 <= minute_i < 60):
        return None
    return hour_i * 60 + minute_i


def _normalize_hhmm(value: Any, fallback: str) -> str:
    """把任意可解析的时间统一回 `HH:MM` 文本。"""

    minutes = _parse_hhmm(value)
    if minutes is None:
        minutes = _parse_hhmm(fallback)
    if minutes is None:
        return fallback
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


@dataclass(frozen=True)
class Config:
    region: Region
    interval: int
    jitter: float
    pickup: PickupScope
    products: tuple[ProductSpec, ...]
    auto: tuple[AutoFamily, ...]
    notify: tuple[NotifyChannel, ...]
    repeat_when_in_stock: bool
    repeat_interval: int
    state_file: Path
    log_level: str
    degraded_alert_after: int
    backoff_ladder: tuple[int, ...] = ()
    quiet_hours: QuietHours = field(default_factory=QuietHours)
    config_path: Path = Path("config.yaml")
    panel_family: str = ""
    proxy: str = ""
    proxy_bypass: str = ""
    # Clash/mihomo 出口轮换。api 非空即启用；proxy 应指向同一个实例的代理端口。
    clash_api: str = ""
    clash_secret: str = ""
    clash_group: str = ""
    # 每轮轮转出口（推荐）。关闭则只在被拦时切换。
    rotate_routes: bool = True
    panel_token: str = ""
    # 推送时间戳与静默时段使用的时区。服务器时钟通常是 UTC，不设这个
    # 的话推送里的时间会差 8 小时，静默时段也会错位。
    timezone: str = "Asia/Shanghai"
    # 是否把家宽（DIRECT）也算一个出口，参与轮转/兜底
    use_direct_route: bool = False


def _as_str_tuple(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if not isinstance(value, (list, tuple)):
        raise ConfigError(f"{field_name} 应当是字符串或字符串列表")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ConfigError(f"{field_name} 里出现了非字符串项：{item!r}")
        text = item.strip()
        if text:
            out.append(text)
    return tuple(out)


def _parse_products(raw: Any) -> tuple[ProductSpec, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError("products 应当是列表")
    out: list[ProductSpec] = []
    for index, item in enumerate(raw):
        where = f"products[{index}]"
        if isinstance(item, str):
            part, name = item.strip(), ""
        elif isinstance(item, dict):
            part = str(item.get("part", "")).strip()
            name = str(item.get("name", "")).strip()
        else:
            raise ConfigError(f"{where} 应当是零件号字符串或包含 part 的字典")
        if not part:
            raise ConfigError(f"{where} 缺少 part（零件号）")
        out.append(ProductSpec(part=part, name=name))
    return tuple(out)


def _parse_auto(raw: Any) -> tuple[AutoFamily, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError("auto 应当是列表")
    out: list[AutoFamily] = []
    for index, item in enumerate(raw):
        where = f"auto[{index}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{where} 应当是字典")
        category = str(item.get("category", "")).strip()
        if not category:
            raise ConfigError(f"{where} 缺少 category，例如 iphone / ipad / mac / watch")
        filters = item.get("filters") or {}
        if not isinstance(filters, dict):
            raise ConfigError(f"{where}.filters 应当是字典")
        out.append(
            AutoFamily(
                category=category,
                family=str(item.get("family", "")).strip(),
                color=_as_str_tuple(filters.get("color"), f"{where}.filters.color"),
                capacity=_as_str_tuple(filters.get("capacity"), f"{where}.filters.capacity"),
                screen=_as_str_tuple(filters.get("screen"), f"{where}.filters.screen"),
            )
        )
    return tuple(out)


def _parse_pickup(raw: Any) -> PickupScope:
    if raw is None:
        raise ConfigError("缺少 pickup 配置：至少需要 cities 或 stores 之一")
    if not isinstance(raw, dict):
        raise ConfigError("pickup 应当是字典")
    scope = PickupScope(
        cities=_as_str_tuple(raw.get("cities"), "pickup.cities"),
        stores=_as_str_tuple(raw.get("stores"), "pickup.stores"),
    )
    if not scope.cities and not scope.stores:
        raise ConfigError("pickup.cities 与 pickup.stores 至少填一个")
    return scope


def _parse_notify(raw: Any) -> tuple[NotifyChannel, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError("notify 应当是列表")
    out: list[NotifyChannel] = []
    for index, item in enumerate(raw):
        where = f"notify[{index}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{where} 应当是字典")
        kind = str(item.get("type", "")).strip().lower()
        if not kind:
            raise ConfigError(f"{where} 缺少 type")
        options = {k: v for k, v in item.items() if k != "type"}
        out.append(NotifyChannel(type=kind, options=options))
    return tuple(out)


def _expand_env(value: Any) -> Any:
    """把 `env:VAR_NAME` 形式的字符串展开成环境变量。

    通知密钥不适合直接写进仓库里的配置文件，这个约定让用户可以把密钥放
    在 systemd 的 EnvironmentFile 或 `.env` 里。
    """

    if isinstance(value, str) and value.startswith("env:"):
        name = value[4:].strip()
        resolved = os.environ.get(name, "")
        if not resolved:
            raise ConfigError(
                f"配置引用了环境变量 {name}，但它没有值（请在 EnvironmentFile 或 .env 里设置）"
            )
        return resolved
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def load_config(path: str | Path) -> Config:
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"找不到配置文件：{path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"配置文件不是合法 YAML：{exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("配置文件顶层应当是字典")

    raw = _expand_env(raw)

    region_value = raw.get("region")
    if not region_value:
        raise ConfigError("缺少 region，例如 region: cn")
    try:
        region = resolve_region(str(region_value))
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc

    try:
        interval = int(raw.get("interval", DEFAULT_INTERVAL))
    except (TypeError, ValueError) as exc:
        raise ConfigError("interval 应当是整数秒") from exc

    # 有出口轮转时下限可以短得多：轮转让每个节点承受的密度变成
    # interval × 节点数，10 秒 × 20 个节点 = 每个节点 200 秒一次。
    rotating = bool(str(raw.get("clash_api") or "").strip()) and bool(
        raw.get("rotate_routes", True)
    )
    floor = MIN_INTERVAL_ROTATE if rotating else MIN_INTERVAL
    if interval < floor:
        if rotating:
            raise ConfigError(f"interval={interval} 太短，轮转模式下最低 {floor} 秒")
        raise ConfigError(
            f"interval={interval} 太短。单出口时最低 {MIN_INTERVAL} 秒："
            "缩短间隔只会更快耗尽出口额度。配置了 clash_api 并开启轮转"
            "（rotate_routes: true）才允许更短的间隔。"
        )

    jitter = float(raw.get("jitter", DEFAULT_JITTER))
    if not 0.0 <= jitter <= 0.9:
        raise ConfigError("jitter 应当在 0 到 0.9 之间（表示间隔的浮动比例）")

    products = _parse_products(raw.get("products"))
    auto = _parse_auto(raw.get("auto"))
    if not products and not auto:
        raise ConfigError("products 与 auto 都是空的，没有可以监控的型号")

    state_file = Path(os.path.expanduser(str(raw.get("state_file", "data/state.json"))))

    return Config(
        region=region,
        interval=interval,
        jitter=jitter,
        pickup=_parse_pickup(raw.get("pickup")),
        products=products,
        auto=auto,
        notify=_parse_notify(raw.get("notify")),
        repeat_when_in_stock=bool(raw.get("repeat_when_in_stock", True)),
        repeat_interval=max(60, int(raw.get("repeat_interval", 1800))),
        state_file=state_file,
        log_level=str(raw.get("log_level", "INFO")).upper(),
        degraded_alert_after=max(2, int(raw.get("degraded_alert_after", 4))),
        backoff_ladder=_parse_backoff(raw.get("backoff_ladder")),
        quiet_hours=_parse_quiet_hours(raw.get("quiet_hours")),
        config_path=path,
        panel_family=str((raw.get("panel") or {}).get("family") or ""),
        proxy=str(raw.get("proxy") or "").strip(),
        proxy_bypass=str(raw.get("proxy_bypass") or "").strip(),
        clash_api=str(raw.get("clash_api") or "").strip(),
        clash_secret=str(raw.get("clash_secret") or "").strip(),
        clash_group=str(raw.get("clash_group") or "").strip(),
        rotate_routes=bool(raw.get("rotate_routes", True)),
        panel_token=str(raw.get("panel_token") or "").strip(),
        timezone=_parse_timezone(raw.get("timezone", "Asia/Shanghai")),
        use_direct_route=bool(raw.get("use_direct_route", False)),
    )


def _parse_timezone(value: Any) -> str:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    name = str(value or "Asia/Shanghai").strip()
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise ConfigError(
            f"timezone {name!r} 不是合法时区名（例如 Asia/Shanghai、UTC）。"
            f"完整列表见 zoneinfo 文档。"
        ) from exc
    return name


DEFAULT_BACKOFF = (600, 1200, 2400, 3600, 7200)


def _parse_backoff(raw: Any) -> tuple[int, ...]:
    """解析失败后的退避阶梯（秒）。

    必须是递增的正数序列；写错就直接报错，而不是悄悄退回默认值——退避是本
    项目避免风控的核心机制，静默失效比启动失败更糟。
    """

    if raw is None:
        return DEFAULT_BACKOFF
    if not isinstance(raw, list) or not raw:
        raise ConfigError("backoff_ladder 应当是非空的秒数列表，例如 [600, 1200, 2400]")
    values: list[int] = []
    for item in raw:
        try:
            seconds = int(item)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"backoff_ladder 里有非数字项：{item!r}") from exc
        if seconds < 60:
            raise ConfigError(f"backoff_ladder 里的 {seconds} 秒太短，最少 60 秒")
        values.append(seconds)
    if values != sorted(values):
        raise ConfigError("backoff_ladder 必须递增")
    return tuple(values)


def _parse_quiet_hours(raw: Any) -> QuietHours:
    if raw is None:
        return QuietHours()
    if isinstance(raw, str):
        # 允许简写 "23:00-08:00"
        text = raw.strip()
        if "-" in text:
            start, _, end = text.partition("-")
            return QuietHours(
                enabled=True,
                start=_normalize_hhmm(start.strip(), "23:00"),
                end=_normalize_hhmm(end.strip(), "08:00"),
            )
        raise ConfigError('quiet_hours 写法应为 "23:00-08:00" 或包含 start/end 的字典')
    if not isinstance(raw, dict):
        raise ConfigError("quiet_hours 应当是字典或 \"HH:MM-HH:MM\" 字符串")

    # 值可能是 "23:30"，也可能是 YAML 读出来的整数 1410，统一归一化。
    start = _normalize_hhmm(raw.get("start", "23:00"), "23:00")
    end = _normalize_hhmm(raw.get("end", "08:00"), "08:00")
    for label, value in (("start", raw.get("start", "23:00")), ("end", raw.get("end", "08:00"))):
        if _parse_hhmm(value) is None:
            raise ConfigError(f"quiet_hours.{label} 不是合法时间：{value!r}（应为 HH:MM）")
    mode = str(raw.get("mode", "silent")).strip().lower()
    if mode not in ("silent", "defer"):
        raise ConfigError("quiet_hours.mode 只能是 silent 或 defer")
    return QuietHours(enabled=bool(raw.get("enabled", True)), start=start, end=end, mode=mode)
