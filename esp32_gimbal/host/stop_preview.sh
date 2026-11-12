#!/bin/bash
# 停止预览程序（保留桥接服务），用于独占测试
for pid in $(pgrep -f 'w4_edgevision_gimbal_preview' 2>/dev/null); do
    kill -9 "$pid" 2>/dev/null
done
sleep 1
if pgrep -f 'w4_edgevision_gimbal_preview' >/dev/null 2>&1; then
    echo "预览还在跑"
else
    echo "预览已停止 ✓"
fi
if ss -lnt 2>/dev/null | grep -q ':9100'; then
    echo "桥接服务在跑 ✓ (可以单独测试)"
else
    echo "桥接服务不在了"
fi
