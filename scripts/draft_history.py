#!/usr/bin/env python3
"""Local, cross-session draft checks. Python 3.9+, standard library only.

Exit 0: requested operation succeeded; 2: duplicate; 3: unavailable/invalid;
4: semantic review required; 5: content/format rejected. No model/network calls.
"""
import argparse
import datetime as dt
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import unicodedata
import uuid

VERSION = 1
MAX_HISTORY = 5000
DEFAULT_DB = Path.home() / '.mengge-copywriting' / 'history.sqlite3'
CONTRACT = json.loads((Path(__file__).resolve().parents[1] / 'references/mode-5-contract.json').read_text(encoding='utf-8'))


def canonical(text):
    return ''.join(c for c in unicodedata.normalize('NFKC', text).casefold()
                   if unicodedata.category(c)[0] in 'LN')


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def label(text):
    # Keep punctuation in account/product identifiers; normalize case and spacing only.
    return ' '.join(unicodedata.normalize('NFKC', text).casefold().split())


def marker(db):
    return db.with_suffix(db.suffix + '.identity.json')


def initialize(db):
    if db.exists():
        c = connect(db); c.close()
        return {'status': 'already_initialized', 'db': str(db)}
    if marker(db).exists():
        raise ValueError('历史库已初始化但数据库丢失；请恢复原文件，不可作为首次使用重建。')
    db.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    identity = str(uuid.uuid4())
    # A failed initialization remains visible; it must not silently reset history.
    with marker(db).open('x', encoding='utf-8') as f:
        json.dump({'version': VERSION, 'id': identity}, f)
    fd = os.open(str(db), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600); os.close(fd)
    c = sqlite3.connect(str(db))
    try:
        c.executescript('''
        CREATE TABLE metadata(id TEXT NOT NULL);
        CREATE TABLE drafts(id INTEGER PRIMARY KEY, account TEXT NOT NULL,
          product TEXT NOT NULL, created TEXT NOT NULL, kind TEXT NOT NULL,
          title TEXT NOT NULL, body TEXT NOT NULL, topics TEXT NOT NULL,
          angle TEXT NOT NULL, mode INTEGER NOT NULL, body_key TEXT NOT NULL,
          variation TEXT NOT NULL);
        CREATE INDEX scope ON drafts(account,product,id);
        CREATE TABLE receipts(request_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL,
          receipt TEXT NOT NULL);
        PRAGMA user_version=1;
        ''')
        c.execute('INSERT INTO metadata VALUES(?)', (identity,)); c.commit()
    finally:
        c.close()
    return {'status': 'initialized', 'db': str(db), 'history_count': 0}


def connect(db):
    if not db.is_file() or not marker(db).is_file():
        raise ValueError('历史库或配对标识不存在；首次使用才可init，已有历史须恢复原目录。')
    meta = json.loads(marker(db).read_text(encoding='utf-8'))
    c = sqlite3.connect(db.resolve().as_uri() + '?mode=rw', uri=True, timeout=15)
    c.row_factory = sqlite3.Row
    try:
        if (c.execute('PRAGMA user_version').fetchone()[0] != VERSION
                or c.execute('PRAGMA quick_check').fetchone()[0] != 'ok'
                or c.execute('SELECT id FROM metadata').fetchone()[0] != meta['id']):
            raise ValueError('历史库版本、完整性或配对标识不符；停止查重，不覆盖原库。')
        c.execute('SELECT request_id,payload_hash,receipt FROM receipts LIMIT 1')
        c.execute('SELECT id,account,product,created,kind,title,body,topics,angle,mode,body_key,variation FROM drafts LIMIT 1')
        return c
    except Exception:
        c.close(); raise


