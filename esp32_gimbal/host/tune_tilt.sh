#!/bin/bash
# tune_tilt.sh — 扫参数找最稳的俯仰追踪组合
#
# 背景：2026-11-06 第一次跑通俯仰闭环（有框、会抬头低头），但"太抖，上下晃动"。
#
# 抖动的机制（看代码，不是猜）：
#   每帧都算一次误差 → 每帧都下发指令 → 舵机还没到位，下一帧指令又来了
#   → 机械跟不上指令 → 看起来抖动
#
# 三个可调点：
#   --kp-tilt A   增益。小=温和但迟钝，大=灵敏但过冲
#   --smooth A    目标位置低通滤波。0=关，0.3~0.5 通常刚好
#   --settle N    下发后暂停 N 帧再算。0=关，3~6 能显著压抖动
#
# 本脚本按组合依次启动、跑固定时长、抓日志里的 tilt 值序列，
# 算出「抖动幅度」= tilt 值的标准差，越小越稳。
#
# ★ 需要你站在镜头前不动 ★ 否则测的是你自己在动，不是抖动。
#
# 用法：
#   bash /root/esp32_gimbal/tune_tilt.sh
# （每轮 20 秒，共 6 轮 ≈ 2 分钟）

set -u

DUR=20        # 每轮测试时长（秒）
CAM=/dev/video81
BIN=/root/lab/w4_edgevision_gimbal_preview
OUTDIR=/tmp/tune
mkdir -p "$OUTDIR"

# 组合：kp_tilt smooth settle
COMBOS=(
  "0.15 0.0  0"     # 当前默认
  "0.10 0.0  0"     # 只降增益
  "0.15 0.35 0"     # 只加平滑
  "0.15 0.0  5"     # 只加 settle
  "0.10 0.35 5"     # 三个都调
  "0.08 0.30 4"     # 更保守
)

echo "================================================================"
echo "俯仰追踪调参扫描"
echo "================================================================"
echo
echo "★★ 请现在站到摄像头前 1.5~2 米处，尽量保持不动 ★★"
echo "   全程约 $(( ${#COMBOS[@]} * (DUR + 6) )) 秒"
echo
for i in $(seq 5 -1 1); do
    printf "\r   %d 秒后开始...  " "$i"
    sleep 1
done
printf "\r                        \r"

# 停掉旧的
for pid in $(pgrep -f 'w4_edgevision_gimbal_preview' 2>/dev/null); do
    kill -9 "$pid" 2>/dev/null
done
systemctl stop edgevision 2>/dev/null
sleep 1

# 确保桥接在
if ! ss -tln 2>/dev/null | grep -q 9100; then
    bash /root/esp32_gimbal/restart_bridge.sh >/dev/null 2>&1
    sleep 4
fi

# 确保引脚映射
cat > /tmp/_remap2.py <<'PYEOF'
import sys, time
sys.path.insert(0, "/root/esp32_gimbal")
from usb_cdc_serial import UsbCdcSerial
try:
    s = UsbCdcSerial(timeout=0.5)
    s.reset_input_buffer()
    for c in ["D 0", "A", "G 15", "T 14", "P 0 0"]:
        s.write((c + "\n").encode()); s.flush(); time.sleep(0.35)
    s.write(b"I\n"); s.flush(); time.sleep(0.8)
    print("  pins:", s.readline().decode("utf-8","replace").strip())
    s.close()
except Exception as e:
    print("  remap skip:", e)
PYEOF
echo
echo "--- 引脚确认 ---"
bash /root/esp32_gimbal/stop_bridge.sh >/dev/null 2>&1 || true
sleep 1
/root/rknn_env/bin/python /tmp/_remap2.py 2>&1
bash /root/esp32_gimbal/restart_bridge.sh >/dev/null 2>&1
sleep 4

echo
printf "%-6s %-8s %-8s %-8s %-10s %-10s %s\n" "轮次" "kp" "smooth" "settle" "tilt标准差" "tilt范围" "锁定率"
echo "------------------------------------------------------------------------"

best_idx=-1
best_std=999999

