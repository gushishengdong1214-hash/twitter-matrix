import os
import streamlit as st

_PASSWORD = os.getenv("TWMATRIX_UI_PASSWORD", "").strip()


def check_auth():
    """全局登录拦截。未设置密码时直接放行;已设置密码但未登录时阻断页面渲染。"""
    if not _PASSWORD:
        return
    if not (st.session_state.get("logged_in") or st.session_state.get("authed")):
        st.error("🔒 未登录，请先返回主页输入密码")
        st.stop()
