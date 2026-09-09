#!/usr/bin/env python3
"""Audit every ordinary share, publish partial results, fail CI on incomplete data."""
import argparse
import csv
import hashlib
import json
import os
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from volbrk_core import JST, VERSION, evaluate, ordinary_code, target_session, window_dates


def read_csv(path):
    with path.open(encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def write_csv(path, rows, fields):
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)


def scan(repo, target, input_dir, out_dir, now=None):
    now = now or datetime.now(JST)
    out = repo / out_dir
    out.mkdir(parents=True, exist_ok=True)
    errors, audit, signals, excluded = [], [], [], []
    cons, gathered, conflicts, hashes = [], {}, set(), {}
    cp = repo / 'data/constituents/tse_prime/current.csv'
    try:
        raw_cons = read_csv(cp)
        codes = [c['code'] for c in raw_cons]
        if not codes or len(codes) != len(set(codes)):
            raise ValueError('EMPTY_OR_DUPLICATE_UNIVERSE')
        cons = [c for c in raw_cons if ordinary_code(c['code'])]
        excluded = [c for c in raw_cons if not ordinary_code(c['code'])]
        if not cons:
            raise ValueError('EMPTY_ORDINARY_UNIVERSE')
        dates = window_dates(target, now)
        for path in sorted((repo / input_dir).glob('chunk_*.csv')):
            hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
            try:
                for row in read_csv(path):
                    code, day = row['code'], row['date']
                    old = gathered.setdefault(code, {}).get(day)
                    if old and any(old.get(k) != row.get(k) for k in ('open','high','low','close','volume')):
                        conflicts.add((code, day))
                    gathered[code][day] = row
            except Exception as exc:
                errors.append(f'{path.name}: {type(exc).__name__}: {exc}')
        for c in cons:
            code = c['code']
            data = gathered.get(code, {})
            row = {'trade_date': target, 'code': code, 'company_name': c['company_name'],
                   'data_status': 'UNVERIFIED', 'reason': '', 'latest_source_date': max(data, default=''),
                   'source': data.get(target, {}).get('source', '')}
            try:
                if any((code, d) in conflicts for d in dates):
                    raise ValueError('CONFLICTING_DUPLICATE')
                row.update(evaluate(data, dates))
                row['data_status'] = 'VERIFIED'
                if row['signal']:
                    signals.append(dict(row))
            except (ValueError, KeyError, TypeError) as exc:
                row['reason'] = str(exc)
            audit.append(row)
    except Exception as exc:
        errors.append(f'{type(exc).__name__}: {exc}')
    checked = sum(r['data_status'] == 'VERIFIED' for r in audit)
    complete = bool(cons) and checked == len(cons) and not errors
    state = 'COMPLETE' if complete else ('PARTIAL' if checked and not errors else 'UNAVAILABLE')
    # A malformed batch makes its completeness unknowable. Never publish candidates from it.
    if errors:
        signals = []
    count = len(signals) if checked and not errors else None
    status = {'strategy': 'VOLBRK v2 Prime', 'rule_version': VERSION,
              'date': target, 'generated_at_jst': now.isoformat(timespec='seconds'),
              'source_commit': os.environ.get('GITHUB_SHA', ''),
              'run_id': os.environ.get('GITHUB_RUN_ID', ''),
              'status': state, 'universe': len(cons), 'excluded_nonordinary_count': len(excluded),
              'symbols_with_input': sum(bool(gathered.get(c['code'])) for c in cons),
              'checked': checked, 'coverage_pct': round(100*checked/len(cons), 2) if cons else 0,
              'signals': count, 'complete': complete, 'missing_count': len(cons)-checked,
              'reason_counts': dict(Counter(r['reason'] for r in audit if r['reason'])),
              'errors': errors, 'signal_rows': signals,
              'universe_sha256': hashlib.sha256(cp.read_bytes()).hexdigest() if cp.exists() else None,
              'input_sha256': hashes,
              'rules': {'lookback_sessions': 20, 'price': 'close > max(prior 20 highs)',
                        'volume_multiple_min': 2.5, 'entry': 'next session open candidate'}}
    if state == 'UNAVAILABLE':
        message = '判定不能：検証済みの結果を提示できません。シグナル数は不明です。'
    elif not complete:
        message = f'部分判定：検証済み{checked}銘柄内の条件一致は{count}銘柄。未判定分は不明です。'
    else:
        message = f'全対象判定済み：条件一致{count}銘柄。'
    status['message'] = message
    lines = [f'VOLBRK v2 Prime | 判定日 {target}', message,
             f'判定可能: {checked}/{len(cons)}銘柄（{status["coverage_pct"]:.2f}%）',
             f'未判定: {len(cons)-checked}銘柄 / 普通株以外の除外: {len(excluded)}銘柄']
    for r in signals:
        lines.append(f'{r["code"]} {r["company_name"]} | 終値 {r["close"]:,.2f} | 直前20日高値 {r["prior_20d_high"]:,.2f} | 出来高 {r["volume_multiple"]:.4f}倍 | 翌営業日寄付きの候補')
    lines += [f'未判定理由: {status["reason_counts"]}', *errors]
    fields = ['trade_date','code','company_name','data_status','reason','latest_source_date',
              'close','prior_20d_high','volume','prior_20d_avg_volume','volume_multiple','signal','source']
    write_csv(out/'audit_latest.csv', audit, fields)
    write_csv(out/'latest.csv', signals, fields)
    write_csv(out/'excluded_latest.csv', excluded, ['code','company_name','ticker_tse'])
    (out/'latest.txt').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    # JSON is written last and serves as the result manifest; never leave a previous success on errors.
    (out/'latest.json').write_text(json.dumps(status, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print('\n'.join(lines))
    return 0 if complete else 2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--repo-root', default='.')
    ap.add_argument('--as-of')
    ap.add_argument('--input-dir', default='runtime/prime_chunks')
    ap.add_argument('--out-dir', default='data/signals/volbrk_v2_prime')
    args = ap.parse_args()
    return scan(Path(args.repo_root), args.as_of or target_session(), args.input_dir, args.out_dir)


if __name__ == '__main__':
    sys.exit(main())
