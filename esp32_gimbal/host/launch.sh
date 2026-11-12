#!/bin/bash
# 启动一体化预览（干净版，避免 pkill 自杀）
set -u

CAM="${CAM_DEV:-/dev/video81}"
PORT="${GIMBAL_PORT:-/dev/ttyUSB0}"
shift 2>/dev/null || true
EXTRA="$*"

# 1. 停网关 + 停旧实例
systemctl stop edgevision 2>/dev/null
for pid in $(pgrep -f "^\./w4_edgevision_gimbal_preview" 2>/dev/null); do
    kill -9 "$pid" 2>/dev/null
done
sleep 2

# 2. 确认摄像头空闲
if fuser "$CAM" >/dev/null 2>&1; then
    echo "[!] $CAM 仍被占用：" >&2
    fuser -v "$CAM" 2>&1 >&2
    exit 1
fi

# 3. 启动
cd /root/lab || exit 1
echo "启动: ./w4_edgevision_gimbal_preview $CAM --gimbal $PORT $EXTRA"
nohup ./w4_edgevision_gimbal_preview "$CAM" --gimbal "$PORT" $EXTRA > /tmp/gp.log 2>&1 &
echo "PID=$!"
sleep 3
head -12 /tmp/gp.log
