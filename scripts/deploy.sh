#!/usr/bin/env bash
# ===== StockA 生产部署脚本（Ubuntu 22.04/24.04，国内服务器）=====
#
# 用法（全程 sudo）：
#   sudo bash scripts/deploy.sh install-docker   # 首次：装 Docker + nginx + 防火墙 + swap
#   sudo bash scripts/deploy.sh deploy           # 首次部署（生成 .env/证书，构建启动）
#   sudo bash scripts/deploy.sh update           # 日常更新（git pull + 重建）
#   sudo bash scripts/deploy.sh restart          # 重启全部服务
#   sudo bash scripts/deploy.sh logs [服务名]    # 看日志（server/agent/postgres/redis）
#   sudo bash scripts/deploy.sh ps               # 看容器状态
#   sudo bash scripts/deploy.sh env-check        # 对比 .env 与模板的键差异（只读）
#   sudo bash scripts/deploy.sh env-append       # 把模板里新增的键补进 .env（会先备份）
#   sudo bash scripts/deploy.sh nginx-install    # 用仓库配置覆盖线上 nginx（改过域名却想同步时）
#
# 备份与探活是独立脚本：scripts/backup.sh、scripts/watchdog.sh（各自有 install-cron）。
#
# 前置条件：已 git clone 本仓库到 /opt/stock-agent（见 deploy/README.prod.md 第 1 步）。
#
# 环境变量 REMOTE_BUNDLE=/tmp/xxx.bundle：update 时跳过 git fetch origin，改从该 bundle
# 取代码后快进合并。服务器到 GitHub 不通时由 scripts/release.sh --via-ssh 传入。

set -euo pipefail

REPO_URL="https://github.com/hrz908/stock-agent-rag.git"
BRANCH="main"
DEPLOY_DIR="/opt/stock-agent"
# 相对 deploy/ 目录（脚本各处都会先 cd 到那里）
COMPOSE_FILE="compose/prod.yml"
SSL_DIR="/etc/nginx/ssl"
NGINX_CONF="/etc/nginx/conf.d/stock-agent.conf"

log()  { echo -e "\033[32m[deploy]\033[0m $*"; }
warn() { echo -e "\033[33m[deploy]\033[0m $*"; }
die()  { echo -e "\033[31m[deploy] 错误: $*\033[0m" >&2; exit 1; }

require_root() {
    [ "$(id -u)" -eq 0 ] || die "请用 sudo 运行：sudo bash scripts/deploy.sh $*"
}

require_repo() {
    [ -d "$DEPLOY_DIR/.git" ] || die "$DEPLOY_DIR 不是 git 仓库。先执行：git clone $REPO_URL $DEPLOY_DIR"
}

require_docker() {
    command -v docker >/dev/null 2>&1 || die "未检测到 docker，先执行：sudo bash scripts/deploy.sh install-docker"
    docker info >/dev/null 2>&1 || die "docker 未运行：systemctl start docker"
}

# ---------------------------------------------------------------- 数据库位置
# DATABASE_DSN 为空（或指向 compose 内的 postgres）时启用自己的 postgres 容器；
# 指向外部 PG（腾讯云内网地址）时跳过 postgres 服务，行为与迁移前一致。
uses_external_pg() {
    local env_file="$DEPLOY_DIR/deploy/.env" dsn
    [ -r "$env_file" ] || return 1
    dsn=$(grep -E '^DATABASE_DSN=' "$env_file" | tail -1 | cut -d= -f2- | tr -d '\r' || true)
    [ -n "$dsn" ] || return 1
    case "$dsn" in
        *@postgres:*|*@localhost:*|*@127.0.0.1:*) return 1 ;;
    esac
    return 0
}

# 所有 compose 调用统一走这里：自带 PG 需要 --profile localpg 才会起 postgres 服务
compose() {
    local args=()
    uses_external_pg || args+=(--profile localpg)
    docker compose -f "$COMPOSE_FILE" "${args[@]}" "$@"
}

# ---------------------------------------------------------------- swap
# 4 vCPU / 3GB 内存同时跑 postgres + redis + Go + Python：没有任何余量，
# 一次 embedding 或大查询抖动就 OOM 杀进程，表现为「容器莫名重启」而没有日志。
# 4G swap 只做兜底，swappiness 调低，避免热数据被换出反而变慢。
ensure_swap() {
    if [ "$(swapon --show --noheadings 2>/dev/null | wc -l)" -gt 0 ]; then
        log "swap 已启用，跳过"
        return 0
    fi
    log "创建 4G swap ..."
    if ! fallocate -l 4G /swapfile 2>/dev/null; then
        dd if=/dev/zero of=/swapfile bs=1M count=4096 status=none
    fi
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null
    swapon /swapfile
    grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
    sysctl -w vm.swappiness=10 >/dev/null 2>&1 || true
    grep -q '^vm.swappiness' /etc/sysctl.conf || echo 'vm.swappiness=10' >> /etc/sysctl.conf
    log "swap 就绪：4G"
}

