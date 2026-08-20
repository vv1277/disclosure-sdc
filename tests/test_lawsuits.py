"""소송 표 구조화 (Phase 4.4)."""
import pandas as pd
import pytest

from src.parse.lawsuits import (
    classify_table,
    extract_lawsuits,
    is_none_marker,
    match_column,
    parse_amount,
    parse_amount_unit,
)
from src.utils.config import load_config

CFG = load_config()["lawsuits"]

LAWSUIT_TABLE = """
<table>
  <tr><th>소송의 내용</th><th>소제기일</th><th>원 고</th>
      <th>소송가액(억원)</th><th>진행현황</th></tr>
  <tr><td>질권소멸통지등</td><td>2020-11-05</td><td>아시아나항공 외1</td>
      <td>50,488</td><td>3심 계류중</td></tr>
  <tr><td>손해배상</td><td>2024-07-25</td><td>고려저축은행</td>
      <td>1,000</td><td>1심 계류중</td></tr>
</table>
"""

NONE_TABLE = "<table><tr><td>해당사항 없음</td></tr></table>"


def test_empty_none_token_does_not_swallow_everything():
    """'-' 는 정규화하면 빈 문자열이다. 빈 문자열은 모든 텍스트의 부분문자열이라
    걸러내지 않으면 모든 표가 '소송 없음' 이 된다 (실제로 89건 전부 그랬다)."""
    toks = CFG["none_tokens"]
    assert is_none_marker("-", toks)
    assert is_none_marker("해당사항 없음", toks)
    assert not is_none_marker(
        "질권소멸통지등 2020-11-05 아시아나항공 외1 50,488 3심 계류중", toks)
    assert not is_none_marker(
        "당사는 손해배상청구 등 총 28건의 피고소송에 계류중이며 "
        "관련 소송가액은 297,641백만원 입니다.", toks)


def test_column_aliases_match_varied_headers():
    from src.parse.lawsuits import build_alias_lookup
    lk = build_alias_lookup(CFG["column_aliases"])
    assert match_column("소송의 내용", lk) == "content"
    assert match_column("소 제기일", lk) == "filed_on"
    assert match_column("소송가액(억원)", lk) == "amount"
    assert match_column("소송 당사자(피고)", lk) == "defendant"
    assert match_column("진행현황", lk) == "status"


def test_amount_unit_read_from_header():
    u = CFG["amount_units"]
    assert parse_amount_unit("소송가액(억원)", u) == 100_000_000
    assert parse_amount_unit("소송가액(원화단위: 천원)", u) == 1_000
    assert parse_amount_unit("소송가액", u) == 1          # 단위 없으면 원


def test_amount_parsed_to_krw():
    u = CFG["amount_units"]
    assert parse_amount("50,488", 100_000_000, u) == 50_488 * 100_000_000
    # 셀에 단위가 있으면 헤더보다 우선한다
    assert parse_amount("297,641백만원", 100_000_000, u) == 297_641 * 1_000_000
    # 해석 불가는 0 이 아니라 None (결측과 '0원' 을 구분해야 한다)
    assert parse_amount("-", 1, u) is None
    assert parse_amount("", 1, u) is None


def test_extract_rows_with_amounts():
    df, audits = extract_lawsuits([LAWSUIT_TABLE], CFG)
    assert len(df) == 2
    assert df.iloc[0]["content"] == "질권소멸통지등"
    assert df.iloc[0]["amount_krw"] == 50_488 * 100_000_000
    assert audits[0]["is_lawsuit"] is True


def test_none_table_yields_zero_rows_not_missing():
    """'해당사항 없음' 은 소송 0건이지 결측이 아니다."""
    df, audits = extract_lawsuits([NONE_TABLE], CFG)
    assert df.empty
    assert audits[0]["has_none_marker"] is True


def test_header_repeat_row_is_dropped():
    html = LAWSUIT_TABLE.replace(
        "</table>",
        "<tr><td>소송의 내용</td><td>소제기일</td><td>원 고</td>"
        "<td>소송가액</td><td>진행현황</td></tr></table>")
    df, _ = extract_lawsuits([html], CFG)
    assert "소송의 내용" not in set(df["content"])


def test_non_lawsuit_table_is_not_picked_up():
    html = ("<table><tr><th>사업부문</th><th>매출액</th><th>영업이익</th></tr>"
            "<tr><td>반도체</td><td>1000</td><td>100</td></tr></table>")
    df, audits = extract_lawsuits([html], CFG)
    assert df.empty
    assert audits[0]["is_lawsuit"] is False
