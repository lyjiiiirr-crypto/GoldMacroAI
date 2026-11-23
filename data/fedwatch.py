"""CME FedWatch 概率快照管理。

官方 FedWatch API 为付费服务，V1.0 采用人工快照：在 CME 页面查看后录入，
系统只计算快照之间的变化，不做未授权网页抓取。
"""
from __future__ import annotations

from datetime import date


def change_pp(snapshots: list[dict], days: int = 7) -> dict | None:
    """返回降息概率 N 天变化（百分点）；数据不足返回 None。"""
    if not snapshots:
        return None
    snapshots = sorted(snapshots, key=lambda r: str(r["snapshot_date"]), reverse=True)
    latest = snapshots[0]
    try:
        target = date.fromisoformat(str(latest["snapshot_date"])).toordinal() - days
    except (ValueError, TypeError):
        return None
    older = next((s for s in snapshots[1:] if date.fromisoformat(str(s["snapshot_date"])).toordinal() <= target), None)
    if older is None:
        return None
    cut_now, cut_old = latest.get("cut_probability"), older.get("cut_probability")
    if cut_now is None or cut_old is None:
        return None
    return {
        "as_of": latest["snapshot_date"],
        "cut_probability": float(cut_now),
        "hold_probability": latest.get("hold_probability"),
        "hike_probability": latest.get("hike_probability"),
        "meeting_date": latest.get("meeting_date", ""),
        "change_pp": round(float(cut_now) - float(cut_old), 2),
        "compared_with": older["snapshot_date"],
    }


def format_snapshot(info: dict | None) -> str:
    if not info:
        return "无 FedWatch 快照（请用 --import-fedwatch 录入）"
    direction = "上升" if info["change_pp"] > 0 else ("下降" if info["change_pp"] < 0 else "持平")
    return (f"下次会议降息概率 {info['cut_probability']:.1f}% "
            f"（较 {info['compared_with']} {direction} {abs(info['change_pp']):.1f} 个百分点）")
