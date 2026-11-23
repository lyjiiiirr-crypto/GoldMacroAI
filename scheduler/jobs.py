"""定时任务主流程：采集 -> 评分 -> AI解读 -> 报告 -> 提醒 -> 落库。"""
from __future__ import annotations

import json
from datetime import date

from analysis.ai_interpreter import generate_analysis
from analysis.data_cleaner import age_days, change_by_days, pct_change
from analysis.macro_score import compute_score
from alerts.alert_rules import evaluate, send_alerts
from alerts.crt_chart import build_crt_chart
from alerts.message_templates import render_alert, render_daily_report
from alerts.notifier import channel_status, notify, notify_photo
from config import SERIES
from data.calendar import format_calendar, upcoming_events
from data.cot import collect as collect_cot
from data.crt import collect as collect_crt
from data.dxy import collect as collect_dxy
from data.etf import collect as collect_etf
from data.fedwatch import change_pp, format_snapshot
from data.fedwatch_auto import collect as collect_fedwatch
from data.fomc import collect as collect_fomc
from data.fred import collect as collect_fred
from data.gex import collect as collect_gex
from data.macro_auto import collect as collect_macro
from data.polymarket import collect as collect_polymarket
from data.technical import collect as collect_technical
from database.models import Database

RATE_SERIES = {"DFII10", "DGS2", "DGS10", "DFF", "T10YIE"}
MACRO_KEYS = ("CPI_YOY", "CORE_CPI_YOY", "NFP_CHG", "PCEPI", "PCEPILFE", "ICSA")


