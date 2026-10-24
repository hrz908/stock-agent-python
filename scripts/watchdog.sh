#!/usr/bin/env bash
# ===== StockA 探活告警（生产服务器）=====
#
# 用法：
#   sudo bash scripts/watchdog.sh check          # 检查一次（cron 调的就是它）
#   sudo bash scripts/watchdog.sh install-cron   # 每 5 分钟检查一次
#   sudo bash scripts/watchdog.sh test           # 直接推一条测试消息，验证通道
#   sudo bash scripts/watchdog.sh status         # 看当前状态与最近一次告警
#
# 检查两件事：
#   1. /healthz 是否通：容器僵死时 restart: unless-stopped 救不了，只能靠外部探活
#   2. 根分区剩余空间
#
# 告警走企微群机器人（deploy/.env 的 WECOM_WEBHOOK，不需要 access_token 与可信 IP）。
# 只在状态翻转时推、持续故障每 30 分钟重推一次：
# 每 5 分钟一条会刷爆群，然后没人再看这个群。

set -euo pipefail

ENV_FILE="/opt/stock-agent/deploy/.env"
STATE_DIR="/var/lib/stock-agent"
STATE_FILE="$STATE_DIR/watchdog.state"
REPEAT_SECONDS=1800
DISK_MIN_MB=2048
# 本机回环探测走自签证书，-k 只影响这次探测，不改对外 TLS 配置
HEALTH_URL="https://127.0.0.1/healthz"

log()  { echo -e "\033[32m[watchdog]\033[0m $*"; }
warn() { echo -e "\033[33m[watchdog]\033[0m $*"; }
die()  { echo -e "\033[31m[watchdog] 错误: $*\033[0m" >&2; exit 1; }

require_root() {
    [ "$(id -u)" -eq 0 ] || die "请用 sudo 运行：sudo bash scripts/watchdog.sh $*"
}

webhook() {
    [ -f "$ENV_FILE" ] || return 0
    grep -E '^WECOM_WEBHOOK=' "$ENV_FILE" | tail -1 | cut -d= -f2- || true
}

notify() {
    local msg="$1" hook esc
    hook=$(webhook)
    if [ -z "$hook" ]; then
        warn "WECOM_WEBHOOK 未配置，只写日志不推送：$msg"
        return 0
    fi
    # 手工拼 JSON：企微文本消息上限 2048 字节，转义错一个引号整条就被丢弃
    esc=$(printf '%s' "$msg" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' | tr '\n' ' ')
    curl -fsS -m 10 -H 'Content-Type: application/json' \
        -d "{\"msgtype\":\"text\",\"text\":{\"content\":\"$esc\"}}" "$hook" >/dev/null \
        || warn "推送失败（webhook 失效或网络不通）"
}

# state 文件一行一个检查项：name=state@epoch
get_state() {
    [ -f "$STATE_FILE" ] || return 0
    grep -E "^$1=" "$STATE_FILE" | tail -1 | cut -d= -f2- || true
}

set_state() {
    local tmp
    mkdir -p "$STATE_DIR"
    tmp="$STATE_FILE.tmp"
    { grep -vE "^$1=" "$STATE_FILE" 2>/dev/null || true; echo "$1=$2@$(date +%s)"; } > "$tmp"
    mv "$tmp" "$STATE_FILE"
}

alert() {
    local key="$1" msg="$2" prev prev_state prev_ts now
    prev=$(get_state "$key")
    prev_state="${prev%@*}"
    prev_ts="${prev#*@}"
    now=$(date +%s)
    # 首次故障、或持续故障超过 REPEAT_SECONDS 才推
    if [ "$prev_state" != "fail" ] || [ $((now - ${prev_ts:-0})) -ge "$REPEAT_SECONDS" ]; then
        notify "$msg"
        set_state "$key" fail
        log "已告警：$msg"
    else
        log "仍在故障中，跳过重复告警：$msg"
    fi
}

recover() {
    local key="$1" ok_msg="$2" prev
    prev=$(get_state "$key")
    # 只在「故障 -> 正常」时推一条恢复消息，一直在正常就不制造噪音
    if [ "${prev%@*}" = "fail" ]; then
        notify "$ok_msg"
    fi
    set_state "$key" ok
}

check_health() {
    local i
    for i in 1 2 3; do
        if curl -kfsS -m 5 "$HEALTH_URL" >/dev/null 2>&1; then
            recover health "【恢复】$(hostname) /healthz 已恢复"
            log "healthz 正常"
            return 0
        fi
        sleep 5
    done
    alert health "【故障】$(hostname) /healthz 连续 3 次不通（$(date '+%F %H:%M')）。排查：sudo bash scripts/deploy.sh ps；sudo bash scripts/deploy.sh logs server"
    return 1
}

check_disk() {
    local free_mb
    free_mb=$(df -Pm / | awk 'NR==2 {print $4}')
    if [ "$free_mb" -lt "$DISK_MIN_MB" ]; then
        alert disk "【故障】$(hostname) 根分区仅剩 ${free_mb}MB（阈值 ${DISK_MIN_MB}MB）。排查：du -sh /var/lib/docker/* | sort -h"
        return 1
    fi
    recover disk "【恢复】$(hostname) 磁盘余量回到 ${free_mb}MB"
    log "磁盘剩余 ${free_mb}MB"
}

cmd_check() {
    local rc=0
    check_health || rc=1
    check_disk || rc=1
    return "$rc"
}

cmd_install_cron() {
    require_root install-cron
    cat > /etc/cron.d/stock-agent-watchdog <<'EOF'
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
*/5 * * * * root bash /opt/stock-agent/scripts/watchdog.sh check >> /var/log/stock-agent-watchdog.log 2>&1
EOF
    chmod 644 /etc/cron.d/stock-agent-watchdog
    log "已安装 /etc/cron.d/stock-agent-watchdog（每 5 分钟）"
    warn "先验证告警通道：sudo bash scripts/watchdog.sh test"
}

cmd_test() {
    require_root test
    [ -n "$(webhook)" ] || die "deploy/.env 里 WECOM_WEBHOOK 为空，先在企微群「群机器人」添加一个"
    notify "【测试】$(hostname) stock-agent 告警通道正常（$(date '+%F %H:%M')）"
    log "已推送测试消息，去群里确认"
}

cmd_status() {
    echo "healthz: $(curl -kfsS -m 5 "$HEALTH_URL" 2>/dev/null || echo 不通)"
    echo "磁盘:    $(df -Ph / | awk 'NR==2 {print $4" 可用"}')"
    echo "webhook: $([ -n "$(webhook)" ] && echo 已配置 || echo 未配置)"
    echo "state:   $STATE_FILE"
    sed -n '1,20p' "$STATE_FILE" 2>/dev/null || echo "（无记录，还没触发过告警）"
}

case "${1:-}" in
    check)        shift; cmd_check "$@" ;;
    install-cron) shift; cmd_install_cron "$@" ;;
    test)         shift; cmd_test "$@" ;;
    status)       shift; cmd_status "$@" ;;
    *)
        echo "用法: sudo bash scripts/watchdog.sh {check|install-cron|test|status}"
        exit 1
        ;;
esac
