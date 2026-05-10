import streamlit as st
import sys
import pandas as pd
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import database as db

st.set_page_config(page_title="运行日志", page_icon="📜", layout="wide")
st.title("📜 运行日志")

workers = db.list_workers()
opt = ["全部"] + [w["nickname"] for w in workers]
nick = st.selectbox("Worker", opt)
worker_id = None if nick == "全部" else next(w["id"] for w in workers if w["nickname"] == nick)

limit = st.slider("显示条数", 50, 2000, 500, step=50)

# 自动刷新开关
c1, c2 = st.columns([1, 3])
with c1:
    auto_refresh = st.toggle("🔄 自动刷新 (5秒)", value=True, key="log_auto_refresh")
with c2:
    if st.button("🔃 立即刷新"):
        st.rerun()

logs = db.list_logs(worker_id=worker_id, limit=limit)

if logs:
    df = pd.DataFrame(logs)[["created_at", "worker_nickname", "level", "message"]]
    df.rename(columns={
        "created_at": "时间",
        "worker_nickname": "Worker",
        "level": "级别",
        "message": "内容",
    }, inplace=True)
    st.dataframe(df, use_container_width=True, hide_index=True, height=600)
else:
    st.info("没有日志。")

# 自动刷新
if auto_refresh:
    time.sleep(5)
    st.rerun()
