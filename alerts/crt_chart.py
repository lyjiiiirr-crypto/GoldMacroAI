"""本地生成「3 根 4H K 线」截图（纯标准库 PNG，无第三方依赖）。

用于交易执行提醒后的 Telegram sendPhoto；生成失败时调用方优雅降级，
不影响文本日报推送。图片仅为价格结构示意，非交易信号。
"""
from __future__ import annotations

import struct
import zlib
from datetime import datetime, timedelta, timezone

from config import CHINA_TIMEZONE_OFFSET_HOURS

# 5×7 点阵字体：只收录图表需要的字符，避免引入字体文件依赖
FONT = {
    " ": ["00000"] * 7,
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
    "3": ["11110", "00001", "00001", "01110", "00001", "00001", "11110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "10000", "11110", "00001", "00001", "11110"],
    "6": ["00110", "01000", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00001", "01110"],
    "A": ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
    "C": ["01111", "10000", "10000", "10000", "10000", "10000", "01111"],
    "D": ["11110", "10001", "10001", "10001", "10001", "10001", "11110"],
    "E": ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
    "F": ["11111", "10000", "10000", "11110", "10000", "10000", "10000"],
    "G": ["01111", "10000", "10000", "10111", "10001", "10001", "01110"],
    "H": ["10001", "10001", "10001", "11111", "10001", "10001", "10001"],
    "I": ["11111", "00100", "00100", "00100", "00100", "00100", "11111"],
    "L": ["10000", "10000", "10000", "10000", "10000", "10000", "11111"],
    "M": ["10001", "11011", "10101", "10101", "10001", "10001", "10001"],
    "N": ["10001", "11001", "10101", "10011", "10001", "10001", "10001"],
    "O": ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
    "P": ["11110", "10001", "10001", "11110", "10000", "10000", "10000"],
    "R": ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
    "S": ["01111", "10000", "10000", "01110", "00001", "00001", "11110"],
    "T": ["11111", "00100", "00100", "00100", "00100", "00100", "00100"],
    "U": ["10001", "10001", "10001", "10001", "10001", "10001", "01110"],
    "X": ["10001", "10001", "01010", "00100", "01010", "10001", "10001"],
    ".": ["00000", "00000", "00000", "00000", "00000", "01100", "01100"],
    ":": ["00000", "01100", "01100", "00000", "01100", "01100", "00000"],
    "-": ["00000", "00000", "00000", "11111", "00000", "00000", "00000"],
    "/": ["00001", "00010", "00010", "00100", "01000", "01000", "10000"],
    "+": ["00000", "00100", "00100", "11111", "00100", "00100", "00000"],
}

BG = (13, 17, 26)
GRID = (44, 54, 72)
TEXT = (198, 210, 226)
MUTED = (120, 134, 154)
UP = (42, 200, 126)
DOWN = (238, 82, 96)
RESIST = (86, 172, 255)
SUPPORT = (132, 156, 196)
TARGET = (58, 214, 142)
STOP = (236, 104, 96)


class Canvas:
    def __init__(self, width: int, height: int, color=BG):
        self.width, self.height = width, height
        self.buffer = bytearray(bytes(color) * (width * height))

    def px(self, x, y, color) -> None:
        x, y = int(x), int(y)
        if 0 <= x < self.width and 0 <= y < self.height:
            index = (y * self.width + x) * 3
            self.buffer[index:index + 3] = bytes(color)

    def rect(self, x0, y0, x1, y1, color) -> None:
        x0, x1 = sorted((int(x0), int(x1)))
        y0, y1 = sorted((int(y0), int(y1)))
        for y in range(max(0, y0), min(self.height, y1 + 1)):
            for x in range(max(0, x0), min(self.width, x1 + 1)):
                self.px(x, y, color)

    def hline(self, x0, x1, y, color, thickness=1) -> None:
        self.rect(x0, y, x1, y + thickness - 1, color)

    def dash_hline(self, x0, x1, y, color, dash=9, gap=7) -> None:
        x = int(x0)
        while x <= x1:
            self.rect(x, y, min(x + dash, x1), y, color)
            x += dash + gap

    def text(self, x, y, value, color=TEXT, scale=1) -> None:
        for char in str(value).upper():
            glyph = FONT.get(char, FONT[" "])
            for row, bits in enumerate(glyph):
                for column, bit in enumerate(bits):
                    if bit == "1":
                        self.rect(
                            x + column * scale, y + row * scale,
                            x + (column + 1) * scale - 1, y + (row + 1) * scale - 1,
                            color,
                        )
            x += 6 * scale

    def png(self) -> bytes:
        stride = self.width * 3
        scanlines = b"".join(
            b"\x00" + bytes(self.buffer[y * stride:(y + 1) * stride])
            for y in range(self.height)
        )

        def chunk(tag: bytes, data: bytes) -> bytes:
            return (struct.pack(">I", len(data)) + tag + data
                    + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", self.width, self.height,
                                         8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(scanlines, 6))
            + chunk(b"IEND", b"")
        )


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _beijing_hhmm(value) -> str:
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(
            timezone(timedelta(hours=CHINA_TIMEZONE_OFFSET_HOURS))
        ).strftime("%H:%M")
    except (TypeError, ValueError):
        return ""


def render_crt_png(candles: list[dict], *, range_high=None, range_low=None,
                   target=None, stop=None) -> bytes:
    """把 3 根 4H K 线渲染为 PNG；levels 为第 1 根区间与结构参考位。"""
    parsed = []
    for item in candles[:3]:
        open_, high, low, close = (
            _number(item.get("open")), _number(item.get("high")),
            _number(item.get("low")), _number(item.get("close")),
        )
        if None in (open_, high, low, close):
            raise ValueError("K 线缺少有效 OHLC")
        parsed.append({
            "open": open_, "high": max(high, open_, close),
            "low": min(low, open_, close), "close": close,
            "time": _beijing_hhmm(item.get("time") or item.get("as_of")),
        })
    if len(parsed) != 3:
        raise ValueError("需要 exactly 3 根 K 线")
    levels = [v for v in (range_high, range_low, target, stop) if _number(v) is not None]
    prices = ([c["high"] for c in parsed] + [c["low"] for c in parsed]
              + [_number(v) for v in levels])
    low_price, high_price = min(prices), max(prices)
    padding = max((high_price - low_price) * 0.12, 0.5)
    low_price -= padding
    high_price += padding

    width, height = 760, 440
    left, right, top, bottom = 66, 72, 66, 96
    plot_width, plot_height = width - left - right, height - top - bottom
    canvas = Canvas(width, height)

    def y_pos(price):
        return int(top + (high_price - float(price)) / (high_price - low_price) * plot_height)

    # 网格与价格刻度
    for step in range(5):
        price = low_price + (high_price - low_price) * step / 4.0
        y = y_pos(price)
        canvas.hline(left, width - right, y, GRID)
        canvas.text(6, y - 4, f"{price:.2f}", MUTED)

    # 第 1 根区间：阻力（高）与支撑（低）
    if _number(range_high) is not None:
        y = y_pos(range_high)
        canvas.dash_hline(left, width - right, y, RESIST)
        canvas.text(width - right + 8, y - 4, "R", RESIST)
    if _number(range_low) is not None:
        y = y_pos(range_low)
        canvas.dash_hline(left, width - right, y, SUPPORT)
        canvas.text(width - right + 8, y - 4, "S", SUPPORT)
    if _number(target) is not None:
        y = y_pos(target)
        canvas.dash_hline(left, width - right, y, TARGET, dash=5, gap=9)
        canvas.text(width - right + 8, y - 4, "T", TARGET)
    if _number(stop) is not None:
        y = y_pos(stop)
        canvas.dash_hline(left, width - right, y, STOP, dash=5, gap=9)
        canvas.text(width - right + 8, y - 4, "SL", STOP)

    # 三根 K 线
    slot = plot_width / 3.0
    body_width = 46
    for index, candle in enumerate(parsed):
        center = int(left + slot * (index + 0.5))
        color = UP if candle["close"] >= candle["open"] else DOWN
        canvas.rect(center - 1, y_pos(candle["high"]),
                    center + 1, y_pos(candle["low"]), color)
        top_y, bottom_y = y_pos(max(candle["open"], candle["close"])), y_pos(
            min(candle["open"], candle["close"]))
        canvas.rect(center - body_width // 2, top_y,
                    center + body_width // 2, max(bottom_y, top_y + 3), color)
        canvas.text(center - 10, top + plot_height + 10, f"P{index + 1}", TEXT)
        if candle["time"]:
            canvas.text(center - 14, top + plot_height + 26, candle["time"], MUTED)

    canvas.text(12, 12, "XAUUSD 4H CRT  P1 RANGE / P2 SWEEP / P3 EXPAND", TEXT)
    canvas.text(12, 30, "BJ CLOSE TIME  R=RESISTANCE S=SUPPORT T=TARGET SL=STOP", MUTED)
    footer = []
    if _number(target) is not None:
        footer.append(f"TARGET {float(target):.2f}")
    if _number(stop) is not None:
        footer.append(f"STOP {float(stop):.2f}")
    footer.append("PUBLIC DATA / NOT A TRADE SIGNAL")
    canvas.text(12, height - 22, "  ".join(footer), MUTED)
    return canvas.png()


def build_crt_chart(market: dict | None) -> bytes | None:
    """从 market['crt'][0] 构造截图；数据不足时返回 None。"""
    for row in ((market or {}).get("crt") or [])[:1]:
        data = row.get("payload") if isinstance(row.get("payload"), dict) else {}

        def pick(key, default=None):
            value = row.get(key)
            return value if value is not None else data.get(key, default)

        candles = pick("candles") or []
        if len(candles) < 3:
            return None
        return render_crt_png(
            candles[-3:],
            range_high=pick("range_high", pick("prev_high")),
            range_low=pick("range_low", pick("prev_low")),
            target=pick("target"), stop=pick("stop_ref"),
        )
    return None
