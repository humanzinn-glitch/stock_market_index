#!/usr/bin/env python3
import csv,json,sys
from datetime import datetime,timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
LOOKBACK=20; VM=2.5; JST=ZoneInfo('Asia/Tokyo')

def expected_latest_session():
 now=datetime.now(JST)
 d=now.date() if now.hour>=16 else now.date()-timedelta(days=1)
 while d.weekday()>=5: d-=timedelta(days=1)
 return d.isoformat()

def main():
 cp=Path('data/constituents/tse_prime/current.csv')
 with cp.open(encoding='utf-8-sig',newline='') as f: cons=list(csv.DictReader(f))
 gathered={}
 for p in Path('runtime/prime_chunks').glob('chunk_*.csv'):
  with p.open(encoding='utf-8-sig',newline='') as f:
   for r in csv.DictReader(f): gathered.setdefault(r['code'],{})[r['date']]=r

 date_sets=[set(gathered.get(c['code'],{})) for c in cons]
 common_dates=set.intersection(*date_sets) if date_sets and all(date_sets) else set()
 common_latest=max(common_dates) if common_dates else 'N/A'
 target=expected_latest_session()

 audit=[];signals=[];missing=[]
 for c in cons:
  bd=gathered.get(c['code'],{}); ds=sorted(bd); idx=ds.index(target) if target in bd else -1
  if idx<LOOKBACK: missing.append(c['code']); continue
  prior=[bd[d] for d in ds[idx-LOOKBACK:idx]]; cur=bd[target]
  ph=max(float(r['high']) for r in prior); av=sum(float(r['volume']) for r in prior)/LOOKBACK; vm=float(cur['volume'])/av if av>0 else 0
  sig=float(cur['close'])>ph and vm>=VM
  row={'trade_date':target,'code':c['code'],'company_name':c['company_name'],'close':float(cur['close']),'prior_20d_high':ph,'volume':int(float(cur['volume'])),'prior_20d_avg_volume':round(av,2),'volume_multiple':round(vm,4),'signal':sig}
  audit.append(row)
  if sig: signals.append(row)
 out=Path('data/signals/volbrk_v2_prime'); out.mkdir(parents=True,exist_ok=True)
 fields=list(audit[0].keys()) if audit else ['trade_date','code']
 with (out/'audit_latest.csv').open('w',encoding='utf-8-sig',newline='') as f:
  w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(audit)
 sf=['trade_date','code','company_name','close','prior_20d_high','volume','prior_20d_avg_volume','volume_multiple']
 with (out/'latest.csv').open('w',encoding='utf-8-sig',newline='') as f:
  w=csv.DictWriter(f,fieldnames=sf); w.writeheader(); w.writerows({k:r[k] for k in sf} for r in signals)
 complete=len(audit)==len(cons) and not missing
 coverage=round(len(audit)/len(cons)*100,2) if cons else 0
 status={'strategy':'VOLBRK v2 Prime','date':target,'universe':len(cons),'checked':len(audit),'coverage_pct':coverage,'signals':len(signals),'complete':complete,'missing_count':len(missing),'missing':missing[:100],'latest_common_source_date':common_latest}
 (out/'latest.json').write_text(json.dumps(status,ensure_ascii=False,indent=2),encoding='utf-8')
 lines=[f'VOLBRK v2 Prime | target {target}',f'判定可能: {len(audit)}/{len(cons)}銘柄（{coverage:.2f}%）',f'判定不能: {len(missing)}銘柄']
 if not complete: lines.append('※取得できた銘柄のみで暫定判定')
 lines.append('判定可能銘柄では新規買いシグナルなし' if not signals else f'買いシグナル {len(signals)}銘柄')
 for r in signals: lines.append(f"{r['code']} {r['company_name']} | 終値 {r['close']:,.2f} | 出来高倍率 {r['volume_multiple']:.2f}x | 翌営業日寄付き買い候補")
 if missing: lines.append('未判定コード: '+', '.join(missing[:20])+(' ...' if len(missing)>20 else ''))
 (out/'latest.txt').write_text('\n'.join(lines)+'\n',encoding='utf-8'); print('\n'.join(lines)); return 0
if __name__=='__main__': sys.exit(main())
