"""CME FedWatch 概率快照的变化计算。

自动采集（data/fedwatch_auto.py）与人工导入（--import-fedwatch）共用本模块。
当加息概率可得时，优先使用"鹰鸽净变化"指标：
    dovish_pp = (cut_now - cut_old) - (hike_now - hike_old)
因为当前周期可能出现"降息概率不变、加息概率明显下降"的偏鸽shift，
单纯看降息概率会漏掉这一信号。
"""
from __future__ import annotations

from datetime import date


def change_pp(snapshots: list[dict], days: int = 7) -> dict | None:
    """比较最新快照与 N 天前快照；返回概率变化，数据不足返回 None。"""
    if not snapshots:
        return None
    snapshots = sorted(snapshots, key=lambda r: str(r["snapshot_date"]), reverse=True)
    latest = snapshots[0]
    try:
        target = date.fromisoformat(str(latest["snapshot_date"])).toordinal() - days
    except (ValueError, TypeError):
        return None
    older = next(
        (s for s in snapshots[1:]
         if date.fromisoformat(str(s["snapshot_date"])).toordinal() <= target),
        None,
    )
    if older is None:
        return None
    cut_now, cut_old = latest.get("cut_probability"), older.get("cut_probability")
    if cut_now is None or cut_old is None:
        return None

    change = round(float(cut_now) - float(cut_old), 2)
    info = {
        "as_of": latest["snapshot_date"],
        "cut_probability": float(cut_now),
        "hold_probability": latest.get("hold_probability"),
        "hike_probability": latest.get("hike_probability"),
        "meeting_date": latest.get("meeting_date", ""),
        "change_pp": change,
        "compared_with": older["snapshot_date"],
    }
    hike_now, hike_old = latest.get("hike_probability"), older.get("hike_probability")
    if hike_now is not None and hike_old is not None:
        dovish = round(change - (float(hike_now) - float(hike_old)), 2)
        info.update({
            "dovish_pp": dovish,
            "metric_pp": dovish,
            "metric_name": "鹰鸽净变化",
        })
    else:
        info.update({
            "dovish_pp": None,
            "metric_pp": change,
            "metric_name": "降息概率变化",
        })
    return info


def format_snapshot(info: dict | None) -> str:
    if not info:
        return "无 FedWatch 快照（自动采集失败时可用 --import-fedwatch 录入）"
    metric = float(info.get("metric_pp", info.get("change_pp", 0.0)) or 0.0)
    label = info.get("metric_name", "降息概率变化")
    direction = "上升" if metric > 0 else ("下降" if metric < 0 else "持平")
    parts = []
    cut = info.get("cut_probability")
    if cut is not None:
        parts.append(f"下次会议降息概率 {float(cut):.1f}%")
    hike = info.get("hike_probability")
    if hike is not None:
        parts.append(f"加息概率 {float(hike):.1f}%")
    parts.append(f"较 {info.get('compared_with', '上次快照')} {label} {direction} {abs(metric):.1f} 个百分点")
    return "，".join(parts)
