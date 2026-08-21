"""P0-c Part 2 — 경계 탐지 수정에 대한 회귀 테스트.

실데이터에서 관측된 세 가지 결함을 재현하는 최소 픽스처를 두고,
수정된 파서가 그것을 잡아내는지 확인한다.
"""
import pytest

from src.parse.legacy_sections import legacy_extract_sections
from src.parse.sections import (
    END_REASON_EOF,
    extract_sections,
    find_headers,
    iter_blocks,
    match_section_name,
    numbering_style,
)

SPEC = {
    "S1": {"name": "사업의 내용", "aliases": []},
    "S4": {"name": "임원 및 직원 등에 관한 사항",
           "aliases": ["임원 및 직원에 관한 사항"]},
}

# ---------------------------------------------------------------------------
# 결함 1: DART 커스텀 컨테이너 태그 안의 <table> 이 본문으로 새어 들어온다
# ---------------------------------------------------------------------------

CUSTOM_TAG_DOC = """<html><body>
  <p>II. 사업의 내용</p>
  <p>당사는 반도체 부문을 중심으로 사업을 영위하고 있습니다.</p>
  <table-group>
    <table><tr><th>성명</th><th>성별</th><th>직위</th></tr>
           <tr><td>홍길동</td><td>남</td><td>대표이사</td></tr></table>
  </table-group>
  <section-3>
    <table><tr><td>구분</td><td>당기</td></tr><tr><td>매출</td><td>1000</td></tr></table>
  </section-3>
  <p>III. 재무에 관한 사항</p>
  <p>재무 내용</p>
  <p>IV. 이사의 경영진단 및 분석의견</p>
  <p>경영진단</p>
</body></html>"""


def test_legacy_leaks_table_from_custom_container():
    """수정 전 파서는 <TABLE-GROUP> 안의 표를 본문 텍스트로 흘린다."""
    s1 = legacy_extract_sections(CUSTOM_TAG_DOC, SPEC)["S1"]
    assert "홍길동" in s1.text, "이 테스트가 재현하려던 결함이 사라졌다"


def test_fixed_parser_keeps_tables_out_of_body():
    s1 = extract_sections(CUSTOM_TAG_DOC, SPEC)["S1"]
    assert "홍길동" not in s1.text
    assert "성명" not in s1.text
    assert "당사는 반도체" in s1.text
    # 표는 버려지지 않고 표 쪽으로 간다
    assert "홍길동" in s1.tables_text
    assert len(s1.tables_html) == 2


def test_table_removal_applies_to_every_section_uniformly():
    for sid in ("S1", "S4"):
        html = CUSTOM_TAG_DOC.replace("II. 사업의 내용", "VIII. 임원 및 직원 등에 관한 사항") \
            if sid == "S4" else CUSTOM_TAG_DOC
        sec = extract_sections(html, SPEC)[sid]
        if sec.found:
            assert "홍길동" not in sec.text


def test_blocks_never_hide_a_table_as_inline_text():
    blocks = iter_blocks(CUSTOM_TAG_DOC)
    assert sum(1 for b in blocks if b.kind == "table") == 2
    for b in blocks:
        if b.kind == "text":
            assert "홍길동" not in b.text


# ---------------------------------------------------------------------------
# 결함 2: 하위 소제목이 최상위 헤더로 오인된다
# ---------------------------------------------------------------------------

SUBHEADING_DOC = """<html><body>
  <p>I. 회사의 개요</p>
  <p>1. 회사의 개요</p>
  <p>당사는 1980년에 설립되었습니다.</p>
  <p>II. 사업의 내용</p>
  <p>반도체 사업을 영위합니다. 이 문장은 S1 본문이어야 합니다.</p>
  <p>2. 재무 등에 관한 사항</p>
  <p>이 문장도 여전히 S1 본문입니다. 하위 소제목이 섹션을 끊으면 안 됩니다.</p>
  <p>III. 재무에 관한 사항</p>
  <p>여기부터는 S1 이 아닙니다.</p>
  <p>IV. 이사의 경영진단 및 분석의견</p>
  <p>경영진단</p>
</body></html>"""


def test_numbering_style_detection():
    assert numbering_style("II. 사업의 내용") == "roman"
    assert numbering_style("2. 재무 등에 관한 사항") == "arabic"
    assert numbering_style("사업의 내용") == "none"


