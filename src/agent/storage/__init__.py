"""agent 侧的数据访问层：所有 psycopg 连接池都从这里取。

分成两个池而不是一个，是因为用途和故障边界不同：
- `read`：主数据只读（stock_basic / v_stock_profile / stock_industry），
  连接带 `default_transaction_read_only=on`，只服务代码里写死的查询 SQL；
- `app`：应用表读写（chat_sessions / agent_runs）。

checkpointer 的池不在这里，见 `core/checkpointer.py`：它只服务 LangGraph saver，
与会话查询分开是为了避免 checkpoint 写入把查询连接的槽位占满。
"""
