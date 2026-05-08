"""推特矩阵搬运系统 V2.0 - 可视化主程序

三大 Tab：
  1. 全局中控台 — 转码/休息时间、解析模式、视频格式 等全局参数
  2. 矩阵账号库 — 多账号配置 + 类型分类
  3. 任务指挥所 — 队列录入、实时控制台、休息倒计时、自动接续

后台 daemon worker 线程串行消费 task_queue：
  pending → running → success/failed → 进入 rest_time 倒计时 → 接续下一个
"""
import html
import json
import threading
import time

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

import database as db
from auto_tweet_engine import download_video, post_to_twitter


# ============================================================
# 后台 worker 线程 — 模块级单例（跨 Streamlit rerun 保持）
# ============================================================
_worker_lock = threading.Lock()
_worker_thread = None
_worker_state = {
    "phase": "idle",                # idle | running | resting
    "current_task_id": None,
    "rest_until": 0.0,
    "rest_total_seconds": 0,
    "last_error": None,
    "started_at": None,
}


def _build_proxy_config(account):
    return {
        "host": account["proxy_host"],
        "port": account["proxy_port"],
        "user": account.get("proxy_user") or "",
        "pass": account.get("proxy_pass") or "",
    }


def _safe_int(value, default):
    try:
        return int(value)
    except Exception:
        return default


def _process_one_task(task):
    """完整执行一个任务：下载 + 发推。所有日志写回 DB。"""
    task_id = task["id"]
    db.update_task_status(task_id, db.TASK_RUNNING, mark_started=True)
    db.append_task_log(task_id, f"=== 任务 #{task_id} 开始执行 ===")

    account = db.get_account_by_id(task["account_id"])
    if not account:
        db.append_task_log(task_id, "✗ 关联账号已被删除，任务终止")
        db.update_task_status(task_id, db.TASK_FAILED, mark_finished=True)
        return

    db.append_task_log(
        task_id,
        f"账号: {account['remark']}  类型: {account.get('account_type') or '通用'}",
    )
    db.append_task_log(
        task_id,
        f"代理: {account['proxy_host']}:{account['proxy_port']}",
    )

    settings = db.get_all_settings()
    upload_wait = _safe_int(settings.get("upload_wait_time"), 15)
    parse_mode = settings.get("parse_mode") or "regular"
    video_format = settings.get("video_format") or "best"

    try:
        cookie_data = json.loads(account["cookie"])
        if not isinstance(cookie_data, list):
            raise ValueError("Cookie 不是 JSON 数组")
    except Exception as e:
        db.append_task_log(task_id, f"✗ Cookie 解析失败: {e}")
        db.update_task_status(task_id, db.TASK_FAILED, mark_finished=True)
        return

    proxy_config = _build_proxy_config(account)

    def push_log(msg, _id=task_id):
        db.append_task_log(_id, msg)

    # === 阶段 1：下载 ===
    try:
        ok_dl, dl_msg = download_video(
            task["video_url"],
            proxy_config,
            account["user_agent"],
            parse_mode=parse_mode,
            video_format=video_format,
            log_callback=push_log,
        )
    except Exception as e:
        ok_dl, dl_msg = False, f"下载阶段抛出未捕获异常: {type(e).__name__}: {e}"
        push_log(f"✗ {dl_msg}")

    if not ok_dl:
        db.append_task_log(task_id, f"✗ 下载阶段失败: {dl_msg}")
        db.update_task_status(task_id, db.TASK_FAILED, mark_finished=True)
        return

    # === 阶段 2：发推 ===
    try:
        ok_post, post_msg = post_to_twitter(
            task["tweet_caption"],
            proxy_config,
            account["user_agent"],
            cookie_data,
            upload_wait_time_minutes=upload_wait,
            log_callback=push_log,
        )
    except Exception as e:
        ok_post, post_msg = False, f"发推阶段抛出未捕获异常: {type(e).__name__}: {e}"
        push_log(f"✗ {post_msg}")

    if ok_post:
        db.append_task_log(task_id, f"✓ {post_msg}")
        db.update_task_status(task_id, db.TASK_SUCCESS, mark_finished=True)
    else:
        db.append_task_log(task_id, f"✗ 发推阶段失败: {post_msg}")
        db.update_task_status(task_id, db.TASK_FAILED, mark_finished=True)


