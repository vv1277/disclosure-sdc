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
