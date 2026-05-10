import streamlit as st
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import database as db
import sync as syn

st.set_page_config(page_title="告警", page_icon="🚨", layout="wide")
st.title("🚨 告警 / 人工处理队列")

# 告警类型中文映射
_ALERT_TYPE_MAP = {
    "popup_unknown": "未知弹窗",
}


def zh_alert_type(t):
    return _ALERT_TYPE_MAP.get(t, t) if t else "—"
st.caption(
    "Worker 探测到未知卡住状态会暂停并上报。"
    "处理流程:看截图 → 在 worker/popup_handler.py 加规则 → "
    "「更新该 Worker 代码」(Workers 页) → 在这里点「让 Worker 继续」。"
)

show_resolved = st.checkbox("显示已处理")
alerts = db.list_alerts(only_unresolved=not show_resolved, limit=200)

if not alerts:
    st.success("✅ 没有待处理告警")
else:
    workers_by_id = {w["id"]: w for w in db.list_workers()}
    for a in alerts:
        with st.container(border=True):
            cols = st.columns([4, 1])
            with cols[0]:
                st.markdown(
                    f"**{a.get('worker_nickname', '?')}** &nbsp; "
                    f"类型 `{zh_alert_type(a.get('type'))}` &nbsp; 任务 ID {a.get('task_id') or '-'}"
                )
                st.caption(f"上报时间:{a['created_at']}")
                if a.get("message"):
                    st.write(a["message"])
                shot = a.get("screenshot_path")
                if shot and Path(shot).exists():
                    st.image(shot, caption="弹窗截图(原尺寸)", use_container_width=True)
                    try:
                        with open(shot, "rb") as fh:
                            st.download_button(
                                "📥 下载原图",
                                data=fh.read(),
                                file_name=Path(shot).name,
                                mime="image/png",
                                key=f"dl_{a['id']}",
                            )
                    except Exception:
                        pass
                elif shot:
                    st.caption(f"截图未同步到中控:{shot}")
            with cols[1]:
                if not a["resolved"]:
                    if st.button("标记已处理", key=f"resolve_{a['id']}"):
                        db.resolve_alert(a["id"])
                        st.rerun()
                    if st.button("▶ 让 Worker 继续", key=f"resume_{a['id']}",
                                 type="primary", help="发 resume 命令到 worker"):
                        w = workers_by_id.get(a["worker_id"])
                        if not w:
                            st.error("找不到对应 Worker")
                        else:
                            try:
                                if a.get("task_id"):
                                    db.update_task(a["task_id"], status="scheduled")
                                syn.push_command(w, "resume")
                                db.resolve_alert(a["id"])
                                st.success("已通知 Worker 继续")
                                st.rerun()
                            except Exception as e:
                                st.error(str(e))
                else:
                    st.caption("已处理")
