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

# ⚠️ 关键：强制 autocrlf=true。
#   Git Bash 里的 git 有时读不到 Windows 全局配置的 core.autocrlf，
#   于是它把 117 个本来正常的文件全判成"已修改"，`git status` 显示一堆 M 号。
#   实测对比（同一目录）：
#     git status                      -> 117 个改动
#     git -c core.autocrlf=true status -> 1 个改动（只有真正在改的那个）
#   `git diff --numstat` 也印证是纯换行符差异（19220 增 / 19220 删，完全对称）。
#   不设这一行，本脚本会永远卡在"有未提交改动"。
GIT="git -c core.autocrlf=true -c core.safecrlf=false"

# 定位仓库根。
# ⚠️ 不要用 `cd "$(dirname "$0")/.."` ——
#   在 Git Bash 里，脚本若被用 Windows 绝对路径调用（D:\...），
#   这个路径 bash 解析不了，cd 失败后所有 git 命令会跑到错误的目录，
#   表现为「明明工作区干净，脚本却报有未提交改动」。
#   用 git 自己判断更可靠：只要在仓库内任意位置调用都能找到根。
if ! REPO_ROOT=$($GIT rev-parse --show-toplevel 2>/dev/null); then
    echo "✗ 当前目录不在任何 git 仓库内"
    exit 1
fi
cd "$REPO_ROOT" || exit 1

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

if ! $GIT remote get-url origin >/dev/null 2>&1; then
    echo "✗ 没有 origin remote"; exit 1
fi
for r in stereo3d gimbal; do
    if [ "$r" = "main" ]; then continue; fi
    if ! $GIT remote get-url "$r" >/dev/null 2>&1; then
        echo "✗ 缺少 remote: $r"
        echo "  修复：$GIT remote add $r git@github.com:ccr1009/<repo>.git"
        exit 1
    fi
done
echo "✓ remote 配置齐全"

# 工作区状态检查。
# ⚠️ 有一类"假改动"必须识别出来：Git Bash 与 Windows 版 git 对
#   core.autocrlf 的处理不同，同一份文件在 bash 里看是"已修改"，在
#   PowerShell 里看是干净的，`git diff --stat` 显示 insertions == deletions
#   （如 19220 增 / 19220 删，完全对称）。
#   那是纯 CRLF↔LF 差异，没有真实内容变化。此时不该拦着不让同步，
#   否则脚本永远卡在"有未提交改动"。
#
# 判据：把 status 输出交给 git diff --numstat 看真实行数变化，
#       如果所有文件都是 +N/-N 且 N 相等，就认为是换行符伪改动。
real_changes() {
    local n
    n=$($GIT diff --numstat 2>/dev/null | awk '$1 != $2 { c++ } END { print c+0 }')
    [ "${n:-0}" -gt 0 ]
}

HAS_CHANGES=0
if [ -n "$($GIT status --porcelain)" ]; then
    if real_changes; then
        HAS_CHANGES=1
    else
        # 只有换行符差异：刷新索引，不产生 commit
        echo "ℹ 检测到 $($GIT status --porcelain | wc -l) 个文件的换行符差异（无内容变化），已刷新索引"
        $GIT add -A >/dev/null 2>&1
        if [ -n "$($GIT status --porcelain)" ]; then
            HAS_CHANGES=1
        fi
    fi
fi

if [ "$HAS_CHANGES" = "1" ] && [ "$DRY_RUN" = "0" ]; then
    echo ""
    echo "⚠ 有真实未提交改动："
    $GIT status --short | head -20
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
    # 只提交"真实改动"；换行符伪改动已在上面被 git add 刷掉，不会进 commit
    if [ -n "$($GIT status --porcelain)" ] && real_changes; then
        run $GIT add -A
        run $GIT commit -m "${MSG:-chore: 同步改动}"
    elif [ -n "$($GIT status --porcelain)" ]; then
        echo "  仅换行符差异，不产生提交"
        run $GIT add -A
    else
        echo "  无改动，跳过提交"
    fi
    run $GIT push origin main || { echo "✗ 主仓库推送失败"; exit 1; }
    echo "  ✓ 已推送 $($GIT rev-parse --short main)"
    ;;
esac

