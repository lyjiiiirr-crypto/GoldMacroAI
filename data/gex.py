"""GEX 地图与 Gamma 翻转点（Yahoo 免费期权链，本地近似估算）。

GEX = Γ × OI × 100 × S² × 0.01，看涨为正、看跌为负（标准 dealer-positioning 假设）。
Gamma flip 通过把现货价当变量、固定 IV/OI/到期时间重定价整条链，找净 GEX 过零点，
而不是按行权价简单累加。数据为延迟/近似值，并非真实做市商库存，仅供研究。
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone

from config import CBOE_OPTIONS_URL, GEX_GRID_STEPS, GEX_MAX_DAYS, GEX_SYMBOLS
from data.common import fetch_cboe_text

SOURCE_NAME = "Cboe 免费延迟期权报价（本地 GEX 估算）"
SYMBOL_LABELS = {
    "GLD": "GLD 黄金ETF代理",
    "SPY": "SPY 大盘风险偏好",
}
RISK_FREE_FALLBACK = 0.04
DISCLAIMER = (
    "GEX 基于 Cboe 免费延迟期权报价与本地 Black-Scholes 估算，"
    "未包含真实交易方向与做市商库存，非交易信号。"
)
CBOE_OPTION_RE = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def bs_gamma(spot: float, strike: float, years: float, iv: float,
             rate: float = 0.0) -> float:
    """欧式期权 gamma（不分看涨/看跌，二者 gamma 相同）。"""
    if spot <= 0 or strike <= 0 or years <= 0 or iv <= 0:
        return 0.0
    sqrt_t = math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * iv * iv) * years) / (iv * sqrt_t)
    return _norm_pdf(d1) / (spot * iv * sqrt_t)


def parse_option_chain(payload: dict, *, rate: float = 0.0,
                       now: datetime | None = None,
                       expiry_limit_days: float | None = None) -> tuple[float, list[dict]]:
    """解析 Yahoo 期权链，返回 (现货价, 期权序列列表)。"""
    try:
        result = payload["optionChain"]["result"][0]
        groups = result.get("options") or []
        spot = float(result["quote"]["regularMarketPrice"])
    except (KeyError, IndexError, TypeError, ValueError):
        return 0.0, []

    now = now or datetime.now(timezone.utc)
    series: list[dict] = []
    for group in groups:
        try:
            years = (float(group["expirationDate"]) - now.timestamp()) / (365.0 * 24 * 3600)
        except (KeyError, TypeError, ValueError):
            continue
        years = max(years, 1.0 / 365.0)
        if expiry_limit_days is not None and years * 365.0 > expiry_limit_days:
            continue
        for side, sign in (("calls", 1), ("puts", -1)):
            for row in group.get(side) or []:
                try:
                    strike = float(row["strike"])
                    oi = float(row["openInterest"])
                    iv = float(row["impliedVolatility"])
                except (KeyError, TypeError, ValueError):
                    continue
                # 过滤深度虚值附近的异常 IV / 零持仓，避免噪声主导 GEX
                if strike <= 0 or oi <= 0 or not 0.005 <= iv <= 5.0:
                    continue
                series.append({
                    "strike": strike, "oi": oi, "iv": iv,
                    "years": years, "sign": sign, "side": side,
                })
    return spot, series


def parse_cboe_chain(payload: dict, *, expiry_limit_days: float | None = None,
                     now: datetime | None = None) -> tuple[float, list[dict]]:
    """解析 Cboe 免费延迟期权报价，返回 (现货价, 期权序列列表)。

    Cboe 单份 JSON 即含整条链、open_interest、gamma 与 iv；期权代码格式为
    ROOT + YYMMDD + C/P + STRIKE×1000（前导零补足 8 位），如 SPY261123P00550000。
    """
    try:
        data = payload["data"]
        rows = data.get("options") or []
        spot = float(data.get("current_price") or data.get("close"))
    except (KeyError, TypeError, ValueError):
        return 0.0, []
    now = now or datetime.now(timezone.utc)
    expiry_limit_days = (
        GEX_MAX_DAYS if expiry_limit_days is None else expiry_limit_days
    )
    series: list[dict] = []
    for row in rows:
        match = CBOE_OPTION_RE.match(str(row.get("option") or ""))
        if not match:
            continue
        _root, yymmdd, cp, strike_raw = match.groups()
        try:
            strike = float(strike_raw) / 1000.0
            oi = float(row.get("open_interest") or 0)
            iv = float(row.get("iv") or 0)
            gamma = float(row.get("gamma") or 0)
            expiry = datetime.strptime(yymmdd, "%y%m%d").replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        years = (expiry - now).total_seconds() / (365.0 * 24 * 3600)
        if years <= 0:
            years = 1.0 / (365.0 * 24.0)  # 当日到期按 1 小时近似，避免除零
        if expiry_limit_days and years * 365.0 > expiry_limit_days:
            continue
        if strike <= 0 or oi <= 0:
            continue
        series.append({
            "strike": strike, "oi": oi, "iv": iv, "gamma": gamma,
            "years": years, "sign": 1 if cp == "C" else -1,
            "side": "calls" if cp == "C" else "puts",
        })
    return spot, series


def gex_per_series(item: dict, spot: float, rate: float) -> float:
    # 用 IV 本地定价以保证 gamma flip 重定价口径一致；IV 缺失时才用 Cboe gamma。
    iv = float(item.get("iv") or 0.0)
    gamma = bs_gamma(spot, item["strike"], item["years"], iv, rate) if iv > 0 else 0.0
    if gamma <= 0 and item.get("gamma"):
        gamma = float(item["gamma"])
    return item["sign"] * gamma * item["oi"] * 100.0 * spot ** 2 * 0.01


def net_gex_at_spot(series: list[dict], spot: float, rate: float) -> float:
    return sum(gex_per_series(item, spot, rate) for item in series)


def gamma_flip(series: list[dict], spot: float, rate: float,
               steps: int = GEX_GRID_STEPS) -> float | None:
    """在 [0.75S, 1.25S] 网格上重定价，取离现价最近的净 GEX 过零点。"""
    if not series or spot <= 0 or steps < 3:
        return None
    low, high = spot * 0.75, spot * 1.25
    xs = [low + (high - low) * i / (steps - 1) for i in range(steps)]
    ys = [net_gex_at_spot(series, x, rate) for x in xs]
    crossings: list[float] = []
    for i in range(len(xs) - 1):
        y0, y1 = ys[i], ys[i + 1]
        if abs(y0) < 1e-6:
            crossings.append(xs[i])
        elif y0 * y1 < 0:
            crossings.append(xs[i] + (xs[i + 1] - xs[i]) * (-y0) / (y1 - y0))
    return round(min(crossings, key=lambda x: abs(x - spot)), 2) if crossings else None


def gex_map(series: list[dict], spot: float, rate: float) -> list[dict]:
    """按行权价聚合的 GEX 地图。"""
    by_strike: dict[float, dict] = {}
    for item in series:
        value = gex_per_series(item, spot, rate)
        row = by_strike.setdefault(item["strike"], {
            "call_gex": 0.0, "put_gex": 0.0, "net_gex": 0.0,
        })
        row["call_gex" if item["sign"] > 0 else "put_gex"] += value
        row["net_gex"] += value
    result = []
    for strike, row in sorted(by_strike.items()):
        result.append({
            "strike": strike,
            "call_gex": round(row["call_gex"]),
            "put_gex": round(row["put_gex"]),
            "net_gex": round(row["net_gex"]),
        })
    return result


def _format_gex(value: float) -> str:
    """净 GEX 简写：十亿用 B，其余用 M。"""
    if abs(value) >= 1e9:
        return f"{value / 1e9:+.2f}B"
    return f"{value / 1e6:+.0f}M"


def hedging_pressure(net_gex: float, spot: float, flip: float | None) -> dict:
    """把净 GEX 符号与 flip 相对位置翻译成对冲压力方向。"""
    if net_gex > 0:
        regime = "正Gamma"
        pressure = "上涨中卖出对冲（压涨），下跌中买入对冲（托底）"
    elif net_gex < 0:
        regime = "负Gamma"
        pressure = "上涨中买入对冲（助推），下跌中卖出对冲（杀跌）"
    else:
        regime = "中性Gamma"
        pressure = "对冲压力接近平衡"

    flip_text = "无可用 flip"
    distance_pct = None
    if flip:
        distance_pct = round((spot / flip - 1.0) * 100.0, 2)
        flip_text = f"flip {flip:.2f}，现价{'高于' if spot > flip else '低于'} flip {abs(distance_pct):.2f}%"
    summary = f"净GEX {_format_gex(net_gex)}/1%，{flip_text}；{pressure}。"
    return {
        "regime": regime, "pressure": pressure,
        "distance_pct": distance_pct, "summary": summary,
    }


def build_snapshot(symbol: str, series: list[dict], spot: float, rate: float,
                   *, as_of: str | None = None,
                   steps: int = GEX_GRID_STEPS) -> dict:
    """由期权序列生成结构化 GEX 快照。"""
    if not series or spot <= 0:
        raise ValueError(f"{symbol} 期权链为空或现货价无效")
    current_net = net_gex_at_spot(series, spot, rate)
    flip = gamma_flip(series, spot, rate, steps=steps)
    mapping = gex_map(series, spot, rate)
    nearby = [row for row in mapping if spot * 0.8 <= row["strike"] <= spot * 1.2]
    call_wall = max(nearby or mapping, key=lambda r: r["call_gex"])["strike"]
    put_wall = min(nearby or mapping, key=lambda r: r["put_gex"])["strike"]
    pressure = hedging_pressure(current_net, spot, flip)
    return {
        "source": SOURCE_NAME,
        "symbol": symbol,
        "label": SYMBOL_LABELS.get(symbol, symbol),
        "as_of": as_of or datetime.now(timezone.utc).date().isoformat(),
        "spot": round(spot, 2),
        "net_gex": round(current_net),
        "gamma_flip": flip,
        "call_wall": call_wall,
        "put_wall": put_wall,
        "regime": pressure["regime"],
        "hedging_pressure": pressure["pressure"],
        "distance_pct": pressure["distance_pct"],
        "summary": pressure["summary"],
        "gex_map": [r for r in mapping
                    if spot * 0.85 <= r["strike"] <= spot * 1.15][:40],
        "disclaimer": DISCLAIMER,
    }


def _risk_free_rate(db) -> float:
    """优先用本地 DFF 作为无风险利率，缺失时用保守默认值。"""
    try:
        rows = db.observations("DFF", limit=1)
        if rows and rows[0].get("value") is not None:
            return float(rows[0]["value"]) / 100.0
    except Exception:  # noqa: BLE001 - 无风险利率失败不影响 GEX 估算
        pass
    return RISK_FREE_FALLBACK


def collect(db, symbols: list[str] | tuple[str, ...] | None = None,
            max_days: int | None = None) -> dict:
    """采集 GLD / SPY 期权链并写入 gex_snapshots（来源+标的+日期幂等）。

    Cboe 免费延迟接口一次返回整条期权链，单标的单次请求即可完成采集；
    单标的失败不影响另一标的。
    """
    targets = list(symbols or GEX_SYMBOLS)
    rate = _risk_free_rate(db)
    snapshots: list[dict] = []
    errors: list[str] = []

    for symbol in targets:
        try:
            text = fetch_cboe_text(CBOE_OPTIONS_URL.format(symbol=symbol))
            payload = json.loads(text)
            spot, series = parse_cboe_chain(
                payload, expiry_limit_days=max_days or GEX_MAX_DAYS
            )
            if spot <= 0 or not series:
                raise RuntimeError("期权链无有效现货价 / 期权序列")
            snap = build_snapshot(symbol, series, spot, rate)
            raw = json.dumps({
                "spot": spot, "rate": rate,
                "expiry_limit_days": max_days or GEX_MAX_DAYS,
                "series": series,
            }, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001 - 单标的失败不影响另一标的
            errors.append(f"{symbol}: {type(exc).__name__}: {exc}")
            continue

        db.upsert_gex_snapshot(
            source=snap["source"], symbol=symbol, date=snap["as_of"],
            spot=snap["spot"], net_gex=snap["net_gex"],
            gamma_flip=snap["gamma_flip"], call_wall=snap["call_wall"],
            put_wall=snap["put_wall"], regime=snap["regime"],
            summary=snap["summary"], payload=snap, raw_json=raw,
        )
        snapshots.append(snap)

    if not snapshots:
        return {
            "ok": False, "source": SOURCE_NAME, "reports": 0,
            "error": "GEX 采集失败：" + "；".join(errors[:2]),
        }
    latest = snapshots[0]
    return {
        "ok": True, "source": SOURCE_NAME, "reports": len(snapshots),
        "symbols": ",".join(s["symbol"] for s in snapshots),
        "errors": errors or None,
        "latest": {
            "date": latest["as_of"], "symbol": latest["symbol"],
            "regime": latest["regime"],
            "spot": latest["spot"], "net_gex": latest["net_gex"],
        },
    }
