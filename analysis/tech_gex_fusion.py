"""技术面 × GEX 融合方向。

技术面（SMA/RSI/MACD）提供基础方向，GEX / Gamma flip 提供对冲压力偏向，
并对技术方向做放大（负 Gamma）或削弱（正 Gamma）；最终给出偏多/偏空/中性。

该结果只写入报告，不改变宏观评分；规则未回测，非交易信号。
"""
from __future__ import annotations

from datetime import date

DISCLAIMER = (
    "技术面 × GEX 融合方向为未回测的初始规则，仅作研究背景，"
    "不参与宏观评分，非交易信号。"
)

# GLD 是黄金直接代理，权重高于作为大盘风险背景的 SPY
GEX_SYMBOL_WEIGHTS = {"GLD": 0.65, "SPY": 0.35}
BIAS_DIR = {"偏多": 1.0, "偏空": -1.0, "中性震荡": 0.0, "中性": 0.0}
GEX_BIAS_ICONS = {"偏多": "🟢", "偏空": "🔴", "中性": "⚪"}
TECH_SCORE_FULL = 4.0   # 技术面 score ±4 视为满信号（bias 阈值为 ±2）
GEX_DIST_FULL = 8.0     # flip 距离 ±8% 视为满信号
TECH_WEIGHT = 0.65      # 融合中技术面权重
GEX_WEIGHT = 0.35       # 融合中 GEX 权重


def _clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _payload(row: dict) -> dict:
    value = row.get("payload")
    return value if isinstance(value, dict) else {}


def technical_direction(reports: list[dict] | None) -> dict:
    """从最新技术报告提取方向分（-1 ~ +1）。"""
    reports = reports or []
    if not reports:
        return {"dir": 0.0, "bias": "无数据", "score": None, "label": "", "ok": False}
    row = reports[0]
    payload = _payload(row)
    raw_score = _number(
        payload.get("score") if payload.get("score") is not None else row.get("score")
    )
    bias = str(payload.get("bias") or row.get("bias") or "中性震荡")
    label = str(payload.get("label") or row.get("label") or row.get("symbol") or "")
    direction = (
        _clamp(raw_score / TECH_SCORE_FULL) if raw_score is not None
        else BIAS_DIR.get(bias, 0.0) * 0.6
    )
    return {
        "dir": round(direction, 3), "bias": bias, "score": raw_score,
        "label": label, "ok": True,
    }


def gex_direction(snapshots: list[dict] | None) -> dict:
    """从 GEX 快照提取方向分与趋势放大系数。"""
    latest: dict[str, dict] = {}
    for row in snapshots or []:
        symbol = str(row.get("symbol") or "").upper()
        if symbol and symbol not in latest:  # 输入按日期倒序，首次即最新
            latest[symbol] = row
    if not latest:
        return {"dir": 0.0, "factor": 1.0, "details": [], "ok": False}

    details: list[dict] = []
    weight_sum = dir_sum = factor_sum = 0.0
    for symbol, row in latest.items():
        payload = _payload(row)
        net = _number(
            row.get("net_gex") if row.get("net_gex") is not None else payload.get("net_gex")
        )
        dist = _number(
            row.get("distance_pct")
            if row.get("distance_pct") is not None else payload.get("distance_pct")
        )
        regime = str(row.get("regime") or payload.get("regime") or "")
        if net is None:
            regime_dir = -1 if "负" in regime else 1 if "正" in regime else 0
        else:
            regime_dir = -1 if net < 0 else 1 if net > 0 else 0
        strength = _clamp(abs(dist or 0.0) / GEX_DIST_FULL, 0.0, 1.0)

        # 现价低于 flip + 负 Gamma → 放大下行（偏空）；
        # 现价高于 flip + 正 Gamma → 托底支撑（温和偏多，因正 Gamma 同时压涨）。
        if regime_dir < 0 and (dist or 0.0) < 0:
            direction = -strength
        elif regime_dir > 0 and (dist or 0.0) > 0:
            direction = strength * 0.6
        else:
            direction = 0.0

        # 负 Gamma 放大趋势、正 Gamma 压制波动
        if regime_dir < 0:
            factor = 1.0 + 0.5 * strength
        elif regime_dir > 0:
            factor = 1.0 - 0.4 * strength
        else:
            factor = 1.0

        weight = GEX_SYMBOL_WEIGHTS.get(symbol, 0.5 / len(latest))
        dir_sum += direction * weight
        factor_sum += factor * weight
        weight_sum += weight
        details.append({
            "symbol": symbol,
            "label": str(payload.get("label") or symbol),
            "regime": regime or (
                "负Gamma" if regime_dir < 0 else "正Gamma" if regime_dir > 0 else "中性Gamma"
            ),
            "distance_pct": dist,
            "dir": round(direction, 3),
            "factor": round(factor, 3),
        })

    details.sort(key=lambda d: -GEX_SYMBOL_WEIGHTS.get(d["symbol"], 0))
    return {
        "dir": round(dir_sum / weight_sum, 3) if weight_sum else 0.0,
        "factor": round(factor_sum / weight_sum, 3) if weight_sum else 1.0,
        "details": details,
        "ok": True,
    }