def load_job(path, importing=False):
    job = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    for key in ('request_id', 'account', 'product', 'source_topics'):
        if not isinstance(job.get(key), str) or (key != 'source_topics' and not job[key].strip()):
            raise ValueError('缺少有效字段：' + key)
    if type(job.get('mode')) is not int or job['mode'] not in (range(0, 6) if importing else range(1, 6)):
        raise ValueError('mode必须是1至5；仅导入旧稿时可用0表示未知。')
    if not isinstance(job.get('drafts'), list) or not 1 <= len(job['drafts']) <= 10:
        raise ValueError('每批需要1至10篇成稿。')
    for draft in job['drafts']:
        for key in ('title', 'body', 'topics', 'angle'):
            if not isinstance(draft.get(key), str): raise ValueError('成稿缺少：' + key)
        if not 20 <= len(canonical(draft['body'])) <= 10000:
            raise ValueError('正文需20至10000个可比较字符。')
        if '#' in draft['body'] or '＃' in draft['body']:
            raise ValueError('正文中的话题需按来源规则单独放进topics，不可混入正文查重。')
        if draft['topics'] != job['source_topics']:
            raise ValueError('话题尾部与来源不一致：空格、换行、顺序和重复项须原样保留。')
        if not canonical(draft['angle']): raise ValueError('需要具体的叙事角度。')
        plan = draft.get('variation')
        if importing and plan is None:
            draft['variation'] = None
        elif (not isinstance(plan, dict) or set(plan) != {'scene', 'focus', 'structure'}
              or any(not isinstance(v, str) or not canonical(v) for v in plan.values())):
            raise ValueError('新稿须先确定variation的scene/focus/structure三个稳定标识，不能只换措辞。')
    return job


def content_issues(job):
    """Narrow deterministic checks from observed mode-5 failures, not semantic proof."""
    if job['mode'] != 5:
        return []
    facts = job.get('product_facts', [])
    known = {f.get('kind') for f in facts if isinstance(f, dict)
             and f.get('status') in ('用户提供', '已核实', 'user_provided', 'verified')
             and f.get('value') and f.get('source')}
    issues = []
    if not facts:
        issues.append({'reason': 'missing_product_facts'})
    bounds = job.get('body_length', {'min': 160, 'max': 210})
    if (not isinstance(bounds, dict) or type(bounds.get('min')) is not int
            or type(bounds.get('max')) is not int or not 1 <= bounds['min'] <= bounds['max'] <= 10000):
        return [{'reason': 'invalid_body_length'}]
    for i, draft in enumerate(job['drafts']):
        body = draft['body']
        count = len(re.findall(r'[\u4e00-\u9fff]', body))
        if not bounds['min'] <= count <= bounds['max']:
            issues.append({'draft_index': i, 'reason': 'body_length', 'hanzi': count, 'required': bounds})
        if len(re.split(r'\n\s*\n', body.strip())) != 3:
            issues.append({'draft_index': i, 'reason': 'paragraph_count'})
        plan = draft.get('variation') or {}
        if any(plan.get(axis) not in choices for axis, choices in CONTRACT['axes'].items()):
            issues.append({'draft_index': i, 'reason': 'unknown_plan_id'})
        for kind, pattern in CONTRACT['unsupported_claim_patterns'].items():
            match = re.search(pattern, draft['title'] + '\n' + body)
            if kind not in known and match:
                issues.append({'draft_index': i, 'reason': 'unsupported_' + kind, 'quote': match.group()})
    return issues


def payload_hash(job):
    return digest({k: v for k, v in job.items() if k != 'semantic_review'})


def rows(c, account, product):
    result = c.execute('SELECT * FROM drafts WHERE account=? AND product=? ORDER BY id',
                       (label(account), label(product))).fetchall()
    if len(result) > MAX_HISTORY:
        raise ValueError('本产品历史超过5000篇，当前检查容量不足；不能截断后宣称全部检查。')
    result = [dict(r) for r in result]
    for r in result: r['variation'] = json.loads(r['variation'])
    return result


def similarity(a, b):
    if a == b: return 1.0, 1.0, 1.0
    def grams(s): return {s[i:i+4] for i in range(max(1, len(s)-3))}
    x, y = grams(a), grams(b)
    common = len(x & y)
    return (difflib.SequenceMatcher(None, a, b, autojunk=False).ratio(),
            2 * common / max(1, len(x)+len(y)), common / max(1, min(len(x), len(y))))


