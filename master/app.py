import streamlit as st
import os
import database as db
from datetime import datetime

st.set_page_config(page_title="推特矩阵中控", page_icon="🐦", layout="wide")
db.init_db()

# 简单密码登录(只在设了 TWMATRIX_UI_PASSWORD 环境变量时启用)
_PASSWORD = os.getenv("TWMATRIX_UI_PASSWORD", "").strip()
if _PASSWORD:
    if not st.session_state.get("authed"):
        st.title("🔒 推特矩阵中控")
        with st.form("login"):
            pwd = st.text_input("登录密码", type="password")
            if st.form_submit_button("登录", type="primary"):
                if pwd == _PASSWORD:
                    st.session_state["authed"] = True
                    st.rerun()
                else:
                    st.error("密码不对")
        st.stop()

st.title("推特矩阵中控")
st.caption(f"快照时间:{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

workers = db.list_workers()
counts = db.task_counts_by_worker()
unresolved = db.list_alerts(only_unresolved=True, limit=999)

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Worker 总数", len(workers))
c2.metric("空闲", sum(1 for w in workers if w["status"] == "idle"))
c3.metric("运行中", sum(1 for w in workers if w["status"] == "running"))
c4.metric("待人工处理", len(unresolved))
c5.metric("出错", sum(1 for w in workers if w["status"] in ("error", "human_required", "paused")))

st.divider()
st.subheader("Worker 状态")

if not workers:
    st.info("还没有添加 Worker。在左侧 Workers 页面添加第一个,或先去 Proxies 页录代理。")
else:
    for w in workers:
        with st.container(border=True):
            cols = st.columns([2, 2, 1, 2, 2])

            cols[0].markdown(f"**{w['nickname']}**")
            cols[0].caption(f"@{w.get('twitter_handle') or '-'}")

            cols[1].caption(f"VPS `{w['vps_host']}:{w['ssh_port']}`")
            cols[1].caption(f"代理 `{w.get('proxy_host', '-')}:{w.get('proxy_port', '-')}`")

            status = w["status"]
            color = {
                "idle": "🟢", "running": "🔵", "human_required": "🔴",
                "error": "🔴", "paused": "🟡", "provisioning": "🟡",
                "pending": "⚪",
            }.get(status, "⚪")
            cols[2].markdown(f"{color} `{status}`")
            cols[2].caption(w.get("last_heartbeat") or "未上线")

            t = counts.get(w["id"], {})
            cols[3].caption(
                f"待:{t.get('pending', 0)} | 计:{t.get('scheduled', 0)} | "
                f"跑:{t.get('running', 0)} | 完:{t.get('done', 0)} | 败:{t.get('failed', 0)}"
            )

            quota = w.get("traffic_quota_gb") or 1
            used = w.get("traffic_used_gb") or 0
            pct = min(100, (used / quota * 100)) if quota else 0
            cols[4].progress(pct / 100, text=f"流量 {used:.1f}/{quota} G ({pct:.0f}%)")

st.divider()

action_cols = st.columns(4)
if action_cols[0].button("🔄 刷新", type="primary"):
    st.rerun()

if action_cols[1].button("🗓️ 重排今日任务"):
    import scheduler as sched
    with st.spinner("重排中..."):
        result = sched.plan_today_for_all()
    msg = " | ".join(f"W{wid}:{n}" for wid, n in result.items()) or "(无 Worker)"
    st.success(f"已重排:{msg}")

if action_cols[2].button("📤 推送任务到全部"):
    import sync as syn
    import scheduler as sched
    ok = 0; fail = 0
    with st.spinner("推送中..."):
        for w in db.list_workers():
            try:
                syn.push_config(w)
                tasks = sched.get_today_active_tasks(w["id"])
                syn.push_tasks(w, tasks)
                ok += 1
            except Exception as e:
                fail += 1
                st.error(f"W{w['id']} {w['nickname']}: {e}")
    st.success(f"成功 {ok},失败 {fail}")

if action_cols[3].button("📡 同步全部状态"):
    import sync as syn
    ok = 0; fail = 0
    with st.spinner("同步中..."):
        for w in db.list_workers():
            try:
                r = syn.sync_worker(w)
                if r["ok"]:
                    ok += 1
                else:
                    fail += 1
            except Exception:
                fail += 1
    st.success(f"成功 {ok},失败 {fail}")
    st.rerun()
