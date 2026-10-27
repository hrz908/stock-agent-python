#!/usr/bin/env bash
# ===== StockA 数据库备份（生产服务器，全程 sudo）=====
#
# 用法：
#   sudo bash scripts/backup.sh run                 # 备份一次（cron 调的就是它）
#   sudo bash scripts/backup.sh install-cron        # 装每日 03:30 的定时任务
#   sudo bash scripts/backup.sh list                # 看现有备份
#   sudo bash scripts/backup.sh verify <文件>       # 校验单个备份可读
#   sudo bash scripts/backup.sh restore <文件>      # 恢复到 stock_agent_restore（演练用，不动生产）
#   sudo bash scripts/backup.sh restore <文件> --into stock_agent   # 真恢复，需再输一次库名确认
#
# 备份落在 /var/backups/stock-agent（700 root），不在仓库目录内：
#   /opt/stock-agent 属主是 ubuntu，`git clean -xfd` 会连 .env 一起删，备份不能放那。
# 迁移只在数据卷为空时执行（deploy/compose/prod.yml 的 initdb 挂载），
# 也就是说改表/误删没有回滚路径——这份备份是唯一退路。
#
# 备份里含 pg 数据与 deploy/.env 副本：.env 曾在服务器上丢失过一次，
# 而同机 root 700 目录与 .env 本身 600 的暴露面一致，所以一并留一份。
# 需要异地容灾再加 rsync/对象存储，本脚本只管本机。

set -euo pipefail

DEPLOY_DIR="/opt/stock-agent"
PG_CONTAINER="stock-agent-postgres"
PG_USER="postgres"
PG_DB="stock_agent"
ENV_FILE="/opt/stock-agent/deploy/.env"
# 外部 PG（腾讯云）时没有常驻 pg 容器，用一次性客户端容器执行 pg_dump/pg_restore；
# 镜像主版本必须 >= 服务端主版本（云实例选 PG16 时保持默认即可）
PG_CLIENT_IMAGE="${PG_CLIENT_IMAGE:-postgres:16}"
BACKUP_DIR="/var/backups/stock-agent"
KEEP_DAYS=7
# 低于这个剩余空间就不备份：写满把 postgres 搞挂，比没有备份更糟
MIN_FREE_MB=2048

log()  { echo -e "\033[32m[backup]\033[0m $*"; }
warn() { echo -e "\033[33m[backup]\033[0m $*"; }
die()  { echo -e "\033[31m[backup] 错误: $*\033[0m" >&2; exit 1; }

require_root() {
    [ "$(id -u)" -eq 0 ] || die "请用 sudo 运行：sudo bash scripts/backup.sh $*"
}

require_container() {
    docker inspect -f '{{.State.Running}}' "$PG_CONTAINER" 2>/dev/null | grep -q true \
        || die "$PG_CONTAINER 没在运行。先 sudo bash scripts/deploy.sh ps"
}

# deploy/.env 是 root:600，备份脚本本来就全程 sudo，直接读出来判库位置
load_env() {
    [ -r "$ENV_FILE" ] || return 0
    set -a
    # shellcheck disable=SC1090
    . <(tr -d '\r' < "$ENV_FILE")
    set +a
}

# 迁移后 .env 的 DATABASE_DSN 指向云 PG；主机名仍是 compose 内的 postgres 则算自带库
pg_is_remote() {
    [ -n "${DATABASE_DSN:-}" ] || return 1
    case "$DATABASE_DSN" in
        *@postgres:*|*@localhost:*|*@127.0.0.1:*) return 1 ;;
    esac
    return 0
}

# 统一的客户端执行入口：本地走常驻容器，云上走一次性 postgres 客户端容器
pg_run() {
    if pg_is_remote; then
        docker run --rm -i "$PG_CLIENT_IMAGE" "$@"
    else
        docker exec -i "$PG_CONTAINER" "$@"
    fi
}

require_pg() {
    if pg_is_remote; then
        # 只打印主机部分，别把密码写进 cron 日志
        log "外部数据库：${DATABASE_DSN#*@}"
    else
        require_container
    fi
}

