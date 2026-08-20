"""S4(임원 및 직원 등에 관한 사항)의 임원 현황 표를 구조화 데이터로 추출한다.

배경 (Gate 0 결론)
  S4 는 본문 텍스트가 평균 1,081자에 불과하다. 내용이 사실상 전부 표이기 때문이다.
  텍스트 섹션에서 탈락시키고, 표를 구조화 데이터 소스로 재분류했다.

입력은 섹션 추출이 이미 떼어낸 표 HTML 이다. 파서를 새로 만들지 않고
`SectionContent.tables_html` 을 그대로 받는다.

실데이터에서 확인한 함정
  1) DART 임원 표는 **2행 헤더**를 쓴다.
       <th rowspan=2>성명</th> ... <th colspan=2>소유주식수</th>
       <th>의결권있는 주식</th><th>의결권없는 주식</th>
     colspan/rowspan 을 펼치지 않으면 컬럼 인덱스가 통째로 어긋나서
     '재직기간' 자리에 '최대주주와의관계' 값이 들어간다.
  2) 헤더 2행째('의결권있는 주식')가 데이터 행으로 오인되어 성명이 된다.
  3) S4 안에는 임원 표 말고도 직원 현황·보수 표가 수십 개 있다.
     성명 컬럼만 보고 고르면 엉뚱한 표까지 긁어온다.

정규화 컬럼
  name, position, is_registered, full_time, duty,
  tenure_raw, tenure_months, term_end_raw

알려진 한계 (Phase 1 에서 마저 해결할 것)
  - **중첩 표**: DART 표 안에 표가 또 들어 있는 경우가 있다. `table_to_grid` 의
    `find_all("tr")` 이 재귀 탐색이라 안쪽 표의 행이 바깥 격자에 섞여 들어가고,
    그 결과 헤더와 데이터 행이 어긋난다. 89건 표본에서 SKC 2024 가 이 경우로
    0행이 나온다. 안쪽 표를 먼저 분리한 뒤 격자를 만들어야 한다.
  - `is_registered` 가 None 인 문서가 있다. 등기임원 표와 미등기임원 표를 따로
    두고 표 제목으로만 구분하는 서식이 있어, 헤더에 '등기임원여부' 컬럼이 없다.
    표 바로 앞의 캡션을 읽어 보완해야 한다.
  - 검증은 89건 표본 중 4건을 눈으로 확인한 수준이다. Phase 1 에서 전량
    추출한 뒤 임원 수 분포로 이상치를 잡아야 한다.
"""
from __future__ import annotations

import logging
import re
from typing import Any

import pandas as pd
from bs4 import BeautifulSoup

from src.utils.textnorm import clean_text

log = logging.getLogger(__name__)

_HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "name": ("성명", "이름"),
    "gender": ("성별",),
    "birth": ("출생년월", "생년월일", "출생년월일"),
    "position": ("직위", "직책"),
    "is_registered": ("등기임원여부", "등기여부", "등기임원"),
    "full_time": ("상근여부", "상근"),
    "duty": ("담당업무",),
    "career": ("주요경력",),
    "shares": ("소유주식수",),
    "relation": ("최대주주와의관계",),
    "tenure": ("재직기간",),
    "term_end": ("임기만료일",),
}

# 임원 현황 표로 인정할 최소 조건: 성명 + 아래 중 2개 이상
_REQUIRED_COMPANIONS = ("position", "is_registered", "tenure", "term_end", "relation")

# 이 단어가 헤더에 있으면 임원 표가 아니다 (직원 현황·보수 표 등)
_REJECT_HEADER = ("직원수", "평균근속연수", "연간급여총액", "1인평균급여액",
                  "보수총액", "인원수", "사업부문")

_TRUE_TOKENS = ("등기임원", "사내이사", "사외이사", "감사위원", "감사", "예", "y", "o",
                "상근", "해당")
