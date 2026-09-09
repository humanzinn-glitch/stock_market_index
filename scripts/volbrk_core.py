"""Shared, fail-closed signal calculation. No order execution."""
import math
import re
from functools import lru_cache
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import exchange_calendars as xc
import pandas as pd
from exchange_calendars.exchange_calendar_xtks import XTKSExchangeCalendar

JST = ZoneInfo('Asia/Tokyo')
LOOKBACK = 20
VM = 2.5
VERSION = 'VOLBRK-Prime-audit-1'


def ordinary_code(code):
    return bool(re.fullmatch(r'[0-9][0-9A-Z]{3}', str(code)))


JPX_HOLIDAYS = {
    2026: '01-01 01-02 01-03 01-12 02-11 02-23 03-20 04-29 05-03 05-04 05-05 05-06 07-20 08-11 09-21 09-22 09-23 10-12 11-03 11-23 12-31',
    2027: '01-01 01-02 01-03 01-11 02-11 02-23 03-21 03-22 04-29 05-03 05-04 05-05 07-19 08-11 09-20 09-23 10-11 11-03 11-23 12-31',
}
# Official JPX 2026/2027 table, verified 2026-09-09:
# https://www.jpx.co.jp/corporate/about-jpx/calendar/
# The pinned upstream calendar lacks the future equinox / bridge holidays.
class VerifiedTokyoCalendar(XTKSExchangeCalendar):
    @property
    def adhoc_holidays(self):
        return super().adhoc_holidays + [pd.Timestamp(f'{year}-{day}')
            for year, days in JPX_HOLIDAYS.items() for day in days.split()]


@lru_cache(maxsize=1)
def calendar():
    return VerifiedTokyoCalendar(start='2000-01-01', end='2028-01-10')


def target_session(now=None):
    now = now or datetime.now(JST)
    if now.year > 2027:
        raise ValueError('CALENDAR_REQUIRES_UPDATE')
    cal = calendar()
    day = cal.date_to_session(now.date().isoformat(), direction='previous')
    if cal.session_close(day).to_pydatetime() > now.astimezone(timezone.utc):
        day = cal.previous_session(day)
    return day.date().isoformat()


def window_dates(target, now=None):
    if int(target[:4]) > 2027:
        raise ValueError('CALENDAR_REQUIRES_UPDATE')
    cal = calendar()
    if not cal.is_session(target):
        raise ValueError('NON_TRADING_DAY')
    now = now or datetime.now(JST)
    if cal.session_close(target).to_pydatetime() > now.astimezone(timezone.utc):
        raise ValueError('SESSION_NOT_CLOSED')
    start = (datetime.fromisoformat(target) - timedelta(days=90)).date().isoformat()
    return [d.date().isoformat() for d in cal.sessions_in_range(start, target)[-21:]]


def validate_bar(row):
    if any(row.get(k) in (None, '') for k in ('open', 'high', 'low', 'close', 'volume')):
        raise ValueError('MISSING_OHLCV')
    values = {k: float(row[k]) for k in ('open', 'high', 'low', 'close', 'volume')}
    if not all(math.isfinite(v) for v in values.values()):
        raise ValueError('NON_FINITE')
    o, h, l, c, v = (values[k] for k in ('open', 'high', 'low', 'close', 'volume'))
    if min(o, h, l, c) <= 0 or v < 0:
        raise ValueError('INVALID_PRICE_OR_VOLUME')
    if not (l <= min(o, c) <= max(o, c) <= h):
        raise ValueError('INVALID_OHLC')
    if not v.is_integer():
        raise ValueError('NON_INTEGER_VOLUME')
    return values


def evaluate(rows_by_date, dates):
    """Require the exact 21 exchange sessions; never skip holes."""
    if len(dates) != 21:
        raise ValueError('INSUFFICIENT_CALENDAR')
    if not rows_by_date:
        raise ValueError('NO_INPUT_DATA')
    if any(d not in rows_by_date for d in dates):
        raise ValueError('MISSING_TARGET' if dates[-1] not in rows_by_date else 'MISSING_SESSION')
    bars = [validate_bar(rows_by_date[d]) for d in dates]
    prior, cur = bars[:-1], bars[-1]
    av = sum(r['volume'] for r in prior) / LOOKBACK
    if av <= 0:
        raise ValueError('ZERO_AVERAGE_VOLUME')
    ph = max(r['high'] for r in prior)
    multiple = cur['volume'] / av
    return {'close': cur['close'], 'prior_20d_high': ph,
            'volume': cur['volume'], 'prior_20d_avg_volume': av,
            'volume_multiple': multiple,
            'signal': cur['close'] > ph and multiple >= VM}
