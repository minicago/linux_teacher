#!/usr/bin/env bash
# 后台运行（配合 systemd 或 nohup 使用）
cd "$(dirname "$0")"
mkdir -p share submit
nohup python3 fileserver.py --host 0.0.0.0 --port 8000 > server.log 2>&1 &
echo "已在后台启动，PID=$!"
echo "日志: $(pwd)/server.log"
