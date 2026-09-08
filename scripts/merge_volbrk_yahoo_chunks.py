#!/usr/bin/env python3
"""Merge distributed Yahoo chunk artifacts into legacy per-code CSV files.
Fails unless exactly all 225 constituents have >=20 prior rows plus target date.
"""
import argparse,csv
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
TOKYO=ZoneInfo('Asia/Tokyo')
FIELDS=['code','ticker_tse','symbol_stooq','date','open','high','low','close','volume','source','fetched_at']

def main():
 p=argparse.ArgumentParser(); p.add_argument('--chunks-dir',default='runtime/yahoo_chunks'); p.add_argument('--as-of',default=''); a=p.parse_args()
 target=a.as_of or datetime.now(TOKYO).strftime('%Y-%m-%d')
 with open('data/constituents/nikkei225/current.csv',encoding='utf-8-sig',newline='') as f: cons=list(csv.DictReader(f))
 if len(cons)!=225: raise SystemExit(f'constituent count {len(cons)} != 225')
 meta={c['code']:c for c in cons}; gathered={}
 files=sorted(Path(a.chunks_dir).glob('chunk_*.csv'))
 for path in files:
  with path.open(encoding='utf-8-sig',newline='') as f:
   for r in csv.DictReader(f): gathered.setdefault(r['code'],{})[r['date']]=r
 missing=[]
 for code,c in meta.items():
  bydate=gathered.get(code,{})
  prior=sorted(d for d in bydate if d<target)
  today=bydate.get(target)
  if today is None or len(prior)<20:
   missing.append(f'{code}: target={today is not None}, prior={len(prior)}'); continue
  path=Path('data/prices/stooq/jp')/f'{code}.csv'; path.parent.mkdir(parents=True,exist_ok=True)
  existing={}
  if path.exists():
   with path.open(encoding='utf-8-sig',newline='') as f:
    for r in csv.DictReader(f):
     if r.get('date'): existing[r['date']]=r
  for d,r in bydate.items():
   existing[d]={'code':code,'ticker_tse':c['ticker_tse'],'symbol_stooq':c['symbol_stooq'],'date':d,'open':r['open'],'high':r['high'],'low':r['low'],'close':r['close'],'volume':r['volume'],'source':r['source'],'fetched_at':datetime.now(TOKYO).isoformat()}
  with path.open('w',encoding='utf-8',newline='') as f:
   w=csv.DictWriter(f,fieldnames=FIELDS); w.writeheader(); w.writerows(existing[d] for d in sorted(existing))
 if missing:
  print('判定不能：全225銘柄の完全照合未完了'); print('\n'.join(missing)); raise SystemExit(2)
 print(f'Yahoo distributed merge COMPLETE 225/225 target={target}')
if __name__=='__main__': main()
