# 项目进度快照 — 推特矩阵系统

> **用途**:防止 Claude 上下文超限失忆。每 ~15 轮对话 + 关键决策节点由 Claude 主动更新。
> **最近更新**:2026-05-11 第 8 轮(修完 Bug A/B)
> **配套文档**:
> - `CLAUDE.md` — 静态架构与坑(每次新会话自动加载)
> - `HANDOFF.md` — 上一个 AI 留下的工程交接(权威背景)
> - 本文件 — 动态状态快照(进度、决策、待办)

---

## 一、项目目标(用户原话整理)

做一个推特视频矩阵搬运系统:

1. 从某 A 网站爬取视频(具体站点**待用户确认**)
2. 下载到指定的 worker VPS(vps1 起步,后续扩矩阵)
3. **必须**用用户指定的住宅 IP 登录推特和上传(已有 proxies 表 + gost 中继架构支持)
4. 调度策略:一天发几部、每部之间间隔多久(对应 `workers.daily_target` / `rest_min_minutes` / `rest_max_minutes` / `work_start` / `work_end`)

## 二、当前阶段

**校对+修 bug 阶段**。业务边界已确认(见 §五 决策),现在要先把已知的关键问题挖出来。

V3 矩阵架构(master + worker)代码已经写了大半,worker1 跑通过一次但有瑕疵。已知关键问题:

1. **【P0 / 严重】"货不对板"**:用户提交的视频链接和实际上传到推特的视频对不上。可能原因(待排查):
   - 多任务并发时视频文件下载路径冲突(文件名重复覆盖)
   - m3u8 sniff 嗅到了页面上**其他**视频的 m3u8(jable / hanime1 详情页面通常有多个 video 标签或推荐位)
   - 任务队列处理时 video 文件 ↔ task_id 没绑定
   - 文案/缩略图/视频三者错位
2. **【P0】爬虫没接通**:用户手动上传任务,而不是用全自动爬虫(`master/crawler/` 已有但没流水线接到任务队列?)
3. **【P1】中控代码"需要修复"** — 但用户也不知道具体修哪;需要我主动扫一遍代码找问题
4. **【P2】worker2 还没部署**(账号在指纹浏览器养着,可以随时拿出来测)

## 三、已完成(本次会话)

| 时间 | 事项 | 文件 |
|---|---|---|
| 第 1 轮 | 创建 CLAUDE.md(项目速读索引,新会话自动加载) | `CLAUDE.md` |
| 第 5 轮 | 建立防失忆三件套:CLAUDE.md / project_summary.md / memory 系统 | 本文件 + `~/.claude/.../memory/` |
| 第 5 轮 | 用户偏好和项目背景写入 memory(跨会话生效) | `memory/user_communication.md`、`memory/project_status.md`、`memory/feedback_progress_summary.md` |
| 第 6 轮 | 业务边界确认(站点、模式、VPS 状态)→ 决策 D-003/D-004/D-005,P0 锁定"货不对板" | 见 §五 决策 |
| 第 7 轮 | 诊断"货不对板"根因:Bug B(m3u8 嗅错)+ Bug A(文件复用) | 见 §六.五 |
| 第 8 轮 | 修复 Bug A + Bug B,改动 `worker/worker.py` + `worker/tweet_engine.py` | 见 §六 变更日志 |

## 四、待办(优先级排序)

### P0 — 立即做
- [ ] **修"货不对板" bug** — 排查 worker 视频下载 ↔ 任务匹配链路,从 `worker/tweet_engine.py` + `worker/worker.py` + 爬虫 m3u8 嗅探入手
- [ ] **接通爬虫到任务队列** — `master/crawler/` 已有爬虫输出 → 怎么变成 `tasks` 表的 pending 记录(目前缺中间环节,所以用户在手动上传)
- [ ] **扫一遍中控代码找用户说的"需要修复"是什么** — sync.py / scheduler.py / pages/ 全部过一遍

