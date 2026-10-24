"""通过真实 Chromium 会话查询 Apple 库存。

为什么不能裸 HTTP
-----------------
Apple 的商品页会先跑一段浏览器环境校验（`shop/shld/v2_1/verify.js`），只有
通过校验的会话去请求 `/shop/retail/pickup-message` 才会拿到 JSON。裸 requests
即便把 UA、Cookie、Referer 都补齐，也会收到自定义状态码 **541**——它不是
标准 HTTP 状态码，专门用来表示「这个请求不像来自浏览器」。

因此这里复用原项目的做法：启动一个持久化的 Chromium 会话，先正常打开地区
购买页完成校验，再**在页面上下文里**用 `fetch()` 请求接口。这样请求天然带着
同一套 cookie、同源 Referer、真实的 Client Hints 和 TLS 指纹。
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from playwright.async_api import BrowserContext, Page, async_playwright

from .regions import Region

log = logging.getLogger(__name__)

MAX_PARTS_PER_REQUEST = 20
MAX_RESPONSE_BYTES = 4 << 20
REQUEST_TIMEOUT_MS = 30_000
SESSION_READY_TIMEOUT_S = 45
MIN_REQUEST_GAP_S = 2.0

# 这些资源对「拿到库存 JSON」毫无用处，却占掉大部分内存与带宽。
# 拦掉它们之后，一个页面从几十 MB 降到几 MB，是 1 核 1G 机器能跑起来的关键。
BLOCKED_RESOURCE_TYPES = frozenset({"image", "media", "font", "stylesheet", "other"})
BLOCKED_URL_HINTS = (
    "rtlimages.apple.com",
    "store.storeimages.cdn-apple.com",
    "images.apple.com",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".avif",
    ".woff",
    ".woff2",
    ".ttf",
    "googletagmanager.com",
    "googlesyndication",
    "doubleclick.net",
    "google-analytics.com",
    "facebook.net",
    "cookielaw.org",
    "onetrust.com",
)


class FetchError(Exception):
    """一次查询失败。

    `blocked` 表示被 Apple 拒绝（541/403），`rate_limited` 表示 429 或 5xx。
    调用方据此决定退避，绝不能把这类失败当成「无货」。
    """

    def __init__(self, message: str, *, blocked: bool = False, rate_limited: bool = False):
        super().__init__(message)
        self.blocked = blocked
        self.rate_limited = rate_limited


# 常见浏览器可执行文件，按优先级排列。真实 Chrome 的 Client Hints 里会出现
# "Google Chrome" 品牌，比 Chromium 更像一台普通电脑。
CHROME_CANDIDATES = (
    "google-chrome-stable",
    "google-chrome",
    "chromium",
    "chromium-browser",
    "microsoft-edge",
)

# PATH 之外的位置：Chrome 的 .deb 装到 /opt，Snap 装到 /snap；macOS 放在 .app 里。
CHROME_ABSOLUTE_PATHS = (
    "/opt/google/chrome/chrome",
    "/usr/lib/chromium/chromium",
    "/usr/bin/chromium",
    "/snap/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)


def find_browser() -> tuple[str, str] | None:
    """找到可用的浏览器，返回 (可执行文件路径, 版本号)。

    优先真实 Chrome/Edge：它们的 Client Hints 与 UA 更符合普通用户环境。
    找不到时退回 Playwright 自带的 Chromium。
    """

    for name in CHROME_CANDIDATES:
        path = shutil.which(name)
        if not path:
            continue
        version = _probe_version(path)
        if version:
            return path, version

    for candidate in CHROME_ABSOLUTE_PATHS:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            version = _probe_version(candidate)
            if version:
                return candidate, version

    # Playwright 自带的 Chromium。这是 VPS 上最常见的方案——不必装系统浏览器。
    # 缓存位置随平台变化，且新版目录名从 chrome-linux 变成了 chrome-linux64，
    # 所以用通配符覆盖几种已知布局。
    roots: list[Path] = []
    env_root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
    if env_root:
        roots.append(Path(env_root))
    roots.append(Path.home() / ".cache" / "ms-playwright")
    roots.append(Path.home() / "Library" / "Caches" / "ms-playwright")

    patterns = (
        "chromium-*/chrome-linux64/chrome",
        "chromium-*/chrome-linux/chrome",
        "chromium-*/chrome-mac*/*.app/Contents/MacOS/*",
    )
    for root in roots:
        if not root.is_dir():
            continue
        for pattern in patterns:
            for candidate in sorted(root.glob(pattern), reverse=True):
                version = _probe_version(str(candidate))
                if version:
                    return str(candidate), version
    return None


def _probe_version(executable: str) -> str:
    try:
        out = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""
    match = re.search(r"(\d+\.\d+\.\d+\.\d+)", out)
    return match.group(1) if match else ""


@dataclass(frozen=True)
class PartStatus:
    part: str
    available: bool
    title: str = ""
    quote: str = ""
    raw: str = ""


@dataclass(frozen=True)
class StoreAvailability:
    store_number: str
    store_name: str
    city: str
    parts: dict[str, PartStatus] = field(default_factory=dict)


def user_agent_for(version: str) -> str:
    """生成与浏览器版本一致的 UA 字符串。

    关键点是不能出现 `HeadlessChrome`：无头模式默认会带这个词，是最明显的
    自动化特征。版本号必须与真实二进制一致，否则 UA 会与浏览器自己发出的
    Client Hints 对不上，反而成了更强的破绽。
    """

    major = version.split(".")[0]
    return (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
    )


# --------------------------------------------------------------------------
# 在页面里执行的取货请求
# --------------------------------------------------------------------------

_PICKUP_JS = """
async (args) => {
  try {
    const url = new URL(args.url);
    for (const [key, value] of args.pairs) url.searchParams.append(key, value);
    const response = await fetch(url.toString(), {
      credentials: 'same-origin',
      headers: {
        'Accept': 'application/json, text/javascript, */*; q=0.01',
        'X-Requested-With': 'XMLHttpRequest'
      }
    });
    const body = await response.text();
    const bytes = new TextEncoder().encode(body).length;
    if (bytes > args.maxBytes) {
      return {status: 0, body: 'response too large: ' + bytes};
    }
    return {status: response.status, body: body};
  } catch (error) {
    const detail = error instanceof Error ? error.name + ': ' + error.message : String(error);
    return {status: 0, body: '__FETCH_FAILED__:' + detail};
  }
}
"""


def _proxy_settings(proxy: str | None, bypass: str | None = None) -> dict[str, str] | None:
    """把代理 URL 转成 Playwright 的 proxy 配置。

    支持 `http://`、`https://`、`socks5://` 和带认证的
    `socks5://user:pass@host:port`。Playwright 要求认证信息单独给字段，
    不能留在 URL 里。

    SOCKS5 的一个关键性质：Chrome 会把**域名**交给代理解析（实测确认，
    发的是 ATYP=3 而不是解析后的 IP）。所以在家宽那端跑代理时，DNS 也在
    家宽解析，VPS 不会泄漏 DNS 查询。
    """

    if not proxy:
        return None
    raw = proxy.strip()
    if "://" not in raw:
        raw = "http://" + raw
    parsed = urlparse(raw)
    if not parsed.hostname:
        raise FetchError(f"代理地址无法解析：{proxy}")

    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https", "socks5", "socks4"):
        raise FetchError(f"不支持的代理协议 {scheme}，可用 http / socks5")

    server = f"{scheme}://{parsed.hostname}"
    if parsed.port:
        server += f":{parsed.port}"

    settings: dict[str, str] = {"server": server}
    if parsed.username:
        settings["username"] = unquote(parsed.username)
    if parsed.password:
        settings["password"] = unquote(parsed.password)
    if bypass:
        settings["bypass"] = bypass
    return settings


class AppleBrowser:
    """一个复用的 Chromium 会话，按地区懒加载。"""

    def __init__(
        self,
        region: Region,
        profile_dir: Path,
        *,
        headless: bool = True,
        no_sandbox: bool = False,
        proxy: str | None = None,
        proxy_bypass: str | None = None,
    ) -> None:
        self.region = region
        self.profile_dir = Path(profile_dir)
        self.headless = headless
        self.no_sandbox = no_sandbox
        self.proxy = proxy
        self.proxy_bypass = proxy_bypass
        self._pw = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._ready = False
        self._lock = asyncio.Lock()
        self._last_request_at = 0.0
        self.user_agent = ""

    async def start(self) -> None:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        found = find_browser()
        if not found:
            raise FetchError(
                "找不到 Chrome/Chromium。Ubuntu 上请先安装：\n"
                "  sudo apt install -y chromium-browser\n"
                "或安装 Google Chrome 后重试。"
            )
        executable, version = found
        self.user_agent = user_agent_for(version)
        log.info("使用浏览器：%s（版本 %s）", executable, version)

        self._pw = await async_playwright().start()
        args = [
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-sync",
            "--disable-default-apps",
            f"--user-agent={self.user_agent}",
            # 低内存环境的关键参数：限制渲染进程与 JS 堆，避免 1G 内存被吃满。
            "--js-flags=--max-old-space-size=128",
            "--renderer-process-limit=1",
            "--disable-gpu",
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--mute-audio",
        ]
        if self.no_sandbox:
            args.extend(["--no-sandbox", "--disable-dev-shm-usage"])

        self._context = await self._launch_context(executable, args)
        pages = self._context.pages
        self._page = pages[0] if pages else await self._context.new_page()
        self._page.set_default_timeout(REQUEST_TIMEOUT_MS)
        await self._page.route("**/*", self._block_heavy_resources)
        log.info("浏览器会话已启动，资料目录：%s", self.profile_dir)

    async def _launch_context(self, executable: str, args: list[str]) -> BrowserContext:
        """启动浏览器，并把「资料目录被占用」翻译成能看懂的提示。

        Chromium 用 profile 目录里的 SingletonLock 保证单实例。同一目录已有
        进程在跑时，Playwright 只会抛一句 ProcessSingleton 的英文原文，用户
        无从判断是自己多开了一个实例、还是上次崩溃留下了残留。
        """

        try:
            return await self._pw.chromium.launch_persistent_context(  # type: ignore[union-attr]
                str(self.profile_dir),
                executable_path=executable,
                headless=self.headless,
                args=args,
                viewport={"width": 1440, "height": 900},
                device_scale_factor=1,
                locale=self.region.navigator_language,
                timezone_id="Asia/Shanghai",
                proxy=_proxy_settings(self.proxy, self.proxy_bypass),
                ignore_default_args=["--enable-automation"],
                java_script_enabled=True,
            )
        except Exception as exc:
            message = str(exc)
            if "ProcessSingleton" in message or "SingletonLock" in message:
                raise FetchError(
                    f"浏览器资料目录被占用：{self.profile_dir}\n"
                    "这通常意味着已经有一个实例在跑（同一个目录不能同时开两个）。\n"
                    "请先停掉另一个实例：\n"
                    "  pgrep -fl 'apple-stock|browser-profile'\n"
                    "若确认没有实例在运行，说明上次异常退出留下了残留锁，删除后重试：\n"
                    f"  rm -f {self.profile_dir}/Singleton*"
                ) from exc
            raise

    @staticmethod
    async def _block_heavy_resources(route: Any) -> None:
        """拦掉图片/字体/统计脚本。

        页面校验只依赖 JS，不依赖这些资源。拦掉它们既省内存也省流量，但**绝不能**
        拦 XHR/fetch、document 或 script——那会让校验失败并直接触发 541。
        """

        request = route.request
        # 样式表是例外里的例外：少数校验脚本会读 computed style，留着更安全。
        if request.resource_type in BLOCKED_RESOURCE_TYPES and request.resource_type != "stylesheet":
            await route.abort()
            return
        url = request.url.lower()
        if any(hint in url for hint in BLOCKED_URL_HINTS):
            await route.abort()
            return
        await route.continue_()

    async def close(self) -> None:
        try:
            if self._context:
                await self._context.close()
        finally:
            if self._pw:
                await self._pw.stop()
            self._context = None
            self._page = None
            self._pw = None

    async def _ensure_session(self) -> None:
        """打开地区页面，等它完成加载与浏览器校验。"""

        assert self._page is not None
        if self._ready:
            return
        url = self.region.session_page
        log.debug("建立会话：%s", url)
        response = await self._page.goto(url, wait_until="domcontentloaded", timeout=REQUEST_TIMEOUT_MS)
        if response is not None and response.status >= 400:
            raise FetchError(f"打开 {url} 返回 HTTP {response.status}")
        deadline = asyncio.get_running_loop().time() + SESSION_READY_TIMEOUT_S
        while True:
            state = await self._page.evaluate("document.readyState")
            if state == "complete":
                break
            if asyncio.get_running_loop().time() > deadline:
                raise FetchError("Apple 地区页面在限定时间内未加载完成")
            await asyncio.sleep(0.25)
        # 页面里的校验脚本需要一点时间跑完；给它一个短暂窗口，而不是立刻开打。
        await asyncio.sleep(1.2)
        self._ready = True

    def _invalidate_session(self) -> None:
        """被拒绝后丢弃会话标记，下一轮重新建立。"""

        self._ready = False

    async def _throttle(self) -> None:
        loop = asyncio.get_running_loop()
        wait = self._last_request_at + MIN_REQUEST_GAP_S - loop.time()
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_request_at = asyncio.get_running_loop().time()

    async def query(
        self,
        *,
        parts: Iterable[str],
        location: str | None = None,
        store: str | None = None,
    ) -> list[StoreAvailability]:
        """查询一批零件号在目标范围内的可取货状态。

        `location` 与 `store` 二选一。前者一次返回该地点周边的全部门店，是
        压降请求量的关键；后者只返回指定门店。
        """

        parts = list(parts)
        if not parts:
            raise FetchError("零件号列表为空")
        if len(parts) > MAX_PARTS_PER_REQUEST:
            raise FetchError(f"单次最多 {MAX_PARTS_PER_REQUEST} 个零件号，收到 {len(parts)}")
        if (location is None) == (store is None):
            raise FetchError("location 与 store 必须且只能给一个")

        async with self._lock:
            if self._page is None:
                raise FetchError("浏览器会话未启动")
            await self._ensure_session()
            await self._throttle()

            pairs: list[list[str]] = [["pl", "true"], ["mts.0", "regular"]]
            if location:
                pairs.append(["location", location])
            else:
                pairs.append(["store", store or ""])
            for index, part in enumerate(parts):
                pairs.append([f"parts.{index}", part])

            try:
                payload = await self._page.evaluate(
                    _PICKUP_JS,
                    {
                        "url": self.region.pickup_url,
                        "pairs": pairs,
                        "maxBytes": MAX_RESPONSE_BYTES,
                    },
                )
            except Exception as exc:
                self._invalidate_session()
                raise FetchError(f"页面内请求失败：{exc}") from exc

            status = int(payload.get("status", 0))
            body = payload.get("body") or ""

            if status in (403, 541):
                self._invalidate_session()
                raise FetchError(f"请求被 Apple 拦截（HTTP {status}）", blocked=True)
            if status == 429:
                raise FetchError("请求被限流（HTTP 429）", rate_limited=True)
            if status >= 500:
                raise FetchError(f"Apple 服务端错误（HTTP {status}）", rate_limited=True)
            if status == 0:
                raise FetchError(f"页面内请求未完成：{body[:200]}")
            if status != 200:
                raise FetchError(f"未预期的 HTTP {status}")
            if body.startswith("__FETCH_FAILED__"):
                self._invalidate_session()
                raise FetchError(f"页面内请求失败：{body[16:][:200]}")

            return parse_pickup_response(body, expected_parts=parts)

    async def probe_egress(self) -> dict[str, Any]:
        """查当前会话的出口 IP 与归属，用于确认代理是否生效。

        走的是第三方回显服务，不消耗 Apple 的请求额度。
        """

        if self._page is None:
            raise FetchError("浏览器会话未启动")
        await self._throttle()
        try:
            result = await self._page.evaluate(
                """async () => {
                    const r = await fetch('https://ipinfo.io/json', {cache: 'no-store'});
                    if (!r.ok) return {error: 'HTTP ' + r.status};
                    return await r.json();
                }"""
            )
        except Exception as exc:
            raise FetchError(f"出口探测请求失败：{exc}") from exc
        if not isinstance(result, dict) or result.get("error"):
            raise FetchError(f"出口探测未返回有效结果：{result}")
        return result

    async def discover_products(self, spec: Any) -> dict[str, str]:
        """从官网购买页读取型号零件号，返回 {零件号: 显示名}。

        购买页会内嵌一段 `productSelectionData`，里面有全部 SKU 的零件号以及
        颜色/容量等维度的展示名。相比把型号写死在配置里，这样新型号上市当天
        就能盯上。

        `spec.family` 为空时，先从品类入口页列出该品类下所有在售机型。
        """

        async with self._lock:
            if self._page is None:
                raise FetchError("浏览器会话未启动")
            await self._ensure_session()

            slugs = [spec.family] if spec.family else await self._list_families(spec.category)
            if not slugs:
                raise FetchError(f"在 {spec.category} 分类下没有找到任何机型")

            found: dict[str, str] = {}
            for slug in slugs:
                # 购买页路径带 `buy-` 前缀，品类入口页不带。这是两套不同的 URL。
                url = f"{self.region.base_url}/shop/{_buy_prefix(spec.category)}/{slug}"
                await self._throttle()
                try:
                    await self._page.goto(
                        url, wait_until="domcontentloaded", timeout=REQUEST_TIMEOUT_MS
                    )
                except Exception as exc:
                    log.warning("打开购买页失败 %s：%s", url, exc)
                    continue
                content = await self._page.content()
                raw = _extract_json_object(content, "productSelectionData")
                if raw is None:
                    log.warning("购买页没有 productSelectionData：%s", url)
                    continue
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError as exc:
                    log.warning("productSelectionData 解析失败 %s：%s", url, exc)
                    continue
                found.update(_products_from_selection(data, spec))
            if not found:
                raise FetchError("购买页里没有解析出任何型号")
            return found

    async def _list_families(self, category: str) -> list[str]:
        assert self._page is not None
        # 品类入口页是 /shop/iphone（不带 buy-），购买页才是 /shop/buy-iphone/<slug>。
        url = f"{self.region.base_url}/shop/{category}"
        await self._throttle()
        await self._page.goto(url, wait_until="domcontentloaded", timeout=REQUEST_TIMEOUT_MS)
        hrefs = await self._page.eval_on_selector_all(
            "a[href]", "els => els.map(e => e.getAttribute('href') || '')"
        )
        prefixes = (f"/shop/{_buy_prefix(category)}/", f"/shop/{category}/")
        slugs: list[str] = []
        for href in hrefs:
            for prefix in prefixes:
                if prefix not in href:
                    continue
                slug = href.split(prefix, 1)[1].split("?")[0].split("#")[0].strip("/")
                # 只要机型页，跳过 <slug>/<partNumber> 这类 SKU 链接。
                if slug and "/" not in slug and slug not in slugs:
                    slugs.append(slug)
        log.debug("%s 分类下发现机型：%s", category, slugs)
        return slugs


def parse_pickup_response(body: str, *, expected_parts: Iterable[str]) -> list[StoreAvailability]:
    """解析取货接口响应。

    只声明用得到的字段。任何结构异常都抛错，绝不降级成「无货」——把「查不到」
    当成「没货」正是这类工具最致命的缺陷。
    """

    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise FetchError(f"响应不是 JSON：{exc}") from exc

    if not isinstance(data, dict):
        raise FetchError("响应顶层不是对象")

    head_status = (data.get("head") or {}).get("status")
    if head_status is not None and str(head_status) not in ("200",):
        raise FetchError(f"接口返回 head.status={head_status}")

    inner = data.get("body") or {}
    error_message = (inner.get("errorMessage") or "").strip()
    if error_message:
        lowered = error_message.lower()
        if "no store associated with this search" in lowered or "没有与此搜索相关" in error_message:
            raise FetchError(f"Apple 未把该地点/门店接入在线取货：{error_message}")
        raise FetchError(f"Apple 返回错误：{error_message}")

    stores = inner.get("stores")
    if not stores:
        content = inner.get("content") or {}
        pickup_message = content.get("pickupMessage") or {}
        stores = pickup_message.get("stores")
    if not stores:
        raise FetchError("响应里没有门店数据（接口结构可能已变化）")

    expected = list(expected_parts)
    out: list[StoreAvailability] = []
    for raw_store in stores:
        if not isinstance(raw_store, dict):
            continue
        store_number = str(raw_store.get("storeNumber") or "").strip()
        if not store_number:
            continue
        availability = raw_store.get("partsAvailability") or {}
        parts: dict[str, PartStatus] = {}
        for part, info in availability.items():
            if not isinstance(info, dict):
                continue
            display = str(info.get("pickupDisplay") or "").strip().lower()
            message_types = info.get("messageTypes") or {}
            regular = message_types.get("regular") or {}
            parts[part] = PartStatus(
                part=part,
                available=(display == "available"),
                title=str(regular.get("storePickupProductTitle") or ""),
                quote=str(regular.get("storePickupQuote") or ""),
                raw=display,
            )
        out.append(
            StoreAvailability(
                store_number=store_number,
                store_name=str(raw_store.get("storeName") or store_number),
                city=str(raw_store.get("city") or ""),
                parts=parts,
            )
        )

    if not out:
        raise FetchError("响应里的门店条目为空")
    # 至少要覆盖到一个请求的零件号，否则说明查询范围没有命中这些型号
    covered = {p for store in out for p in store.parts}
    if not covered & set(expected):
        raise FetchError("响应未覆盖任何请求的零件号")
    return out


# --------------------------------------------------------------------------
# 购买页解析
# --------------------------------------------------------------------------


def _buy_prefix(category: str) -> str:
    """品类名 → 购买页路径前缀。

    购买页是 `/shop/buy-iphone/<slug>`，品类入口页是 `/shop/iphone`，
    两者前缀不同。混用会拿到一个 404 页面而不是报错，于是静默解析失败。
    """

    return f"buy-{category.strip().lower()}"


def _extract_json_object(text: str, marker: str) -> str | None:
    """从页面源码里截出 `marker` 后面的那个 JSON 对象。

    不能用正则：JSON 里有嵌套花括号和字符串里的花括号，必须做括号配对。
    """

    index = text.find(marker)
    if index < 0:
        return None
    start = text.find("{", index)
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for position in range(start, len(text)):
        char = text[position]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : position + 1]
    return None


_TAG_RE = re.compile(r"<[^>]+>")


def _clean_display(value: Any) -> str:
    """把购买页里的展示值洗成一行纯文本。

    原始值是带 HTML 的片段，例如 `6.1 英寸显示屏<as-footnote>…</as-footnote>`。
    脚注并不携带型号信息，洗掉之后再压平空白。
    """

    if isinstance(value, dict):
        value = value.get("value") or ""
    text = _TAG_RE.sub(" ", str(value))
    text = html.unescape(text)
    text = re.sub(r"脚注\s*\d*", "", text)
    return " ".join(text.split())


def _products_from_selection(data: dict, spec: Any) -> dict[str, str]:
    products = data.get("products")
    if not isinstance(products, list):
        return {}
    display_values = data.get("displayValues") or {}

    def name_of(dimension: str, key: Any) -> str:
        if not key:
            return ""
        table = display_values.get(dimension) or {}
        return _clean_display(table.get(key, key))

    def matches(value: str, allowed: tuple[str, ...]) -> bool:
        """空筛选表示不限制；否则任一关键词命中即算匹配。"""
        return not allowed or any(token.lower() in value.lower() for token in allowed)

    out: dict[str, str] = {}
    for item in products:
        if not isinstance(item, dict):
            continue
        part = str(item.get("partNumber") or "").strip()
        if not part:
            continue
        screen = name_of("dimensionScreensize", item.get("dimensionScreensize"))
        color = name_of("dimensionColor", item.get("dimensionColor"))
        capacity = name_of("dimensionCapacity", item.get("dimensionCapacity"))
        if not matches(color, spec.color):
            continue
        if not matches(capacity, spec.capacity):
            continue
        if not matches(screen, spec.screen):
            continue
        label = " ".join(piece for piece in (screen, color, capacity) if piece)
        out[part] = label or part
    return out
