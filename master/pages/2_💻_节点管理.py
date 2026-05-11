import streamlit as st
import sys
import json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import database as db
import sync as syn
from ssh_client import test_connection
from timezone_utils import to_beijing

st.set_page_config(page_title="Workers", page_icon="💻", layout="wide")
st.title("💻 Workers (VPS 节点)")
st.caption("1 Worker = 1 VPS + 1 静态住宅代理 + 1 推特账号。")

# ========== 状态中文映射 ==========
_WORKER_STATUS_MAP = {
    "idle": "空闲",
    "running": "运行中",
    "paused": "已暂停",
    "error": "错误",
    "human_required": "需人工处理",
    "provisioning": "部署中",
    "pending": "待配置",
}


def zh_worker_status(s):
    return _WORKER_STATUS_MAP.get(s, s) if s else "—"

TIMEZONES = [
    ("美国 - 纽约(东部)", "America/New_York"),
    ("美国 - 芝加哥(中部)", "America/Chicago"),
    ("美国 - 丹佛(山地)", "America/Denver"),
    ("美国 - 洛杉矶(西部)", "America/Los_Angeles"),
    ("加拿大 - 多伦多", "America/Toronto"),
    ("加拿大 - 温哥华", "America/Vancouver"),
    ("英国 - 伦敦", "Europe/London"),
    ("法国 - 巴黎", "Europe/Paris"),
    ("德国 - 柏林", "Europe/Berlin"),
    ("荷兰 - 阿姆斯特丹", "Europe/Amsterdam"),
    ("日本 - 东京", "Asia/Tokyo"),
    ("韩国 - 首尔", "Asia/Seoul"),
    ("中国香港", "Asia/Hong_Kong"),
    ("中国台湾(台北)", "Asia/Taipei"),
    ("新加坡", "Asia/Singapore"),
    ("中国大陆(北京)", "Asia/Shanghai"),
    ("澳大利亚 - 悉尼", "Australia/Sydney"),
    ("巴西 - 圣保罗", "America/Sao_Paulo"),
]

LOCALES = [
    ("英语(美国)", "en-US"),
    ("英语(英国)", "en-GB"),
    ("英语(加拿大)", "en-CA"),
    ("英语(澳洲)", "en-AU"),
    ("日语", "ja-JP"),
    ("韩语", "ko-KR"),
    ("法语", "fr-FR"),
    ("德语", "de-DE"),
    ("西班牙语(西班牙)", "es-ES"),
    ("西班牙语(墨西哥)", "es-MX"),
    ("葡萄牙语(巴西)", "pt-BR"),
    ("意大利语", "it-IT"),
    ("中文(简体)", "zh-CN"),
    ("中文(繁体)", "zh-TW"),
    ("俄语", "ru-RU"),
    ("阿拉伯语", "ar"),
]

USER_AGENTS = {
    "Windows 10 Chrome": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Windows 11 Edge": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36 Edg/121.0.0.0",
    "macOS Chrome": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "macOS Safari": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "iPhone Safari": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1",
    "Android Chrome": "Mozilla/5.0 (Linux; Android 13; SM-S908U) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Mobile Safari/537.36",
}


def _ua_label_from_value(ua: str) -> str:
    for label, val in USER_AGENTS.items():
        if val == ua:
            return label
    return "自定义"


def _index_of(items: list, value, default: int = 0) -> int:
    try:
        return items.index(value)
    except (ValueError, TypeError):
        return default


