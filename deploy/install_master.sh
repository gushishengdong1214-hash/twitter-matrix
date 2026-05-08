#!/bin/bash
# 中控 VPS 一键部署。在中控 VPS 上跑一次即可。
#
# 用法 1(交互式,会问你密码):
#   bash deploy/install_master.sh
#
# 用法 2(直接传密码):
#   TWMATRIX_UI_PASSWORD=你的密码 bash deploy/install_master.sh

set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/opt/twitter-matrix}"
UI_PORT="${UI_PORT:-8501}"
UI_PASSWORD="${TWMATRIX_UI_PASSWORD:-}"

if [ ! -d "$INSTALL_DIR/master" ]; then
    echo "错误:$INSTALL_DIR/master 不存在。先把项目代码上传到 $INSTALL_DIR"
    echo "(例如 git clone 到 $INSTALL_DIR)"
    exit 1
fi

if [ -z "$UI_PASSWORD" ]; then
    echo
    read -rp "设一个 Web UI 登录密码: " UI_PASSWORD
    if [ -z "$UI_PASSWORD" ]; then
        echo "密码不能为空"
        exit 1
    fi
fi

cd "$INSTALL_DIR"

echo ">> 安装系统包"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3 python3-pip python3-venv

echo ">> 建 venv 装依赖"
if [ ! -d master/venv ]; then
    python3 -m venv master/venv
fi
master/venv/bin/pip install --upgrade pip
master/venv/bin/pip install -r master/requirements.txt

echo ">> 初始化数据库"
master/venv/bin/python master/database.py

echo ">> 写 systemd unit:twmatrix-ui"
cat >/etc/systemd/system/twmatrix-ui.service <<EOF
[Unit]
Description=Twitter Matrix UI (Streamlit)
After=network.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR/master
Environment="TWMATRIX_UI_PASSWORD=$UI_PASSWORD"
ExecStart=$INSTALL_DIR/master/venv/bin/streamlit run app.py \\
    --server.address=0.0.0.0 \\
    --server.port=$UI_PORT \\
    --server.headless=true \\
    --browser.gatherUsageStats=false
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

echo ">> 写 systemd unit:twmatrix-daemon"
cat >/etc/systemd/system/twmatrix-daemon.service <<EOF
[Unit]
Description=Twitter Matrix Daemon (scheduler/sync)
After=network.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR/master
ExecStart=$INSTALL_DIR/master/venv/bin/python daemon.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable twmatrix-ui twmatrix-daemon
systemctl restart twmatrix-ui twmatrix-daemon
sleep 2
systemctl --no-pager -l status twmatrix-ui | head -n 15 || true

# 放开本地防火墙(evoxt 云防火墙要在控制面板手动放)
if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
    ufw allow "$UI_PORT/tcp" || true
fi
iptables -C INPUT -p tcp --dport "$UI_PORT" -j ACCEPT 2>/dev/null || \
    iptables -I INPUT -p tcp --dport "$UI_PORT" -j ACCEPT 2>/dev/null || true

PUBLIC_IP=$(curl -s --max-time 5 ifconfig.me || echo "<中控IP>")

echo
echo "============================================================"
echo "中控部署完成。"
echo
echo "Web UI: http://$PUBLIC_IP:$UI_PORT"
echo "登录密码: $UI_PASSWORD"
echo
echo "如果浏览器打不开,可能是 evoxt 的防火墙没放开 $UI_PORT 端口,"
echo "去控制面板放开,或改 UI_PORT=80 重跑这个脚本。"
echo "============================================================"
