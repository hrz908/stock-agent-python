#!/usr/bin/env bash
# ===== 一条命令发布：本地同步 origin → 推送 → 服务器 ff-only 更新 → 校验 =====
#
# 用法：
#   git release                           # 完整发布（推荐；仓库本地 alias，任意子目录都行）
#   git release --via-ssh                 # 本地连不上 GitHub 时用：只更新服务器，不推 origin
#   git release --dry-run                 # 预演：fetch + 打印待推送提交与服务器当前提交，不推送不动服务器
#   git release --force                   # 服务器已是同一提交也重建（只改了 .env 才需要）
#   git release --skip-health             # 跳过发布后的 /healthz 探活
#
# 换机器没有 alias 时：git config alias.release '!bash scripts/release.sh'
# 别用 PowerShell 里的 `bash`（本机指向 WSL，会报 /bin/bash 不存在），要么用 git release，要么进 Git Bash。
#
# 服务器地址取自本地 remote.prod（ssh://user@host/path），也可用 SSH_TARGET / DEPLOY_DIR 覆盖。
# 前置：本地工作区干净（脏树不代提交）；服务器 update 需要 root，有密码时 ssh -t 会让你输一次。
#
# 与 git pushsafe 的分工：pushsafe 只到「推上 GitHub」；本脚本后续还会跑服务器 update，
# 并在结束后校验服务器 HEAD == 本地 HEAD，避免「推了但没部署」这种静默失败。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BRANCH="${BRANCH:-main}"
ORIGIN="${ORIGIN:-origin}"
SSH_TARGET="${SSH_TARGET:-}"
DEPLOY_DIR="${DEPLOY_DIR:-}"
DRY_RUN=0
FORCE=0
SKIP_HEALTH=0
VIA_SSH=0


log()  { echo -e "\033[32m[release]\033[0m $*"; }
warn() { echo -e "\033[33m[release]\033[0m $*" >&2; }
die()  { echo -e "\033[31m[release] 错误: $*\033[0m" >&2; exit 1; }

usage() {
    cat <<'EOF'
一条命令发布：本地同步 origin → 推送 → 服务器 ff-only 更新 → 校验提交与探活

用法（任意子目录，git 会切到仓库根执行）：
  git release                            完整发布
  git release --via-ssh                  本地连不上 GitHub 时用：只更新服务器，不推 origin
  git release --dry-run                  预演：fetch + 打印待推送提交与服务器当前提交，不推送不动服务器
  git release --force                    服务器已是同一提交也重建（只改了 .env 才需要）
  git release --skip-health              跳过发布后的 /healthz 探活

换机器没有 alias：git config alias.release '!bash scripts/release.sh'
PowerShell 里的 bash 指向 WSL，会报 /bin/bash 不存在；要么用 git release，要么进 Git Bash。

服务器地址取自 remote.prod，可用环境变量覆盖：SSH_TARGET / DEPLOY_DIR / BRANCH / ORIGIN
EOF
}

# 服务器与部署目录默认取自 remote.prod：仓库里只有这一处写过生产地址，
# 再抄一份常量就会在两处漂移（换机器/换域名时漏改一边，发布打到旧机器）。
resolve_target() {
    local url rest
    url=$(git -C "$ROOT" config --get remote.prod.url || true)
    if [ -n "$url" ]; then
        case "$url" in
            ssh://*)
                rest="${url#ssh://}"
                SSH_TARGET="${SSH_TARGET:-${rest%%/*}}"
                DEPLOY_DIR="${DEPLOY_DIR:-/${rest#*/}}"
                ;;
            *:*)
                SSH_TARGET="${SSH_TARGET:-${url%%:*}}"
                DEPLOY_DIR="${DEPLOY_DIR:-${url#*:}}"
                ;;
        esac
    fi
    [ -n "$SSH_TARGET" ] || die "未找到 remote.prod，先 git remote add prod ssh://user@host/opt/stock-agent，或设 SSH_TARGET/DEPLOY_DIR"
    [ -n "$DEPLOY_DIR" ] || die "无法从 remote.prod 解析部署目录，请设 DEPLOY_DIR"
    command -v ssh >/dev/null 2>&1 || die "未找到 ssh 客户端"
}

