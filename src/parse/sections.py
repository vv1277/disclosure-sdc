"""섹션 추출 (프롬프트 P0 작업 4~5, P0-c Part 2~3 수정 반영).

핵심 원칙
  - 섹션 번호(로마숫자)는 연도마다 바뀐다. 번호가 아니라 '이름'으로 매칭한다.
  - 섹션의 시작 = 그 이름이 나오는 헤더 블록,
    끝  = 그 다음 '최상위' 섹션 헤더 블록.
  - 표기 변형(공백, 중점, 괄호)에 견디도록 정규화 후 매칭한다.
  - 문서 앞머리의 목차(TOC)에도 같은 이름이 나오므로,
    같은 이름의 후보가 여러 개면 '본문 길이가 가장 긴' 후보를 택한다.

P0-c 에서 고친 것
  1) 블록 분해가 화이트리스트 방식이라 DART 커스텀 컨테이너 태그
     (<TABLE-GROUP>, <SECTION-3>, <COVER>, <LIBRARY> ...) 안의 <table> 이
     인라인으로 취급되어 표 전체가 본문 문단으로 새어 들어왔다.
     -> 인라인 태그만 블랙리스트로 지정하고, 나머지는 전부 블록으로 재귀한다.
        <table> 은 어디에 있든 표 블록으로 떼어낸다.
  2) '1. 회사의 개요', '2. 재무 등에 관한 사항' 같은 하위 소제목이
     최상위 헤더로 오인되어 섹션이 잘못 잘렸다.
     -> 정규화 후 '정확 일치'를 요구하고(퍼지 92 -> 97), 문서가 로마숫자 번호
        체계를 쓰면 그 문서에서는 로마숫자 헤더만 섹션 '시작'으로 인정한다.
        단, 섹션 '종료' 판정은 번호 체계를 가리지 않는다 — 종료를 놓치면
        섹션이 문서 끝까지 흐르는 큰 피해가 나지만, 조금 이르게 끊는 것은
        피해가 작기 때문이다. (실제로 '1. 전문가의 확인'이 XI 섹션의 정당한
        종료 헤더인 문서가 다수 있다)
  3) 종료 헤더를 못 찾아도 성공으로 집계되어, 섹션이 문서 끝까지 흘러
     재무제표 주석(K-IFRS)이 통째로 섞여 들어갔다.
     -> require_terminator=True 면 종료 헤더가 없을 때 found=False 로 강등한다.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Iterable

from bs4 import BeautifulSoup, NavigableString, Tag
from rapidfuzz import fuzz

from src.parse.paragraphs import merge_short_paragraphs
from src.utils.textnorm import (
    clean_text, norm_name, split_paragraphs, strip_artifacts)

log = logging.getLogger(__name__)

# 인라인 태그만 열거한다. 나머지 미지의 태그는 전부 '블록 컨테이너'로 보고
# 재귀한다. DART 원문은 <TE>, <TU>, <TABLE-GROUP>, <SECTION-3>, <COVER> 등
# 표준 HTML 에 없는 태그를 쓰므로 화이트리스트 방식은 반드시 실패한다.
INLINE_TAGS = {
    "span", "b", "i", "u", "em", "strong", "font", "a", "sub", "sup",
    "small", "big", "strike", "s", "tt", "code", "label", "abbr", "mark",
    "ins", "del", "q", "cite", "var", "kbd", "samp", "bdo", "wbr",
}
SKIP_TAGS = {"script", "style", "head", "meta", "link", "col", "colgroup"}
INLINE_JOIN = " "

# 사업보고서 표준 목차 전체. 이 중 '어느 것이든' 다음에 나오면 현재 섹션을 종료한다.
# (P0-c Part 2-4). 연도별 표기 변형을 같은 항목으로 묶는다.
TOP_LEVEL_SECTIONS: dict[str, tuple[str, ...]] = {
    "회사의 개요": ("회사의 개요",),
    "사업의 내용": ("사업의 내용",),
    "재무에 관한 사항": ("재무에 관한 사항",),
    "감사인의 감사의견 등": (
        "감사인의 감사의견 등",
        "회계감사인의 감사의견 등",
        "감사인의 감사의견",
    ),
    "이사의 경영진단 및 분석의견": ("이사의 경영진단 및 분석의견",),
    "이사회 등 회사의 기관에 관한 사항": (
        "이사회 등 회사의 기관에 관한 사항",
        "이사회 등 회사의 기관 및 계열회사에 관한 사항",
    ),
    "주주에 관한 사항": ("주주에 관한 사항",),
    "임원 및 직원 등에 관한 사항": (
        "임원 및 직원 등에 관한 사항",
        "임원 및 직원에 관한 사항",
    ),
    "계열회사 등에 관한 사항": ("계열회사 등에 관한 사항",),
    "이해관계자와의 거래내용": ("이해관계자와의 거래내용",),
    "대주주 등과의 거래내용": ("대주주 등과의 거래내용",),
    "그 밖에 투자자 보호를 위하여 필요한 사항": (
        "그 밖에 투자자 보호를 위하여 필요한 사항",
    ),
    "재무제표 등": ("재무제표 등", "재무제표"),
    "부속명세서": ("부속명세서",),
    "전문가의 확인": ("전문가의 확인",),
    "상세표": ("상세표",),
}

# 하위 호환: 기존 코드/테스트가 참조하던 평면 목록
TOP_LEVEL_SECTION_NAMES: tuple[str, ...] = tuple(
    v for variants in TOP_LEVEL_SECTIONS.values() for v in variants
)

_FUZZ_THRESHOLD = 97    # 정규화 후에도 남는 사소한 변형만 허용 (P0-c 에서 92 -> 97)
_MAX_HEADER_CHARS = 60  # 헤더는 짧다. 이보다 길면 본문 문장으로 본다.
_ROMAN_MIN_FOR_STRICT = 3   # 로마숫자 헤더가 이만큼 있으면 그 문서는 로마숫자 체계

_ROMAN_PREFIX = re.compile(r"^\s*[\(\[<]?\s*([IVXLivxl]{1,7})\s*[\)\]>\.\-–—:]")
_ARABIC_PREFIX = re.compile(r"^\s*[\(\[<]?\s*(\d{1,2})\s*[\)\]>\.\-–—:]")

END_REASON_EOF = "EOF"


@dataclass
class Block:
    kind: str          # "text" | "table"
    text: str
    html: str = ""
    tag: str = ""      # 원본 태그명 (헤더 판정 보조)
    index: int = -1    # 문서 내 블록 순번 (표를 떼어내도 원래 위치를 안다)


@dataclass
class TableRef:
    """섹션에서 떼어낸 표 하나. 원래 위치와 캡션을 함께 들고 있는다.

    표를 본문에서 분리하면 '표 제목만 남은 문단' 이 고아가 된다. 나중에
    소송 표처럼 캡션으로 표를 식별해야 할 때 이 연결이 없으면 불가능하다.
    """
    block_index: int          # 문서 내 블록 순번
    order_in_section: int     # 섹션 안에서 몇 번째 표인가
    html: str
    text: str
    caption: str = ""         # 표 직전 텍스트 블록 (짧을 때만)
    caption_block_index: int = -1

    @property
    def n_chars(self) -> int:
        return len(self.text)


@dataclass
class SectionContent:
    section_id: str
    name: str
    found: bool
    text: str = ""
    paragraphs: list[str] = field(default_factory=list)
    tables_html: list[str] = field(default_factory=list)
    tables_text: str = ""
    tables: list["TableRef"] = field(default_factory=list)
    paragraph_indices: list[int] = field(default_factory=list)
    start_header: str = ""
    end_header: str = ""          # 종료 헤더 원문. 없으면 "" 이고 end_reason=="EOF"
    end_reason: str = ""          # 종료시킨 표준 섹션명 또는 "EOF"
    has_body: bool = False        # 시작 헤더는 찾았고 본문도 있었는가 (강등 전 상태)

    @property
    def char_len_text(self) -> int:
        return len(self.text)

    @property
    def char_len_table(self) -> int:
        return len(self.tables_text)

    @property
    def n_paragraphs(self) -> int:
        return len(self.paragraphs)


# --------------------------------------------------------------------------
# 1) HTML -> 블록 시퀀스
# --------------------------------------------------------------------------

def iter_blocks(html: str, parser: str = "html.parser", *,
                remove_artifacts: bool = True) -> list[Block]:
    """문서를 블록 시퀀스로 평탄화한다. 표는 통째로 하나의 표 블록이 된다."""
    soup = BeautifulSoup(html, parser)
    for tag in soup.find_all(list(SKIP_TAGS)):
        tag.decompose()
    root = soup.body or soup
    out: list[Block] = []
    _walk(root, out, remove_artifacts=remove_artifacts)
    for i, b in enumerate(out):
        b.index = i          # 표를 떼어내도 원래 위치를 알 수 있게 순번을 박아 둔다
    return out


def _walk(node: Tag, out: list[Block], *, remove_artifacts: bool = True) -> None:
    pending: list[str] = []
    tag_name = (node.name or "").lower()

    def flush() -> None:
        if not pending:
            return
        t = clean_text(INLINE_JOIN.join(pending), remove_artifacts=remove_artifacts)
        pending.clear()
        if t:
            out.append(Block("text", t, tag=tag_name))

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
            # 표는 어디에 묻혀 있든 여기서 떼어낸다. 절대 본문 텍스트로 흘리지 않는다.
            flush()
            out.append(
                Block("table",
                      clean_text(child.get_text(INLINE_JOIN),
                                 remove_artifacts=remove_artifacts),
                      html=str(child), tag="table")
            )
            continue
        if name == "br":
            flush()
            continue
        if name in INLINE_TAGS:
            pending.append(child.get_text(INLINE_JOIN))
            continue

        # 미지의 태그는 블록 컨테이너로 본다 (DART 커스텀 태그 대응).
        flush()
        _walk(child, out, remove_artifacts=remove_artifacts)

    flush()


# --------------------------------------------------------------------------
# 2) 최상위 섹션 헤더 탐지
# --------------------------------------------------------------------------

def _canonical_lookup() -> dict[str, str]:
    """정규화된 변형 이름 -> 표준 섹션명."""
    out: dict[str, str] = {}
    for canonical, variants in TOP_LEVEL_SECTIONS.items():
        for v in variants:
            out[norm_name(v)] = canonical
    return out


def numbering_style(text: str) -> str:
    """헤더 원문의 번호 체계: 'roman' | 'arabic' | 'none'."""
    if _ROMAN_PREFIX.match(text):
        return "roman"
    if _ARABIC_PREFIX.match(text):
        return "arabic"
    return "none"


def match_section_name(text: str, candidates: dict[str, str] | None = None) -> str | None:
    """블록 텍스트가 최상위 섹션 헤더인지 판정하고, 표준 이름을 돌려준다."""
    if candidates is None:
        candidates = _canonical_lookup()
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
    return best_name if best_score >= _FUZZ_THRESHOLD else None


def find_all_header_candidates(blocks: list[Block]) -> list[tuple[int, str, str, str]]:
    """표준 목차 이름과 일치하는 모든 블록: (인덱스, 표준명, 원문, 번호체계)."""
    candidates = _canonical_lookup()
    out: list[tuple[int, str, str, str]] = []
    for i, b in enumerate(blocks):
        if b.kind != "text":
            continue
        name = match_section_name(b.text, candidates)
        if name:
            out.append((i, name, b.text, numbering_style(b.text)))
    return out


def find_headers(blocks: list[Block]) -> list[tuple[int, str, str]]:
    """섹션 '시작'으로 인정할 최상위 헤더 목록.

    문서가 로마숫자 번호 체계를 쓰면(로마숫자 헤더 >= 3개) 그 문서에서는
    로마숫자 헤더만 최상위로 인정한다. '1. 회사의 개요' 같은 하위 소제목이
    최상위로 오인되어 섹션을 잘못 자르는 것을 막는다.
    """
    raw = find_all_header_candidates(blocks)
    n_roman = sum(1 for _, _, _, style in raw if style == "roman")
    if n_roman >= _ROMAN_MIN_FOR_STRICT:
        kept = [r for r in raw if r[3] == "roman"]
        dropped = len(raw) - len(kept)
        if dropped:
            log.debug("로마숫자 체계 문서: 비로마 시작 헤더 후보 %d개 제외", dropped)
        raw = kept
    return [(i, name, text) for i, name, text, _ in raw]


def find_terminators(blocks: list[Block]) -> list[tuple[int, str, str]]:
    """섹션 '종료'로 인정할 헤더 목록 — 번호 체계를 가리지 않는다.

    시작 헤더보다 기준을 넓게 잡는 이유: 종료 헤더를 놓치면 섹션이 문서 끝까지
    흘러 재무제표 주석이 통째로 섞여 들어간다(피해 큼). 반대로 종료 헤더를
    조금 이르게 잡으면 섹션이 약간 짧아질 뿐이다(피해 작음).
    실제로 '1. 전문가의 확인'(아라비아 번호)이 XI 섹션의 정당한 종료 헤더인
    문서가 다수 있는데, 로마숫자 전용 규칙은 이를 놓쳤다.
    """
    return [(i, name, text) for i, name, text, _ in find_all_header_candidates(blocks)]


# --------------------------------------------------------------------------
# 3) 섹션 추출
# --------------------------------------------------------------------------

def _spans_for(headers: list[tuple[int, str, str]],
               terminators: list[tuple[int, str, str]],
               n_blocks: int, target: str
               ) -> list[tuple[int, int, str, str, str]]:
    """target 표준명 구간 후보: (start, end, 시작헤더, 종료헤더, 종료사유).

    종료는 시작 이후에 나오는 첫 번째 '종료 후보'로 정한다. 시작 헤더 자신과
    같은 블록은 제외한다.
    """
    spans = []
    for idx, name, htext in headers:
        if name != target:
            continue
        nxt = next(((ti, tn, tt) for ti, tn, tt in terminators if ti > idx), None)
        if nxt is None:
            spans.append((idx, n_blocks, htext, "", END_REASON_EOF))
        else:
            end_idx, end_name, end_text = nxt
            spans.append((idx, end_idx, htext, end_text, end_name))
    return spans


CAPTION_MAX_CHARS = 120     # 이보다 길면 캡션이 아니라 본문 문단으로 본다


def _collect(blocks: list[Block], start: int, end: int
             ) -> tuple[list[str], list[int], list[TableRef], str]:
    """구간을 (본문 문단, 문단 블록 순번, 표 목록, 표 텍스트)로 분리한다.

    표를 떼어낼 때 **직전 텍스트 블록이 짧으면 캡션으로 붙여 둔다**.
    표를 분리하면 '표 제목만 남은 문단' 이 본문에 고아로 남는데, 그 연결을
    잃으면 나중에 캡션으로 표를 식별할 수 없다 (소송 표가 그런 경우다).
    """
    texts: list[str] = []
    para_idx: list[int] = []
    tables: list[TableRef] = []
    tables_text: list[str] = []
    prev_text: Block | None = None

    for b in blocks[start + 1 : end]:
        if b.kind == "table":
            cap, cap_idx = "", -1
            if prev_text is not None and len(prev_text.text) <= CAPTION_MAX_CHARS:
                cap, cap_idx = prev_text.text, prev_text.index
            tables.append(TableRef(
                block_index=b.index, order_in_section=len(tables),
                html=b.html, text=b.text, caption=cap, caption_block_index=cap_idx))
            if b.text:
                tables_text.append(b.text)
        elif b.text:
            for p in split_paragraphs(b.text):
                texts.append(p)
                para_idx.append(b.index)
            prev_text = b
    return texts, para_idx, tables, "\n".join(tables_text)


def extract_sections(
    html: str,
    section_spec: dict[str, dict],
    *,
    parser: str = "html.parser",
    require_terminator: bool = True,
    merge_min_chars: int = 10,
    remove_artifacts: bool = True,
) -> dict[str, SectionContent]:
    """config.yaml 의 sections 스펙대로 섹션을 뽑는다.

    section_spec: {"S1": {"name": "사업의 내용", "aliases": [...]}, ...}
    require_terminator: 종료 헤더를 못 찾으면 found=False 로 강등한다.
    merge_min_chars: 이보다 짧은 문단은 앞 문단에 병합한다 (0이면 병합 안 함).
    """
    blocks = iter_blocks(html, parser=parser, remove_artifacts=remove_artifacts)
    headers = find_headers(blocks)
    terminators = find_terminators(blocks)
    n = len(blocks)
    lookup = _canonical_lookup()

    out: dict[str, SectionContent] = {}
    for sid, spec in section_spec.items():
        names = [spec["name"], *spec.get("aliases", [])]
        targets = {match_section_name(nm, lookup) or nm for nm in names}
        spans: list[tuple[int, int, str, str, str]] = []
        for t in targets:
            spans.extend(_spans_for(headers, terminators, n, t))

        if not spans:
            out[sid] = SectionContent(sid, spec["name"], found=False,
                                      end_reason="시작 헤더 없음")
            continue

        # 목차(TOC) 엔트리는 본문 길이가 0에 가깝다 -> 가장 긴 후보를 택한다.
        best = None
        for start, end, htext, end_text, end_reason in spans:
            paras, pidx, tabs, ttext = _collect(blocks, start, end)
            score = sum(len(p) for p in paras) + len(ttext)
            if best is None or score > best[0]:
                best = (score, paras, pidx, tabs, ttext, htext, end_text, end_reason)
        _, paras, pidx, tabs, ttext, htext, end_text, end_reason = best

        if merge_min_chars:
            paras = merge_short_paragraphs(paras, min_chars=merge_min_chars)
        text = "\n".join(paras)

        has_body = bool(text)
        found = has_body
        if require_terminator and end_reason == END_REASON_EOF:
            found = False   # 종료 헤더를 못 찾았다 -> 문서 끝까지 흘렀을 가능성

        out[sid] = SectionContent(
            sid, spec["name"], found=found,
            text=text, paragraphs=paras,
            tables_html=[t.html for t in tabs], tables_text=ttext, tables=tabs,
            paragraph_indices=pidx,
            start_header=htext, end_header=end_text, end_reason=end_reason,
            has_body=has_body,
        )
    return out


def parse_ok(sections: Iterable[SectionContent], min_found: int = 1) -> bool:
    """문서 단위 파싱 성공 판정: 지정한 개수 이상의 섹션에서 본문을 얻었는가."""
    hits = sum(1 for s in sections if s.found and s.char_len_text > 0)
    return hits >= min_found
