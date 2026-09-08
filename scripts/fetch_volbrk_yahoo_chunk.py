#!/usr/bin/env python3
"""Fetch a small Nikkei-225 chunk from Yahoo chart API.

Designed for GitHub Actions matrix jobs so no single shared runner IP makes
hundreds of Yahoo requests. Uses query1/query2 fallback, no cookie/crumb.
Writes one compact CSV artifact containing recent OHLCV rows.
"""
import argparse,csv,json,time,urllib.request,urllib.error
from datetime import datetime,timezone
from pathlib import Path
from zoneinfo import ZoneInfo

TOKYO=ZoneInfo('Asia/Tokyo')
UA='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'
HOSTS=('query1.finance.yahoo.com','query2.finance.yahoo.com')
FIELDS=['code','ticker_tse','date','open','high','low','close','volume','source']

def fetch(symbol,retries=2):
    last=None
    for attempt in range(retries+1):
        for host in HOSTS:
            url=f'https://{host}/v8/finance/chart/{symbol}?range=2mo&interval=1d&events=history&includeAdjustedClose=false'
            try:
                req=urllib.request.Request(url,headers={'User-Agent':UA,'Accept':'application/json'})
                with urllib.request.urlopen(req,timeout=20) as r:
                    data=json.loads(r.read().decode())
                result=(data.get('chart') or {}).get('result') or []
                if result: return result[0],url
                last='empty chart result'
            except Exception as e: last=repr(e)
        if attempt<retries: time.sleep(3*(attempt+1))
    raise RuntimeError(f'{symbol}: {last}')

def rows_from_chart(code,symbol,chart,url):
    ts=chart.get('timestamp') or []
    q=((chart.get('indicators') or {}).get('quote') or [{}])[0]
    out=[]
    for i,t in enumerate(ts):
        try: vals=[q[k][i] for k in ('open','high','low','close','volume')]
        except Exception: continue
        if any(v is None for v in vals): continue
        d=datetime.fromtimestamp(int(t),timezone.utc).astimezone(TOKYO).strftime('%Y-%m-%d')
        out.append({'code':code,'ticker_tse':symbol,'date':d,'open':f'{float(vals[0]):.2f}','high':f'{float(vals[1]):.2f}','low':f'{float(vals[2]):.2f}','close':f'{float(vals[3]):.2f}','volume':str(int(vals[4])),'source':url})
    return out

def main():
    p=argparse.ArgumentParser(); p.add_argument('--chunk',type=int,required=True); p.add_argument('--chunk-size',type=int,default=15); p.add_argument('--out-dir',default='runtime/yahoo_chunks'); a=p.parse_args()
    with open('data/constituents/nikkei225/current.csv',encoding='utf-8-sig',newline='') as f: cons=list(csv.DictReader(f))
    start=a.chunk*a.chunk_size; selected=cons[start:start+a.chunk_size]
    if not selected: raise SystemExit(f'empty chunk {a.chunk}')
    allrows=[]; errors=[]
    for n,c in enumerate(selected,1):
        symbol=c['ticker_tse']
        try:
            chart,url=fetch(symbol); rr=rows_from_chart(c['code'],symbol,chart,url)
            if len(rr)<21: raise RuntimeError(f'only {len(rr)} valid rows')
            allrows.extend(rr[-45:]); print(f'[{n}/{len(selected)}] OK {symbol} rows={len(rr)}')
        except Exception as e:
            errors.append(f'{symbol}: {e}'); print(f'[{n}/{len(selected)}] FAIL {e}')
        time.sleep(0.4)
    out=Path(a.out_dir); out.mkdir(parents=True,exist_ok=True)
    path=out/f'chunk_{a.chunk:02d}.csv'
    with path.open('w',encoding='utf-8',newline='') as f:
        w=csv.DictWriter(f,fieldnames=FIELDS); w.writeheader(); w.writerows(allrows)
    if errors:
        (out/f'chunk_{a.chunk:02d}.errors.txt').write_text('\n'.join(errors),encoding='utf-8')
        raise SystemExit(f'chunk incomplete: {len(errors)} errors')
    print(f'chunk COMPLETE symbols={len(selected)} rows={len(allrows)}')
if __name__=='__main__': main()