# ---------------------------------------------------------------------------
# 2. 同步子树到独立仓库
# ---------------------------------------------------------------------------
sync_subtree() {
    local prefix="$1" remote="$2" branch="$3" label="$4"

    echo ""
    echo "──── $label ────"

    # ⚠️ 性能关键：先看目录 tree 有没有变，变了才跑 subtree split。
    #   `git subtree split` 要遍历全部历史重建提交链，本仓库实测 50s/次。
    #   但 `git rev-parse main:$prefix` 只读一个 tree 对象，毫秒级。
    #   日常"改主项目、子项目没动"是最常见的情况，这一层能省 100 秒。
    local cur_tree
    cur_tree=$($GIT rev-parse "main:$prefix" 2>/dev/null)
    if [ -z "$cur_tree" ]; then
        echo "  ✗ 取不到 main:$prefix 的 tree"; return 1
    fi

    $GIT fetch "$remote" >/dev/null 2>&1
    local remote_tree
    remote_tree=$($GIT rev-parse "refs/remotes/$remote/main^{tree}" 2>/dev/null)

    if [ -n "$remote_tree" ] && [ "$cur_tree" = "$remote_tree" ]; then
        echo "  ✓ 无变化（tree ${cur_tree:0:7} 与 $remote/main 相同），跳过"
        return 0
    fi

    # 目录确实变了，才付出 split 的代价
    echo "  → 目录有改动，切分子树（遍历历史，约 50s）..."
    local tmpsub="_sync_check_$$"
    $GIT branch -D "$tmpsub" >/dev/null 2>&1
    local subsha
    subsha=$($GIT subtree split --prefix="$prefix" -b "$tmpsub" 2>/dev/null | tail -1)
    $GIT branch -D "$tmpsub" >/dev/null 2>&1

    if [ -z "$subsha" ]; then
        echo "  ✗ 子树切分失败: $prefix"; return 1
    fi

    local sub_tree
    sub_tree=$($GIT rev-parse "$subsha^{tree}" 2>/dev/null)

    # 内容一致性检查：split 出来的内容 vs 远端
    if [ -n "$remote_tree" ] && [ "$sub_tree" != "$remote_tree" ]; then
        local diff
        diff=$($GIT diff --stat "$subsha" "$remote/main" 2>/dev/null)
        echo "  ⚠ 主仓库 $prefix/ 与 $remote/main 内容不一致："
        echo "$diff" | head -8 | sed 's/^/      /'
        echo ""
        echo "  这通常意味着独立仓库被直接改过（本脚本约定：只改主仓库）。"
        echo "  若确认以主仓库为准，执行："
        echo "      git subtree push --prefix=$prefix $remote main --force"
        return 1
    fi

    if [ "$DRY_RUN" = "1" ]; then
        echo "    [dry-run] git subtree push --prefix=$prefix $remote main"
        echo "    （内容有变化，实际执行约需 60s）"
        return 0
    fi

    echo "  → 推送中（subtree 需遍历历史，约 60s）..."
    $GIT subtree push --prefix="$prefix" "$remote" main 2>&1 \
        | grep -vE '^[0-9]+/[0-9]+ \(' \
        | sed 's/^/    /'

    $GIT fetch "$remote" >/dev/null 2>&1
    local now_tree
    now_tree=$($GIT rev-parse "refs/remotes/$remote/main^{tree}" 2>/dev/null)
    if [ "$now_tree" = "$sub_tree" ]; then
        echo "  ✓ 推送成功（远端 tree = ${sub_tree:0:7}）"
        return 0
    fi
    echo "  ✗ 推送后远端 tree 是 ${now_tree:0:7}，期望 ${sub_tree:0:7}"
    return 1
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
# 汇总只显示主仓库 SHA + 两个 remote 的最新提交。
# ⚠️ 不要在这里再跑 subtree split —— 那要遍历全部历史，在真实仓库上
#    要几十秒且输出会卡住。推没推成功看 remote ref 就够了。
printf "  %-24s %s\n" "rk3588-edge-ai" "$($GIT rev-parse --short main 2>/dev/null || echo '?')"
for pair in "stereo3d:stereo3d-rk3588" "gimbal:esp32-gimbal-rk3588"; do
    remote="${pair%%:*}"; name="${pair##*:}"
    # 推完之后远端 ref 已经更新，直接读本地缓存的 remote-tracking ref
    sha=$($GIT rev-parse --short "refs/remotes/$remote/main" 2>/dev/null \
          || $GIT ls-remote --heads "$remote" main 2>/dev/null | cut -c1-7 \
          || echo "?")
    printf "  %-24s %s\n" "$name" "$sha"
done

