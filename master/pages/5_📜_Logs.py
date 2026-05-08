import streamlit as st
import sys
import pandas as pd
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import database as db

st.set_page_config(page_title="日志", page_icon="📜", layout="wide")
st.title("📜 运行日志")

workers = db.list_workers()
opt = ["全部"] + [w["nickname"] for w in workers]
nick = st.selectbox("Worker", opt)
worker_id = None if nick == "全部" else next(w["id"] for w in workers if w["nickname"] == nick)

limit = st.slider("显示条数", 50, 2000, 500, step=50)
logs = db.list_logs(worker_id=worker_id, limit=limit)

if logs:
    df = pd.DataFrame(logs)[["created_at", "worker_nickname", "level", "message"]]
    st.dataframe(df, use_container_width=True, hide_index=True, height=600)
else:
    st.info("没有日志。")
