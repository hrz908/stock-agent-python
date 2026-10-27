"""Quick Ask 的数据结构。

Candidate 里 Amount / Turnover / Heat 只参与打分、不写 Redis：
池子的读取方（抽样 + 模板）只需要名称、板块、涨跌幅和量比。
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

    def encode(self) -> str:
        """Redis 成员 5 段格式：code|name|sector|changePct|volumeRatio。

        名称与板块由主数据保证不含竖线，解码用 split("|", 4) 就够，不必上 JSON。
        """
        return "|".join(
            [
                self.code,
                self.name,
                self.sector,
                f"{self.change_pct:.2f}",
                f"{self.volume_ratio:.2f}",
            ]
        )

    @classmethod
    def decode(cls, member: str, score: float) -> Candidate | None:
        parts = member.split("|", 4)
        if len(parts) != 5 or not parts[0] or not parts[1]:
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
        )


@dataclass(frozen=True, slots=True)
class Template:
    """一条问句模板。

    display 是前端展示的一句话（短，像随口一问）；
    ask 是真正发给 Agent 的完整问题（带代码与维度）。
    """

    tag: str
    display: str
    ask: str


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