def gex_bias(row: dict | None) -> dict:
    """给单条 GEX 快照标注多空偏向（仅作背景，不改变宏观评分）。

    规则与 gex_direction 保持一致：
    - 负 Gamma 且现价低于 flip → 放大下行，标 🔴偏空；
    - 正 Gamma 且现价高于 flip → 托底支撑，标 🟢偏多；
    - 其余（含数据缺失、flip 距离未知、方向冲突）→ ⚪中性。
    """
    row = row or {}
    payload = _payload(row)
    net = _number(
        row.get("net_gex") if row.get("net_gex") is not None else payload.get("net_gex")
    )
    dist = _number(
        row.get("distance_pct")
        if row.get("distance_pct") is not None else payload.get("distance_pct")
    )
    regime = str(row.get("regime") or payload.get("regime") or "")

    if net is None:
        regime_dir = -1 if "负" in regime else 1 if "正" in regime else 0
    else:
        regime_dir = -1 if net < 0 else 1 if net > 0 else 0
    regime_text = regime or (
        "负Gamma" if regime_dir < 0 else "正Gamma" if regime_dir > 0 else "中性Gamma"
    )

    if regime_dir == 0:
        return {
            "label": "中性", "icon": GEX_BIAS_ICONS["中性"], "dir": 0.0, "ok": False,
            "reason": "GEX 符号与 regime 均不可用，未判定偏向",
        }
    if dist is None:
        return {
            "label": "中性", "icon": GEX_BIAS_ICONS["中性"], "dir": 0.0, "ok": False,
            "reason": f"{regime_text}但 flip 距离未知，未判定偏向",
        }

    strength = _clamp(abs(dist) / GEX_DIST_FULL, 0.0, 1.0)
    if regime_dir < 0 and dist < 0:
        label, direction = "偏空", -strength
        reason = f"{regime_text}且现价低于 flip {abs(dist):.2f}%，对冲放大下行"
    elif regime_dir > 0 and dist > 0:
        label, direction = "偏多", strength * 0.6
        reason = f"{regime_text}且现价高于 flip {abs(dist):.2f}%，对冲托底偏多"
    else:
        label, direction = "中性", 0.0
        side = "高于" if dist > 0 else "低于" if dist < 0 else "接近"
        reason = f"{regime_text}但现价{side} flip {abs(dist):.2f}%，方向未确认"

    return {
        "label": label, "icon": GEX_BIAS_ICONS[label], "dir": round(direction, 3),
        "ok": True, "reason": reason,
    }


def _direction_label(score: float) -> str:
    if score >= 0.2:
        return "偏多"
    if score <= -0.2:
        return "偏空"
    return "中性震荡"


def _strength_label(score: float) -> str:
    value = abs(score)
    return "强" if value >= 0.6 else "中" if value >= 0.35 else "弱"


def fuse(technical: list[dict] | None, gex: list[dict] | None) -> dict:
    """融合技术面与 GEX，返回偏多 / 偏空 / 中性结论。"""
    tech = technical_direction(technical)
    g = gex_direction(gex)
    base = TECH_WEIGHT * tech["dir"] + GEX_WEIGHT * g["dir"]
    # GEX 放大系数只做温和调节：1.0× 不变，负 Gamma 满强度约 +12.5%，正 Gamma 约 -10%
    adjusted_factor = 0.75 + 0.25 * g["factor"]
    combined = _clamp(base * adjusted_factor)

    points: list[str] = []
    if tech["ok"]:
        points.append(f"技术面{tech['bias']}（{tech['dir']:+.2f}）")
    else:
        points.append("技术面暂无数据")
    if g["ok"]:
        for item in g["details"]:
            dist = item["distance_pct"]
            dist_text = (
                f"现价{'高于' if dist > 0 else '低于'} flip {abs(dist):.2f}%"
                if dist is not None else "flip 距离未知"
            )
            points.append(f"{item['symbol']} {item['regime']}（{dist_text}，方向 {item['dir']:+.2f}）")
        points.append(f"GEX 综合 {g['dir']:+.2f}，放大 {g['factor']:.2f}×")
    else:
        points.append("GEX 暂无数据")
    reason = "；".join(points) + f"。融合分 {combined:+.2f} → {_direction_label(combined)}。"

    return {
        "direction": _direction_label(combined),
        "score": round(combined, 3),
        "strength": _strength_label(combined),
        "technical": tech,
        "gex": g,
        "reason": reason,
        "as_of": date.today().isoformat(),
        "disclaimer": DISCLAIMER,
    }