def inspect(job, history):
    blocked, neighbors = [], []
    for index, draft in enumerate(job['drafts']):
        prior_batch = [dict(d, id='batch:' + str(i), body_key=canonical(d['body']))
                       for i, d in enumerate(job['drafts'][:index])]
        ranked = []
        for old in history + prior_batch:
            key = canonical(draft['body'])
            seq, dice, containment = similarity(key, old['body_key'])
            reason = ('exact_body' if key == old['body_key'] else
                      'near_body' if seq >= .82 or dice >= .72 or containment >= .88 else None)
            # Reusing a generic named mode is not an angle; require a new concrete cut.
            if reason is None and canonical(draft['angle']) == canonical(old['angle']):
                reason = 'reused_angle'
            # Observed live model test: paraphrased breakfast/key-search scene passed lexical
            # thresholds. Stable, pre-generation axes prevent accepting the same plan again.
            new_plan, old_plan = draft.get('variation'), old.get('variation')
            if reason is None and new_plan and old_plan and (old in history[-20:] or isinstance(old['id'], str)):
                def plan_id(plan, axis):
                    value = label(plan[axis])
                    return CONTRACT['legacy_scene_aliases'].get(value, value) if axis == 'scene' else value
                changed = sum(plan_id(new_plan, k) != plan_id(old_plan, k) for k in ('scene','focus','structure'))
                if changed < 2: reason = 'reused_variation'
            info = {'draft_index': index, 'history_id': str(old['id']),
                    'sequence_ratio': round(seq, 4), 'ngram_dice': round(dice, 4),
                    'containment': round(containment, 4)}
            if reason: blocked.append(dict(info, reason=reason))
            if isinstance(old['id'], int):
                ranked.append((max(seq, dice), dict(info, title=old['title'],
                                                   body=old['body'], angle=old['angle'],
                                                   variation=old.get('variation'))))
        neighbors.extend(v for _, v in sorted(ranked, key=lambda x: x[0], reverse=True)[:3])
    reviewed_ids = sorted({n['history_id'] for n in neighbors})
    issues = content_issues(job)
    return {'status': 'content_rejected' if issues else 'duplicate' if blocked else 'lexical_pass',
            'payload_hash': payload_hash(job), 'history_checked': len(history),
            'batch_checked': len(job['drafts']), 'matches': blocked, 'neighbors': neighbors,
            'required_review_ids': reviewed_ids,
            'content_issues': issues,
            'semantic_coverage': '最近历史由history读取；本次返回每稿最接近的3篇，需另做语义复核。',
            'platform_similarity_claim': False}


def semantic_pass(job, result):
    review = job.get('semantic_review', {})
    if job['mode'] == 5:
        if review.get('facts_supported') is not True or review.get('distinct_content') is not True:
            return False
        evidence = review.get('comparison_evidence', [])
        if not isinstance(evidence, list):
            return False
        allowed_pairs = {(n['history_id'], n['draft_index']) for n in result['neighbors']}
        if (not isinstance(review.get('compared_ids'), list)
                or set(review['compared_ids']) != set(result['required_review_ids'])
                or any(not isinstance(e, dict) or (e.get('history_id'), e.get('draft_index')) not in allowed_pairs
                       for e in evidence)):
            return False
        for neighbor in result['neighbors']:
            draft = job['drafts'][neighbor['draft_index']]
            if not any(isinstance(e, dict) and e.get('history_id') == neighbor['history_id']
                       and e.get('draft_index') == neighbor['draft_index']
                       and isinstance(e.get('old_quote'), str) and len(e['old_quote']) >= 8
                       and e['old_quote'] in neighbor['body']
                       and isinstance(e.get('new_quote'), str) and len(e['new_quote']) >= 8
                       and e['new_quote'] in draft['body']
                       and isinstance(e.get('changed_dimensions'), list)
                       and set(e['changed_dimensions']) <= {'scene','focus','structure'}
                       and len(set(e['changed_dimensions'])) >= 2 for e in evidence):
                return False
    return (review.get('status') == 'passed' and review.get('payload_hash') == result['payload_hash']
            and isinstance(review.get('notes'), str) and len(review['notes'].strip()) >= 10
            and isinstance(review.get('compared_ids'), list)
            and set(result['required_review_ids']) <= set(review['compared_ids'])
            and review.get('batch_reviewed') is True)