def render_worker_form(proxies: list, worker: dict | None = None):
    """渲染添加 / 编辑表单。worker=None 表示新增。"""
    is_edit = worker is not None
    form_key = f"edit_worker_{worker['id']}" if is_edit else "add_worker"

    proxy_ids = [p["id"] for p in proxies]
    default_proxy_index = (
        _index_of(proxy_ids, worker.get("proxy_id")) if is_edit else 0
    )
    default_ua_label = _ua_label_from_value(worker.get("user_agent", "")) if is_edit \
                      else "Windows 10 Chrome"
    tz_values = [t[1] for t in TIMEZONES]
    locale_values = [l[1] for l in LOCALES]

    with st.form(form_key, clear_on_submit=not is_edit):
        c1, c2 = st.columns(2)
        nickname = c1.text_input("Worker 备注名", value=worker.get("nickname", "") if is_edit else "",
                                 placeholder="acc-01")
        twitter_handle = c2.text_input("推特账号 @",
                                       value=worker.get("twitter_handle", "") if is_edit else "",
                                       placeholder="my_handle")

        st.markdown("**VPS 连接**")
        v1, v2, v3 = st.columns([3, 1, 1])
        vps_host = v1.text_input("VPS IP / 域名",
                                 value=worker.get("vps_host", "") if is_edit else "")
        ssh_port = v2.number_input("SSH 端口", min_value=1, max_value=65535,
                                   value=int(worker.get("ssh_port") or 22) if is_edit else 22)
        ssh_user = v3.text_input("SSH 用户",
                                 value=worker.get("ssh_user", "root") if is_edit else "root")
        a1, a2 = st.columns(2)
        ssh_password = a1.text_input(
            "SSH 密码", type="password",
            value=worker.get("ssh_password", "") if is_edit else "",
            help="evoxt 给的 root 密码;用 key 就这里留空",
        )
        ssh_key_path = a2.text_input(
            "SSH key 路径(可选)",
            value=worker.get("ssh_key_path", "") if is_edit else "",
            help="只在你已有私钥文件时填,例:C:/Users/LL/.ssh/id_rsa",
        )

        st.markdown("**绑定**")
        b1, b2 = st.columns(2)
        proxy_id = b1.selectbox(
            "住宅代理", proxy_ids, index=default_proxy_index,
            format_func=lambda x: next(
                f"{p['nickname'] or '(无名)'}  {p['host']}:{p['port']}"
                for p in proxies if p["id"] == x
            ),
        )
        source_site = b2.text_input(
            "视频来源站点",
            value=worker.get("source_site", "") if is_edit else "",
            placeholder="例:jable.tv",
        )

        st.markdown("**推特账号(2FA / 重登备用)**")
        st.caption("Cookie 通常够用。买的号在新IP第一次登录可能要 2FA;填了下面这两个,worker 自动过 2FA。")
        l1, l2 = st.columns(2)
        account_password = l1.text_input(
            "账号密码", type="password",
            value=worker.get("account_password", "") if is_edit else "",
        )
        twofa_secret = l2.text_input(
            "2FA Secret(TOTP)",
            value=worker.get("twofa_secret", "") if is_edit else "",
            help="买号商家给的 TOTP 密钥,16-32 位大写字母+数字,例:JBSWY3DPEHPK3PXP",
        )

        st.markdown("**Cookie(从浏览器导出 x.com 的整段 JSON 数组)**")
        cookie_json = st.text_area(
            "Cookie JSON", height=120,
            value=worker.get("cookie_json", "") if is_edit else "",
            placeholder='[{"name":"auth_token", "value":"...", "domain":".x.com", ...}]',
        )

        st.markdown("**浏览器指纹**")
        st.caption("时区/语言尽量和住宅 IP 所在地匹配。比如住宅IP在纽约就选美国-纽约+英语美国。")
        ua_label = st.selectbox(
            "User Agent 预设",
            list(USER_AGENTS.keys()) + ["自定义"],
            index=_index_of(list(USER_AGENTS.keys()) + ["自定义"], default_ua_label),
        )
        ua_custom = st.text_input(
            "自定义 UA(只在上面选自定义时生效)",
            value=worker.get("user_agent", "") if is_edit and default_ua_label == "自定义" else "",
            placeholder="选自定义时填,否则忽略",
        )
        f1, f2, f3, f4 = st.columns(4)
        viewport_width = f1.number_input(
            "屏幕宽", min_value=800, max_value=3840,
            value=int(worker.get("viewport_width") or 1920) if is_edit else 1920,
        )
        viewport_height = f2.number_input(
            "屏幕高", min_value=600, max_value=2160,
            value=int(worker.get("viewport_height") or 1080) if is_edit else 1080,
        )
        timezone = f3.selectbox(
            "时区", tz_values,
            index=_index_of(tz_values, worker.get("timezone")) if is_edit else 0,
            format_func=lambda x: next(t[0] for t in TIMEZONES if t[1] == x),
        )
        locale = f4.selectbox(
            "语言", locale_values,
            index=_index_of(locale_values, worker.get("locale")) if is_edit else 0,
            format_func=lambda x: next(l[0] for l in LOCALES if l[1] == x),
        )

        st.markdown("**调度参数**")
        s1, s2, s3, s4 = st.columns(4)
        work_start = s1.text_input("工作开始", value=worker.get("work_start", "08:00") if is_edit else "08:00")
        work_end = s2.text_input("工作结束", value=worker.get("work_end", "23:30") if is_edit else "23:30")
        rest_min = s3.number_input(
            "间隔下限(分)", min_value=5,
            value=int(worker.get("rest_min_minutes") or 30) if is_edit else 30,
        )
        rest_max = s4.number_input(
            "间隔上限(分)", min_value=10,
            value=int(worker.get("rest_max_minutes") or 90) if is_edit else 90,
        )
        s5, s6 = st.columns(2)
        daily_target = s5.number_input(
            "每日任务数", min_value=1, max_value=20,
            value=int(worker.get("daily_target") or 8) if is_edit else 8,
        )
        traffic_quota_gb = s6.number_input(
            "流量配额(GB)", min_value=10,
            value=int(worker.get("traffic_quota_gb") or 1000) if is_edit else 1000,
        )

        if is_edit:
            cc1, cc2 = st.columns([1, 1])
            submit = cc1.form_submit_button("💾 保存修改", type="primary")
            cancel = cc2.form_submit_button("取消")
            if cancel:
                st.session_state.pop("editing_worker_id", None)
                st.rerun()
        else:
            submit = st.form_submit_button("➕ 添加", type="primary")

        if submit:
            errs = []
            if not nickname:
                errs.append("备注名必填")
            if not vps_host:
                errs.append("VPS Host 必填")
            if not (ssh_password or ssh_key_path):
                errs.append("SSH 密码 / Key 至少填一个")
            if rest_max <= rest_min:
                errs.append("间隔上限要大于下限")
            if cookie_json.strip():
                try:
                    json.loads(cookie_json)
                except Exception:
                    errs.append("Cookie 不是合法 JSON")

            if errs:
                for e in errs:
                    st.error(e)
                return

            user_agent = ua_custom.strip() if ua_label == "自定义" and ua_custom.strip() \
                         else USER_AGENTS.get(ua_label, USER_AGENTS["Windows 10 Chrome"])

            fields = dict(
                nickname=nickname,
                twitter_handle=twitter_handle,
                vps_host=vps_host,
                ssh_port=int(ssh_port),
                ssh_user=ssh_user,
                ssh_password=ssh_password,
                ssh_key_path=ssh_key_path,
                proxy_id=proxy_id,
                source_site=source_site,
                cookie_json=cookie_json,
                account_password=account_password,
                twofa_secret=twofa_secret,
                user_agent=user_agent,
                viewport_width=int(viewport_width),
                viewport_height=int(viewport_height),
                timezone=timezone,
                locale=locale,
                work_start=work_start,
                work_end=work_end,
                rest_min_minutes=int(rest_min),
                rest_max_minutes=int(rest_max),
                daily_target=int(daily_target),
                traffic_quota_gb=int(traffic_quota_gb),
            )
            try:
                if is_edit:
                    db.update_worker(worker["id"], **fields)
                    st.session_state["last_success"] = f"已保存 Worker '{nickname}' 的修改"
                    st.session_state.pop("editing_worker_id", None)
                else:
                    wid = db.add_worker(**fields)
                    st.session_state["last_success"] = f"已添加 Worker '{nickname}' (ID={wid})"
                st.rerun()
            except Exception as e:
                msg = str(e)
                if "UNIQUE constraint failed: workers.nickname" in msg:
                    st.error(f"备注名 '{nickname}' 已存在,请换一个")
                else:
                    st.error(f"保存失败:{msg}")


