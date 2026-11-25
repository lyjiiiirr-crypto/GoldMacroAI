"""Telegram 消息模板（HTML parse_mode）。"""
from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone

from analysis.gold_bias import DISCLAIMER, bias_label, score_bar
from analysis.tech_gex_fusion import fuse, gex_bias
from config import CHINA_TIMEZONE_OFFSET_HOURS, SERIES
from data.crt import p3_vs_p2

ESC = html.escape


def china_now() -> datetime:
    return datetime.now(timezone(timedelta(hours=CHINA_TIMEZONE_OFFSET_HOURS)))


GEX_SYMBOL_LABELS = {
    "GLD": "GLD 黄金ETF代理",
    "SPY": "SPY 大盘风险偏好",
}


def render_gex_lines(gex: list[dict], *, limit: int = 2) -> list[str]:
    """渲染 GEX / Gamma flip 摘要，日报与 /gex 命令共用。"""
    lines = ["<b>GEX / Gamma 翻转点（延迟近似）</b>"]
    if not gex:
        lines.append("  - 暂无 GEX 数据（运行 --sync-only 或 --import-gex 导入）")
        return lines
    for row in gex[:limit]:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        symbol = str(row.get("symbol") or payload.get("symbol") or "")
        label = payload.get("label") or GEX_SYMBOL_LABELS.get(symbol, symbol)
        as_of = row.get("as_of_date") or payload.get("as_of") or ""
        regime = row.get("regime") or payload.get("regime") or ""
        bias = gex_bias(row)
        head = (
            f"- {ESC(str(label))}（{ESC(str(as_of))}）：{ESC(str(regime))} "
            f"<b>{bias['icon']}{ESC(bias['label'])}</b>"
        )
        lines.append(head)
        lines.append("  " + ESC(bias["reason"]))
        summary = row.get("summary") or payload.get("summary") or ""
        if summary:
            lines.append("  " + ESC(str(summary)))
        spot = row.get("spot") if row.get("spot") is not None else payload.get("spot")
        flip = row.get("gamma_flip") if row.get("gamma_flip") is not None else payload.get("gamma_flip")
        call_wall = row.get("call_wall") if row.get("call_wall") is not None else payload.get("call_wall")
        put_wall = row.get("put_wall") if row.get("put_wall") is not None else payload.get("put_wall")
        bits = []
        if spot is not None:
            bits.append(f"现价 {float(spot):.2f}")
        if flip is not None:
            bits.append(f"flip {float(flip):.2f}")
        if call_wall is not None:
            bits.append(f"Call墙 {float(call_wall):.2f}")
        if put_wall is not None:
            bits.append(f"Put墙 {float(put_wall):.2f}")
        if bits:
            lines.append("  " + ESC("；".join(bits)))
    lines.append(
        "  <i>偏向按净GEX符号与 flip 相对位置推导，未回测，仅作背景；"
        "来源：Cboe 免费延迟期权报价 + 本地 Black-Scholes 估算，非做市商真实库存，非交易信号。</i>"
    )
    return lines


def render_fusion_lines(fusion: dict) -> list[str]:
    """渲染「技术面 × GEX 融合方向」结论（日报 /report 共用）。"""
    direction = str(fusion.get("direction") or "中性震荡")
    icon = {"偏多": "🟢", "偏空": "🔴", "中性震荡": "⚪"}.get(direction, "⚪")
    score = fusion.get("score")
    strength = fusion.get("strength") or ""
    lines = ["<b>技术面 × GEX 融合方向（延迟近似，未回测）</b>"]
    conclusion = f"- 结论：<b>{icon} {ESC(direction)}</b>"
    if score is not None:
        conclusion += f"（融合分 {float(score):+.2f}，强度 {ESC(str(strength))}）"
    lines.append(conclusion)
    if fusion.get("reason"):
        lines.append("  " + ESC(str(fusion["reason"])))
    if fusion.get("disclaimer"):
        lines.append(f"  <i>{ESC(str(fusion['disclaimer']))}</i>")
    return lines


