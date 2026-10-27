# 本地环境一次性配置（换电脑看这里）

代码都在 GitHub 上，**clone 恢复不了的只有三类**：`.git/hooks/` 里的钩子、`.git/config` 里的 remote 与别名、全局 git 配置；外加被 `.gitignore` 忽略的本地文件。

## 一条命令

```bash
bash scripts/install-hooks.sh           # 装钩子 + remote.prod + release/pushsafe 别名
bash scripts/install-hooks.sh --check   # 只体检，有缺失时退出码 1
```

> 终端用 **Git Bash**（本机默认 shell），本节命令直接 `bash scripts/install-hooks.sh`。
> 已在 PowerShell 里时 `bash` 指向 WSL，会报 `/bin/bash: No such file or directory`，换到 Git Bash 窗口再跑（装好之后日常走 `git release`，不需要 bash）。

装完后 `git pushsafe` / `git release` 就能直接用（任意子目录）。脚本只写"缺失项"，已存在且内容不同的会保留并提示，`--force` 才覆盖。

| 项 | 位置 | 作用 |
|---|---|---|
| `.git/hooks/pre-push` | 仓库内 | push 前 fetch，远端领先就中止 |
| `remote.prod` | `.git/config` | 指向 `ssh://ubuntu@tearsinrain.top/opt/stock-agent`，`pushurl=DISABLED` |
| `alias.release` | `.git/config` | `!bash scripts/release.sh`，发布入口 |
| `alias.pushsafe` | `~/.gitconfig` | fetch + 干净门禁 + rebase + push |

## 脚本不管、要手动补

### 1. SSH 与 GitHub 凭据

```bash
ssh-keygen -t ed25519                 # 新机没有私钥就连不上服务器
ssh-copy-id ubuntu@tearsinrain.top    # 或手工追加到服务器 ~/.ssh/authorized_keys
ssh ubuntu@tearsinrain.top 'echo ok'  # 首次连接接受指纹，写入 known_hosts
```

GitHub 走 HTTPS（`origin`），首次 push 需要浏览器授权或 PAT；Windows 下由 Git Credential Manager 代管。

### 2. 全局 git 配置（旧机 `git config --global --list` 对照抄）

```bash
git config --global user.name 'hrz'
git config --global user.email 'z6478189288@gmail.com'
git config --global http.proxy http://127.0.0.1:7897   # 端口随代理软件，错了 fetch/push 全失败
git config --global https.proxy http://127.0.0.1:7897
git config --global pull.ff only
git config --global rerere.enabled true
git config --global merge.conflictStyle zdiff3
git config --global alias.pushsafe '!git fetch origin && git diff --quiet && git diff --cached --quiet && git rebase origin/main && git push'
```

`alias.pushsafe` 上面这条已包含在 `install-hooks.sh` 里；`safe.directory = *` 不要照抄，那是旧机为绕过属主告警加的。

### 3. 本地文件与依赖（被忽略，clone 拿不到）

| 文件/目录 | 用途 | 恢复方式 |
|---|---|---|
| 仓库根 `.env` | 本地跑服务必需（`dev.sh dev`） | `cp .env.example .env` 后填 `OPENAI_API_KEY`；键更全的模板见 `deploy/.env.production.example`。旧位置的 `server/.env`、`src/gateway/.env` 仍能被 `dev.sh` 与 `load_config` 兜住 |
| `stock-agent-miniapp/project.private.config.json` | 开发者工具个人配置 | 可不要，工具会重新生成 |
| `stock-agent-miniapp/node_modules` + `miniprogram_npm` | 小程序依赖 | `bash scripts/dev.sh miniapp` + 工具里「构建 npm」 |
| `.venv`（仓库根，server+agent 共用一套） | Python 依赖 | `bash scripts/dev.sh setup`（有 uv 走 uv，秒级；无 pip 的 uv venv 会自动 ensurepip） |
| Docker Desktop / Python 3.11（uv 可选）/ Node | 本地开发 | 各自装 |
| `/opt/stock-agent/models`（服务器） | 向量模型权重 | 只在服务器需要，本地不用 |

### 4. 验证

```bash
git remote -v           # prod 的 push 显示 DISABLED
git fetch prod          # 能拉到就说明 SSH、地址、权限都对（只读，不动服务器）
git release --dry-run   # 打印本地与服务器当前提交，不推送
```

## 服务器换地址时要改的 4 处

1. 本地 remote：`bash scripts/install-hooks.sh --force`（或 `git remote set-url prod ssh://ubuntu@<新>/opt/stock-agent`）
2. 旧主机指纹：`ssh-keygen -R tearsinrain.top`，再连一次接受新指纹
3. 小程序 `stock-agent-miniapp/config.js` 的 `develop` / `trial` / `release` 三处 `apiBase`
4. 服务器侧 nginx `server_name` 与证书：自签证书的 SAN 写的是生成时的公网 IP，换 IP 必须重签；域名变了还要改微信后台合法域名

部署目录若不再是 `/opt/stock-agent`，还得改 `deploy.sh:23`、`backup.sh:23`、`watchdog.sh:20` 及 `watchdog.sh` 写进 cron 的绝对路径。

## 常见坑

- **CRLF**：`.git/hooks/pre-push` 必须是 LF，否则 `sh` 报 syntax error（脚本用 heredoc 写入，天然 LF；手工拷文件要确认）。
- **别用 WSL 的 bash**：本机 `bash` 指向 WSL，`git release` 走的是 Git 自带的 bash，没这个问题。
- **Git Bash 的 TTY**：`psql` / `redis` 是交互式命令，mintty 窗口里 docker 拿不到 Windows 控制台，`dev.sh` 会自动加 `winpty` 前缀；被管道调用（CI、脚本里）时这两条会直接报错退出。
- **服务器连不上 GitHub**（`[release] 错误: 服务器更新失败`，服务器上 `git fetch` 报
  `Failed to connect to github.com port 443`）：改用 `git release --via-ssh`，
  本地把分支打成 bundle 传到服务器 `/tmp`，服务器侧从文件取代码、跳过 fetch；线路恢复后照旧 `git release`。
- **代理端口**：全局 `http.proxy` 写死 `127.0.0.1:7897`，换代理端口后 push 会卡住或报 `Failed to connect`。
- **`JWT_SECRET` 换机器迁移服务器时要保持不变**：换了等于所有用户 token 失效，小程序强制重新登录。

服务器侧的安装、备份、探活见 [`deploy/README.md`](../deploy/README.md)。
