#!/bin/bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_DIR"

echo "========================================"
echo "  Twitter Matrix 一键部署脚本"
echo "========================================"

# ---------- Step 1: Git Push ----------
echo ""
echo "[1/3] 推送到 GitHub..."
git push origin main
echo "✅ Git push 完成"

# ---------- Step 2: 中控 VPS 更新 ----------
echo ""
MASTER_HOST="${MASTER_HOST:-}"
MASTER_PASS="${MASTER_PASS:-}"
MASTER_SSH_PORT="${MASTER_SSH_PORT:-22}"

if [ -z "$MASTER_HOST" ]; then
    read -rp "中控 VPS IP/域名 (直接回车跳过远程操作): " MASTER_HOST
fi

if [ -n "$MASTER_HOST" ]; then
    if [ -z "$MASTER_PASS" ]; then
        read -rsp "中控 SSH 密码: " MASTER_PASS
        echo ""
    fi

    if ! command -v sshpass >/dev/null 2>&1; then
        echo "⚠️  未安装 sshpass，尝试安装..."
        apt-get install -y sshpass 2>/dev/null || \
            yum install -y sshpass 2>/dev/null || \
            brew install hudochenkov/sshpass/sshpass 2>/dev/null || \
            { echo "❌ 无法自动安装 sshpass，请手动安装后重试"; exit 1; }
    fi

    echo "[2/3] 中控拉取代码并重启服务..."
    sshpass -p "$MASTER_PASS" ssh -o StrictHostKeyChecking=accept-new \
        -p "$MASTER_SSH_PORT" "root@$MASTER_HOST" \
        'cd /opt/twitter-matrix && git pull && systemctl restart twmatrix-ui twmatrix-daemon && echo "中控重启完成"'
    echo "✅ 中控更新完成"

    # ---------- Step 3: 更新所有 Worker 代码 ----------
    echo ""
    echo "[3/3] 推送最新代码到所有 Worker 并重启..."
    sshpass -p "$MASTER_PASS" ssh -o StrictHostKeyChecking=accept-new \
        -p "$MASTER_SSH_PORT" "root@$MASTER_HOST" \
        "$(cat <<'PYEOF'
cd /opt/twitter-matrix/master
source ../venv/bin/activate
python3 <<'PY'
import sys
sys.path.insert(0, '/opt/twitter-matrix/master')
import database as db
import sync as syn
workers = db.list_workers()
ok = 0; fail = 0
for w in workers:
    try:
        success, msg = syn.update_worker_code(w)
        print(f'  {w[\"nickname\"]}: {msg}')
        if success: ok += 1
        else: fail += 1
    except Exception as e:
        print(f'  {w[\"nickname\"]}: 失败 - {e}')
        fail += 1
print(f'Worker 更新: {ok} 成功, {fail} 失败')
PY
PYEOF
)"
    echo "✅ Worker 代码更新完成"
else
    echo "⏭️  跳过远程操作。请手动在 VPS 上执行:"
    echo "    cd /opt/twitter-matrix && git pull && systemctl restart twmatrix-ui twmatrix-daemon"
    echo "    然后在 Web UI → Workers 页点击 '🔄 更新代码'"
fi

echo ""
echo "========================================"
echo "  部署完成"
echo "========================================"