def _crt_p3_view(row: dict) -> dict:
    """取 CRT 快照的 P3/P2 视图；旧快照缺字段时用 3 根 K 线重算。"""
    data = row.get("payload") if isinstance(row.get("payload"), dict) else {}

    def pick(key, default=None):
        value = row.get(key)
        return value if value is not None else data.get(key, default)

    view = {
        "label": pick("label") or row.get("symbol") or "",
        "as_of": pick("as_of"),
        "scenario": pick("scenario") or "待确认",
        "bias": pick("bias") or "中性",
        "target": pick("target"),
        "stop_ref": pick("stop_ref"),
        "summary": pick("summary"),
        "range_high": pick("range_high", pick("prev_high")),
        "range_low": pick("range_low", pick("prev_low")),
        "equilibrium": pick("equilibrium"),
    }
    for key in (
        "p2_high", "p2_low", "p3_high", "p3_low", "p3_close",
        "p3_sweep_side", "p3_swept_p2_high", "p3_swept_p2_low",
        "p3_reclaimed_p2_range", "p3_reclaim_text",
        "dist_p3_high_to_p2_high", "dist_p3_low_to_p2_low",
    ):
        view[key] = pick(key)
    if view["p2_high"] is None:
        view["p2_high"] = pick("sweep_high")
    if view["p2_low"] is None:
        view["p2_low"] = pick("sweep_low")
    if view["p3_high"] is None:
        view["p3_high"] = pick("current_high")
    if view["p3_low"] is None:
        view["p3_low"] = pick("current_low")
    if view["p3_close"] is None:
        view["p3_close"] = pick("current_close")

    if view["p3_swept_p2_high"] is None and view["p3_swept_p2_low"] is None:
        candles = pick("candles") or []
        if len(candles) >= 3:
            try:
                computed = p3_vs_p2(candles[-3:])
            except (TypeError, ValueError, KeyError):
                computed = None
            if computed:
                for key in (
                    "p2_high", "p2_low", "p3_high", "p3_low", "p3_close",
                    "p3_sweep_side", "p3_swept_p2_high", "p3_swept_p2_low",
                    "p3_reclaimed_p2_range", "p3_reclaim_text",
                    "dist_p3_high_to_p2_high", "dist_p3_low_to_p2_low",
                ):
                    if view[key] is None:
                        view[key] = computed[key]
                # 旧快照的 P1 版情景与新规则冲突，以重算结果为准
                view["scenario"] = computed["p3_scenario"]
                view["bias"] = computed["p3_bias"]
                view["target"] = computed["p3_target"]
                view["stop_ref"] = computed["p3_stop_ref"]
    return view


def _p3_sweep_text(view: dict) -> str:
    """供 /crt 与执行提醒共用的扫荡描述。"""
    swept_high = bool(view.get("p3_swept_p2_high"))
    swept_low = bool(view.get("p3_swept_p2_low"))
    if swept_high and swept_low:
        return "同时扫过 P2 高低点"
    if swept_high:
        return "扫过 P2 高点"
    if swept_low:
        return "扫过 P2 低点"
    return "未扫过 P2 高低点"


def render_crt_lines(crt: list[dict], *, limit: int = 2) -> list[str]:
    """渲染 4H CRT 三根结构摘要（/crt 命令用，日报在执行提醒中引用）。"""
    lines = ["<b>4H CRT 三根结构（UTC 02/06/... 收线，近似）</b>"]
    if not crt:
        lines.append("  - 暂无 CRT 数据（运行 --sync-only 或 --import-crt 导入）")
        return lines

    def num(value, fallback: str = "—") -> str:
        try:
            return f"{float(value):.2f}"
        except (TypeError, ValueError):
            return fallback

    def signed(value) -> str:
        try:
            return f"{float(value):+.2f}"
        except (TypeError, ValueError):
            return "—"

    for row in crt[:limit]:
        view = _crt_p3_view(row)
        as_of = str(view["as_of"] or "")[:16]
        lines.append(
            f"- {ESC(str(view['label']))}（{ESC(as_of)}）："
            f"{ESC(str(view['scenario']))}（{ESC(str(view['bias']))}）"
        )
        lines.append(
            f"  P1 背景区间 高 {num(view['range_high'])} / 低 {num(view['range_low'])}"
            "（历史参考，不再作为当前目标）"
        )
        lines.append(
            f"  P2 当前区间 高 {num(view['p2_high'])} / 低 {num(view['p2_low'])}"
        )
        lines.append(
            f"  P3 高 {num(view['p3_high'])}（距 P2 高点 "
            f"{signed(view['dist_p3_high_to_p2_high'])} 点）/ "
            f"低 {num(view['p3_low'])}（距 P2 低点 "
            f"{signed(view['dist_p3_low_to_p2_low'])} 点）：{_p3_sweep_text(view)}"
        )
        lines.append(
            f"  P3 收盘 {num(view['p3_close'])}：{ESC(str(view['p3_reclaim_text'] or '收回状态未记录'))}"
        )
        refs = []
        if view["target"] is not None:
            refs.append(f"目标 {num(view['target'])}")
        if view["stop_ref"] is not None:
            refs.append(f"失效参考 {num(view['stop_ref'])}")
        if refs:
            lines.append("  结构参考：" + "；".join(refs))
        if view["summary"]:
            lines.append("  " + ESC(str(view["summary"])))
    lines.append("  <i>来源：TradingView 公开行情优先，Yahoo Finance 公开行情回退；未回测，非交易信号。</i>")
    return lines


