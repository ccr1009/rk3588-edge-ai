#!/usr/bin/env bash
# run_board_tests.sh —— 跑**真正的主线**回归（需要板子在线）
#
# 为什么单独一个脚本：
#   这些用例会独占摄像头/NPU/麦克风，必须人在场、且要花几分钟到十几分钟。
#   把它们和 PC 侧的快速检查混在一个入口里，结果就是"要么全跑太慢、
#   要么为了快而跳过它们" —— 后者正是项目之前的实际状态（主线零覆盖）。
#
# 覆盖的用例（全部在 lab/tests/，都是 2026-10 之后为 Agent 主线写的）：
#   · regress.py                20 条期望词+禁止词断言，守护"曾经修好过的坑"
#   · explore.py                15 项边界探测，撞未知问题
#   · agent_regression_test.py  12 用例，含故障注入，~6.5 分钟
#   · protocol_selftest         248B 协议契约（板端也编一遍，验证 pack 行为）
#
# 用法：
#   ./scripts/run_board_tests.sh              # 跑默认集合
#   ./scripts/run_board_tests.sh --quick      # 只跑 regress + 协议自检
#   ./scripts/run_board_tests.sh --only agent # 只跑 Agent 12 用例
#
# 环境变量：
#   EDGEVISION_BOARD_HOST   板子地址（不设则用 ssh 别名 rk3588）
#   EDGEVISION_SSH_KEY      SSH 私钥路径（可选）
#
# 退出码：0 = 全绿；1 = 有失败；2 = 连不上板子（**不是**静默跳过）

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT" || exit 2

HOST="${EDGEVISION_BOARD_HOST:-rk3588}"
SSH_OPTS="-o LogLevel=ERROR -o ConnectTimeout=8"
[ -n "${EDGEVISION_SSH_KEY:-}" ] && SSH_OPTS="$SSH_OPTS -i $EDGEVISION_SSH_KEY"
SSH="ssh $SSH_OPTS $HOST"

MODE="default"
case "${1:-}" in
    --quick) MODE="quick" ;;
    --only)  MODE="${2:-default}" ;;
    "")      ;;
    *)       echo "用法: $0 [--quick | --only regress|explore|agent|proto]"; exit 2 ;;
esac

echo "=================================================================="
echo " run_board_tests.sh —— 板端主线回归"
echo " 目标: $HOST"
echo " 模式: $MODE"
echo "=================================================================="

# ---------------------------------------------------------------------------
# 步 0：连通性。连不上就明确失败 —— 绝不静默跳过。
#   项目 OPEN_ISSUES 第 12 条就是这个教训：「回路不通时 bench 仍然产出
#   具体数字（0/10 + 幻觉文本），看起来像识别率差」。没有测量 != 失败，
#   但更不能伪装成通过。
# ---------------------------------------------------------------------------
echo ""
echo "[0] 板子连通性"
if ! $SSH "echo ok" >/dev/null 2>&1; then
    echo "  ✗ 连不上 $HOST"
    echo ""
    echo "  这不是测试失败，是**没有测量**。请先："
    echo "    1. 确认板子开机、网线插好"
    echo "    2. 设置地址：export EDGEVISION_BOARD_HOST=<board-ip>"
    echo "    3. 或确认 ssh 别名可用：ssh rk3588 'echo ok'"
    echo ""
    echo "  只想验不依赖硬件的东西？用 ./scripts/check_repo.sh"
    exit 2
fi
echo "  ✓ 已连接"

# ---------------------------------------------------------------------------
# 板端环境自检：最容易误判的几项
# ---------------------------------------------------------------------------
echo ""
echo "[1] 板端前置状态"
$SSH 'echo "  NPU 驱动: $(cat /sys/kernel/debug/rknpu/version 2>/dev/null || echo 未知)"; \
      echo "  9000 网关: $(systemctl is-active edgevision 2>/dev/null || echo 未装)"; \
      echo "  语音网关: $(systemctl is-active voice-gateway 2>/dev/null || echo 未装)"; \
      echo "  巡检守护: $(systemctl is-active edgevision-patrol 2>/dev/null || echo 未装)"'