def test_roman_numbered_document_ignores_arabic_subheadings():
    blocks = iter_blocks(SUBHEADING_DOC)
    texts = [t for _, _, t in find_headers(blocks)]
    assert "II. 사업의 내용" in texts
    assert "1. 회사의 개요" not in texts
    assert "2. 재무 등에 관한 사항" not in texts


def test_section_not_truncated_by_subheading():
    s1 = extract_sections(SUBHEADING_DOC, SPEC)["S1"]
    assert "하위 소제목이 섹션을 끊으면 안 됩니다" in s1.text
    assert "여기부터는 S1 이 아닙니다" not in s1.text
    assert s1.end_reason == "재무에 관한 사항"


def test_fuzzy_match_no_longer_confuses_similar_names():
    """'재무 등에 관한 사항' 은 '재무에 관한 사항' 과 다른 항목이다."""
    assert match_section_name("2. 재무 등에 관한 사항") is None
    assert match_section_name("III. 재무에 관한 사항") == "재무에 관한 사항"


# ---------------------------------------------------------------------------
# 결함 3: 종료 헤더가 없는데 성공으로 집계된다
# ---------------------------------------------------------------------------

NO_TERMINATOR_DOC = """<html><body>
  <p>I. 회사의 개요</p><p>개요</p>
  <p>IV. 이사의 경영진단 및 분석의견</p><p>경영진단</p>
  <p>II. 사업의 내용</p>
  <p>본문이 문서 끝까지 이어집니다.</p>
  <p>기대신용손실 및 공정가치 서열체계에 관한 주석입니다.</p>
</body></html>"""


def test_missing_terminator_is_reported_as_eof():
    s1 = extract_sections(NO_TERMINATOR_DOC, SPEC)["S1"]
    assert s1.end_reason == END_REASON_EOF
    assert s1.end_header == ""
    assert s1.has_body is True


def test_missing_terminator_demotes_found_when_required():
    strict = extract_sections(NO_TERMINATOR_DOC, SPEC, require_terminator=True)["S1"]
    loose = extract_sections(NO_TERMINATOR_DOC, SPEC, require_terminator=False)["S1"]
    assert strict.found is False
    assert loose.found is True


def test_legacy_counted_missing_terminator_as_success():
    s1 = legacy_extract_sections(NO_TERMINATOR_DOC, SPEC)["S1"]
    assert s1.found is True and s1.end_reason == END_REASON_EOF


def test_start_and_end_headers_recorded():
    s1 = extract_sections(SUBHEADING_DOC, SPEC)["S1"]
    assert s1.start_header == "II. 사업의 내용"
    assert s1.end_header == "III. 재무에 관한 사항"


# ---------------------------------------------------------------------------
# 표준 목차 전체가 종료 헤더로 등록되어 있는가 (Part 2-4)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("terminator", [
    "III. 재무에 관한 사항",
    "V. 감사인의 감사의견 등",
    "VI. 이사회 등 회사의 기관에 관한 사항",
    "VII. 주주에 관한 사항",
    "IX. 계열회사 등에 관한 사항",
    "X. 이해관계자와의 거래내용",
    "XI. 그 밖에 투자자 보호를 위하여 필요한 사항",
    "XII. 재무제표 등",
    "XIII. 부속명세서",
    "XIV. 전문가의 확인",
])
def test_any_standard_section_terminates_current_section(terminator):
    html = f"""<html><body>
      <p>I. 회사의 개요</p><p>개요</p>
      <p>IV. 이사의 경영진단 및 분석의견</p><p>경영진단</p>
      <p>II. 사업의 내용</p><p>사업 본문입니다.</p>
      <p>{terminator}</p><p>다음 섹션 본문</p>
    </body></html>"""
    s1 = extract_sections(html, SPEC)["S1"]
    assert s1.found is True
    assert s1.end_header == terminator
    assert "다음 섹션 본문" not in s1.text


# ---------------------------------------------------------------------------
# 종료 헤더는 번호 체계를 가리지 않는다 (실데이터에서 확인된 케이스)
# ---------------------------------------------------------------------------

