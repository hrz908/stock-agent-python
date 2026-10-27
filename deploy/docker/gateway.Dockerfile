# 网关 + agent 单进程镜像：一次启动同时提供 /api/v1/* 与 LangGraph 问答。
#
# 构建上下文是仓库根（见 deploy/compose/*.yml 的 context），因为镜像要同时拿到
# src/gateway、src/agent、src/shared 与 sql/bootstrap.sql。
#
# 为什么用 PYTHONPATH 而不是 pip install -e .：三个部署单元都不是可分发包，
# 装进 site-packages 只会让「改了代码没生效」的排查多一层。src 布局 + 一个
# PYTHONPATH 就够，且与 scripts/dev.sh 完全一致。
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

WORKDIR /app

# 先装依赖再拷代码：改业务代码不会击穿这一层构建缓存
COPY requirements/gateway.txt requirements/agent.txt /tmp/
# BuildKit 缓存挂载：pip 的 HTTP 缓存放在镜像层之外，requirements 变动时
# 不必重下整棵依赖树（pandas/akshare 那几个包最贵），镜像体积也不受影响。
# 依赖 compose v2 / docker 23+ 的默认 BuildKit；关掉 BuildKit 会报 unknown flag: --mount。
RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    pip install -i "$PIP_INDEX_URL" \
        -r /tmp/gateway.txt \
        -r /tmp/agent.txt

COPY src/ ./src/
# 网关自管三张表的幂等 DDL，启动时执行（gateway/main.py 的 SCHEMA_FILE）
COPY sql/ ./sql/

EXPOSE 8080

# 不加 --proxy-headers：真实 IP 由 gateway/middleware/clientip.py 按 TRUSTED_PROXIES 解析，
# 让 uvicorn 也改写一次 scope["client"] 会让同一套判断出现两个真相。
CMD ["uvicorn", "gateway.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-proxy-headers"]
