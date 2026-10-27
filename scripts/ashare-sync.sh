#!/usr/bin/env bash
# ===== A股语料定时入库（生产服务器，全程 sudo）=====
#
# 用法：
#   sudo bash scripts/ashare-sync.sh daily            # 收盘后跑一次（cron 调的就是它）
#   sudo bash scripts/ashare-sync.sh data             # 财务指标/估值落业务表（cron 15:40）
#   sudo bash scripts/ashare-sync.sh install-cron     # 装定时任务（语料 15:10、业务表 15:40）
#   sudo bash scripts/ashare-sync.sh universe         # 只刷新成交额 Top N 股票池
#   sudo bash scripts/ashare-sync.sh rescale          # 换范围：刷池子 → 清池外语料 → 清旧断点（顺序固定）
#   sudo bash scripts/ashare-sync.sh pool-prune       # 每周六清池外语料（带宽限，cron 调）
#   sudo bash scripts/ashare-sync.sh backfill --source cninfo --universe --shard 1/3
#
# 为什么是「一次性容器 + cron」而不是常驻 daemon：
# 采集只在收盘后有意义，常驻 daemon 会按 5~30 分钟的频率空转、反复打源站，
# 在 3GB 的机器上还白占内存；每天跑一个 --rm 容器的成本可以忽略。
#
# 回填用 --shard 拆成多份并行（每个容器一片代码），互不重叠：
#   for i in 1 2 3; do sudo bash scripts/ashare-sync.sh backfill --shard $i/3 --universe & done
#
# 五年回填默认只跑定期报告（年报/半年报/季报）：全量公告五年有 200 多万条，
# 光列表就要翻几万页，且单份 PDF 动辄上百页，磁盘和 embedding 费用都撑不住。

set -euo pipefail

DEPLOY_DIR="/opt/stock-agent"
# 绝对路径：cron 的 CWD 是 /，相对路径会找不到 compose 文件
COMPOSE_FILE="/opt/stock-agent/deploy/compose/prod.yml"
ENV_FILE="/opt/stock-agent/deploy/.env"
LOCK_FILE="/var/lock/stock-agent-ashare.lock"
CRON_FILE="/etc/cron.d/stock-agent-ashare"

log()  { echo -e "\033[32m[ashare]\033[0m $*"; }
warn() { echo -e "\033[33m[ashare]\033[0m $*"; }
die()  { echo -e "\033[31m[ashare] 错误: $*\033[0m" >&2; exit 1; }

require_root() {
    [ "$(id -u)" -eq 0 ] || die "请用 sudo 运行：sudo bash scripts/ashare-sync.sh $*"
}

# 与 deploy.sh 保持一致：自带 PG 才需要 localpg profile；.env 必须显式给
uses_external_pg() {
    [ -r "$ENV_FILE" ] || return 1
    local dsn
    dsn=$(grep -E '^DATABASE_DSN=' "$ENV_FILE" | tail -1 | cut -d= -f2- | tr -d '\r' || true)
    [ -n "$dsn" ] || return 1
    case "$dsn" in
        *@postgres:*|*@localhost:*|*@127.0.0.1:*) return 1 ;;
    esac
    return 0
}

compose() {
    local args=(--profile ashare)
    uses_external_pg || args+=(--profile localpg)
    docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "${args[@]}" "$@"
}

# 采集容器与 server/agent 抢 CPU 时接口会变慢，同一时刻只允许一个同类任务；
# 回填会拆成多片并行，锁名按片区分，不能互相挡住
take_lock() {
    local name="${1:-global}"
    LOCK_FILE="/var/lock/stock-agent-ashare-${name}.lock"
    exec 9>"$LOCK_FILE"
    flock -n 9 || die "已有同类任务在跑（$LOCK_FILE），本次跳过"
}

ashare_cli() {
    # -T：cron 里没有 TTY，少了这个参数 compose 会直接报 "input device is not a TTY"
    compose run --rm --no-deps -T ashare python -m ingest.ashare.cli "$@"
}

cmd_universe() {
    ashare_cli sync-universe --top "${ASHARE_UNIVERSE_TOP_N:-300}"
}

# 切换采集范围（Top N）。顺序写死，避免手工拼命令时把 prune 跑在刷新池子之前：
# prune --outside-universe 的判定集就是 ashare_universe 的当前内容，池子没刷之前跑等于空转（matched=0）。
cmd_rescale() {
    take_lock rescale
    warn "切换采集范围到 Top${ASHARE_UNIVERSE_TOP_N:-300} ..."
    cmd_universe
    log "池外个股语料统计（不删）..."
    ashare_cli prune --outside-universe
    log "执行删除..."
    ashare_cli prune --outside-universe --yes
    log "清理旧口径的回填断点（scope=universe）..."
    ashare_cli forget-backfill --scope universe
    log "完成：回填请用新 scope（universe${ASHARE_UNIVERSE_TOP_N:-300}#i-n）重起"
}

# 稳态维护：每周清一次池外语料。带宽限，rank 边界股反复进出不会被连根删掉
# （删掉等于把它的历史语料清零，再回池子也只能按水位重采近期）。
cmd_pool_prune() {
    take_lock pool-prune
    log "清理池外语料（宽限 ${ASHARE_POOL_PRUNE_GRACE_DAYS:-14} 天）..."
    ashare_cli prune --outside-universe --yes \
        --grace-days "${ASHARE_POOL_PRUNE_GRACE_DAYS:-14}"
    log "完成"
}

