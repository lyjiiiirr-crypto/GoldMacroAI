"""AI 解释层：本地 Ollama 优先 -> 云端兼容接口备用 -> 规则模板兜底。

AI 只解释宏观驱动力，不直接预测涨跌或给入场点。
"""
from __future__ import annotations

import json
import urllib.request

from config import (
    CLOUD_AI_API_KEY, CLOUD_AI_BASE_URL, CLOUD_AI_MODEL, CLOUD_AI_TIMEOUT,
    OLLAMA_ENABLED, OLLAMA_HOST, OLLAMA_MODEL, OLLAMA_TIMEOUT,
)

SYSTEM_PROMPT = (
    "你是一名黄金(XAUUSD)宏观分析师。严格依据给定的结构化数据做解读，不要编造数据。"
    "必须区分宏观方向、交易方向、入场信号三个层级，明确说明宏观方向不等于入场信号。"
    "不要给具体入场价位，不要承诺涨跌。用中文输出，控制在250字以内，"
    "包含：政策环境、实际利率与美元、主要矛盾、反转条件。"
)


def build_payload(score, market: dict | None = None) -> dict:
    return {
        "score": score.as_dict(),
        "market": market or {},
        "notice": "评分未经回测，仅作宏观背景参考。",
    }


def rule_based_analysis(score, market: dict | None = None) -> str:
    """无 AI 时的确定性中文解读。"""
    parts = []
    if not score.sufficient:
        parts.append("当前有效数据不足，系统不给出宏观方向。请先配置 FRED API Key 并导入 FedWatch 快照。")
    else:
        parts.append(f"宏观评分 {score.total:+.1f}/100，方向为「{score.bias}」，置信度 {score.confidence:.0%}。")
        bullish = [s for s in score.signals if s.fresh and s.contribution > 0]
        bearish = [s for s in score.signals if s.fresh and s.contribution < 0]
        if bullish:
            parts.append("支持黄金的因素：" + "、".join(f"{s.name}（{s.detail}）" for s in bullish) + "。")
        if bearish:
            parts.append("压制黄金的因素：" + "、".join(f"{s.name}（{s.detail}）" for s in bearish) + "。")
        if not bullish and not bearish:
            parts.append("各模块信号接近中性，缺乏明确宏观驱动力。")
    parts.append("宏观方向是数日至数周的背景，交易方向需结合当前时段，入场必须由价格结构确认。")
    if score.warnings:
        parts.append("数据提示：" + "；".join(score.warnings) + "。")
    parts.append("反转条件：实际收益率与美元同步反向、降息预期大幅回摆，或关键经济数据显著偏离预期。")
    return " ".join(parts)


def _post_json(url: str, payload: dict, headers: dict | None = None, timeout: int = 30) -> dict:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def _ask_ollama(prompt: str) -> str:
    result = _post_json(
        f"{OLLAMA_HOST}/api/generate",
        {"model": OLLAMA_MODEL, "prompt": prompt, "stream": False,
         "system": SYSTEM_PROMPT, "options": {"temperature": 0.2}},
        timeout=OLLAMA_TIMEOUT,
    )
    return str(result.get("response", "")).strip()


def _ask_cloud(prompt: str) -> str:
    if not CLOUD_AI_BASE_URL or not CLOUD_AI_API_KEY:
        return ""
    result = _post_json(
        f"{CLOUD_AI_BASE_URL}/chat/completions",
        {"model": CLOUD_AI_MODEL, "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ], "temperature": 0.2},
        headers={"Authorization": f"Bearer {CLOUD_AI_API_KEY}"},
        timeout=CLOUD_AI_TIMEOUT,
    )
    choices = result.get("choices") or []
    if not choices:
        return ""
    return str(choices[0].get("message", {}).get("content", "")).strip()


def generate_analysis(score, market: dict | None = None) -> dict:
    """返回 {"text": 分析文本, "source": ollama|cloud|rule}。"""
    payload = build_payload(score, market)
    prompt = "请根据以下 JSON 数据生成中文宏观解读：\n" + json.dumps(payload, ensure_ascii=False)
    providers = []
    if OLLAMA_ENABLED:
        providers.append(("ollama", _ask_ollama))
    providers.append(("cloud", _ask_cloud))
    for name, fn in providers:
        try:
            text = fn(prompt)
            if text:
                return {"text": text, "source": name}
        except Exception:  # noqa: BLE001 - AI 不可用时自动降级
            continue
    return {"text": rule_based_analysis(score, market), "source": "rule"}


def generate_report_text(score, market: dict | None = None) -> dict:
    return generate_analysis(score, market)