### P1 — bug 修完后做
- [ ] 配 worker1 调度参数(`daily_target` / `rest_*` / `work_*` 用户期望值待问)
- [ ] 端到端跑通:爬虫拉一批 → 自动入任务队列 → worker1 调度执行 → 推上去且视频对得上
- [ ] 准备 worker2(用户说账号在指纹浏览器养着,可拿出来测)

### P2 — 稳定后扩展
- [ ] HANDOFF.md §六 TODO 按需取(DB 备份、日志清理、告警外推)
- [ ] 文案模板化(同一视频多账号不同文案,防关联)

## 五、关键决策记录

(决策 = 不止"做什么",还包括"为什么这么做",方便失忆后接续)

| # | 决策 | 理由 |
|---|---|---|
| D-001 | 防失忆机制采用三层组合:CLAUDE.md(静态)+ project_summary.md(动态)+ memory(偏好) | 用户明确要求 project_summary.md;CLAUDE.md 已存在适合放架构;memory 跨会话最稳 |
| D-002 | 起步阶段不写新代码,先校对现状 | 现有 V3 代码大部分功能已有,但可跑性未知;贸然加新东西可能踩重复坑 |
| D-003 | 目标站点 = jable.tv + hanime1.me 两个都做 | 用户回答"1、2 都是的";现有 `master/crawler/` 两个爬虫都已写 |
| D-004 | 工作模式 = 基于现有 V3 矩阵继续完善 | 用户回答;已写代码量大,从零重做浪费 |
| D-005 | 优先修"货不对板"而不是先做新功能 | 这是会破坏整个矩阵可信度的根本性 bug;不修无法上量到 5-10 台 |

## 六、核心逻辑变更日志

### 第 8 轮 — 修复"货不对板" Bug A + Bug B

**`worker/worker.py`:**
- `VIDEO_TEMP`(固定路径)→ 删除
- 新增 `VIDEO_TEMP_DIR = Path("/tmp")` + `VIDEO_TEMP_PREFIX = "twitter-worker-video-"`
- 新增 `video_temp_for(task_id)` 函数,返回 `/tmp/twitter-worker-video-{tid}.mp4`(每任务独立路径)
- 新增 `cleanup_all_video_temps()` 函数,清理 /tmp 下所有 worker 视频残留
- `run_one_task` 改用 `video_temp = video_temp_for(tid)`,**开头强制清理同名残留**,下载失败也清,finally 也清
- `main_loop` 启动时调一次 `cleanup_all_video_temps()`(清上次崩溃残留)

**`worker/tweet_engine.py`:**
- `download_video` 删除 "已有完整视频, 跳过下载" 那段(Bug A 的导火索)
- `_sniff_m3u8` 重写,三层策略:
  1. 主策略:`page.evaluate` 在 DOM 里找 `<video>.currentSrc`(若是直接 m3u8 url 命中)
  2. 兜底 1:URL 模式排除(`/preview`、`/trailer`、`/thumb`、`/ad/` 等),只剩 1 个直接用
  3. 兜底 2:多候选时 GET 每个 m3u8,选 `#EXTINF` segment 数最多的(主视频 segment 数 >> 预览)
  4. 最后兜底:返回排除后的第一个

**Why:** Bug B 的根因是 jable / hanime1 详情页预加载推荐位 m3u8,旧逻辑"返回第一个"几乎必踩。新策略用 DOM + URL 排除 + segment 数三重过滤,主视频 hit rate 应该接近 100%。Bug A 的根因是固定路径 + 跳过下载逻辑组合,改成 task_id 隔离 + 强制清理,即使 worker 崩溃也不会复用残留。

## 六.五、Bug 诊断:"货不对板"根因

> **症状**:用户提交链接 → 推上去的视频是别的;**视频错文案对**;**几乎所有任务都错**
> **诊断完成于第 7 轮,诊断人:Claude**

**诊断结论:Bug B 为主,Bug A 为辅**

### Bug B(主因,几乎都错的解释):m3u8 嗅探拿错

