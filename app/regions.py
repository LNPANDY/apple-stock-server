"""地区与接口地址。

只保留本项目实际需要的部分：大陆、香港、澳门、台湾、日本、新加坡、澳洲、
马来西亚。地址与原项目 `apw-core` 的实现保持一致，避免自行拼错路径。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Region:
    """一个 Apple 在线商店地区站点。"""

    key: str
    title: str
    base_url: str
    locale: str

    @property
    def pickup_url(self) -> str:
        return f"{self.base_url}/shop/retail/pickup-message"

    @property
    def session_page(self) -> str:
        """用于建立会话的页面。

        大陆站的送货接口依赖购买页建立的在线商店会话，只开首页会让
        `/shop/fulfillment-messages` 返回 541；取货接口用购买页同样有效，
        因此统一走购买页。
        """
        return f"{self.base_url}/shop/buy-iphone"

    @property
    def accept_language(self) -> str:
        return {
            "zh_CN": "zh-CN,zh;q=0.9",
            "zh_HK": "zh-HK,zh;q=0.9",
            "zh_TW": "zh-TW,zh;q=0.9",
            "ja_JP": "ja-JP,ja;q=0.9",
        }.get(self.locale, "en-US,en;q=0.9")

    @property
    def navigator_language(self) -> str:
        return {
            "zh_CN": "zh-CN",
            "zh_HK": "zh-HK",
            "zh_TW": "zh-TW",
            "ja_JP": "ja-JP",
        }.get(self.locale, "en-US")


REGIONS: dict[str, Region] = {
    r.key: r
    for r in (
        Region("cn", "中国大陆", "https://www.apple.com.cn", "zh_CN"),
        Region("hk", "香港", "https://www.apple.com/hk-zh", "zh_HK"),
        Region("mo", "澳门", "https://www.apple.com/mo-zh", "zh_MO"),
        Region("tw", "台湾", "https://www.apple.com/tw", "zh_TW"),
        Region("jp", "日本", "https://www.apple.com/jp", "ja_JP"),
        Region("sg", "新加坡", "https://www.apple.com/sg", "en_SG"),
        Region("au", "澳洲", "https://www.apple.com/au", "en_AU"),
        Region("my", "马来西亚", "https://www.apple.com/my", "en_MY"),
    )
}


def resolve_region(value: str) -> Region:
    """接受地区简称或 locale。"""

    key = value.strip().lower()
    if key in REGIONS:
        return REGIONS[key]
    for region in REGIONS.values():
        if region.locale.lower() == key:
            return region
    raise ValueError(
        f"未知地区 {value!r}，可用值：" + "、".join(f"{k}({v.title})" for k, v in REGIONS.items())
    )
