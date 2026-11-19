#!/bin/bash
# build_patrol_daemon.sh — 板上原生编译巡检守护快层
# 为什么原生编译: 交叉编译有 glibc 风险(PC 端 2.35 vs 板子 Debian11 2.31), 一律板上 g++ 编译。
# 只依赖 pthread/std, 不链接 RKLLM —— 快层绝不加载大模型。
#
# 2026-11-19 变更：守护现在 #include "edgevision_protocol.h"（248B 帧协议唯一真源），
# 需要 -I../include。本脚本已 cd 到 lab/ 根，所以路径相对它写。
set -e
# 本脚本在 lab/build/，源码在 lab/agent/（2026-11-02 目录重整后）
# 反推 lab/ 根再按相对路径编译，无论从哪个目录调用都成立
cd "$(dirname "$0")/.."
SRC="agent/edgevision_patrol_daemon.cpp"
INC="include"

if [ ! -f "$INC/edgevision_protocol.h" ]; then
    echo "✗ 缺少 $INC/edgevision_protocol.h（248B 协议真源）"
    echo "  它应该在仓库的 lab/include/ 下。若在板上运行，先同步："
    echo "    scp -r lab/include rk3588:/root/lab/"
    exit 1
fi

echo "[build] g++ -O2 -std=c++17 -Wall -Wextra -I$INC $SRC -lpthread -o edgevision_patrol_daemon"
g++ -O2 -std=c++17 -Wall -Wextra -I"$INC" "$SRC" -lpthread -o edgevision_patrol_daemon

echo "[build] OK -> $(pwd)/edgevision_patrol_daemon"
echo "[note] 板上编译时源码位于 /root/lab/agent/，本脚本也可直接在那里运行"
