# HANDOFF — 推特矩阵系统

> 这是把项目交接给下一位维护者(人或 AI)用的速读手册。**不是用户文档**,是工程交接。
>
> 真实凭证(VPS IP / SSH 密码 / Streamlit UI 密码 / cookie / 2FA secret / 住宅代理认证)**全部不在仓库里**,需要找项目业主单独获取。

---

## 一、代码资产

### 1. 完整代码树

```
推特矩阵系统/                       (本地: C:\Users\LL\Desktop\推特矩阵系统\)
├── .gitignore
├── HANDOFF.md                      ← 你正在读的这个
├── master/                          ← 中控代码,部署到中控 VPS:/opt/twitter-matrix/master/
│   ├── app.py                       Streamlit 主页(看板) + 密码登录
│   ├── database.py                  SQLite schema + 增量迁移 + CRUD
│   ├── ssh_client.py                paramiko 封装 + 底层 banner 探测
│   ├── sync.py                      中控↔worker 同步层(push_config/tasks, pull_state/log/screenshots, provision)
│   ├── scheduler.py                 任务调度算法(每天工作时段内分布,随机休息)
│   ├── daemon.py                    后台 daemon:每分钟 sync,每 5 分钟 push,每天 3:00 重排
│   ├── requirements.txt             streamlit / pandas / paramiko<4 / apscheduler
│   ├── data/
│   │   ├── matrix.db                SQLite(WAL),不在 git(被 .gitignore 排除)
│   │   └── screenshots/             从 worker 同步过来的告警截图
│   └── pages/                       Streamlit 多页面(目录名带 emoji 是 streamlit 原生命名约定)
│       ├── 1_🔌_Proxies.py          住宅代理库
│       ├── 2_💻_Workers.py          VPS / 账号录入,有"测连接 / 部署 / 更新代码 / 推送任务 / 同步 / 重启 / 删除 / 编辑"按钮
│       ├── 3_📋_Tasks.py            批量录入任务(URL====文案 / 用 | 行内换行带 @)
│       ├── 4_🚨_Alerts.py           人工干预告警 + 截图查看 + 让 worker 继续
│       └── 5_📜_Logs.py             运行日志(按 worker 筛选)
├── worker/                          ← worker 代码,部署到每台 worker VPS:/opt/twitter-worker/
│   ├── worker.py                    主循环(systemd 拉起);读 config.json/tasks.json/cmd.json,写 state.json/log.txt
│   ├── tweet_engine.py              下载视频(yt-dlp + 浏览器嗅 m3u8) + 发推(playwright chromium)
│   ├── popup_handler.py             弹窗规则库 + dismiss_all_overlays + @combobox 选第一项 + 多行 caption
│   ├── traffic.py                   vnstat 月流量统计(GB)
│   ├── socks_relay.py               gost SOCKS5 中继(chromium 不支持带认证 SOCKS5,所以本地无认证→住宅认证)
│   └── requirements.txt             playwright / yt-dlp / PySocks / pyotp / curl-cffi
└── deploy/
    ├── install_master.sh            中控一键部署:装环境 + 写 systemd unit (twmatrix-ui / twmatrix-daemon)
    └── upload_to_master.sh          本地 rsync 到中控(国内 → 海外 SSH 被 GFW 拦,实际不走这条,改用 GitHub 中转)
```

### 2. 版本管理

