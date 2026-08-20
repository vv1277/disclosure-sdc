"""텍스트 정규화 — 섹션명 매칭용(강한 정규화)과 본문 저장용(약한 정규화)을 분리한다."""
from __future__ import annotations

import re
import unicodedata

# 공시 원문에 흔한 비표시/특수 공백
_INVISIBLE = dict.fromkeys(
    map(ord, "\u200b\u200c\u200d\ufeff\u00ad\u2060"), None
)
_SPACE_LIKE = re.compile(r"[\u00a0\u1680\u2000-\u200a\u202f\u205f\u3000\t]+")
_MULTI_SPACE = re.compile(r"[ ]{2,}")
_MULTI_NEWLINE = re.compile(r"\n{3,}")

# 각주 마커: 주1), 주1, *1, ※, (주1) 등
_FOOTNOTE = re.compile(r"(?:\(\s*주\s*\d*\s*\)|주\s*\d+\s*\)|※|\*\s*\d+\s*\))")

# DART 편집기 플레이스홀더. 공시 내용이 아니라 작성 도구의 위젯 라벨이 본문에
# 그대로 실려 나온 것이다. 예: "◆click◆『수주상황』 삽입"
# 2016/2020 서식의 전 기업 문서 절반에 들어 있어, 제거하지 않으면 '기업 간 공통
# 변경 문단' 신호를 통째로 오염시킨다. (P0-d 에서 발견)
_EDITOR_PLACEHOLDER = re.compile(
    r"◆\s*click\s*◆\s*(?:『[^』]{0,60}』)?\s*(?:삽입|추가)?",
    re.IGNORECASE,
)

# 같은 계열의 DART 편집기 잔재.
#   &cr  : 줄바꿈 엔티티가 이스케이프되지 않고 본문에 그대로 남은 것 (356건 중 236건)
#   11011#*_수주상황.dsl : 서식 템플릿 파일명이 본문에 실려 나온 것 (59건)
_DART_ENTITY = re.compile(r"&cr(?![A-Za-z0-9]);?", re.IGNORECASE)
_DSL_REF = re.compile(r"\d{4,6}\s*#\s*\*?\s*_\S+?\.dsl", re.IGNORECASE)

# 섹션명 매칭 시 무시할 문자: 공백, 중점류, 괄호, 구두점
_NAME_STRIP = re.compile(r"[\s·ㆍ・･\.\,\:\;\-\—\–\_\(\)\[\]\{\}<>「」『』\"'’‘“”/\|]+")

# 앞머리 번호: 로마숫자/아라비아/한글 자모 + 구분자
_LEADING_NUM = re.compile(
    r"^\s*[\(\[<]?\s*"
    r"(?:[IVXLivxl]{1,7}|[0-9]{1,2}|[가나다라마바사아자차카타파하]|[①-⑳])"
    r"\s*[\)\]>\.\-–—:]*\s*"
)


def clean_text(s: str) -> str:
    """본문 저장용 정규화. 내용은 보존하고 표기 노이즈만 제거한다."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = s.translate(_INVISIBLE)
    s = _SPACE_LIKE.sub(" ", s)
    s = _EDITOR_PLACEHOLDER.sub(" ", s)
    s = _DSL_REF.sub(" ", s)
    s = _DART_ENTITY.sub(" ", s)
    s = _FOOTNOTE.sub(" ", s)
    s = "\n".join(line.strip() for line in s.split("\n"))
    s = _MULTI_SPACE.sub(" ", s)
    s = _MULTI_NEWLINE.sub("\n\n", s)
    return s.strip()


def norm_name(s: str) -> str:
    """섹션명 매칭용 강한 정규화.

    '11. 그 밖에 투자자 보호를 위하여 필요한 사항' 과
    'XI.그밖에  투자자보호를 위하여 필요한 사항' 이 같은 문자열이 되게 한다.
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = s.translate(_INVISIBLE)
    s = _SPACE_LIKE.sub(" ", s).strip()
    s = strip_leading_number(s)
    s = _NAME_STRIP.sub("", s)
    return s.lower()


def strip_leading_number(s: str) -> str:
    """앞머리 번호를 제거한다. 번호는 연도마다 바뀌므로 매칭에 쓰지 않는다."""
    prev = None
    out = s
    # 'XI. 11. ' 같은 이중 번호까지 최대 2회 제거
    for _ in range(2):
        prev = out
        out = _LEADING_NUM.sub("", out, count=1)
        if out == prev:
            break
    return out.strip()


def split_paragraphs(text: str, min_chars: int = 1) -> list[str]:
    """문단 리스트. 빈 줄 또는 줄바꿈 기준으로 쪼갠다."""
    if not text:
        return []
    parts = [p.strip() for p in re.split(r"\n+", text)]
    return [p for p in parts if len(p) >= min_chars]


def tokenize_eojeol(text: str) -> list[str]:
    """어절 단위 토큰 (Jaccard용). 형태소 분석기를 쓰지 않는다."""
    return [t for t in re.split(r"\s+", text) if t]


def char_ngrams(text: str, n: int = 3) -> set[str]:
    """문자 n-gram 집합 (MinHash용). 공백은 제거하고 만든다."""
    s = re.sub(r"\s+", "", text)
    if len(s) < n:
        return {s} if s else set()
    return {s[i : i + n] for i in range(len(s) - n + 1)}
