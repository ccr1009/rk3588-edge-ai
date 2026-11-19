#!/usr/bin/env bash
# check_repo.sh —— 不需要板子、不需要 NPU、不需要编译主线源码的「仓库体检」
#
# 为什么要有这个脚本（2026-11-19）：
#   项目里原本唯一被文档当作"一键全量自测"的是 scripts/run_all_tests.sh，
#   但它打的全是 2026-09 的早期 Python 原型（track/ tests/ convert/ simulate/），
#   而项目主线 lab/agent、lab/vision、lab/voice —— 面试要讲的东西 —— **零覆盖**。
#   等于每天认真检查一栋已经搬空的楼。
#
#   真正有价值的是 lab/tests/ 的那十几个用例，但它们都要连板子、要 NPU、
#   要独占麦克风和摄像头。于是"改完代码先验一下"这件事就没有低成本入口了。
#
#   这个脚本补的就是那个低成本入口：全部在 PC 上跑，几十秒出结果，
#   覆盖所有**不依赖硬件**就能验的东西。它不能替代板端回归，但能挡住绝大多数
#   "手滑改坏了"的低级错误。
#
# 用法：
#   ./scripts/check_repo.sh
#   ./scripts/check_repo.sh --verbose     # 打印每条检查的细节
#
# 退出码：0 = 全绿；1 = 有检查失败

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT" || { echo "✗ 无法进入仓库根目录"; exit 2; }

VERBOSE=0
[ "${1:-}" = "--verbose" ] && VERBOSE=1

FAILED=0
CHECKS=0

pass() { CHECKS=$((CHECKS+1)); printf '  \033[32m✓\033[0m %s\n' "$1"; }
fail() { CHECKS=$((CHECKS+1)); FAILED=$((FAILED+1)); printf '  \033[31m✗\033[0m %s\n' "$1"; }
skip() { printf '  \033[33m-\033[0m %s\n' "$1"; }

echo "=================================================================="
echo " check_repo.sh —— 仓库体检（PC 侧，无需板子）"
echo " 仓库: $REPO_ROOT"
echo "=================================================================="

# ---------------------------------------------------------------------------
# 1. 凭据泄漏（最重要，放第一条）
# ---------------------------------------------------------------------------
echo ""
echo "[1] 凭据 / 内网 IP 泄漏"
if [ -x "$SCRIPT_DIR/check_secrets.sh" ] || [ -f "$SCRIPT_DIR/check_secrets.sh" ]; then
    if bash "$SCRIPT_DIR/check_secrets.sh" >/tmp/_cs.log 2>&1; then
        pass "无凭据泄漏（8 条规则 0 命中）"
    else
        fail "发现凭据泄漏，详情："
        sed 's/^/      /' /tmp/_cs.log | tail -20
    fi
    rm -f /tmp/_cs.log
else
    fail "缺少 scripts/check_secrets.sh"
fi

# ---------------------------------------------------------------------------
# 2. 248B 协议口径（只做数值复核，不要求单一真源）
# ---------------------------------------------------------------------------
# 说明：协议结构体目前在 5 个 C++ 文件里各有副本（发送端 1 + 消费端 4）。
# 曾尝试抽出 lab/include/edgevision_protocol.h 做单一真源 + 编译期断言，
# 但那需要改 5 个主线 .cpp 和 5 个构建脚本，而 PC 上没有 C++ 编译器、
# 板子当时也不可达 —— 改动无法验证。项目已转入面试准备、不再演进协议，
# 这个风险窗口本来就关闭了，所以**回退**了那次重构（见 OPEN_ISSUES #18）。
#
# 这里保留的是当初用来交叉验证的数值复核：不依赖 C++ 编译器，
# 只用 Python struct 按 `#pragma pack(1)` 的规则独立复算一遍。
# 它证明的是「口径没算错」，不证明「各副本仍一致」—— 后者靠人改的时候对一眼。
echo ""
echo "[2] 248B 协议口径复核"
python3 - <<'PY' 2>/dev/null && pass "结构尺寸符合 248B 口径（BBox=24 / Header=8 / 整帧=248）" || fail "协议尺寸不符"
import struct, sys
box = struct.Struct("<ifffff")      # int32 + 5*float = 24B
hdr = struct.Struct("<II")          # 2*uint32     = 8B
ok = (box.size == 24 and hdr.size == 8 and hdr.size + 10*box.size == 248)
sys.exit(0 if ok else 1)
PY

# ---------------------------------------------------------------------------
# 3. Python 语法
# ---------------------------------------------------------------------------
echo ""
echo "[3] Python 语法"
py_files=$(git ls-files '*.py' | grep -v -E '^(data/|3rdparty/)' || true)
py_count=$(printf '%s\n' "$py_files" | grep -c . || true)
if [ "$py_count" = "0" ]; then
    skip "没有 Python 文件"