# ---------------------------------------------------------------- install-docker
cmd_install_docker() {
    require_root install-docker

    if command -v docker >/dev/null 2>&1; then
        log "docker 已存在，跳过安装（如需重装请手动处理）"
    else
        log "安装 docker-ce（阿里云源）..."
        apt-get update
        apt-get install -y ca-certificates curl gnupg
        install -m 0755 -d /etc/apt/keyrings
        curl -fsSL https://mirrors.aliyun.com/docker-ce/linux/ubuntu/gpg \
            -o /etc/apt/keyrings/docker.asc
        chmod a+r /etc/apt/keyrings/docker.asc
        echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://mirrors.aliyun.com/docker-ce/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
            > /etc/apt/sources.list.d/docker.list
        apt-get update
        apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
        systemctl enable docker
        systemctl start docker
    fi

    log "配置 registry 镜像加速（拉镜像失败时可删掉 /etc/docker/daemon.json 里的 mirrors 两项）..."
    mkdir -p /etc/docker
    if [ ! -f /etc/docker/daemon.json ]; then
        cat > /etc/docker/daemon.json <<'EOF'
{
  "registry-mirrors": ["https://docker.1ms.run", "https://docker.m.daocloud.io"]
}
EOF
        systemctl restart docker
    fi

    if ! command -v nginx >/dev/null 2>&1; then
        log "安装 nginx..."
        apt-get update
        apt-get install -y nginx
        systemctl enable nginx
    else
        log "nginx 已存在，跳过安装"
    fi

    ensure_swap

    log "配置防火墙 ufw（仅放行 SSH/80/443）..."
    if ufw status 2>/dev/null | grep -q "Status: active"; then
        ufw allow OpenSSH
        ufw allow 80/tcp
        ufw allow 443/tcp
    else
        warn "ufw 未激活，跳过。请确认云厂商安全组已放行 22/80/443"
    fi

    # 把调用者加入 docker 组，以后免 sudo 用 docker（重新登录生效）
    if [ -n "${SUDO_USER:-}" ] && id "$SUDO_USER" >/dev/null 2>&1; then
        usermod -aG docker "$SUDO_USER" || true
        log "已将 $SUDO_USER 加入 docker 组（重新登录后生效）"
    fi

    log "install-docker 完成。下一步：sudo bash scripts/deploy.sh deploy"
}

# ---------------------------------------------------------------- deploy（首次）
gen_self_signed_cert() {
    if [ -f "$SSL_DIR/stock-agent.crt" ] && [ -f "$SSL_DIR/stock-agent.key" ]; then
        log "自签名证书已存在，跳过生成"
        return
    fi
    log "生成自签名证书（SAN 带公网 IP，微信开发者工具需勾选不校验域名）..."
    local ip
    ip=$(curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null \
        || curl -fsS --max-time 5 https://ifconfig.me 2>/dev/null \
        || hostname -I | awk '{print $1}')
    mkdir -p "$SSL_DIR"
    openssl req -x509 -nodes -newkey rsa:2048 -days 3650 \
        -keyout "$SSL_DIR/stock-agent.key" \
        -out "$SSL_DIR/stock-agent.crt" \
        -subj "/CN=stock-agent" \
        -addext "subjectAltName=IP:${ip},IP:127.0.0.1,DNS:localhost"
    chmod 600 "$SSL_DIR/stock-agent.key"
    log "证书公网 IP: $ip"
}

# 线上 nginx 配置常被手工改（换域名、换证书路径、加限流）。无条件 cp 会把改动
# 静默打回 server_name _ + 自签证书，症状是「域名切好了，跑一次 deploy 又变回去」。
# 因此：已存在且不同就保留线上版本，只提示差异；要覆盖必须显式 --force。
install_nginx_conf() {
    local force="${1:-}"
    local src="$DEPLOY_DIR/deploy/nginx/prod.conf"
    if [ -f "$NGINX_CONF" ] && [ "$force" != "--force" ]; then
        if cmp -s "$src" "$NGINX_CONF"; then
            log "nginx 配置与仓库一致"
        else
            warn "线上 nginx 配置与仓库不一致，保留线上版本（手工改过域名/证书就会这样）"
            warn "查看差异：diff -u $src $NGINX_CONF"
            warn "确认用仓库版本覆盖：sudo bash scripts/deploy.sh nginx-install --force"
        fi
    else
        if [ -f "$NGINX_CONF" ]; then
            cp -a "$NGINX_CONF" "${NGINX_CONF}.bak.$(date +%Y%m%d%H%M%S)"
            warn "已备份旧配置：${NGINX_CONF}.bak.*"
        fi
        cp "$src" "$NGINX_CONF"
        log "已安装 nginx 配置"
    fi
    rm -f /etc/nginx/sites-enabled/default
    nginx -t
    systemctl reload nginx || systemctl start nginx
}

