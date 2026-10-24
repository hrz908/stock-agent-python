# StockA 生产部署手册（国内 Ubuntu 服务器，阶段一：IP + 自签名证书）

> 目标架构：宿主机 nginx(443) → Go server 容器(:8080, 仅回环) → agent / postgres(pgvector) / redis 容器。
> 数据库与 Redis **不对公网暴露端口**，全部走 compose 内网。

```
小程序/浏览器
   │  https://<服务器IP>  (自签名证书，开发者工具关域名校验)
   ▼
宿主机 nginx ──► 127.0.0.1:8080 ──► server 容器 (Go)
                                     ├─► agent 容器 (Python/LangGraph) ─► 硅基流动 LLM
                                     ├─► postgres 容器 (pgvector, 数据卷)
                                     └─► redis 容器 (限流/会话, 数据卷)
```

## 0. 前置准备

1. **本地代码已全部 push**：服务器从 `https://github.com/hrz908/stock-agent-rag.git`（main 分支）拉取。先在本地 `git status` 确认没有未提交改动。
2. **微信 AppSecret**：mp.weixin.qq.com → 开发管理 → 开发设置 → AppSecret（重置后只显示一次，先复制）。
3. **硅基流动 API Key**（LLM + 语音识别）：`OPENAI_API_KEY`。
4. **云厂商安全组放行**：22（SSH）、80、443。ufw 由脚本自动配置。

## 1. 登录服务器，clone 仓库

```bash
ssh root@<服务器IP>
git clone https://github.com/hrz908/stock-agent-rag.git /opt/stock-agent
cd /opt/stock-agent
```

## 2. 安装 Docker / nginx / 防火墙

```bash
sudo bash scripts/deploy.sh install-docker
```

阿里云源装 docker-ce + registry 镜像加速 + nginx + ufw(22/80/443)。拉镜像失败时编辑 `/etc/docker/daemon.json` 删掉 mirrors 两项或换加速器。

## 3. 首次部署

```bash
sudo bash scripts/deploy.sh deploy
```

脚本会：生成 `deploy/.env`（**首次运行只生成模板然后退出，要求你填密钥**）→ 校验必填项 → 生成自签名证书 → 装 nginx 配置 → 构建启动（首次 5~15 分钟）→ 健康检查。

编辑 `deploy/.env` 填入：

| 变量 | 说明 | 获取方式 |
|---|---|---|
| `JWT_SECRET` | JWT 签名密钥 | `openssl rand -hex 32` |
| `POSTGRES_PASSWORD` | 数据库密码 | `openssl rand -hex 16` |
| `REDIS_PASSWORD` | Redis 密码 | 同上 |
| `WECHAT_APPID` | 小程序 appid | 已预填 `wx6da8b7aed78257af` |
| `WECHAT_SECRET` | 小程序 secret | 见前置准备第 2 条 |
| `OPENAI_API_KEY` | LLM/ASR 密钥 | 硅基流动 |
| `GO_SERVER_TOKEN` | 选填，agent 回调 Go 的 JWT | 见下文「常见问题 5」 |

填完再跑一次 `sudo bash scripts/deploy.sh deploy`。

## 4. 验证

```bash
curl -k https://<服务器IP>/healthz          # 期望 {"code":0,...,"status":"ok"} 之类
sudo bash scripts/deploy.sh ps              # 四个容器全部 Up
```

## 5. 小程序联调（阶段一，无域名）

1. 改 `stock-agent-miniapp/config.js` 的 `develop` 段：

```js
develop: {
  apiBase: 'https://<服务器IP>',
  wsBase:  'wss://<服务器IP>'
},
```

2. `project.config.json` 的 `setting.urlCheck` 已是 `false`（等价于开发者工具勾选「不校验合法域名、TLS 版本以及 HTTPS 证书」），**自签名证书 + IP 直连**可以正常调试。
3. 开发者工具编译预览 / 真机调试，验证：登录、行情卡、AI 对话（WebSocket）、语音、提醒。

