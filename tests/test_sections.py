"""섹션 추출의 핵심 함정 테스트.

  1. 섹션 번호가 연도마다 바뀌어도 이름으로 찾아야 한다.
  2. 문서 앞머리 목차(TOC)를 본문으로 착각하면 안 된다.
  3. 섹션의 끝은 '다음 최상위 섹션'이어야 한다 (하위 소제목이 아니라).
  4. 표는 본문 텍스트와 분리되어야 한다.
"""
import pytest

from src.parse.sections import extract_sections, find_headers, iter_blocks

SPEC = {
    "S1": {"name": "사업의 내용", "aliases": ["사업의내용"]},
    "S2": {"name": "이사의 경영진단 및 분석의견", "aliases": []},
    "S3": {"name": "그 밖에 투자자 보호를 위하여 필요한 사항", "aliases": []},
    "S4": {"name": "임원 및 직원 등에 관한 사항", "aliases": []},
}


def _doc(numbering: list[str]) -> str:
    """numbering: 각 최상위 섹션에 붙일 번호 접두사."""
    n1, n2, n3, n4, n5 = numbering
    return f"""
    <html><body>
      <table>
        <tr><td>{n1} 회사의 개요</td></tr>
        <tr><td>{n2} 사업의 내용</td></tr>
        <tr><td>{n3} 이사의 경영진단 및 분석의견</td></tr>
        <tr><td>{n4} 임원 및 직원 등에 관한 사항</td></tr>
        <tr><td>{n5} 그 밖에 투자자 보호를 위하여 필요한 사항</td></tr>
        <tr><td>XII. 재무제표 등</td></tr>
      </table>
      <p>{n1} 회사의 개요</p>
      <p>당사는 1980년에 설립되었습니다.</p>
      <p>{n2} 사업의 내용</p>
      <p>1. 사업의 개요</p>
      <p>반도체 부문 매출은 1000억원입니다.</p>
      <table><tr><td>구분</td><td>매출</td></tr><tr><td>반도체</td><td>1000</td></tr></table>
      <p>2. 주요 제품 및 서비스</p>
      <p>주요 제품은 메모리입니다.</p>
      <p>{n3} 이사의 경영진단 및 분석의견</p>
      <p>영업이익은 전기 대비 증가하였습니다.</p>
      <p>{n4} 임원 및 직원 등에 관한 사항</p>
      <p>직원 수는 100명입니다.</p>
      <p>{n5} 그 밖에 투자자 보호를 위하여 필요한 사항</p>
      <p>계류중인 소송은 없습니다.</p>
      <p>XII. 재무제표 등</p>
      <p>재무제표는 별첨과 같습니다.</p>
    </body></html>
    """


ROMAN_DOC = _doc(["I.", "II.", "IV.", "VIII.", "XI."])
ARABIC_DOC = _doc(["1.", "2.", "5.", "9.", "12."])


def test_headers_found_regardless_of_numbering():
    for html in (ROMAN_DOC, ARABIC_DOC):
        blocks = iter_blocks(html)
        names = [n for _, n, _ in find_headers(blocks)]
        # 목차(표 안)는 헤더로 잡히지 않아야 하므로 본문의 5개만
        assert names.count("사업의 내용") == 1, names
        assert "임원 및 직원 등에 관한 사항" in names


def test_numbering_change_does_not_change_result():
    a = extract_sections(ROMAN_DOC, SPEC)
    b = extract_sections(ARABIC_DOC, SPEC)
    for sid in SPEC:
        assert a[sid].found and b[sid].found
        assert a[sid].text == b[sid].text


def test_section_stops_at_next_top_level_header():
    secs = extract_sections(ROMAN_DOC, SPEC)
    s1 = secs["S1"].text
    # 하위 소제목은 포함되어야 한다
    assert "주요 제품 및 서비스" in s1
    # 다음 최상위 섹션의 본문은 포함되면 안 된다
    assert "영업이익은" not in s1
    assert "직원 수는" not in s1


def test_tables_separated_from_text():
    secs = extract_sections(ROMAN_DOC, SPEC)
    s1 = secs["S1"]
    assert s1.char_len_table > 0
    assert "<table" in s1.tables_html[0]
    assert "반도체 부문 매출은" in s1.text


def test_toc_entry_not_selected_as_section_body():
    """목차 항목이 선택되면 본문이 비거나 아주 짧아진다."""
    secs = extract_sections(ROMAN_DOC, SPEC)
    assert secs["S3"].found
    assert "계류중인 소송은 없습니다." in secs["S3"].text


def test_missing_section_reported_as_not_found():
    html = ("<html><body><p>II. 사업의 내용</p><p>내용</p>"
            "<p>III. 재무에 관한 사항</p><p>재무</p></body></html>")
    secs = extract_sections(html, SPEC)
    assert secs["S1"].found
    assert not secs["S2"].found
    assert secs["S2"].char_len_text == 0


@pytest.mark.parametrize("variant", [
    "VIII. 임원 및 직원에 관한 사항",     # 구서식
    "VIII. 임원 및 직원 등에 관한 사항",  # 신서식
])
def test_alias_variants_of_employee_section(variant):
    html = f"<html><body><p>{variant}</p><p>직원 수는 100명입니다.</p>" \
           f"<p>IX. 계열회사 등에 관한 사항</p><p>없음</p></body></html>"
    spec = {"S4": {"name": "임원 및 직원 등에 관한 사항",
                   "aliases": ["임원 및 직원에 관한 사항"]}}
    secs = extract_sections(html, spec)
    assert secs["S4"].found
    assert "직원 수는 100명입니다." in secs["S4"].text
    assert "없음" not in secs["S4"].text
