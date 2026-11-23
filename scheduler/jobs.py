"""定时任务主流程：采集 -> 评分 -> AI解读 -> 报告 -> 提醒 -> 落库。"""
from __future__ import annotations

from datetime import date

from analysis.ai_interpreter import generate_analysis
from analysis.data_cleaner import age_days, change_by_days, pct_change
from analysis.macro_score import compute_score
from alerts.alert_rules import evaluate, send_alerts
from alerts.message_templates import render_alert, render_daily_report
from alerts.notifier import channel_status, notify
from config import SERIES
from data.calendar import format_calendar, upcoming_events
from data.fedwatch import change_pp, format_snapshot
from data.fred import collect, describe_collection
from database.models import Database

RATE_SERIES = {"DFII10", "DGS2", "DGS10", "DFF", "T10YIE"}


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
        if series_id in RATE_SERIES:
            change_text = f"5日 {delta * 100:+.1f} bp" if delta is not None else ""
        else:
            change_text = f"5日 {delta:+.2f}" if delta is not None else ""
        info = {
            "value": float(latest["value"]), "date": latest["date"],
            "delta": delta, "change_text": change_text, "stale": stale,
        }
        if series_id == "DTWEXBGS":
            info["pct"] = pct_change(change)
        market[series_id] = info

    real = market.get("DFII10")
    if real:
        inputs["real_rate"] = {
            "delta_bp": (real.get("delta") or 0.0) * 100,
            "as_of": real["date"], "stale": real["stale"],
        }
    dollar = market.get("DTWEXBGS")
    if dollar and dollar.get("pct") is not None:
        inputs["dollar"] = {
            "pct": dollar["pct"], "as_of": dollar["date"], "stale": dollar["stale"],
        }

    fed = change_pp(db.fedwatch_snapshots(), days=7)
    market["fedwatch"] = {"text": format_snapshot(fed), **(fed or {})}
    if fed:
        inputs["fed_policy"] = {
            "change_pp": fed["change_pp"], "as_of": fed["as_of"], "stale": False,
        }

    events = []
    for row in db.events(limit=50):
        surprise = float(row["actual"]) - float(row["expected"])
        events.append({
            "name": row["event_name"], "actual": row["actual"],
            "expected": row["expected"], "previous": row.get("previous"),
            "release_date": row["release_date"], "scale": row["scale"],
            "surprise": round(surprise, 4),
            "stale": age_days(row["release_date"]) > 20,
        })
    market["events"] = [{k: e[k] for k in ("name", "actual", "expected", "release_date", "surprise")} for e in events]
    fresh_events = [e for e in events if not e["stale"]]
    if fresh_events:
        inputs["econ_data"] = fresh_events

    flows = []
    seen: set[str] = set()
    for row in db.flows(limit=100):
        kind = row["flow_type"]
        if kind in seen:
            continue
        seen.add(kind)
        same_type = [f for f in db.flows(limit=100) if f["flow_type"] == kind]
        earlier = same_type[1] if len(same_type) > 1 else None
        change = row["value"] - earlier["value"] if earlier else 0.0
        label = {"COT": "COT净多头", "ETF": "黄金ETF持仓"}.get(kind, kind)
        text = (f"变化 {change:+,.0f}（截至 {row['as_of_date']}）" if earlier
                else f"最新值 {row['value']:,.0f}（截至 {row['as_of_date']}，暂无历史对比）")
        flows.append({"name": label, "change": change, "scale": row["scale"],
                      "as_of": row["as_of_date"], "stale": age_days(row["as_of_date"]) > 30,
                      "text": text})
    market["flows"] = flows
    fresh_flows = [f for f in flows if not f["stale"] and f["change"] != 0.0]
    if fresh_flows:
        inputs["flows"] = fresh_flows

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
        "fedwatch": {"text": "下次会议降息概率 68.0%（较 7 天前 上升 10.0 个百分点）",
                     "cut_probability": 68.0, "change_pp": 10.0,
                     "compared_with": "7天前", "as_of": today},
        "events": [{"name": "CPI 同比", "actual": 2.7, "expected": 2.8,
                    "release_date": today, "surprise": -0.1}],
        "flows": [{"name": "COT净多头", "change": 18000, "scale": 30000,
                   "text": f"变化 +18,000（截至 {today}）", "stale": False}],
        "calendar": [],
    }
    inputs = {
        "fed_policy": {"change_pp": 10.0, "stale": False},
        "real_rate": {"delta_bp": -12.0, "stale": False},
        "dollar": {"pct": -0.70, "stale": False},
        "econ_data": [{"name": "CPI 同比", "surprise": -0.1, "scale": 0.3, "stale": False}],
        "flows": [{"name": "COT净多头", "change": 18000, "scale": 30000, "stale": False}],
    }
    return market, inputs


def run_once(db: Database | None = None, *, dry_run: bool = False, send: bool = False,
             demo: bool = False, collect_data: bool = True) -> dict:
    db = db or Database()
    db.init_schema()

    if demo:
        market, inputs = demo_market()
        collection = {}
    else:
        collection = collect(db) if collect_data else {}
        if collection:
            print("[fred] 采集结果：\n" + describe_collection(collection))
        market, inputs = build_market(db)

    score = compute_score(inputs)
    analysis = generate_analysis(score, market)
    report = render_daily_report(score, analysis, market)
    print("\n" + report + "\n")

    result = {"score": score, "analysis": analysis, "report": report, "sent": []}

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
