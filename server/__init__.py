"""stock-agent 网关（Python 版）。

原 Go 服务（gin + gorm）的 1:1 迁移，并与 agent 合并为同一 FastAPI 进程：
- 对外契约不变：{code,message,data} 信封、/api/v1 路径、SSE 帧格式；
- 部署契约不变：容器内监听 8080、/healthz、uploads 卷。
"""