# 一次 SSH 拿两样东西：服务器 HEAD 与工作区状态。拆成两条 ssh 就多一次完整往返
# （跨国链路 0.3~1.5s，且每次都要重新做 TCP+认证）。
# 输出约定：第一行 SHA，其余行是 git status --porcelain 的内容。
preflight_remote() {
    local out
    out=$(ssh "$SSH_TARGET" "cd '$DEPLOY_DIR' && git rev-parse HEAD && git status --porcelain" 2>/dev/null) \
        || die "连不上 $SSH_TARGET 或 $DEPLOY_DIR 不是 git 仓库"
    REMOTE_SHA=$(printf '%s\n' "$out" | head -1 | tr -d '\r')
    local dirty
    dirty=$(printf '%s\n' "$out" | tail -n +2 | tr -d '\r')
    if [ -n "$dirty" ]; then
        printf '%s\n' "$dirty" >&2
        die "服务器 $DEPLOY_DIR 有未提交改动（见上），update 会拒绝执行：先在服务器上导出 diff 并回滚"
    fi
}

# 发布末尾的「校验提交 + 探活」也合并成一次 SSH：
#   remote_final_check health   先打印 SHA，再轮询 /healthz（30s 上限）
#   remote_final_check nohealth 只打印 SHA（--skip-health）
# 退出码：health 模式下 /healthz 未通过返回 1；SHA 始终通过 REMOTE_FINAL_SHA 带回。
remote_final_check() {
    local mode="${1:-health}" out rc=0
    local script="cd '$DEPLOY_DIR' && echo SHA=\$(git rev-parse HEAD)"
    if [ "$mode" = "health" ]; then
        # 单进程重启要等 agent 预热（实测 2026-10-27：约 2 分钟），只等 30s 会把
        # 「部署成功」误判成失败。这里放到 4 分钟，并与 deploy.sh 的等待窗口一致。
        script="$script && for i in \$(seq 1 80); do curl -kfsS https://127.0.0.1/healthz >/dev/null 2>&1 && exit 0; sleep 3; done; exit 1"
    fi
    out=$(ssh "$SSH_TARGET" "$script" 2>/dev/null) || rc=$?
    REMOTE_FINAL_SHA=$(printf '%s\n' "$out" | sed -n 's/^SHA=//p' | tr -d '\r')
    return "$rc"
}

# 脏树不代提交：自动 commit 会把半成品（调试开关、临时日志）一起发到生产，
# 而没有提交的改动又不可能通过 push 到达服务器。
precheck_local() {
    cd "$ROOT"
    local cur
    cur=$(git rev-parse --abbrev-ref HEAD)
    [ "$cur" = "$BRANCH" ] || die "当前分支 $cur，发布只允许 $BRANCH"
    if ! git diff --quiet || ! git diff --cached --quiet; then
        die "工作区有未提交改动，先 commit 再发布"
    fi
    if git rev-parse -q --verify REBASE_HEAD >/dev/null 2>&1 \
        || [ -f "$(git rev-parse --git-path MERGE_HEAD)" ]; then
        die "rebase/merge 尚未结束，先处理完再发布"
    fi
}

sync_origin() {
    log "同步 $ORIGIN/$BRANCH"
    git fetch --quiet "$ORIGIN"
    if git merge-base --is-ancestor "$ORIGIN/$BRANCH" HEAD; then
        return 0
    fi
    log "本地落后或已分叉，rebase 到 $ORIGIN/$BRANCH"
    if ! git rebase "$ORIGIN/$BRANCH"; then
        die "rebase 冲突：解决后 git rebase --continue，再重跑本脚本"
    fi
}

