#!/usr/bin/env bash
# ============================================================================
#  sync-all.sh —— 一条命令把本地改动同步到三个 GitHub 仓库
#
#  背景：本地是一个 git 仓库（含 stereo3d/ 与 esp32_gimbal/ 两个子目录），
#        但 GitHub 上拆成了三个独立仓库。本脚本负责把子目录推到各自的仓库。
#
#  用法：
#    bash scripts/sync-all.sh                    # 同步全部（会先问确认）
#    bash scripts/sync-all.sh -m "改了XX"         # 带 commit message
#    bash scripts/sync-all.sh --dry-run          # 只看会做什么，不执行
#    bash scripts/sync-all.sh --only main        # 只推主仓库
#    bash scripts/sync-all.sh --only stereo3d    # 只推 stereo3d
#
#  设计原则：
#    ① 主仓库是唯一真相源 —— stereo3d/ 和 esp32_gimbal/ 目录才是权威副本，
#       独立仓库是它的镜像。改代码永远改主仓库，不要直接改独立仓库。
#    ② 推送前先做内容一致性检查，不一致时给出明确提示而不是硬推。
#    ③ 任何一步失败就停下，不继续做后续操作（避免"部分成功"的混乱状态）。
# ============================================================================
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1

REPOS="main stereo3d gimbal"
MSG=""
DRY_RUN=0
ONLY=""

while [ $# -gt 0 ]; do
    case "$1" in
        -m|--message) MSG="${2:-}"; shift 2 ;;
        --dry-run)    DRY_RUN=1; shift ;;
        --only)       ONLY="${2:-}"; shift 2 ;;
        -h|--help)    sed -n '2,25p' "$0"; exit 0 ;;
        *) echo "未知参数: $1"; exit 2 ;;
    esac
done

[ -n "$ONLY" ] && REPOS="$ONLY"

run() {
    if [ "$DRY_RUN" = "1" ]; then
        echo "    [dry-run] $*"
    else
        "$@"
    fi
}

# ---------------------------------------------------------------------------
# 前置检查
# ---------------------------------------------------------------------------
echo "══════════════════════════════════════════════"
echo " EdgeVision 三仓库同步"
echo "══════════════════════════════════════════════"

if ! git remote get-url origin >/dev/null 2>&1; then
    echo "✗ 没有 origin remote"; exit 1
fi
for r in stereo3d gimbal; do
    if [ "$r" = "main" ]; then continue; fi
    if ! git remote get-url "$r" >/dev/null 2>&1; then
        echo "✗ 缺少 remote: $r"
        echo "  修复：git remote add $r git@github.com:ccr1009/<repo>.git"
        exit 1
    fi
done
echo "✓ remote 配置齐全"

# 工作区必须干净，否则 commit 会漏文件
if [ -n "$(git status --porcelain)" ] && [ "$DRY_RUN" = "0" ]; then
    echo ""
    echo "⚠ 有未提交改动："
    git status --short | head -20
    echo ""
    if [ -z "$MSG" ]; then
        echo "✗ 请用 -m \"说明\" 指定 commit message，或先手动提交"
        exit 1
    fi
fi

# ---------------------------------------------------------------------------
# 1. 提交并推送主仓库
# ---------------------------------------------------------------------------
case " $REPOS " in
*" main "*)
    echo ""
    echo "──── ① 主仓库 rk3588-edge-ai ────"
    if [ -n "$(git status --porcelain)" ]; then
        run git add -A
        run git commit -m "${MSG:-chore: 同步改动}"
    else
        echo "  无改动，跳过提交"
    fi
    run git push origin main || { echo "✗ 主仓库推送失败"; exit 1; }
    echo "  ✓ 已推送 $(git rev-parse --short main)"
    ;;
esac

# ---------------------------------------------------------------------------
# 2. 同步子树到独立仓库
# ---------------------------------------------------------------------------
sync_subtree() {
    local prefix="$1" remote="$2" branch="$3" label="$4"

    echo ""
    echo "──── $label ────"

    # 一致性检查：主仓库的子树 split 结果 vs 独立仓库的 main
    # ⚠️ 必须用 split 出来的子树比，不能用主仓库 main 直接比 ——
    #    main 里含整个项目（lab/ deploy/ docs/ …），跟独立仓库必然"不一致"。
    local tmpsub="_sync_check_$$"
    git branch -D "$tmpsub" >/dev/null 2>&1
    local subsha
    subsha=$(git subtree split --prefix="$prefix" -b "$tmpsub" 2>/dev/null | tail -1)
    git branch -D "$tmpsub" >/dev/null 2>&1

    if [ -z "$subsha" ]; then
        echo "  ✗ 子树切分失败: $prefix"; return 1
    fi

    # 拉取远端最新，确保比较的是真实远端状态
    git fetch "$remote" >/dev/null 2>&1

    local diff
    diff=$(git diff --stat "$subsha" "$remote/main" 2>/dev/null)
    if [ -n "$diff" ]; then
        echo "  ⚠ 主仓库 $prefix/ 子树与 $remote/main 内容不一致："
        echo "$diff" | head -8 | sed 's/^/      /'
        echo ""
        echo "  这通常意味着独立仓库被直接改过（本脚本约定：只改主仓库）。"
        echo "  若确认以主仓库为准，执行："
        echo "      git subtree push --prefix=$prefix $remote main --force"
        return 1
    fi
    echo "  ✓ 内容一致（本地子树 = $remote/main）"

    run git subtree push --prefix="$prefix" "$remote" main || {
        echo "  ✗ 推送失败（可能是快进不了）"
        echo "    查看差异: git log $remote/main..main --oneline"
        return 1
    }
    return 0
}

case " $REPOS " in
*" stereo3d "*)
    sync_subtree stereo3d stereo3d main "② stereo3d-rk3588" || exit 1
    ;;
esac

case " $REPOS " in
*" gimbal "*)
    sync_subtree esp32_gimbal gimbal main "③ esp32-gimbal-rk3588" || exit 1
    ;;
esac

# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
echo ""
echo "══════════════════════════════════════════════"
echo " 完成"
echo "══════════════════════════════════════════════"
# 汇总：主仓库用本地 main 的 SHA；子树用 split 出来的 SHA。
# ⚠️ 不要用 $remote/main —— dry-run 时不会 fetch，读到的是过期 ref。
printf "  %-24s %s\n" "rk3588-edge-ai" "$(git rev-parse --short main 2>/dev/null || echo '?')"
for pair in "stereo3d:stereo3d-rk3588" "esp32_gimbal:esp32-gimbal-rk3588"; do
    prefix="${pair%%:*}"; name="${pair##*:}"
    tmpsub="_sync_sum_$$"
    git branch -D "$tmpsub" >/dev/null 2>&1
    sha=$(git subtree split --prefix="$prefix" -b "$tmpsub" 2>/dev/null | tail -1)
    git branch -D "$tmpsub" >/dev/null 2>&1
    printf "  %-24s %s\n" "$name" "${sha:0:7}"
done

