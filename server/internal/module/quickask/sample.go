package quickask

import (
	"context"
	"encoding/json"
	"log"
	"math/rand"
	"strconv"
	"time"

	"stock-agent-server/internal/deps"
)

const (
	seenTTL = 30 * time.Minute
	lastTTL = 30 * time.Minute

	// maxPerSector 单次结果同板块上限：热度榜天然被题材股占据，
	// 不设约束时四条建议可能全是同一个板块，看起来像刷出来的
	maxPerSector = 2
)

// sampleDiverse 热度加权抽样 + 板块多样性软约束。
// 抽样不放回：同一批问句里重复出现同一只股票没有意义。
func sampleDiverse(pool []Candidate, k, sectorLimit int) []Candidate {
	if k <= 0 || len(pool) == 0 {
		return nil
	}
	picked := make([]Candidate, 0, k)
	rest := append([]Candidate(nil), pool...)
	sectorCount := make(map[string]int)

	for len(picked) < k && len(rest) > 0 {
		batch := weightedSample(rest, k-len(picked))
		if len(batch) == 0 {
			break
		}
		for _, c := range batch {
			rest = removeCode(rest, c.Code)
		}
		for _, c := range batch {
			if len(picked) >= k {
				break
			}
			// 板块扎堆的直接丢掉，下一轮会从剩余池里再抽
			if c.Sector != "" && sectorCount[c.Sector] >= sectorLimit {
				continue
			}
			if c.Sector != "" {
				sectorCount[c.Sector]++
			}
			picked = append(picked, c)
		}
	}

	// 多样性是软约束：热门股全挤在少数板块时宁可重复板块，也不能少给一条
	if len(picked) < k {
		for _, c := range weightedSample(pool, len(pool)) {
			if len(picked) >= k {
				break
			}
			if containsCode(picked, c.Code) {
				continue
			}
			picked = append(picked, c)
		}
	}
	return picked
}

// weightedSample 轮盘赌：按热度权重大小抽 k 支，抽中即移出池子。
// 全池权重都为 0 时退化为均匀抽样，保证极端数据下仍有结果。
func weightedSample(pool []Candidate, k int) []Candidate {
	if k <= 0 || len(pool) == 0 {
		return nil
	}
	rest := append([]Candidate(nil), pool...)
	picked := make([]Candidate, 0, k)

	total := 0.0
	for _, c := range rest {
		total += weightOf(c)
	}

	for len(picked) < k && len(rest) > 0 && total > 0 {
		r := rand.Float64() * total
		acc := 0.0
		idx := len(rest) - 1
		for i, c := range rest {
			acc += weightOf(c)
			if acc >= r {
				idx = i
				break
			}
		}
		c := rest[idx]
		picked = append(picked, c)
		total -= weightOf(c)
		rest = append(rest[:idx], rest[idx+1:]...)
	}

	for len(picked) < k && len(rest) > 0 {
		i := rand.Intn(len(rest))
		picked = append(picked, rest[i])
		rest = append(rest[:i], rest[i+1:]...)
	}
	return picked
}

// weightOf 给热度分加一个底：0 分的股票（停牌、缺量额）如果权重是 0，
// 就永远抽不到，池子会长期只剩头部那几只。
func weightOf(c Candidate) float64 {
	if c.Score <= 0 {
		return 1
	}
	return c.Score + 1
}

func removeCode(list []Candidate, code string) []Candidate {
	for i, c := range list {
		if c.Code == code {
			return append(list[:i], list[i+1:]...)
		}
	}
	return list
}

func containsCode(list []Candidate, code string) bool {
	for _, c := range list {
		if c.Code == code {
			return true
		}
	}
	return false
}

// ---------- 会话去重 ----------

func seenKey(uid int64, sid string) string {
	return "quickask:seen:" + strconv.FormatInt(uid, 10) + ":" + sid
}

func lastKey(uid int64, sid string) string {
	return "quickask:last:" + strconv.FormatInt(uid, 10) + ":" + sid
}

func readSeen(ctx context.Context, uid int64, sid string) map[string]bool {
	out := make(map[string]bool)
	if deps.RDB == nil {
		return out
	}
	members, err := deps.RDB.SMembers(ctx, seenKey(uid, sid)).Result()
	if err != nil {
		log.Printf("read quick ask seen failed: %v", err)
		return out
	}
	for _, m := range members {
		out[m] = true
	}
	return out
}

func markSeen(ctx context.Context, uid int64, sid string, codes []string) {
	if deps.RDB == nil || len(codes) == 0 {
		return
	}
	key := seenKey(uid, sid)
	pipe := deps.RDB.Pipeline()
	for _, code := range codes {
		pipe.SAdd(ctx, key, code)
	}
	pipe.Expire(ctx, key, seenTTL)
	if _, err := pipe.Exec(ctx); err != nil {
		log.Printf("mark quick ask seen failed: %v", err)
	}
}

func readLast(ctx context.Context, uid int64, sid string) map[string]bool {
	out := make(map[string]bool)
	if deps.RDB == nil {
		return out
	}
	b, err := deps.RDB.Get(ctx, lastKey(uid, sid)).Bytes()
	if err != nil || len(b) == 0 {
		return out
	}
	var codes []string
	if err := json.Unmarshal(b, &codes); err != nil {
		return out
	}
	for _, c := range codes {
		out[c] = true
	}
	return out
}

func saveLast(ctx context.Context, uid int64, sid string, codes []string) {
	if deps.RDB == nil || len(codes) == 0 {
		return
	}
	b, err := json.Marshal(codes)
	if err != nil {
		return
	}
	if err := deps.RDB.Set(ctx, lastKey(uid, sid), b, lastTTL).Err(); err != nil {
		log.Printf("save quick ask last failed: %v", err)
	}
}

func clearSeen(ctx context.Context, uid int64, sid string) {
	if deps.RDB == nil {
		return
	}
	if err := deps.RDB.Del(ctx, seenKey(uid, sid)).Err(); err != nil {
		log.Printf("clear quick ask seen failed: %v", err)
	}
}

// pickForSession 三层去重：会话已见 → 相邻刷新 → 板块多样性。
// 任何一层都不能把候选池抽干，宁可放宽也要凑够条数。
func pickForSession(pool []Candidate, count int, seen, last map[string]bool) []Candidate {
	candidates := excludeCodes(pool, seen)
	if len(candidates) < count {
		// 池子快被看完了：去重是为了新鲜感，不是为了把池子抽干
		candidates = pool
	}

	if len(candidates) > count {
		// 相邻刷新至少换 2 支：先排除上一轮，剩余够数才采用
		if fresh := excludeCodes(candidates, last); len(fresh) >= count {
			candidates = fresh
		}
	}

	return sampleDiverse(candidates, count, maxPerSector)
}

// countUnseen 池内未被该会话见过的条数：只用于判阈值，不物化新切片
func countUnseen(pool []Candidate, skip map[string]bool) int {
	if len(skip) == 0 {
		return len(pool)
	}
	n := 0
	for _, c := range pool {
		if !skip[c.Code] {
			n++
		}
	}
	return n
}

func excludeCodes(pool []Candidate, skip map[string]bool) []Candidate {
	if len(skip) == 0 {
		return pool
	}
	out := make([]Candidate, 0, len(pool))
	for _, c := range pool {
		if !skip[c.Code] {
			out = append(out, c)
		}
	}
	return out
}