# NPU IOMMU 是关键约束（硬约束 ⑥）：两个 rkllm 实例必崩。
# 跑 Agent 用例前必须确认没有别的 rkllm 在跑。
echo ""
echo "  检查是否有残留 rkllm 实例（IOMMU 只有 4 GiB，第二个必 errno 14）："
$SSH 'n=$(pgrep -c -f rkllm_embodied_agent 2>/dev/null || echo 0); \
      if [ "$n" -gt 1 ]; then echo "  ⚠ 有 $n 个 rkllm 实例，请先清理：pkill -f rkllm_embodied_agent"; \
      else echo "  ✓ 无冲突实例（$n 个）"; fi'

FAILED=0
run_remote() {
    local name="$1" cmd="$2"
    echo ""
    echo "------------------------------------------------------------------"
    echo " $name"
    echo "------------------------------------------------------------------"
    if $SSH "$cmd"; then
        echo "  ✓ $name 通过"
    else
        echo "  ✗ $name 失败"
        FAILED=$((FAILED+1))
    fi
}

# ---------------------------------------------------------------------------
# 协议契约（最便宜，先跑）
# ---------------------------------------------------------------------------
if [ "$MODE" = "default" ] || [ "$MODE" = "quick" ] || [ "$MODE" = "proto" ]; then
    echo ""
    echo "[2] 248B 协议契约（板端编译，验证 pack 在 aarch64 上的行为）"
    $SSH "mkdir -p /tmp/protochk" >/dev/null 2>&1
    scp $SSH_OPTS -q lab/include/edgevision_protocol.h "$HOST:/tmp/protochk/" 2>/dev/null
    if scp $SSH_OPTS -q lab/tests/protocol_selftest.cpp "$HOST:/tmp/protochk/" 2>/dev/null; then
        if $SSH "cd /tmp/protochk && g++ -std=c++17 -I. protocol_selftest.cpp -o pst && ./pst"; then
            echo "  ✓ 协议契约（板端）通过"
        else
            echo "  ✗ 协议契约（板端）失败"
            FAILED=$((FAILED+1))
        fi
    else
        echo "  ⚠ 传输失败，跳过板端协议自检"
    fi
fi

# ---------------------------------------------------------------------------
# regress.py：20 条期望词+禁止词断言
# ---------------------------------------------------------------------------
if [ "$MODE" = "default" ] || [ "$MODE" = "quick" ] || [ "$MODE" = "regress" ]; then
    run_remote "regress.py（20 条已知行为守护，约 2 分钟）" \
        "test -f /root/regress.py && python3 /root/regress.py || { echo '  缺少 /root/regress.py'; exit 1; }"
fi

# ---------------------------------------------------------------------------
# explore.py：边界探测
# ---------------------------------------------------------------------------
if [ "$MODE" = "default" ] || [ "$MODE" = "explore" ]; then
    run_remote "explore.py（15 项边界探测 + 3 项副作用检查，约 2 分钟）" \
        "test -f /root/explore.py && python3 /root/explore.py || { echo '  缺少 /root/explore.py'; exit 1; }"
fi

# ---------------------------------------------------------------------------
# agent_regression_test.py：12 用例，含故障注入，最慢
# ---------------------------------------------------------------------------
if [ "$MODE" = "default" ] || [ "$MODE" = "agent" ]; then
    echo ""
    echo "  提示：这一项约 6.5 分钟，含故障注入（会临时停掉 9000 网关再恢复）"
    run_remote "agent_regression_test.py（12 用例，约 6.5 分钟）" \
        "test -f /root/agent_regression_test.py && python3 /root/agent_regression_test.py --out-dir /tmp/regr-\$(date +%s) || { echo '  缺少 /root/agent_regression_test.py'; exit 1; }"
fi

# ---------------------------------------------------------------------------
echo ""
echo "=================================================================="
if [ "$FAILED" -eq 0 ]; then
    printf ' \033[32m全部通过\033[0m\n'
    echo "=================================================================="
    exit 0
else
    printf ' \033[31m%d 项失败\033[0m\n' "$FAILED"
    echo "=================================================================="
    echo " 详细输出见板端 /tmp 下的回归产物目录"
    exit 1
fi
