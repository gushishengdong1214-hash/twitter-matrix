import streamlit as st
import sys
import pandas as pd
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import database as db

st.set_page_config(page_title="任务录入", page_icon="📋", layout="wide")
st.title("📋 任务录入")
st.caption("一行一个任务,格式:`视频URL====文案`(用 4 个等号分隔,与现有脚本一致)")

workers = db.list_workers()
if not workers:
    st.warning("还没有 Worker。先去 Workers 页添加。")
    st.stop()

worker_id = st.selectbox(
    "归属 Worker",
    [w["id"] for w in workers],
    format_func=lambda x: next(
        f"{w['nickname']}  @{w.get('twitter_handle','-')}  来源:{w.get('source_site','-')}"
        for w in workers if w["id"] == x
    ),
)

raw = st.text_area(
    "批量录入",
    height=320,
    placeholder="https://jable.tv/xxx====这是文案1\nhttps://jable.tv/yyy====这是文案2",
)

c1, c2 = st.columns([1, 4])
with c1:
    submit = st.button("提交", type="primary")
with c2:
    st.caption("空行会自动跳过;格式错误的行会报错位置。")

if submit:
    items, bad = [], []
    for i, line in enumerate(raw.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        if "====" not in line:
            bad.append(i)
            continue
        url, _, caption = line.partition("====")
        url, caption = url.strip(), caption.strip()
        if not url or not caption:
            bad.append(i)
            continue
        items.append((url, caption))

    if bad:
        st.error(f"以下行格式错误:第 {bad} 行")
    if items:
        n = db.add_tasks_bulk(worker_id, items)
        st.success(f"已录入 {n} 条任务。")
        st.rerun()

st.divider()
st.subheader("当前 Worker 任务列表(最近 500 条)")

status_filter = st.selectbox(
    "筛选状态",
    ["全部", "pending", "scheduled", "running", "done", "failed", "human_required"],
)
tasks = db.list_tasks(
    worker_id=worker_id,
    status=None if status_filter == "全部" else status_filter,
    limit=500,
)
if tasks:
    df = pd.DataFrame(tasks)[
        ["id", "status", "video_url", "caption", "attempt",
         "scheduled_at", "started_at", "finished_at", "error_message"]
    ]
    st.dataframe(df, use_container_width=True, hide_index=True, height=500)
else:
    st.info("没有任务。")
