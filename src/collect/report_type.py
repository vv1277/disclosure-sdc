"""보고서 유형 판정 — 원본 / 첨부추가 / 기재정정 (Phase 1 결정 2).

왜 분리하는가
  처음에는 대괄호가 붙은 것을 전부 '정정' 으로 봤다. 그러자 46건이
  '정정본만 존재' 로 분류되어 표본에서 빠질 뻔했다. DART 를 직접 조회해 보니
  그 46건은 전부 `[첨부추가]사업보고서` 였고, **그것이 원본 제출**이었다.
  (첨부추가는 3월 제출 창구, 기재정정은 4~8월로 후행.
   바디텍메드 2024 는 [첨부추가] 단 1건만 존재해 원본이 따로 없다.)

  본문 텍스트를 바꾸는 것은 `[기재정정]` 뿐이다. 첨부만 붙는 것은 leakage 와
  무관하다. 그래서 판정 규칙을 config 로 빼고, 판정 '사유' 를 컬럼으로 남겨
  나중에 "왜 이 문서를 원본으로 봤는가" 를 재구성할 수 있게 한다.
"""
from __future__ import annotations

import re
from typing import Any

ORIGINAL = "original"
ATTACHMENT_ADDED = "attachment_added"
MATERIAL_AMENDMENT = "material_amendment"

_BRACKET = re.compile(r"\[([^\]]*)\]")


def bracket_tags(report_nm: str) -> list[str]:
    """'[기재정정]사업보고서 (2024.12)' -> ['기재정정']"""
    return [t.strip() for t in _BRACKET.findall(str(report_nm or ""))]


def classify_report(report_nm: str, rules: dict[str, list[str]]) -> dict[str, Any]:
    """(유형, 판정 사유). 사유를 남겨야 나중에 재구성할 수 있다."""
    tags = bracket_tags(report_nm)
    if not tags:
        return {"report_type": ORIGINAL, "type_reason": "대괄호 표기 없음",
                "bracket_tags": ""}

    joined = ", ".join(tags)
    material = rules.get(MATERIAL_AMENDMENT, [])
    attach = rules.get(ATTACHMENT_ADDED, [])

    hit_material = [t for t in tags if any(m in t for m in material)]
    if hit_material:
        return {"report_type": MATERIAL_AMENDMENT,
                "type_reason": f"본문 정정 표기: {', '.join(hit_material)}",
                "bracket_tags": joined}

    hit_attach = [t for t in tags if any(a in t for a in attach)]
    if hit_attach:
        return {"report_type": ATTACHMENT_ADDED,
                "type_reason": f"첨부 관련 표기(본문 불변): {', '.join(hit_attach)}",
                "bracket_tags": joined}

    return {"report_type": ORIGINAL,
            "type_reason": f"미분류 표기, 원본으로 간주: {joined}",
            "bracket_tags": joined}


def is_body_original(report_type: str) -> bool:
    """본문이 원본 그대로인가. 첨부추가는 원본으로 본다."""
    return report_type in (ORIGINAL, ATTACHMENT_ADDED)
