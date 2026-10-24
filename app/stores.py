"""门店目录。

面板需要让用户从「真实存在的门店」里选，而不是手敲 R 编号。数据来自 Apple
的门店列表页（`/retail/storelist/`），结构与项目内置的 `stores.json` 相同。

抓取失败时回退到内置快照：门店列表只影响界面选项，不应该因为一次网络失败
就让整个面板打不开。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

import aiohttp

from .regions import Region
from .tls import ssl_context

log = logging.getLogger(__name__)

STORELIST_PATH = "/retail/storelist/"
CACHE_TTL_S = 24 * 3600
TIMEOUT = aiohttp.ClientTimeout(total=20)


@dataclass(frozen=True)
class StoreInfo:
    number: str
    name: str
    province: str
    city: str

    @property
    def label(self) -> str:
        return f"{self.name}（{self.city}）"

    @property
    def location(self) -> str:
        """`location=` 查询用的地点串。

        大陆站必须写成「省 市」。直辖市的省和市同名，Apple 那边要求写成
        「北京 北京」而不是「北京」——只写一个词会被拒绝，返回空门店。
        """

        province = self.province.strip()
        city = self.city.strip()
        if province and city and province != city:
            return f"{province} {city}"
        if province:
            return f"{province} {province}"
        return city


@dataclass
class StoreCatalog:
    stores: list[StoreInfo]
    fetched_at: float = 0.0
    source: str = "内置"

    def by_number(self) -> dict[str, StoreInfo]:
        return {store.number: store for store in self.stores}

    def provinces(self) -> list[str]:
        seen: list[str] = []
        for store in self.stores:
            if store.province and store.province not in seen:
                seen.append(store.province)
        return seen

    def in_province(self, province: str) -> list[StoreInfo]:
        return [store for store in self.stores if store.province == province]

    def resolve_locations(self, provinces: list[str]) -> list[str]:
        """把省份名展开成 `location=` 查询用的「省 市」串。

        大陆站只写城市或只写省份都会被拒绝，必须是「省 市」。一个省可能有多个
        城市（广东有广州、深圳、东莞……），因此按城市分组，每个城市出一个查询
        串，最后去重。这是「每省一次请求」在单城市省份成立、多城市省份会多出
        几次的原因——如实展开，而不是假装只发一次。
        """

        out: list[str] = []
        for raw in provinces:
            value = raw.strip()
            if not value:
                continue
            matched = [s for s in self.stores if s.province == value]
            if not matched:
                matched = [s for s in self.stores if s.city == value]
            if not matched:
                # 认不出来就原样透传，交给 Apple 判断
                if value not in out:
                    out.append(value)
                continue
            for store in matched:
                location = store.location
                if location and location not in out:
                    out.append(location)
        return out

    def province_of_location(self, location: str) -> str:
        """把 `location=` 查询串反查回省份名。

        配置里存的是「北京 北京」这种查询串，但面板按省份展示和保存。反查不到
        时退回第一个词——「北京 北京」的第一段正好就是省。
        """

        value = location.strip()
        for store in self.stores:
            if store.location == value:
                return store.province
        for store in self.stores:
            if store.province and value.startswith(store.province):
                return store.province
        head = value.split()[0] if value.split() else value
        return head


def parse_storelist(payload: dict[str, Any]) -> list[StoreInfo]:
    """从门店列表页的 `__NEXT_DATA__` 里抽出中国大陆门店。"""

    groups = payload.get("props", {}).get("pageProps", {}).get("storeList") or []
    out: list[StoreInfo] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        if group.get("locale") not in ("zh_CN",):
            continue
        for province in group.get("state") or []:
            province_name = str(province.get("name") or "").strip()
            for store in province.get("store") or []:
                number = str(store.get("id") or "").strip()
                if not number:
                    continue
                address = store.get("address") or {}
                city = str(address.get("city") or province_name).strip()
                out.append(
                    StoreInfo(
                        number=number,
                        name=str(store.get("name") or number).strip(),
                        province=province_name,
                        city=city,
                    )
                )
    return out


def _extract_next_data(html: str) -> dict[str, Any] | None:
    marker = "__NEXT_DATA__"
    index = html.find(marker)
    if index < 0:
        return None
    start = html.find("{", index)
    end = html.find("</script>", start)
    if start < 0 or end < 0:
        return None
    try:
        return json.loads(html[start:end].rstrip())
    except json.JSONDecodeError:
        return None


async def fetch_stores(region: Region) -> StoreCatalog:
    """抓取门店列表；失败时返回空目录，由调用方决定是否回退。"""

    url = region.base_url + STORELIST_PATH
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
        ),
        "Accept-Language": region.accept_language,
    }
    # 部分环境（如 python.org 的 macOS 安装包）没有系统根证书，默认 SSL 上下文
    # 会直接失败。统一走 tls.ssl_context()，避免各模块各写一份又漏掉某处。
    try:
        async with aiohttp.ClientSession(
            timeout=TIMEOUT, connector=aiohttp.TCPConnector(ssl=ssl_context())
        ) as session, session.get(url, headers=headers) as resp:
            if resp.status != 200:
                log.warning("门店列表页返回 HTTP %s", resp.status)
                return StoreCatalog(stores=[], source="抓取失败")
            html = await resp.text()
    # asyncio.TimeoutError 在 3.10 上与内置 TimeoutError 不是同一个类，都要接
    except (TimeoutError, asyncio.TimeoutError, aiohttp.ClientError, OSError) as exc:
        log.warning("门店列表抓取失败：%s", exc)
        return StoreCatalog(stores=[], source="抓取失败")

    data = _extract_next_data(html)
    if data is None:
        log.warning("门店列表页结构变化，找不到 __NEXT_DATA__")
        return StoreCatalog(stores=[], source="结构变化")

    stores = parse_storelist(data)
    if not stores:
        return StoreCatalog(stores=[], source="无数据")
    return StoreCatalog(stores=stores, source="官网")
