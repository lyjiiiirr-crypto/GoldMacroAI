"""Telegram Bot API：发送消息、读取更新、命令轮询（仅标准库）。"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Callable

from config import TELEGRAM_API_BASE, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID


class TelegramError(RuntimeError):
    pass


def api_url(method: str, token: str | None = None) -> str:
    return f"{TELEGRAM_API_BASE}/bot{token or TELEGRAM_BOT_TOKEN}/{method}"


def _request(method: str, data: dict | None = None, token: str | None = None, timeout: int = 20) -> dict:
    url = api_url(method, token)
    if data is None:
        request = urllib.request.Request(url, headers={"User-Agent": "GoldMacroAI/1.0"})
    else:
        request = urllib.request.Request(
            url, data=json.dumps(data).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "GoldMacroAI/1.0"},
            method="POST",
        )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except Exception as exc:  # noqa: BLE001
        raise TelegramError(f"Telegram {method} 请求失败: {exc}") from exc
    if not payload.get("ok"):
        raise TelegramError(f"Telegram {method} 返回错误: {payload.get('description', payload)}")
    return payload


def send_message(text: str, chat_id: str | None = None, parse_mode: str = "HTML",
                 token: str | None = None) -> bool:
    token = token or TELEGRAM_BOT_TOKEN
    chat_id = chat_id or TELEGRAM_CHAT_ID
    if not token or not chat_id:
        return False
    _request("sendMessage", {
        "chat_id": chat_id, "text": text[:4096], "parse_mode": parse_mode,
    }, token=token)
    return True


def get_updates(offset: int | None = None, timeout: int = 10, token: str | None = None) -> list[dict]:
    params: dict = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    url = api_url("getUpdates", token) + "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers={"User-Agent": "GoldMacroAI/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout + 10) as response:
            payload = json.load(response)
    except Exception as exc:  # noqa: BLE001
        raise TelegramError(f"getUpdates 失败: {exc}") from exc
    if not payload.get("ok"):
        raise TelegramError(f"getUpdates 返回错误: {payload.get('description', payload)}")
    return payload.get("result", [])


def poll_commands(handler: Callable[[str, dict], str], token: str | None = None,
                  poll_timeout: int = 10, max_iterations: int | None = None) -> None:
    """长轮询处理 /now /fed 等命令；handler 返回要发回的文本。"""
    token = token or TELEGRAM_BOT_TOKEN
    if not token:
        print("[telegram] 未配置 TELEGRAM_BOT_TOKEN，命令轮询跳过")
        return
    offset = None
    iteration = 0
    while max_iterations is None or iteration < max_iterations:
        iteration += 1
        try:
            updates = get_updates(offset=offset, timeout=poll_timeout, token=token)
        except TelegramError as exc:
            print(f"[telegram] {exc}")
            continue
        for update in updates:
            offset = update["update_id"] + 1
            message = update.get("message") or {}
            text = (message.get("text") or "").strip()
            if not text.startswith("/"):
                continue
            command = text.split()[0].split("@")[0].lower()
            try:
                reply = handler(command, message)
            except Exception as exc:  # noqa: BLE001
                reply = f"命令处理失败：{exc}"
            if reply:
                try:
                    send_message(reply, chat_id=str(message["chat"]["id"]), token=token)
                except TelegramError as exc:
                    print(f"[telegram] 回复失败: {exc}")


COMMANDS = {
    "/now": "当前黄金宏观方向",
    "/fed": "美联储利率预期",
    "/rates": "2Y、10Y、实际收益率",
    "/dxy": "美元指数分析",
    "/calendar": "未来24小时经济日历",
    "/cot": "最新黄金机构持仓",
    "/tech": "最新技术面（本地自算）",
    "/bias": "黄金综合多空评分",
    "/report": "生成完整宏观报告",
    "/help": "命令列表",
}
