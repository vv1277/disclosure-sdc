"""Phase 3 P3-a — characteristics 패널의 뼈대: look-ahead 통제와 종목 경계.

이 파일이 하는 일은 두 가지뿐이고, 둘 다 이 프로젝트에서 가장 자주 나는 사고다.

1) look-ahead 통제
   재무 변수는 **회계연도 종료일이 아니라 공시가 나온 뒤에야** 쓸 수 있다.
   2024-12-31 로 끝난 회계연도의 재무제표는 2025-03 에 공시된다. 그것을
   2025-01 시점 모형에 넣으면 미래 정보를 쓰는 것이다.
   사용 가능 시점 = event_date(접수일) = 접수일 + 1 거래일 (Phase 1 결정 1).

2) 종목 경계
   shift / rolling / diff 가 종목 경계를 넘으면 A 종목의 마지막 값이 B 종목의
   첫 값으로 흘러 들어간다. 계획서 부록 B.2 가 '이 프로젝트의 1순위 함정' 으로
   지목한 것이다. 여기 있는 함수는 전부 groupby 를 강제한다.
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from src.utils.trading_calendar import event_date, trading_days

log = logging.getLogger(__name__)

KEY = "stock_code"          # 종목 경계 기준 키


class PanelKeyError(ValueError):
    """패널의 키 구성이 잘못됐다. 조용히 틀린 값을 만드느니 여기서 멈춘다."""


# --------------------------------------------------------------------------
# 1) look-ahead 통제
# --------------------------------------------------------------------------

def add_available_from(filings: pd.DataFrame, *, rcept_col: str = "rcept_dt",
                       out: str = "available_from") -> pd.DataFrame:
    """재무 변수를 언제부터 쓸 수 있는지 (= 접수일 + 1 거래일).

    Phase 1 결정 1 에서 고정한 이벤트일 규칙을 그대로 재사용한다.
    접수 당일 진입은 look-ahead 이므로 반드시 다음 거래일이다.
    """
    cal = trading_days()
    df = filings.copy()
    df[out] = [event_date(v, cal) for v in df[rcept_col]]
    return df


def as_of_join(panel: pd.DataFrame, facts: pd.DataFrame, *,
               on: str = KEY, panel_date: str = "date",
               fact_date: str = "available_from",
               value_cols: list[str] | None = None) -> pd.DataFrame:
    """시점 기준 조인 — 그 날짜에 **이미 공시된** 값만 붙인다.

    merge_asof 를 쓰되 by=종목, direction='backward' 로 고정한다.
    allow_exact_matches=True: available_from 당일부터 쓸 수 있다
    (available_from 자체가 이미 접수일+1거래일이므로 당일 사용이 맞다).
    """
    if panel.empty or facts.empty:
        return panel.copy()
    value_cols = value_cols or [c for c in facts.columns
                                if c not in (on, fact_date)]
    left = panel.sort_values(panel_date).copy()
    right = facts[[on, fact_date, *value_cols]].sort_values(fact_date).copy()
    left[panel_date] = pd.to_datetime(left[panel_date])
    right[fact_date] = pd.to_datetime(right[fact_date])

    out = pd.merge_asof(left, right, left_on=panel_date, right_on=fact_date,
                        by=on, direction="backward", allow_exact_matches=True)
    return out


def assert_no_lookahead(df: pd.DataFrame, *, panel_date: str = "date",
                        fact_date: str = "available_from") -> None:
    """붙은 값의 공시 시점이 관측 시점보다 미래면 즉시 실패한다."""
    if fact_date not in df or panel_date not in df:
        return
    m = df[fact_date].notna()
    bad = m & (pd.to_datetime(df.loc[m, fact_date])
               > pd.to_datetime(df.loc[m, panel_date]))
    n = int(bad.sum())
    if n:
        raise PanelKeyError(
            f"look-ahead {n}행: 공시 전 값이 붙었다. "
            f"as_of_join 의 direction/by 설정을 확인할 것.")


# --------------------------------------------------------------------------
# 2) 종목 경계를 넘지 않는 시계열 연산
# --------------------------------------------------------------------------

def _require_sorted(df: pd.DataFrame, by: str, date_col: str) -> pd.DataFrame:
    if by not in df.columns:
        raise PanelKeyError(f"경계 키 '{by}' 가 없다. groupby 없이 shift 하면 "
                            f"종목 경계를 넘는다.")
    if date_col not in df.columns:
        raise PanelKeyError(f"날짜 컬럼 '{date_col}' 가 없다.")
    return df.sort_values([by, date_col])


def safe_shift(df: pd.DataFrame, col: str, periods: int = 1, *,
               by: str = KEY, date_col: str = "date") -> pd.Series:
    """종목별 shift. 경계를 넘으면 NaN 이 된다."""
    d = _require_sorted(df, by, date_col)
    return d.groupby(by, sort=False)[col].shift(periods).reindex(df.index)


def safe_diff(df: pd.DataFrame, col: str, periods: int = 1, *,
              by: str = KEY, date_col: str = "date") -> pd.Series:
    d = _require_sorted(df, by, date_col)
    return d.groupby(by, sort=False)[col].diff(periods).reindex(df.index)


def safe_pct_change(df: pd.DataFrame, col: str, periods: int = 1, *,
                    by: str = KEY, date_col: str = "date") -> pd.Series:
    prev = safe_shift(df, col, periods, by=by, date_col=date_col)
    return (df[col] - prev) / prev.replace(0, np.nan)


def safe_rolling(df: pd.DataFrame, col: str, window: int, how: str = "mean", *,
                 by: str = KEY, date_col: str = "date",
                 min_periods: int | None = None) -> pd.Series:
    """종목별 rolling. 창이 종목 경계를 넘지 않는다."""
    d = _require_sorted(df, by, date_col)
    g = d.groupby(by, sort=False)[col].rolling(
        window, min_periods=min_periods or window)
    return getattr(g, how)().reset_index(level=0, drop=True).reindex(df.index)


def forward_return(df: pd.DataFrame, price_col: str, horizon: int, *,
                   by: str = KEY, date_col: str = "date") -> pd.Series:
    """t+1 ~ t+horizon 수익률. 종목 경계를 넘지 않는다.

    주의: 미래 값을 당겨오는 연산이므로 **라벨 전용**이다. feature 로 쓰면
    그 자체가 leakage 다.
    """
    d = _require_sorted(df, by, date_col)
    fwd = d.groupby(by, sort=False)[price_col].shift(-horizon)
    return ((fwd - d[price_col]) / d[price_col]).reindex(df.index)


def assert_no_cross_boundary(df: pd.DataFrame, col: str, *, by: str = KEY,
                             date_col: str = "date") -> None:
    """각 종목의 첫 행에서 shift 결과가 NaN 인지 확인한다.

    NaN 이 아니면 이전 종목의 값이 넘어온 것이다.
    """
    d = _require_sorted(df, by, date_col)
    first = d.groupby(by, sort=False).head(1)
    bad = first[col].notna()
    if bad.any():
        raise PanelKeyError(
            f"'{col}' 이 종목 경계를 넘었다: 각 종목 첫 행 중 {int(bad.sum())}개가 "
            f"NaN 이 아니다. groupby 없이 shift/rolling 했을 때 나는 증상이다.")
