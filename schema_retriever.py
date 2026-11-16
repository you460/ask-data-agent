# -*- coding: utf-8 -*-
"""P1：结构化元数据检索（Schema Retrieval）。

解决的问题：现有 metadata_tools.search_table 只是对"表名+字段名+描述"做子串匹配，
一次返回整表全部字段，既没有排序也没有 join 关系利用；实际使用中只能靠模型自己
猜关键词，猜偏就检索不到表。

本模块提供三档检索，便于做消融：
  legacy     —— 近似现有 search_table 的子串匹配（按命中词数排序），仅作基线
  bm25       —— 中文 bigram + 英文单词的 BM25，字段级打分后聚合到表级
  bm25_join  —— 在 bm25 基础上按 metadata 里的 11 条关系做最短路补全 join 路径，
                并只保留相关字段（含主键/分区键/关联键），用于压缩提示词

不依赖任何外部服务；枚举值可选地从数据库读取（读不到自动降级）。
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

META_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'metadata', 'metadata.json')

# 这些字段每张表都有，命中它们说明不了业务意图，单独降权
GENERIC_COLUMNS = {'pty_id', 'data_dt', 'prdt_id', 'sys_source', 'ccy', 'org_id'}

# 维度词 → 需要补的枚举字段，命中即把 dim_public 带上（schema linking）
DIMENSION_HINTS = {
    '客户等级': 'cust_lvl_cd', '等级': 'cust_lvl_cd',
    '学历': 'edu_cd', '性别': 'gender_cd', '职业': 'prof_cd', '账户状态': 'cust_status',
}
# 产品相关词 → 需要把 dim_product 带上
PRODUCT_HINTS = ('产品', '债券', '股票', '基金', '理财', 'ETF', '科创板', '沪港通',
                 '深港通', 'A股', '创业板', '新三板', '期权', '私募', '贵金属')
# 业务语义 → 必须召回的表：这类词在表名/字段名里没有同义写法，BM25 召不回来
TABLE_HINTS = {
    'dws_cust_fin_d': ('盈亏', '资金流水', '资金流入', '资金流出', '银证转账',
                       '划转', '入金', '出金', '转入', '转出'),
}
# 低基数文本维度字段：采样真实取值，避免把"江苏"写成库里不存在的字面量
SAMPLE_COLUMNS = {'prov_name', 'city_name', 'prdt_name', 'prdt_type_name',
                  'up_prdt_type_name', 'org_name', 'up_org_name'}
SAMPLE_LIMIT = 8


# ---------------------------------------------------------------- 文本处理

def tokenize(text: str) -> List[str]:
    """英文/数字按下划线切词，中文同时产出单字与 bigram（无需分词库）。"""
    if not text:
        return []
    text = text.lower()
    tokens = re.findall(r'[a-z0-9_]+', text)
    for run in re.findall(r'[\u4e00-\u9fff]+', text):
        tokens.extend(run)
        tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
    return tokens


def char_bigrams(text: str) -> Set[str]:
    """用于查重：只取中文 bigram 集合。"""
    s = re.sub(r'[^\u4e00-\u9fff0-9a-z]', '', (text or '').lower())
    return {s[i:i + 2] for i in range(len(s) - 1)}


class BM25:
    """极简 BM25，语料是小规模元数据，纯内存即可。"""

    def __init__(self, docs: Dict[str, List[str]], k1: float = 1.2, b: float = 0.75):
        self.k1, self.b = k1, b
        self.doc_tokens = docs
        self.doc_len = {k: len(v) for k, v in docs.items()}
        self.avgdl = (sum(self.doc_len.values()) / len(docs)) if docs else 1.0
        self.tf: Dict[str, Dict[str, int]] = {}
        self.df: Dict[str, int] = {}
        for doc_id, toks in docs.items():
            counter: Dict[str, int] = {}
            for t in toks:
                counter[t] = counter.get(t, 0) + 1
            self.tf[doc_id] = counter
            for t in counter:
                self.df[t] = self.df.get(t, 0) + 1
        self.n = len(docs)

    def idf(self, term: str) -> float:
        df = self.df.get(term, 0)
        return math.log(1 + (self.n - df + 0.5) / (df + 0.5))

    def score(self, query_tokens: List[str]) -> Dict[str, float]:
        scores = {doc_id: 0.0 for doc_id in self.doc_tokens}
        for term in set(query_tokens):
            idf = self.idf(term)
            for doc_id, counter in self.tf.items():
                f = counter.get(term)
                if not f:
                    continue
                denom = f + self.k1 * (1 - self.b + self.b * self.doc_len[doc_id] / self.avgdl)
                scores[doc_id] += idf * f * (self.k1 + 1) / denom
        return scores


# ---------------------------------------------------------------- 结果结构

@dataclass
class RetrievalResult:
    tables: List[str]
    columns: Dict[str, List[str]]
    joins: List[str]
    metrics: List[str]
    scores: Dict[str, float] = field(default_factory=dict)
    expanded_tables: List[str] = field(default_factory=list)

    def render(self) -> str:
        lines = ['【候选表】']
        for t in self.tables:
            cols = self.columns.get(t, [])
            lines.append(f'- {t}: ' + (', '.join(cols) if cols else '(无匹配字段)'))
        if self.joins:
            lines.append('【可用关联】')
            lines.extend(f'- {j}' for j in self.joins)
        if self.metrics:
            lines.append('【相关指标】')
            lines.extend(f'- {m}' for m in self.metrics)
        return '\n'.join(lines)


# ---------------------------------------------------------------- 检索器

class SchemaRetriever:
    def __init__(self, metadata_path: str = META_PATH, enums: Optional[Dict[str, Dict[str, str]]] = None):
        with open(metadata_path, 'r', encoding='utf-8') as f:
            self.meta = json.load(f)
        self.tables = {t['table_name']: t for t in self.meta['tables']}
        self.relationships = self.meta.get('table_relationships', [])
        self.metrics = self.meta.get('business_metrics', [])
        self.enums = enums if enums is not None else self._try_load_enums()
        self._build_index()
        self._sample_cache: Dict[Tuple[str, str], List[str]] = {}

    def sample_values(self, table_name: str, column_name: str,
                      limit: int = SAMPLE_LIMIT) -> List[str]:
        """采样文本维度字段的真实取值，供提示词做值域接地。"""
        key = (table_name, column_name)
        if key in self._sample_cache:
            return self._sample_cache[key]
        values: List[str] = []
        try:
            import psycopg2
            from config import DB_CONFIG
            with psycopg2.connect(**DB_CONFIG) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"select distinct {column_name} from {table_name} "
                        f"where {column_name} is not null limit %s", (limit,)
                    )
                    values = [str(row[0]) for row in cur.fetchall()]
        except Exception as error:
            print(f"字段取值采样失败 {table_name}.{column_name}: {str(error)[:60]}")
        self._sample_cache[key] = values
        return values

    # -- 索引 ----------------------------------------------------------
    @staticmethod
    def _try_load_enums() -> Dict[str, Dict[str, str]]:
        """优先复用项目里已有的枚举加载逻辑；数据库不可用时静默降级。"""
        try:
            from metadata_tools import _load_enum_cache
            return _load_enum_cache() or {}
        except Exception:
            return {}

    def _enum_text(self, col: dict) -> str:
        if not col.get('is_enum'):
            return ''
        if col.get('enum_source') == 'inline':
            return ' '.join(str(v.get('desc', '')) for v in col.get('enum_values', []))
        type_id = col.get('enum_type_id', '')
        return ' '.join(self.enums.get(type_id, {}).values())

    def _build_index(self):
        self.col_to_table: Dict[str, str] = {}
        col_docs: Dict[str, List[str]] = {}
        table_docs: Dict[str, List[str]] = {}
        self.col_meta: Dict[str, dict] = {}

        for tname, table in self.tables.items():
            table_text = ' '.join(filter(None, [
                tname,
                table.get('table_cn_name', ''),
                table.get('table_description', ''),
                table.get('table_alias', ''),
                ' '.join(c.get('column_cn_name', '') for c in table.get('columns', [])),
            ]))
            for col in table.get('columns', []):
                cid = f"{tname}.{col['column_name']}"
                self.col_to_table[cid] = tname
                self.col_meta[cid] = col
                col_text = ' '.join(filter(None, [
                    col.get('column_name', ''),
                    col.get('column_cn_name', ''),
                    col.get('column_description', ''),
                    self._enum_text(col),
                ]))
                col_docs[cid] = tokenize(col_text)
            table_docs[tname] = tokenize(table_text)

        self.col_bm25 = BM25(col_docs)
        self.table_bm25 = BM25(table_docs)
        self.metric_docs = {m.get('metric_name', str(i)): tokenize(' '.join(filter(None, [
            m.get('metric_name', ''), m.get('metric_desc', ''), m.get('metric_formula', '')])))
            for i, m in enumerate(self.metrics)}
        self.metric_bm25 = BM25(self.metric_docs) if self.metric_docs else None

        # 表关系图
        self.graph: Dict[str, List[Tuple[str, str, str]]] = {}
        for rel in self.relationships:
            a, b = rel['from_table'], rel['to_table']
            edge = (rel['from_column'], rel['to_column'], rel.get('relation_desc', ''))
            self.graph.setdefault(a, []).append((b,) + edge)
            self.graph.setdefault(b, []).append((a, edge[1], edge[0], edge[2]))

    # -- 检索 ----------------------------------------------------------
    def _score_columns(self, question: str) -> Tuple[Dict[str, float], List[str]]:
        q_tokens = tokenize(question)
        bm25 = self.col_bm25.score(q_tokens)
        scores = dict(bm25)
        lowered = question.lower()
        for cid, col in self.col_meta.items():
            if col.get('column_name') in GENERIC_COLUMNS:
                scores[cid] = scores.get(cid, 0.0) * 0.25
            cn = (col.get('column_cn_name') or '').strip()
            if cn and len(cn) >= 2 and cn in question:
                scores[cid] = scores.get(cid, 0.0) + 3.0
            if col.get('column_name') and col['column_name'].lower() in lowered:
                scores[cid] = scores.get(cid, 0.0) + 1.5
            # 枚举中文直接命中（如"钻石卡""科创板""美元"）
            et = self._enum_text(col)
            for value in re.split(r'\s+', et):
                if value and len(value) >= 2 and value in question:
                    scores[cid] = scores.get(cid, 0.0) + 2.0
                    break
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        return scores, [cid for cid, s in ranked if s > 0]

    def _score_tables(self, question: str, col_scores: Dict[str, float]) -> Dict[str, float]:
        q_tokens = tokenize(question)
        table_bm25 = self.table_bm25.score(q_tokens)
        scores: Dict[str, float] = {}
        for tname in self.tables:
            col_max = max([s for cid, s in col_scores.items()
                           if self.col_to_table.get(cid) == tname] or [0.0])
            scores[tname] = 0.45 * col_max + 0.35 * table_bm25.get(tname, 0.0)
            table = self.tables[tname]
            for alias in filter(None, [table.get('table_cn_name'), table.get('table_alias')]):
                alias = alias.replace('表', '')
                if len(alias) >= 2 and alias in question:
                    scores[tname] += 0.6
        return scores

    def _companion_tables(self, selected: List[str], col_scores: Dict[str, float],
                          question: str) -> List[str]:
        """按"字段命中→维表"补全隐含依赖表。

        真实查询里，"各客户等级"要 join dim_public、"持有债券产品"要 join dim_product，
        但问题本身不会出现这两个表名。这里沿 join 边与枚举来源补齐，属于 schema linking。
        """
        extra: List[str] = []
        # 1) 维度词命中 → 编码字典表（dim_public）
        for keyword, column in DIMENSION_HINTS.items():
            if keyword not in question:
                continue
            for tname in selected:
                cols = {c['column_name'] for c in self.tables[tname].get('columns', [])}
                if column in cols and 'dim_public' not in selected and 'dim_public' not in extra:
                    extra.append('dim_public')
        # 2) 产品词命中 → 产品维表（dim_product）
        if any(k in question for k in PRODUCT_HINTS):
            for tname in selected:
                cols = {c['column_name'] for c in self.tables[tname].get('columns', [])}
                if 'prdt_id' in cols and 'dim_product' not in selected and 'dim_product' not in extra:
                    extra.append('dim_product')
        # 3) 强命中的关联键字段也补上目标维表
        for tname in selected:
            for col in self.tables[tname].get('columns', []):
                cid = f"{tname}.{col['column_name']}"
                target = col.get('related_table')
                if col_scores.get(cid, 0.0) >= 1.5 and col.get('is_join_key') and target:
                    if target in self.tables and target not in selected and target not in extra:
                        extra.append(target)
        return extra[:2]

    def _expand_joins(self, selected: List[str]) -> Tuple[List[str], List[str]]:
        """把选中的表用最短路连起来，返回（补全后的表集合, join 条件文本）。"""
        if not selected:
            return [], []
        ordered = list(selected)
        needed: Set[str] = set(selected)
        if len(selected) > 1:
            root = selected[0]
            for target in selected[1:]:
                path = self._bfs_path(root, target)
                if path:
                    needed.update(path)
        # 生成 join 边：只保留两端都在集合内的关系
        edges, seen = [], set()
        for rel in self.relationships:
            a, b = rel['from_table'], rel['to_table']
            if a in needed and b in needed:
                key = tuple(sorted((a, b)))
                if key in seen:
                    continue
                seen.add(key)
                edges.append(f"{a}.{rel['from_column']} = {b}.{rel['to_column']}"
                             + (f"  # {rel['relation_desc']}" if rel.get('relation_desc') else ''))
        expanded = [t for t in ordered if t in needed] + [t for t in needed if t not in ordered]
        return expanded, edges

    def _bfs_path(self, src: str, dst: str) -> Optional[List[str]]:
        if src == dst:
            return [src]
        prev: Dict[str, str] = {src: ''}
        queue = [src]
        while queue:
            cur = queue.pop(0)
            for nxt, *_ in self.graph.get(cur, []):
                if nxt in prev:
                    continue
                prev[nxt] = cur
                if nxt == dst:
                    path, node = [], nxt
                    while node:
                        path.append(node)
                        node = prev[node]
                    return list(reversed(path))
                queue.append(nxt)
        return None

    def retrieve(self, question: str, top_k_tables: int = 3, top_k_cols: int = 3,
                 expand_join: bool = True, min_score: float = 0.05) -> RetrievalResult:
        col_scores, ranked_cols = self._score_columns(question)
        table_scores = self._score_tables(question, col_scores)
        ranked_all = [t for t, s in sorted(table_scores.items(), key=lambda kv: kv[1], reverse=True)
                      if s > min_score]
        # 相对阈值：与最高分差距过大的表不进入上下文，避免固定塞满 top_k
        if ranked_all:
            top_score = table_scores[ranked_all[0]]
            ranked_tables = [t for t in ranked_all if table_scores[t] >= 0.45 * top_score][:top_k_tables]
        else:
            ranked_tables = []
        if not ranked_tables:  # 兜底：至少给客户主表
            ranked_tables = ['ads_cust_info_d']
        # 业务语义强召回：这类表名/字段名与问题用词没有字面对应，纯 BM25 进不了 top_k
        forced = [t for t, keywords in TABLE_HINTS.items()
                  if t in self.tables and any(k in question for k in keywords)]
        ranked_tables = ranked_tables + [t for t in forced if t not in ranked_tables]

        expanded, joins = ranked_tables, []
        if expand_join:
            companions = self._companion_tables(ranked_tables, col_scores, question)
            expanded, joins = self._expand_joins(ranked_tables + companions)

        # 字段裁剪：每表 top_k_cols + 主键 + 分区键 + 关联键
        columns: Dict[str, List[str]] = {}
        for tname in expanded:
            table = self.tables[tname]
            keep: List[str] = []
            cols_sorted = sorted(
                [c for c in table.get('columns', [])],
                key=lambda c: col_scores.get(f"{tname}.{c['column_name']}", 0.0), reverse=True)
            for c in cols_sorted[:top_k_cols]:
                if col_scores.get(f"{tname}.{c['column_name']}", 0.0) > 0 or len(keep) < top_k_cols:
                    keep.append(c['column_name'])
            musts: List = [table.get('primary_key'), table.get('partition_key')]
            musts += [c['column_name'] for c in table.get('columns', []) if c.get('is_join_key')]
            for must in musts:
                items = must if isinstance(must, list) else [must]
                for item in items:
                    name = item if isinstance(item, str) else (item or {}).get('column_name')
                    if name and name not in keep:
                        keep.append(name)
            columns[tname] = keep

        metrics: List[str] = []
        if self.metric_bm25:
            ms = self.metric_bm25.score(tokenize(question))
            top = sorted(ms.items(), key=lambda kv: kv[1], reverse=True)[:2]
            metrics = [f"{name} = {self._metric_formula(name)}" for name, s in top if s > 0]

        return RetrievalResult(tables=expanded, columns=columns, joins=joins,
                               metrics=metrics, scores=table_scores,
                               expanded_tables=[t for t in expanded if t not in ranked_tables])

    def _metric_formula(self, name: str) -> str:
        for m in self.metrics:
            if m.get('metric_name') == name:
                return m.get('metric_formula', '')
        return ''

    # -- 基线：近似现有 search_table ----------------------------------
    def legacy_retrieve(self, question: str, top_k_tables: int = 3) -> RetrievalResult:
        grams = [g for g in char_bigrams(question) if len(g) == 2]
        scored: Dict[str, float] = {}
        for tname, table in self.tables.items():
            text = ' '.join(filter(None, [table.get('table_cn_name', ''), table.get('table_description', '')]))
            text += ''.join((c.get('column_name', '') + c.get('column_cn_name', '') + c.get('column_description', ''))
                            for c in table.get('columns', []))
            scored[tname] = float(sum(1 for g in grams if g in text))
        ranked = [t for t, s in sorted(scored.items(), key=lambda kv: kv[1], reverse=True) if s > 0][:top_k_tables]
        if not ranked:
            ranked = ['ads_cust_info_d']
        columns = {t: [c['column_name'] for c in self.tables[t].get('columns', [])] for t in ranked}
        return RetrievalResult(tables=ranked, columns=columns, joins=[], metrics=[], scores=scored)


# ---------------------------------------------------------------- 评测工具

def tables_in_sql(sql: str) -> Set[str]:
    """从基准 SQL 中抽出真实表名（排除 CTE 别名）。"""
    try:
        import sqlglot
        from sqlglot import exp
        parsed = sqlglot.parse_one(sql, read='postgres')
        ctes = {c.alias for c in parsed.find_all(exp.CTE) if c.alias}
        return {t.name for t in parsed.find_all(exp.Table) if t.name and t.name not in ctes}
    except Exception:
        return set(re.findall(r'(?:from|join)\s+([a-z_][a-z0-9_]*)', sql, re.I))


def benchmark(questions: List[dict], retriever: SchemaRetriever) -> dict:
    modes = ['legacy', 'bm25', 'bm25_join']
    stats = {m: {'recall': [], 'precision': [], 'full': [], 'tables': [], 'chars': [], 'ms': []}
             for m in modes}
    for item in questions:
        question, sql = item['question'], item['standard_sql']
        truth = tables_in_sql(sql)
        if not truth:
            continue
        for mode in modes:
            t0 = time.perf_counter()
            if mode == 'legacy':
                res = retriever.legacy_retrieve(question)
            else:
                res = retriever.retrieve(question, expand_join=(mode == 'bm25_join'))
            ms = (time.perf_counter() - t0) * 1000
            pred = set(res.tables)
            hit = truth & pred
            stats[mode]['recall'].append(len(hit) / len(truth))
            stats[mode]['precision'].append(len(hit) / len(pred) if pred else 0.0)
            stats[mode]['full'].append(1.0 if truth <= pred else 0.0)
            stats[mode]['tables'].append(len(pred))
            stats[mode]['chars'].append(len(res.render()))
            stats[mode]['ms'].append(ms)

    def agg(vals):
        return round(sum(vals) / len(vals), 4) if vals else 0.0

    summary = {}
    for mode in modes:
        s = stats[mode]
        summary[mode] = {
            'table_recall': agg(s['recall']),
            'table_precision': agg(s['precision']),
            'full_set_hit': agg(s['full']),
            'avg_tables': agg(s['tables']),
            'avg_render_chars': int(agg(s['chars'])),
            'avg_ms': round(agg(s['ms']), 2),
            'n': len(s['recall']),
        }
    return summary


def main():
    import argparse
    ap = argparse.ArgumentParser(description='元数据检索（P1）')
    ap.add_argument('--question', help='单条问题，打印检索结果')
    ap.add_argument('--bench', action='store_true', help='在 150 题上对比三种检索方式的表召回')
    ap.add_argument('--expanded', action='store_true', help='单条问题时展示 join 扩展')
    args = ap.parse_args()

    retriever = SchemaRetriever()

    if args.question:
        res = retriever.retrieve(args.question, expand_join=args.expanded)
        print(res.render())
        print('\n表级得分:', {k: round(v, 3) for k, v in sorted(res.scores.items(), key=lambda kv: -kv[1])[:4]})
        print('join 补全新增:', res.expanded_tables)
        return

    if args.bench:
        base = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(base, 'evaluation', 'test_questions.json'), encoding='utf-8') as f:
            questions = json.load(f)
        summary = benchmark(questions, retriever)
        print(f"{'模式':<12}{'表召回':>8}{'表精确':>8}{'全命中':>8}{'平均表数':>9}{'平均字符':>9}{'平均耗时':>9}")
        for mode, s in summary.items():
            print(f"{mode:<12}{s['table_recall']:>8.3f}{s['table_precision']:>8.3f}"
                  f"{s['full_set_hit']:>8.3f}{s['avg_tables']:>9.2f}{s['avg_render_chars']:>9d}"
                  f"{s['avg_ms']:>8.2f}ms")
        with open(os.path.join(base, 'evaluation', 'retrieval_bench.json'), 'w', encoding='utf-8') as f:
            json.dump({'n': summary['bm25']['n'], 'summary': summary}, f, ensure_ascii=False, indent=2)
        print('\n已写入 evaluation/retrieval_bench.json')


if __name__ == '__main__':
    main()
