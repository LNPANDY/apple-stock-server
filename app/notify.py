"""通知渠道。

每个渠道都是 `async send(title, body) -> None`，失败抛异常由调用方记录。
新增渠道只要在 `build_channels` 里登记即可。
"""

from __future__ import annotations

import asyncio
import json
import logging
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any

import aiohttp

from .tls import ssl_context

log = logging.getLogger(__name__)

TIMEOUT = aiohttp.ClientTimeout(total=15)


def _connector() -> aiohttp.TCPConnector:
    """带正确 CA 的连接器。

    直接用默认连接器时，某些 Python 环境会因为缺根证书而报
    CERTIFICATE_VERIFY_FAILED——看起来像网络故障，实际是环境问题。
    """

    return aiohttp.TCPConnector(ssl=ssl_context())


class NotifyError(Exception):
    pass


class Channel:
    type = "base"

    def describe(self) -> str:
        return self.type

    async def send(self, title: str, body: str) -> None:
        raise NotImplementedError


class BarkChannel(Channel):
    """Bark（iOS 推送）。地址形如 https://api.day.app/<key>。"""

    type = "bark"

    def __init__(
        self,
        url: str,
        *,
        group: str = "Apple",
        level: str = "timeSensitive",
        sound: str = "",
        volume: int | None = None,
        call: bool = False,
        icon: str = "",
        click: str = "",
    ) -> None:
        self.url = url.rstrip("/")
        self.group = group
        self.level = level
        self.sound = sound
        self.volume = volume
        self.call = call
        self.icon = icon
        self.click = click

    def describe(self) -> str:
        key = self.url.rsplit("/", 1)[-1]
        masked = f"{key[:6]}…" if len(key) > 6 else key
        return f"Bark({masked})"

    async def send(self, title: str, body: str) -> None:
        payload: dict[str, Any] = {
            "title": title,
            "body": body,
            "group": self.group,
        }
        if self.level:
            payload["level"] = self.level
        # 以下都是可选项：留空就不发，保持与 Bark 默认行为一致。
        if self.sound:
            payload["sound"] = self.sound
        if self.volume is not None:
            payload["volume"] = int(self.volume)
        if self.call:
            payload["call"] = "1"
        if self.icon:
            payload["icon"] = self.icon
        if self.click:
            payload["url"] = self.click

        async with aiohttp.ClientSession(timeout=TIMEOUT, connector=_connector()) as session:
            async with session.post(self.url, json=payload) as resp:
                text = await resp.text()
                if resp.status >= 300:
                    raise NotifyError(f"Bark 返回 HTTP {resp.status}：{text[:200]}")
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    return
                if isinstance(parsed, dict) and parsed.get("code") not in (200, None):
                    raise NotifyError(f"Bark 返回 {parsed.get('code')}：{parsed.get('message')}")


class TelegramChannel(Channel):
    type = "telegram"

    def __init__(self, token: str, chat_id: str) -> None:
        self.token = token
        self.chat_id = chat_id

    def describe(self) -> str:
        return f"Telegram({self.chat_id})"

    async def send(self, title: str, body: str) -> None:
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        data = {
            "chat_id": self.chat_id,
            "text": f"*{title}*\n\n{body}",
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        async with aiohttp.ClientSession(timeout=TIMEOUT, connector=_connector()) as session:
            async with session.post(url, data=data) as resp:
                text = await resp.text()
                if resp.status >= 300:
                    raise NotifyError(f"Telegram 返回 HTTP {resp.status}：{text[:200]}")


class WebhookChannel(Channel):
    """通用 webhook（飞书/钉钉/企业微信/Slack 的入站机器人均可）。

    `template` 决定 JSON 形状，默认发 {"text": ...}。飞书与钉钉要求各自的
    字段名，用 `template` 覆盖即可。

    钉钉若开启「自定义关键词」安全设置，把关键词写进 `title` 里就能通过。
    钉钉还要求 `msgtype`，因此默认模板不能直接复用——用文档里的示例配置。
    """

    type = "webhook"

    def __init__(self, url: str, *, template: str = "text") -> None:
        self.url = url
        self.template = template

    def describe(self) -> str:
        return f"Webhook({self.template})"

    def _payload(self, title: str, body: str) -> dict[str, Any]:
        text = f"{title}\n{body}"
        if self.template == "feishu":
            return {"msg_type": "text", "content": {"text": text}}
        if self.template == "dingtalk":
            return {"msgtype": "text", "text": {"content": f"{title}\n{body}"}}
        if self.template == "wecom":
            return {"msgtype": "text", "text": {"content": f"{title}\n{body}"}}
        return {"text": text}

    async def send(self, title: str, body: str) -> None:
        async with aiohttp.ClientSession(timeout=TIMEOUT, connector=_connector()) as session:
            async with session.post(self.url, json=self._payload(title, body)) as resp:
                text = await resp.text()
                if resp.status >= 300:
                    raise NotifyError(f"Webhook 返回 HTTP {resp.status}：{text[:200]}")
                # 钉钉/飞书在 HTTP 200 里用业务码表示失败
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    return
                if isinstance(parsed, dict):
                    if parsed.get("errcode") not in (0, None):
                        raise NotifyError(f"Webhook 业务错误：{parsed.get('errmsg')}")
                    if parsed.get("code") not in (0, None) and parsed.get("StatusCode") is not None:
                        raise NotifyError(f"Webhook 业务错误：{parsed}")


class SmtpChannel(Channel):
    type = "smtp"

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        to: str,
        *,
        security: str = "ssl",
        from_addr: str = "",
    ) -> None:
        self.host = host
        self.port = int(port)
        self.username = username
        self.password = password
        self.to = to
        self.security = security.lower()
        self.from_addr = from_addr or username

    def describe(self) -> str:
        return f"SMTP({self.host}:{self.port})"

    async def send(self, title: str, body: str) -> None:
        message = EmailMessage()
        message["Subject"] = title
        message["From"] = self.from_addr
        message["To"] = self.to
        message.set_content(body)
        await asyncio.to_thread(self._send_blocking, message)

    def _send_blocking(self, message: EmailMessage) -> None:
        try:
            if self.security == "ssl":
                with smtplib.SMTP_SSL(
                    self.host, self.port, timeout=20, context=ssl.create_default_context()
                ) as client:
                    client.login(self.username, self.password)
                    client.send_message(message)
            else:
                with smtplib.SMTP(self.host, self.port, timeout=20) as client:
                    if self.security == "starttls":
                        client.starttls(context=ssl.create_default_context())
                    client.login(self.username, self.password)
                    client.send_message(message)
        except (OSError, smtplib.SMTPException) as exc:
            raise NotifyError(f"SMTP 发送失败：{exc}") from exc