def _beijing_clock(value) -> str:
    """把 UTC ISO 时间转成北京时间 HH:MM（用于标注 4H 收线时刻）。"""
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(
            timezone(timedelta(hours=CHINA_TIMEZONE_OFFSET_HOURS))
        ).strftime("%H:%M")
    except (TypeError, ValueError):
        return str(value or "")[11:16]


def render_execution_lines(market: dict, fusion: dict | None = None,
                           macro_bias: str | None = None) -> list[str]:
    """交易执行提醒：4H 三根 CRT 结构定关键位，4H/15M LTF 参与 P2 扫荡。"""
    lines = ["<b>交易执行提醒（4H CRT 三根结构 → 4H/15M 执行）</b>"]

    def num(value, fallback: str = "—") -> str:
        try:
            return f"{float(value):.2f}"
        except (TypeError, ValueError):
            return fallback

    def signed(value) -> str:
        try:
            return f"{float(value):+.2f}"
        except (TypeError, ValueError):
            return "—"

    crt = market.get("crt") or []
    chart_ready = bool(crt)
    if crt:
        view = _crt_p3_view(crt[0])
        lines.append(
            f"- 4H 结构（北京 {ESC(_beijing_clock(view['as_of']))} 收线）："
            f"{ESC(str(view['scenario']))}（{ESC(str(view['bias']))}）"
        )
        lines.append(
            f"- P2 区间（当前参考）：高 {num(view['p2_high'])}"
            f" / 低 {num(view['p2_low'])}"
        )
        lines.append(
            f"- P3 高低点距 P2 边界：高点距 P2 高点 "
            f"{signed(view['dist_p3_high_to_p2_high'])} 点"
            f"（{'已扫过' if view['p3_swept_p2_high'] else '未扫过'}），"
            f"低点距 P2 低点 {signed(view['dist_p3_low_to_p2_low'])} 点"
            f"（{'已扫过' if view['p3_swept_p2_low'] else '未扫过'}）"
        )
        lines.append(
            f"- P3 收盘 {num(view['p3_close'])}："
            f"{ESC(str(view['p3_reclaim_text'] or '收回状态未记录'))}"
            f"（{num(view['p2_low'])}~{num(view['p2_high'])}）"
        )
        refs = []
        if view["target"] is not None:
            refs.append(f"目标 {num(view['target'])}")
        if view["stop_ref"] is not None:
            refs.append(f"失效参考 {num(view['stop_ref'])}")
        if refs:
            lines.append(
                "- 结构参考：" + "；".join(refs) + "（仅为结构位，非挂单建议）"
            )
    else:
        chart_ready = False
        lines.append("- 4H CRT：暂无数据（运行 --sync-only 或 --import-crt 导入）")
        lines.append("- 4H 结构：等待至少 3 根已收盘 4H K 线后再判断 P1/P2/P3")

    if fusion and fusion.get("direction"):
        try:
            score_text = f"{float(fusion.get('score') or 0):+.2f}"
        except (TypeError, ValueError):
            score_text = "—"
        lines.append(
            f"- 融合方向：{ESC(str(fusion['direction']))}（{score_text}）"
            " → 与 4H 情景冲突时降级观望，先等结构确认"
        )
    lines += [
        "- LTF：4H/15M 以 P3 扫荡与收盘是否收回 P2 区间为依据，等回测确认再入场，不追价",
        f"- 宏观（{macro_bias or '方向'}）≠ 入场信号；止损先行，单笔风险可控",
    ]
    if chart_ready:
        lines.append("  <i>附：3 根 4H K 线图见随后消息。</i>")
    return lines


def _xau_bias_mark(bias: str) -> str:
    return {"偏多": "🟢偏多", "偏空": "🔴偏空"}.get(bias, "⚪中性")


