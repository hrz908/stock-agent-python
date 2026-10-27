-- v_stock_profile 不再绑定 std='em'
--
-- 行业数据的主源是东财（std='em'），但东财 push2 会整段拒绝云主机 IP，
-- 那时 ashare 退到申万（std='sw'，概念则退新浪 std/source='sina'）。
-- 视图如果写死 em，换源后 profile 就整片空掉。
-- 这里按「东财优先、否则取最新一条」选一行，只改视图不动表，可重复执行。
--
-- 注意：两套行业词表不能共存于同一条 l1，ashare 换源成功后会删掉另一源的行；
-- 这里的 LATERAL 只是兜住「清理被跳过」时的残留。

CREATE OR REPLACE VIEW v_stock_profile AS
SELECT b.code,
       b.market,
       b.code || '.' || b.market AS ts_code,
       b.market || b.code        AS gw_code,
       b.name,
       b.board,
       b.status,
       b.is_st,
       b.pinyin_abbr,
       b.list_date,
       b.delist_date,
       i.l1 AS industry_l1,
       i.l2 AS industry_l2,
       i.l3 AS industry_l3,
       COALESCE(c.concepts, ARRAY[]::text[]) AS concepts
FROM stock_basic b
LEFT JOIN LATERAL (
  SELECT l1, l2, l3
  FROM stock_industry
  WHERE code = b.code
  ORDER BY (std = 'em') DESC, updated_at DESC
  LIMIT 1
) i ON TRUE
LEFT JOIN LATERAL (
  SELECT array_agg(sc.concept ORDER BY sc.concept) AS concepts
  FROM stock_concept sc
  WHERE sc.code = b.code
) c ON TRUE;
