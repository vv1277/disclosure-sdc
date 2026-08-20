"""소송·제재 표 구조화 (Phase 4.4).

배경
  S3(그 밖에 투자자 보호를 위하여 필요한 사항)를 표본에 넣은 이유가 소송이다.
  CMN(2020) 이 임원 변화와 함께 가장 강한 예측력을 발견한 채널이고, 논문
  Table 3 의 한 행을 담당한다. 그런데 한국 사업보고서의 소송은 대부분 표로
  작성되므로, 표를 텍스트에서 분리한 현재 상태로는 이 채널이 통째로 소실된다.
  임원 feature 와 같은 방식으로 표에서 직접 뽑는다.

실데이터에서 확인한 것
  - 헤더가 기업마다 크게 다르다.
      소송의 내용 | 소제기일 | 원고 | 소송가액 | 진행현황 | 최근판결일
      구분 | 계류법원 | 소송가액 | 진행상황
      소송내용(청구취지, 청구원인 등) | 소 제기일 | 소송 당사자(피고) | 소송가액(억원)
    -> 컬럼명 매핑 사전을 config 로 분리한다.
  - **금액 단위가 헤더에 박혀 있다**: `소송가액(억원)`, `소송가액(원화단위: 천원)`.
    셀에만 단위가 있는 경우도 있어 둘 다 본다. 원 단위로 통일한다.
  - '해당사항 없음' 은 소송 0건이지 결측이 아니다.

한계
  캡션(표 바로 앞 문단)은 현재 섹션 추출이 표와 텍스트를 분리해 보관하므로
  인접 관계가 남아 있지 않다. 지금은 **헤더 컬럼명과 표 내부 문구**로만
  판정한다. 캡션까지 쓰려면 iter_blocks 가 표마다 직전 텍스트 블록을 기록해야
  하고, 그러면 파싱 캐시를 다시 만들어야 한다.
"""
from __future__ import annotations

import logging
import re
from typing import Any

import pandas as pd
from bs4 import BeautifulSoup

from src.parse.officers import table_to_grid
from src.utils.textnorm import clean_text

log = logging.getLogger(__name__)

_NUM = re.compile(r"[-+]?[\d,]*\.?\d+")
_UNIT_IN_TEXT = re.compile(r"(조|억|백만|천)\s*원")


def _norm(s: str) -> str:
    return re.sub(r"[\s\(\)\[\]·ㆍ\.\,\:\-]+", "", str(s or "")).lower()


def build_alias_lookup(aliases: dict[str, list[str]]) -> dict[str, str]:
    """정규화된 컬럼명 -> 표준 필드명."""
    out: dict[str, str] = {}
    for canon, names in aliases.items():
        for n in names:
            out[_norm(n)] = canon
    return out


def match_column(cell: str, lookup: dict[str, str]) -> str | None:
    """헤더 셀을 표준 필드로. 정확 일치 후 부분 포함으로 완화한다."""
    key = _norm(cell)
    if not key:
        return None
    if key in lookup:
        return lookup[key]
    # '소송가액(억원)' 처럼 단위가 붙은 경우
    for alias_key, canon in lookup.items():
        if len(alias_key) >= 2 and alias_key in key:
            return canon
    return None


def parse_amount_unit(header_cell: str, units: dict[str, int]) -> int:
    """헤더의 '(억원)', '(원화단위: 천원)' 에서 배수를 읽는다. 없으면 1(원)."""
    m = _UNIT_IN_TEXT.search(str(header_cell or ""))
    if m:
        return int(units.get(m.group(1), 1))
    return 1


def parse_amount(cell: str, unit_multiplier: int, units: dict[str, int]) -> float | None:
    """셀의 금액을 원 단위로. 해석 불가면 None (0 으로 채우지 않는다)."""
    t = str(cell or "").strip()
    if not t:
        return None
    m = _NUM.search(t.replace(" ", ""))
    if not m:
        return None
    try:
        val = float(m.group(0).replace(",", ""))
    except ValueError:
        return None
    # 셀 자체에 단위가 있으면 그것이 헤더보다 우선한다
    cell_unit = _UNIT_IN_TEXT.search(t)
    mult = int(units.get(cell_unit.group(1), 1)) if cell_unit else unit_multiplier
    return val * mult


def is_none_marker(text: str, none_tokens: list[str], *, max_len: int = 40) -> bool:
    """'해당사항 없음' 류 표기인가.

    주의: 토큰을 정규화하면 '-' 는 빈 문자열이 된다. 빈 문자열은 모든 텍스트의
    부분문자열이라, 걸러내지 않으면 **모든 표가 '소송 없음'** 이 되어 버린다
    (실제로 89건 전부가 그렇게 판정된 적이 있다).
    그래서 빈 토큰은 버리고, 부분 일치는 짧은 텍스트에만 허용한다.
    """
    raw = str(text or "").strip()
    if not raw:
        return False
    if raw in {tok.strip() for tok in none_tokens if tok}:
        return True
    t = _norm(raw)
    toks = [x for x in (_norm(tok) for tok in none_tokens if tok) if x]
    if not t or not toks:
        return False
    if any(tok == t for tok in toks):
        return True
    return len(raw) <= max_len and any(tok in t for tok in toks)


