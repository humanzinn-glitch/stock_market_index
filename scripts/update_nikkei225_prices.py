#!/usr/bin/env python3
"""Update daily OHLCV for the fixed Nikkei 225 constituent universe.

Daily operation is intentionally lightweight: instead of calling Yahoo's chart
endpoint once for every stock, this script obtains a Yahoo cookie/crumb once and
requests multi-symbol quotes in a few batches. The existing per-symbol CSV is
then updated with the target day's final OHLCV row.

The historical chart endpoint is used only as a bootstrap for a constituent
whose local CSV is missing or has fewer than 21 rows. This keeps the normal
weekday run to only a handful of Yahoo requests and avoids the 225-request rate
limit pattern that caused HTTP 429 errors on GitHub-hosted runners.

Inputs:
- data/constituents/nikkei225/current.csv

Outputs:
- data/prices/stooq/jp/<code>.csv   (legacy path retained for compatibility)
- data/panels/nikkei225_current_constituents_latest.csv
- data/panels/nikkei225_current_constituents_close_wide_260d.csv
- runtime/update_nikkei225_prices_status.json
"""
from __future__ import annotations

import argparse
import csv
import http.cookiejar
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from common_market_io import (
    csv_text,
    ensure_dir,
    log,
    now_iso,
    write_status,
    write_text_if_changed,
)

TOKYO = ZoneInfo("Asia/Tokyo")
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
YAHOO_COOKIE_URL = "https://fc.yahoo.com"
YAHOO_CRUMB_URL = "https://query1.finance.yahoo.com/v1/test/getcrumb"
YAHOO_QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=3mo&interval=1d&events=history&includeAdjustedClose=false"

PRICE_FIELDNAMES = [
    "code",
    "ticker_tse",
    "symbol_stooq",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "source",
    "fetched_at",
]

