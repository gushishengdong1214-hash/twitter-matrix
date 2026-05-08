#!/bin/bash
# 把本地代码同步到中控 VPS。
#
# 用法:
#   bash deploy/upload_to_master.sh <中控用户>@<中控IP> [目标目录,默认 /opt/twitter-matrix]

set -e

REMOTE="${1:?用法:bash $0 root@中控IP [/opt/twitter-matrix]}"
DEST="${2:-/opt/twitter-matrix}"

PROJ_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo ">> 同步 $PROJ_DIR/  →  $REMOTE:$DEST/"
rsync -avz --delete \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='master/data/' \
    --exclude='master/venv/' \
    --exclude='*.db' \
    "$PROJ_DIR/" "$REMOTE:$DEST/"

echo ">> 同步完成。下一步在中控上跑:"
echo "   ssh $REMOTE bash $DEST/deploy/install_master.sh"