# --------------------------------------------------------------------------
# 표 식별
# --------------------------------------------------------------------------

def classify_table(grid: list[list[str]], cfg_law: dict) -> dict[str, Any]:
    """표가 소송/제재 표인지 판정하고 근거를 남긴다."""
    lookup = build_alias_lookup(cfg_law["column_aliases"])
    flat = " ".join(" ".join(r) for r in grid[:3])
    all_text = " ".join(" ".join(r) for r in grid)

    colmap: dict[int, str] = {}
    unit_by_col: dict[int, int] = {}
    header_row = -1
    for i, row in enumerate(grid[:4]):
        cand: dict[int, str] = {}
        for j, cell in enumerate(row):
            canon = match_column(cell, lookup)
            if canon and canon not in cand.values():
                cand[j] = canon
        if len(cand) >= 2:
            colmap, header_row = cand, i
            for j, canon in cand.items():
                if canon == "amount":
                    unit_by_col[j] = parse_amount_unit(
                        row[j], cfg_law["amount_units"])
            break

    kw = [k for k in cfg_law["caption_keywords"] if k in all_text]
    sanction_kw = [k for k in cfg_law["sanction_keywords"] if k in all_text]
    n_data = max(0, len(grid) - (header_row + 1)) if header_row >= 0 else 0

    is_lawsuit = bool(colmap) and (
        "amount" in colmap.values() or "status" in colmap.values()
        or "case_no" in colmap.values() or bool(kw))
    return {
        "is_lawsuit": is_lawsuit,
        "is_sanction": bool(sanction_kw) and not is_lawsuit,
        "header_row": header_row,
        "colmap": colmap,
        "unit_by_col": unit_by_col,
        "n_data_rows": n_data,
        "matched_keywords": ", ".join(kw),
        "header_text": flat[:200],
        # 표 전체가 '해당사항 없음' 한 줄인 경우만 소송 0건으로 본다
        "has_none_marker": is_none_marker(all_text, cfg_law["none_tokens"]),
    }


def extract_lawsuits(tables_html: list[str], cfg_law: dict
                     ) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    """(소송 행 표, 표별 판정 근거)."""
    rows: list[dict[str, Any]] = []
    audits: list[dict[str, Any]] = []

    for k, t in enumerate(tables_html):
        grid = table_to_grid(t)
        if not grid:
            continue
        info = classify_table(grid, cfg_law)
        info["table_index"] = k
        audits.append(info)
        if not info["is_lawsuit"] or info["header_row"] < 0:
            continue

        colmap, units = info["colmap"], info["unit_by_col"]
        lookup_all = build_alias_lookup(cfg_law["column_aliases"])
        for row in grid[info["header_row"] + 1:]:
            vals = {c: (row[j] if j < len(row) else "") for j, c in colmap.items()}
            joined = " ".join(v for v in vals.values() if v).strip()
            if not joined:
                continue
            # 헤더가 다시 나온 행 (표가 페이지마다 헤더를 반복한다)
            n_header_like = sum(
                1 for v in vals.values()
                if v and match_column(v, lookup_all) is not None)
            if n_header_like >= max(2, len(vals) - 1):
                continue
            # 합계/소계 행
            if _norm(vals.get("content", "")) in ("계", "합계", "소계"):
                continue
            if is_none_marker(joined, cfg_law["none_tokens"]):
                continue
            amount = None
            for j, c in colmap.items():
                if c == "amount":
                    amount = parse_amount(row[j] if j < len(row) else "",
                                          units.get(j, 1), cfg_law["amount_units"])
                    break
            rows.append({
                "table_index": k,
                "case_no": vals.get("case_no", ""),
                "plaintiff": vals.get("plaintiff", ""),
                "defendant": vals.get("defendant", ""),
                "content": vals.get("content", "")[:300],
                "status": vals.get("status", ""),
                "filed_on": vals.get("filed_on", ""),
                "amount_krw": amount,
            })

    return pd.DataFrame(rows), audits


def extract_for_document(tables_html: list[str], cfg_law: dict, *,
                         corp_code: str, fy: int, rcept_no: str, section: str
                         ) -> tuple[pd.DataFrame, dict[str, Any]]:
    df, audits = extract_lawsuits(tables_html, cfg_law)
    n_law_tables = sum(1 for a in audits if a["is_lawsuit"])
    n_sanction_tables = sum(1 for a in audits if a["is_sanction"])
    any_none = any(a["has_none_marker"] for a in audits)

    diag = {
        "corp_code": corp_code, "fy": fy, "rcept_no": rcept_no, "section": section,
        "n_tables": len(tables_html),
        "n_lawsuit_tables": n_law_tables,
        "n_sanction_tables": n_sanction_tables,
        "n_lawsuit_rows": len(df),
        # 표는 있는데 행이 0 이고 '해당사항 없음' 이 있으면 소송 0건 (결측 아님)
        "explicit_none": bool(any_none and len(df) == 0),
        "extraction_method": "grid_alias_v1",
    }
    if not df.empty:
        df = df.copy()
        df.insert(0, "section", section)
        df.insert(0, "rcept_no", rcept_no)
        df.insert(0, "fy", fy)
        df.insert(0, "corp_code", corp_code)
    return df, diag