_FALSE_TOKENS = ("미등기", "비상근", "아니오", "아니요", "n", "x", "해당없음", "없음", "-")

_TENURE = re.compile(r"^(?:(\d+)\s*년)?\s*(?:(\d+)\s*(?:개월|월))?$")


def _norm(s: str) -> str:
    return re.sub(r"[\s\(\)\[\]·ㆍ\.\,\-]+", "", str(s or "")).lower()


def _match_header(cell: str) -> str | None:
    key = _norm(cell)
    if not key:
        return None
    for canon, aliases in _HEADER_ALIASES.items():
        if any(key == _norm(a) for a in aliases):
            return canon
    if "등기" in key and "미등기" not in key:
        return "is_registered"
    if "상근" in key:
        return "full_time"
    return None


def parse_tenure_months(text: str) -> float | None:
    """'3년 2개월' -> 38.0. 해석 불가면 None (0 으로 채우지 않는다)."""
    t = re.sub(r"\s+", "", str(text or ""))
    if not t or t in ("-", "해당없음", "없음"):
        return None
    m = _TENURE.match(t)
    if not m or (m.group(1) is None and m.group(2) is None):
        return None
    return int(m.group(1) or 0) * 12 + int(m.group(2) or 0)


def parse_bool(text: str) -> bool | None:
    """'등기임원'/'미등기' 를 bool 로. 판단 불가면 None."""
    t = re.sub(r"\s+", "", str(text or "")).lower()
    if not t:
        return None
    for f in _FALSE_TOKENS:      # '미등기' 가 '등기' 를 포함하므로 부정을 먼저 본다
        if f in t:
            return False
    for tok in _TRUE_TOKENS:
        if tok in t:
            return True
    return None


# --------------------------------------------------------------------------
# 표 -> 격자 (colspan / rowspan 펼치기)
# --------------------------------------------------------------------------

def table_to_grid(table_html: str, *, max_rows: int = 2000) -> list[list[str]]:
    """colspan/rowspan 을 펼쳐 직사각형 격자로 만든다."""
    soup = BeautifulSoup(table_html, "html.parser")
    grid: list[list[str | None]] = []

    for r, tr in enumerate(soup.find_all("tr")):
        if r >= max_rows:
            break
        while len(grid) <= r:
            grid.append([])
        col = 0
        for cell in tr.find_all(["td", "th"]):
            while col < len(grid[r]) and grid[r][col] is not None:
                col += 1            # rowspan 으로 이미 채워진 칸은 건너뛴다
            text = clean_text(cell.get_text(" "))
            try:
                cs = max(1, int(cell.get("colspan", 1)))
                rs = max(1, int(cell.get("rowspan", 1)))
            except (TypeError, ValueError):
                cs, rs = 1, 1
            cs, rs = min(cs, 50), min(rs, 200)
            for dr in range(rs):
                rr = r + dr
                while len(grid) <= rr:
                    grid.append([])
                for dc in range(cs):
                    cc = col + dc
                    while len(grid[rr]) <= cc:
                        grid[rr].append(None)
                    if grid[rr][cc] is None:
                        grid[rr][cc] = text
            col += cs

    width = max((len(r) for r in grid), default=0)
    return [[(c if c is not None else "") for c in r] + [""] * (width - len(r))
            for r in grid]


def _header_score(row: list[str]) -> int:
    return sum(1 for c in row if _match_header(c))