cmd_nginx_install() {
    require_root nginx-install
    require_repo
    install_nginx_conf --force
}

cmd_deploy() {
    require_root deploy
    require_repo
    require_docker

    local env_file="$DEPLOY_DIR/deploy/.env"
    if [ ! -f "$env_file" ]; then
        cp "$DEPLOY_DIR/deploy/.env.production.example" "$env_file"
        warn "已生成 $env_file，请先编辑填入密钥（JWT_SECRET/POSTGRES_PASSWORD/REDIS_PASSWORD/WECHAT_SECRET/OPENAI_API_KEY），再重新运行 deploy"
        exit 1
    fi

    local missing=""
    local key val
    for key in JWT_SECRET POSTGRES_PASSWORD REDIS_PASSWORD WECHAT_SECRET OPENAI_API_KEY; do
        val=$(grep -E "^${key}=" "$env_file" | tail -1 | cut -d= -f2- || true)
        [ -z "$val" ] && missing="$missing $key"
    done
    [ -z "$missing" ] || die ".env 缺少必填项：$missing"

    cmd_env_check

    gen_self_signed_cert

    log "安装 nginx 配置..."
    install_nginx_conf

    log "构建并启动全部服务（首次约 5-15 分钟，主要在拉镜像和 pip 装依赖）..."
    cd "$DEPLOY_DIR/deploy"
    compose up -d --build

    log "等待服务就绪..."
    local ok=0
    for _ in $(seq 1 30); do
        if curl -kfsS https://127.0.0.1/healthz >/dev/null 2>&1; then ok=1; break; fi
        sleep 2
    done
    [ "$ok" = "1" ] || die "健康检查未通过，请查看日志：sudo bash scripts/deploy.sh logs server"

    log "部署完成。验证：curl -k https://$(curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null || echo '<服务器IP>')/healthz"
    log "运维收尾（首次各一次）：sudo bash scripts/backup.sh install-cron；sudo bash scripts/watchdog.sh install-cron"
    log "下一步（小程序联调）：改 stock-agent-miniapp/config.js 的 develop 段为 https://<服务器IP>，详见 deploy/README.prod.md"
}

# ---------------------------------------------------------------- update
cmd_update() {
    require_root update
    require_repo
    require_docker

    cd "$DEPLOY_DIR"
    # 门禁 1：服务器工作区必须干净（.env 等 ignored 文件不算），
    # 防止服务器上的手工改动被 pull 覆盖或阻塞快进
    [ -z "$(git status --porcelain)" ] || die "服务器有未提交改动，先导出 diff 并回滚，禁止直接更新"

    # 两条取代码的通道：默认走 origin（GitHub）；服务器连不上 GitHub 时，
    # release.sh --via-ssh 把 bundle 传到 /tmp，这里从文件取代码后快进合并
    local src_ref
    if [ -n "${REMOTE_BUNDLE:-}" ]; then
        log "使用本地传来的 bundle $REMOTE_BUNDLE（跳过 git fetch origin）"
        [ -f "$REMOTE_BUNDLE" ] || die "找不到 $REMOTE_BUNDLE：确认本地上传成功了"
        git fetch "$REMOTE_BUNDLE" "$BRANCH"
        src_ref="FETCH_HEAD"
    else
        log "拉取最新代码（$BRANCH）..."
        git fetch origin "$BRANCH"
        src_ref="origin/$BRANCH"
    fi
    # 门禁 2：只允许快进；分叉时中止，交给人工处理
    git merge-base --is-ancestor HEAD "$src_ref" || die "服务器与 $src_ref 已分叉，先人工处理"
    PREV=$(git rev-parse --short HEAD)
    log "更新前提交：$PREV"
    git merge --ff-only "$src_ref"

    # 新版本可能引入新配置项：模板变了但已存在的 .env 不会被更新，这里提示差异
    cmd_env_check

    log "重建并重启..."
    cd "$DEPLOY_DIR/deploy"
    compose up -d --build
    docker image prune -f

    log "更新完成。状态：sudo bash scripts/deploy.sh ps"
}

