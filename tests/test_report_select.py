from src.collect.report_select import (
    is_amendment,
    matches_annual,
    period_year,
    search_window,
    select_annual_report,
)

INCLUDE = ["사업보고서"]
EXCLUDE = ["분기보고서", "반기보고서", "분기", "반기"]


def _row(nm, rcept_no, dt):
    return {"report_nm": nm, "rcept_no": rcept_no, "rcept_dt": dt}


def test_quarterly_and_semiannual_excluded():
    assert matches_annual("사업보고서 (2020.12)", INCLUDE, EXCLUDE)
    assert not matches_annual("분기보고서 (2020.03)", INCLUDE, EXCLUDE)
    assert not matches_annual("반기보고서 (2020.06)", INCLUDE, EXCLUDE)


def test_amendment_still_matches_annual():
    assert matches_annual("[기재정정]사업보고서 (2020.12)", INCLUDE, EXCLUDE)
    assert is_amendment("[첨부정정]사업보고서 (2020.12)")
    assert not is_amendment("사업보고서 (2020.12)")


def test_period_year_parsed():
    assert period_year("사업보고서 (2016.12)") == 2016
    assert period_year("사업보고서") is None


def test_original_preferred_over_amendment():
    rows = [
        _row("[기재정정]사업보고서 (2020.12)", "20210520000002", "20210520"),
        _row("사업보고서 (2020.12)", "20210331000001", "20210331"),
        _row("반기보고서 (2020.06)", "20200814000003", "20200814"),
    ]
    got = select_annual_report(rows, 2020, include=INCLUDE, exclude=EXCLUDE)
    assert got["rcept_no"] == "20210331000001"
    assert got["is_amendment"] is False
    assert got["n_amendments"] == 1


def test_amendment_used_when_no_original():
    rows = [_row("[기재정정]사업보고서 (2020.12)", "20210520000002", "20210520")]
    got = select_annual_report(rows, 2020, include=INCLUDE, exclude=EXCLUDE)
    assert got["is_amendment"] is True


def test_wrong_fiscal_year_filtered_out():
    rows = [_row("사업보고서 (2019.12)", "20200331000001", "20200331")]
    assert select_annual_report(rows, 2020, include=INCLUDE, exclude=EXCLUDE) is None


def test_search_window_covers_late_filing():
    bgn, end = search_window(2016)
    assert bgn == "20170101" and end == "20180630"
