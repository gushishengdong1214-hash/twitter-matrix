#!/bin/bash
# Worker VPS 一键依赖初始化(V3.1)
# 在新 VPS 上以 root 跑一次即可。完成后回中控 UI「节点管理」勾选「我已完成环境配置」并填写信息。
#
# 用法 1(本地 scp 到 VPS):
#     scp deploy/install_worker.sh root@<VPS>:/tmp/
#     ssh root@<VPS> "bash /tmp/install_worker.sh"
#
# 用法 2(VPS 上一行 curl):
#     curl -fsSL https://raw.githubusercontent.com/<owner>/<repo>/main/deploy/install_worker.sh | bash

set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/twitter-worker}"
GOST_VER="${GOST_VER:-2.11.5}"

if [ "$(id -u)" -ne 0 ]; then
    echo "请以 root 运行(sudo bash $0 或 ssh root)"
    exit 1
fi

echo
echo "============================================================"
echo " Worker VPS 环境初始化(V3.1)"
echo " 目标目录: $INSTALL_DIR"
echo "============================================================"

# ───────────────────────────────────────────
# 第 1 步:系统更新
# ───────────────────────────────────────────
echo
echo ">> [1/7] 系统更新"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get -y upgrade

# ───────────────────────────────────────────
# 第 2 步:系统包(ffmpeg + Python + 工具链 + 中文字体 + vnstat)
# ffmpeg 用于视频合规压制(ensure_video_compliance)
# fonts-noto-cjk 让 chromium 截图里的中文不变方块
# vnstat 用于 worker 月流量统计(worker/traffic.py)
# ───────────────────────────────────────────
echo
echo ">> [2/7] 系统包"
apt-get install -y \
    ffmpeg \
    python3 python3-pip python3-venv \
    git curl wget ca-certificates \
    vnstat \
    fonts-noto-cjk

# vnstat 第一次跑要 enable 服务,否则统计不到
systemctl enable --now vnstat 2>/dev/null || true

# ───────────────────────────────────────────
# 第 3 步:gost(Chromium 不支持认证 SOCKS5,必装)
# ───────────────────────────────────────────
echo
echo ">> [3/7] gost SOCKS5 中继"
if [ ! -x /usr/local/bin/gost ] || ! /usr/local/bin/gost -V 2>&1 | grep -q "${GOST_VER}"; then
    tmpdir=$(mktemp -d)
    (
        cd "$tmpdir"
        wget -q "https://github.com/ginuerzh/gost/releases/download/v${GOST_VER}/gost-linux-amd64-${GOST_VER}.gz" -O gost.gz
        gunzip -f gost.gz
        install -m 0755 gost /usr/local/bin/gost
    )
    rm -rf "$tmpdir"
fi
/usr/local/bin/gost -V

# ───────────────────────────────────────────
# 第 4 步:Worker 目录 + Python venv
# ───────────────────────────────────────────
echo
echo ">> [4/7] Worker 目录 + venv"
mkdir -p "$INSTALL_DIR"
if [ ! -d "$INSTALL_DIR/venv" ]; then
    python3 -m venv "$INSTALL_DIR/venv"
fi
"$INSTALL_DIR/venv/bin/pip" install --upgrade pip

# ───────────────────────────────────────────
# 第 5 步:V3.1 Python 依赖全集
# 与 worker/requirements.txt 一致,新增 playwright-stealth(指纹补丁)
# ───────────────────────────────────────────
echo
echo ">> [5/7] V3.1 Python 依赖"
"$INSTALL_DIR/venv/bin/pip" install \
    "playwright>=1.40" \
    "playwright-stealth>=1.0.6" \
    "yt-dlp>=2024.04.09" \
    "PySocks>=1.7" \
    "pyotp>=2.9" \
    "curl-cffi>=0.7"

# ───────────────────────────────────────────
# 第 6 步:Playwright Chromium + 系统依赖
# --with-deps 自动装 libnss/libxkbcommon/libasound 等运行时库
# ───────────────────────────────────────────
echo
echo ">> [6/7] Playwright Chromium 浏览器 + 系统依赖"
# 某些 VPS 上 venv 的 playwright 二进制会静默失败,
# 优先用 python -m playwright 更稳(绕过 PATH/脚本解析问题)
if ! "$INSTALL_DIR/venv/bin/python" -m playwright install --with-deps chromium; then
    echo "venv playwright install 失败,尝试系统 python3..."
    python3 -m playwright install --with-deps chromium
fi

# ───────────────────────────────────────────
# 第 7 步:环境就绪验证
# ───────────────────────────────────────────
echo
echo ">> [7/7] 环境验证"
echo "─── ffmpeg ───"
ffmpeg -version | head -1
echo "─── ffprobe ───"
ffprobe -version | head -1
echo "─── gost ───"
/usr/local/bin/gost -V
echo "─── python deps ───"
"$INSTALL_DIR/venv/bin/python" -c "import playwright, playwright_stealth, yt_dlp, pyotp, curl_cffi; print('python deps OK')"
echo "─── playwright sync ───"
"$INSTALL_DIR/venv/bin/python" -c "from playwright.sync_api import sync_playwright; print('playwright sync OK')"
echo "─── vnstat ───"
vnstat --version | head -1 || true

echo
echo "============================================================"
echo " Worker 环境就绪。"
echo
echo " 下一步:"
echo " 1) 回中控 Web UI -> 节点管理 -> 添加 Worker"
echo " 2) 勾选「我已完成上述环境配置」"
echo " 3) 填本机 IP / SSH / 代理 / 账号信息"
echo " 4) 提交后用「🚀 部署」推送 worker 代码并起 systemd 服务"
echo "============================================================"
