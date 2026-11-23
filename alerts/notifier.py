"""统一推送通道：Telegram 优先，未配置时可用免注册的 ntfy.sh。"""
from __future__ import annotations

import html
import json
import re
import urllib.request

from config import NTFY_BASE_URL, NTFY_TOPIC, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from alerts.telegram_bot import TelegramError, send_message


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
