"""유니버스 구성 — 생존편향 처리 (Phase 1)."""
import pandas as pd

from src.collect.universe import listed_at


def _tables():
    cur = pd.DataFrame({
        "Code": ["005930", "000660"],
        "Name": ["삼성전자", "SK하이닉스"],
        "Market": ["KOSPI", "KOSPI"],
    })
    dead = pd.DataFrame({
        "Symbol": ["111111", "222222", "333333"],
        "Name": ["폐지2018", "폐지2023", "상장전"],
        "Market": ["KOSPI", "KOSDAQ", "KOSPI"],
        "ListingDate": pd.to_datetime(["2010-01-01", "2010-01-01", "2022-01-01"]),
        "DelistingDate": pd.to_datetime(["2018-06-01", "2023-06-01", "2024-01-01"]),
    })
    return cur, dead


def test_snapshot_includes_firms_delisted_later():
    """2016년 스냅샷에는 2018년에 폐지된 기업이 반드시 들어가야 한다."""
    cur, dead = _tables()
    snap = listed_at(cur, dead, "2016-12-29")
    codes = set(snap["stock_code"])
    assert "111111" in codes, "이후 폐지된 기업이 빠지면 생존편향이다"
    assert "222222" in codes
    assert snap.loc[snap.stock_code == "111111", "is_delisted_later"].item()


def test_snapshot_excludes_firms_not_yet_listed():
    cur, dead = _tables()
    snap = listed_at(cur, dead, "2016-12-29")
    assert "333333" not in set(snap["stock_code"]), "상장 전 종목이 들어가면 안 된다"


def test_snapshot_excludes_already_delisted():
    """2020년 시점에는 2018년에 폐지된 기업이 없어야 한다."""
    cur, dead = _tables()
    snap = listed_at(cur, dead, "2020-12-30")
    assert "111111" not in set(snap["stock_code"])
    assert "222222" in set(snap["stock_code"])


def test_currently_listed_are_not_flagged_as_delisted():
    cur, dead = _tables()
    snap = listed_at(cur, dead, "2016-12-29")
    assert not snap.loc[snap.stock_code == "005930", "is_delisted_later"].item()


def test_delisted_flag_is_not_mere_universe_dropout():
    """시총 순위에서 밀려난 것과 상장폐지는 다르다.

    '다음 연도 유니버스에 없다' 로 판정하면 2015년 폐지비율이 45% 로 나온다.
    그것은 정상 회전율이지 상장폐지가 아니다. 실제 폐지일로만 판정해야 한다.
    """
    cur, dead = _tables()
    snap = listed_at(cur, dead, "2016-12-29")
    # 삼성전자는 어떤 연도 유니버스에서 빠지더라도 폐지가 아니다
    row = snap[snap.stock_code == "005930"].iloc[0]
    assert row["is_delisted_later"] is False or row["is_delisted_later"] == False
    assert pd.isna(row["delist_date"])
