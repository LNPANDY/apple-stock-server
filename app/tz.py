"""时区统一处理。

为什么需要这个
--------------
服务器时钟通常是 UTC（VPS 默认），而用户在中国。两处受影响：

1. **推送里的时间戳**。Bark 消息里的「时间：10-24 07:23」实际是 UTC，
   比北京时间慢 8 小时，用户看到会以为是历史推送。
2. **静默时段的判断**。用户设 23:00–08:00 期望的是北京时间；按 UTC 判断
   会在北京时间 15:00 生效，深夜反而照常推送。

网页面板不受影响：前端用 `toLocaleTimeString`，跟随浏览器时区。

所以后端所有「给人看的时间」和「按钟点判断的逻辑」都从这里取时间，
由 `timezone` 配置项统一控制（默认 Asia/Shanghai）。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

# 默认北京时间。configure() 在配置加载后覆盖。
_TZ = ZoneInfo("Asia/Shanghai")


def configure(timezone: str) -> None:
    """设置全局时区。非法名称抛 ZoneInfoKeyError，由调用方转为配置错误。"""

    global _TZ
    _TZ = ZoneInfo(timezone)


def current() -> datetime:
    """当前时间（带配置时区的 tzinfo）。"""

    return datetime.now(_TZ)


def zone() -> ZoneInfo:
    return _TZ