def _worker_loop():
    """daemon 主循环。"""
    while True:
        try:
            task = db.get_next_pending_task()
            if not task:
                _worker_state["phase"] = "idle"
                _worker_state["current_task_id"] = None
                time.sleep(3)
                continue

            _worker_state["phase"] = "running"
            _worker_state["current_task_id"] = task["id"]
            _worker_state["started_at"] = time.time()

            _process_one_task(task)

            _worker_state["current_task_id"] = None

            # 休息阶段
            settings = db.get_all_settings()
            rest_minutes = _safe_int(settings.get("task_rest_time"), 30)
            db.append_task_log(
                task["id"],
                f"=== 任务结束，进入 {rest_minutes} 分钟休息倒计时 ===",
            )
            _worker_state["phase"] = "resting"
            _worker_state["rest_total_seconds"] = max(0, rest_minutes * 60)
            _worker_state["rest_until"] = time.time() + _worker_state["rest_total_seconds"]

            target = _worker_state["rest_until"]
            while time.time() < target:
                # 小颗粒 sleep，便于将来支持 abort
                time.sleep(min(5, max(0.5, target - time.time())))

            _worker_state["rest_until"] = 0.0
            _worker_state["phase"] = "idle"
        except Exception as e:
            _worker_state["last_error"] = f"{type(e).__name__}: {e}"
            _worker_state["phase"] = "idle"
            _worker_state["current_task_id"] = None
            time.sleep(5)


def ensure_worker_started():
    """保证后台 worker 仅启动一次。Streamlit rerun 不会重启。"""
    global _worker_thread
    with _worker_lock:
        if _worker_thread is None or not _worker_thread.is_alive():
            db.reset_running_tasks()  # 崩溃恢复：把上次卡死的"执行中"重置回"等待中"
            _worker_thread = threading.Thread(
                target=_worker_loop, daemon=True, name="tweet-matrix-worker"
            )
            _worker_thread.start()


# ============================================================
# UI 工具
# ============================================================
def _render_console(logs, height=380, key=""):
    """控制台风格日志容器（HTML iframe，自动滚动到底部）。"""
    safe = html.escape(logs or "(暂无日志，等待任务执行...)")
    html_doc = f"""
    <html><head><style>
        html, body {{ margin: 0; padding: 0; height: 100%; }}
        #c {{
            background: #0d1117;
            color: #c9d1d9;
            font-family: 'Consolas','Monaco','Courier New',monospace;
            font-size: 12.5px;
            line-height: 1.55;
            padding: 12px 14px;
            border: 1px solid #30363d;
            border-radius: 6px;
            height: calc(100% - 4px);
            overflow-y: auto;
            white-space: pre-wrap;
            word-break: break-all;
            box-sizing: border-box;
        }}
        #c::-webkit-scrollbar {{ width: 8px; }}
        #c::-webkit-scrollbar-thumb {{ background: #30363d; border-radius: 4px; }}
    </style></head>
    <body>
        <div id='c'>{safe}</div>
        <script>
            var el = document.getElementById('c');
            if (el) el.scrollTop = el.scrollHeight;
        </script>
    </body></html>
    """
    components.html(html_doc, height=height, scrolling=False)


def _format_countdown(seconds):
    seconds = max(0, int(seconds))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


# ============================================================
# 启动
# ============================================================
st.set_page_config(page_title="推特矩阵搬运 V2.0 专业版", page_icon="🚀", layout="wide")
db.init_db()
ensure_worker_started()

st.title("🚀 推特矩阵搬运系统 V2.0 专业版")
st.caption("多账号矩阵 · 多源抓取 · 工业级长时挂机")

tab_ctrl, tab_acct, tab_task = st.tabs([
    "🛠️ 全局中控台",
    "👥 矩阵账号库",
    "📡 任务指挥所",
])


