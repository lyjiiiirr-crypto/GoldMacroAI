"""六类推送中的变化触发型提醒规则（晨报由 scheduler 单独发送）。"""
from __future__ import annotations

from dataclasses import dataclass

from config import ALERT_COOLDOWN_HOURS


@dataclass
class Alert:
    kind: str
    title: str
    body: str
    fingerprint: str


def evaluate(current, previous, market: dict | None = None) -> list[Alert]:
    """比较本次与上次报告，生成去重指纹后的提醒列表。"""
    market = market or {}
    alerts: list[Alert] = []

    # E. 宏观方向跨档变化
    if previous is not None:
        before, after = previous.bias, current.bias
        if before != after and current.sufficient:
            alerts.append(Alert(
                kind="bias_change",
                title="黄金宏观方向变化",
                body=f"宏观方向由「{before}」变为「{after}」；评分 {current.total:+.1f}/100，置信度 {current.confidence:.0%}。请重新评估持仓宏观背景，勿直接据此进场。",
                fingerprint=f"{before}->{after}",
            ))

    # B. 利率预期显著变化
    fed = market.get("fedwatch") or {}
    pp = fed.get("change_pp")
    if pp is not None and abs(pp) >= 8:
        alerts.append(Alert(
            kind="fedwatch_shift",
            title="降息预期显著变化",
            body=f"降息概率 {fed.get('cut_probability')}% ，较 {fed.get('compared_with')} 变化 {pp:+.1f} 个百分点。",
            fingerprint=f"{fed.get('as_of')}:{pp:+.0f}",
        ))

    # C. 实际利率 / 美元异动
    rate = (market.get("DFII10") or {}).get("delta")
    if rate is not None and abs(rate) >= 0.10:
        alerts.append(Alert(
            kind="real_rate_move",
            title="10年期实际收益率异动",
            body=f"DFII10 变化 {rate * 100:+.1f} bp，需关注对黄金估值的影响。",
            fingerprint=f"rate:{(market['DFII10']['date'])}:{rate:+.2f}",
        ))
    dollar = (market.get("DTWEXBGS") or {}).get("pct")
    if dollar is not None and abs(dollar) >= 0.8:
        alerts.append(Alert(
            kind="dollar_move",
            title="美元广义指数异动",
            body=f"DTWEXBGS 变化 {dollar:+.2f}%，注意美元方向对黄金的压制/支撑。",
            fingerprint=f"dxy:{(market['DTWEXBGS']['date'])}:{dollar:+.2f}",
        ))

    # D. 重大经济数据意外
    for event in (market.get("events") or [])[:3]:
        surprise = float(event.get("surprise", 0.0))
        if abs(surprise) >= float(event.get("scale", 0.3)) * 0.6:
            alerts.append(Alert(
                kind="data_surprise",
                title=f"{event['name']} 显著偏离预期",
                body=f"实际 {event['actual']}，预期 {event['expected']}，意外值 {surprise:+.2f}。注意实际利率与美元的后续反应。",
                fingerprint=f"{event['name']}:{event.get('release_date')}:{event.get('actual')}",
            ))
    return alerts


def send_alerts(alerts: list[Alert], db, send_fn) -> list[str]:
    """通过 db 去重后调用 send_fn(title, body)，返回实际发送的题目。"""
    sent = []
    for alert in alerts:
        if db.can_send_alert(alert.kind, alert.fingerprint, ALERT_COOLDOWN_HOURS):
            if send_fn(alert.title, alert.body):
                sent.append(alert.title)
    return sent