idx=0
for combo in "${COMBOS[@]}"; do
    idx=$((idx + 1))
    read -r kp sm st <<< "$combo"

    log="$OUTDIR/run_${idx}.log"
    : > "$log"

    # 启动（关掉 --no-pan 之外的一切增强，只测这三个变量）
    (
        cd /root/lab || exit 1
        exec "$BIN" "$CAM" --bridge 127.0.0.1:9100 \
            --no-pan --no-3d \
            --kp-tilt "$kp" \
            --smooth "$sm" \
            --settle "$st"
    ) > "$log" 2>&1 &
    pid=$!

    sleep "$DUR"

    kill -9 "$pid" 2>/dev/null
    wait "$pid" 2>/dev/null
    sleep 1

    # 从日志提取 tilt 值：格式 "P+0.0 T-7.5 LOCK"
    vals=$(grep -oE 'T[-+][0-9]+\.[0-9]+' "$log" | sed 's/T//' )
    total=$(echo "$vals" | grep -c . 2>/dev/null || echo 0)
    lock_n=$(grep -oE '[0-9]+ [A-Z]' "$log" 2>/dev/null | head -1 || echo 0)
    # 统计帧数、锁定帧数（尾部状态字是 LOCK / TRACK / LOST / SATURATED）
    lock_cnt=$(grep -oE 'LOCK\b' "$log" 2>/dev/null | wc -l)
    lost_cnt=$(grep -oE 'LOST\b' "$log" 2>/dev/null | wc -l)
    track_cnt=$(grep -oE 'TRACK\b' "$log" 2>/dev/null | wc -l)

    if [ "${total:-0}" -lt 5 ]; then
        printf "%-6s %-8s %-8s %-8s %-10s %-10s %s\n" "$idx" "$kp" "$sm" "$st" "无数据" "-" "-"
        continue
    fi

    # ★ 关键修正：tilt 全程几乎不动（标准差≈0）有两种可能：
    #     a) 真的稳（好）
    #     b) 根本没检测到人，tilt 停在某个值没动过（假数据！）
    # 必须用「有没有出现 LOCK/TRACK」来区分，否则会把"没目标"误判成"最稳"。
    # 第一版就是这里漏了：settle 那两轮全是 0.00°，被选中了。
    see_target=$((lock_cnt + track_cnt))
    if [ "$see_target" -lt 10 ]; then
        printf "%-6s %-8s %-8s %-8s %-10s %-10s %s\n" \
            "$idx" "$kp" "$sm" "$st" "无效" "-" "只看到目标 ${see_target} 帧"
        continue
    fi

    stats=$(echo "$vals" | awk '
        {v[NR]=$1; s+=$1}
        END{
            n=NR; if(n<2){print "0 0 0"; exit}
            m=s/n
            for(i=1;i<=n;i++){d=v[i]-m; ss+=d*d}
            sd=sqrt(ss/n)
            mn=v[1]; mx=v[1]
            for(i=2;i<=n;i++){if(v[i]<mn)mn=v[i]; if(v[i]>mx)mx=v[i]}
            printf "%.2f %.1f %.1f", sd, mn, mx
        }')

    read -r sd mn mx <<< "$stats"
    lock_pct=$(awk -v l="$see_target" -v t="$total" \
        'BEGIN{if(t>0) printf "%.0f%%", 100*l/t; else print "-"}')

    printf "%-6s %-8s %-8s %-8s %-10s %-10s %s\n" \
        "$idx" "$kp" "$sm" "$st" "${sd}°" "${mn}~${mx}°" "$lock_pct"

    # 用 awk 比较浮点
    if awk -v a="$sd" -v b="$best_std" 'BEGIN{exit !(a < b)}'; then
        best_std=$sd
        best_idx=$idx
        best_combo="$combo"
    fi
done

echo
echo "================================================================"
if [ "$best_idx" -gt 0 ]; then
    read -r bkp bsm bst <<< "$best_combo"
    echo " 最稳的组合：第 $best_idx 轮"
    echo "   --kp-tilt $bkp --smooth $bsm --settle $bst"
    echo "   tilt 标准差 ${best_std}°（越小越稳）"
    echo
    echo " 用这个参数启动："
    echo "   cd /root/lab"
    echo "   ./w4_edgevision_gimbal_preview /dev/video81 \\"
    echo "       --bridge 127.0.0.1:9100 --no-pan --no-3d \\"
    echo "       --kp-tilt $bkp --smooth $bsm --settle $bst"
else
    echo " 没拿到有效数据。可能："
    echo "   · 人不在镜头前（日志里没有 LOCK）"
    echo "   · 或者 LOST 状态太多"
    echo " 完整日志在 $OUTDIR/run_*.log"
fi
echo "================================================================"
