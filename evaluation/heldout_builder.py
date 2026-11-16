# -*- coding: utf-8 -*-
"""P0：留出集（held-out set）的校验与冻结工具。

为什么需要它：
现有 150 题全部由团队自己编写、且规则/提示词是在这批题上迭代出来的，100% 只能
说明"见过的题会做"。留出集的作用是给"没见过的问法"一个独立基准，并且必须满足：

  1) 编写时不参考 main.SYSTEM_PROMPT 里的业务规则（只依据表结构与业务含义）；
  2) 与 150 题、30 道改写题的措辞相似度低于阈值（防重复）；
  3) 基准 SQL 能在真实库上执行且结果非空（防写错答案）；
  4) 冻结后任何修改都会被 --verify 检出（防事后调题）。

用法：
  python evaluation/heldout_builder.py --leakage    # 查重
  python evaluation/heldout_builder.py --validate   # 校验基准 SQL
  python evaluation/heldout_builder.py --freeze     # 查重+校验通过后冻结
  python evaluation/heldout_builder.py --verify     # 检查冻结后是否被改动
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import sys
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(BASE)
SPEC_PATH = os.path.join(BASE, 'heldout_spec.json')
MANIFEST_PATH = os.path.join(BASE, 'heldout_manifest.json')
BANK_PATHS = [os.path.join(BASE, 'test_questions.json'),
              os.path.join(BASE, 'generalization_questions.json')]

LEAK_THRESHOLD = 0.55  # 中文 bigram Jaccard，超过即视为近似重复


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: str) -> str:
    with open(path, 'rb') as f:
        return _sha256_bytes(f.read())


def _norm(text: str) -> str:
    return re.sub(r'[^\u4e00-\u9fff0-9a-z]', '', (text or '').lower())


def _bigrams(text: str):
    s = _norm(text)
    return {s[i:i + 2] for i in range(len(s) - 1)}


def jaccard(a: str, b: str) -> float:
    sa, sb = _bigrams(a), _bigrams(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def load_spec() -> dict:
    if not os.path.exists(SPEC_PATH):
        raise SystemExit(f'找不到留出集定义：{SPEC_PATH}')
    with open(SPEC_PATH, 'r', encoding='utf-8') as f:
        return json.load(f)


def load_bank() -> list:
    """读取现有题库，仅用于查重。"""
    items = []
    for path in BANK_PATHS:
        if not os.path.exists(path):
            continue
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        for q in data if isinstance(data, list) else data.get('questions', data.get('items', [])):
            if q.get('question'):
                items.append({'source': os.path.basename(path), 'question': q['question']})
    return items


def check_leakage(spec: dict, verbose: bool = True):
    bank = load_bank()
    rows, failures = [], []
    for item in spec['items']:
        scored = sorted(((jaccard(item['question'], b['question']), b) for b in bank),
                        key=lambda kv: -kv[0])
        best_sim, best = scored[0]
        rows.append((item['id'], best_sim, best['question'], best['source']))
        if best_sim >= LEAK_THRESHOLD:
            failures.append(item['id'])
    if verbose:
        rows.sort(key=lambda r: -r[1])
        print(f'与现有 {len(bank)} 道题的最大相似度（阈值 {LEAK_THRESHOLD}）：')
        for iid, sim, text, src in rows:
            flag = '重复' if sim >= LEAK_THRESHOLD else 'ok'
            print(f'  {iid:<4} {sim:.2f} {flag}  最近邻[{src}] {text[:38]}')
    return failures


def check_sql(spec: dict, verbose: bool = True):
    """在只读连接上执行基准 SQL：必须能跑通，且结果不应是"全空但语义应为有值"。"""
    sys.path.insert(0, PROJECT)
    from main import execute_sql  # 复用已有的只读连接 + 超时 + 行数上限

    failures = []
    for item in spec['items']:
        cols, rows, err = execute_sql(item['standard_sql'], validate_sql=False)
        status = 'ok'
        if err:
            status = f'执行错误: {err[:90]}'
            failures.append(item['id'])
        elif not rows:
            status = '结果为空（若确属空结果场景请在 note 中说明）'
            if item.get('expected_behavior') != 'empty_ok':
                failures.append(item['id'])
        if verbose:
            head = '' if not rows else f' 首行={str(rows[0])[:60]}'
            print(f"  {item['id']:<4} 行数={0 if not rows else len(rows):<5} {status}{head}")
    return failures


def freeze(spec: dict, force: bool = False):
    if os.path.exists(MANIFEST_PATH) and not force:
        raise SystemExit('已存在 heldout_manifest.json。留出集只能冻结一次；'
                         '如确需新建，请复制为 heldout_v2_spec.json 并改脚本常量。')
    leak = check_leakage(spec, verbose=False)
    bad_sql = check_sql(spec, verbose=False)
    if leak or bad_sql:
        raise SystemExit(f'冻结失败：疑似重复 {leak}；SQL 未通过 {bad_sql}。')

    prompt_sha = ''
    try:
        sys.path.insert(0, PROJECT)
        from main import SYSTEM_PROMPT
        prompt_sha = _sha256_bytes(SYSTEM_PROMPT.encode('utf-8'))
    except Exception:
        pass

    manifest = {
        'frozen_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'spec_file': os.path.basename(SPEC_PATH),
        'spec_sha256': _sha256_file(SPEC_PATH),
        'n_items': len(spec['items']),
        'skills': sorted({i.get('skill', '') for i in spec['items']}),
        'prompt_sha256': prompt_sha,
        'metadata_sha256': _sha256_file(os.path.join(PROJECT, 'metadata', 'metadata.json')),
        'question_bank_sha256': {os.path.basename(p): _sha256_file(p) for p in BANK_PATHS if os.path.exists(p)},
        'python': sys.version.split()[0],
        'platform': platform.platform(),
        'rule': '冻结后禁止修改 spec；改动会被 --verify 检出；调参后只能新建 v2',
    }
    with open(MANIFEST_PATH, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


def verify():
    if not os.path.exists(MANIFEST_PATH):
        raise SystemExit('尚未冻结：请先运行 --freeze')
    with open(MANIFEST_PATH, 'r', encoding='utf-8') as f:
        manifest = json.load(f)
    now_sha = _sha256_file(SPEC_PATH)
    ok = now_sha == manifest['spec_sha256']
    print(f"留出集：{manifest['n_items']} 题，冻结于 {manifest['frozen_at']}")
    print(f"提交时 SHA256：{manifest['spec_sha256']}")
    print(f"当前     SHA256：{now_sha}")
    print('结论：' + ('未被改动' if ok else '已被改动，成绩不再具备留出意义'))
    if not ok:
        raise SystemExit(1)


def main():
    ap = argparse.ArgumentParser(description='留出集校验与冻结（P0）')
    ap.add_argument('--leakage', action='store_true', help='与现有题库查重')
    ap.add_argument('--validate', action='store_true', help='执行每条基准 SQL')
    ap.add_argument('--freeze', action='store_true', help='查重+校验通过后写入冻结清单')
    ap.add_argument('--force', action='store_true', help='覆盖已有清单（不推荐）')
    ap.add_argument('--verify', action='store_true', help='校验冻结后是否被改动')
    args = ap.parse_args()

    if args.verify:
        verify()
        return
    spec = load_spec()
    print(f"留出集：{len(spec['items'])} 题，覆盖技能 {sorted({i.get('skill') for i in spec['items']})}\n")

    if args.leakage:
        bad = check_leakage(spec)
        print(f"\n疑似重复：{bad if bad else '无'}")
    if args.validate:
        print('\n基准 SQL 校验：')
        bad = check_sql(spec)
        print(f"\n未通过：{bad if bad else '无'}")
    if args.freeze:
        freeze(spec, force=args.force)
    if not any([args.leakage, args.validate, args.freeze]):
        print('请指定 --leakage / --validate / --freeze / --verify')


if __name__ == '__main__':
    main()
