#!/usr/bin/env bash
# ===== 新机器一条命令恢复「不入库的本地状态」=====
#
# 用法（仓库内任意目录）：
#   bash scripts/install-hooks.sh           # 装 pre-push 钩子 + remote.prod + release/pushsafe 别名
#   bash scripts/install-hooks.sh --force   # 钩子内容/地址/别名不一致时覆盖
#   bash scripts/install-hooks.sh --check   # 只体检不写文件；有缺失时退出码 1
#
# 这些全在 .git/ 与 git 配置里，不入库，clone 完一定是空的。
# 缺钩子的后果：远端已有别人提交时 push 照样「成功」并推成非快进，服务器 update 随即分叉卡死。

set -euo pipefail

PROD_URL="${PROD_URL:-ssh://ubuntu@tearsinrain.top/opt/stock-agent}"
RELEASE_ALIAS='!bash scripts/release.sh'
PUSHSAFE_ALIAS='!git fetch origin && git diff --quiet && git diff --cached --quiet && git rebase origin/main && git push'
FORCE=0
CHECK=0
FAIL=0

log()  { echo -e "\033[32m[setup]\033[0m $*"; }
warn() { echo -e "\033[33m[setup]\033[0m $*"; }
die()  { echo -e "\033[31m[setup] 错误: $*\033[0m" >&2; exit 1; }

usage() {
    cat <<'EOF'
新机器一次性配置：pre-push 钩子 + remote.prod + release/pushsafe 别名

用法：
  bash scripts/install-hooks.sh           安装（已存在且不同的项保留不动）
  bash scripts/install-hooks.sh --force   覆盖钩子内容、remote 地址、同名的旧别名
  bash scripts/install-hooks.sh --check   只体检，有缺失时退出码 1

可用环境变量覆盖：PROD_URL（默认 ssh://ubuntu@tearsinrain.top/opt/stock-agent）
EOF
}

ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || die "当前不在 git 仓库内"
cd "$ROOT"
# worktree 里 .git 是文件不是目录，必须走 --git-path 解析
HOOK=$(git rev-parse --git-path hooks/pre-push)

hook_body() {
    cat <<'HOOK'
#!/bin/sh
# push 前强制同步：远端领先就中止，避免把别人的提交盖掉
# 装在 .git/hooks（不随仓库分发），本地与服务器各一份
remote="${1:-origin}"

if ! git fetch --quiet "$remote" 2>/dev/null; then
    echo "pre-push: git fetch $remote 失败，已中止推送（先确认网络/代理）" >&2
    exit 1
fi

# 没有 upstream（新分支首次推送）时不做拦截
upstream=$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null) || exit 0

behind=$(git rev-list --count "HEAD..$upstream" 2>/dev/null || echo 0)
if [ "${behind:-0}" -gt 0 ]; then
    echo "pre-push: $upstream 领先本地 $behind 个提交，先同步再推" >&2
    echo "          git pushsafe   # fetch + rebase origin/main + push" >&2
    exit 1
fi

exit 0
HOOK
}

hook_ok() {
    [ -f "$HOOK" ] || return 1
    hook_body | cmp -s - "$HOOK"
}

setup_hook() {
    if hook_ok; then
        log "pre-push 钩子：正常"
        return 0
    fi
    if [ "$CHECK" = "1" ]; then
        warn "pre-push 钩子：缺失或内容不同（$HOOK）"
        FAIL=1
        return 0
    fi
    # 已有旧钩子时不动：可能是手工调的，覆盖属于破坏性操作
    if [ -f "$HOOK" ] && [ "$FORCE" = "0" ]; then
        warn "pre-push 钩子内容与当前版本不同，保留现有（--force 覆盖）"
        return 0
    fi
    mkdir -p "$(dirname "$HOOK")"
    hook_body > "$HOOK"
    chmod +x "$HOOK" 2>/dev/null || true
    log "pre-push 钩子：已写入 $HOOK"
}

setup_remote() {
    local cur
    cur=$(git config --get remote.prod.url || true)
    if [ -z "$cur" ]; then
        if [ "$CHECK" = "1" ]; then
            warn "remote.prod：缺失（期望 $PROD_URL）"
            FAIL=1
            return 0
        fi
        git remote add prod "$PROD_URL"
        git config remote.prod.pushurl DISABLED
        log "remote.prod：已添加 $PROD_URL（push 已禁用）"
        return 0
    fi
    if [ "$cur" = "$PROD_URL" ]; then
        log "remote.prod：$cur"
        return 0
    fi
    if [ "$CHECK" = "1" ]; then
        warn "remote.prod：指向 $cur，期望 $PROD_URL"
        FAIL=1
        return 0
    fi
    if [ "$FORCE" = "1" ]; then
        git remote set-url prod "$PROD_URL"
        git config remote.prod.pushurl DISABLED
        log "remote.prod：已改为 $PROD_URL"
        return 0
    fi
    warn "remote.prod 已指向 $cur，保留（服务器换地址加 --force）"
}

# scope: local（仓库）| global（用户）
setup_alias() {
    local scope="$1" name="$2" want="$3" cur
    if [ "$scope" = "global" ]; then
        cur=$(git config --global --get "alias.$name" || true)
    else
        cur=$(git config --get "alias.$name" || true)
    fi
    if [ "$cur" = "$want" ]; then
        log "alias.$name（$scope）：正常"
        return 0
    fi
    if [ "$CHECK" = "1" ]; then
        warn "alias.$name（$scope）：缺失或与约定不同"
        if [ "$scope" = "local" ]; then
            FAIL=1
        fi
        return 0
    fi
    if [ -n "$cur" ] && [ "$FORCE" = "0" ]; then
        warn "alias.$name（$scope）已存在且不同，保留现有（--force 覆盖）"
        return 0
    fi
    git config --"$scope" "alias.$name" "$want"
    log "alias.$name（$scope）：已设置"
}

check_global_env() {
    if [ -z "$(git config --global --get alias.pushsafe || true)" ]; then
        warn "全局没有 pushsafe 别名（本脚本默认会装，--force 可覆盖同名旧值）"
    fi
    if [ -z "$(git config --global --get http.proxy || true)" ]; then
        warn "全局没有 http.proxy：国内直连 GitHub 常失败，确认代理端口（本机是 127.0.0.1:7897）"
    fi
}

while [ $# -gt 0 ]; do
    case "$1" in
        --force|-f) FORCE=1 ;;
        --check)    CHECK=1 ;;
        -h|--help)  usage; exit 0 ;;
        *)          die "未知参数：$1（bash scripts/install-hooks.sh --help）" ;;
    esac
    shift
done

setup_hook
setup_remote
setup_alias local release "$RELEASE_ALIAS"
setup_alias global pushsafe "$PUSHSAFE_ALIAS"
check_global_env

if [ "$CHECK" = "1" ]; then
    if [ "$FAIL" != "0" ]; then
        die "有缺失项，跑 bash scripts/install-hooks.sh 修复"
    fi
    log "体检通过"
    exit 0
fi

log "完成。自检：git fetch prod && git release --dry-run"
