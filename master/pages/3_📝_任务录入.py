import streamlit as st
import sys
import pandas as pd
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import database as db
import scheduler as sched
from timezone_utils import to_beijing

# ========== 状态中文映射 ==========
_STATUS_MAP = {
    "pending": "等待中",
    "scheduled": "已排期",
    "running": "执行中",
    "done": "已完成",
    "failed": "失败",
    "human_required": "需人工处理",
}
_STATUS_REVERSE = {v: k for k, v in _STATUS_MAP.items()}


def zh_status(s):
    return _STATUS_MAP.get(s, s) if s else "—"


st.set_page_config(page_title="任务录入", page_icon="📋", layout="wide")
st.title("📋 任务录入")
st.caption(
    "一行一个任务。基本格式:`视频URL====文案`。需要 @ 别人时,用 `|` 表示换行,@ 各占一行。"
)
with st.expander("📖 格式示例(点开看)", expanded=False):
    st.code(
        """\
# 普通任务
https://jable.tv/aaa====今天分享这个,挺好的

# 同一条带 2 个 @,| 表示换行
https://jable.tv/bbb====聊聊这个|@elonmusk|@x

# 带 3 个 @
https://jable.tv/ccc====推荐|@friend1|@friend2|@friend3
""",
        language=None,
    )
    st.caption("第二条发到 X 上呈现:")
    st.code("聊聊这个\n@elonmusk\n@x", language=None)

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
    "批量录入任务",
    height=320,
    placeholder="https://jable.tv/xxx====文案1\nhttps://jable.tv/yyy====文案2|@user1|@user2",
)

c1, c2 = st.columns([1, 4])
with c1:
    submit = st.button("提交", type="primary")
with c2:
    st.caption("空行自动跳过;格式错误的行会标行号。")

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
        caption_lines = [seg.strip() for seg in caption.split("|") if seg.strip()]
        caption_final = "\n".join(caption_lines)
        items.append((url, caption_final))

    if bad:
        st.error(f"以下行格式错误:第 {bad} 行")
    if items:
        n = db.add_tasks_bulk(worker_id, items)
        # 提交成功后自动为该 worker 重排今日任务
        worker = next((w for w in workers if w["id"] == worker_id), None)
        w_nickname = worker["nickname"] if worker else f"W{worker_id}"
        planned = sched.plan_today_for_worker(worker) if worker else 0
        st.success(f"提交成功！已录入 {n} 条任务，自动为 {w_nickname} 排期 {planned} 条今日任务")
        st.rerun()

st.divider()
st.subheader("当前 Worker 任务列表(最近 500 条)")

status_filter = st.selectbox(
    "筛选状态",
    ["全部"] + list(_STATUS_MAP.values()),
)
tasks = db.list_tasks(
    worker_id=worker_id,
    status=None if status_filter == "全部" else _STATUS_REVERSE.get(status_filter),
    limit=500,
)
if tasks:
    df = pd.DataFrame(tasks)[
        ["id", "status", "video_url", "caption", "attempt",
         "scheduled_at", "started_at", "finished_at", "error_message"]
    ]
    df["status"] = df["status"].apply(zh_status)
    for col in ("scheduled_at", "started_at", "finished_at"):
        df[col] = df[col].apply(to_beijing)
    df.rename(columns={
        "id": "ID",
        "status": "状态",
        "video_url": "视频链接",
        "caption": "推文文案",
        "attempt": "尝试次数",
        "scheduled_at": "排期时间(北京)",
        "started_at": "开始时间(北京)",
        "finished_at": "完成时间(北京)",
        "error_message": "错误信息",
    }, inplace=True)
    st.dataframe(df, use_container_width=True, hide_index=True, height=500)

    # ───────────────────────────────────────────
    # 批量管理
    # ───────────────────────────────────────────
    st.divider()
    st.subheader("批量管理")

    col1, col2 = st.columns([1, 1])

    with col1:
        st.markdown("**🧹 清空队列**")
        clear_status_label = st.selectbox(
            "清空状态",
            ["全部"] + list(_STATUS_MAP.values()),
            key=f"clear_status_{worker_id}",
        )
        clear_status_key = None if clear_status_label == "全部" else _STATUS_REVERSE.get(clear_status_label)
        count = sum(1 for t in tasks if (clear_status_key is None or t.get("status") == clear_status_key))
        st.caption(f"当前该 Worker 此状态下共 {count} 条任务")
        if st.button("🧹 清空队列", key=f"btn_clear_{worker_id}", type="secondary"):
            if count == 0:
                st.warning("没有任务可清空")
            else:
                deleted = db.delete_tasks_by_status(worker_id, clear_status_key)
                st.success(f"已删除 {deleted} 条任务")
                st.rerun()

    with col2:
        st.markdown("**🗑️ 批量删除**")
        task_options = {
            t["id"]: f"ID:{t['id']} [{zh_status(t['status'])}] {t['video_url'][:50]}..."
            for t in tasks
        }
        selected_ids = st.multiselect(
            "选择要删除的任务",
            options=list(task_options.keys()),
            format_func=lambda x: task_options[x],
            key=f"batch_del_{worker_id}",
        )
        if selected_ids:
            st.caption(f"已选 {len(selected_ids)} 条")
            if st.button("🗑️ 批量删除选中任务", key=f"btn_del_{worker_id}", type="primary"):
                deleted = db.delete_tasks(selected_ids)
                st.success(f"已删除 {deleted} 条任务")
                st.rerun()
        else:
            st.caption("未选择任务")

else:
    st.info("没有任务。")