# 取代码通道：本地直接把提交推进服务器仓库（不是让服务器去 fetch GitHub）。
# 服务器出口网络到 github.com 会间歇性超时（实测 129s 后 Connection timed out），
# 而本地 → 服务器的 ssh 一直可用；服务器仓库已设 receive.denyCurrentBranch=updateInstead，
# push 会顺带更新工作区，deploy.sh 再按 DEPLOY_TARGET_SHA 快进即可。
push_to_server() {
    log "推送代码到服务器仓库 $SSH_TARGET:$DEPLOY_DIR"
    git push "ssh://$SSH_TARGET$DEPLOY_DIR" "HEAD:$BRANCH" \
        || die "直推服务器仓库失败：确认 $SSH_TARGET 可写、且服务器工作区干净（git status 为空）"
}

# sudo 需要密码，必须分配 TTY；没有 TTY 时让它直接失败，而不是静默跳过部署。
# DEPLOY_TARGET_SHA 用 bash -c 传：sudo 默认不允许在命令行设环境变量（需要 SETENV 权限）。
run_remote_update() {
    local sha
    sha=$(git rev-parse HEAD)
    ssh -t "$SSH_TARGET" "cd '$DEPLOY_DIR' && sudo bash -c 'DEPLOY_TARGET_SHA=$sha bash scripts/deploy.sh update'" || {
        echo "  看服务器日志：ssh $SSH_TARGET 'cd $DEPLOY_DIR && sudo bash scripts/deploy.sh logs server'" >&2
        die "服务器更新失败"
    }
}

main() {
    resolve_target
    precheck_local
    sync_origin

    local local_sha local_short remote_short
    local_sha=$(git rev-parse HEAD)
    local_short=$(git rev-parse --short HEAD)

    preflight_remote
    remote_short="${REMOTE_SHA:0:7}"

    if [ "$DRY_RUN" = "1" ]; then
        log "待推送提交（$ORIGIN/$BRANCH..HEAD）："
        git --no-pager log --oneline "$ORIGIN/$BRANCH..HEAD" | sed 's/^/    /'
        log "服务器 $SSH_TARGET:$DEPLOY_DIR 当前 $remote_short，本地 $local_short"
        log "预演结束：未推送、未改动服务器"
        return 0
    fi

    if [ "$VIA_SSH" = "1" ]; then
        # GitHub 只是代码托管，本地连不上它时不该卡住上线：跳过推送，只更新服务器
        log "跳过 $ORIGIN 推送（--via-ssh：本次只更新服务器）"
    else
        log "推送 $ORIGIN $BRANCH"
        git push "$ORIGIN" "HEAD:$BRANCH"
    fi

    if [ "$REMOTE_SHA" = "$local_sha" ] && [ "$FORCE" = "0" ]; then
        log "服务器已是 $local_short，跳过 update（改了 .env 或想强制重建用 --force）"
    else
        push_to_server
        log "服务器更新：$remote_short → $local_short"
        run_remote_update
    fi

    local deployed
    if [ "$SKIP_HEALTH" = "0" ]; then
        log "校验提交并探活 /healthz"
        remote_final_check health || {
            echo "  看后端日志：ssh $SSH_TARGET 'cd $DEPLOY_DIR && sudo bash scripts/deploy.sh logs server'" >&2
            die "4 分钟内 /healthz 未通过"
        }
    else
        remote_final_check nohealth || die "取服务器 HEAD 失败，无法确认部署结果"
    fi
    deployed="${REMOTE_FINAL_SHA:-}"
    if [ "$deployed" != "$local_sha" ]; then
        die "服务器 HEAD 是 ${deployed:0:7}，本地是 $local_short：代码已推送但没部署到目标提交"
    fi

    log "发布完成：$remote_short → $local_short"
    # 服务器原提交不一定在本地（别人推的），取不到就别硬列
    if git cat-file -e "${REMOTE_SHA}^{commit}" 2>/dev/null; then
        git --no-pager log --oneline "$REMOTE_SHA..$local_sha" | sed 's/^/    /'
    fi
}

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)     DRY_RUN=1 ;;
        --via-ssh)     VIA_SSH=1 ;;
        --force|-f)    FORCE=1 ;;
        --skip-health) SKIP_HEALTH=1 ;;
        -h|--help)     usage; exit 0 ;;
        *)             die "未知参数：$1（bash scripts/release.sh --help）" ;;
    esac
    shift
done

main