# ============================================================

# 一次性成功反馈
if "last_success" in st.session_state:
    st.success("✅ " + st.session_state.pop("last_success"))

proxies = db.list_proxies()
if not proxies:
    st.warning("还没有任何代理,先去 Proxies 页录入。")
    st.stop()

# 编辑模式 / 新增模式
if "editing_worker_id" in st.session_state:
    eid = st.session_state["editing_worker_id"]
    w = db.get_worker(eid)
    if not w:
        st.session_state.pop("editing_worker_id", None)
        st.rerun()
    st.subheader(f"✏️ 编辑 Worker: {w['nickname']}")
    render_worker_form(proxies, worker=w)
    st.divider()
else:
    with st.expander("➕ 添加 Worker", expanded=False):
        render_worker_form(proxies)


st.subheader("现有 Workers")
workers = db.list_workers()
if not workers:
    st.info("还没有 Worker。")
else:
    for w in workers:
        with st.container(border=True):
            cols = st.columns([3, 3, 1])
            with cols[0]:
                st.markdown(f"**{w['nickname']}** &nbsp; @{w.get('twitter_handle') or '-'}")
                st.caption(
                    f"VPS `{w['vps_host']}:{w['ssh_port']}` | "
                    f"代理 `{w.get('proxy_host', '-')}:{w.get('proxy_port', '-')}` | "
                    f"来源 {w.get('source_site') or '-'}"
                )
                st.caption(
                    f"工作时段 {w['work_start']}–{w['work_end']} | "
                    f"间隔 {w['rest_min_minutes']}–{w['rest_max_minutes']} 分 | "
                    f"每日 {w['daily_target']} 条 | 配额 {w['traffic_quota_gb']} G | "
                    f"时区 {w.get('timezone', '-')} | 语言 {w.get('locale', '-')}"
                )
            with cols[1]:
                pct = (w.get("traffic_used_gb") or 0) / (w.get("traffic_quota_gb") or 1) * 100
                st.markdown(f"流量 **{w.get('traffic_used_gb') or 0:.1f} / {w.get('traffic_quota_gb')} G**({pct:.0f}%)")
                st.progress(min(1.0, pct / 100))
            with cols[2]:
                st.markdown(f"`{zh_worker_status(w['status'])}`")
                hb = w.get("last_heartbeat")
                st.caption(to_beijing(hb) + " (北京)" if hb else "未上线")

            b1, b2, b3, b4, b5, b6, b7, b8 = st.columns(8)

            if b1.button("🔌 测连接", key=f"test_{w['id']}"):
                with st.spinner("..."):
                    ok, info = test_connection(
                        host=w["vps_host"], port=w["ssh_port"], user=w["ssh_user"],
                        password=w.get("ssh_password"), key_path=w.get("ssh_key_path"),
                    )
                    if ok:
                        st.success(info[:300])
                    else:
                        st.error(info)

            if b2.button("✏️ 编辑", key=f"edit_{w['id']}"):
                st.session_state["editing_worker_id"] = w["id"]
                st.rerun()

            if b3.button("🚀 部署", key=f"deploy_{w['id']}",
                         help="首次部署:装环境+推代码+注册systemd(几分钟)"):
                placeholder = st.empty()
                progress_msgs = []
                def report(m):
                    progress_msgs.append(m)
                    placeholder.info("\n".join(progress_msgs))
                with st.spinner("部署中..."):
                    ok, msg = syn.provision_worker(w, on_progress=report)
                if ok:
                    st.success(msg)
                else:
                    st.error(msg)

            if b4.button("🔄 更新代码", key=f"upd_{w['id']}",
                         help="只推 worker 代码 + 重启服务"):
                with st.spinner("更新中..."):
                    ok, msg = syn.update_worker_code(w)
                (st.success if ok else st.error)(msg)

            if b5.button("📤 推送任务", key=f"push_{w['id']}",
                         help="推 config + 今日已排任务"):
                with st.spinner("推送中..."):
                    try:
                        syn.push_config(w)
                        from scheduler import get_today_active_tasks
                        tasks = get_today_active_tasks(w["id"])
                        syn.push_tasks(w, tasks)
                        st.success(f"已推送 config + {len(tasks)} 条任务")
                    except Exception as e:
                        st.error(str(e))

            if b6.button("📡 同步", key=f"sync_{w['id']}", help="拉取状态/任务进度/日志"):
                with st.spinner("同步中..."):
                    try:
                        r = syn.sync_worker(w)
                        st.success(r["message"])
                    except Exception as e:
                        st.error(str(e))

            if b7.button("🔁 重启服务", key=f"rs_{w['id']}"):
                ok, msg = syn.restart_worker(w)
                (st.success if ok else st.error)(msg)

            if b8.button("🗑️ 删除", key=f"del_w_{w['id']}"):
                db.delete_worker(w["id"])
                st.session_state["last_success"] = f"已删除 Worker '{w['nickname']}'"
                st.rerun()
