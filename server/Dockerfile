# 网关 + agent 单进程镜像：一次启动同时提供 /api/v1/* 与 LangGraph 问答。
#
# 与旧版的双镜像相比：
# - 少一份 Python 运行时与一套依赖树（原 server 是 Go 镜像，agent 是 Python 镜像）；
# - 少一跳 HTTP（原来网关要 POST 到 agent:8000 的 ndjson 流再转 SSE）。
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # agent 侧按「项目根 = agent/」的方式互引（from core.x / from tools.x）
    PYTHONPATH=/app/agent

ARG PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

WORKDIR /app

# 先装依赖再拷代码：改业务代码不会击穿这一层构建缓存
COPY agent/requirements.txt /tmp/requirements-agent.txt
COPY server/requirements.txt /tmp/requirements-gateway.txt
# BuildKit 缓存挂载：pip 的 HTTP 缓存放在镜像层之外，requirements 变动时
# 不必重下整棵依赖树（pandas/akshare 那几个包最贵），镜像体积也不受影响。
# 依赖 compose v2 / docker 23+ 的默认 BuildKit；关掉 BuildKit 会报 unknown flag: --mount。
RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    pip install -i "$PIP_INDEX_URL" \
        -r /tmp/requirements-agent.txt \
        -r /tmp/requirements-gateway.txt

COPY agent/ ./agent/
COPY server/ ./server/

EXPOSE 8080

# 不加 --proxy-headers：真实 IP 由 server/middleware/clientip.py 按 TRUSTED_PROXIES 解析，
# 让 uvicorn 也改写一次 scope["client"] 会让同一套判断出现两个真相。
CMD ["uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-proxy-headers"]
