from src.utils.textnorm import (
    char_ngrams,
    clean_text,
    norm_name,
    split_paragraphs,
    strip_leading_number,
    tokenize_eojeol,
)


def test_norm_name_ignores_numbering_and_spacing():
    """번호와 공백/중점/괄호가 달라도 같은 섹션명으로 인식되어야 한다."""
    variants = [
        "II. 사업의 내용",
        "2. 사업의 내용",
        "Ⅱ. 사업의  내용",
        "(2) 사업의내용",
        "II . 사업의 내용",
    ]
    keys = {norm_name(v) for v in variants}
    assert len(keys) == 1, keys
    assert keys.pop() == norm_name("사업의 내용")


def test_norm_name_long_section():
    a = norm_name("XI. 그 밖에 투자자 보호를 위하여 필요한 사항")
    b = norm_name("11. 그밖에 투자자보호를 위하여 필요한 사항")
    assert a == b


def test_norm_name_keeps_distinct_sections_distinct():
    assert norm_name("사업의 내용") != norm_name("사업의 개요")


def test_strip_leading_number_double_numbering():
    assert strip_leading_number("VIII. 8. 임원 및 직원") == "임원 및 직원"


def test_clean_text_removes_footnote_markers_and_nbsp():
    s = "매출액은 1,000억원입니다.(주1) ※ 주석 참조"
    out = clean_text(s)
    assert "(주1)" not in out
    assert "※" not in out
    assert " " not in out
    assert "1,000억원" in out


def test_split_paragraphs_min_chars():
    text = "짧음\n" + "가" * 50 + "\n\n" + "나" * 40
    assert len(split_paragraphs(text)) == 3
    assert len(split_paragraphs(text, min_chars=30)) == 2


def test_tokenize_and_ngrams():
    assert tokenize_eojeol(" 가나 다라  마 ") == ["가나", "다라", "마"]
    assert char_ngrams("가나 다라", 3) == {"가나다", "나다라"}


# ---------------------------------------------------------------------------
# DART 편집기 플레이스홀더 제거 (P0-d 에서 발견)
# ---------------------------------------------------------------------------

def test_editor_placeholder_removed():
    """'◆click◆『수주상황』 삽입' 은 공시 내용이 아니라 작성 도구의 위젯 라벨이다.

    2016/2020 서식 문서의 절반에 전 기업 공통으로 들어 있어, 남겨두면
    '기업 간 공통 변경 문단' 신호를 통째로 오염시킨다.
    """
    assert clean_text("◆click◆『수주상황』 삽입") == ""
    assert clean_text("◆click◆『신용보강 제공 현황』 삽입") == ""
    assert clean_text("◆ click ◆ 『공모자금의 사용내역』 삽입") == ""


def test_editor_placeholder_removed_inline_keeping_real_text():
    got = clean_text("당사의 수주 현황은 다음과 같습니다. ◆click◆『수주상황』 삽입")
    assert "click" not in got and "◆" not in got
    assert got == "당사의 수주 현황은 다음과 같습니다."


def test_normal_text_with_brackets_is_untouched():
    """『』 자체는 정상 텍스트에도 쓰인다. 플레이스홀더 패턴일 때만 지운다."""
    src = "당사는 『회사법』 에 따라 이사회를 운영합니다."
    assert clean_text(src) == src


def test_dart_entity_and_template_filename_removed():
    """`&cr` 엔티티(356건 중 236건)와 서식 파일명(59건)도 편집기 잔재다."""
    assert clean_text("가. 생산능력 및 산출근거&cr (1) 생산능력") == \
        "가. 생산능력 및 산출근거 (1) 생산능력"
    assert clean_text("3. 재무상태 &cr &cr 가. 재무상태") == "3. 재무상태 가. 재무상태"
    assert clean_text("11011#*_수주상황.dsl 1. 사업의 개요") == "1. 사업의 개요"
    assert clean_text("11011#*_수주상황.dsl &cr") == ""


def test_ampersand_words_that_are_not_entities_survive():
    """'&cr' 만 지운다. 정상 텍스트의 & 는 건드리지 않는다."""
    src = "당사는 R&D 및 M&A 를 통해 성장하였습니다."
    assert clean_text(src) == src
