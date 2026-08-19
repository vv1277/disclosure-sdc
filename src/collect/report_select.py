"""공시검색 결과에서 해당 회계연도의 사업보고서 1건을 고른다 (프롬프트 P0 작업 2).

규칙
  - report_nm 에 '사업보고서' 포함, '분기'/'반기' 제외
  - report_nm 의 (YYYY.MM) 이 대상 회계연도와 일치해야 한다
  - 정정보고서는 is_amendment 플래그로 표시하되, 원본이 있으면 원본을 우선 사용
"""
from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger(__name__)

_PERIOD_RE = re.compile(r"\((\d{4})\.(\d{1,2})\)")
_AMEND_RE = re.compile(r"\[(기재정정|첨부정정|첨부추가|정정|연장결정|변경등록)\]")


def is_amendment(report_nm: str) -> bool:
    return bool(_AMEND_RE.search(report_nm or ""))


def period_year(report_nm: str) -> int | None:
    m = _PERIOD_RE.search(report_nm or "")
    return int(m.group(1)) if m else None


def matches_annual(report_nm: str, include: list[str], exclude: list[str]) -> bool:
    nm = report_nm or ""
    if not any(tok in nm for tok in include):
        return False
    # '[기재정정]사업보고서' 는 통과, '반기보고서' 는 차단
    stripped = _AMEND_RE.sub("", nm)
    return not any(tok in stripped for tok in exclude)


def select_annual_report(
    rows: list[dict[str, Any]],
    fy: int,
    *,
    include: list[str],
    exclude: list[str],
    prefer_original: bool = True,
) -> dict[str, Any] | None:
    """해당 회계연도의 사업보고서 1건. 없으면 None."""
    cands = []
    for r in rows:
        nm = r.get("report_nm", "")
        if not matches_annual(nm, include, exclude):
            continue
        py = period_year(nm)
        if py is not None and py != fy:
            continue
        if py is None:
            # 기간 표기가 없는 예외적 케이스: 접수일자 연도로 추정 (fy+1 에 제출)
            rcept_dt = str(r.get("rcept_dt", ""))
            if not rcept_dt.startswith(str(fy + 1)):
                continue
        cands.append(dict(r, is_amendment=is_amendment(nm)))

    if not cands:
        return None

    originals = [c for c in cands if not c["is_amendment"]]
    pool = originals if (prefer_original and originals) else cands
    # 같은 종류가 여럿이면 가장 먼저 접수된 건
    pool.sort(key=lambda c: str(c.get("rcept_dt", "")))
    chosen = pool[0]
    chosen["n_amendments"] = sum(1 for c in cands if c["is_amendment"])
    return chosen


def search_window(fy: int) -> tuple[str, str]:
    """FY 사업보고서는 통상 FY+1년 3월에 제출된다. 지연 제출까지 감안해 폭을 준다."""
    return f"{fy + 1}0101", f"{fy + 2}0630"
