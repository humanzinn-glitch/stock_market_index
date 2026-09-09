#!/usr/bin/env python3
"""Fetch a bounded chunk and validate dates before declaring success."""
import argparse
import csv
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from volbrk_core import JST, evaluate, ordinary_code, target_session, window_dates

HOSTS = ('query1.finance.yahoo.com', 'query2.finance.yahoo.com')
FIELDS = ['code','company_name','ticker_tse','date','open','high','low','close','volume','source','fetched_at_jst']


def fetch(c, target, dates):
    errors, best = [], []
    for host in HOSTS:
        url = f'https://{host}/v8/finance/chart/{c["ticker_tse"]}?range=3mo&interval=1d&events=history&includeAdjustedClose=false'
        try:
            request = urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0', 'Accept':'application/json'})
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.load(response)
            result = (payload.get('chart') or {}).get('result') or []
            if not result:
                raise ValueError('EMPTY_RESPONSE')
            z = result[0]
            meta = z.get('meta', {})
            if meta.get('symbol') != c['ticker_tse'] or meta.get('currency') != 'JPY':
                raise ValueError('SYMBOL_OR_CURRENCY_MISMATCH')
            q = z['indicators']['quote'][0]
            rows = []
            for i, timestamp in enumerate(z.get('timestamp') or []):
                day = datetime.fromtimestamp(timestamp, timezone.utc).astimezone(JST).date().isoformat()
                if day > target:
                    continue
                values = [q[k][i] for k in ('open','high','low','close','volume')]
                # Keep incomplete rows visible to the scanner instead of filling values.
                rows.append(dict(zip(FIELDS, [c['code'],c['company_name'],c['ticker_tse'],day,*values,url,datetime.now(JST).isoformat(timespec='seconds')])))
            if len(rows) > len(best):
                best = rows
            by_date = {r['date']:r for r in rows}
            if len(by_date) != len(rows):
                raise ValueError('DUPLICATE_SOURCE_DATES')
            evaluate(by_date, dates)
            return rows, None
        except urllib.error.HTTPError as exc:
            errors.append(f'{host}: HTTP {exc.code}')
            if exc.code in (401, 403, 429):
                # Respect access/rate limits. Do not retry another host to bypass them.
                break
        except Exception as exc:
            errors.append(f'{host}: {type(exc).__name__}: {exc}')
    return best, f'{c["code"]}: ' + '; '.join(errors)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--chunk', type=int, required=True)
    ap.add_argument('--chunk-size', type=int, default=25)
    ap.add_argument('--as-of')
    ap.add_argument('--workers', type=int, default=1)
    x = ap.parse_args()
    if x.chunk < 0 or x.chunk_size <= 0 or not 1 <= x.workers <= 4:
        ap.error('invalid chunk/size/workers')
    target = x.as_of or target_session()
    dates = window_dates(target)
    with open('data/constituents/tse_prime/current.csv', encoding='utf-8-sig', newline='') as f:
        cs = [c for c in csv.DictReader(f) if ordinary_code(c['code'])]
    selected = cs[x.chunk*x.chunk_size:(x.chunk+1)*x.chunk_size]
    out = Path('runtime/prime_chunks')
    out.mkdir(parents=True, exist_ok=True)
    rows, errors = [], []
    with ThreadPoolExecutor(max_workers=x.workers) as executor:
        for c, result in zip(selected, executor.map(lambda c: fetch(c, target, dates), selected)):
            bars, error = result
            rows.extend(bars)
            if error:
                errors.append(error)
            print(('FAIL' if error else 'OK'), c['code'], len(bars), flush=True)
    path = out/f'chunk_{x.chunk:02d}.csv'
    with path.open('w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    (out/f'chunk_{x.chunk:02d}.status.json').write_text(json.dumps({'date':target,'requested':len(selected),'failed':len(errors),'errors':errors}, ensure_ascii=False, indent=2), encoding='utf-8')
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main())
