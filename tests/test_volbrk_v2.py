#!/usr/bin/env python3
import csv
import json
import subprocess
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCANNER = ROOT / "scripts" / "scan_volbrk_v2.py"


def write_csv(path, fieldnames, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


def build_repo(base: Path):
    constituents = []
    start = date(2026, 8, 19)
    dates = [(start + timedelta(days=i)).isoformat() for i in range(21)]
    target = dates[-1]

    for i in range(225):
        code = str(1000 + i)
        constituents.append({"code": code, "company_name": f"Company {code}"})
        rows = []
        for j, d in enumerate(dates):
            high = 100 + j * 0.1
            close = 99 + j * 0.1
            volume = 1000
            if j == 20 and i == 0:
                close = 120
                high = 121
                volume = 3000
            rows.append({"date": d, "high": high, "close": close, "volume": volume})
        write_csv(
            base / "data" / "prices" / "stooq" / "jp" / f"{code}.csv",
            ["date", "high", "close", "volume"],
            rows,
        )

    write_csv(
        base / "data" / "constituents" / "nikkei225" / "current.csv",
        ["code", "company_name"],
        constituents,
    )
    return target


def run_scan(repo: Path, target: str):
    return subprocess.run(
        [sys.executable, str(SCANNER), "--repo-root", str(repo), "--as-of", target],
        text=True,
        capture_output=True,
        check=False,
    )


def main():
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td)
        target = build_repo(repo)
        result = run_scan(repo, target)
        assert result.returncode == 0, result.stdout + result.stderr
        payload = json.loads((repo / "data/signals/volbrk_v2/latest.json").read_text(encoding="utf-8"))
        assert payload["status"] == "COMPLETE"
        assert payload["checked"] == 225
        assert payload["signals"] == 1
        assert payload["signal_rows"][0]["code"] == "1000"

        broken = repo / "data/prices/stooq/jp/1224.csv"
        with broken.open("r", encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        write_csv(broken, ["date", "high", "close", "volume"], rows[:-1])

        result = run_scan(repo, target)
        assert result.returncode == 2
        payload = json.loads((repo / "data/signals/volbrk_v2/latest.json").read_text(encoding="utf-8"))
        assert payload["status"] == "INCOMPLETE"
        assert payload["message"] == "判定不能：全225銘柄の完全照合未完了"
        assert payload["checked"] == 224

    print("OK")


if __name__ == "__main__":
    main()