def process(c, job, action):
    """Recheck and record in one transaction; simultaneous sessions cannot race the gate."""
    c.execute('BEGIN IMMEDIATE' if action in ('commit', 'import') else 'BEGIN')
    try:
        old_receipt = c.execute('SELECT * FROM receipts WHERE request_id=?', (job['request_id'],)).fetchone()
        if old_receipt:
            if old_receipt['payload_hash'] != payload_hash(job):
                raise ValueError('request_id已用于另一份成稿；不可覆盖原回执。')
            result = json.loads(old_receipt['receipt']); result['status'] = 'already_recorded'
            c.rollback(); return result, 0
        history = rows(c, job['account'], job['product'])
        result = inspect(job, history)
        if action != 'import':
            if result['content_issues']: c.rollback(); return result, 5
            if result['matches']: c.rollback(); return result, 2
            if action == 'check': c.rollback(); return result, 0
            if not semantic_pass(job, result):
                result['status'] = 'semantic_review_required'; c.rollback(); return result, 4
        ids = []
        for d in job['drafts']:
            # Imports are existing manuscripts, never a bypass for new generation.
            if action == 'import':
                duplicate = c.execute('SELECT id FROM drafts WHERE account=? AND product=? AND body_key=?',
                    (label(job['account']), label(job['product']), canonical(d['body']))).fetchone()
                if duplicate: ids.append(duplicate['id']); continue
            cursor = c.execute('INSERT INTO drafts(account,product,created,kind,title,body,topics,angle,mode,body_key,variation) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (label(job['account']), label(job['product']), dt.datetime.now(dt.timezone.utc).isoformat(),
                 'imported' if action == 'import' else 'generated', d['title'], d['body'], d['topics'],
                 d['angle'], job['mode'], canonical(d['body']), json.dumps(d.get('variation'),ensure_ascii=False)))
            ids.append(cursor.lastrowid)
        receipt = {'status': 'imported' if action == 'import' else 'recorded',
                   'request_id': job['request_id'], 'payload_hash': payload_hash(job), 'ids': ids,
                   'history_checked': len(history), 'semantic_review': job.get('semantic_review'),
                   'drafts': job['drafts'], 'platform_similarity_claim': False}
        c.execute('INSERT INTO receipts VALUES(?,?,?)',
                  (job['request_id'], payload_hash(job), json.dumps(receipt, ensure_ascii=False)))
        c.commit(); return receipt, 0
    except Exception:
        c.rollback(); raise


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=DEFAULT_DB)
    parser.add_argument('action', choices=['init', 'history', 'check', 'commit', 'import'])
    parser.add_argument('--account'); parser.add_argument('--product'); parser.add_argument('--input')
    args = parser.parse_args()
    try:
        if args.action == 'init': result, code = initialize(args.db), 0
        else:
            c = connect(args.db)
            try:
                if args.action == 'history':
                    if not args.account or not args.product: raise ValueError('history需要账号和产品标识。')
                    history = rows(c, args.account, args.product)
                    result, code = {'status': 'history_read', 'total': len(history),
                        'displayed': min(20, len(history)), 'drafts': history[-20:],
                        'coverage': '本机本库本账号本产品；check/commit会比较全部历史'}, 0
                else:
                    if not args.input: raise ValueError('需要--input指向本次JSON记录。')
                    result, code = process(c, load_job(args.input, args.action == 'import'), args.action)
            finally: c.close()
    except (ValueError, OSError, sqlite3.Error, KeyError, TypeError) as exc:
        result, code = {'status': 'unavailable', 'error': str(exc), 'passed': False}, 3
    print(json.dumps(result, ensure_ascii=False, indent=2)); return code


if __name__ == '__main__':
    sys.exit(main())
