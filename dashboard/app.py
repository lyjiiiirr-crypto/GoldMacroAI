"""可选仪表盘：streamlit run dashboard/app.py（需先 pip install -r requirements-dashboard.txt）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import streamlit as st  # noqa: E402

from config import DB_PATH  # noqa: E402
from database.models import Database  # noqa: E402

st.set_page_config(page_title="GoldMacroAI 宏观仪表盘", layout="wide")
st.title("GoldMacroAI · XAUUSD 宏观仪表盘")

db = Database(DB_PATH)
db.init_schema()
status = db.status()
st.caption(f"数据库：{status['db']}")

col1, col2, col3 = st.columns(3)
col1.metric("观测值条数", status["counts"]["observations"])
col2.metric("历史报告", status["counts"]["reports"])
col3.metric("提醒记录", status["counts"]["alert_log"])

st.subheader("历史评分")
reports = db.reports(limit=100)
if reports:
    st.line_chart([r["score"] for r in reversed(reports)])
    for report in reports[:10]:
        payload = report.get("payload", {})
        score = payload.get("score", {})
        with st.expander(f"{report['created_at']} · {report['bias']} · {report['score']:+.1f}"):
            st.write("置信度：", score.get("confidence"))
            st.write("信号：", score.get("signals", []))
            st.write("数据提示：", score.get("warnings", []))
else:
    st.info("尚无报告。先运行 python main.py --dry-run 或 --send-now。")

st.subheader("最新 FRED 观测值")
from config import SERIES  # noqa: E402

rows = []
for series_id, meta in SERIES.items():
    latest = db.observations(series_id, limit=1)
    if latest:
        rows.append({"序列": series_id, "名称": meta["name"], "日期": latest[0]["date"],
                     "数值": latest[0]["value"]})
st.dataframe(rows, use_container_width=True)
