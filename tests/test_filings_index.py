"""filings_index 필터 (Phase 1)."""
from src.collect.fetch_filings import _annual_rows, _pick_original

YEARS = {2019}


def _r(nm, dt, no):
    return {"report_nm": nm, "rcept_dt": dt, "rcept_no": no}


def test_extension_notice_is_not_an_annual_report():
    """'사업보고서제출기한연장신고서' 는 사업보고서가 아니다.

    부분문자열로 보면 걸린다. 실제로 30건이 이렇게 잘못 잡혔고,
    그 문서는 2KB 짜리 신고서라 파싱하면 섹션이 하나도 안 나온다.
    """
    rows = [_r("사업보고서제출기한연장신고서 (2019.12)", "20200320", "B")]
    assert _annual_rows(rows, YEARS, True) == []


def test_real_report_and_correction_are_kept():
    rows = [_r("사업보고서 (2019.12)", "20200407", "A"),
            _r("사업보고서제출기한연장신고서 (2019.12)", "20200320", "B"),
            _r("[기재정정]사업보고서 (2019.12)", "20201230", "C"),
            _r("반기보고서 (2019.06)", "20190814", "D")]
    got = [r["rcept_no"] for r in _annual_rows(rows, YEARS, True)]
    assert got == ["A", "C"]


def test_non_december_fiscal_year_excluded():
    rows = [_r("사업보고서 (2019.03)", "20190620", "E")]
    assert _annual_rows(rows, YEARS, True) == []
    assert len(_annual_rows(rows, YEARS, False)) == 1


def test_original_preferred_over_correction():
    cands = _annual_rows(
        [_r("[기재정정]사업보고서 (2019.12)", "20201230", "C"),
         _r("사업보고서 (2019.12)", "20200407", "A")], YEARS, True)
    ch = _pick_original(cands)
    assert ch["rcept_no"] == "A"
    assert ch["is_correction"] is False
    assert ch["is_correction_exists"] is True


def test_body_original_preferred_over_material_amendment_same_day():
    """같은 날 기재정정·첨부정정·첨부추가가 함께 올라오는 경우가 있다.

    한국단자공업 2016 이 그랬다 (셋 다 2017-03-31). 날짜만으로 정렬하면
    기재정정본을 집을 수 있는데, 그건 본문이 바뀐 문서라 leakage 다.
    첨부 계열은 본문이 원본 그대로이므로 그쪽을 택해야 한다.
    """
    rows = [_r("[기재정정]사업보고서 (2016.12)", "20170331", "A"),
            _r("[첨부정정]사업보고서 (2016.12)", "20170331", "B"),
            _r("[첨부추가]사업보고서 (2016.12)", "20170331", "C")]
    ch = _pick_original(_annual_rows(rows, {2016}, True))
    assert ch["report_type"] == "attachment_added"
    assert ch["is_correction"] is False
    assert ch["is_correction_exists"] is True     # 정정이 있었다는 사실은 남는다


def test_plain_original_beats_attachment_added():
    rows = [_r("[첨부추가]사업보고서 (2016.12)", "20170331", "C"),
            _r("사업보고서 (2016.12)", "20170331", "D")]
    ch = _pick_original(_annual_rows(rows, {2016}, True))
    assert ch["rcept_no"] == "D" and ch["report_type"] == "original"