class ConsoleChannel(Channel):
    """回退渠道：只写日志。保证「什么都不配」也会在 journalctl 里看到提醒。"""

    type = "console"

    def describe(self) -> str:
        return "Console(日志)"

    async def send(self, title: str, body: str) -> None:
        log.warning("【提醒】%s\n%s", title, body)


def build_channels(raw: tuple[Any, ...]) -> list[Channel]:
    channels: list[Channel] = []
    for item in raw:
        kind = item.type
        opts = dict(item.options)
        if kind in ("bark", "bark_encrypted"):
            volume_raw = opts.get("volume")
            volume: int | None
            try:
                volume = int(volume_raw) if volume_raw not in (None, "") else None
            except (TypeError, ValueError):
                volume = None
            channels.append(
                BarkChannel(
                    _require(opts, "url", kind),
                    group=str(opts.get("group", "Apple")),
                    level=str(opts.get("level", "timeSensitive")),
                    sound=str(opts.get("sound") or ""),
                    volume=volume,
                    call=bool(opts.get("call")),
                    icon=str(opts.get("icon") or ""),
                    click=str(opts.get("click") or ""),
                )
            )
        elif kind == "telegram":
            channels.append(
                TelegramChannel(
                    _require(opts, "token", kind), _require(opts, "chat_id", kind)
                )
            )
        elif kind == "webhook":
            channels.append(
                WebhookChannel(
                    _require(opts, "url", kind), template=str(opts.get("template", "text"))
                )
            )
        elif kind == "smtp":
            channels.append(
                SmtpChannel(
                    _require(opts, "host", kind),
                    opts.get("port", 465),
                    _require(opts, "username", kind),
                    _require(opts, "password", kind),
                    _require(opts, "to", kind),
                    security=str(opts.get("security", "ssl")),
                    from_addr=str(opts.get("from", "")),
                )
            )
        elif kind == "console":
            channels.append(ConsoleChannel())
        else:
            log.warning("忽略未知通知渠道：%s", kind)
    if not channels:
        channels.append(ConsoleChannel())
    return channels


def _require(opts: dict[str, Any], key: str, kind: str) -> str:
    value = opts.get(key)
    if value is None or str(value).strip() == "":
        raise NotifyError(f"通知渠道 {kind} 缺少必填项 {key}")
    return str(value)


class Notifier:
    """把一个提醒并发投递到所有渠道，任一失败不影响其他渠道。"""

    def __init__(self, channels: list[Channel]) -> None:
        self.channels = channels

    async def send(self, title: str, body: str) -> None:
        results = await asyncio.gather(
            *(channel.send(title, body) for channel in self.channels),
            return_exceptions=True,
        )
        for channel, result in zip(self.channels, results, strict=True):
            if isinstance(result, BaseException):
                log.error("通知渠道 %s 发送失败：%s", channel.describe(), result)
            else:
                log.info("通知已通过 %s 发送", channel.describe())

    def describe(self) -> str:
        return "、".join(channel.describe() for channel in self.channels)
