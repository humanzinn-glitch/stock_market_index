# VOLBRK scan audit and corrections — 2026-09-09

## Findings grounded in the existing implementation

Inspected main commit `49e8a1ff64ed8a9b35ccaf5bb9ba501cae9eab43`, its workflow, the latest JSON/CSV audit, and relevant earlier chat records. Prior chat retrieval is partial, not a complete transcript.

- Run `34376432808` was marked successful although only 2/1,556 symbols were checked.
- Both checked symbols, `75505` and `92025`, were bond-type class shares, not ordinary shares.
- Seven nonordinary codes were present in the universe: `25935, 50765, 75505, 92015, 92025, 94345, 94346`. The saved ordinary-share universe therefore has 1,549 codes. This count is a snapshot, not a permanent constant.
- The scheduled chat still required a complete Nikkei 225 scan at 15:45 JST. The repository scanned TSE Prime at 21:05 JST. The prompt ignored the user's later request for partial results with counts.
- Fetch success meant enough rows were present, without verifying that the target session had usable OHLCV. Missing numeric rows were silently discarded by the old fetcher.
- Weekends were skipped but exchange holidays were not. Missing sessions could be replaced by older observations when selecting the last 20 rows.
- The scanner always returned exit code zero, so its incomplete-data failure gate could never run.
- The existing event study exited at a future close, not the agreed future open; it omitted costs, used the upper middle observation as median for even samples, and mixed shortened MAE/MFE windows with full windows.

## Rules preserved and repaired

The signal remains strictly `close > maximum high of the prior 20 exchange sessions` AND `volume / average volume of the prior 20 exchange sessions >= 2.5`. The signal day's high and volume are not in the denominators. No news proxy or relaxed threshold is used.

Earlier Japanese-stock chat records specify next-session-open entry, exit at the open 15 exchange sessions after entry, 10 bp round-trip cost, and at most 10 positions with 10% allocation each. The repaired event-return study implements the entry/exit timing and labels the 10 bp cost as an assumption. It does **not** claim to simulate that portfolio. The Prime expansion is a changed universe; old Nikkei-225 performance cannot be carried over to it.

## New result contract

- `COMPLETE`: every ordinary share is validated; signal count is known for the full stated universe.
- `PARTIAL`: some shares are validated; candidates and zero matches apply only to that subset.
- `UNAVAILABLE`: no publishable validated result; `signals` is JSON `null`, never an invented zero.
- Every target gets an audit row, including missing data and its reason. Nonordinary exclusions get a separate CSV.
- All 21 exact exchange sessions, finite OHLCV, valid price relationships, and nonconflicting duplicates are required. Missing data are not filled.
- Target date is fixed once for the workflow. JSON records rule version, generation time, source commit, run ID, and SHA-256 hashes of the universe and input chunks.
- Incomplete results are still saved and archived, then the job fails. Workflow success no longer means merely that the Python process ran.
- 2026/2027 calendar dates are checked against the JPX official table. The pinned upstream XTKS calendar omitted future equinox/bridge holidays; explicit official holidays repair that. Dates after 2027 require a calendar update.
- GitHub runs retain raw chunks and errors for 30 days in the scan artifact. Run manifests remain in the repository. Data-source availability and exact notification timing are not guaranteed.

## Validation completed locally

Nine regression tests passed: exact thresholds and lookback exclusion; missing session; invalid numbers/OHLC; preclose and holiday handling; all calendar dates in 2026/2027 against the official table; ordinary/alphanumeric codes; partial and zero-data states; conflicting duplicates; actual next-open entry/15-session-open exit and costs. The existing Nikkei-225 test also passed. Both workflow YAML files parsed successfully.

A real-data fetch attempted the first 25 ordinary shares for 2026-09-09. All failed OHLCV validation. A separate Toyota sample contained a 2026-09-09 timestamp, open/high/low/volume, but a null close from both Yahoo chart hosts. Thus a date label is not proof of a valid completed daily bar. The other 1,524 ordinary shares were not locally fetched; no full-universe market result is claimed from that sample. The failure path was executed and returned nonzero with `UNAVAILABLE`, `signals: null`.

This repair improves correctness, traceability, and failure reporting. It does not establish profitable performance or guarantee that the current free vendor will supply all required bars. A source with missing closes must be repaired or replaced before complete daily market judgments and a new portfolio/OOS backtest can be certified.

## Reproduce

```bash
python -m pip install -r requirements-scan.txt
python -m unittest discover -s tests -p test_prime_scan.py
python tests/test_volbrk_v2.py
python scripts/build_tse_prime_universe.py
python scripts/fetch_prime_yahoo_chunk.py --chunk 0 --as-of 2026-09-09
python scripts/scan_prime_volbrk_v2.py --as-of 2026-09-09
```

The final scan command must return 2 when only chunk 0 is present; that is expected partial/unavailable coverage, not a failed installation.

## References

- [Original latest result](https://github.com/humanzinn-glitch/stock_market_index/blob/49e8a1ff64ed8a9b35ccaf5bb9ba501cae9eab43/data/signals/volbrk_v2_prime/latest.json)
- [Original audit rows](https://github.com/humanzinn-glitch/stock_market_index/blob/49e8a1ff64ed8a9b35ccaf5bb9ba501cae9eab43/data/signals/volbrk_v2_prime/audit_latest.csv)
- [Previously successful run](https://github.com/humanzinn-glitch/stock_market_index/actions/runs/34376432808)
- [JPX market hours](https://www.jpx.co.jp/equities/trading/domestic/01.html)
- [JPX holidays for 2026/2027](https://www.jpx.co.jp/corporate/about-jpx/calendar/)
- [GitHub scheduling limitations](https://docs.github.com/en/actions/how-tos/troubleshoot-workflows)
