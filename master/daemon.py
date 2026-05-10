"""
中控 Daemon。独立进程跑(不依赖 streamlit)。systemd 拉起。

负责:
  - 每天凌晨 3 点重排所有 worker 的今日任务,并推送 config + tasks 到 worker
  - 每分钟同步一次 worker 状态(state/log/任务进度)
  - 每 5 分钟把 DB 里 scheduled 状态的任务重新推一遍 tasks.json(防丢)

可选环境变量:
  TWMATRIX_DAEMON_PLAN_HOUR   (默认 3)
  TWMATRIX_DAEMON_PLAN_MINUTE (默认 0)
  TWMATRIX_DAEMON_SYNC_INTERVAL_S (默认 60)
  TWMATRIX_DAEMON_PUSH_INTERVAL_S (默认 300)
"""

import hashlib
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from apscheduler.schedulers.blocking import BlockingScheduler

import database as db
import scheduler as sched
import sync as syn
from crawler import crawl_site, SUPPORTED_SITES
from crawler.translator import translate
import scoring


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("daemon")


PLAN_HOUR = int(os.getenv("TWMATRIX_DAEMON_PLAN_HOUR", "3"))
PLAN_MINUTE = int(os.getenv("TWMATRIX_DAEMON_PLAN_MINUTE", "0"))
SYNC_INTERVAL = int(os.getenv("TWMATRIX_DAEMON_SYNC_INTERVAL_S", "60"))
PUSH_INTERVAL = int(os.getenv("TWMATRIX_DAEMON_PUSH_INTERVAL_S", "300"))


def daily_plan_and_push():
    log.info("== 开始今日调度 ==")
    result = sched.plan_today_for_all()
    for wid, n in result.items():
        if n == -1:
            log.info(f"Worker {wid}: 跳过（Worker 不健康）")
        else:
            log.info(f"Worker {wid}: 排 {n} 条")
    push_to_all()


def _is_worker_healthy(w: dict) -> bool:
    """判断 Worker 是否健康（可推送任务、可排期）。"""
    return w.get("status") in ("idle", "running", "resting", "pending")


def push_to_all():
    workers = db.list_workers()
    for w in workers:
        if not _is_worker_healthy(w):
            log.info(f"Worker {w['id']} 状态为 {w.get('status')}，跳过推送")
            continue
        try:
            syn.push_config(w)
            tasks = sched.get_today_active_tasks(w["id"])
            syn.push_tasks(w, tasks)
            log.info(f"Worker {w['id']} ({w['nickname']}): 推送 {len(tasks)} 条今日任务")
        except Exception as e:
            log.error(f"推送 worker {w['id']} 失败:{e}")


def crawl_and_translate():
    """自动采集：遍历所有站点 → 爬虫 → 翻译 → 评分 → 入库。"""
    log.info("== 开始自动采集 ==")
    total_new = 0
    for site in SUPPORTED_SITES:
        try:
            items = crawl_site(site, limit=10)
            site_new = 0
            for item in items:
                url = item["url"]
                url_hash = hashlib.md5(url.encode()).hexdigest()
                if db.is_url_crawled(url_hash):
                    continue

                title = item.get("title", "")
                thumb = item.get("thumbnail_url", "")
                translated = translate(title)

                # 评分
                score = scoring.score_video(item)

                db.add_crawled_video(
                    url=url,
                    url_hash=url_hash,
                    site=site,
                    title=title,
                    original_description=title,
                    translated_caption=translated,
                    thumbnail_url=thumb,
                    score=score,
                )
                site_new += 1
                total_new += 1
            log.info(f"站点 {site}: 新增 {site_new} 条")
        except Exception as e:
            log.error(f"站点 {site} 采集失败: {e}")
    log.info(f"自动采集完成，共新增 {total_new} 条")


def prune_logs_daily():
    """每天自动清理超过 20000 条的旧日志，防止 SQLite 无限膨胀。"""
    try:
        db.prune_logs(keep_last=20000)
        log.info("日志清理完成")
    except Exception as e:
        log.error(f"日志清理失败: {e}")


