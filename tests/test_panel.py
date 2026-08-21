"""Phase 3 P3-a — look-ahead 통제와 종목 경계 (이 프로젝트 1순위 함정)."""
import numpy as np
import pandas as pd
import pytest

from src.features.panel import (
    PanelKeyError,
    add_available_from,
    as_of_join,
    assert_no_cross_boundary,
    assert_no_lookahead,
    forward_return,
    safe_diff,
    safe_pct_change,
    safe_rolling,
    safe_shift,
)
from src.utils.trading_calendar import set_calendar

CAL = pd.bdate_range("2024-01-01", "2025-12-31")


@pytest.fixture(autouse=True)
def _cal():
    set_calendar(CAL)


def _panel():
    """A 3일 + B 3일. B 의 첫 행이 A 의 마지막 값을 물려받으면 안 된다."""
    return pd.DataFrame({
        "stock_code": ["A"] * 3 + ["B"] * 3,
        "date": pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03"] * 2),
        "px": [100.0, 110.0, 121.0, 500.0, 550.0, 605.0],
    })


# ---------------------------------------------------------------- 종목 경계

def test_shift_does_not_cross_stock_boundary():
    df = _panel()
    got = safe_shift(df, "px")
    assert pd.isna(got.iloc[0])          # A 첫 행
    assert pd.isna(got.iloc[3]), "B 첫 행에 A 의 마지막 값이 넘어왔다"
    assert got.iloc[4] == 500.0


def test_diff_does_not_cross_stock_boundary():
    df = _panel()
    got = safe_diff(df, "px")
    assert pd.isna(got.iloc[3])
    assert got.iloc[1] == 10.0


def test_pct_change_does_not_cross_stock_boundary():
    df = _panel()
    got = safe_pct_change(df, "px")
    assert pd.isna(got.iloc[3])
    assert got.iloc[1] == pytest.approx(0.10)


def test_rolling_window_does_not_cross_stock_boundary():
    df = _panel()
    got = safe_rolling(df, "px", window=2)
    assert pd.isna(got.iloc[3]), "B 첫 행 창이 A 로 넘어갔다"
    assert got.iloc[4] == pytest.approx((500 + 550) / 2)


def test_forward_return_does_not_cross_stock_boundary():
    df = _panel()
    got = forward_return(df, "px", horizon=1)
    assert pd.isna(got.iloc[2]), "A 마지막 행이 B 첫 값을 미래로 당겨왔다"
    assert got.iloc[0] == pytest.approx(0.10)


def test_naive_shift_would_have_leaked():
    """대조군 — groupby 없이 하면 실제로 넘어간다는 것을 고정해 둔다."""
    df = _panel().sort_values(["stock_code", "date"])
    naive = df["px"].shift(1)
    assert naive.iloc[3] == 121.0        # A 의 마지막 값이 B 첫 행에 들어왔다


def test_assert_no_cross_boundary_catches_leak():
    df = _panel().sort_values(["stock_code", "date"])
    df["bad"] = df["px"].shift(1)        # groupby 없이
    with pytest.raises(PanelKeyError, match="종목 경계"):
        assert_no_cross_boundary(df, "bad")


def test_assert_no_cross_boundary_passes_for_safe_shift():
    df = _panel()
    df["ok"] = safe_shift(df, "px")
    assert_no_cross_boundary(df, "ok")


def test_missing_key_raises_instead_of_silent_wrong_answer():
    df = _panel().drop(columns=["stock_code"])
    with pytest.raises(PanelKeyError, match="경계 키"):
        safe_shift(df, "px")


def test_unsorted_input_still_correct():
    """입력이 뒤섞여 있어도 결과가 원래 행에 정확히 되돌아가야 한다."""
    df = _panel().sample(frac=1, random_state=0)
    got = safe_shift(df, "px")
    for i, r in df.iterrows():
        if r["date"] == pd.Timestamp("2024-01-01"):
            assert pd.isna(got.loc[i])
        elif r["stock_code"] == "A" and r["date"] == pd.Timestamp("2024-01-02"):
            assert got.loc[i] == 100.0


# ---------------------------------------------------------------- look-ahead

def test_available_from_is_filing_date_plus_one_trading_day():
    f = pd.DataFrame({"stock_code": ["A"], "rcept_dt": ["20250313"]})
    got = add_available_from(f)
    assert got["available_from"].iloc[0] == pd.Timestamp("2025-03-14")


def test_financials_not_usable_before_disclosure():
    """회계연도 종료일이 아니라 공시일 기준이어야 한다."""
    facts = add_available_from(pd.DataFrame({
        "stock_code": ["A"], "rcept_dt": ["20250313"], "roa": [0.12]}))
    panel = pd.DataFrame({
        "stock_code": ["A", "A", "A"],
        "date": pd.to_datetime(["2025-01-02", "2025-03-13", "2025-03-14"])})
    out = as_of_join(panel, facts, value_cols=["roa"])
    got = out.sort_values("date")["roa"].tolist()
    assert pd.isna(got[0]), "회계연도 종료 직후에 이미 값이 붙었다 (look-ahead)"
    assert pd.isna(got[1]), "접수 당일에 값이 붙었다 (look-ahead)"
    assert got[2] == 0.12, "접수일+1거래일부터는 쓸 수 있어야 한다"


def test_as_of_join_does_not_mix_stocks():
    facts = add_available_from(pd.DataFrame({
        "stock_code": ["A", "B"], "rcept_dt": ["20250313", "20250320"],
        "roa": [0.12, 0.99]}))
    panel = pd.DataFrame({
        "stock_code": ["A", "B"],
        "date": pd.to_datetime(["2025-03-17", "2025-03-17"])})
    out = as_of_join(panel, facts, value_cols=["roa"]).set_index("stock_code")
    assert out.loc["A", "roa"] == 0.12
    assert pd.isna(out.loc["B", "roa"]), "B 에 A 의 값이 붙었거나 미공시 값이 붙었다"


def test_assert_no_lookahead_catches_future_values():
    df = pd.DataFrame({
        "date": pd.to_datetime(["2025-01-02"]),
        "available_from": pd.to_datetime(["2025-03-14"])})
    with pytest.raises(PanelKeyError, match="look-ahead"):
        assert_no_lookahead(df)