else
    py_bad=0
    while IFS= read -r f; do
        [ -z "$f" ] && continue
        [ -f "$f" ] || continue
        python3 -m py_compile "$f" 2>/dev/null || { py_bad=$((py_bad+1)); [ "$VERBOSE" = "1" ] && echo "      语法错误: $f"; }
    done <<< "$py_files"
    if [ "$py_bad" = "0" ]; then
        pass "$py_count 个 .py 语法全部通过"
    else
        fail "$py_bad / $py_count 个 .py 有语法错误"
    fi
    # 清理 py_compile 产物
    find . -name '__pycache__' -type d -not -path './.git/*' -exec rm -rf {} + 2>/dev/null || true
fi

# ---------------------------------------------------------------------------
# 4. Shell 语法
# ---------------------------------------------------------------------------
echo ""
echo "[4] Shell 语法"
sh_files=$(git ls-files '*.sh' || true)
sh_count=$(printf '%s\n' "$sh_files" | grep -c . || true)
sh_bad=0
while IFS= read -r f; do
    [ -z "$f" ] && continue
    [ -f "$f" ] || continue
    bash -n "$f" 2>/dev/null || { sh_bad=$((sh_bad+1)); [ "$VERBOSE" = "1" ] && echo "      语法错误: $f"; }
done <<< "$sh_files"
if [ "$sh_bad" = "0" ]; then
    pass "$sh_count 个 .sh 语法全部通过"
else
    fail "$sh_bad / $sh_count 个 .sh 有语法错误"
fi

# ---------------------------------------------------------------------------
# 5. systemd unit 完整性
# ---------------------------------------------------------------------------
echo ""
echo "[5] systemd unit 与 control_service 别名一致性"
# control_service 的 kServices[] 里每个 unit 都必须在 lab/services/ 里存在，
# 否则 clone 下来装不全，Agent 调用会失败。
missing=""
for u in edgevision edgevision-dashboard edgevision-dashboard-cam \
         edgevision-patrol edgevision-preview voice-gateway tts-daemon; do
    [ -f "lab/services/$u.service" ] || missing="$missing $u"
done
if [ -z "$missing" ]; then
    pass "7 个 unit 全部存在（含 9000 网关 edgevision.service）"
else
    fail "缺少 unit：$missing"
fi

# ---------------------------------------------------------------------------
# 6. 文档里的服务数量是否与实际一致
# ---------------------------------------------------------------------------
echo ""
echo "[6] 文档服务数量口径"
actual_unit=$(ls lab/services/*.service 2>/dev/null | wc -l | tr -d ' ')
actual_alias=$(grep -cE '^\s*\{"[a-z_]+",\s*"' lab/agent/rkllm_embodied_agent.cpp 2>/dev/null || echo 0)
doc_bad=""

# 根 AGENTS.md: "N 个 systemd unit"
n_agents=$(grep -oE '\*\*[0-9]+ 个 systemd unit\*\*|[0-9]+ 个 systemd unit' AGENTS.md 2>/dev/null \
           | grep -oE '[0-9]+' | head -1)
[ -n "$n_agents" ] && [ "$n_agents" != "$actual_unit" ] \
    && doc_bad="$doc_bad AGENTS.md(unit写$n_agents实$actual_unit)"

# README: "N 个服务别名 / M 个 systemd unit"
n_readme_alias=$(grep -oE '[0-9]+ 个服务别名' README.md 2>/dev/null | grep -oE '^[0-9]+' | head -1)
n_readme_unit=$(grep -oE '[0-9]+ 个 systemd unit' README.md 2>/dev/null | grep -oE '^[0-9]+' | head -1)
[ -n "$n_readme_alias" ] && [ "$n_readme_alias" != "$actual_alias" ] \
    && doc_bad="$doc_bad README.md(别名写$n_readme_alias实$actual_alias)"
[ -n "$n_readme_unit" ] && [ "$n_readme_unit" != "$actual_unit" ] \
    && doc_bad="$doc_bad README.md(unit写$n_readme_unit实$actual_unit)"

if [ -z "$doc_bad" ]; then
    pass "文档口径一致（$actual_unit 个 unit / $actual_alias 个别名）"
else
    fail "文档口径不一致:$doc_bad"
fi

# ---------------------------------------------------------------------------
# 7. LFS/大文件守卫：确认构建产物没被误提交
# ---------------------------------------------------------------------------
echo ""
echo "[7] 构建产物未被误提交"
artifacts=$(git ls-files | grep -E '(^|/)(build|build_release|build_native)/.*\.(o|so|a)$|\.pyc$|__pycache__' | head -5 || true)
if [ -z "$artifacts" ]; then
    pass "无 .o/.so/.pyc/__pycache__ 被跟踪"
else
    fail "有构建产物被跟踪："
    printf '%s\n' "$artifacts" | sed 's/^/      /'
fi

# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
echo ""
echo "=================================================================="
if [ "$FAILED" -eq 0 ]; then
    printf ' \033[32m全部通过\033[0m：%d 项检查\n' "$CHECKS"
    echo "=================================================================="
    echo ""
    echo "注意：本脚本只覆盖「不依赖硬件」的检查。"
    echo "      板端回归（Agent 12 用例 / regress 20 条）仍需："
    echo "        ./scripts/run_board_tests.sh"
    exit 0
else
    printf ' \033[31m%d / %d 项失败\033[0m\n' "$FAILED" "$CHECKS"
    echo "=================================================================="
    exit 1
fi
