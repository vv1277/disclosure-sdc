"""S4 임원 표 구조화 추출 테스트 (Phase 1 요구사항 3)."""
import pandas as pd

from src.parse.officers import (
    extract_officers,
    find_header,
    parse_bool,
    parse_tenure_months,
    table_to_grid,
)

# 실데이터와 같은 2행 헤더 구조 (rowspan + colspan)
OFFICER_TABLE = """
<table>
  <tr>
    <th rowspan="2">성명</th><th rowspan="2">직위</th>
    <th rowspan="2">등기임원여부</th><th rowspan="2">상근여부</th>
    <th rowspan="2">담당업무</th>
    <th colspan="2">소유주식수</th>
    <th rowspan="2">재직기간</th><th rowspan="2">임기만료일</th>
  </tr>
  <tr><th>의결권있는 주식</th><th>의결권없는 주식</th></tr>
  <tr><td>홍길동</td><td>대표이사</td><td>등기임원</td><td>상근</td><td>경영총괄</td>
      <td>1,000</td><td>0</td><td>3년 2개월</td><td>2027.03.20</td></tr>
  <tr><td>김철수</td><td>전무</td><td>미등기</td><td>비상근</td><td>영업본부장</td>
      <td>0</td><td>0</td><td>12개월</td><td>-</td></tr>
</table>
"""


def test_grid_expands_colspan_and_rowspan():
    g = table_to_grid(OFFICER_TABLE)
    assert len(g[0]) == 9
    # rowspan=2 인 '성명' 은 두 행 모두에 채워진다
    assert g[0][0] == "성명" and g[1][0] == "성명"
    # colspan=2 인 '소유주식수' 아래에 하위 헤더가 자리잡는다
    assert g[1][5] == "의결권있는 주식" and g[1][6] == "의결권없는 주식"


def test_header_columns_are_not_shifted_by_colspan():
    """colspan 을 펼치지 않으면 '재직기간' 자리에 다른 값이 들어간다."""
    g = table_to_grid(OFFICER_TABLE)
    start, colmap = find_header(g)
    assert colmap[0] == "name"
    assert colmap[7] == "tenure"
    assert colmap[8] == "term_end"
    # 헤더 1행만으로 매핑이 완성되면 start=1 이고, 2행째(의결권 하위 헤더)는
    # _is_header_echo 가 걸러낸다. 어느 쪽이든 데이터 행부터 읽으면 된다.
    assert start in (1, 2)


def test_extract_officers_normalizes_rows():
    df = extract_officers([OFFICER_TABLE])
    assert len(df) == 2
    a = df.iloc[0]
    assert a["name"] == "홍길동" and a["position"] == "대표이사"
    assert bool(a["is_registered"]) and bool(a["full_time"])
    assert a["tenure_months"] == 38
    assert a["term_end_raw"] == "2027.03.20"
    b = df.iloc[1]
    assert not bool(b["is_registered"]) and not bool(b["full_time"])
    assert b["tenure_months"] == 12


def test_header_echo_row_is_not_treated_as_officer():
    df = extract_officers([OFFICER_TABLE])
    assert "의결권있는 주식" not in set(df["name"])
    assert "성명" not in set(df["name"])


def test_employee_table_is_rejected():
    """직원 현황 표는 임원 표가 아니다."""
    html = """<table><tr><th>사업부문</th><th>성별</th><th>직 원 수</th>
              <th>평균근속연수</th><th>연간급여총액</th></tr>
              <tr><td>반도체</td><td>남</td><td>100</td><td>10.5</td><td>500</td></tr>
              </table>"""
    assert extract_officers([html]).empty


def test_summary_rows_dropped():
    html = OFFICER_TABLE.replace("<td>김철수</td>", "<td>합계</td>")
    df = extract_officers([html])
    assert "합계" not in set(df["name"])


def test_parse_tenure_months_variants():
    assert parse_tenure_months("3년 2개월") == 38
    assert parse_tenure_months("12개월") == 12
    assert parse_tenure_months("2년") == 24
    assert parse_tenure_months("3년0월") == 36
    # 해석 불가는 0 이 아니라 None (결측과 '0개월' 을 구분해야 한다)
    assert parse_tenure_months("-") is None
    assert parse_tenure_months("") is None
    assert parse_tenure_months("계열회사임원") is None
    assert parse_tenure_months("2022년~현재") is None


def test_parse_bool_negation_wins():
    """'미등기' 는 '등기' 를 포함한다. 부정을 먼저 봐야 한다."""
    assert parse_bool("미등기임원") is False
    assert parse_bool("등기임원") is True
    assert parse_bool("비상근") is False
    assert parse_bool("상근") is True
    assert parse_bool("") is None


def test_extract_for_document_adds_keys():
    from src.parse.officers import extract_for_document
    df = extract_for_document([OFFICER_TABLE], corp_code="00126380", fy=2024,
                              rcept_no="20250311001085")
    assert list(df.columns[:3]) == ["corp_code", "fy", "rcept_no"]
    assert (df["fy"] == 2024).all()