# ---- 采集 ----
def collect_all(db: Database) -> dict:
    """运行全部免费采集器；单个来源失败不影响其他来源。"""
    summary: dict[str, dict] = {}
    try:
        summary.update(collect_fred(db))
    except Exception as exc:  # noqa: BLE001
        summary["FRED"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    collectors = (
        ("DXY", collect_dxy),
        ("FedWatch", collect_fedwatch),
        ("Polymarket", collect_polymarket),
        ("BLS", collect_macro),
        ("COT", collect_cot),
        ("WGC_ETF", collect_etf),
        ("FOMC", collect_fomc),
        ("Technical", collect_technical),
        ("GEX", collect_gex),
        ("CRT", collect_crt),
    )
    for name, func in collectors:
        try:
            result = func(db)
            result.setdefault("ok", True)
            summary[name] = result
        except Exception as exc:  # noqa: BLE001 - 单源失败不阻断日报
            summary[name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return summary


def describe_collection(summary: dict) -> str:
    """生成跨数据源的采集日志。"""
    lines = []
    for name, result in summary.items():
        if not result.get("ok"):
            lines.append(f"  [FAIL] {name}: {result.get('error')}")
            continue
        parts = [f"[OK] {name}"]
        if result.get("source"):
            parts.append(f"[{result['source']}]")
        for key, label in (("fetched", "抓取"), ("saved", "写入"),
                           ("snapshots", "快照"), ("weeks", "周"),
                           ("documents", "文件"), ("reports", "报告"),
                           ("high", "高"), ("low", "低")):
            if result.get(key) is not None:
                parts.append(f"{label} {result[key]}")
        if isinstance(result.get("series"), dict):
            parts.append(f"{len(result['series'])} 个序列")
        latest = result.get("latest")
        if isinstance(latest, dict):
            if latest.get("date"):
                parts.append(f"最新 {latest['date']}")
            if latest.get("value") is not None:
                parts.append(f"= {latest['value']}")
            if latest.get("cut") is not None:
                parts.append(f"降息 {latest['cut']}% / 加息 {latest.get('hike')}%")
            if latest.get("symbol"):
                parts.append(f"{latest['symbol']} {latest.get('bias', '')}")
            if latest.get("close") is not None:
                parts.append(f"收盘 {latest['close']}")
            if latest.get("scenario"):
                parts.append(f"情景 {latest['scenario']}")
        lines.append("  " + " ".join(parts))
    return "\n".join(lines)


# ---- 市场快照 ----
def build_market(db: Database) -> tuple[dict, dict]:
    """从数据库构造市场快照与评分输入。"""
    market: dict = {}
    inputs: dict = {}

    for series_id, meta in SERIES.items():
        rows = db.observations(series_id, limit=400)
        if not rows:
            continue
        latest = rows[0]
        change = change_by_days(rows, days=5)
        delta = change["delta"] if change else None
        stale = age_days(latest["date"]) > meta["stale_days"]
        label = meta.get("change_label", "5日")
        if series_id in RATE_SERIES:
            change_text = f"{label} {delta * 100:+.1f} bp" if delta is not None else ""
        elif delta is not None and abs(delta) >= 1000:
            change_text = f"{label} {delta:+,.0f}"
        else:
            change_text = f"{label} {delta:+.2f}" if delta is not None else ""
        info = {
            "value": float(latest["value"]), "date": latest["date"],
            "delta": delta, "change_text": change_text, "stale": stale,
        }
        if series_id in ("DTWEXBGS", "DXY"):
            info["pct"] = pct_change(change)
        market[series_id] = info

    real = market.get("DFII10")
    if real:
        inputs["real_rate"] = {
            "delta_bp": (real.get("delta") or 0.0) * 100,
            "as_of": real["date"], "stale": real["stale"],
        }
    # 用户优先关注 ICE DXY；缺失时回退到 FRED 美元广义指数
    dollar, dollar_source = market.get("DXY"), "DXY"
    if not dollar or dollar.get("pct") is None:
        dollar, dollar_source = market.get("DTWEXBGS"), "DTWEXBGS"
    if dollar and dollar.get("pct") is not None:
        inputs["dollar"] = {
            "pct": dollar["pct"], "as_of": dollar["date"],
            "stale": dollar["stale"], "source": dollar_source,
        }

    fed = change_pp(db.fedwatch_snapshots(), days=7)
    market["fedwatch"] = {"text": format_snapshot(fed), **(fed or {})}
    if fed:
        inputs["fed_policy"] = {
            "change_pp": fed["change_pp"], "dovish_pp": fed.get("dovish_pp"),
            "as_of": fed["as_of"], "stale": False,
        }

    poly_rows = db.polymarket_snapshots(limit=1)
    if poly_rows:
        market["polymarket"] = poly_rows[0].get("payload") or {}

    events = []
    for row in db.events(limit=50):
        raw_surprise = float(row["actual"]) - float(row["expected"])
        direction = str(row.get("direction") or "normal")
        # 初请失业金等数据：数值高于预期意味着就业走弱，对黄金反而偏多
        surprise = -raw_surprise if direction == "inverse" else raw_surprise
        events.append({
            "name": row["event_name"], "actual": row["actual"],
            "expected": row["expected"], "previous": row.get("previous"),
            "release_date": row["release_date"], "scale": row["scale"],
            "direction": direction, "source": row.get("source"),
            "surprise": round(surprise, 4),
            "stale": age_days(row["release_date"]) > 20,
        })
    market["events"] = [
        {k: e[k] for k in ("name", "actual", "expected", "release_date", "surprise")}
        for e in events
    ]
    fresh_events = [e for e in events if not e["stale"]]
    if fresh_events:
        inputs["econ_data"] = fresh_events

    flows = []
    seen: set[str] = set()
    all_flows = db.flows(limit=200)
    for row in all_flows:
        kind = row["flow_type"]
        if kind in seen:
            continue
        seen.add(kind)
        same_type = [f for f in all_flows if f["flow_type"] == kind]
        earlier = same_type[1] if len(same_type) > 1 else None
        change = row["value"] - earlier["value"] if earlier else 0.0
        label = {"COT": "COT净多头", "ETF": "黄金ETF持仓"}.get(kind, kind)
        text = (f"变化 {change:+,.0f}（截至 {row['as_of_date']}）" if earlier
                else f"最新值 {row['value']:,.0f}（截至 {row['as_of_date']}，暂无历史对比）")
        detail = {}
        if row.get("raw_json"):
            try:
                detail = json.loads(row["raw_json"])
            except (TypeError, ValueError):
                detail = {}
        flows.append({
            "name": label, "change": change, "scale": row["scale"],
            "as_of": row["as_of_date"], "stale": age_days(row["as_of_date"]) > 30,
            "text": text, "source": row.get("source"), "detail": detail,
        })
    market["flows"] = flows
    fresh_flows = [f for f in flows if not f["stale"] and f["change"] != 0.0]
    if fresh_flows:
        inputs["flows"] = fresh_flows

    macro = []
    for key in MACRO_KEYS:
        item = market.get(key)
        if not item:
            continue
        meta = SERIES[key]
        macro.append({
            "key": key, "name": meta["name"], "unit": meta["unit"],
            "value": item["value"], "date": item["date"],
            "change_text": item["change_text"], "stale": item["stale"],
            "agency": meta.get("agency", ""),
        })
    market["macro"] = macro

    docs = db.fomc_documents(limit=8)
    market["fomc"] = {
        "next_meeting": (fed or {}).get("meeting_date", ""),
        "documents": docs,
    }
    # 技术面 / GEX / CRT 只作为报告上下文展示，不参与宏观评分
    market["technical"] = db.technical_reports(limit=12)
    market["gex"] = db.gex_snapshots(limit=8)
    crt_rows = db.crt_snapshots(limit=8)
    # 旧版两根快照没有完整 candles，优先展示可用的三根结构（截图也依赖它）
    three_candle = [
        row for row in crt_rows
        if len((row.get("payload") or {}).get("candles") or []) >= 3
    ]
    market["crt"] = three_candle or crt_rows
    market["calendar"] = upcoming_events(hours=24)
    return market, inputs


def demo_market() -> tuple[dict, dict]:
    """无密钥时的演示数据：用于本地查看完整报告格式。"""
    today = date.today().isoformat()
    market = {
        "DFII10": {"value": 1.85, "date": today, "delta": -0.12,
                   "change_text": "5日 -12.0 bp", "stale": False},
        "DGS2": {"value": 3.55, "date": today, "delta": -0.08,
                 "change_text": "5日 -8.0 bp", "stale": False},
        "DGS10": {"value": 4.12, "date": today, "delta": -0.05,
                  "change_text": "5日 -5.0 bp", "stale": False},
        "DTWEXBGS": {"value": 120.4, "date": today, "delta": -0.85, "pct": -0.70,
                     "change_text": "5日 -0.85", "stale": False},
        "DXY": {"value": 99.8, "date": today, "delta": -0.62, "pct": -0.62,
                "change_text": "5日 -0.62", "stale": False},
        "fedwatch": {"text": "下次会议降息概率 68.0%（较 7 天前 鹰鸽净变化 上升 10.0 个百分点）",
                     "cut_probability": 68.0, "change_pp": 10.0, "dovish_pp": 10.0,
                     "metric_pp": 10.0, "metric_name": "鹰鸽净变化",
                     "compared_with": "7天前", "as_of": today},
        "polymarket": {
            "source": "Polymarket 公开预测市场（Gamma API）",
            "as_of": f"{today}T08:00:00+00:00",
            "markets": [{
                "question": "美联储下次会议降息 25bp?", "label": "Yes",
                "yes_probability": 68.0, "volume": 1250000.0,
                "end_date": "", "url": "",
            }],
            "text": "美联储下次会议降息 25bp?: Yes 68.0%",
            "disclaimer": "Polymarket 赔率为公开预测市场定价，非官方利率数据；仅作报告上下文，不构成交易信号。",
        },
        "events": [{"name": "CPI 同比", "actual": 2.7, "expected": 2.8,
                     "release_date": today, "surprise": -0.1}],
        "flows": [{"name": "COT净多头", "change": 18000, "scale": 30000,
                   "text": f"变化 +18,000（截至 {today}）", "stale": False}],
        "macro": [
            {"name": "CPI同比", "value": 2.7, "unit": "%", "date": today,
             "change_text": "月度变化 -0.10", "stale": False},
            {"name": "初请失业金人数", "value": 203.0, "unit": "千人", "date": today,
             "change_text": "周度变化 +2,000", "stale": False},
        ],
        "fomc": {
            "next_meeting": "",
            "documents": [{"doc_type": "Statement", "doc_date": today,
                           "title": f"FOMC Statement {today}",
                           "url": "https://www.federalreserve.gov/"}],
        },
        "technical": [{
            "source": "Yahoo Finance 公开行情 + 本地规则计算",
            "symbol": "GC=F", "label": "COMEX黄金期货", "report_date": today,
            "as_of": today, "bias": "偏多", "close": 3623.5,
            "summary": f"日线偏多：收盘 3623.50，RSI14 58.2；MACD 柱 +12.40；支撑 3560.00 / 3520.00，阻力 3680.00 / 3720.00。",
            "disclaimer": "本地自算技术面，数据来自 Yahoo Finance 公开行情；非 Trading Central 官方研报，不构成投资建议。",
        }],
        "gex": [
            {
                "source": "Cboe 免费延迟期权报价（本地 GEX 估算）",
                "symbol": "GLD", "as_of_date": today, "spot": 322.87,
                "net_gex": 185_000_000, "gamma_flip": 318.4,
                "call_wall": 330.0, "put_wall": 315.0, "regime": "正Gamma",
                "summary": "净GEX +185M/1%，flip 318.40，现价高于 flip 1.40%；上涨中卖出对冲（压涨），下跌中买入对冲（托底）。",
                "payload": {
                    "label": "GLD 黄金ETF代理", "as_of": today, "spot": 322.87,
                    "net_gex": 185_000_000, "gamma_flip": 318.4,
                    "call_wall": 330.0, "put_wall": 315.0, "regime": "正Gamma",
                    "hedging_pressure": "上涨中卖出对冲（压涨），下跌中买入对冲（托底）",
                    "distance_pct": 1.4, "summary": "净GEX +185M/1%，flip 318.40，现价高于 flip 1.40%；上涨中卖出对冲（压涨），下跌中买入对冲（托底）。",
                    "gex_map": [],
                    "disclaimer": "GEX 基于 Cboe 免费延迟期权报价与本地 Black-Scholes 估算，未包含真实交易方向与做市商库存，非交易信号。",
                },
            },
            {
                "source": "Cboe 免费延迟期权报价（本地 GEX 估算）",
                "symbol": "SPY", "as_of_date": today, "spot": 585.2,
                "net_gex": -240_000_000, "gamma_flip": 592.1,
                "call_wall": 600.0, "put_wall": 575.0, "regime": "负Gamma",
                "summary": "净GEX -240M/1%，flip 592.10，现价低于 flip 1.16%；上涨中买入对冲（助推），下跌中卖出对冲（杀跌）。",
                "payload": {
                    "label": "SPY 大盘风险偏好", "as_of": today, "spot": 585.2,
                    "net_gex": -240_000_000, "gamma_flip": 592.1,
                    "call_wall": 600.0, "put_wall": 575.0, "regime": "负Gamma",
                    "hedging_pressure": "上涨中买入对冲（助推），下跌中卖出对冲（杀跌）",
                    "distance_pct": -1.16, "summary": "净GEX -240M/1%，flip 592.10，现价低于 flip 1.16%；上涨中买入对冲（助推），下跌中卖出对冲（杀跌）。",
                    "gex_map": [],
                    "disclaimer": "GEX 基于 Cboe 免费延迟期权报价与本地 Black-Scholes 估算，未包含真实交易方向与做市商库存，非交易信号。",
                },
            },
        ],
        "crt": [{
            "source": "TradingView 公开行情（4H CRT）",
            "symbol": "XAUUSD", "as_of": f"{today}T10:00:00+00:00",
            "timeframe": "4h", "current_close": 3605.80,
            "current_high": 3620.50, "current_low": 3580.20,
            "range_high": 3610.00, "range_low": 3575.40,
            "prev_high": 3610.00, "prev_low": 3575.40, "equilibrium": 3592.70,
            "sweep_high": 3608.00, "sweep_low": 3568.00,
            "dist_high_to_resistance": -10.50, "dist_low_to_support": 4.80,
            "dist_sweep_low_to_support": -7.40, "dist_sweep_high_to_resistance": 2.00,
            "sweep_side": "低", "swept_prev_high": False, "swept_prev_low": True,
            "scenario": "Power of Three · 偏多确认", "bias": "偏多",
            "target": 3610.00, "stop_ref": 3568.00,
            "summary": f"P1 区间 高 3610.00 / 低 3575.40；P2 扫低 3568.00 后收回区间；"
                       f"P3 扩张 高 3620.50 / 低 3580.20；情景 Power of Three · 偏多确认。",
            "payload": {
                "label": "现货黄金 XAUUSD", "as_of": f"{today}T10:00:00+00:00",
                "candles": [
                    {"time": f"{today}T02:00:00+00:00", "open": 3580.0,
                     "high": 3610.0, "low": 3575.4, "close": 3590.0},
                    {"time": f"{today}T06:00:00+00:00", "open": 3590.0,
                     "high": 3608.0, "low": 3568.0, "close": 3595.0},
                    {"time": f"{today}T10:00:00+00:00", "open": 3595.0,
                     "high": 3620.5, "low": 3580.2, "close": 3605.8},
                ],
            },
        }],
        "calendar": [],
    }
    inputs = {
        "fed_policy": {"change_pp": 10.0, "dovish_pp": 10.0, "stale": False},
        "real_rate": {"delta_bp": -12.0, "stale": False},
        "dollar": {"pct": -0.62, "source": "DXY", "stale": False},
        "econ_data": [{"name": "CPI 同比", "surprise": -0.1, "scale": 0.3, "stale": False}],
        "flows": [{"name": "COT净多头", "change": 18000, "scale": 30000, "stale": False}],
    }
    return market, inputs


# ---- 主流程 ----
def run_once(db: Database | None = None, *, dry_run: bool = False, send: bool = False,
             demo: bool = False, collect_data: bool = True,
             sync_only: bool = False) -> dict:
    db = db or Database()
    db.init_schema()

    if demo:
        market, inputs = demo_market()
        collection = {}
    else:
        collection = collect_all(db) if collect_data else {}
        if collection:
            print("[collect] 采集结果：\n" + describe_collection(collection))
        market, inputs = build_market(db)

    if sync_only:
        return {"collection": collection, "synced": True}
    if not collection:
        print("[collect] 本次跳过联网采集，仅使用本地缓存（--no-collect）")

    score = compute_score(inputs)
    analysis = generate_analysis(score, market)
    report = render_daily_report(score, analysis, market)
    print("\n" + report + "\n")

    result = {"score": score, "analysis": analysis, "report": report, "sent": [],
              "collection": collection}

    if demo or dry_run:
        return result

    previous_reports = db.reports(limit=2)
    previous = None
    if len(previous_reports) > 1:
        prev_payload = previous_reports[1].get("payload", {})
        previous = _score_from_payload(prev_payload)
    db.save_report(score.total, score.bias, {"score": score.as_dict(), "market": market,
                                             "analysis": analysis})

    if send:
        delivered, channel = notify(report, title="XAUUSD 宏观日报")
        if delivered:
            result["sent"].append(f"daily_report({channel})")
            alerts = evaluate(score, previous, market)

            def _send_alert(title: str, body: str) -> bool:
                ok, _ = notify(render_alert("alert", title, body), title=title)
                return ok

            result["sent"] += send_alerts(alerts, db, _send_alert)

            # 执行提醒后附 3 根 4H K 线截图；失败不影响文本报告与提醒
            try:
                chart = build_crt_chart(market)
            except Exception as exc:  # noqa: BLE001
                chart = None
                print(f"[chart] 3 根 4H 截图生成失败: {exc}")
            if chart:
                photo_ok, photo_channel = notify_photo(
                    chart, caption="3根4H K线（P1区间 / P2扫荡 / P3扩张）",
                    title="4H CRT K线",
                )
                if photo_ok:
                    result["sent"].append(f"crt_chart({photo_channel})")
        else:
            print(f"[notify] 推送通道不可用（{channel or channel_status()}），跳过全部推送。"
                  "配置 Telegram Bot 或设置 NTFY_TOPIC 即可启用")
    return result


def _score_from_payload(payload: dict):
    from analysis.macro_score import ScoreResult, Signal
    try:
        raw = payload["score"]
        signals = [Signal(**s) for s in raw.get("signals", [])]
        return ScoreResult(
            total=float(raw["total"]), bias=str(raw["bias"]),
            confidence=float(raw.get("confidence", 0)), signals=signals,
            warnings=list(raw.get("warnings", [])), sufficient=bool(raw.get("sufficient")),
        )
    except (KeyError, TypeError, ValueError):
        return None