def find_header(grid: list[list[str]]) -> tuple[int, dict[int, str]] | None:
    """헤더 블록(최대 3행)을 찾아 컬럼 -> 정규화 이름 매핑을 만든다.

    Returns: (데이터 시작 행 인덱스, {열 인덱스: 정규화 이름})
    """
    best: tuple[int, int, dict[int, str]] | None = None   # (score, data_start, map)
    for i in range(min(len(grid), 6)):
        if _header_score(grid[i]) < 2:
            continue
        for span in (1, 2, 3):                 # 헤더가 몇 행에 걸쳐 있는가
            if i + span > len(grid):
                break
            block = grid[i:i + span]
            width = max(len(r) for r in block)
            colmap: dict[int, str] = {}
            for j in range(width):
                # 같은 열의 헤더 행들을 합쳐서 본다 (2행 헤더 대응)
                for row in block:
                    cell = row[j] if j < len(row) else ""
                    canon = _match_header(cell)
                    if canon and canon not in colmap.values():
                        colmap[j] = canon
                        break
            names = set(colmap.values())
            if "name" not in names:
                continue
            if len(names & set(_REQUIRED_COMPANIONS)) < 2:
                continue
            joined = _norm("".join("".join(r) for r in block))
            if any(_norm(b) in joined for b in _REJECT_HEADER):
                continue
            score = len(colmap)
            if best is None or score > best[0]:
                best = (score, i + span, colmap)
    if best is None:
        return None
    return best[1], best[2]


def _is_header_echo(values: dict[str, str]) -> bool:
    """헤더 2행째가 데이터 행으로 새어 들어온 경우를 걸러낸다."""
    name = values.get("name", "")
    return _match_header(name) is not None or _norm(name) in (
        _norm("의결권있는주식"), _norm("의결권없는주식"))


def extract_officers(tables_html: list[str]) -> pd.DataFrame:
    out: list[dict[str, Any]] = []
    for t in tables_html:
        grid = table_to_grid(t)
        if not grid:
            continue
        found = find_header(grid)
        if not found:
            continue
        start, colmap = found
        for row in grid[start:]:
            values = {canon: (row[j] if j < len(row) else "")
                      for j, canon in colmap.items()}
            name = (values.get("name") or "").strip()
            if not name or len(name) > 20:
                continue
            if name in ("계", "합계", "소계", "-"):
                continue
            if _is_header_echo(values):
                continue
            out.append({
                "name": name,
                "position": values.get("position", ""),
                "is_registered": parse_bool(values.get("is_registered", "")),
                "full_time": parse_bool(values.get("full_time", "")),
                "duty": values.get("duty", ""),
                "tenure_raw": values.get("tenure", ""),
                "tenure_months": parse_tenure_months(values.get("tenure", "")),
                "term_end_raw": values.get("term_end", ""),
            })

    df = pd.DataFrame(out)
    if df.empty:
        return df
    # 같은 임원이 여러 표에 중복 등장한다 (요약표 + 상세표)
    return df.drop_duplicates(subset=["name", "position", "duty"]).reset_index(drop=True)


def has_nested_table(tables_html: list[str]) -> bool:
    """표 안에 표가 또 있는가. 중첩이면 격자 생성이 어긋난다 (알려진 한계)."""
    for t in tables_html:
        soup = BeautifulSoup(t, "html.parser")
        outer = soup.find("table")
        if outer is not None and outer.find("table") is not None:
            return True
    return False


def extract_for_document(tables_html: list[str], *, corp_code: str, fy: int,
                         rcept_no: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    """(임원 표, 진단 지표).

    파서를 지금 고치지 않는 대신, 나중에 규모를 보고 판단할 수 있도록
    문서마다 진단 지표를 남긴다 (Phase 1 요구 C).
    """
    df = extract_officers(tables_html)
    n = len(df)
    null_rate = (float(df["is_registered"].isna().mean())
                 if n else None)
    diag: dict[str, Any] = {
        "corp_code": corp_code, "fy": fy, "rcept_no": rcept_no,
        "n_tables": len(tables_html),
        "n_officers_extracted": n,
        "has_nested_table": has_nested_table(tables_html),
        "is_registered_null_rate": round(null_rate, 4) if null_rate is not None else None,
        "extraction_method": "grid_colspan_v1",
    }
    if n:
        df = df.copy()
        df.insert(0, "rcept_no", rcept_no)
        df.insert(0, "fy", fy)
        df.insert(0, "corp_code", corp_code)
    return df, diag
