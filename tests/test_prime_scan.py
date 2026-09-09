import csv
import json
import sys
import tempfile
import subprocess
import unittest
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parents[1]/'scripts'))
from volbrk_core import JST, JPX_HOLIDAYS, calendar, evaluate, ordinary_code, target_session, window_dates
from scan_prime_volbrk_v2 import scan

NOW = datetime(2026,9,10,8,tzinfo=JST)
DATES = window_dates('2026-09-09', NOW)


def bars():
    rows = {d:dict(open=95,high=100,low=90,close=95,volume=1000) for d in DATES}
    rows[DATES[-1]].update(open=101,high=103,low=100,close=102,volume=2500)
    return rows


class AuditTest(unittest.TestCase):
    def test_exact_threshold_and_exclusion_of_signal_day(self):
        data=bars()
        self.assertTrue(evaluate(data,DATES)['signal'])
        data[DATES[-1]]['volume']=2499
        self.assertFalse(evaluate(data,DATES)['signal'])
        data[DATES[-1]].update(open=100,close=100,volume=2500)
        self.assertFalse(evaluate(data,DATES)['signal'])

    def test_hole_is_not_backfilled_by_older_day(self):
        data=bars(); del data[DATES[3]]; data['2026-08-10']=data[DATES[0]]
        with self.assertRaisesRegex(ValueError,'MISSING_SESSION'): evaluate(data,DATES)

    def test_invalid_numbers(self):
        for field,value in [('close','nan'),('volume',-1),('high',1),('volume','inf')]:
            data=bars(); data[DATES[-1]][field]=value
            with self.subTest(field=field,value=value), self.assertRaises(ValueError): evaluate(data,DATES)

    def test_calendar_and_unclosed(self):
        self.assertEqual(target_session(datetime(2026,9,23,17,tzinfo=JST)), '2026-09-18')
        self.assertEqual(target_session(datetime(2026,9,9,15,29,tzinfo=JST)), '2026-09-08')
        with self.assertRaisesRegex(ValueError,'SESSION_NOT_CLOSED'):
            window_dates('2026-09-09',datetime(2026,9,9,15,29,tzinfo=JST))

    def test_official_calendar_all_dates_2026_2027(self):
        from datetime import date,timedelta
        for year, holidays in JPX_HOLIDAYS.items():
            closed={f'{year}-{d}' for d in holidays.split()}
            d=date(year,1,1)
            while d.year==year:
                expected=d.weekday()<5 and d.isoformat() not in closed
                self.assertEqual(calendar().is_session(d.isoformat()),expected,d.isoformat())
                d+=timedelta(days=1)

    def test_backtest_exits_at_open_after_15_sessions_and_cost(self):
        root_script=Path(__file__).parents[1]/'scripts/backtest_prime_volbrk_v2.py'
        days=[d.date().isoformat() for d in calendar().sessions_in_range('2026-06-01','2026-08-31')]
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); cp=root/'data/constituents/tse_prime';cp.mkdir(parents=True)
            (cp/'current.csv').write_text('code,company_name\n1000,One\n')
            inp=root/'runtime/prime_chunks';inp.mkdir(parents=True)
            rows=[]
            for i,d in enumerate(days):
                row=dict(code='1000',date=d,open=95,high=100,low=90,close=95,volume=1000)
                if i==20: row.update(open=101,high=103,low=100,close=102,volume=2500)
                if i==36: row.update(open=99,close=97)
                rows.append(row)
            with (inp/'chunk_00.csv').open('w',newline='') as f:
                w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
            p=subprocess.run([sys.executable,str(root_script)],cwd=root,capture_output=True,text=True)
            self.assertEqual(p.returncode,0,p.stderr)
            with (root/'data/backtests/volbrk_v2_prime/trades.csv').open(encoding='utf-8-sig') as f: trades=list(csv.DictReader(f))
            self.assertEqual(len(trades),1)
            self.assertEqual(trades[0]['entry_date'],days[21])
            self.assertEqual(trades[0]['exit_15d_date'],days[36])
            self.assertAlmostEqual(float(trades[0]['gross_15d_pct']),(99/95-1)*100)
            self.assertLess(float(trades[0]['net_15d_pct']),float(trades[0]['gross_15d_pct']))

    def test_ordinary_codes(self):
        self.assertTrue(ordinary_code('166A'))
        self.assertFalse(ordinary_code('75505'))

    def test_partial_then_no_data_does_not_reuse_success(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); cp=root/'data/constituents/tse_prime'; cp.mkdir(parents=True)
            (cp/'current.csv').write_text('code,company_name,ticker_tse\n1000,One,1000.T\n1001,Two,1001.T\n75505,Preferred,75505.T\n')
            inp=root/'runtime/prime_chunks'; inp.mkdir(parents=True)
            def write(rows):
                with (inp/'chunk_00.csv').open('w',newline='') as f:
                    w=csv.DictWriter(f,fieldnames=['code','date','open','high','low','close','volume']);w.writeheader();w.writerows(rows)
            rows=[dict(code='1000',date=d,**b) for d,b in bars().items()]
            write(rows)
            self.assertEqual(scan(root,'2026-09-09','runtime/prime_chunks','out',NOW),2)
            result=json.loads((root/'out/latest.json').read_text())
            self.assertEqual((result['status'],result['universe'],result['checked'],result['signals']),('PARTIAL',2,1,1))
            with (root/'out/audit_latest.csv').open(encoding='utf-8-sig') as f:
                audit=list(csv.DictReader(f))
            self.assertEqual(len(audit),2)
            write([])
            self.assertEqual(scan(root,'2026-09-09','runtime/prime_chunks','out',NOW),2)
            result=json.loads((root/'out/latest.json').read_text())
            self.assertIsNone(result['signals']); self.assertEqual(result['status'],'UNAVAILABLE')

    def test_duplicate_conflict(self):
        # Conflicts are explicitly checked before calling the shared evaluator.
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); cp=root/'data/constituents/tse_prime'; cp.mkdir(parents=True)
            (cp/'current.csv').write_text('code,company_name\n1000,One\n')
            inp=root/'runtime/prime_chunks';inp.mkdir(parents=True)
            for n in [0,1]:
                with (inp/f'chunk_{n:02d}.csv').open('w',newline='') as f:
                    w=csv.DictWriter(f,fieldnames=['code','date','open','high','low','close','volume']);w.writeheader()
                    data=bars();data[DATES[-1]]['volume']+=n
                    w.writerows(dict(code='1000',date=d,**b) for d,b in data.items())
            self.assertEqual(scan(root,'2026-09-09','runtime/prime_chunks','out',NOW),2)
            r=json.loads((root/'out/latest.json').read_text())
            self.assertEqual(r['reason_counts'],{'CONFLICTING_DUPLICATE':1})

if __name__=='__main__': unittest.main()
