"""보고서 유형 판정 (Phase 1 결정 2)."""
from src.collect.report_type import (
    ATTACHMENT_ADDED,
    MATERIAL_AMENDMENT,
    ORIGINAL,
    classify_report,
    is_body_original,
)
from src.utils.config import load_config

RULES = load_config()["phase1"]["report_types"]


def test_plain_report_is_original():
    c = classify_report("사업보고서 (2024.12)", RULES)
    assert c["report_type"] == ORIGINAL
    assert "대괄호" in c["type_reason"]


def test_attachment_added_is_treated_as_original_body():
    """[첨부추가] 는 원본 제출이다. 본문 텍스트를 바꾸지 않는다."""
    for nm in ("[첨부추가]사업보고서 (2024.12)", "[첨부정정]사업보고서 (2024.12)"):
        c = classify_report(nm, RULES)
        assert c["report_type"] == ATTACHMENT_ADDED
        assert is_body_original(c["report_type"])


def test_material_amendment_is_the_only_real_correction():
    c = classify_report("[기재정정]사업보고서 (2024.12)", RULES)
    assert c["report_type"] == MATERIAL_AMENDMENT
    assert not is_body_original(c["report_type"])


def test_reason_is_recorded_for_reconstruction():
    """나중에 '왜 이 문서를 원본으로 봤는가' 를 재구성할 수 있어야 한다."""
    c = classify_report("[첨부추가]사업보고서 (2024.12)", RULES)
    assert "첨부추가" in c["type_reason"] and c["bracket_tags"] == "첨부추가"


def test_unknown_tag_defaults_to_original_with_reason():
    c = classify_report("[연장결정]사업보고서 (2024.12)", RULES)
    assert c["report_type"] == ORIGINAL
    assert "미분류" in c["type_reason"]
