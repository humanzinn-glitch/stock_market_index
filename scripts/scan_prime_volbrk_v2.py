#!/usr/bin/env python3
import csv,json,sys
from pathlib import Path
LOOKBACK=20; VM=2.5

def main():
 cp=Path('data/constituents/tse_prime/current.csv')
 with cp.open(encoding='utf-8-sig',newline='') as f: cons=list(csv.DictReader(f))
 gathered={}
 for p in Path('runtime/prime_chunks').glob('chunk_*.csv'):
  with p.open(encoding='utf-8-sig',newline='') as f:
   for r in csv.DictReader(f): gathered.setdefault(r['code'],{})[r['date']]=r

 # Scan the latest trading date that is present for every constituent.
 # This makes manual/overnight runs robust while scheduled post-close runs
 # naturally select the current session once all symbols have that date.
 date_sets=[set(gathered.get(c['code'],{})) for c in cons]
 common_dates=set.intersection(*date_sets) if date_sets and all(date_sets) else set()
 today=max(common_dates) if common_dates else 'N/A'

 audit=[];signals=[];missing=[]
 for c in cons:
  bd=gathered.get(c['code'],{}); ds=sorted(bd); idx=ds.index(today) if today in bd else -1
  if idx<LOOKBACK: missing.append(c['code']); continue
  prior=[bd[d] for d in ds[idx-LOOKBACK:idx]]; cur=bd[today]
  ph=max(float(r['high']) for r in prior); av=sum(float(r['volume']) for r in prior)/LOOKBACK; vm=float(cur['volume'])/av if av>0 else 0
  sig=float(cur['close'])>ph and vm>=VM
  row={'trade_date':today,'code':c['code'],'company_name':c['company_name'],'close':float(cur['close']),'prior_20d_high':ph,'volume':int(float(cur['volume'])),'prior_20d_avg_volume':round(av,2),'volume_multiple':round(vm,4),'signal':sig}
  audit.append(row)
  if sig: signals.append(row)
 out=Path('data/signals/volbrk_v2_prime'); out.mkdir(parents=True,exist_ok=True)
 fields=list(audit[0].keys()) if audit else ['trade_date','code']
 with (out/'audit_latest.csv').open('w',encoding='utf-8-sig',newline='') as f:
  w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(audit)
 sf=['trade_date','code','company_name','close','prior_20d_high','volume','prior_20d_avg_volume','volume_multiple']
 with (out/'latest.csv').open('w',encoding='utf-8-sig',newline='') as f:
  w=csv.DictWriter(f,fieldnames=sf); w.writeheader(); w.writerows({k:r[k] for k in sf} for r in signals)
 complete=len(audit)==len(cons) and not missing and today!='N/A'
 status={'strategy':'VOLBRK v2 Prime','date':today,'universe':len(cons),'checked':len(audit),'signals':len(signals),'complete':complete,'missing':missing[:100]}
 (out/'latest.json').write_text(json.dumps(status,ensure_ascii=False,indent=2),encoding='utf-8')
 lines=[f'VOLBRK v2 Prime | {today}',f'照合: {len(audit)}/{len(cons)}']
 if complete:
  lines.append('本日は新規買いシグナルなし' if not signals else f'買いシグナル {len(signals)}銘柄')
  for r in signals: lines.append(f"{r['code']} {r['company_name']} | 終値 {r['close']:,.2f} | 出来高倍率 {r['volume_multiple']:.2f}x | 翌営業日寄付き買い候補")
 else: lines.append(f'判定不能：プライム全銘柄の完全照合未完了（不足 {len(missing)}）')
 (out/'latest.txt').write_text('\n'.join(lines)+'\n',encoding='utf-8'); print('\n'.join(lines)); return 0 if complete else 2
if __name__=='__main__': sys.exit(main())