LATEST_PANEL_FIELDNAMES = [
    "code",
    "ticker_tse",
    "company_name",
    "sector",
    "date",
    "close",
    "volume",
    "price_file",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update Nikkei 225 constituent prices")
    parser.add_argument("--repo-root", default=".", help="Repository root")
    parser.add_argument("--constituents-csv", default="data/constituents/nikkei225/current.csv")
    parser.add_argument("--as-of", default="", help="Target trading date YYYY-MM-DD; default is current JST date")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--batch-size", type=int, default=75)
    parser.add_argument("--batch-sleep-seconds", type=float, default=1.0)
    parser.add_argument("--rate-limit-cooldown-seconds", type=float, default=30.0)
    parser.add_argument("--max-symbols", type=int, default=0, help="For testing; 0 means all")
    return parser.parse_args()


def read_constituents(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"Constituents file not found: {path}")
    with path.open("r", newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    required = {"code", "ticker_tse", "symbol_stooq", "company_name", "sector"}
    if not rows:
        raise ValueError("Constituents CSV is empty")
    missing = required - set(rows[0].keys())
    if missing:
        raise ValueError(f"Constituents CSV missing columns: {sorted(missing)}")
    rows = [r for r in rows if r.get("code") and r.get("ticker_tse") and r.get("symbol_stooq")]
    if not rows:
        raise ValueError("Constituents CSV has no usable rows")
    return rows


def parse_number(value: object) -> str:
    if value is None or value == "":
        return ""
    return f"{float(value):.2f}"


def parse_volume(value: object) -> str:
    if value is None or value == "":
        return ""
    return str(int(float(value)))


def make_yahoo_opener(timeout: int) -> tuple[urllib.request.OpenerDirector, str]:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [("User-Agent", USER_AGENT), ("Accept", "application/json,text/plain,*/*")]

    # fc.yahoo.com commonly returns HTTP 404 while still setting the A3 cookie.
    try:
        opener.open(YAHOO_COOKIE_URL, timeout=timeout).read()
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise

    with opener.open(YAHOO_CRUMB_URL, timeout=timeout) as response:
        crumb = response.read().decode("utf-8", errors="replace").strip()
    if not crumb or crumb.startswith("{") or "Too Many Requests" in crumb:
        raise RuntimeError(f"Yahoo crumb acquisition failed: {crumb[:120]!r}")
    return opener, crumb


def fetch_json_with_retry(
    opener: urllib.request.OpenerDirector,
    url: str,
    *,
    timeout: int,
    retries: int,
    cooldown: float,
) -> dict:
    last_exc: Exception | None = None
    for attempt in range(1, retries + 2):
        try:
            with opener.open(url, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code == 429 and attempt <= retries:
                log(f"Yahoo HTTP 429; cooldown {cooldown:.0f}s before retry {attempt + 1}/{retries + 1}")
                time.sleep(cooldown)
                continue
            raise
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt <= retries:
                time.sleep(min(5.0, 1.5 * attempt))
                continue
            raise
    raise RuntimeError(f"Yahoo request failed: {last_exc}")


def quote_to_price_row(
    quote: dict,
    *,
    constituent: dict[str, str],
    target_date: str,
    fetched_at: str,
) -> dict[str, str]:
    symbol = constituent["ticker_tse"]
    market_time = quote.get("regularMarketTime")
    if market_time is None:
        raise ValueError(f"{symbol}: regularMarketTime missing")
    quote_date = datetime.fromtimestamp(int(market_time), tz=timezone.utc).astimezone(TOKYO).strftime("%Y-%m-%d")
    if quote_date != target_date:
        raise ValueError(f"{symbol}: quote date {quote_date} != target {target_date}")

    values = {
        "open": quote.get("regularMarketOpen"),
        "high": quote.get("regularMarketDayHigh"),
        "low": quote.get("regularMarketDayLow"),
        "close": quote.get("regularMarketPrice"),
        "volume": quote.get("regularMarketVolume"),
    }
    missing = [key for key, value in values.items() if value is None]
    if missing:
        raise ValueError(f"{symbol}: missing quote fields {missing}")
    if float(values["volume"]) <= 0:
        raise ValueError(f"{symbol}: non-positive regularMarketVolume")

    return {
        "code": constituent["code"],
        "ticker_tse": symbol,
        "symbol_stooq": constituent["symbol_stooq"],
        "date": target_date,
        "open": parse_number(values["open"]),
        "high": parse_number(values["high"]),
        "low": parse_number(values["low"]),
        "close": parse_number(values["close"]),
        "volume": parse_volume(values["volume"]),
        "source": f"{YAHOO_QUOTE_URL}?symbols={symbol}",
        "fetched_at": fetched_at,
    }


def fetch_bulk_quotes(
    constituents: list[dict[str, str]],
    *,
    target_date: str,
    fetched_at: str,
    timeout: int,
    retries: int,
    batch_size: int,
    batch_sleep_seconds: float,
    cooldown: float,
) -> tuple[dict[str, dict[str, str]], list[dict[str, str]]]:
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    opener, crumb = make_yahoo_opener(timeout)
    by_symbol = {row["ticker_tse"]: row for row in constituents}
    out: dict[str, dict[str, str]] = {}
    failures: list[dict[str, str]] = []

    for start in range(0, len(constituents), batch_size):
        batch = constituents[start : start + batch_size]
        symbols = [row["ticker_tse"] for row in batch]
        query = urllib.parse.urlencode(
            {"symbols": ",".join(symbols), "crumb": crumb, "formatted": "false"}
        )
        url = f"{YAHOO_QUOTE_URL}?{query}"
        log(f"bulk quote batch {start // batch_size + 1}: {len(symbols)} symbols")
        payload = fetch_json_with_retry(
            opener,
            url,
            timeout=timeout,
            retries=retries,
            cooldown=cooldown,
        )
        results = payload.get("quoteResponse", {}).get("result") or []
        returned = {str(item.get("symbol", "")): item for item in results}

        for symbol in symbols:
            constituent = by_symbol[symbol]
            quote = returned.get(symbol)
            if quote is None:
                failures.append({"code": constituent["code"], "ticker_tse": symbol, "error": "symbol missing from Yahoo bulk quote"})
                continue
            try:
                out[constituent["code"]] = quote_to_price_row(
                    quote,
                    constituent=constituent,
                    target_date=target_date,
                    fetched_at=fetched_at,
                )
            except Exception as exc:  # noqa: BLE001
                failures.append({"code": constituent["code"], "ticker_tse": symbol, "error": str(exc)})

        if start + batch_size < len(constituents):
            time.sleep(batch_sleep_seconds)

    return out, failures


def normalize_chart_json(
    payload: dict,
    *,
    constituent: dict[str, str],
    fetched_at: str,
) -> list[dict[str, str]]:
    result = payload.get("chart", {}).get("result")
    if not result:
        raise ValueError(f"Yahoo chart payload empty for {constituent['ticker_tse']}")
    chart = result[0]
    timestamps = chart.get("timestamp") or []
    quote_rows = chart.get("indicators", {}).get("quote") or []
    if not timestamps or not quote_rows:
        raise ValueError(f"Yahoo chart payload missing quote rows for {constituent['ticker_tse']}")
    quote = quote_rows[0]
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []
    rows: list[dict[str, str]] = []
    for idx, ts in enumerate(timestamps):
        vals = [
            opens[idx] if idx < len(opens) else None,
            highs[idx] if idx < len(highs) else None,
            lows[idx] if idx < len(lows) else None,
            closes[idx] if idx < len(closes) else None,
            volumes[idx] if idx < len(volumes) else None,
        ]
        if any(value is None for value in vals):
            continue
        date = datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(TOKYO).strftime("%Y-%m-%d")
        rows.append(
            {
                "code": constituent["code"],
                "ticker_tse": constituent["ticker_tse"],
                "symbol_stooq": constituent["symbol_stooq"],
                "date": date,
                "open": parse_number(vals[0]),
                "high": parse_number(vals[1]),
                "low": parse_number(vals[2]),
                "close": parse_number(vals[3]),
                "volume": parse_volume(vals[4]),
                "source": YAHOO_CHART_URL.format(symbol=constituent["ticker_tse"]),
                "fetched_at": fetched_at,
            }
        )
    rows.sort(key=lambda row: row["date"])
    return rows


def bootstrap_history(
    opener: urllib.request.OpenerDirector,
    constituent: dict[str, str],
    *,
    fetched_at: str,
    timeout: int,
    retries: int,
    cooldown: float,
) -> list[dict[str, str]]:
    url = YAHOO_CHART_URL.format(symbol=urllib.parse.quote(constituent["ticker_tse"], safe=""))
    payload = fetch_json_with_retry(opener, url, timeout=timeout, retries=retries, cooldown=cooldown)
    rows = normalize_chart_json(payload, constituent=constituent, fetched_at=fetched_at)
    if len(rows) < 21:
        raise ValueError(f"{constituent['ticker_tse']}: bootstrap returned only {len(rows)} daily rows")
    return rows


def read_price_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    rows = [row for row in rows if row.get("date")]
    rows.sort(key=lambda row: row["date"])
    return rows


def merge_daily_row(existing: list[dict[str, str]], daily: dict[str, str]) -> list[dict[str, str]]:
    by_date = {row["date"]: row for row in existing if row.get("date")}
    by_date[daily["date"]] = daily
    return [by_date[date] for date in sorted(by_date)]


def build_latest_panel(
    constituents: list[dict[str, str]],
    file_map: dict[str, Path],
    repo_root: Path,
) -> list[dict[str, str]]:
    constituent_map = {row["code"]: row for row in constituents}
    latest_rows: list[dict[str, str]] = []
    for code, path in sorted(file_map.items()):
        rows = read_price_rows(path)
        if not rows:
            continue
        last = rows[-1]
        meta = constituent_map[code]
        latest_rows.append(
            {
                "code": code,
                "ticker_tse": meta["ticker_tse"],
                "company_name": meta["company_name"],
                "sector": meta["sector"],
                "date": last["date"],
                "close": last["close"],
                "volume": last["volume"],
                "price_file": str(path.relative_to(repo_root)),
            }
        )
    return latest_rows


def build_close_wide_recent(
    file_map: dict[str, Path],
    lookback_days: int = 260,
) -> tuple[list[str], list[dict[str, str]]]:
    closes_by_date: dict[str, dict[str, str]] = defaultdict(dict)
    all_codes = sorted(file_map)
    all_dates: set[str] = set()
    for code, path in file_map.items():
        for rec in read_price_rows(path)[-lookback_days:]:
            closes_by_date[rec["date"]][code] = rec["close"]
            all_dates.add(rec["date"])
    fields = ["date"] + all_codes
    rows: list[dict[str, str]] = []
    for date in sorted(all_dates):
        row = {"date": date}
        for code in all_codes:
            row[code] = closes_by_date[date].get(code, "")
        rows.append(row)
    return fields, rows


def main() -> int:
    args = parse_args()
    repo_root = Path(args.repo_root).resolve()
    constituents_path = repo_root / args.constituents_csv
    status_path = repo_root / "runtime/update_nikkei225_prices_status.json"
    prices_root = repo_root / "data/prices/stooq/jp"
    latest_panel_path = repo_root / "data/panels/nikkei225_current_constituents_latest.csv"
    close_wide_path = repo_root / "data/panels/nikkei225_current_constituents_close_wide_260d.csv"
    ensure_dir(prices_root)
    ensure_dir(latest_panel_path.parent)
    fetched_at = now_iso()
    target_date = args.as_of or datetime.now(TOKYO).strftime("%Y-%m-%d")

    try:
        datetime.strptime(target_date, "%Y-%m-%d")
        constituents = read_constituents(constituents_path)
        if args.max_symbols > 0:
            constituents = constituents[: args.max_symbols]

        quote_rows, quote_failures = fetch_bulk_quotes(
            constituents,
            target_date=target_date,
            fetched_at=fetched_at,
            timeout=args.timeout,
            retries=args.retries,
            batch_size=args.batch_size,
            batch_sleep_seconds=args.batch_sleep_seconds,
            cooldown=args.rate_limit_cooldown_seconds,
        )

        # Reuse the authenticated opener for rare history bootstraps.
        bootstrap_opener, _ = make_yahoo_opener(args.timeout)
        file_map: dict[str, Path] = {}
        failures = list(quote_failures)
        changed_files = 0
        bootstrapped = 0

        failed_codes = {item["code"] for item in quote_failures}
        for idx, constituent in enumerate(constituents, start=1):
            code = constituent["code"]
            path = prices_root / f"{code}.csv"
            existing = read_price_rows(path)

            if len(existing) < 21:
                try:
                    log(f"[{idx}/{len(constituents)}] bootstrap history for {constituent['ticker_tse']}")
                    existing = bootstrap_history(
                        bootstrap_opener,
                        constituent,
                        fetched_at=fetched_at,
                        timeout=args.timeout,
                        retries=args.retries,
                        cooldown=args.rate_limit_cooldown_seconds,
                    )
                    bootstrapped += 1
                except Exception as exc:  # noqa: BLE001
                    failures.append({"code": code, "ticker_tse": constituent["ticker_tse"], "error": f"bootstrap failed: {exc}"})
                    failed_codes.add(code)
                    continue

            daily = quote_rows.get(code)
            if daily is None:
                failed_codes.add(code)
                continue

            merged = merge_daily_row(existing, daily)
            if len([row for row in merged if row["date"] < target_date]) < 20:
                failures.append({"code": code, "ticker_tse": constituent["ticker_tse"], "error": "fewer than 20 prior trading rows after merge"})
                failed_codes.add(code)
                continue

            changed = write_text_if_changed(path, csv_text(merged, PRICE_FIELDNAMES))
            if changed:
                changed_files += 1
            file_map[code] = path

        complete = len(file_map) == len(constituents) and not failed_codes
        if not complete:
            missing = sorted(set(row["code"] for row in constituents) - set(file_map))
            raise RuntimeError(
                f"Daily price update incomplete for {target_date}: "
                f"updated={len(file_map)}/{len(constituents)}, missing={len(missing)}"
            )

        latest_panel_rows = build_latest_panel(constituents, file_map, repo_root)
        latest_panel_changed = write_text_if_changed(
            latest_panel_path, csv_text(latest_panel_rows, LATEST_PANEL_FIELDNAMES)
        )
        close_fields, close_rows = build_close_wide_recent(file_map)
        close_wide_changed = write_text_if_changed(
            close_wide_path, csv_text(close_rows, close_fields)
        )

        status = {
            "ok": True,
            "fetched_at": fetched_at,
            "target_date": target_date,
            "symbols_requested": len(constituents),
            "symbols_updated": len(file_map),
            "symbols_failed": 0,
            "quote_batches": (len(constituents) + args.batch_size - 1) // args.batch_size,
            "history_bootstraps": bootstrapped,
            "price_files_changed": changed_files,
            "latest_panel_changed": latest_panel_changed,
            "close_wide_changed": close_wide_changed,
            "price_provider": "yahoo_bulk_quote",
        }
        write_status(status_path, status)
        log(
            f"prices COMPLETE {len(file_map)}/{len(constituents)} for {target_date}; "
            f"batches={status['quote_batches']} bootstraps={bootstrapped} changed={changed_files}"
        )
        return 0

    except Exception as exc:  # noqa: BLE001
        write_status(
            status_path,
            {
                "ok": False,
                "fetched_at": fetched_at,
                "target_date": target_date,
                "error": str(exc),
                "price_provider": "yahoo_bulk_quote",
            },
        )
        log(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
