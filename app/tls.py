"""HTTPS 连接的公共配置。

为什么需要这个
--------------
部分 Python 环境（典型是 python.org 的 macOS 安装包、以及某些精简的 Linux
镜像）没有随附系统根证书。这时 `ssl.create_default_context()` 拿不到任何 CA，
所有 HTTPS 请求都会报：

    [SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer certificate

表现出来就是「门店列表抓不到」「Bark 推送失败」，但错误信息看起来像网络问题，
很容易误判。显式指向 certifi 的 CA 包可以解决，同时保持证书校验开启——
绝不能为了「能连上」而关掉验证。
"""

from __future__ import annotations

import ssl

_context: ssl.SSLContext | None = None


def ssl_context() -> ssl.SSLContext | bool:
    """返回带正确 CA 的 SSL 上下文。

    拿不到 certifi 时返回 True，交给 aiohttp 用默认上下文——那种环境下
    默认上下文本来就能用，缺 certifi 只是极少数情况。
    """

    global _context
    if _context is not None:
        return _context

    try:
        import certifi

        _context = ssl.create_default_context(cafile=certifi.where())
    except (ImportError, OSError):
        return True
    return _context
