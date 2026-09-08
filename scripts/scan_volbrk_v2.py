#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
LOOKBACK = 20
VOLUME_MULTIPLE = 2.5
EXPECTED_UNIVERSE = 225


def norm(s: str) -> str:
    return "".join(ch for ch in str(s).strip().lower() if ch.isalnum())


def pick_col(fieldnames, candidates):
    mapping = {norm(c): c for c in fieldnames or []}
    for c in candidates:
        if norm(c) in mapping:
            return mapping[norm(c)]
    return None


def to_float(v):
    if v is None:
        raise ValueError("missing numeric value")
    s = str(v).strip().replace(",", "")
    if s in {"", "-", "nan", "None"}:
        raise ValueError("missing numeric value")
    return float(s)


def code4(v: str) -> str:
    s = "".join(ch for ch in str(v) if ch.isdigit())
    return s[-4:].zfill(4) if s else ""


@dataclass
class Constituent:
    code: str
    name: str


@dataclass
class Signal:
    trade_date: str
    code: str
    company_name: str
    close: float
    prior_20d_high: float
    volume: float
    prior_20d_avg_volume: float
    volume_multiple: float


def load_constituents(path: Path):
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        r = csv.DictReader(f)
        if not r.fieldnames:
            raise RuntimeError(f"No header in constituents CSV: {path}")
        c_code = pick_col(r.fieldnames, ["code", "symbol", "ticker", "ticker_local", "security_code", "local_code"])
        c_name = pick_col(r.fieldnames, ["company_name", "name", "company", "銘柄名", "companyname"])
        if not c_code:
            raise RuntimeError(f"Cannot identify code column: {r.fieldnames}")
        out = []
        seen = set()
        for row in r:
            code = code4(row.get(c_code, ""))
            if not code or code in seen:
                continue
            name = (row.get(c_name, "") if c_name else "").strip() or code
            out.append(Constituent(code, name))
            seen.add(code)
    return out


def find_price_file(repo: Path, code: str):
    preferred = repo / "data" / "prices" / "stooq" / "jp" / f"{code}.csv"
    if preferred.exists():
        return preferred
    candidates = list((repo / "data" / "prices").glob(f"**/jp/{code}.csv"))
    if not candidates:
        candidates = list((repo / "data" / "prices").glob(f"**/{code}.csv"))
    return candidates[0] if candidates else None


def load_price_rows(path: Path):
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        r = csv.DictReader(f)
        if not r.fieldnames:
            raise RuntimeError("no header")
        c_date = pick_col(r.fieldnames, ["trade_date", "date", "datetime"])
        c_high = pick_col(r.fieldnames, ["high"])
        c_close = pick_col(r.fieldnames, ["close", "adj_close", "adjclose"])
        c_volume = pick_col(r.fieldnames, ["volume", "vol"])
        if not all([c_date, c_high, c_close, c_volume]):
            raise RuntimeError(f"required columns missing: {r.fieldnames}")
        rows = []
        for row in r:
            try:
                d = str(row[c_date]).strip()[:10]
                datetime.strptime(d, "%Y-%m-%d")
                rows.append((d, to_float(row[c_high]), to_float(row[c_close]), to_float(row[c_volume])))
            except Exception:
                continue
    rows.sort(key=lambda x: x[0])
    dedup = {x[0]: x for x in rows}
    return [dedup[k] for k in sorted(dedup)]


