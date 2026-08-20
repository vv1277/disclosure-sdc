"""거래일 달력과 이벤트일 계산.

이벤트일 규칙 (Phase 1 결정 1)
    이벤트일 = 접수일 + 1 거래일

  접수'시각'은 수집하지 않는다. 주 예측 horizon 이 t+1~t+120 거래일(6개월)이라
  시각 단위 차이가 무의미하고, 진입 규칙이 이미 '접수일 +1 거래일 시가' 로
  고정되어 있어 시각을 알아도 이벤트일이 바뀌지 않기 때문이다.
  대신 이 규칙을 코드에 강제하고 단위 테스트로 고정한다.

거래일 소스
  KOSPI 지수(KS11) 의 거래일이 곧 KRX 거래일이다. FinanceDataReader 로 받으며
  KRX 로그인이 필요 없다. 한 번 받아 parquet 으로 캐시한다.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from src.utils.config import PROJECT_ROOT

log = logging.getLogger(__name__)

CACHE = PROJECT_ROOT / "data" / "meta" / "trading_days.parquet"
_INDEX = "KS11"          # KOSPI 종합지수
_CACHED: pd.DatetimeIndex | None = None


def _to_ts(d: str | date | datetime | pd.Timestamp) -> pd.Timestamp:
    """'20250313', '2025-03-13', date, datetime 을 모두 받는다."""
    if isinstance(d, pd.Timestamp):
        return d.normalize()
    if isinstance(d, (datetime, date)):
        return pd.Timestamp(d).normalize()
    s = str(d).strip()
    if s.isdigit() and len(s) == 8:
        return pd.Timestamp(f"{s[:4]}-{s[4:6]}-{s[6:]}")
    return pd.Timestamp(s).normalize()


def build_calendar(start: str = "2014-01-01", end: str | None = None,
                   *, force: bool = False) -> pd.DatetimeIndex:
    """거래일 달력을 만들고 캐시한다."""
    if not force and CACHE.exists():
        days = pd.to_datetime(pd.read_parquet(CACHE)["date"])
        return pd.DatetimeIndex(days).sort_values()

    import FinanceDataReader as fdr
    end = end or (pd.Timestamp.today() + pd.DateOffset(years=1)).strftime("%Y-%m-%d")
    df = fdr.DataReader(_INDEX, start, end)
    days = pd.DatetimeIndex(df.index).normalize().sort_values()
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"date": days}).to_parquet(CACHE, index=False)
    log.info("거래일 달력 %d일 (%s ~ %s) -> %s", len(days),
             days[0].date(), days[-1].date(), CACHE)
    return days


def trading_days(force: bool = False) -> pd.DatetimeIndex:
    global _CACHED
    if _CACHED is None or force:
        _CACHED = build_calendar(force=force)
    return _CACHED


def set_calendar(days) -> None:
    """테스트용 주입. 실제 달력 대신 임의의 거래일 집합을 쓴다."""
    global _CACHED
    _CACHED = pd.DatetimeIndex(pd.to_datetime(list(days))).normalize().sort_values()


def is_trading_day(d, calendar: pd.DatetimeIndex | None = None) -> bool:
    cal = trading_days() if calendar is None else calendar
    return _to_ts(d) in set(cal)


def next_trading_day(d, calendar: pd.DatetimeIndex | None = None) -> pd.Timestamp:
    """d **이후** 첫 거래일. d 자체가 거래일이어도 그 다음 날을 돌려준다."""
    cal = trading_days() if calendar is None else calendar
    ts = _to_ts(d)
    pos = cal.searchsorted(ts, side="right")     # ts 보다 큰 첫 위치
    if pos >= len(cal):
        raise ValueError(
            f"{ts.date()} 이후의 거래일이 달력에 없습니다. "
            f"달력 마지막 날짜는 {cal[-1].date()} 입니다. "
            f"build_calendar(force=True) 로 갱신하세요.")
    return cal[pos]


def event_date(rcept_dt, calendar: pd.DatetimeIndex | None = None) -> pd.Timestamp:
    """이벤트일 = 접수일 + 1 거래일.

    접수일이 비거래일(주말·공휴일)이면 그 이후 첫 거래일이 된다.
    접수일이 거래일이면 그 다음 거래일이다 (접수 당일 진입은 금지 — 장중
    제출된 공시를 당일 시가에 살 수 없으므로 look-ahead 가 된다).
    """
    return next_trading_day(rcept_dt, calendar)


def add_event_date(df: pd.DataFrame, col: str = "rcept_dt",
                   out: str = "event_date") -> pd.DataFrame:
    """인덱스 전체에 이벤트일을 붙인다."""
    cal = trading_days()
    df = df.copy()
    df[out] = [event_date(v, cal) for v in df[col]]
    return df