`worker/tweet_engine.py` 的 `_sniff_m3u8` (line 70-91):
```python
captured: list[str] = []
page.on("request", lambda r: captured.append(r.url) if ".m3u8" in r.url else None)
page.goto(url, wait_until="domcontentloaded", timeout=60_000)
page.wait_for_timeout(8000)
...
for link in captured:
    if ".m3u8" in link:
        return link   # 返回捕获到的第一个 m3u8
```

**问题**:
- `jable_crawler` 返回详情页 URL(如 `https://jable.tv/videos/xxx/`)
- `hanime1_crawler` 返回详情页 URL(如 `https://hanime1.me/watch?v=xxx`)
- 详情页面会**预加载推荐位的 hover preview m3u8**,这些请求会先于主视频 m3u8 触发(或乱序)
- 8 秒内捕获到的"第一个" m3u8 极大概率是预览位的,不是主视频
- 完美匹配"几乎都错"的现象

### Bug A(次因,偶发但要修):VIDEO_TEMP 文件复用

`worker/worker.py:32` + `worker/tweet_engine.py:102`:
```python
VIDEO_TEMP = Path("/tmp/twitter-worker-video.mp4")  # 所有任务共用
...
if out_path.exists() and out_path.stat().st_size > 10 * 1024 * 1024:
    return True  # 直接复用上次的!
```

`run_one_task` 的 `finally` 会清理,但:
- worker 崩溃 / systemctl restart 时残留
- download_video 部分写入但返回 False(下载失败)时残留
- 下次任务来,> 10MB 就复用了

### 修复方案

**修 Bug B**:
1. **主策略**:用 `page.evaluate` 在 DOM 里找主 `<video>` 元素,读 `currentSrc` / `src`
2. **兜底策略**:URL 模式过滤(剔除 `/preview/`、`/trailer/`、`/ad/`、`/thumb` 等)
3. **次兜底**:选 m3u8 中包含 segment 数最多的(主视频通常 segment 数远多于预览)

**修 Bug A**:
1. `VIDEO_TEMP_FMT = "/tmp/twitter-worker-video-{tid}.mp4"`,每个任务独立路径
2. 删除"大小 > 10MB 就跳过下载"逻辑(单任务无意义)
3. `run_one_task` 开头强制清理同名残留(以防万一)

## 七、待用户回答的问题

> 这是"未消化的输入",每轮整理一次,避免遗忘

- [x] ~~Q1:目标站点是 jable.tv / hanime1.me 之一,还是新站点?~~ → 两个都是 (D-003)
- [x] ~~Q2:这次是基于现有 V3 架构继续完善,还是要重做?~~ → 基于 V3 继续 (D-004)
- [x] ~~Q3:vps1 当前能不能正常 SSH?中控 VPS 现在是已部署的状态吗?~~ → 中控代码需修复(具体不知),worker1 跑过一次有 bug,worker2 没部署
- [ ] **Q4:【关键】"货不对板"具体表现** —
  - 是发推时上传的**视频**和任务里的链接对不上(链接 A,实际发 B 视频)?
  - 还是**文案**和视频对不上(视频对了文案错了)?
  - 还是**缩略图/标题**和视频对不上?
- [ ] Q5:期望的调度参数(一天几条、间隔区间、工作时段) — 等 bug 修完再问
- [ ] Q6:中控 VPS 当前能 SSH 上去吗?域名/IP 我能在哪里看到部署?(暂时不需要直连,但后面要用)

## 八、给未来 Claude 的提示

如果你接手时本文件已经有很多内容:
1. 先读 `CLAUDE.md` 搞清架构
2. 再读 `HANDOFF.md` §五(已修复 bug)和 §六(TODO)知道历史包袱
3. 然后读本文件的 **二、当前阶段** 和 **四、待办** 知道现在做到哪
4. 看 **七、待用户回答的问题** 知道还有什么悬而未决
5. 不要重新提一遍用户已经回答过的问题(在 **五、关键决策记录** 里找)
