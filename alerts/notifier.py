"""统一推送通道：Telegram 优先，未配置时可用免注册的 ntfy.sh。"""
from __future__ import annotations

import html
import json
import re
import urllib.request

from config import NTFY_BASE_URL, NTFY_TOPIC, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from alerts.telegram_bot import TelegramError, send_message, send_photo


def ntfy_publish(text: str, topic: str | None = None, title: str = "GoldMacroAI") -> bool:
    topic = topic or NTFY_TOPIC
    if not topic:
        return False
    url = f"{NTFY_BASE_URL}/{topic}"
    payload = json.dumps({"message": text[:4096], "title": title, "tags": ["chart_with_upwards_trend"]}).encode("utf-8")
    request = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return 200 <= response.status < 300


def channel_status() -> str:
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        return "telegram"
    if NTFY_TOPIC:
        return "ntfy"
    return "none"


def ntfy_publish_bytes(data: bytes, topic: str | None = None,
                       title: str = "GoldMacroAI",
                       content_type: str = "image/png") -> bool:
    """ntfy.sh 附件推送（PUT 二进制）；未配置 topic 时返回 False。"""
    topic = topic or NTFY_TOPIC
    if not topic:
        return False
    url = f"{NTFY_BASE_URL}/{topic}"
    request = urllib.request.Request(
        url, data=bytes(data), method="PUT",
        headers={"Content-Type": content_type, "X-Title": title[:250]},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return 200 <= response.status < 300


def notify_photo(photo: bytes, caption: str = "",
                 title: str = "GoldMacroAI") -> tuple[bool, str]:
    """推送 K 线截图；失败只记录日志，不影响文本报告。"""
    channel = channel_status()
    if channel == "telegram":
        try:
            return send_photo(photo, caption=caption or None), "telegram"
        except TelegramError as exc:
            print(f"[telegram] 图片推送失败: {exc}")
            return False, "telegram"
    if channel == "ntfy":
        try:
            return ntfy_publish_bytes(photo, title=title), "ntfy"
        except Exception as exc:  # noqa: BLE001
            print(f"[ntfy] 图片推送失败: {exc}")
            return False, "ntfy"
    return False, "none"


def notify(text: str, title: str = "GoldMacroAI") -> tuple[bool, str]:
    """返回 (是否成功, 使用通道)。两者都未配置时返回 (False, 'none')。"""
    channel = channel_status()
    if channel == "telegram":
        try:
            return send_message(text), "telegram"
        except TelegramError as exc:
            print(f"[telegram] {exc}")
            return False, "telegram"
    if channel == "ntfy":
        try:
            plain = html.unescape(re.sub(r"<[^>]+>", "", text))
            return ntfy_publish(plain, title=title), "ntfy"
        except Exception as exc:  # noqa: BLE001
            print(f"[ntfy] 推送失败: {exc}")
            return False, "ntfy"
    return False, "none"
