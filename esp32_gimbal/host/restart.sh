#!/bin/bash
# 重启一体化预览（干净、可带参数）
set -u
CAM="${CAM_DEV:-/dev/video81}"
PORT="${GIMBAL_PORT:-/dev/ttyUSB0}"

# 只杀本程序，不要用宽泛的 pkill -f 把自己也杀了
for pid in $(pgrep -f 'w4_edgevision_gimbal_preview' 2>/dev/null); do
    # 排除当前脚本自身及其父 shell
    if [ "$pid" != "$$" ] && [ "$pid" != "$PPID" ]; then
        kill -9 "$pid" 2>/dev/null
    fi
done
sleep 2

if fuser "$CAM" >/dev/null 2>&1; then
    echo "[!] $CAM 被占用" >&2
    fuser -v "$CAM" 2>&1 >&2
    exit 1
fi

cd /root/lab || exit 1
nohup ./w4_edgevision_gimbal_preview "$CAM" --gimbal "$PORT" "$@" > /tmp/gp.log 2>&1 &
echo "PID=$!"
sleep 3
head -10 /tmp/gp.log