# ============================================================
# Tab 1：全局中控台
# ============================================================
with tab_ctrl:
    st.subheader("⚙️ 全局执行参数")

    settings = db.get_all_settings()

    col_a, col_b = st.columns(2)
    with col_a:
        st.markdown("**视频上传转码等待时间**（分钟）")
        st.caption("视频上传 X 平台后等待转码完成的最长时间。视频越大需要越久。")
        new_upload = st.slider(
            "upload_wait_time",
            min_value=1, max_value=60,
            value=_safe_int(settings.get("upload_wait_time"), 15),
            step=1, label_visibility="collapsed",
            key="ui_upload_wait",
        )
    with col_b:
        st.markdown("**单任务结束后休眠时间**（分钟）")
        st.caption("两个任务之间的冷却时间，降低被风控概率。可设为 0 表示不休息。")
        new_rest = st.slider(
            "task_rest_time",
            min_value=0, max_value=180,
            value=_safe_int(settings.get("task_rest_time"), 30),
            step=1, label_visibility="collapsed",
            key="ui_rest",
        )

    st.markdown("---")
    st.subheader("🎬 视频抓取高级设置")
    col_c, col_d = st.columns(2)
    with col_c:
        cur_mode = settings.get("parse_mode") or "regular"
        new_mode = st.radio(
            "解析模式",
            options=["regular", "sniff"],
            format_func=lambda v: (
                "🔧 常规模式（yt-dlp 直接抓取）"
                if v == "regular"
                else "🕵️ 深度嗅探模式（Playwright 拦截 m3u8）"
            ),
            index=0 if cur_mode == "regular" else 1,
            key="ui_parse_mode",
        )
        st.caption(
            "深度嗅探：先用无头浏览器加载页面 → 拦截 .m3u8/.mpd 网络请求 → 再交给 yt-dlp 拉流。"
            "适合 yt-dlp 直接打不开的复杂站点。"
        )
    with col_d:
        cur_fmt = settings.get("video_format") or "best"
        fmt_options = ["best", "mp4", "1080p", "720p"]
        new_fmt = st.selectbox(
            "视频格式偏好",
            options=fmt_options,
            index=fmt_options.index(cur_fmt) if cur_fmt in fmt_options else 0,
            key="ui_video_format",
        )
        st.caption("best=最高质量；mp4=兼容性优先；720p/1080p=限制分辨率以加快下载。")

    if st.button("💾 保存全局设置", type="primary", use_container_width=True):
        db.set_setting("upload_wait_time", new_upload)
        db.set_setting("task_rest_time", new_rest)
        db.set_setting("parse_mode", new_mode)
        db.set_setting("video_format", new_fmt)
        st.success("全局设置已保存。下一个任务即生效。")
        st.rerun()

    st.markdown("---")
    st.subheader("🩺 引擎运行状态")
    phase = _worker_state["phase"]
    phase_label = {
        "idle": "🟢 空闲（等待新任务）",
        "running": "🟡 运行中（正在执行任务）",
        "resting": "🔵 休息中（任务间冷却）",
    }.get(phase, "❔ 未知")

    col_s1, col_s2, col_s3 = st.columns(3)
    col_s1.metric("引擎状态", phase_label)
    col_s2.metric(
        "工作线程",
        "存活" if (_worker_thread and _worker_thread.is_alive()) else "未启动",
    )
    if phase == "running" and _worker_state.get("started_at"):
        elapsed = int(time.time() - _worker_state["started_at"])
        col_s3.metric("当前任务已运行", _format_countdown(elapsed))
    elif phase == "resting":
        remaining = max(0, int(_worker_state["rest_until"] - time.time()))
        col_s3.metric("距离下个任务", _format_countdown(remaining))
    else:
        col_s3.metric("当前任务", "—")

    if _worker_state["last_error"]:
        st.error(f"上次循环错误：{_worker_state['last_error']}")


