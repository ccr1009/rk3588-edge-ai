#!/bin/bash
# tune_settle.sh — 只测 settle 参数（kp 固定为已验证最稳的 0.10）
#
# 上一轮结论（2026-11-06）：
#   kp 0.15 → tilt 标准差 6.96°
#   kp 0.10 → tilt 标准差 4.39°   ← 改善 37%，采用
#
# 这轮固定 kp=0.10，只改变 settle（下发后暂停帧数），找最佳值。
# 理论：settle 让舵机有时间到位再去算下一帧，能显著压抖动。
#
# 每轮之间会打印明显的分隔提示，提醒人保持不动（避免上一轮那种
# 后几轮人累了晃、被误判成"参数无效"的问题）。

set -u

DUR=18
CAM=/dev/video81
BIN=/root/lab/w4_edgevision_gimbal_preview
OUTDIR=/tmp/tune2
mkdir -p "$OUTDIR"

KP=0.10
SETTLES=(0 3 5 8 12)

echo "================================================================"
echo " settle 参数扫描（kp 固定 $KP）"
echo "================================================================"
echo
echo "★★ 请站到镜头前 1.5~2 米，全程保持不动 ★★"
echo "   共 ${#SETTLES[@]} 轮 × ${DUR}s ≈ $(( ${#SETTLES[@]} * (DUR+5) )) 秒"
echo

# 停旧的 + 准备环境
for pid in $(pgrep -f 'w4_edgevision_gimbal_preview' 2>/dev/null); do
    kill -9 "$pid" 2>/dev/null
done
systemctl stop edgevision 2>/dev/null
sleep 1

bash /root/esp32_gimbal/stop_bridge.sh >/dev/null 2>&1 || true
sleep 1

cat > /tmp/_remap3.py <<'PYEOF'
import sys, time
sys.path.insert(0, "/root/esp32_gimbal")
from usb_cdc_serial import UsbCdcSerial
try:
    s = UsbCdcSerial(timeout=0.5)
    s.reset_input_buffer()
    for c in ["D 0", "A", "G 15", "T 14", "P 0 0"]:
        s.write((c + "\n").encode()); s.flush(); time.sleep(0.35)
    s.write(b"I\n"); s.flush(); time.sleep(0.8)
    print("  引脚:", s.readline().decode("utf-8","replace").strip())
    s.close()
except Exception as e:
    print("  映射跳过:", e)
PYEOF
/root/rknn_env/bin/python /tmp/_remap3.py 2>&1
bash /root/esp32_gimbal/restart_bridge.sh >/dev/null 2>&1
sleep 4

printf "\n%-6s %-10s %-12s %-12s %s\n" "轮次" "settle" "抖动(std)" "tilt范围" "看到目标帧数"
echo "----------------------------------------------------------------"

best_sd=99999
best_settle=""

idx=0
for st in "${SETTLES[@]}"; do
    idx=$((idx + 1))

    echo
    echo "=============================================================="
    echo "  第 $idx 轮 / ${#SETTLES[@]}   settle=$st"
    echo "  >>> 请保持不动，${DUR} 秒 <<<"
    echo "=============================================================="

    log="$OUTDIR/s${st}.log"
    : > "$log"

    (
        cd /root/lab || exit 1
        exec "$BIN" "$CAM" --bridge 127.0.0.1:9100 \
            --no-pan --no-3d \
            --kp-tilt "$KP" \
            --settle "$st"
    ) > "$log" 2>&1 &
    pid=$!

    sleep "$DUR"
    kill -9 "$pid" 2>/dev/null
    wait "$pid" 2>/dev/null
    sleep 1

    vals=$(grep -oE 'T[-+][0-9]+\.[0-9]+' "$log" | sed 's/T//')
    total=$(echo "$vals" | grep -c . 2>/dev/null || echo 0)
    lock_cnt=$(grep -oE 'LOCK\b' "$log" 2>/dev/null | wc -l)
    track_cnt=$(grep -oE 'TRACK\b' "$log" 2>/dev/null | wc -l)
    see=$((lock_cnt + track_cnt))

    if [ "${total:-0}" -lt 5 ] || [ "$see" -lt 8 ]; then
        printf "%-6s %-10s %-12s %-12s %s\n" "$idx" "$st" "无效" "-" "只看到 ${see} 帧"
        continue
    fi

    stats=$(echo "$vals" | awk '
        {v[NR]=$1; s+=$1}
        END{
            n=NR; if(n<2){print "0 0 0"; exit}
            m=s/n
            for(i=1;i<=n;i++){d=v[i]-m; ss+=d*d}
            printf "%.2f %.1f %.1f", sqrt(ss/n), v[1], v[1]
        }')
    # 单独算极值
    mn=$(echo "$vals" | sort -g | head -1)
    mx=$(echo "$vals" | sort -g | tail -1)
    sd=$(echo "$stats" | awk '{print $1}')

    printf "%-6s %-10s %-12s %-12s %s\n" \
        "$idx" "$st" "${sd}°" "${mn}~${mx}°" "$see"

    if awk -v a="$sd" -v b="$best_sd" 'BEGIN{exit !(a < b)}'; then
        best_sd=$sd
        best_settle=$st
    fi
done

echo
echo "================================================================"
if [ -n "$best_settle" ]; then
    echo " 最佳 settle = $best_settle   抖动 ${best_sd}°"
    echo
    echo " 启动命令："
    echo "   cd /root/lab"
    echo "   ./w4_edgevision_gimbal_preview /dev/video81 \\"
    echo "       --bridge 127.0.0.1:9100 --no-pan --no-3d \\"
    echo "       --kp-tilt $KP --settle $best_settle"
else
    echo " 全是无效轮次 —— 可能人不在镜头前或角度太偏"
    echo " 日志在 $OUTDIR/"
fi
echo "================================================================"
