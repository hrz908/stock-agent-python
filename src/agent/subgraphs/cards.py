"""前端卡片载荷的 pydantic 校验层（双模式输出的「结构化闸门」）。

- 结构化链路：载荷先过本模块校验，通过才发 card/table 事件；
- 降级链路：校验失败只丢弃那张卡并记日志，正文由 synthesize 的文本兜底，用户永远能拿到答案。

模型刻意宽松（extra=allow、业务字段可选）：数据源（东财/新浪/baostock）字段偶有缺失，
这里只拦结构性错误（缺标识字段、行结构不对、空载荷），不做数值审查——
数值口径由工具层归一（tools/market 的 num/text），前端契约见 stock 子图 _emit_structured。
"""
from typing import Any

from pydantic import BaseModel, Field, model_validator


class _Base(BaseModel):
    model_config = {"extra": "allow"}


def _filled(v) -> bool:
    """宽松判「字段有值」：None / 空串都算缺。"""
    if v is None:
        return False
    return bool(str(v).strip())


class QuoteCard(_Base):
    """行情速览卡（get_quote -> Go 网关当日快照行）。"""

    code: Any | None = Field(default=None, description="标的代码")
    name: Any | None = Field(default=None, description="标的名称")
    price: Any | None = Field(default=None, description="最新价")
    change_percent: Any | None = Field(default=None, description="涨跌幅")

    @model_validator(mode="after")
    def _check(self):
        if not _filled(self.code) and not _filled(self.name):
            raise ValueError("行情卡缺少标的标识（code/name 均为空）")
        if self.price is None and self.change_percent is None:
            raise ValueError("行情卡缺少价格与涨跌幅")
        return self


class ValuationCard(_Base):
    """估值卡（get_valuation 单日行）。"""

    ts_code: Any | None = None
    trade_date: Any | None = None

    @model_validator(mode="after")
    def _check(self):
        if not _filled(self.ts_code) or not _filled(self.trade_date):
            raise ValueError("估值卡缺少 ts_code 或 trade_date")
        return self


class FundflowCard(_Base):
    """资金流向卡（get_money_flow 行换算，可并入龙虎榜区块；也承载纯龙虎榜卡）。"""

    ts_code: Any | None = None
    lhb: Any | None = None

    @model_validator(mode="after")
    def _check(self):
        has_lhb = isinstance(self.lhb, list) and bool(self.lhb)
        if not _filled(self.ts_code) and not has_lhb:
            raise ValueError("资金流卡缺少标的代码，且无龙虎榜区块")
        return self


class ReportCard(_Base):
    """财报速览卡（get_financial_report 组合载荷：指标 + 利润表 + 现金流量表）。"""

    indicator: Any | None = None
    income: Any | None = None
    cashflow: Any | None = None

    @model_validator(mode="after")
    def _check(self):
        if self.indicator is None and self.income is None and self.cashflow is None:
            raise ValueError("财报卡三张表全为空")
        return self


class CompareCard(_Base):
    """同业对比卡（get_peer_compare，本股 + 最多 2 家同行）。"""

    items: Any | None = None

    @model_validator(mode="after")
    def _check(self):
        if not isinstance(self.items, list) or not self.items:
            raise ValueError("对比卡 items 为空")
        return self


class ResearchCard(_Base):
    """研报观点卡（get_research_reports 单行：机构 / 评级 / 报告名称 / 原文链接）。

    东财研报没有目标价与盈利预测调整方向：这两个字段留空由前端降级显示「--」，
    因此校验只拦「机构与摘要全空」这类无法成卡的行。
    """

    org: Any | None = None
    summary: Any | None = None

    @model_validator(mode="after")
    def _check(self):
        if not _filled(self.org) and not _filled(self.summary):
            raise ValueError("研报卡缺少机构与摘要")
        return self


class EventCard(_Base):
    """公司事件卡（get_stock_news 折成的事件行，一条一条进时间轴）。"""

    events: Any | None = None

    @model_validator(mode="after")
    def _check(self):
        if not isinstance(self.events, list) or not self.events:
            raise ValueError("事件卡 events 为空")
        return self


def _validate_symbol_list(payload) -> None:
    """股票搜索候选表：非空列表，每行有 code。"""
    if not isinstance(payload, list) or not payload:
        raise ValueError("搜索候选为空")
    for row in payload:
        if not isinstance(row, dict) or not _filled(row.get("code")):
            raise ValueError("搜索候选行缺少 code")


def _validate_kline_table(payload) -> None:
    """K 线表：非空 rows，每行有日期（开高低收由工具层保证）。"""
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError("K 线表缺少行数据")
    for row in rows:
        if not isinstance(row, dict) or not _filled(row.get("date")):
            raise ValueError("K 线行缺少日期")


_CARD_VALIDATORS = {
    "quote_card": QuoteCard,
    "valuation_card": ValuationCard,
    "fundflow_card": FundflowCard,
    "report_card": ReportCard,
    "compare_card": CompareCard,
    "research_card": ResearchCard,
    "event_card": EventCard,
}

_TABLE_VALIDATORS = {
    "symbol_list": _validate_symbol_list,
    "kline_table": _validate_kline_table,
}


def validate_card(card_type: str, payload) -> bool:
    """校验 card 载荷；未登记的类型按「非空 dict」宽松放行。"""
    model = _CARD_VALIDATORS.get(card_type)
    if model is None:
        return isinstance(payload, dict) and bool(payload)
    try:
        model.model_validate(payload)
        return True
    except Exception:  # noqa: BLE001 - 校验失败统一按 False 处理
        return False


def validate_table(table_type: str, payload) -> bool:
    """校验 table 载荷；未登记的类型按「非空 dict」宽松放行。"""
    fn = _TABLE_VALIDATORS.get(table_type)
    if fn is None:
        return isinstance(payload, dict) and bool(payload)
    try:
        fn(payload)
        return True
    except Exception:  # noqa: BLE001
        return False