ARABIC_TERMINATOR_DOC = """<html><body>
  <p>I. 회사의 개요</p><p>개요</p>
  <p>II. 사업의 내용</p><p>사업 내용</p>
  <p>III. 재무에 관한 사항</p><p>재무</p>
  <p>XI. 그 밖에 투자자 보호를 위하여 필요한 사항</p>
  <p>계류중인 소송은 없습니다.</p>
  <p>1. 전문가의 확인</p>
  <p>해당사항 없음</p>
</body></html>"""


def test_arabic_numbered_terminator_still_ends_the_section():
    """'1. 전문가의 확인'은 아라비아 번호지만 XI 섹션의 정당한 종료 헤더다.

    시작 헤더는 로마숫자만 인정하되, 종료 헤더는 번호 체계를 가리지 않는다.
    """
    spec = {"S3": {"name": "그 밖에 투자자 보호를 위하여 필요한 사항", "aliases": []}}
    s3 = extract_sections(ARABIC_TERMINATOR_DOC, spec)["S3"]
    assert s3.found is True
    assert s3.end_reason == "전문가의 확인"
    assert s3.end_header == "1. 전문가의 확인"
    assert "해당사항 없음" not in s3.text
    assert "계류중인 소송은 없습니다." in s3.text


def test_arabic_subheading_still_not_a_section_start():
    """종료 기준을 넓혀도 하위 소제목이 섹션 '시작'이 되지는 않는다."""
    blocks = iter_blocks(SUBHEADING_DOC)
    starts = [t for _, _, t in find_headers(blocks)]
    assert "1. 회사의 개요" not in starts


# ---------------------------------------------------------------------------
# 파싱 캐시 무효화 — 파싱 경로 모듈이 바뀌면 지문이 바뀌어야 한다
# ---------------------------------------------------------------------------

def test_parser_fingerprint_covers_whole_parse_path(tmp_path, monkeypatch):
    """지문 계산에 파싱 경로 모듈이 전부 들어 있어야 한다.

    textnorm 이 빠져 있어 clean_text() 수정이 캐시에 반영되지 않은 사고가 있었다.
    """
    import src.pilot.parse_cache as pc

    before = pc.parser_fingerprint()

    # textnorm 소스가 바뀐 상황을 흉내낸다
    fake = tmp_path / "textnorm_modified.py"
    fake.write_text("# 내용이 달라진 파일\n", encoding="utf-8")
    monkeypatch.setattr(pc.textnorm, "__file__", str(fake))

    assert pc.parser_fingerprint() != before, "textnorm 변경이 지문에 반영되지 않는다"


def test_parser_fingerprint_is_stable_without_changes():
    import src.pilot.parse_cache as pc
    assert pc.parser_fingerprint() == pc.parser_fingerprint()


# ---------------------------------------------------------------------------
# 하위 소제목이 섹션명을 반복하면 섹션이 즉시 끊긴다 (전량 파싱에서 발견)
# ---------------------------------------------------------------------------

SELF_REPEAT_DOC = """<html><body>
  <p>I. 회사의 개요</p><p>개요</p>
  <p>IV. 이사의 경영진단 및 분석의견</p><p>경영진단</p>
  <p>II. 사업의 내용</p>
  <p>1. 사업의 내용</p>
  <p>당사는 목재 가공업을 영위합니다. 이 문장이 본문이어야 합니다.</p>
  <p>III. 재무에 관한 사항</p><p>재무</p>
</body></html>"""


def test_section_is_not_terminated_by_its_own_name():
    """'II. 사업의 내용' 바로 아래 '1. 사업의 내용' 이 오는 서식이 있다.

    종료 판정이 번호 체계를 가리지 않으므로 이 소제목이 섹션을 즉시 끊었다.
    7,900건 중 67건이 그렇게 잘렸고 18건은 본문이 0자가 됐다.
    """
    s1 = extract_sections(SELF_REPEAT_DOC, SPEC)["S1"]
    assert s1.found is True
    assert s1.char_len_text > 0, "자기 이름 소제목이 섹션을 즉시 끊었다"
    assert "목재 가공업" in s1.text
    assert s1.end_reason == "재무에 관한 사항"


def test_other_section_name_still_terminates():
    """자기 이름만 예외다. 다른 표준 섹션명은 여전히 종료시킨다."""
    s1 = extract_sections(SELF_REPEAT_DOC, SPEC)["S1"]
    assert "재무" not in s1.text.replace("재무에 관한 사항", "")
