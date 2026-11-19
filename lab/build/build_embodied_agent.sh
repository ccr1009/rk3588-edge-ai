#!/bin/bash
# build_embodied_agent.sh — 板上原生编译纯端侧具身智能体
#
# 为什么在板上编译：交叉编译会撞 glibc（PC Ubuntu 2.35 vs 板子 Debian 11 的 2.31）。
#
# 2026-11-19 变更：Agent 现在 #include "edgevision_protocol.h"（248B 帧协议唯一真源），
# 所以编译必须带 -I，且头文件要先同步到这个目录。
# 同步用 lab/build/deploy.sh agent（它会一并 scp 头文件），或手工：
#   scp lab/include/edgevision_protocol.h rk3588:/root/rkllm_deploy/
set -e
cd /root/rkllm_deploy

# 头文件就在当前目录，显式 -I. 避免依赖编译器的默认搜索顺序
if [ ! -f edgevision_protocol.h ]; then
    echo "✗ 缺少 edgevision_protocol.h（248B 协议真源）"
    echo "  先同步：scp lab/include/edgevision_protocol.h rk3588:/root/rkllm_deploy/"
    exit 1
fi

echo "=== 正在板端原生编译纯端侧具身智能体 rkllm_embodied_agent ==="
g++ -O2 -std=c++17 -I. rkllm_embodied_agent.cpp -L. -lrkllmrt -Wl,-rpath=. -o rkllm_embodied_agent
echo "=== 编译成功，启动端侧 Agent 闭环实测 ==="
./rkllm_embodied_agent "$@"
