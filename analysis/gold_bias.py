"""宏观方向描述与可视化辅助。"""
from __future__ import annotations

from analysis.macro_score import ScoreResult

BIAS_EMOJI = {
    "强烈偏多": "🟢🟢",
    "温和偏多": "🟢",
    "中性震荡": "⚪",
    "温和偏空": "🔴",
    "强烈偏空": "🔴🔴",
    "数据不足": "⚠️",
}

DISCLAIMER = "宏观方向 ≠ 入场信号；评分未经历史回测，不构成投资建议。"


def bias_label(score: ScoreResult) -> str:
    label = f"{BIAS_EMOJI.get(score.bias, '')} {score.bias}"
    if score.confidence < 0.7 and score.sufficient:
        label += "（低置信度）"
    return label


def score_bar(total: float, width: int = 20) -> str:
    center = width // 2
    pos = center + int(round(total / 100 * center))
    pos = max(0, min(width, pos))
    chars = ["·"] * width
    chars[pos] = "●"
    return "[" + "".join(chars) + "]"
