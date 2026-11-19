#!/bin/bash
# build_agent_server.sh — 板上原生编译 8088 控制面 RPC 服务
#
# 为什么之前没有这个脚本：这个服务一直是"手动敲 g++ 命令"起来的，
# 命令只写在源文件顶部的注释里。结果是没有人能确认它到底是怎么编的、
# 带没带该带的 -I。补成脚本，让"怎么构建"有唯一答案。
#
# 依赖：只 std + pthread（不链接 RKNN / RKLLM），所以能在任何板子上编。
#
# 2026-11-19 变更：源码现在 #include "edgevision_protocol.h"（248B 帧协议真源），
# 需要 -I../include。本脚本已 cd 到 lab/ 根，所以路径相对它写。
set -e
cd "$(dirname "$0")/.."

SRC="agent/edgevision_agent_server.cpp"
INC="include"
OUT="edgevision_agent_server"

if [ ! -f "$INC/edgevision_protocol.h" ]; then
    echo "✗ 缺少 $INC/edgevision_protocol.h（248B 协议真源）"
    echo "  它应该在仓库的 lab/include/ 下。若在板上运行，先同步："
    echo "    scp -r lab/include rk3588:/root/lab/"
    exit 1
fi

echo "[build] g++ -O2 -std=c++17 -Wall -Wextra -I$INC $SRC -lpthread -o $OUT"
g++ -O2 -std=c++17 -Wall -Wextra -I"$INC" "$SRC" -lpthread -o "$OUT"

echo "[build] OK -> $(pwd)/$OUT"
echo "[note] 运行时监听 8088；9000 网关需另起（见 edgevision.service）"
