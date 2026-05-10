import streamlit as st
import sys
import hashlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import database as db
from crawler import crawl_site, SUPPORTED_SITES
from crawler.translator import translate

st.set_page_config(page_title="视频采集", page_icon="🕷️", layout="wide")
st.title("🕷️ 视频采集")
st.caption("从目标站点抓取视频链接和标题,自动翻译为中文文案,人工确认后加入任务队列。")

# ========== 采集配置 ==========
st.subheader("采集设置")
workers = db.list_workers()
if not workers:
    st.warning("还没有 Worker。先去 Workers 页添加。")
    st.stop()

col1, col2 = st.columns(2)
with col1:
    st.markdown("**选择站点**")
    site_jable = st.checkbox("jable.tv", value=True)
    site_hanime1 = st.checkbox("hanime1.me", value=True)

with col2:
    st.markdown("**各站采集数量**")
    limit_per_site = st.number_input("每站抓多少条", min_value=1, max_value=50, value=10, step=1)

selected_sites = []
if site_jable:
    selected_sites.append("jable.tv")
if site_hanime1:
    selected_sites.append("hanime1.me")

# 目标 Worker（确认后加入哪个 Worker 的任务队列）
target_worker = st.selectbox(
    "采集确认后加入任务队列的 Worker",
    [w["id"] for w in workers],
    format_func=lambda x: next(
        f"{w['nickname']}  @{w.get('twitter_handle','-')}  来源:{w.get('source_site','-')}"
        for w in workers if w["id"] == x
    ),
)

# ========== 开始采集 ==========
if st.button("🚀 开始采集", type="primary", use_container_width=True):
    if not selected_sites:
        st.error("请至少选择一个站点")
    else:
        total_new = 0
        total_dup = 0
        detail_logs = []

        for idx, site in enumerate(selected_sites):
            st.write(f"⏳ 正在采集 {site} ...")
            try:
                items = crawl_site(site, limit=limit_per_site)
                site_new = 0
                site_dup = 0
                for item in items:
                    url = item["url"]
                    url_hash = hashlib.md5(url.encode()).hexdigest()
                    if db.is_url_crawled(url_hash):
                        total_dup += 1
                        site_dup += 1
                        continue

                    title = item.get("title", "")
                    thumb = item.get("thumbnail_url", "")
                    translated = translate(title)

                    db.add_crawled_video(
                        url=url,
                        url_hash=url_hash,
                        site=site,
                        title=title,
                        original_description=title,
                        translated_caption=translated,
                        thumbnail_url=thumb,
                    )
                    total_new += 1
                    site_new += 1

                detail_logs.append(f"✅ {site}: 抓到 {len(items)} 条, 新增 {site_new} 条, 去重 {site_dup} 条")
            except Exception as e:
                detail_logs.append(f"❌ {site}: 采集失败 — {e}")

        if total_new == 0:
            st.warning(f"本次采集 0 条新增，全部已存在（去重 {total_dup} 条）。如需新内容，可更换站点或等站点更新后再采。")
        else:
            st.success(f"采集完成！新增 {total_new} 条，去重跳过 {total_dup} 条")
        with st.expander("查看详细日志"):
            for line in detail_logs:
                st.write(line)
        import time
        time.sleep(1)
        st.rerun()

st.divider()

# ========== 候选池 ==========
st.subheader("📋 候选池（人工审查）")

# 统计
cnt_pending = len(db.list_crawled_videos(status="pending", limit=9999))
cnt_approved = len(db.list_crawled_videos(status="approved", limit=9999))
cnt_rejected = len(db.list_crawled_videos(status="rejected", limit=9999))
st.caption(f"待审查 {cnt_pending} | 已通过 {cnt_approved} | 已拒绝 {cnt_rejected}")

tab_pending, tab_approved, tab_rejected = st.tabs(["⏳ 待审查", "✅ 已通过", "❌ 已拒绝"])


def render_candidate_list(status: str):
    candidates = db.list_crawled_videos(status=status, limit=200)
    if not candidates:
        st.info("没有记录")
        return

    for c in candidates:
        with st.container(border=True):
            cols = st.columns([1, 3, 1])

            with cols[0]:
                thumb = c.get("thumbnail_url", "")
                if thumb:
                    st.image(thumb, use_container_width=True)
                else:
                    st.caption("无缩略图")
                st.caption(f"来源: {c['site']}")

            with cols[1]:
                st.markdown(f"**原标题:** {c.get('title') or '—'}")
                # 一键打开链接
                url = c['url']
                st.html(f'<a href="{url}" target="_blank" style="font-size:12px;color:#58a6ff;">🔗 在新标签页打开视频</a>')

                # 可编辑的翻译文案
                edited_key = f"edit_cap_{c['id']}"
                if edited_key not in st.session_state:
                    st.session_state[edited_key] = c.get("translated_caption") or ""

                new_caption = st.text_area(
                    "推文文案（可编辑）",
                    value=st.session_state[edited_key],
                    key=f"ta_{c['id']}",
                    height=80,
                )
                if new_caption != st.session_state[edited_key]:
                    st.session_state[edited_key] = new_caption
                    db.update_crawled_video(c["id"], translated_caption=new_caption)

            with cols[2]:
                if status == "pending":
                    if st.button("✅ 确认发布", key=f"approve_{c['id']}", type="primary"):
                        caption = st.session_state.get(f"edit_cap_{c['id']}", "") or c.get("translated_caption") or c.get("title", "")
                        tid = db.approve_crawled_video(c["id"], target_worker)
                        if tid:
                            st.success(f"已转为任务 #{tid}")
                            st.rerun()
                        else:
                            st.error("转换失败")

                    if st.button("❌ 删除", key=f"reject_{c['id']}"):
                        db.update_crawled_video(c["id"], status="rejected")
                        st.rerun()

                elif status == "approved":
                    st.caption("已加入任务队列")

                elif status == "rejected":
                    if st.button("🔄 恢复", key=f"restore_{c['id']}"):
                        db.update_crawled_video(c["id"], status="pending")
                        st.rerun()
                    if st.button("🗑️ 彻底删除", key=f"del_{c['id']}"):
                        db.delete_crawled_video(c["id"])
                        st.rerun()


with tab_pending:
    render_candidate_list("pending")

with tab_approved:
    render_candidate_list("approved")

with tab_rejected:
    render_candidate_list("rejected")
