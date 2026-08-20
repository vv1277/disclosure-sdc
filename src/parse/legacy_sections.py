"""P0-c 이전 섹션 추출기의 동결 사본 — A/B 비교 전용.

이 모듈은 **의도적으로 버그를 그대로 보존**한다. 수정 전/후 수치를 같은
코드베이스에서 재현 가능하게 비교하기 위한 참조 구현이며, 새 코드는 절대
여기에 의존하면 안 된다. Gate 0 판정이 끝나면 삭제한다.

보존된 결함 3가지
  1) BLOCK_TAGS 화이트리스트 — 목록에 없는 컨테이너 태그(<TABLE-GROUP>,
     <SECTION-3>, <COVER>, <LIBRARY>)는 인라인으로 취급되어, 그 안의
     <table> 이 통째로 본문 문단으로 새어 들어온다.
  2) 퍼지 임계값 92 + 번호 체계 무시 — '1. 회사의 개요',
     '2. 재무 등에 관한 사항' 같은 하위 소제목이 최상위 헤더로 오인된다.
  3) 종료 헤더가 없어도 성공으로 집계 — 섹션이 문서 끝까지 흐른다.
"""
from __future__ import annotations

from bs4 import BeautifulSoup, NavigableString, Tag
from rapidfuzz import fuzz

from src.parse.sections import Block, SectionContent, END_REASON_EOF
from src.utils.textnorm import clean_text, norm_name, split_paragraphs

# 결함 1: 화이트리스트. DART 커스텀 태그가 빠져 있다.
LEGACY_BLOCK_TAGS = {
    "p", "div", "li", "ul", "ol", "dl", "dd", "dt",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "td", "th", "tr", "tbody", "thead", "caption",
    "section", "article", "center", "blockquote", "pre", "body",
    "section-1", "section-2", "title", "document", "library",
}
LEGACY_SKIP_TAGS = {"script", "style", "head", "meta", "link"}

LEGACY_TOP_LEVEL_NAMES = (
    "회사의 개요", "사업의 내용", "재무에 관한 사항",
    "이사의 경영진단 및 분석의견", "감사인의 감사의견 등",
    "회계감사인의 감사의견 등", "이사회 등 회사의 기관에 관한 사항",
    "주주에 관한 사항", "임원 및 직원 등에 관한 사항",
    "임원 및 직원에 관한 사항", "계열회사 등에 관한 사항",
    "이해관계자와의 거래내용", "대주주 등과의 거래내용",
    "그 밖에 투자자 보호를 위하여 필요한 사항",
    "전문가의 확인", "재무제표 등", "부속명세서", "상세표",
)

_LEGACY_FUZZ = 92          # 결함 2
_MAX_HEADER_CHARS = 60


def _legacy_walk(node: Tag, out: list[Block]) -> None:
    pending: list[str] = []

    def flush() -> None:
        if not pending:
            return
        t = clean_text(" ".join(pending))
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
        if name in LEGACY_SKIP_TAGS:
            continue
        if name == "table":
            flush()
            out.append(Block("table", clean_text(child.get_text(" ")), html=str(child)))
            continue
        if name == "br":
            flush()
            continue
        if name in LEGACY_BLOCK_TAGS:
            flush()
            _legacy_walk(child, out)
            continue
        pending.append(child.get_text(" "))   # <- 결함 1이 발생하는 지점
    flush()


def legacy_iter_blocks(html: str, parser: str = "html.parser") -> list[Block]:
    soup = BeautifulSoup(html, parser)
    for tag in soup.find_all(list(LEGACY_SKIP_TAGS)):
        tag.decompose()
    out: list[Block] = []
    _legacy_walk(soup.body or soup, out)
    return out


def _legacy_lookup() -> dict[str, str]:
    return {norm_name(n): n for n in LEGACY_TOP_LEVEL_NAMES}


def _legacy_match(text: str, cands: dict[str, str]) -> str | None:
    if not text or len(text) > _MAX_HEADER_CHARS:
        return None
    key = norm_name(text)
    if not key or len(key) < 3:
        return None
    if key in cands:
        return cands[key]
    best, score = None, 0.0
    for k, v in cands.items():
        s = fuzz.ratio(key, k)
        if s > score:
            best, score = v, s
    return best if score >= _LEGACY_FUZZ else None


def legacy_find_headers(blocks: list[Block]) -> list[tuple[int, str, str]]:
    cands = _legacy_lookup()
    out = []
    for i, b in enumerate(blocks):
        if b.kind != "text":
            continue
        name = _legacy_match(b.text, cands)
        if name:
            out.append((i, name, b.text))
    return out


def legacy_extract_sections(
    html: str, section_spec: dict[str, dict], *, parser: str = "html.parser"
) -> dict[str, SectionContent]:
    """수정 전 동작 그대로. 종료 헤더가 없어도 found=True 가 된다."""
    blocks = legacy_iter_blocks(html, parser=parser)
    headers = legacy_find_headers(blocks)
    n = len(blocks)
    cands = _legacy_lookup()

    out: dict[str, SectionContent] = {}
    for sid, spec in section_spec.items():
        names = [spec["name"], *spec.get("aliases", [])]
        targets = {_legacy_match(nm, cands) or nm for nm in names}

        spans = []
        for pos, (idx, name, htext) in enumerate(headers):
            if name not in targets:
                continue
            if pos + 1 < len(headers):
                spans.append((idx, headers[pos + 1][0], htext,
                              headers[pos + 1][2], headers[pos + 1][1]))
            else:
                spans.append((idx, n, htext, "", END_REASON_EOF))

        if not spans:
            out[sid] = SectionContent(sid, spec["name"], found=False,
                                      end_reason="시작 헤더 없음")
            continue

        best = None
        for start, end, htext, etext, ereason in spans:
            texts, thtml, ttext = [], [], []
            for b in blocks[start + 1 : end]:
                if b.kind == "table":
                    thtml.append(b.html)
                    if b.text:
                        ttext.append(b.text)
                elif b.text:
                    texts.append(b.text)
            body = "\n".join(texts)
            score = len(body) + sum(len(t) for t in ttext)
            if best is None or score > best[0]:
                best = (score, body, thtml, "\n".join(ttext), htext, etext, ereason)

        _, body, thtml, ttext, htext, etext, ereason = best
        out[sid] = SectionContent(
            sid, spec["name"],
            found=bool(body),               # <- 결함 3: 종료 헤더를 요구하지 않는다
            text=body, paragraphs=split_paragraphs(body),
            tables_html=thtml, tables_text=ttext,
            start_header=htext, end_header=etext, end_reason=ereason,
            has_body=bool(body),
        )
    return out