# 下面四个入口对本地容器与云实例各走一套命令，调用方不关心库在哪
list_dbs() {
    if pg_is_remote; then
        pg_run psql "$DATABASE_DSN" -lqt
    else
        pg_run psql -U "$PG_USER" -lqt
    fi
}

drop_db() {
    if pg_is_remote; then
        pg_run psql "$DATABASE_DSN" -c "DROP DATABASE \"$1\""
    else
        pg_run dropdb -U "$PG_USER" "$1"
    fi
}

create_db() {
    if pg_is_remote; then
        pg_run psql "$DATABASE_DSN" -c "CREATE DATABASE \"$1\""
    else
        pg_run createdb -U "$PG_USER" "$1"
    fi
}

# 恢复到指定库：云上把 DSN 的库名换成 target（DSN 固定指向 stock_agent）
restore_into() {
    if pg_is_remote; then
        local dsn="${DATABASE_DSN/\/$PG_DB/\/$1}"
        pg_run pg_restore --dbname="$dsn" --no-owner < "$2" 2> "$3" || true
    else
        pg_run pg_restore -U "$PG_USER" -d "$1" --no-owner < "$2" 2> "$3" || true
    fi
}

prepare_dir() {
    install -d -m 700 -o root -g root "$BACKUP_DIR" "$BACKUP_DIR/db" "$BACKUP_DIR/env"
}

# 空间检查放在 dump 之前：中途写满会留下半截文件，看起来像备份成功
check_space() {
    local free_mb
    free_mb=$(df -Pm "$BACKUP_DIR" | awk 'NR==2 {print $4}')
    [ "$free_mb" -ge "$MIN_FREE_MB" ] \
        || die "$BACKUP_DIR 所在分区只剩 ${free_mb}MB（阈值 ${MIN_FREE_MB}MB），先清理再备份"
}

# 用容器内的 pg_dump：宿主机不一定装了匹配版本的客户端，版本不一致会报错
# local socket 走 trust，不需要把 POSTGRES_PASSWORD 传进来
do_dump() {
    local ts file
    ts=$(date +%Y%m%d-%H%M%S)
    file="$BACKUP_DIR/db/${PG_DB}-${ts}.dump"
    log "导出 $PG_DB ..."
    if pg_is_remote; then
        pg_run pg_dump --dbname="$DATABASE_DSN" -Fc > "$file"
    else
        pg_run pg_dump -U "$PG_USER" -Fc -d "$PG_DB" > "$file"
    fi
    chmod 600 "$file"
    verify_dump "$file" >/dev/null || { rm -f "$file"; die "备份文件校验失败：$file"; }
    log "完成：$file（$(du -h "$file" | cut -f1)）"
    echo "$file"
}

# 自定义格式以 PGDMP 开头：比「文件非空」更能识别半截文件
verify_dump() {
    local f="$1"
    [ -s "$f" ] || return 1
    [ "$(head -c 5 "$f")" = "PGDMP" ] || return 1
    return 0
}

dump_env() {
    local src="$DEPLOY_DIR/deploy/.env"
    [ -f "$src" ] || { warn "$src 不存在，跳过密钥备份"; return 0; }
    install -m 600 -o root -g root "$src" "$BACKUP_DIR/env/env-$(date +%Y%m%d-%H%M%S)"
    log "已备份 .env（仅保留 root 可读）"
}

prune() {
    local n
    n=$(find "$BACKUP_DIR" -type f -mtime +"$KEEP_DAYS" | wc -l)
    find "$BACKUP_DIR" -type f -mtime +"$KEEP_DAYS" -delete
    [ "$n" -gt 0 ] && log "清理 $KEEP_DAYS 天前的备份：$n 个文件"
    # 至少留一份 .env 副本，别让 retention 把唯一副本也删了
    ls -1t "$BACKUP_DIR"/env/env-* 2>/dev/null | tail -n +2 | xargs -r rm -f
    return 0
}

cmd_run() {
    require_root run
    require_pg
    prepare_dir
    check_space
    do_dump >/dev/null
    dump_env
    prune
    log "备份完成。查看：sudo bash scripts/backup.sh list"
}

