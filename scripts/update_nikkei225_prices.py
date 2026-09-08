#!/usr/bin/env python3
"""Update Nikkei 225 daily OHLCV from Stooq using an API key.

The Yahoo endpoints are intentionally not used here because GitHub-hosted
runners are frequently rate-limited (HTTP 429). Each Stooq request downloads a
small recent window, merges it into the existing per-symbol history, and the
run is considered complete only when all 225 constituents contain the exact
target-date row plus at least 20 earlier trading sessions.
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from common_market_io import csv_text, ensure_dir, log, now_iso, write_status, write_text_if_changed

TOKYO = ZoneInfo("Asia/Tokyo")
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"
STOOQ_URL = "https://stooq.com/q/d/l/"
PRICE_FIELDNAMES = ["code","ticker_tse","symbol_stooq","date","open","high","low","close","volume","source","fetched_at"]
LATEST_PANEL_FIELDNAMES = ["code","ticker_tse","company_name","sector","date","close","volume","price_file"]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Update Nikkei 225 constituent prices from Stooq")
    p.add_argument("--repo-root", default=".")
    p.add_argument("--constituents-csv", default="data/constituents/nikkei225/current.csv")
    p.add_argument("--as-of", default="", help="Target date YYYY-MM-DD; default current JST date")
    p.add_argument("--timeout", type=int, default=20)
    p.add_argument("--retries", type=int, default=2)
    p.add_argument("--sleep-seconds", type=float, default=0.25)
    p.add_argument("--lookback-calendar-days", type=int, default=60)
    p.add_argument("--max-symbols", type=int, default=0)
    return p.parse_args()


def read_constituents(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    required = {"code","ticker_tse","symbol_stooq","company_name","sector"}
    if not rows or not required.issubset(rows[0].keys()):
        raise ValueError("Constituents CSV is empty or missing required columns")
    return [r for r in rows if r.get("code") and r.get("symbol_stooq")]


def read_price_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8-sig") as f:
        rows = [r for r in csv.DictReader(f) if r.get("date")]
    return sorted(rows, key=lambda r: r["date"])


def parse_num(v: str) -> str:
    return f"{float(v):.2f}"


def parse_vol(v: str) -> str:
    return str(int(float(v)))


def fetch_recent(constituent: dict[str, str], *, target_date: str, days: int, api_key: str,
                 fetched_at: str, timeout: int, retries: int) -> list[dict[str, str]]:
    target = datetime.strptime(target_date, "%Y-%m-%d").date()
    start = target - timedelta(days=days)
    symbol = constituent["symbol_stooq"].lower()
    params = urllib.parse.urlencode({
        "s": symbol,
        "d1": start.strftime("%Y%m%d"),
        "d2": target.strftime("%Y%m%d"),
        "i": "d",
        "apikey": api_key,
    })
    url = f"{STOOQ_URL}?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/csv,*/*"})
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            first = raw.splitlines()[0] if raw.splitlines() else ""
            if "Exceeded the daily hits limit" in raw:
                raise RuntimeError("Stooq daily hits limit exceeded")
            if "requires JavaScript" in raw or not first.startswith("Date,Open,High,Low,Close,Volume"):
                raise RuntimeError(f"Unexpected Stooq response: {first[:120]}")
            out: list[dict[str, str]] = []
            for rec in csv.DictReader(io.StringIO(raw)):
                if not rec.get("Date") or not rec.get("Close") or rec.get("Close") == "-":
                    continue
                if any(not rec.get(k) or rec.get(k) == "-" for k in ("Open","High","Low","Volume")):
                    continue
                out.append({
                    "code": constituent["code"],
                    "ticker_tse": constituent["ticker_tse"],
                    "symbol_stooq": constituent["symbol_stooq"],
                    "date": rec["Date"],
                    "open": parse_num(rec["Open"]),
                    "high": parse_num(rec["High"]),
                    "low": parse_num(rec["Low"]),
                    "close": parse_num(rec["Close"]),
                    "volume": parse_vol(rec["Volume"]),
                    "source": f"stooq:{symbol}",
                    "fetched_at": fetched_at,
                })
            if not out:
                raise RuntimeError("No Stooq price rows parsed")
            return sorted(out, key=lambda r: r["date"])
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, RuntimeError) as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(2 ** attempt)
            else:
                raise RuntimeError(str(last_exc)) from exc
    raise RuntimeError(str(last_exc))


def merge_rows(existing: list[dict[str, str]], incoming: list[dict[str, str]]) -> list[dict[str, str]]:
    by_date = {r["date"]: r for r in existing if r.get("date")}
    for r in incoming:
        by_date[r["date"]] = r
    return [by_date[d] for d in sorted(by_date)]


def build_latest_panel(constituents, file_map, repo_root):
    meta = {r["code"]: r for r in constituents}
    out = []
    for code, path in sorted(file_map.items()):
        rows = read_price_rows(path)
        if not rows:
            continue
        last = rows[-1]
        m = meta[code]
        out.append({"code":code,"ticker_tse":m["ticker_tse"],"company_name":m["company_name"],"sector":m["sector"],
                    "date":last["date"],"close":last["close"],"volume":last["volume"],"price_file":str(path.relative_to(repo_root))})
    return out


def build_close_wide_recent(file_map, lookback_days=260):
    closes = defaultdict(dict)
    dates = set()
    codes = sorted(file_map)
    for code, path in file_map.items():
        for r in read_price_rows(path)[-lookback_days:]:
            closes[r["date"]][code] = r["close"]
            dates.add(r["date"])
    fields = ["date"] + codes
    rows = []
    for d in sorted(dates):
        row = {"date": d}
        for code in codes:
            row[code] = closes[d].get(code, "")
        rows.append(row)
    return fields, rows


def main() -> int:
    args = parse_args()
    root = Path(args.repo_root).resolve()
    target_date = args.as_of or datetime.now(TOKYO).strftime("%Y-%m-%d")
    fetched_at = now_iso()
    status_path = root / "runtime/update_nikkei225_prices_status.json"
    prices_root = root / "data/prices/stooq/jp"
    latest_path = root / "data/panels/nikkei225_current_constituents_latest.csv"
    wide_path = root / "data/panels/nikkei225_current_constituents_close_wide_260d.csv"
    ensure_dir(prices_root); ensure_dir(latest_path.parent)

    api_key = os.environ.get("STOOQ_APIKEY", "").strip()
    if not api_key:
        write_status(status_path, {"ok":False,"target_date":target_date,"error":"STOOQ_APIKEY secret is not configured","price_provider":"stooq_apikey"})
        log("ERROR: STOOQ_APIKEY secret is not configured")
        return 1

    try:
        constituents = read_constituents(root / args.constituents_csv)
        if args.max_symbols > 0:
            constituents = constituents[:args.max_symbols]
        if args.max_symbols == 0 and len(constituents) != 225:
            raise RuntimeError(f"Expected 225 constituents, found {len(constituents)}")

        file_map: dict[str, Path] = {}
        failures = []
        changed = 0
        for idx, c in enumerate(constituents, 1):
            code = c["code"]
            log(f"[{idx}/{len(constituents)}] Stooq {c['symbol_stooq']}")
            try:
                incoming = fetch_recent(c, target_date=target_date, days=args.lookback_calendar_days, api_key=api_key,
                                        fetched_at=fetched_at, timeout=args.timeout, retries=args.retries)
                path = prices_root / f"{code}.csv"
                merged = merge_rows(read_price_rows(path), incoming)
                target_rows = [r for r in merged if r["date"] == target_date]
                prior = [r for r in merged if r["date"] < target_date]
                if len(target_rows) != 1:
                    raise RuntimeError(f"target-date row missing for {target_date}")
                if len(prior) < 20:
                    raise RuntimeError(f"only {len(prior)} prior rows")
                if write_text_if_changed(path, csv_text(merged, PRICE_FIELDNAMES)):
                    changed += 1
                file_map[code] = path
            except Exception as exc:  # noqa: BLE001
                failures.append({"code":code,"symbol":c["symbol_stooq"],"error":str(exc)})
            time.sleep(args.sleep_seconds)

        if failures or len(file_map) != len(constituents):
            raise RuntimeError(f"Daily price update incomplete: {len(file_map)}/{len(constituents)} complete; failures={len(failures)}")

        latest_changed = write_text_if_changed(latest_path, csv_text(build_latest_panel(constituents, file_map, root), LATEST_PANEL_FIELDNAMES))
        fields, wide_rows = build_close_wide_recent(file_map)
        wide_changed = write_text_if_changed(wide_path, csv_text(wide_rows, fields))
        write_status(status_path, {"ok":True,"fetched_at":fetched_at,"target_date":target_date,"symbols_requested":len(constituents),
                                  "symbols_updated":len(file_map),"symbols_failed":0,"price_files_changed":changed,
                                  "latest_panel_changed":latest_changed,"close_wide_changed":wide_changed,"price_provider":"stooq_apikey"})
        log(f"prices COMPLETE {len(file_map)}/{len(constituents)} for {target_date}; changed={changed}")
        return 0
    except Exception as exc:  # noqa: BLE001
        write_status(status_path, {"ok":False,"fetched_at":fetched_at,"target_date":target_date,"error":str(exc),
                                  "price_provider":"stooq_apikey","failures":failures[:25] if 'failures' in locals() else []})
        log(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