> 注意：此配置只能用于开发版/调试。正式版提交审核必须用**已备案域名 + CA 证书**（见阶段二）。

## 6. 日常运维

```bash
sudo bash scripts/deploy.sh update            # 本地 push 后：拉代码 + 重建 + 重启
sudo bash scripts/deploy.sh restart           # 只重启
sudo bash scripts/deploy.sh logs server       # 看某服务日志（server/agent/postgres/redis）
sudo bash scripts/deploy.sh logs              # 全部服务日志
```

## 7. 阶段二：域名备案下来后

1. **DNS**：域名 A 记录 → 服务器 IP，生效后用 `dig +short <域名>` 确认。
2. **签证书**（Let's Encrypt，80 端口脚本已预留 ACME 入口）：

```bash
sudo apt-get install -y certbot
sudo certbot certonly --webroot -w /var/www/html -d <域名>
```

3. **改 nginx 三处**（`/etc/nginx/conf.d/stock-agent.conf`，同时把改动同步回 `deploy/nginx.prod.conf` 防被 update 覆盖）：

```nginx
server_name _;                        →  server_name <域名>;        # 两个 server 块都改
ssl_certificate     /etc/nginx/ssl/stock-agent.crt;
ssl_certificate_key /etc/nginx/ssl/stock-agent.key;
→  /etc/letsencrypt/live/<域名>/fullchain.pem;  # 与 privkey.pem
```

然后 `sudo nginx -t && sudo systemctl reload nginx`。自动续期：`sudo crontab -e` 加 `0 3 * * 1 certbot renew --quiet && systemctl reload nginx`。

4. **微信公众平台** → 开发管理 → 服务器域名：
   - request 合法域名：`https://<域名>`
   - socket 合法域名：`wss://<域名>`
5. **改小程序配置**：`config.js` 的 `trial`（体验版）/ `release`（正式版）段换成 `https://<域名>` / `wss://<域名>`，然后上传体验版测试 → 提交审核。

## 8. 安全提醒（重要）

- **企业微信 secret 泄露风险**：`server/internal/config/config.go` 的 `setDefaults()` 里硬编码了 `wecom_corp_id / wecom_secret / wecom_agent_id` 默认值，而仓库是**公开**的，secret 按已泄露处理。收掉它：
  1. 企业微信管理后台 → 应用管理 → 自建应用 →「查看」→ **重置 Secret**（旧值立即作废）；
  2. 把新的 `WECOM_CORP_ID / WECOM_SECRET / WECOM_AGENT_ID` 填进 `deploy/.env`——`wecom.Enabled()` 要求三项齐全，只填 secret 不生效；
  3. `sudo bash scripts/deploy.sh restart`。轮换后不填 `.env` 的后果：企业微信推送失败，小程序内 WebSocket 提醒不受影响。
- **自签名证书只用于阶段一调试**，别用于正式版。
- `.env` 含全部生产密钥，只在服务器上存在；仓库里只有 `.env.production.example` 模板。
- **模板不会自动同步到 `.env`**：`.env` 仅在首次部署时从模板复制一次，之后 `update` 只做 `git pull + 重建`，模板里新增的配置项不会自己出现在生产。`deploy` / `update` 会顺手跑一次 `env-check` 打印差异，确认后执行 `sudo bash scripts/deploy.sh env-append` 补齐（先备份，补的是模板默认值）。
- Go server 的 8080 只绑 `127.0.0.1`；postgres/redis 无宿主端口映射——保持这个设计，别为了方便暴露出去。

## 9. 常见问题