def _polymarket_bias(question: str, probability) -> str:
    """按利率市场问法与 Yes 概率给出 XAUUSD 偏向（仅报告标注）。"""
    try:
        value = float(probability)
    except (TypeError, ValueError):
        return "中性"
    if value < 50:
        return "中性"
    text = str(question or "").lower()
    # 用单词边界，避免 "ease" 命中 "increase"（加息）这类子串误判
    dovish = re.search(
        r"\b(cut|cuts|cutting|ease|easing|lower|lowers|lowering|decrease|decreases|"
        r"decreasing|dovish)\b", text
    )
    hawkish = re.search(
        r"\b(hike|hikes|hiking|raise|raises|raising|increase|increases|increasing|hawkish)\b",
        text,
    )
    if dovish or any(key in text for key in ("降息", "宽松", "下调")):
        return "偏多"
    if hawkish or any(key in text for key in ("加息", "收紧", "上调")):
        return "偏空"
    return "中性"


def _macro_xau_bias(key: str, item: dict) -> str:
    """根据宏观数据的变化方向给出对 XAUUSD 的简化偏向（仅报告标注）。"""
    delta = item.get("delta") or item.get("pct") or 0.0
    pct = item.get("pct") or 0.0
    try:
        delta, pct = float(delta), float(pct)
    except (TypeError, ValueError):
        delta, pct = 0.0, 0.0
    if key in {"DFII10", "DGS2", "DGS10", "DXY", "DTWEXBGS", "DFF"}:
        if delta > 0 or pct > 0:
            return "偏空"
        if delta < 0 or pct < 0:
            return "偏多"
    if key == "T10YIE":
        if delta > 0:
            return "偏空"
        if delta < 0:
            return "偏多"
    return "中性"


