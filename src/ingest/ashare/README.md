# ashare：A股文本语料入库

设计与取舍见 [`docs/ASHARE_VECTORIZATION.md`](../../docs/ASHARE_VECTORIZATION.md)。

## 最小用法

```bash
cd ingest
pip install -r requirements.txt
pip install -r requirements-embed.txt          # 本地向量化；用 API 则不用装
python -m ingest.ashare.cli init-db                  # 执行 007 迁移
python -m ingest.ashare.cli init-db --file 008_stock_master.sql --file 009_stock_finance_indicator.sql
python -m ingest.ashare.cli sync-master              # 股票清单/行业/概念（迁移 008 的三张表）
python -m ingest.ashare.cli collect --source cls --dry-run
python -m ingest.ashare.cli collect --source cninfo --since 2026-10-01
python -m ingest.ashare.cli stats
python -m ingest.ashare.cli storage
python -m ingest.ashare.cli search "贵州茅台 回购" --code 600519
```

主数据同步（`sync-master`）与文本入库是两条线：只写 `stock_basic` / `stock_industry` /
`stock_concept`，不做向量化。首次全量约 5 分钟（概念板块 400+ 个，按 2 QPS 串行）；
`--only basic` 可只刷清单，`--concept-limit 50` 可先刷前 50 个概念。

## 数据流

`source.fetch()` 产出 `RawDoc` → `pipeline._prepare()`（取 PDF 正文、清洗、门禁、去重）
→ `chunk_document()`（中文分块 + 实体上下文头）→ `Embedder`（本地模型）→
`Store.upsert_document()`（单事务写 `documents` / `chunks` / `ashare_documents`）。

## 目录

| 文件 | 职责 |
|---|---|
| `config.py` | 环境变量配置，默认值即可跑本地 |
| `types.py` | `RawDoc` / `Chunk` / `RunStats` |
| `clean.py` | HTML/PDF 清洗、报表章节剔除、政策与公告类型识别 |
| `quality.py` | 质量分、指纹、SimHash 近重复 |
| `chunking.py` | 中文分块 |
| `embedder.py` | sentence-transformers / FlagEmbedding / OpenAI 兼容 API |
| `store.py` | pgvector 写入、水位、统计、重嵌、检索自检 |
| `master.py` | A股主数据同步（股票清单/行业/概念），写 008 的三张表 |
| `pipeline.py` | 编排与 spool |
| `sources/` | 巨潮、东财研报、财联社、同花顺、国务院政策文件库、东财快讯/个股新闻 |
| `cli.py` | 命令行入口 |

## 注意

- `ASHARE_EMBED_DIM`、`chunks.embedding` 维度、agent 的 `EMBED_DIM` 三处必须一致；
- 换模型后先 `reembed`，再确认 `documents.embed_model` 全部切换；
- 水位在小比例失败时照常推进，靠重叠窗口重试；只有大面积失败或源级异常才停；
- 个股新闻需显式 `--codes` 或 `ASHARE_NEWS_CODES`，否则只跑市场级快讯。