# ---------------------------------------------------------------- .env 一致性
# 模板只在首次部署时 cp 一次，之后 git pull 不会同步到已存在的 .env：新增配置项
# 不会自动出现在生产（代码有默认值，功能不受影响，但运维看不到开关）。
# 这里只做只读比对；补写必须显式跑 env-append，避免 update 静默改生产配置。
env_keys() {
    grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$1" 2>/dev/null | cut -d= -f1 | sort -u || true
}

env_missing_keys() {
    comm -23 <(env_keys "$DEPLOY_DIR/deploy/.env.production.example") <(env_keys "$1")
}

# .env 是 root:600，非 root 跑会把「读不到」当成「键全缺」：env-check 报 31 项全缺，
# env-append 直接把整套键重复追加一遍。前置校验必须放在比对之前。
env_guard() {
    local env_file="$DEPLOY_DIR/deploy/.env"
    local tpl="$DEPLOY_DIR/deploy/.env.production.example"
    [ -f "$tpl" ] || die "模板缺失：$tpl"
    [ -f "$env_file" ] || die "$env_file 不存在，先执行 deploy"
    [ -r "$env_file" ] || die "$env_file 不可读（root:600），用 sudo 重跑：sudo bash scripts/deploy.sh $1"
}

cmd_env_check() {
    env_guard env-check
    local env_file="$DEPLOY_DIR/deploy/.env"
    local tpl="$DEPLOY_DIR/deploy/.env.production.example"
    local miss
    miss=$(env_missing_keys "$env_file")
    if [ -z "$miss" ]; then
        log ".env 键集合与模板一致"
        return 0
    fi
    warn ".env 缺少模板里的这些键（代码有默认值，不补也能跑）："
    while read -r k; do
        [ -n "$k" ] || continue
        echo "    $(grep -E "^${k}=" "$tpl" | tail -1)"
    done <<< "$miss"
    warn "补齐：sudo bash scripts/deploy.sh env-append（补完 restart 生效）"
    return 0
}

cmd_env_append() {
    require_root env-append
    require_repo
    env_guard env-append
    local env_file="$DEPLOY_DIR/deploy/.env"
    local tpl="$DEPLOY_DIR/deploy/.env.production.example"
    local miss
    miss=$(env_missing_keys "$env_file")
    if [ -z "$miss" ]; then
        log "无缺失项，未改动 .env"
        return 0
    fi
    # 兜底：缺失数 ≈ 模板键数，几乎只可能是读到了空 .env；宁可中止也不能重复追加一整份
    local total miss_n
    total=$(env_keys "$tpl" | grep -c . || true)
    miss_n=$(printf '%s\n' "$miss" | grep -c . || true)
    if [ "$miss_n" -ge "$total" ]; then
        die "缺失 $miss_n 项等于模板键数 $total，疑似 .env 读取失败，已中止（文件未改动）"
    fi
    # 只留一份 .env.bak（每次覆盖）：追加是纯尾部追加、不改原有行，备份只是兜底；
    # 带时间戳会攒出一堆含生产密钥的副本，反而扩大泄露面
    cp -a "$env_file" "${env_file}.bak"
    {
        echo ""
        echo "# ===== 自动补齐自 .env.production.example（$(date +%F)）====="
        while read -r k; do
            [ -n "$k" ] || continue
            grep -E "^${k}=" "$tpl" | tail -1 || true
        done <<< "$miss"
    } >> "$env_file"
    log "已追加到 $env_file：$(echo "$miss" | tr '\n' ' ')"
    warn "补的是模板默认值，与线上实际不符请手改，然后 restart"
}

# ---------------------------------------------------------------- 其它
cmd_restart() {
    require_root restart
    require_docker
    cd "$DEPLOY_DIR/deploy"
    compose restart
    log "已重启"
}

cmd_logs() {
    require_docker
    cd "$DEPLOY_DIR/deploy"
    compose logs -f --tail=100 "${@:-}"
}

cmd_ps() {
    require_docker
    cd "$DEPLOY_DIR/deploy"
    compose ps
}

case "${1:-}" in
    install-docker) shift; cmd_install_docker "$@" ;;
    deploy)         shift; cmd_deploy "$@" ;;
    update)         shift; cmd_update "$@" ;;
    restart)        shift; cmd_restart "$@" ;;
    logs)           shift; cmd_logs "$@" ;;
    ps)             shift; cmd_ps "$@" ;;
    env-check)      shift; cmd_env_check "$@" ;;
    env-append)     shift; cmd_env_append "$@" ;;
    nginx-install)  shift; cmd_nginx_install "$@" ;;
    *)
        echo "用法: sudo bash scripts/deploy.sh {install-docker|deploy|update|restart|logs [服务]|ps|env-check|env-append|nginx-install}"
        exit 1
        ;;
esac