1. **prod 模式启动即退出**（`JWT_SECRET 未设置或仍是默认值` / `WECHAT_* 未配置`）：`.env` 必填项没填全，`deploy.sh` 会提前拦，但如果手工改过配置就会在 server 日志里看到。
2. **pip / 镜像拉取慢或失败**：确认用的是 `scripts/deploy.sh`（已内置清华源 + registry 加速）；Dockerfile 的 `PIP_INDEX_URL` / `GOPROXY` 可用 build-arg 覆盖。
3. **nginx 502**：server 容器没起来。`sudo bash scripts/deploy.sh logs server`；常见原因是 `.env` 密钥问题导致 prod 拒绝启动。
4. **migrations 不生效**：`/docker-entrypoint-initdb.d` 只在 postgres 数据卷**为空**时执行。改表请新增 `server/migrations/00X_*.sql`，已在跑的库要手工执行；`ashare` 容器里可用 `python -m ashare.cli init-db --file 008_stock_master.sql` 逐条执行（`--file` 可重复）。
5. **agent 行情工具 401**：`GO_SERVER_TOKEN` 没配。先正常部署，用小程序登录后从 `deploy.sh logs server` 或接口响应里拿一个 JWT 填进 `.env`，`restart` 一次即可；不填不影响对话和行情卡。
6. **改防火墙后 SSH 断了**：`install-docker` 的 ufw 步骤先 allow OpenSSH 再 allow 80/443；若已被锁，用云厂商 VNC 进服务器 `ufw allow OpenSSH`。
7. **`env-check` 提示 `.env` 缺键**：新版本在模板里加了配置项，而 `.env` 是首次部署时复制的、不会被 `git pull` 更新。不补也能跑（代码里有同名默认值），只是运维看不到这些开关；要补就 `sudo bash scripts/deploy.sh env-append`，之后 `restart`。

## 10. A股语料与主数据（可选 profile）

设计与参数说明见 [`docs/ASHARE_VECTORIZATION.md`](../docs/ASHARE_VECTORIZATION.md)；
业务表取舍见 [`docs/ASHARE_BUSINESS_SCHEMA.md`](../docs/ASHARE_BUSINESS_SCHEMA.md)。

```bash
# 1) 模型权重放到宿主机（compose 只读挂载，容器内不联网拉取）
#    /opt/stock-agent/models/Qwen3-Embedding-4B
# 2) 首次部署：先跑迁移（已有数据卷不会自动执行 initdb）
cd /opt/stock-agent/deploy
docker compose -f docker-compose.prod.yml --profile ashare run --rm ashare \
  python -m ashare.cli init-db
# 2.1) 主数据表：008 清单/行业/概念，009 财务指标宽表（P1，可后置），010 profile 视图
docker compose -f docker-compose.prod.yml --profile ashare run --rm ashare \
  python -m ashare.cli init-db --file 008_stock_master.sql \
    --file 009_stock_finance_indicator.sql --file 010_stock_profile_any_source.sql
# 3) 首次灌主数据：先清单，再行业/概念（东财可用时约 4~5 分钟；退新浪兜底时约 3 分钟）
docker compose -f docker-compose.prod.yml --profile ashare run --rm ashare \
  python -m ashare.cli sync-master --only basic
docker compose -f docker-compose.prod.yml --profile ashare run --rm ashare \
  python -m ashare.cli sync-master
# 4) 启动常驻采集
docker compose -f docker-compose.prod.yml --profile ashare up -d --build ashare
# 5) 验证
docker compose -f docker-compose.prod.yml exec ashare python -m ashare.cli stats
```

主数据的日常维护：`--only basic` 每天一次（改名/退市/ST），行业与概念每周一次。
没灌数据时 `/api/v1/stocks/profile` 会返回 404、搜索的本地兜底不生效（正常搜索不受影响，
仍走腾讯源）。验收 SQL 见业务表文档 §10。

注意：`ashare` 与文件 ingest 共用 `chunks` 表，维度由 `.env` 的 `EMBED_DIM` 决定
（当前 1536），与 agent 的 `ONLINE_EMBED_MODEL/EMBED_DIM` 必须一致；换模型要同时重嵌两边。
