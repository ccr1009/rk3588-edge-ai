#!/usr/bin/env bash
# run_all_tests.sh —— 测试总入口（分发器）
#
# ===========================================================================
#  ⚠️ 2026-11-19 重写：这个脚本以前测的是**已经废弃的代码**。
#
#  原来的它号称「7 项全量自测」，实际打的是：
#      track/test_tracker.py      ┐
#      tests/test_bev.py          │ 2026-09 的早期 Python 原型
#      tests/test_gateway.py      │ （gateway/ bev/ pipeline/ track/
#      convert/export_onnx.py     │   simulate/ 这几个目录）
#      simulate/simulator_eval.py ┘
#      ./edgevision_pro
#
#  而项目真正的主线 —— lab/agent（7 个硬件工具）、lab/vision（9000 网关）、
#  lab/voice（语音链路）—— **一项都没被它覆盖**。
#  等于每天认真检查一栋已经搬空的楼，而真正住人的那栋没人看。
#
#  现在改成按「要不要板子」分流，因为这两类测试的代价差三个数量级：
#    不加参数    → 只跑 PC 侧检查（几十秒），任何改动后都该跑
#    --board     → 再跑板端主线回归（几分钟到十几分钟，会独占硬件）
#    --all       → 两者都跑
#
#  早期原型的测试**仍然保留**（代码还在，测试还有意义），
#  但它们不再是"默认动作"，而是显式放在 --legacy 下。
# ===========================================================================

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT" || exit 2

echo "=================================================================="
echo " rk3588-edge-ai 测试总入口"
echo "=================================================================="
echo "  默认      : PC 侧仓库体检（无需板子，约 30 秒）"
echo "  --board   : PC 侧 + 板端主线回归（需板子在线）"
echo "  --all     : 同上"
echo "  --legacy  : 额外跑 2026-09 早期 Python 原型测试（默认不跑）"
echo "=================================================================="

RUN_BOARD=0
RUN_LEGACY=0
for arg in "$@"; do
    case "$arg" in
        --board|--all) RUN_BOARD=1 ;;
        --legacy)      RUN_LEGACY=1 ;;
        -h|--help)
            echo ""
            echo "用法: $0 [--board|--all] [--legacy]"
            exit 0
            ;;
        *) echo "✗ 未知参数: $arg"; exit 2 ;;
    esac
done

FAILED=0

# ---------------------------------------------------------------------------
# 第一段：PC 侧（永远可跑）
# ---------------------------------------------------------------------------
echo ""
echo "##################################################################"
echo "#  第一段：PC 侧仓库体检"
echo "##################################################################"
bash "$SCRIPT_DIR/check_repo.sh" || FAILED=$((FAILED+1))

# ---------------------------------------------------------------------------
# 第二段：板端主线回归
# ---------------------------------------------------------------------------
if [ "$RUN_BOARD" = "1" ]; then
    echo ""
    echo "##################################################################"
    echo "#  第二段：板端主线回归"
    echo "##################################################################"
    bash "$SCRIPT_DIR/run_board_tests.sh" || FAILED=$((FAILED+1))
else
    echo ""
    echo "  （跳过板端回归。板子在线时加 --board 一起跑）"
fi

# ---------------------------------------------------------------------------
# 第三段：早期原型（显式 opt-in）
# ---------------------------------------------------------------------------
if [ "$RUN_LEGACY" = "1" ]; then
    echo ""
    echo "##################################################################"
    echo "#  第三段：2026-09 早期 Python 原型测试（历史代码，非主线）"
    echo "##################################################################"
    echo ""
    echo "  说明：这些目录（gateway/ bev/ pipeline/ track/ simulate/）"
    echo "        是 2026-09 的 Python 原型，已被 C++ 主线取代，"
    echo "        PROJECT_STRUCTURE.md 标注为「历史遗留，别动」。"
    echo "        保留测试是为了不破坏当时的设计记录。"
    echo ""

    if [ -d .venv ]; then
        PY=".venv/bin/python"
    else
        PY="python3"
        echo "  ⚠ 没有 .venv，用系统 python3（可能缺依赖）"
    fi

    if [ -f track/test_tracker.py ]; then
        echo "  [1/3] ByteTrack 关联测试"
        PYTHONPATH=. $PY -m unittest track/test_tracker.py || FAILED=$((FAILED+1))
    fi

    if [ -f tests/test_bev.py ]; then
        echo "  [2/3] BEV 空间感知测试"
        PYTHONPATH=. $PY tests/test_bev.py || FAILED=$((FAILED+1))
    fi

    if [ -f tests/test_gateway.py ]; then
        echo "  [3/3] 多流网关测试"
        PYTHONPATH=. $PY tests/test_gateway.py || FAILED=$((FAILED+1))
    fi
fi

# ---------------------------------------------------------------------------
echo ""
echo "=================================================================="
if [ "$FAILED" -eq 0 ]; then
    echo " 全部通过"
    echo "=================================================================="
    exit 0
else
    echo " $FAILED 个阶段有失败"
    echo "=================================================================="
    exit 1
fi
