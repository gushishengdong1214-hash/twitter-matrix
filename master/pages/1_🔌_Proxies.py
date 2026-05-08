import streamlit as st
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
import database as db

st.set_page_config(page_title="代理库", page_icon="🔌", layout="wide")
st.title("🔌 代理库")
st.caption("静态住宅代理(SOCKS5)。每台 Worker 绑定一个,1:1 不共用。")

with st.expander("➕ 添加代理", expanded=False):
    with st.form("add_proxy", clear_on_submit=True):
        c1, c2, c3 = st.columns([2, 2, 1])
        nickname = c1.text_input("备注名", placeholder="例:US-NY-Static-1")
        host = c2.text_input("Host", placeholder="例:1.2.3.4")
        port = c3.number_input("Port", min_value=1, max_value=65535, value=1080)

        c4, c5 = st.columns(2)
        username = c4.text_input("用户名", value="")
        password = c5.text_input("密码", value="", type="password")
        note = st.text_area("备注", placeholder="可选,例:供应商/到期日")

        if st.form_submit_button("添加", type="primary"):
            if not host:
                st.error("Host 必填")
            else:
                pid = db.add_proxy(nickname, host, int(port), username, password, note)
                st.success(f"已添加,ID = {pid}")
                st.rerun()

st.divider()
st.subheader("现有代理")
proxies = db.list_proxies()
if not proxies:
    st.info("还没有代理。先在上面添加。")
else:
    for p in proxies:
        with st.container(border=True):
            cols = st.columns([4, 1])
            with cols[0]:
                st.markdown(f"**{p['nickname'] or '(无备注)'}** &nbsp; `{p['host']}:{p['port']}`")
                st.caption(
                    f"用户名:{p['username'] or '-'} | 类型:{p['type']} | "
                    f"备注:{p['note'] or '-'} | 创建:{p['created_at']}"
                )
            with cols[1]:
                if st.button("删除", key=f"del_p_{p['id']}"):
                    db.delete_proxy(p["id"])
                    st.rerun()
