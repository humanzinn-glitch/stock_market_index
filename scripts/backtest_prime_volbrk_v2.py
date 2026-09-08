#!/usr/bin/env python3
import argparse,csv,json,math
from pathlib import Path

LOOKBACK=20
VOLUME_MULTIPLE=2.5
HORIZONS=(5,10,15,20)

def pct(a,b): return (b/a-1)*100 if a else None

def main():
 ap=argparse.ArgumentParser()
 ap.add_argument('--input-dir',default='runtime/prime_chunks')
 ap.add_argument('--out-dir',default='data/backtests/volbrk_v2_prime')
 args=ap.parse_args()
 cp=Path('data/constituents/tse_prime/current.csv')
 with cp.open(encoding='utf-8-sig',newline='') as f: cons={r['code']:r for r in csv.DictReader(f)}
 by={}
 for p in Path(args.input_dir).glob('chunk_*.csv'):
  with p.open(encoding='utf-8-sig',newline='') as f:
   for r in csv.DictReader(f):
    if r.get('code') in cons: by.setdefault(r['code'],{})[r['date']]=r
 trades=[]
 for code,dates in by.items():
  ds=sorted(dates)
  for i in range(LOOKBACK,len(ds)-1):
   cur=dates[ds[i]]; prior=[dates[d] for d in ds[i-LOOKBACK:i]]
   ph=max(float(x['high']) for x in prior); av=sum(float(x['volume']) for x in prior)/LOOKBACK
   vm=float(cur['volume'])/av if av else 0
   if float(cur['close'])<=ph or vm<VOLUME_MULTIPLE: continue
   entry_i=i+1; entry=float(dates[ds[entry_i]]['open'])
   row={'code':code,'company_name':cons[code]['company_name'],'signal_date':ds[i],'entry_date':ds[entry_i],'entry_open':entry,'signal_close':float(cur['close']),'prior_20d_high':ph,'volume_multiple':round(vm,4)}
   end=min(len(ds)-1,entry_i+max(HORIZONS))
   lows=[float(dates[ds[j]]['low']) for j in range(entry_i,end+1)]
   highs=[float(dates[ds[j]]['high']) for j in range(entry_i,end+1)]
   row['mae_20d_pct']=round(pct(entry,min(lows)),4); row['mfe_20d_pct']=round(pct(entry,max(highs)),4)
   for h in HORIZONS:
    j=entry_i+h
    row[f'ret_{h}d_pct']=round(pct(entry,float(dates[ds[j]]['close'])),4) if j<len(ds) else None
   trades.append(row)
 out=Path(args.out_dir); out.mkdir(parents=True,exist_ok=True)
 fields=['code','company_name','signal_date','entry_date','entry_open','signal_close','prior_20d_high','volume_multiple','ret_5d_pct','ret_10d_pct','ret_15d_pct','ret_20d_pct','mae_20d_pct','mfe_20d_pct']
 with (out/'trades.csv').open('w',encoding='utf-8-sig',newline='') as f:
  w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(trades)
 summary={'strategy':'VOLBRK v2 Prime','rules':{'lookback':20,'volume_multiple':2.5,'entry':'next trading day open'},'symbols_with_data':len(by),'trades':len(trades),'horizons':{}}
 for h in HORIZONS:
  vals=[r[f'ret_{h}d_pct'] for r in trades if r[f'ret_{h}d_pct'] is not None]
  summary['horizons'][str(h)]={'n':len(vals),'win_rate_pct':round(sum(v>0 for v in vals)/len(vals)*100,2) if vals else None,'avg_return_pct':round(sum(vals)/len(vals),4) if vals else None,'median_return_pct':round(sorted(vals)[len(vals)//2] if vals else 0,4) if vals else None}
 maes=[r['mae_20d_pct'] for r in trades]; mfes=[r['mfe_20d_pct'] for r in trades]
 summary['avg_mae_20d_pct']=round(sum(maes)/len(maes),4) if maes else None; summary['avg_mfe_20d_pct']=round(sum(mfes)/len(mfes),4) if mfes else None
 (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
 print(json.dumps(summary,ensure_ascii=False,indent=2))
if __name__=='__main__': main()
