"""이벤트일 = 접수일 + 1 거래일 (Phase 1 결정 1)."""
import pandas as pd
import pytest

from src.utils.trading_calendar import (
    event_date,
    is_trading_day,
    next_trading_day,
    set_calendar,
)

# 2025년 실제 KRX 휴장을 흉내낸 달력.
#   3/1 토, 3/2 일 (주말)
#   3/3 월요일은 삼일절 대체공휴일로 휴장
#   3/7 금 -> 다음 거래일은 3/10 월
CAL = [
    "2025-02-26", "2025-02-27", "2025-02-28",
    "2025-03-04", "2025-03-05", "2025-03-06", "2025-03-07",
    "2025-03-10", "2025-03-11",
]


@pytest.fixture(autouse=True)
def _cal():
    set_calendar(CAL)
    yield
    set_calendar(CAL)


def test_normal_weekday_moves_to_next_trading_day():
    assert event_date("20250304") == pd.Timestamp("2025-03-05")


def test_filing_day_itself_is_never_the_event_day():
    """접수 당일 시가 진입은 look-ahead 다. 반드시 다음 거래일이어야 한다."""
    for d in ("20250304", "2025-03-05", "20250310"):
        assert event_date(d) > pd.Timestamp(d.replace("-", "")[:4] + "-"
                                            + d.replace("-", "")[4:6] + "-"
                                            + d.replace("-", "")[6:8])


def test_friday_filing_skips_the_weekend():
    """금요일 접수 -> 월요일. 주말을 건너뛴다."""
    assert event_date("20250307") == pd.Timestamp("2025-03-10")


def test_filing_before_holiday_block_skips_all_of_it():
    """2/28(금) 접수 -> 3/1~3/3 휴장이므로 3/4(화)."""
    assert event_date("20250228") == pd.Timestamp("2025-03-04")


def test_non_trading_filing_date_falls_to_next_trading_day():
    """주말 제출은 없지만 방어적으로. 토요일 접수 -> 다음 거래일."""
    assert event_date("20250301") == pd.Timestamp("2025-03-04")   # 토
    assert event_date("20250302") == pd.Timestamp("2025-03-04")   # 일
    assert event_date("20250303") == pd.Timestamp("2025-03-04")   # 공휴일


def test_accepts_multiple_date_formats():
    want = pd.Timestamp("2025-03-05")
    for d in ("20250304", "2025-03-04", pd.Timestamp("2025-03-04")):
        assert event_date(d) == want


def test_is_trading_day():
    assert is_trading_day("20250304")
    assert not is_trading_day("20250303")


def test_raises_when_calendar_runs_out():
    """달력 끝을 넘어가면 조용히 틀린 날짜를 주지 말고 실패해야 한다."""
    with pytest.raises(ValueError, match="거래일이 달력에 없습니다"):
        next_trading_day("20250311")


def test_next_trading_day_is_strictly_after():
    assert next_trading_day("20250304") == pd.Timestamp("2025-03-05")
    assert next_trading_day("20250304") != pd.Timestamp("2025-03-04")
