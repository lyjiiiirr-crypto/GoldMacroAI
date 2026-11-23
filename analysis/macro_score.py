"""100 分制宏观评分引擎：Python 规则打分，AI 只负责解释。

每个模块输出 -1 ~ +1：+1 偏多黄金，-1 偏空黄金。
总分 = Σ(权重 × 信号)，权重之和为 100。
这是一套未经回测的初始规则，不构成交易信号。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from config import FULL_SCALE, GRADES, WEIGHTS


@dataclass
class Signal:
    module: str
    name: str
    signal: float
    weight: int
    contribution: float
    detail: str
    fresh: bool = True


@dataclass
class ScoreResult:
    total: float
    bias: str
    confidence: float
    signals: list[Signal] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    sufficient: bool = False

    def as_dict(self) -> dict:
        return {
            "total": round(self.total, 1),
            "bias": self.bias,
            "confidence": round(self.confidence, 2),
            "sufficient": self.sufficient,
            "warnings": self.warnings,
            "signals": [
                {
                    "module": s.module, "name": s.name,
                    "signal": round(s.signal, 2), "weight": s.weight,
                    "contribution": round(s.contribution, 1),
                    "detail": s.detail, "fresh": s.fresh,
                }
                for s in self.signals
            ],
        }


def clamp(value: float) -> float:
    return max(-1.0, min(1.0, value))


def classify(total: float) -> str:
    for threshold, label in GRADES:
        if total >= threshold:
            return label
    return "强烈偏空"


def _add(signals: list[Signal], module: str, name: str, raw: float | None,
         detail: str, fresh: bool = True) -> None:
    signal = clamp(raw) if (raw is not None and fresh) else 0.0
    weight = WEIGHTS[module]
    signals.append(Signal(
        module=module, name=name, signal=signal, weight=weight,
        contribution=round(weight * signal, 1), detail=detail, fresh=bool(fresh),
    ))


def compute_score(inputs: dict | None = None) -> ScoreResult:
    """根据结构化输入计算评分。

    inputs 示例：
    {
      "fed_policy": {"change_pp": 8, "as_of": "2026-11-20", "stale": False},
      "real_rate":  {"delta_bp": -12, "as_of": "...", "stale": False},
      "dollar":     {"pct": -0.8, "as_of": "...", "stale": False},
      "econ_data":  [{"name": "CPI", "surprise": -0.1, "scale": 0.3, "as_of": "...", "stale": False}],
      "flows":      [{"name": "COT", "change": 12000, "scale": 30000, "as_of": "...", "stale": False}],
    }
    """
    inputs = inputs or {}
    signals: list[Signal] = []
    warnings: list[str] = []

    fed = inputs.get("fed_policy")
    if fed:
        stale = bool(fed.get("stale"))
        dovish = fed.get("dovish_pp")
        value = float(dovish) if dovish is not None else float(fed.get("change_pp", 0.0))
        label = "鹰鸽净变化" if dovish is not None else "降息概率变化"
        raw = value / FULL_SCALE["fed_policy_pp"]
        detail = f"{label} {value:+.1f} 个百分点"
        if stale:
            warnings.append("FedWatch 快照已过期")
        _add(signals, "fed_policy", "美联储政策预期", raw, detail, not stale)
    else:
        _add(signals, "fed_policy", "美联储政策预期", 0, "无 FedWatch 快照", False)

    rate = inputs.get("real_rate")
    if rate:
        stale = bool(rate.get("stale"))
        delta_bp = float(rate.get("delta_bp", 0.0))
        raw = -delta_bp / FULL_SCALE["real_rate_bp"]  # 实际收益率下降利好黄金
        detail = f"10Y实际收益率变化 {delta_bp:+.1f} bp"
        if stale:
            warnings.append("实际收益率数据已过期")
        _add(signals, "real_rate", "实际利率", raw, detail, not stale)
    else:
        _add(signals, "real_rate", "实际利率", 0, "无 DFII10 数据", False)

    dollar = inputs.get("dollar")
    if dollar:
        stale = bool(dollar.get("stale"))
        pct = float(dollar.get("pct", 0.0))
        raw = -pct / FULL_SCALE["dollar_pct"]  # 美元走弱利好黄金
        detail = f"美元广义指数变化 {pct:+.2f}%"
        if stale:
            warnings.append("美元指数数据已过期")
        _add(signals, "dollar", "美元强弱", raw, detail, not stale)
    else:
        _add(signals, "dollar", "美元强弱", 0, "无 DTWEXBGS 数据", False)

    events = inputs.get("econ_data") or []
    valid_events = [e for e in events if not e.get("stale")]
    if valid_events:
        raw = sum(-float(e.get("surprise", 0.0)) / float(e.get("scale", 0.3)) for e in valid_events) / len(valid_events)
        detail = "；".join(
            f"{e['name']} 意外 {float(e.get('surprise', 0.0)):+.2f}" for e in valid_events[:3]
        )
    else:
        raw, detail = None, "无实际值/预期值数据"
        if events:
            warnings.append("经济数据已过期")
    _add(signals, "econ_data", "美国经济数据", raw, detail, bool(valid_events))

    flows = inputs.get("flows") or []
    valid_flows = [f for f in flows if not f.get("stale")]
    if valid_flows:
        parts = []
        values = []
        for f in valid_flows:
            value = float(f.get("change", f.get("pct", 0.0))) / float(f.get("scale", 30000))
            values.append(value)
            parts.append(f"{f['name']} {'+' if value >= 0 else ''}{float(f.get('change', f.get('pct', 0.0))):.0f}")
        raw = sum(values) / len(values)
        detail = "；".join(parts[:3])
    else:
        raw, detail = None, "无 COT / ETF 数据"
        if flows:
            warnings.append("资金流向数据已过期")
    _add(signals, "flows", "资金流向", raw, detail, bool(valid_flows))

    total = round(sum(s.contribution for s in signals), 1)
    confidence = round(sum(s.weight for s in signals if s.fresh) / sum(WEIGHTS.values()), 2)
    sufficient = confidence >= 0.4 and any(
        s.fresh for s in signals if s.module in {"real_rate", "dollar", "fed_policy"}
    )
    if confidence < 0.7 and sufficient:
        warnings.append("数据覆盖不足，信号置信度较低")

    bias = classify(total) if sufficient else "数据不足"
    return ScoreResult(
        total=total if sufficient else 0.0,
        bias=bias, confidence=confidence, signals=signals,
        warnings=warnings, sufficient=sufficient,
    )
