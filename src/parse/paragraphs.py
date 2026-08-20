"""문단 정규화 — 짧은 조각 병합과 문단 길이 통계 (P0-c Part 3).

배경: DART 원문에서 한 문단이 여러 줄로 쪼개지는 경우가 흔하다.
      (기준일 : / 2024년 12월 31일 / ) 처럼 괄호와 값이 분리되거나,
      표에서 새어 나온 셀이 한 글자짜리 줄로 남는다.
      이런 조각이 남으면 문단 단위 diff(Phase 4)가 전부 오작동한다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# 이 문자로만 이루어진 줄은 앞 문단에 붙인다 (내용이 없는 조각)
_PUNCT_ONLY = re.compile(r"^[\s\(\)\[\]\{\}<>·ㆍ・,\.:;\-–—_/\\|~!@#$%^&*+='\"’‘“”]*$")


def _is_punct_only(s: str) -> bool:
    return bool(_PUNCT_ONLY.fullmatch(s))


def merge_short_paragraphs(
    paragraphs: list[str],
    *,
    min_chars: int = 10,
    max_merged_chars: int = 2000,
) -> list[str]:
    """min_chars 미만인 문단을 앞 문단에 병합한다.

    규칙
      - 길이가 min_chars 미만이면 직전 문단 뒤에 공백 하나로 이어 붙인다.
      - 앞 문단이 없으면(맨 앞 조각) 뒤 문단 앞에 붙인다.
      - 병합 결과가 max_merged_chars 를 넘으면 더 붙이지 않고 새 문단을 연다.
        (표가 통째로 새어 들어온 경우 한 문단이 무한정 커지는 것을 막는다)
      - 구두점/괄호만 있는 줄은 길이와 무관하게 병합한다.
    """
    out: list[str] = []
    pending_head: list[str] = []   # 첫 문단 앞에 쌓인 조각

    for raw in paragraphs:
        p = raw.strip()
        if not p:
            continue
        short = len(p) < min_chars or _is_punct_only(p)
        if not short:
            if pending_head:
                p = " ".join(pending_head + [p])
                pending_head.clear()
            out.append(p)
            continue

        # 짧은 조각
        if out and len(out[-1]) + 1 + len(p) <= max_merged_chars:
            out[-1] = out[-1] + " " + p
        elif out:
            out.append(p)          # 앞 문단이 이미 너무 큼 -> 새 문단으로 둔다
        else:
            pending_head.append(p)  # 아직 앞 문단이 없다

    if pending_head:
        joined = " ".join(pending_head)
        if out:
            out[0] = joined + " " + out[0]
        else:
            out.append(joined)
    return out


@dataclass(frozen=True)
class ParagraphStats:
    n: int
    mean: float
    median: float
    p10: float
    p90: float
    n_under_10: int

    @property
    def share_under_10(self) -> float:
        return self.n_under_10 / self.n if self.n else 0.0


def paragraph_stats(paragraphs: list[str]) -> ParagraphStats:
    """문단당 문자 수 분포 (P0-c Part 3-6)."""
    lens = sorted(len(p) for p in paragraphs)
    n = len(lens)
    if n == 0:
        return ParagraphStats(0, 0.0, 0.0, 0.0, 0.0, 0)

    def pct(q: float) -> float:
        if n == 1:
            return float(lens[0])
        pos = q * (n - 1)
        lo = int(pos)
        hi = min(lo + 1, n - 1)
        frac = pos - lo
        return float(lens[lo] * (1 - frac) + lens[hi] * frac)

    return ParagraphStats(
        n=n,
        mean=sum(lens) / n,
        median=pct(0.5),
        p10=pct(0.10),
        p90=pct(0.90),
        n_under_10=sum(1 for x in lens if x < 10),
    )