def write_csv(path: Path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in rows:
            w.writerow(row)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo-root", default=".")
    ap.add_argument("--constituents", default="data/constituents/nikkei225/current.csv")
    ap.add_argument("--as-of", help="YYYY-MM-DD. Default: current JST date")
    ap.add_argument("--expected-universe", type=int, default=EXPECTED_UNIVERSE)
    args = ap.parse_args()

    repo = Path(args.repo_root).resolve()
    constituents_path = repo / args.constituents
    outdir = repo / "data" / "signals" / "volbrk_v2"
    outdir.mkdir(parents=True, exist_ok=True)
    today = args.as_of or datetime.now(JST).date().isoformat()

    status = {
        "strategy": "VOLBRK v2",
        "rule": {
            "lookback_days": LOOKBACK,
            "volume_multiple_min": VOLUME_MULTIPLE,
            "price_rule": "today_close > max(prior_20_trading_days.high)",
        },
        "as_of_requested": today,
        "generated_at_jst": datetime.now(JST).isoformat(timespec="seconds"),
        "expected_universe": args.expected_universe,
        "status": "UNKNOWN",
        "checked": 0,
        "signals": 0,
        "errors": [],
    }

    try:
        cons = load_constituents(constituents_path)
    except Exception as e:
        status["status"] = "ERROR"
        status["errors"].append(f"constituents: {e}")
        (outdir / "latest.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 2

    status["universe_count"] = len(cons)
    if len(cons) != args.expected_universe:
        status["status"] = "INCOMPLETE"
        status["errors"].append(f"universe_count={len(cons)} expected={args.expected_universe}")

    signals = []
    audit = []
    for c in cons:
        pf = find_price_file(repo, c.code)
        if pf is None:
            status["errors"].append(f"{c.code}: price file not found")
            continue
        try:
            rows = load_price_rows(pf)
            idx = next((i for i, x in enumerate(rows) if x[0] == today), None)
            if idx is None:
                latest = rows[-1][0] if rows else "NONE"
                status["errors"].append(f"{c.code}: no row for {today}; latest={latest}")
                continue
            if idx < LOOKBACK:
                status["errors"].append(f"{c.code}: fewer than {LOOKBACK} prior trading days")
                continue
            prior = rows[idx - LOOKBACK : idx]
            cur = rows[idx]
            prior_high = max(x[1] for x in prior)
            avg_vol = sum(x[3] for x in prior) / LOOKBACK
            if avg_vol <= 0:
                status["errors"].append(f"{c.code}: prior average volume <= 0")
                continue
            vol_mult = cur[3] / avg_vol
            price_break = cur[2] > prior_high
            volume_break = vol_mult >= VOLUME_MULTIPLE
            audit.append(
                {
                    "trade_date": today,
                    "code": c.code,
                    "company_name": c.name,
                    "close": cur[2],
                    "prior_20d_high": prior_high,
                    "volume": cur[3],
                    "prior_20d_avg_volume": round(avg_vol, 4),
                    "volume_multiple": round(vol_mult, 6),
                    "price_break": price_break,
                    "volume_break": volume_break,
                    "signal": price_break and volume_break,
                    "source_file": str(pf.relative_to(repo)),
                }
            )
            if price_break and volume_break:
                signals.append(Signal(today, c.code, c.name, cur[2], prior_high, cur[3], avg_vol, vol_mult))
        except Exception as e:
            status["errors"].append(f"{c.code}: {type(e).__name__}: {e}")

    status["checked"] = len(audit)
    status["signals"] = len(signals)
    complete = len(cons) == args.expected_universe and len(audit) == args.expected_universe and not status["errors"]
    if complete:
        status["status"] = "COMPLETE"
        status["message"] = "本日は新規買いシグナルなし" if not signals else f"VOLBRK v2 買いシグナル {len(signals)}銘柄"
    else:
        status["status"] = "INCOMPLETE"
        status["message"] = "判定不能：全225銘柄の完全照合未完了"

    fields = [
        "trade_date",
        "code",
        "company_name",
        "close",
        "prior_20d_high",
        "volume",
        "prior_20d_avg_volume",
        "volume_multiple",
    ]
    signal_rows = []
    for s in signals:
        d = asdict(s)
        d["prior_20d_avg_volume"] = round(d["prior_20d_avg_volume"], 4)
        d["volume_multiple"] = round(d["volume_multiple"], 4)
        signal_rows.append(d)
    write_csv(outdir / "latest.csv", signal_rows, fields)
    write_csv(outdir / "audit_latest.csv", audit, list(audit[0].keys()) if audit else ["trade_date", "code"])

    hist = outdir / "history.csv"
    if complete and signals:
        existing = []
        if hist.exists():
            with hist.open("r", encoding="utf-8-sig", newline="") as f:
                existing = list(csv.DictReader(f))
        keys = {(r.get("trade_date"), r.get("code")) for r in existing}
        for r in signal_rows:
            if (r["trade_date"], r["code"]) not in keys:
                existing.append({k: r[k] for k in fields})
        write_csv(hist, existing, fields)
    elif not hist.exists():
        write_csv(hist, [], fields)

    (outdir / "latest.json").write_text(
        json.dumps({**status, "signal_rows": signal_rows}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [f"VOLBRK v2 | {today}", status["message"], f"照合: {len(audit)}/{args.expected_universe}"]
    if complete and signals:
        lines.append("")
        for r in signal_rows:
            lines.append(
                f"{r['code']} {r['company_name']} | 終値 {r['close']:,.2f} | 出来高倍率 {r['volume_multiple']:.2f}x | 翌営業日寄付き買い候補"
            )
    if status["errors"]:
        lines.extend(["", f"取得/検証エラー: {len(status['errors'])}件"] + status["errors"][:20])
    (outdir / "latest.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0 if complete else 2


if __name__ == "__main__":
    sys.exit(main())