cmd_daily() {
    take_lock daily
    log "刷新热门股票池..."
    # 池子刷新失败不该拖垮当天的采集：昨天的池子今天照样能用，差一天而已
    if ! cmd_universe; then
        warn "热门池刷新失败，沿用库中现有池子继续采集"
    fi
    log "增量采集（按热门池）..."
    # 各源按水位取重叠窗口：收盘后跑一次即可覆盖前一交易日收盘以来的公告/研报
    ashare_cli collect --universe --source "${ASHARE_DAILY_SOURCES:-cninfo,em-report,cls,ths,em-global}"

    # 保留窗口：库只留最近 N 天，避免每天增量把 40G 磁盘慢慢吃满
    local retention="${ASHARE_RETENTION_DAYS:-365}"
    if [ "$retention" -gt 0 ]; then
        log "清理 ${retention} 天之前的语料..."
        ashare_cli prune --older-than "$retention" --yes
    fi
    log "完成"
}

# 数据类（数值）落业务表：财务指标 + 每日估值。
# 与向量语料同一池子（--codes 留空=当前热门池），这样「问数字」和「问文字」覆盖的是同一批股票。
# 财务指标按报告期取（一次调用覆盖全市场一期），估值逐股但只写最近几天。
cmd_data() {
    take_lock data
    log "同步报告期财务指标（最近 ${ASHARE_FINANCE_YEARS:-5} 年）..."
    ashare_cli sync-finance --years "${ASHARE_FINANCE_YEARS:-5}"
    log "同步每日估值（每股 ${ASHARE_VALUATION_DAYS:-5} 个交易日）..."
    ashare_cli sync-valuation --days "${ASHARE_VALUATION_DAYS:-5}"
    log "完成"
}

cmd_backfill() {
    # 分片参数在透传参数里，用片号区分锁：多片并行时不能互相挡住
    local shard=""
    local prev=""
    for arg in "$@"; do
        [ "$prev" = "--shard" ] && shard="$arg"
        prev="$arg"
    done
    take_lock "backfill-${shard//\//-}"
    # 回填必须开分类过滤，否则巨潮列表要翻全市场每天上万条公告
    local categories="${ASHARE_CNINFO_CATEGORIES:-}"
    if [ -z "$categories" ]; then
        categories="category_ndbg_szsh;category_bndbg_szsh;category_yjdbg_szsh;category_sjdbg_szsh;"
    fi
    log "回填参数：$*（公告分类：$categories）"
    compose run --rm --no-deps -T \
        -e "ASHARE_CNINFO_CATEGORIES=$categories" \
        ashare python -m ingest.ashare.cli backfill "$@"
}

cmd_install_cron() {
    require_root install-cron
    local hour=${ASHARE_CRON_HOUR:-15}
    local minute=${ASHARE_CRON_MINUTE:-10}
    # 业务表同步错开半小时：两个任务都在打外部源（巨潮/东财），同时起会互相拖慢
    local data_hour=$hour
    local data_min=$((minute + 30))
    if [ "$data_min" -ge 60 ]; then
        data_min=$((data_min - 60))
        data_hour=$((data_hour + 1))
    fi
    # 池外清理放周六：当天没有行情，宽限期判定不受池子刷新影响
    local prune_hour=${ASHARE_POOL_PRUNE_HOUR:-9}
    local prune_min=${ASHARE_POOL_PRUNE_MINUTE:-20}
    # cron 里不能再依赖调用者的 CWD：用脚本自身的绝对路径
    local self
    self=$(readlink -f "$0")
    cat > "$CRON_FILE" <<EOF
# 收盘后刷新热门池并增量入库（周一至周五；法定节假日由源侧无新数据自然空跑）
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
$minute $hour * * 1-5 root bash $self daily >> /var/log/stock-agent-ashare.log 2>&1
# 数据类落业务表（财务指标 + 每日估值），数值问答从这里取数
$data_min $data_hour * * 1-5 root bash $self data >> /var/log/stock-agent-ashare.log 2>&1
# 每周清一次池外语料（带宽限，池子每天整体替换，不做这步「范围=Top N」会慢慢失真）
$prune_min $prune_hour * * 6 root bash $self pool-prune >> /var/log/stock-agent-ashare.log 2>&1
EOF
    chmod 644 "$CRON_FILE"
    log "已安装：$CRON_FILE（语料 ${hour}:${minute}、业务表 ${data_hour}:${data_min} 周一至周五；池外清理 ${prune_hour}:${prune_min} 周六）→ $self"
    log "日志：/var/log/stock-agent-ashare.log"
}

main() {
    local cmd="${1:-}"
    [ $# -gt 0 ] && shift || true
    case "$cmd" in
        daily)        require_root daily; cmd_daily ;;
        data)         require_root data; cmd_data ;;
        universe)     require_root universe; take_lock universe; cmd_universe ;;
        rescale)      require_root rescale; cmd_rescale ;;
        pool-prune)   require_root pool-prune; cmd_pool_prune ;;
        backfill)     require_root backfill; cmd_backfill "$@" ;;
        install-cron) cmd_install_cron ;;
        *)            die "用法：$0 {daily|data|universe|rescale|pool-prune|backfill|install-cron} [参数...]" ;;
    esac
}

main "$@"