def render_daily_report(score, analysis: dict, market: dict | None = None) -> str:
    market = market or {}
    now = china_now()
    lines = [
        "<b>📊 GOLD MACRO BOT · XAUUSD 宏观日报</b>",
        f"<i>{now:%Y-%m-%d} 北京时间 {now:%H:%M}</i>",
        "",
        f"宏观方向：<b>{ESC(bias_label(score))}</b>",
        f"<b>{score.total:+.1f} / 100</b>  置信度 {score.confidence:.0%}",
        f"<code>{score_bar(score.total)}</code>",
        "",
        "<b>关键市场数据（对 XAUUSD 偏向）</b>",
    ]
    for series_id, label in (
        ("DFII10", "10Y实际收益率"), ("DXY", "DXY"),
        ("T10YIE", "10Y通胀预期"), ("DFF", "联邦基金利率"),
    ):
        item = market.get(series_id)
        if not item or item.get("value") is None:
            continue
        unit = SERIES[series_id]["unit"]
        direction = _macro_xau_bias(series_id, item)
        delta_text = f"（{item['change_text']}）" if item.get("change_text") else ""
        stale = " ⚠️过期" if item.get("stale") else ""
        lines.append(
            f"- {label} {item['value']:.2f}{unit}{delta_text} → XAUUSD {_xau_bias_mark(direction)}{stale}"
        )
    if not any(market.get(k) for k in SERIES):
        lines.append("- 暂无 FRED 数据（配置 FRED_API_KEY 后自动采集）")

    fed = market.get("fedwatch")
    lines += ["", "<b>美联储政策预期</b>"]
    if fed:
        dovish = fed.get("dovish_pp")
        if dovish is None:
            dovish = fed.get("change_pp") or 0.0
        try:
            fed_bias = "偏多" if float(dovish) > 0 else "偏空" if float(dovish) < 0 else "中性"
        except (TypeError, ValueError):
            fed_bias = "中性"
        lines.append(
            f"- {ESC(fed.get('text', '无 FedWatch 快照'))} → XAUUSD {_xau_bias_mark(fed_bias)}"
        )
    else:
        lines.append("- 无 FedWatch 快照（自动采集失败时可用 --import-fedwatch 录入）")

    poly = market.get("polymarket") or {}
    for item in (poly.get("markets") or [])[:2]:
        question = str(item.get("question") or "利率预测")
        probability = item.get("yes_probability")
        volume = item.get("volume")
        vol_text = ""
        try:
            if volume and float(volume) > 0:
                vol_text = f"，24h 成交额 ${float(volume):,.0f}"
        except (TypeError, ValueError):
            pass
        bias = _polymarket_bias(question, probability)
        prob_text = f"{float(probability):.1f}%" if probability is not None else "—"
        lines.append(
            f"- Polymarket「{ESC(question)}」Yes {prob_text}{vol_text}"
            f" → XAUUSD {_xau_bias_mark(bias)}"
        )
    if poly:
        lines.append(
            f"  <i>{ESC(poly.get('disclaimer') or 'Polymarket 为公开预测市场赔率，非官方利率数据，仅作参考。')}</i>"
        )

    events = market.get("events") or []
    if events:
        lines += ["", "<b>经济数据意外</b>"]
        for e in events[:3]:
            surprise = e.get("surprise") or 0.0
            try:
                surprise = float(surprise)
            except (TypeError, ValueError):
                surprise = 0.0
            name = str(e.get("name") or "")
            if "CPI" in name or "PCE" in name:
                bias = "偏多" if surprise < 0 else "偏空" if surprise > 0 else "中性"
            elif "失业" in name or "非农" in name or "NFP" in name:
                bias = "偏空" if surprise > 0 else "偏多" if surprise < 0 else "中性"
            else:
                bias = "中性"
            lines.append(
                f"- {ESC(e['name'])}：实际 {e['actual']}，预期 {e['expected']}"
                f" → XAUUSD {_xau_bias_mark(bias)}"
            )

    flows = market.get("flows") or []
    if flows:
        lines += ["", "<b>资金流向</b>"]
        for f in flows[:3]:
            try:
                change = float(f.get("change") or 0.0)
            except (TypeError, ValueError):
                change = 0.0
            bias = "偏多" if change > 0 else "偏空" if change < 0 else "中性"
            lines.append(
                f"- {ESC(f['name'])}：{f['text']} → XAUUSD {_xau_bias_mark(bias)}"
            )

    technical = market.get("technical") or []
    if technical:
        lines += ["", "<b>技术面（本地自算）</b>"]
        sources: list[str] = []
        for report in technical[:2]:
            label = report.get("label") or report.get("symbol") or "技术面"
            as_of = report.get("as_of") or report.get("report_date") or ""
            head = (f"- {ESC(str(label))}（{ESC(str(as_of))}）："
                    f"{ESC(str(report.get('bias') or '中性震荡'))}")
            close = report.get("close")
            if close is not None:
                head += f"，收盘 {float(close):.2f}"
            lines.append(head)
            if report.get("summary"):
                lines.append("  " + ESC(str(report["summary"])))
            payload = report.get("payload")
            indicators = (payload.get("indicators") if isinstance(payload, dict) else {}) or {}
            levels = (payload.get("levels") if isinstance(payload, dict) else {}) or {}
            bits = []
            if indicators.get("rsi14") is not None:
                bits.append(f"RSI {float(indicators['rsi14']):.1f}")
            if indicators.get("histogram") is not None:
                bits.append(f"MACD柱 {float(indicators['histogram']):+.2f}")
            for key, name in (("support", "支撑"), ("resistance", "阻力")):
                values = levels.get(key) or []
                if values:
                    bits.append(f"{name} " + "/".join(f"{float(v):.2f}" for v in values))
            if bits:
                lines.append("  " + ESC("；".join(bits)))
            payload = report.get("payload")
            source = report.get("source") or (
                (payload.get("source") if isinstance(payload, dict) else None)
            )
            if source:
                sources.append(str(source))
        source_text = " / ".join(dict.fromkeys(sources)) or "公开行情 + 本地规则计算"
        lines.append(f"  <i>来源：{ESC(source_text)}，非 Trading Central 官方研报。</i>")

    gex = market.get("gex") or []
    if gex:
        lines += [""] + render_gex_lines(gex, limit=2)

    # 技术面 × GEX 融合方向：只作报告上下文，不参与宏观评分
    fusion = None
    if technical or gex:
        fusion = fuse(market.get("technical") or [], market.get("gex") or [])
        lines += [""] + render_fusion_lines(fusion)

    calendar = market.get("calendar") or []
    if calendar:
        lines += ["", "<b>未来24小时事件</b>"]
        lines += [f"- {ESC(e['time'])} {ESC(e['name'])}" for e in calendar[:5]]

    source = {"ollama": "本地Ollama", "cloud": "云端备用模型", "rule": "规则模板"}.get(analysis.get("source", ""), "规则模板")
    lines += [
        "", f"<b>AI 综合判断（{source}）</b>",
        ESC(analysis.get("text", "")),
        "",
    ]
    lines += render_execution_lines(market, fusion, bias_label(score))
    lines += ["", f"⚠️ {ESC(DISCLAIMER)}"]
    if score.warnings:
        lines.append("⚠️ 数据提示：" + ESC("；".join(score.warnings)))
    return "\n".join(lines)


def render_alert(kind: str, title: str, body: str) -> str:
    return f"<b>⚡ {ESC(title)}</b>\n{ESC(body)}\n\n⚠️ {ESC(DISCLAIMER)}"