cmd_install_cron() {
    require_root install-cron
    cat > /etc/cron.d/stock-agent-backup <<EOF
# 每日备份：凌晨 3:30（与证书续期错开，避免同时占 IO）
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
30 3 * * * root bash $DEPLOY_DIR/scripts/backup.sh run >> /var/log/stock-agent-backup.log 2>&1
EOF
    chmod 644 /etc/cron.d/stock-agent-backup
    log "已安装 /etc/cron.d/stock-agent-backup（每日 03:30）"
    warn "先手动跑一次确认可用：sudo bash scripts/backup.sh run"
}

cmd_list() {
    [ -d "$BACKUP_DIR" ] || die "还没有任何备份（$BACKUP_DIR 不存在）"
    echo "== 数据库备份 =="
    ls -lht "$BACKUP_DIR/db" 2>/dev/null | sed -n '2,$p' || true
    echo "== .env 副本 =="
    ls -lht "$BACKUP_DIR/env" 2>/dev/null | sed -n '2,$p' || true
    echo "== 磁盘 =="
    df -h "$BACKUP_DIR" | sed -n '1p;2p'
}

cmd_verify() {
    require_root verify
    local f="${1:-}"
    [ -f "$f" ] || die "用法：backup.sh verify /var/backups/stock-agent/db/xxx.dump"
    verify_dump "$f" || die "不是有效的 pg_dump 自定义格式文件：$f"
    require_pg
    pg_run pg_restore -l < "$f" > /dev/null \
        || die "pg_restore 无法解析该备份：$f"
    log "校验通过：$f"
}

cmd_restore() {
    require_root restore
    local f="${1:-}" target="stock_agent_restore"
    shift || true
    if [ "${1:-}" = "--into" ]; then
        target="${2:?--into 后面要跟库名}"
        [ "$target" = "$PG_DB" ] || die "只允许恢复到 $PG_DB 或中间库，收到 $target"
        warn "即将覆盖生产库 $PG_DB，当前数据不可恢复"
        read -r -p "输入库名 $PG_DB 确认：" ans
        [ "$ans" = "$PG_DB" ] || die "已取消"
    fi
    [ -f "$f" ] || die "找不到备份文件：$f"
    verify_dump "$f" || die "不是有效的 pg_dump 文件：$f"
    require_pg

    # 中间库演练是默认路径：先证明备份能用，再考虑动生产
    if list_dbs | cut -d'|' -f1 | grep -qw "$target"; then
        [ "$target" = "$PG_DB" ] || drop_db "$target"
    fi
    [ "$target" = "$PG_DB" ] || create_db "$target"
    log "恢复 $f -> $target ..."
    # --no-owner：备份里的属主可能与目标库不一致；扩展(pgvector)由 initdb 脚本负责。
    # pg_restore 对「扩展已存在/权限」这类非致命项会返回非 0，不能直接 die，
    # 但也不能静默吞掉：把 stderr 落到日志，只对 ERROR 计数告警
    local errlog="$BACKUP_DIR/restore-$(date +%Y%m%d-%H%M%S).log" errs=0
    restore_into "$target" "$f" "$errlog"
    chmod 600 "$errlog"
    errs=$(grep -c '^pg_restore: error' "$errlog" 2>/dev/null || true)
    log "恢复结束，日志：$errlog"
    [ "${errs:-0}" -gt 0 ] && warn "pg_restore 报了 $errs 条 error，先看日志再判断是否可用"
    log "抽查：sudo -u postgres psql ... -d $target -c '\\dt'（本地可用 docker exec $PG_CONTAINER）"
    [ "$target" = "$PG_DB" ] || warn "演练库用完删掉：$target"
}

load_env

case "${1:-}" in
    run)          shift; cmd_run "$@" ;;
    install-cron) shift; cmd_install_cron "$@" ;;
    list)         shift; cmd_list "$@" ;;
    verify)       shift; cmd_verify "$@" ;;
    restore)      shift; cmd_restore "$@" ;;
    *)
        echo "用法: sudo bash scripts/backup.sh {run|install-cron|list|verify <文件>|restore <文件> [--into stock_agent]}"
        exit 1
        ;;
esac
