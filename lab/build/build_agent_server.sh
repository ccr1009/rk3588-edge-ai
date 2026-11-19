#!/bin/bash
# build_agent_server.sh — 板上原生编译 8088 控制面 RPC 服务
#
# 为什么之前没有这个脚本：这个服务一直是"手动敲 g++ 命令"起来的，
# 命令只写在源文件顶部的注释里。结果是没有人能确认它到底是怎么编的、
# 带没带该带的 -I。补成脚本，让"怎么构建"有唯一答案。
#
# 依赖：只 std + pthread（不链接 RKNN / RKLLM），所以能在任何板子上编。
set -e
cd "$(dirname "$0")/.."

SRC="agent/edgevision_agent_server.cpp"
OUT="edgevision_agent_server"

echo "[build] g++ -O2 -std=c++17 -Wall -Wextra $SRC -lpthread -o $OUT"
g++ -O2 -std=c++17 -Wall -Wextra "$SRC" -lpthread -o "$OUT"

echo "[build] OK -> $(pwd)/$OUT"
echo "[note] 运行时监听 8088；9000 网关需另起（见 edgevision.service）"
