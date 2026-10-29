"""Quick Ask 的数据结构。

Candidate 里 Amount / Turnover / Heat 只参与打分、不写 Redis：
池子的读取方（抽样 + 模板）只需要名称、板块、涨跌幅和量比。

corpus 参与写 Redis：模板要避开「问了就吃闭门羹」的问句，而抽样大多读的是 10 分钟前
写好的池子，标记跟着候选走一次查询才划算（每次请求重查一遍语料库不现实）。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class Candidate:
    code: str  # 带市场前缀：SH600519
    name: str = ""
    sector: str = ""
    change_pct: float = 0.0
    volume_ratio: float = 0.0
    score: float = 0.0
    amount: float = 0.0
    turnover: float = 0.0
    heat: float = 0.0
    corpus: bool = True  # 有没有已入库的文本语料；False 时模板只挑能靠取数答的问句

    def encode(self) -> str:
        """Redis 成员 6 段格式：code|name|sector|changePct|volumeRatio|corpus。

        名称与板块由主数据保证不含竖线；解码用 split("|")，缺第 6 段的旧成员
        按「有语料」处理。
        """
        return "|".join(
            [
                self.code,
                self.name,
                self.sector,
                f"{self.change_pct:.2f}",
                f"{self.volume_ratio:.2f}",
                "1" if self.corpus else "0",
            ]
        )

    @classmethod
    def decode(cls, member: str, score: float) -> Candidate | None:
        parts = member.split("|")
        if len(parts) < 5 or not parts[0] or not parts[1]:
            return None
        try:
            change_pct = float(parts[3])
        except (TypeError, ValueError):
            return None
        try:
            volume_ratio = float(parts[4])
        except (TypeError, ValueError):
            volume_ratio = 0.0
        return cls(
            code=parts[0],
            name=parts[1],
            sector=parts[2],
            change_pct=change_pct,
            volume_ratio=volume_ratio,
            score=score,
            corpus=len(parts) < 6 or parts[5] != "0",
        )


@dataclass(frozen=True, slots=True)
class Template:
    """一条问句模板。

    display 是前端展示的一句话（短，像随口一问）；
    ask 是真正发给 Agent 的完整问题（带代码与维度）。

    needs_corpus=False 表示这条问句靠实时取数就能答（行情 / 资金 / 财务指标口径）：
    标的没有文本语料时也可以问，出发前已经确认它的 ask 会被判定为「要取数」，
    不会撞上「不在文本语料范围内」的话术。True 的模板只对有语料的标的出。
    """

    tag: str
    display: str
    ask: str
    needs_corpus: bool = True


@dataclass(slots=True)
class Ask:
    id: str = ""
    display: str = ""
    query: str = ""
    stock_code: str = ""
    stock_name: str = ""
    tag: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "display": self.display,
            "query": self.query,
            "stock_code": self.stock_code,
            "stock_name": self.stock_name,
            "tag": self.tag,
        }


@dataclass(slots=True)
class MarketMeta:
    code: str
    name: str
    sector: str
    list_date: str
    status: str
    is_st: bool = field(default=False)