# ============================================================
# Tab 2：矩阵账号库
# ============================================================
with tab_acct:
    st.subheader("👥 矩阵账号库")

    with st.expander("➕ 新增账号", expanded=False):
        with st.form("add_account_form_v2", clear_on_submit=True):
            col1, col2 = st.columns(2)
            with col1:
                in_remark = st.text_input("账号备注 *", placeholder="例如：美食号_01")
                in_account_type = st.text_input(
                    "账号类型/定位 *",
                    placeholder="例如：美食 / 游戏 / 财经 / 娱乐",
                )
                in_proxy_host = st.text_input("代理 IP *", placeholder="127.0.0.1")
                in_proxy_port = st.text_input("代理端口 *", placeholder="1080")
            with col2:
                in_proxy_user = st.text_input("代理账号", placeholder="可选")
                in_proxy_pass = st.text_input("代理密码", placeholder="可选", type="password")

            in_user_agent = st.text_area(
                "User-Agent *",
                height=80,
                placeholder="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ...",
            )
            in_cookie = st.text_area(
                "Cookie（Playwright JSON 数组格式）*",
                height=180,
                placeholder='[{"name":"auth_token","value":"...","domain":".x.com",...}]',
            )

            submit_acct = st.form_submit_button(
                "💾 保存账号", type="primary", use_container_width=True
            )

            if submit_acct:
                required = [
                    in_remark, in_account_type, in_proxy_host,
                    in_proxy_port, in_user_agent, in_cookie,
                ]
                if not all(s and s.strip() for s in required):
                    st.error("请填写所有带 * 的必填项。")
                else:
                    try:
                        parsed = json.loads(in_cookie)
                        if not isinstance(parsed, list):
                            raise ValueError("Cookie 必须是 JSON 数组（list）")
                    except Exception as e:
                        st.error(f"Cookie 格式错误：{e}")
                    else:
                        new_id = db.add_account(
                            remark=in_remark.strip(),
                            account_type=in_account_type.strip(),
                            proxy_host=in_proxy_host.strip(),
                            proxy_port=in_proxy_port.strip(),
                            proxy_user=in_proxy_user.strip(),
                            proxy_pass=in_proxy_pass.strip(),
                            user_agent=in_user_agent.strip(),
                            cookie=in_cookie.strip(),
                        )
                        st.success(f"账号添加成功（ID = {new_id}）")
                        st.rerun()

    accounts = db.get_all_accounts()
    if not accounts:
        st.info("暂无账号，请先在上方添加。")
    else:
        rows = []
        for a in accounts:
            ua = a["user_agent"]
            rows.append({
                "ID": a["id"],
                "备注": a["remark"],
                "类型": a.get("account_type") or "通用",
                "代理": f"{a['proxy_host']}:{a['proxy_port']}",
                "代理认证": "是" if (a.get("proxy_user") or "").strip() else "否",
                "User-Agent": (ua[:55] + "...") if len(ua) > 55 else ua,
                "Cookie 长度": len(a["cookie"]),
                "创建时间": a["created_at"],
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        with st.expander("🗑️ 删除账号"):
            del_id = st.selectbox(
                "选择要删除的账号",
                options=[a["id"] for a in accounts],
                format_func=lambda i: next(
                    f"#{a['id']} - {a['remark']} ({a.get('account_type') or '通用'})"
                    for a in accounts if a["id"] == i
                ),
                key="del_account_select",
            )
            if st.button("⚠️ 确认删除", type="primary", key="del_account_btn"):
                db.delete_account(del_id)
                st.success(f"已删除账号 #{del_id}")
                st.rerun()


# ============================================================
# Tab 3：任务指挥所
# ============================================================
with tab_task:
    st.subheader("📡 任务指挥所")

    accounts_for_task = db.get_all_accounts()

    if not accounts_for_task:
        st.warning("尚未配置任何账号，请先在『矩阵账号库』中添加。")
    else:
        # ---- 任务录入 ----
        with st.expander("➕ 录入新任务", expanded=True):
            with st.form("add_task_form", clear_on_submit=True):
                opts = {
                    f"#{a['id']} - {a['remark']} ({a.get('account_type') or '通用'})": a["id"]
                    for a in accounts_for_task
                }
                in_acct_label = st.selectbox("选择目标账号", list(opts.keys()))
                in_video_url = st.text_input(
                    "视频源 URL",
                    placeholder="https://www.youtube.com/watch?v=... / https://任意可被解析的视频页",
                )
                in_caption = st.text_area(
                    "推文文案",
                    height=120,
                    placeholder="支持 \\n 换行；可加 #话题 @用户 等。",
                )
                add_task_submit = st.form_submit_button(
                    "➤ 加入任务队列", type="primary", use_container_width=True
                )

                if add_task_submit:
                    if not in_video_url.strip() or not in_caption.strip():
                        st.error("视频 URL 与推文文案不可为空。")
                    else:
                        new_task_id = db.add_task(
                            account_id=opts[in_acct_label],
                            video_url=in_video_url.strip(),
                            tweet_caption=in_caption.strip(),
                        )
                        st.success(
                            f"任务 #{new_task_id} 已加入队列。"
                            f"{'引擎将立即拾取。' if _worker_state['phase'] == 'idle' else '完成当前任务后顺序执行。'}"
                        )
                        st.rerun()

        # ---- 监控控制条 ----
        st.markdown("---")
        col_r1, col_r2, col_r3, col_r4 = st.columns([1.2, 1, 1.5, 4])
        with col_r1:
            auto_refresh = st.toggle(
                "🔄 自动刷新 (2s)",
                value=True,
                key="auto_refresh",
                help="开启后页面每 2 秒自动刷新一次以拉取最新日志",
            )
        with col_r2:
            if st.button("🔃 立即刷新", use_container_width=True):
                st.rerun()
        with col_r3:
            if st.button("🧹 清理已完成任务", use_container_width=True):
                n = db.clear_finished_tasks()
                st.success(f"已清理 {n} 条已完成任务")
                st.rerun()

        # ---- KPI 仪表盘 ----
        all_tasks = db.get_tasks(limit=200)
        n_pending = sum(1 for t in all_tasks if t["status"] == db.TASK_PENDING)
        n_running = sum(1 for t in all_tasks if t["status"] == db.TASK_RUNNING)
        n_success = sum(1 for t in all_tasks if t["status"] == db.TASK_SUCCESS)
        n_failed = sum(1 for t in all_tasks if t["status"] == db.TASK_FAILED)

        kpi1, kpi2, kpi3, kpi4 = st.columns(4)
        kpi1.metric("⏳ 等待中", n_pending)
        kpi2.metric("⚙️ 执行中", n_running)
        kpi3.metric("✅ 成功", n_success)
        kpi4.metric("❌ 失败", n_failed)

        # ---- 实时控制台 ----
        st.markdown("##### 🖥️ 实时控制台")
        cur_id = _worker_state.get("current_task_id")

        if cur_id:
            cur_task = db.get_task(cur_id)
            if cur_task:
                url_short = (cur_task["video_url"][:90] + "...") if len(cur_task["video_url"]) > 90 else cur_task["video_url"]
                st.markdown(
                    f"**🟡 任务 #{cur_id}** · 账号：`{cur_task.get('account_remark') or '?'}` "
                    f"· URL：`{url_short}`"
                )
                _render_console(cur_task.get("log_messages") or "", height=380, key=f"live-{cur_id}")
        elif _worker_state["phase"] == "resting":
            remaining = max(0, int(_worker_state["rest_until"] - time.time()))
            total = max(1, _worker_state["rest_total_seconds"])
            st.info(
                f"⏱️ 任务间休息中，倒计时 **{_format_countdown(remaining)}** 后自动执行下一个任务"
            )
            progress = 1 - (remaining / total) if total else 1
            st.progress(min(1.0, max(0.0, progress)))
            # 顺便展示上一个任务的日志
            recent = [t for t in all_tasks if t["status"] in (db.TASK_SUCCESS, db.TASK_FAILED)]
            if recent:
                last = recent[0]
                st.caption(f"上一个任务 #{last['id']} 状态：{last['status']}")
                _render_console(last.get("log_messages") or "", height=300, key=f"last-{last['id']}")
        else:
            if n_pending > 0:
                st.info("队列中有等待任务，引擎即将拾取...")
            else:
                st.info("当前空闲，无任务执行。在上方录入任务后会自动开始。")
            _render_console("", height=180, key="empty")

        # ---- 任务总览 ----
        st.markdown("##### 📋 任务队列总览")
        if not all_tasks:
            st.caption("暂无任务记录。")
        else:
            status_emoji = {
                db.TASK_PENDING: "⏳",
                db.TASK_RUNNING: "⚙️",
                db.TASK_SUCCESS: "✅",
                db.TASK_FAILED: "❌",
            }
            list_rows = []
            for t in all_tasks:
                list_rows.append({
                    "ID": t["id"],
                    "状态": f"{status_emoji.get(t['status'], '?')} {t['status']}",
                    "账号": t.get("account_remark") or "(已删除)",
                    "类型": t.get("account_type") or "—",
                    "URL": (t["video_url"][:55] + "...") if len(t["video_url"]) > 55 else t["video_url"],
                    "文案": (t["tweet_caption"][:35] + "...") if len(t["tweet_caption"]) > 35 else t["tweet_caption"],
                    "入队": t.get("created_at") or "",
                    "完成": t.get("finished_at") or "",
                })
            st.dataframe(pd.DataFrame(list_rows), use_container_width=True, hide_index=True)

            with st.expander("🔍 查看任务详细日志 / 删除任务"):
                view_id = st.selectbox(
                    "选择任务",
                    options=[t["id"] for t in all_tasks],
                    format_func=lambda i: next(
                        f"#{t['id']} [{t['status']}] {t.get('account_remark') or '?'}"
                        for t in all_tasks if t["id"] == i
                    ),
                    key="task_log_view",
                )
                t_view = db.get_task(view_id)
                if t_view:
                    st.markdown(f"**视频 URL**：`{t_view['video_url']}`")
                    st.markdown("**推文文案**：")
                    st.code(t_view["tweet_caption"], language=None)
                    st.markdown("**执行日志**：")
                    _render_console(
                        t_view.get("log_messages") or "",
                        height=380,
                        key=f"view-{view_id}",
                    )
                    if t_view["status"] in (db.TASK_SUCCESS, db.TASK_FAILED, db.TASK_PENDING):
                        if st.button(
                            f"🗑️ 删除任务 #{view_id}",
                            key=f"del_task_{view_id}",
                            type="secondary",
                        ):
                            db.delete_task(view_id)
                            st.success(f"已删除任务 #{view_id}")
                            st.rerun()

        # ---- 自动刷新（仅在有活动时触发，避免静态空闲下也每秒重跑）----
        should_refresh = auto_refresh and (
            _worker_state["phase"] != "idle" or n_pending > 0 or n_running > 0
        )
        if should_refresh:
            time.sleep(2)
            st.rerun()
