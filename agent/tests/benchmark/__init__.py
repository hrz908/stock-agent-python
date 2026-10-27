"""股票问答 Agent 测试与调优基准（benchmark）包。

模块划分：
- cases      : 题库（覆盖类型 / 难度 / 期望断言）
- sse        : 生产链路客户端（JWT 签发 + SSE 帧解析 + /api/v1/agent/stream）
- runner     : 批量执行（in-process 图 or 生产 HTTP），收集回答与事件
- evaluate   : 质量评估（规则断言 + 数字/引用诊断 + LLM 评委五维打分）
- report     : 报告生成与 A/B 对比（P50/P90/P95/P99）
- run        : 质量 CLI 入口
- perf       : 阶梯压测（并发曲线 + 容器/PG 资源 + 429/降级观测）
- gen_retrieval_cases : 检索评测集生成（chunk 反向出题，带 gold_chunk_ids）
- sweep_retrieval     : 候选池 N × 上线数量 k 扫描与选优（采集 → 离线重放 → 报告）

统一入口：`bash scripts/bench.sh help`
"""