def auto_approve_candidates():
    """自动审批：score >= 35 的 pending 候选自动转为任务，含内容过滤。

    分配策略：轮询分配给健康 Worker，让各账号内容多样化。
    内容过滤：命中敏感词直接拒绝，命中警告词降低分数。
    """
    log.info("== 开始自动审批 ==")
    from filter import check_content

    candidates = db.list_crawled_videos(status="pending", limit=500)
    if not candidates:
        log.info("没有待审批候选")
        return

    # 按 score 降序，高分优先
    candidates.sort(key=lambda x: x.get("score", 0), reverse=True)

    workers = [w for w in db.list_workers() if _is_worker_healthy(w)]
    if not workers:
        log.info("没有健康 Worker，跳过自动审批")
        return

    approved = 0
    skipped = 0
    rejected = 0
    worker_idx = 0

    for c in candidates:
        score = c.get("score", 0)
        title = c.get("title", "")
        caption = c.get("translated_caption", "") or title

        # 内容过滤
        result = check_content(title, caption)
        if result.blocked:
            db.update_crawled_video(c["id"], status="rejected")
            log.warning(f"内容过滤拒绝 vid={c['id']}: {result.reason}")
            rejected += 1
            continue

        if result.warning:
            score -= 30
            log.info(f"内容警告 vid={c['id']}: {result.reason}, 分数降至 {score}")

        if score < 35:
            skipped += 1
            continue

        # 轮询分配给健康 Worker
        w = workers[worker_idx % len(workers)]
        worker_idx += 1

        try:
            tid = db.approve_crawled_video(c["id"], w["id"])
            if tid:
                approved += 1
            else:
                skipped += 1
        except Exception as e:
            log.error(f"自动审批失败 vid={c['id']}: {e}")
            skipped += 1

    log.info(f"自动审批完成: 通过 {approved} 条, 跳过 {skipped} 条, 拒绝 {rejected} 条")


def sync_all():
    workers = db.list_workers()
    if not workers:
        return
    # 限制并发数为 3，降低 SSH 连接压力和 DB 锁竞争频率。
    # database.py 已加 _db_write_lock 根本解决并发写问题；这里的限制是第二层保险。
    with ThreadPoolExecutor(max_workers=min(len(workers), 3)) as ex:
        for w in workers:
            ex.submit(_safe_sync, w)


def _safe_sync(w: dict):
    try:
        syn.sync_worker(w)
    except Exception as e:
        log.error(f"sync_worker {w['id']} 失败:{e}")


# 自动采集和审批的时间配置
CRAWL_HOUR = int(os.getenv("TWMATRIX_DAEMON_CRAWL_HOUR", "2"))
CRAWL_MINUTE = int(os.getenv("TWMATRIX_DAEMON_CRAWL_MINUTE", "0"))
APPROVE_HOUR = int(os.getenv("TWMATRIX_DAEMON_APPROVE_HOUR", "2"))
APPROVE_MINUTE = int(os.getenv("TWMATRIX_DAEMON_APPROVE_MINUTE", "30"))


def main():
    db.init_db()
    s = BlockingScheduler()

    # 每天凌晨调度任务
    s.add_job(daily_plan_and_push, "cron", hour=PLAN_HOUR, minute=PLAN_MINUTE,
              id="plan_daily", max_instances=1)
    # 每分钟同步 Worker 状态
    s.add_job(sync_all, "interval", seconds=SYNC_INTERVAL,
              id="sync", max_instances=1, coalesce=True)
    # 每 5 分钟推送任务
    s.add_job(push_to_all, "interval", seconds=PUSH_INTERVAL,
              id="push", max_instances=1, coalesce=True)
    # 每天凌晨自动采集视频
    s.add_job(crawl_and_translate, "cron", hour=CRAWL_HOUR, minute=CRAWL_MINUTE,
              id="crawl_daily", max_instances=1)
    # 每天凌晨自动审批高分候选（采集后 30 分钟）
    s.add_job(auto_approve_candidates, "cron", hour=APPROVE_HOUR, minute=APPROVE_MINUTE,
              id="approve_daily", max_instances=1)
    # 每天凌晨 4 点自动清理旧日志
    s.add_job(prune_logs_daily, "cron", hour=4, minute=0,
              id="prune_logs", max_instances=1)

    log.info(f"Daemon 启动 plan={PLAN_HOUR:02d}:{PLAN_MINUTE:02d} "
             f"sync_interval={SYNC_INTERVAL}s push_interval={PUSH_INTERVAL}s "
             f"crawl={CRAWL_HOUR:02d}:{CRAWL_MINUTE:02d} approve={APPROVE_HOUR:02d}:{APPROVE_MINUTE:02d}")

    # 启动时先做一次同步
    try:
        sync_all()
    except Exception as e:
        log.error(f"启动同步异常:{e}")

    try:
        s.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Daemon 退出")


if __name__ == "__main__":
    main()
