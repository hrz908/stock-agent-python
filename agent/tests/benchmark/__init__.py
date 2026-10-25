"""股票问答 Agent 测试与调优基准（benchmark）包。

模块划分：
- cases      : 题库（覆盖类型 / 难度 / 期望断言）
- runner     : 批量执行（in-process 图 or HTTP），收集回答与事件
- evaluate   : 质量评估（规则断言 + LLM 评委五维打分）
- report     : 报告生成与 A/B 对比
- run        : CLI 入口
- gen_retrieval_cases : 检索评测集生成（chunk 反向出题，带 gold_chunk_ids）
- sweep_retrieval     : 候选池 N × 上线数量 k 扫描与选优（采集 → 离线重放 → 报告）
"""
