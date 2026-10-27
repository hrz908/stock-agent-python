"""采集器注册表。按名字延迟导入，避免没装某个源的依赖时整包不可用。"""
_NAMES = {
    "cninfo": "巨潮资讯公告与定期报告",
    "em-report": "东方财富研报（个股/行业/策略/宏观）",
    "em-global": "东方财富全球财经快讯",
    "em-news": "东方财富个股新闻",
    "cls": "财联社电报",
    "ths": "同花顺 7×24 快讯",
    "gov-policy": "国务院政策文件库",
}


def build_source(name: str):
    if name == "cninfo":
        from ingest.ashare.sources.cninfo import CninfoAnnouncement
        return CninfoAnnouncement()
    if name == "em-report":
        from ingest.ashare.sources.eastmoney_report import EastmoneyReport
        return EastmoneyReport()
    if name == "em-global":
        from ingest.ashare.sources.eastmoney_news import EastmoneyGlobalNews
        return EastmoneyGlobalNews()
    if name == "em-news":
        from ingest.ashare.sources.eastmoney_news import EastmoneyStockNews
        return EastmoneyStockNews()
    if name == "cls":
        from ingest.ashare.sources.cls_telegraph import ClsTelegraph
        return ClsTelegraph()
    if name == "ths":
        from ingest.ashare.sources.ths_news import ThsGlobalNews
        return ThsGlobalNews()
    if name == "gov-policy":
        from ingest.ashare.sources.gov_policy import GovPolicy
        return GovPolicy()
    raise ValueError(f"未知数据源：{name}（可选：{', '.join(_NAMES)}）")


def source_names() -> list[str]:
    return list(_NAMES)
