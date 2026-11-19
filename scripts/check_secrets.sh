#!/usr/bin/env bash
# check_secrets.sh —— 扫整个 git 跟踪内容里有没有凭据/内网 IP 泄漏
#
# 为什么需要这个脚本（2026-11-19 事故）：
#   2026-10-25 项目做过一次 `git filter-branch` 清理公开仓库里的板子 root 密码，
#   当时的终验是 CLEAN、并且记下了「已推送的 origin/main 经查本就不含，暴露面为零」。
#   但 10-26 之后的提交 0c9f6db 又把**未脱敏**的 docs/workspace/AGENTS.md
#   （含板子 IP 与 root 密码）推了上去，公开仓库可读 —— 直到 11-19 才发现。
#
#   教训不是「上次没做对」，而是：**一次性的 purge + 人工核对，只能证明那一刻干净。
#   此后任何一次提交都可能重新引入。必须有一个可重复执行、不靠人记性的机器检查。**
#
# 用法：
#   ./scripts/check_secrets.sh          # 扫 git 已跟踪的内容
#   ./scripts/check_secrets.sh --all    # 连 git 历史一起扫（慢，发布前跑一次）
#
# 退出码：0 = 干净；1 = 有命中（会把位置全列出来）；2 = 环境问题（不在 git 仓库里等）

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT" || { echo "✗ 无法进入仓库根目录"; exit 2; }

if ! git rev-parse --git-dir >/dev/null 2>&1; then
    echo "✗ 当前目录不是 git 仓库：$REPO_ROOT"
    exit 2
fi

SCAN_HISTORY=0
[ "${1:-}" = "--all" ] && SCAN_HISTORY=1

# ---------------------------------------------------------------------------
# 规则表
#   每条是 "正则|说明"。宁可误报也不漏报 —— 误报改一行白名单就行，
#   漏报要等下一次事故才发现。
# ---------------------------------------------------------------------------
RULES=(
    'ematech|板子 root 密码（已知明文）'
    '10\.21\.3\.[0-9]{1,3}|本项目内网 IP（具体网段）'
    '\b10\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\b|RFC1918 私有网段 10/8'
    '\b192\.168\.[0-9]{1,3}\.[0-9]{1,3}\b|RFC1918 私有网段 192.168/16'
    '\b172\.(1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3}\b|RFC1918 私有网段 172.16/12'
    # 只匹配「引号/反引号里的字面量」—— `password: str` 这种类型标注不是机密。
    # 同时排除 <...> 尖括号占位符（<board-ip> / <板子密码> 是**正确**写法，不是泄漏）。
    'password\s*[=:]\s*["'"'"'`][^"'"'"'`<>]{2,}|引号内的明文 password'
    '(密码|口令)\s*[=:：]\s*["'"'"'`][^"'"'"'`<>]{2,}|引号内的明文密码（中文）'
    # 环境变量赋了「看起来像真值」的裸字面量；<...> 占位符豁免
    'PASSWORD[[:space:]]*=[[:space:]]*[^ $#<>"'"'"'`(\[]{2,}|PASSWORD 环境变量赋了字面值'
)

# ---------------------------------------------------------------------------
# 白名单：允许出现的文件（历史归档、本脚本自身、说明性文档里的示例）
#   用路径前缀匹配。加白名单必须写清理由。
# ---------------------------------------------------------------------------
WHITELIST=(
    'scripts/check_secrets.sh'   # 本脚本自己就写着这些正则和密码明文，不豁免必自我误报
    'docs/archive/'              # 早期设计稿归档，保留当时上下文，不对外宣传
)

is_whitelisted() {
    local f="$1"
    local w
    for w in "${WHITELIST[@]}"; do
        case "$f" in
            "$w"*) return 0 ;;
        esac
    done
    return 1
}

TOTAL=0
FAILED=0

echo "=================================================================="
echo " check_secrets.sh —— 凭据 / 内网 IP 泄漏扫描"
echo " 仓库: $REPO_ROOT"
echo " 范围: $( [ "$SCAN_HISTORY" = "1" ] && echo 'git 全部历史' || echo 'git 已跟踪内容' )"
echo "=================================================================="

for entry in "${RULES[@]}"; do
    pattern="${entry%%|*}"
    desc="${entry#*|}"

    if [ "$SCAN_HISTORY" = "1" ]; then
        # 历史模式：逐个 rev 扫，能定位到「哪个提交引入的」
        raw="$(git grep -n -E -I "$pattern" $(git rev-list --all) -- . 2>/dev/null || true)"
    else
        raw="$(git grep -n -E -I "$pattern" -- . 2>/dev/null || true)"
    fi

    # 过滤白名单
    filtered=""
    if [ -n "$raw" ]; then
        while IFS= read -r line; do
            [ -z "$line" ] && continue
            # git grep 输出形如 path:lineno:content 或 <rev>:path:lineno:content
            filepart="${line%%:*}"
            if [ "$SCAN_HISTORY" = "1" ]; then
                filepart="$(echo "$line" | sed -E 's/^[0-9a-f]{7,40}:(.*)/\1/' | cut -d: -f1)"
            fi
            is_whitelisted "$filepart" && continue
            filtered="$filtered$line"$'\n'
        done <<< "$raw"
    fi

    if [ -n "${filtered//[$'\n' ]/}" ]; then
        n="$(printf '%s' "$filtered" | grep -c . || true)"
        echo ""
        echo "✗ 命中 [$desc] 共 $n 处："
        printf '%s' "$filtered" | sed 's/^/    /' | cut -c1-200
        FAILED=$((FAILED + n))
    fi
    TOTAL=$((TOTAL + 1))
done

echo ""
echo "------------------------------------------------------------------"
if [ "$FAILED" -gt 0 ]; then
    echo "✗ 共 $FAILED 处命中，请改为占位符后重试："
    echo "    板子 IP   -> <board-ip>"
    echo "    板子密码  -> 见本地私密记录（不要写进仓库）"
    echo "  参考 docs/workspace/AGENTS.md 第 2 节的写法。"
    exit 1
fi

echo "✓ 干净：$TOTAL 条规则，0 处命中。"
[ "$SCAN_HISTORY" = "1" ] || echo "  （要连历史一起查：$0 --all）"
exit 0