- **Git 仓库初始化在本地**:`C:\Users\LL\Desktop\推特矩阵系统\`
- **远程**:GitHub,具体 URL 找业主获取(私有或公开)
- **默认分支**:`main`
- **部署链路**:
  ```
  本地编辑 → git push origin main
       ↓
  中控 VPS:cd /opt/twitter-matrix && git pull && systemctl restart twmatrix-ui twmatrix-daemon
       ↓
  浏览器 Web UI → Workers 卡片 → "🔄 更新代码"
       ↓ (这一步 paramiko 自动)
  worker VPS:把 worker/*.py 推到 /opt/twitter-worker/ + systemctl restart twitter-worker
  ```

### 3. 依赖和环境

| | |
|---|---|
| 中控 venv | `/opt/twitter-matrix/master/venv/` |
| Worker venv | `/opt/twitter-worker/venv/` |
| `requirements.txt` | 已存,版本约束宽松(`paramiko<4` 是关键约束,4.0 有 SSH banner 兼容问题) |
| 中控 systemd | `twmatrix-ui.service`(streamlit)、`twmatrix-daemon.service`(后台) |
| Worker systemd | `twitter-worker.service` |
| 关键环境变量 | `TWMATRIX_UI_PASSWORD`(必填,Streamlit 登录密码,在 install_master.sh 里写到 systemd unit)<br>`TWMATRIX_DAEMON_PLAN_HOUR/MINUTE`(可选,默认 3:00 重排)<br>`TWMATRIX_DAEMON_SYNC_INTERVAL_S`(默认 60)<br>`TWMATRIX_DAEMON_PUSH_INTERVAL_S`(默认 300) |

---

## 二、部署和运维

### 4. 中控 systemd unit(install_master.sh 自动写)

```ini
[Unit]
Description=Twitter Matrix UI (Streamlit)
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/twitter-matrix/master
Environment="TWMATRIX_UI_PASSWORD=<密码>"
ExecStart=/opt/twitter-matrix/master/venv/bin/streamlit run app.py \
    --server.address=0.0.0.0 --server.port=8501 --server.headless=true \
    --browser.gatherUsageStats=false
Restart=always
```

```ini
[Unit]
Description=Twitter Matrix Daemon (scheduler/sync)
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/twitter-matrix/master
ExecStart=/opt/twitter-matrix/master/venv/bin/python daemon.py
Restart=always
```

### 5. Worker systemd unit(provision 时由 sync.py 写)

```ini
[Unit]
Description=Twitter Matrix Worker
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/twitter-worker
ExecStart=/opt/twitter-worker/venv/bin/python /opt/twitter-worker/worker.py
Restart=always
RestartSec=10
```

### 6. 数据库

- **路径**:`/opt/twitter-matrix/master/data/matrix.db`(SQLite,WAL 模式)
- **备份**:**没有自动备份策略 — TODO**。临时方案 `cp matrix.db matrix.db.$(date +%Y%m%d)`
- **可清理(无影响)**:
  - 旧告警:`UPDATE alerts SET resolved=1;`
  - 重复日志:`DELETE FROM logs;`
  - 注意 `tasks` / `workers` / `proxies` 不能动,这是真实状态
- **Schema 自动迁移**:`database.py` 的 `_migrate()` 在每次 `init_db()` 时跑,幂等加列(已加过的不会再加)

### 7. 网络和访问(凭证不在这里)

| 资源 | 怎么访问 |
|---|---|
| Streamlit Web UI | `http://<中控IP>:8501`,密码登录 |
| 中控 SSH | `root@<中控IP>:22`,密码 |
| Worker SSH | `root@<workerIP>:22`,密码 |
| 中国本地 SSH 海外 VPS | **基本走不通**(GFW QoS 拦 SSH 流量),走 evoxt web console |
| 代理服务商 | cliproxy.com(SOCKS5 + 认证) |
| Cookie 来源 | 指纹浏览器(配住宅 IP 后)用 Cookie Editor / EditThisCookie 导出 JSON 数组 |

---

## 三、状态机和数据流

### 任务状态机

```
pending → scheduled → running → done       (成功)
                   ↓        ↓
                   ↓        → failed       (下载/网络错误)
                   ↓        → human_required (未知弹窗,worker 自动暂停 + 截图上报)
```

### 中控 ↔ Worker 文件协议

**中控推 → worker**(paramiko SFTP):
- `/opt/twitter-worker/config.json`:绑定信息(cookie/2fa/proxy/ua/viewport/timezone)+ 调度参数
- `/opt/twitter-worker/tasks.json`:今日任务清单(只 scheduled 状态进 push,running 已被 worker 接管不推)
- `/opt/twitter-worker/cmd.json`:命令(action: pause/resume/reload),worker 处理后删除

**Worker 写 → 中控拉**(SFTP / SSH cat):
- `/opt/twitter-worker/state.json`:当前状态(idle/running/paused/human_required)+ heartbeat + 流量
- `/opt/twitter-worker/log.txt`:运行日志(中控用 `tail -c +N` 增量拉,基于 `workers.last_log_offset` 字段)
- `/opt/twitter-worker/screenshots/`:告警截图

### 调度算法(scheduler.plan_today_for_worker)

1. 当天已 `scheduled` 的任务 → 回退 `pending`(允许重排)
2. 超过 2 小时还在 `running` 的任务(stale)→ 回退 `pending`(worker 重启丢任务的兜底)
3. 从 `pending` 池按 ID 顺序取 `daily_target`(默认 8)条
4. 起始时间 = `today work_start ± 30 分钟随机`,如果已过则改成 `now + 5 分钟`
5. 每条 `scheduled_at = 上一条 + 60 分钟(任务时长估值) + random(rest_min, rest_max)`
6. 超过 work_end 的任务不排,留到明天

---

## 四、已知限制和反风控

### X 平台风控应对

| 风控类型 | 解决方案 |
|---|---|
| **2FA(TOTP)** | 用户填 secret 到 worker 表单,worker 用 `pyotp` 自动算 6 位码 |
| **Arkose 滑动验证** | **代码无解**。首次登录在指纹浏览器(配同一住宅 IP)手动通过 → 导出 cookie 给 worker 用,后续用 cookie 不再触发 |
| **新 IP 触发"安全检查"** | 同上,关键是确保 worker 的"住宅 IP + UA + 时区"和 cookie 来源浏览器一致 |
| **视频被审核拒绝** | wait `disabled` 消失 30 分钟超时,触发 human_required + 截图。代码无法救,只能换视频 |
| **自动化检测** | 已加:`--disable-blink-features=AutomationControlled` + JS 屏蔽 `navigator.webdriver`。**未加** Canvas/WebGL/字体/AudioContext 深度伪装。如以后被识破,加 [playwright-stealth](https://github.com/AtuboDad/playwright_stealth) |
| **行为识别** | 任务间随机休息 30-90 分钟、工作时间窗口、每天 8 条上限、发推前拟人延迟 5-15 秒 |

### 视频源处理

| 站点 | 处理方式 |
|---|---|
| `jable.tv` | yt-dlp 拒绝(Piracy 检测)→ **必须**先用 chromium 嗅 m3u8,嗅不到直接放弃 |
| `hanime1.me` 等 Cloudflare 站 | 同上,嗅 m3u8 绕过 |
| YouTube / 其他 yt-dlp 内置支持 | 嗅 m3u8 失败时 fallback 给 yt-dlp 直接下原 URL,加了 `impersonate=chrome-124` 参数过 Cloudflare |

### chromium SOCKS5 认证问题

- playwright proxy auth 只对 firefox/webkit 有效,**chromium 不支持带认证的 SOCKS5**
- 解决:worker 上跑 `gost`(`/usr/local/bin/gost`)做本地中继
  - `socks5://127.0.0.1:11080(无认证)` → `socks5://住宅IP:port(带认证)`
- chromium / yt-dlp 都连本地 11080,认证由 gost 在外层处理
- 见 `worker/socks_relay.py`,worker 启动每次任务前根据 `config.proxy` 启停 gost

### 国内 → 海外 SSH 不通(GFW)

- 本地 Windows 无法 SSH 海外 VPS(banner timeout)
- **不能直连 worker 部署**,必须通过中控中转(中控也是海外,中控 → worker 都海外内部互通)
- 本地访问中控 Web UI 走 HTTP(S),GFW 不拦 8501 这种端口
- 代码上传走 GitHub 中转(本地 push → 中控 pull),不走 scp/rsync

---

## 五、已修复 bug(全部已 commit 到 git)

| Bug | 表现 | 修复 |
|---|---|---|
| paramiko 4.0 SSH banner timeout | "Error reading SSH protocol banner" | 降级 paramiko < 4 |
| chromium 不支持 socks5 auth | "Browser does not support socks5 proxy authentication" | gost 中继 |
| yt-dlp 拒绝 jable.tv | "[Piracy] This website is no longer supported" | 浏览器嗅 m3u8,只把 m3u8 URL 给 yt-dlp |
| hanime1.me Cloudflare 拦 | HTTP 403 | 同上,嗅 m3u8 绕过 |
| X compose modal 被 Escape 误关 | textarea 变 X 主页 | 去掉 dismiss_all_overlays 里的 Escape |
| 发推按钮"激活"判断错 | 按钮始终视觉黑色,以为没激活 | 改用 `:not([disabled])` 选择器,等 disabled 属性消失而非 aria-disabled='false' |
| playwright strict mode locator 报错 | "tweetTextarea_0 resolved to 2 elements" | 全部 locator 加 `.first` |
| @ 边界判断错(中文紧贴 @) | `看@alice` 不识别 mention | parse_caption_for_mentions 用 `_is_ident_continuation()` 只判断 ASCII 字母数字 |
| 蓝V首发弹窗 / 各种 modal | textarea 被覆盖层挡住 click | 加 `dismiss_all_overlays`(只 sweep,不按 Escape) + click 失败用 force=True / JS focus 兜底 |
| 日志重复同步 | UI 上"Worker 启动"重复 N 遍 | sync_worker 用 `last_log_offset` 增量拉 |
| Alert 重复(18 条) | 同一截图反复创建 alert | sync_worker 加重复检测,同 worker+task 未解决的只创建一条 |
| 任务状态双写覆盖 | worker 写 done,daemon push 把 running 推回覆盖 | `push_tasks` 改成"读 worker 当前 tasks.json 合并",worker 已是终态(done/failed/human_required)的不覆盖 |

---

## 六、未完成 / TODO

### 优先级高

- [ ] **数据库自动备份**:加 cron 每天 dump 一份 `matrix.db`
- [ ] **日志/截图清理**:`logs` 表会无限增长,`screenshots/` 目录也会(`database.prune_logs()` 已写但没自动调用)
- [ ] **多 Worker 矩阵真实测试**:目前只跑通 1 台 worker,矩阵的本质验证未做
- [ ] **24-72 小时长跑稳定性**:chromium 长跑是否积累 RSS / 句柄泄漏,未知

### 优先级中

- [ ] **告警通知外推**:目前告警只在 UI 显示,没 Telegram / 邮件推送
- [ ] **Logs 页自动刷新**:目前是手动 F5,可加 `streamlit-autorefresh` 5 秒自动刷
- [ ] **Tasks 页"重置/重试" 按钮**:目前 reset 失败任务靠 SQL,UI 上没按钮
- [ ] **批量任务 CSV 导入**:目前 textarea 粘贴,30 条以上不方便
- [ ] **工作日 / 周末调度差异化**:防风控更彻底
- [ ] **任务文案模板化**:同一视频不同账号自动生成不同文案(矩阵防关联)

### 优先级低

- [ ] 视频站点黑白名单(目前任意站都尝试嗅 m3u8 + fallback yt-dlp)
- [ ] 浏览器深度指纹伪装(playwright-stealth)
- [ ] storageState 持久化(每次起新 chromium 都要重 set_cookies,可考虑保存 storage_state.json)
- [ ] @ 真实存在性校验(用户 @ 不存在的 ID,worker 会作为纯文本提交)

---

## 七、调试技巧

### 看 worker 真实状态(绕过 UI 的延迟)

```bash
# 在中控 console 跑(中控有 sshpass)
sshpass -p <worker密码> ssh root@<workerIP> 'cat /opt/twitter-worker/state.json'
sshpass -p <worker密码> ssh root@<workerIP> 'tail -50 /opt/twitter-worker/log.txt'
sshpass -p <worker密码> ssh root@<workerIP> 'cat /opt/twitter-worker/tasks.json'
```

### 实时跟 worker 日志

```bash
sshpass -p <worker密码> ssh root@<workerIP> 'tail -f /opt/twitter-worker/log.txt'
```

### 强制 reset 任务

```bash
sqlite3 /opt/twitter-matrix/master/data/matrix.db "UPDATE tasks SET status='pending', scheduled_at=NULL, started_at=NULL, error_message=NULL WHERE id=<TID>;"
```

### Worker 重启

```bash
sshpass -p <worker密码> ssh root@<workerIP> 'systemctl restart twitter-worker'
```

### 中控重启

```bash
systemctl restart twmatrix-ui twmatrix-daemon
```

### 看 daemon 跑没跑

```bash
systemctl status twmatrix-daemon --no-pager | head -20
journalctl -u twmatrix-daemon -n 50 --no-pager
```

### 数据库快查

```bash
sqlite3 /opt/twitter-matrix/master/data/matrix.db \
  "SELECT id, status, scheduled_at, finished_at, substr(video_url,1,60), substr(error_message,1,80) FROM tasks ORDER BY id;"

sqlite3 /opt/twitter-matrix/master/data/matrix.db \
  "SELECT id, nickname, status, last_heartbeat, traffic_used_gb FROM workers;"
```

---

## 八、未来需求(项目业主当前期望)

按时间线:

1. **当前阶段**:测 1 台 worker 跑 24-30 条混合任务的 3 天稳定性。间隔参数测试时改成 `5/10`,正常运营改回 `30/90`
2. **2-3 天后**:加第 2 台 worker(账号在指纹浏览器养够 2 天后)→ 验证矩阵并行不打架
3. **1-2 周后**:扩到 5-10 台 worker
4. **未来**:加调度规则(工作日/周末)、文案模板化、Telegram 告警、视频源管理后台(支持手动/半自动录入待发列表)

---

## 九、给下一位维护者的建议

1. **改代码先看 `tasks.json` 协议** — 中控 ↔ worker 主要靠这一个文件做状态机,改任何相关逻辑都要考虑双方读写顺序和合并语义
2. **修 worker 端 bug 后必须走"🔄 更新代码"** — 只 git push 不够,worker 上的代码不会自动同步;`update_worker_code` 现在是自动遍历 worker/ 目录所有 .py
3. **不要轻易改 popup_handler 的 sweep_known_popups 规则** — 太宽的 selector 会误关 X compose modal 自己的关闭按钮(吃过这个亏)
4. **不要按 Escape 来关浮层** — X compose 自己也响应 Escape,会把 modal 一并关掉
5. **新加视频站点不需要改代码** — `_sniff_m3u8` 对所有站点都尝试嗅,嗅到 m3u8 就用,嗅不到 fallback yt-dlp;只有 jable.tv 这种被 yt-dlp 黑名单的会硬失败
6. **别再用 force click 当万能解** — `disabled` 属性是真实存在的,React 不会处理 onClick,假成功最坑

---

> 仓库根目录 `.gitignore` 已经排除数据库 / venv / 截图 / 视频缓存,git push 不会泄漏运营数据。
> 凭证全部在 SQLite 里,数据库本身被 gitignore,所以 `git push` 不带凭证。如果改了 schema 加了新敏感字段,记得也 gitignore 对应导出。
