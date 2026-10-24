"""配置回写。

面板上改设置要落回 `config.yaml`。这里用 ruamel.yaml 而不是 PyYAML：后者
会把注释和字段顺序全部丢掉，用户手写的说明性注释保存一次就没了。

写入是原子的（临时文件 + rename），避免写一半断电留下半个 YAML。
"""

from __future__ import annotations

import contextlib
import io
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import SingleQuotedScalarString as _QuotedScalarString

log = logging.getLogger(__name__)

_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.width = 4096
_yaml.indent(mapping=2, sequence=4, offset=2)


class ConfigWriteError(Exception):
    pass


def _load_raw(path: Path) -> Any:
    if not path.is_file():
        return None
    with path.open(encoding="utf-8") as handle:
        return _yaml.load(handle) or {}


def _dump_raw(data: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.StringIO()
    _yaml.dump(data, buffer)
    text = buffer.getvalue()

    # delete=False + 手动 rename：需要原子替换，不能让它自动删除
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".config-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        # 失败时清掉临时文件，不留垃圾
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _as_time_str(value: Any) -> str:
    """把时间统一成 `HH:MM` 字符串，并强制加引号。

    ruamel 默认会把 `23:30` 裸写出去，而 YAML 1.1 解析器（含它自己）会读成
    六十进制整数 1410。这里转成带引号的标量，保证回读仍是时间。
    """

    text = str(value).strip()
    if ":" not in text and text.isdigit():
        # 兼容已经被写坏的六十进制值
        total = int(text)
        text = f"{total // 60:02d}:{total % 60:02d}"
    return _QuotedScalarString(text)


def save_settings(
    path: Path,
    *,
    interval: int | None = None,
    jitter: float | None = None,
    cities: list[str] | None = None,
    stores: list[str] | None = None,
    products: list[dict[str, str]] | None = None,
    auto: list[dict[str, Any]] | None = None,
    panel_family: str | None = None,
    backoff: list[int] | None = None,
    degraded_alert_after: int | None = None,
    repeat_when_in_stock: bool | None = None,
    repeat_interval: int | None = None,
    quiet_hours: dict[str, Any] | None = None,
    notify: list[dict[str, Any]] | None = None,
) -> None:
    """把给定的字段写回配置文件，未提供的字段保持原样。"""

    try:
        data = _load_raw(path)
    except Exception as exc:
        raise ConfigWriteError(f"读取现有配置失败：{exc}") from exc
    if not isinstance(data, dict):
        data = {}

    if interval is not None:
        data["interval"] = int(interval)
    if jitter is not None:
        data["jitter"] = round(float(jitter), 3)

    if cities is not None or stores is not None:
        pickup = data.get("pickup")
        if not isinstance(pickup, dict):
            pickup = {}
        if cities is not None:
            pickup["cities"] = list(cities)
        if stores is not None:
            pickup["stores"] = list(stores)
        data["pickup"] = pickup

    if products is not None:
        data["products"] = [
            {"part": item["part"], "name": item.get("name") or item["part"]} for item in products
        ]

    if auto is not None:
        data["auto"] = auto

    if panel_family is not None:
        panel = data.get("panel")
        if not isinstance(panel, dict):
            panel = {}
        panel["family"] = str(panel_family)
        data["panel"] = panel

    if backoff is not None:
        data["backoff_ladder"] = [int(value) for value in backoff]
    if degraded_alert_after is not None:
        data["degraded_alert_after"] = int(degraded_alert_after)

    if repeat_when_in_stock is not None:
        data["repeat_when_in_stock"] = bool(repeat_when_in_stock)
    if repeat_interval is not None:
        data["repeat_interval"] = int(repeat_interval)

    if quiet_hours is not None:
        # 时间必须强制写成带引号的字符串。YAML 1.1 会把裸写的 23:30 当成六十
        # 进制整数 1410，回读时就不是时间了。
        data["quiet_hours"] = {
            "enabled": bool(quiet_hours.get("enabled")),
            "start": _as_time_str(quiet_hours.get("start") or "23:00"),
            "end": _as_time_str(quiet_hours.get("end") or "08:00"),
            "mode": str(quiet_hours.get("mode") or "silent"),
        }

    if notify is not None:
        data["notify"] = notify

    try:
        _dump_raw(data, path)
    except Exception as exc:
        raise ConfigWriteError(f"写入配置失败：{exc}") from exc
    log.info("配置已更新：%s", path)
