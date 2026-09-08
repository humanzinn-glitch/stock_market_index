#!/usr/bin/env python3
import argparse,csv,json,time,urllib.request
from datetime import datetime,timezone
from pathlib import Path
from zoneinfo import ZoneInfo
TOKYO=ZoneInfo('Asia/Tokyo'); UA='Mozilla/5.0'; HOSTS=('query2.finance.yahoo.com','query1.finance.yahoo.com')

def fetch(sym):
 last=None
 for attempt in range(4):
  for host in HOSTS:
   try:
    u=f'https://{host}/v8/finance/chart/{sym}?range=5y&interval=1d&events=history&includeAdjustedClose=false'
    with urllib.request.urlopen(urllib.request.Request(u,headers={'User-Agent':UA,'Accept':'application/json'}),timeout=30) as r: d=json.loads(r.read())
    z=(d.get('chart') or {}).get('result') or []
    if z:return z[0],u
   except Exception as e:last=e
  time.sleep(3*(attempt+1))
 raise RuntimeError(repr(last))

def main():
 a=argparse.ArgumentParser();a.add_argument('--chunk',type=int,required=True);a.add_argument('--chunk-size',type=int,default=25);x=a.parse_args()
 with open('data/constituents/tse_prime/current.csv',encoding='utf-8-sig',newline='') as f: cs=list(csv.DictReader(f))
 sel=cs[x.chunk*x.chunk_size:(x.chunk+1)*x.chunk_size]
 out=Path('runtime/prime_history_chunks');out.mkdir(parents=True,exist_ok=True);rows=[];errs=[]
 for c in sel:
  try:
   z,u=fetch(c['ticker_tse']);ts=z.get('timestamp') or [];q=((z.get('indicators') or {}).get('quote') or [{}])[0];n=0
   for i,t in enumerate(ts):
    try:v=[q[k][i] for k in ('open','high','low','close','volume')]
    except (KeyError,IndexError):continue
    if any(y is None for y in v):continue
    d=datetime.fromtimestamp(int(t),timezone.utc).astimezone(TOKYO).strftime('%Y-%m-%d')
    rows.append([c['code'],c['company_name'],c['ticker_tse'],d,*v,u]);n+=1
   if n<21:raise RuntimeError(f'rows={n}')
   print('OK',c['ticker_tse'],n)
  except Exception as e:errs.append(f"{c['ticker_tse']}: {e}");print('FAIL',errs[-1])
  time.sleep(.5)
 p=out/f'chunk_{x.chunk:02d}.csv'
 with p.open('w',encoding='utf-8',newline='') as f:
  w=csv.writer(f);w.writerow(['code','company_name','ticker_tse','date','open','high','low','close','volume','source']);w.writerows(rows)
 if errs:(out/f'chunk_{x.chunk:02d}.errors.txt').write_text('\n'.join(errs),encoding='utf-8');raise SystemExit(1)
 print(f'COMPLETE chunk={x.chunk} symbols={len(sel)} rows={len(rows)}')
if __name__=='__main__':main()
