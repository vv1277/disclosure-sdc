"""섹션 추출 (프롬프트 P0 작업 4~5).

핵심 원칙
  - 섹션 번호(로마숫자)는 연도마다 바뀐다. 번호가 아니라 '이름'으로 매칭한다.
  - 섹션의 시작 = 그 이름이 나오는 헤더 블록,
    끝  = 그 다음 '최상위' 섹션 헤더 블록.
  - 표기 변형(공백, 중점, 괄호)에 견디도록 정규화 후 매칭한다.
  - 문서 앞머리의 목차(TOC)에도 같은 이름이 나오므로,
    같은 이름의 후보가 여러 개면 '본문 길이가 가장 긴' 후보를 택한다.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable

from bs4 import BeautifulSoup, NavigableString, Tag
from rapidfuzz import fuzz

from src.utils.textnorm import clean_text, norm_name, split_paragraphs

log = logging.getLogger(__name__)

# 블록 경계로 취급할 태그. DART 원본의 커스텀 태그(section-1 등)도 포함.
BLOCK_TAGS = {
    "p", "div", "li", "ul", "ol", "dl", "dd", "dt",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "td", "th", "tr", "tbody", "thead", "caption",
    "section", "article", "center", "blockquote", "pre", "body",
    "section-1", "section-2", "title", "document", "library",
}
SKIP_TAGS = {"script", "style", "head", "meta", "link"}
INLINE_JOIN = " "

# 사업보고서 최상위 섹션의 표준 이름 (연도별 표기 변형 포함).
# 이 목록은 '경계 탐지'용이다. 진단 대상 4개는 config.yaml 의 sections 에서 온다.
TOP_LEVEL_SECTION_NAMES: tuple[str, ...] = (
    "회사의 개요",
    "사업의 내용",
    "재무에 관한 사항",
    "이사의 경영진단 및 분석의견",
    "감사인의 감사의견 등",
    "회계감사인의 감사의견 등",
    "이사회 등 회사의 기관에 관한 사항",
    "주주에 관한 사항",
    "임원 및 직원 등에 관한 사항",
    "임원 및 직원에 관한 사항",
    "계열회사 등에 관한 사항",
    "이해관계자와의 거래내용",
    "대주주 등과의 거래내용",
    "그 밖에 투자자 보호를 위하여 필요한 사항",
    "전문가의 확인",
    "재무제표 등",
    "부속명세서",
    "상세표",
)

_FUZZ_THRESHOLD = 92   # 정규화 후에도 남는 사소한 오타/변형 허용치
_MAX_HEADER_CHARS = 60  # 헤더는 짧다. 이보다 길면 본문 문장으로 본다.


@dataclass
class Block:
    kind: str          # "text" | "table"
    text: str
    html: str = ""


@dataclass
class SectionContent:
    section_id: str
    name: str
    found: bool
    text: str = ""
    tables_html: list[str] = field(default_factory=list)
    tables_text: str = ""
    header_text: str = ""

    @property
    def char_len_text(self) -> int:
        return len(self.text)

    @property
    def char_len_table(self) -> int:
        return len(self.tables_text)

    @property
    def n_paragraphs(self) -> int:
        return len(split_paragraphs(self.text))


# --------------------------------------------------------------------------
# 1) HTML -> 블록 시퀀스
# --------------------------------------------------------------------------

def iter_blocks(html: str, parser: str = "html.parser") -> list[Block]:
    soup = BeautifulSoup(html, parser)
    for tag in soup.find_all(list(SKIP_TAGS)):
        tag.decompose()
    root = soup.body or soup
    out: list[Block] = []
    _walk(root, out)
    return out


def _walk(node: Tag, out: list[Block]) -> None:
    pending: list[str] = []

    def flush() -> None:
        if not pending:
            return
        t = clean_text(INLINE_JOIN.join(pending))
        pending.clear()
        if t:
            out.append(Block("text", t))

    for child in node.children:
        if isinstance(child, NavigableString):
            pending.append(str(child))
            continue
        if not isinstance(child, Tag):
            continue
        name = (child.name or "").lower()
        if name in SKIP_TAGS:
            continue
        if name == "table":
            flush()
            out.append(
                Block("table", clean_text(child.get_text(INLINE_JOIN)), str(child))
            )
            continue
        if name == "br":
            flush()
            continue
        if name in BLOCK_TAGS:
            flush()
            _walk(child, out)
            continue
        pending.append(child.get_text(INLINE_JOIN))  # span, b, font, a ...

    flush()


# --------------------------------------------------------------------------
# 2) 최상위 섹션 헤더 탐지
# --------------------------------------------------------------------------

def _canonical_lookup() -> dict[str, str]:
    return {norm_name(n): n for n in TOP_LEVEL_SECTION_NAMES}


def match_section_name(text: str, candidates: dict[str, str]) -> str | None:
    """블록 텍스트가 최상위 섹션 헤더인지 판정하고, 표준 이름을 돌려준다."""
    if not text or len(text) > _MAX_HEADER_CHARS:
        return None
    key = norm_name(text)
    if not key or len(key) < 3:
        return None
    if key in candidates:
        return candidates[key]
    best_name, best_score = None, 0.0
    for cand_key, cand_name in candidates.items():
        score = fuzz.ratio(key, cand_key)
        if score > best_score:
            best_name, best_score = cand_name, score
    if best_score >= _FUZZ_THRESHOLD:
        return best_name
    return None


def find_headers(blocks: list[Block]) -> list[tuple[int, str, str]]:
    """(블록 인덱스, 표준 섹션명, 원문 헤더 텍스트) 목록. 표 안의 헤더는 제외."""
    candidates = _canonical_lookup()
    headers: list[tuple[int, str, str]] = []
    for i, b in enumerate(blocks):
        if b.kind != "text":
            continue
        name = match_section_name(b.text, candidates)
        if name:
            headers.append((i, name, b.text))
    return headers


# --------------------------------------------------------------------------
# 3) 섹션 추출
# --------------------------------------------------------------------------

def _spans_for(headers: list[tuple[int, str, str]], n_blocks: int, target: str
               ) -> list[tuple[int, int, str]]:
    """target 표준명에 해당하는 (start, end, header_text) 후보 구간들."""
    spans = []
    for pos, (idx, name, htext) in enumerate(headers):
        if name != target:
            continue
        end = headers[pos + 1][0] if pos + 1 < len(headers) else n_blocks
        spans.append((idx, end, htext))
    return spans


def _collect(blocks: list[Block], start: int, end: int) -> tuple[str, list[str], str]:
    """구간의 (본문텍스트, 표 HTML 목록, 표 텍스트)를 분리해서 모은다."""
    texts, tables_html, tables_text = [], [], []
    for b in blocks[start + 1 : end]:
        if b.kind == "table":
            tables_html.append(b.html)
            if b.text:
                tables_text.append(b.text)
        elif b.text:
            texts.append(b.text)
    return "\n".join(texts), tables_html, "\n".join(tables_text)


def extract_sections(
    html: str,
    section_spec: dict[str, dict],
    *,
    parser: str = "html.parser",
) -> dict[str, SectionContent]:
    """config.yaml 의 sections 스펙대로 섹션을 뽑는다.

    section_spec: {"S1": {"name": "사업의 내용", "aliases": [...]}, ...}
    """
    blocks = iter_blocks(html, parser=parser)
    headers = find_headers(blocks)
    n = len(blocks)

    out: dict[str, SectionContent] = {}
    for sid, spec in section_spec.items():
        names = [spec["name"], *spec.get("aliases", [])]
        targets = {
            match_section_name(nm, _canonical_lookup()) or nm for nm in names
        }
        spans: list[tuple[int, int, str]] = []
        for t in targets:
            spans.extend(_spans_for(headers, n, t))

        if not spans:
            out[sid] = SectionContent(sid, spec["name"], found=False)
            continue

        # 목차(TOC) 엔트리는 본문 길이가 0에 가깝다 -> 가장 긴 후보를 택한다.
        best = None
        for start, end, htext in spans:
            text, thtml, ttext = _collect(blocks, start, end)
            score = len(text) + len(ttext)
            if best is None or score > best[0]:
                best = (score, text, thtml, ttext, htext)
        _, text, thtml, ttext, htext = best
        out[sid] = SectionContent(
            sid, spec["name"], found=True,
            text=text, tables_html=thtml, tables_text=ttext, header_text=htext,
        )
    return out


def parse_ok(sections: Iterable[SectionContent], min_found: int = 1) -> bool:
    """문서 단위 파싱 성공 판정: 지정한 개수 이상의 섹션에서 본문을 얻었는가."""
    hits = sum(1 for s in sections if s.found and s.char_len_text > 0)
    return hits >= min_found
