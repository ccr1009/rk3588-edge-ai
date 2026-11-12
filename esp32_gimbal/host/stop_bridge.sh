#!/bin/bash
# 停止桥接服务，让出 USB 设备
for pid in $(pgrep -f 'gimbal_bridge.py' 2>/dev/null); do
    kill -9 "$pid" 2>/dev/null
done
sleep 2
pgrep -f 'gimbal_bridge.py' >/dev/null 2>&1 && echo "桥接还在" || echo "桥接已停 ✓"
