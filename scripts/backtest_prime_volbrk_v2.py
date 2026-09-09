#!/usr/bin/env python3
"""Event-return study; not a portfolio backtest or evidence of profitability."""
import argparse
import csv
import json
import statistics
from collections import Counter
from pathlib import Path
from volbrk_core import VERSION, calendar, evaluate, ordinary_code, validate_bar

HORIZONS = (5,10,15,20)


def returns(entry, exit_price, cost_bps):
    half = cost_bps / 20000
    return (exit_price/entry-1)*100, (exit_price*(1-half)/(entry*(1+half))-1)*100


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--input-dir',default='runtime/prime_chunks')
    ap.add_argument('--out-dir',default='data/backtests/volbrk_v2_prime')
    ap.add_argument('--round-trip-cost-bps',type=float,default=10)
    args=ap.parse_args()
    if not 0 <= args.round_trip_cost_bps < 10000: ap.error('invalid cost')
    with open('data/constituents/tse_prime/current.csv',encoding='utf-8-sig',newline='') as f:
        cons={r['code']:r for r in csv.DictReader(f) if ordinary_code(r['code'])}
    by, conflicts = {}, set()
    for p in sorted(Path(args.input_dir).glob('chunk_*.csv')):
        with p.open(encoding='utf-8-sig',newline='') as f:
            for r in csv.DictReader(f):
                if r.get('code') not in cons: continue
                old=by.setdefault(r['code'],{}).get(r['date'])
                if old and any(old[k]!=r[k] for k in ('open','high','low','close','volume')):
                    conflicts.add(r['code'])
                by[r['code']][r['date']]=r
    trades, excluded = [], Counter()
    cal=calendar()
    for code, data in by.items():
        if code in conflicts:
            excluded['conflicting_symbol']+=1;continue
        sessions=[d.date().isoformat() for d in cal.sessions_in_range(min(data),max(data))]
        for i in range(20,len(sessions)-1):
            try:
                result=evaluate(data,sessions[i-20:i+1])
                if not result['signal']: continue
                entry_i=i+1; entry=validate_bar(data[sessions[entry_i]])['open']
            except (KeyError,ValueError,TypeError):
                excluded['invalid_signal_or_entry_window']+=1;continue
            row={'code':code,'company_name':cons[code]['company_name'],
                 'signal_date':sessions[i],'entry_date':sessions[entry_i],
                 'entry_open':entry,'signal_close':result['close'],
                 'volume_multiple':result['volume_multiple']}
            for h in HORIZONS:
                # Entry D+1 open -> exit D+1+h open. Never exit at that day's close.
                j=entry_i+h
                row[f'exit_{h}d_date']=None
                row[f'gross_{h}d_pct']=None
                row[f'net_{h}d_pct']=None
                if j>=len(sessions): continue
                try:
                    held=[validate_bar(data[sessions[k]]) for k in range(entry_i,j)]
                    exit_bar=validate_bar(data[sessions[j]])
                except (KeyError,ValueError,TypeError):
                    continue
                gross,net=returns(entry,exit_bar['open'],args.round_trip_cost_bps)
                row[f'exit_{h}d_date']=sessions[j]
                row[f'gross_{h}d_pct']=gross;row[f'net_{h}d_pct']=net
                if h==20:
                    row['mae_20d_pct']=(min(b['low'] for b in held)/entry-1)*100
                    row['mfe_20d_pct']=(max(b['high'] for b in held)/entry-1)*100
            trades.append(row)
    out=Path(args.out_dir);out.mkdir(parents=True,exist_ok=True)
    fields=['code','company_name','signal_date','entry_date','entry_open','signal_close','volume_multiple']
    fields += [f'{p}_{h}d_{suffix}' for h in HORIZONS for p,suffix in [('exit','date'),('gross','pct'),('net','pct')]]
    fields += ['mae_20d_pct','mfe_20d_pct']
    with (out/'trades.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(trades)
    summary={'analysis_type':'event_return_study_not_portfolio_backtest','rule_version':VERSION,
             'entry':'next exchange session open','exit':'entry session + h exchange sessions, open',
             'round_trip_cost_bps_assumption':args.round_trip_cost_bps,
             'universe':len(cons),'symbols_with_data':len(by),'trades':len(trades),
             'excluded':dict(excluded),'horizons':{},
             'limitations':['Current constituents: survivorship bias',
                            'Overlapping trades; 10-position/10%-weight portfolio is NOT simulated',
                            'No out-of-sample split or strategy selection validation',
                            'No certification of vendor split/volume adjustments or executable fills']}
    for h in HORIZONS:
        vals=[r[f'net_{h}d_pct'] for r in trades if r[f'net_{h}d_pct'] is not None]
        summary['horizons'][str(h)]={'n':len(vals),'pending_or_invalid':len(trades)-len(vals),
          'win_rate_pct':100*sum(v>0 for v in vals)/len(vals) if vals else None,
          'avg_net_return_pct':statistics.mean(vals) if vals else None,
          'median_net_return_pct':statistics.median(vals) if vals else None}
    (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False,indent=2))

if __name__=='__main__': main()
