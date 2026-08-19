"""텍스트 유사도 3종 + 문단 diff + MinHash 공통문단 탐지 (프롬프트 P0-b).

유사도
  - TF-IDF cosine : 한국어 형태소 분석 없이 문자 2-gram
  - Jaccard       : 어절 단위 집합
  - 정규화 Levenshtein : rapidfuzz (긴 문서에서도 선형에 가깝게 동작)
                        참고로 difflib.SequenceMatcher.ratio 도 함께 기록한다
                        (짧은 문서에 한정. 길면 NaN)

변화율 = 1 - 유사도
"""
from __future__ import annotations

import difflib
import logging
from dataclasses import dataclass

import numpy as np
from rapidfuzz.distance import Levenshtein
from sklearn.feature_extraction.text import TfidfVectorizer

from src.utils.textnorm import char_ngrams, tokenize_eojeol

log = logging.getLogger(__name__)

# difflib 은 O(n^2) 에 가깝다. 이보다 긴 텍스트에서는 계산하지 않고 NaN 을 남긴다.
DIFFLIB_MAX_CHARS = 20_000


def tfidf_cosine_matrix(texts: list[str], ngram: tuple[int, int] = (2, 2)) -> np.ndarray:
    """문서 리스트에 대해 문자 n-gram TF-IDF 코사인 유사도 행렬.

    idf 를 섹션 코퍼스 전체에서 추정하기 위해 쌍별이 아니라 한 번에 fit 한다.
    """
    if len(texts) < 2:
        return np.eye(max(len(texts), 1))
    vec = TfidfVectorizer(analyzer="char", ngram_range=ngram, min_df=1, sublinear_tf=True)
    X = vec.fit_transform(texts)          # TfidfVectorizer 는 L2 정규화된 행을 준다
    return (X @ X.T).toarray()


def jaccard(a: str, b: str) -> float:
    sa, sb = set(tokenize_eojeol(a)), set(tokenize_eojeol(b))
    if not sa and not sb:
        return 1.0
    union = sa | sb
    return len(sa & sb) / len(union) if union else 0.0


def levenshtein_sim(a: str, b: str) -> float:
    if not a and not b:
        return 1.0
    return float(Levenshtein.normalized_similarity(a, b))


def difflib_ratio(a: str, b: str, max_chars: int = DIFFLIB_MAX_CHARS) -> float:
    """difflib.SequenceMatcher ratio. 너무 길면 계산을 포기하고 NaN."""
    if max(len(a), len(b)) > max_chars:
        return float("nan")
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


# ---------------------------------------------------------------------------
# 문단 단위 diff
# ---------------------------------------------------------------------------

@dataclass
class ParagraphDiff:
    added: list[str]      # 새로 생긴 문단
    modified: list[str]   # 대체된 문단(신규 쪽)
    removed: list[str]
    n_same: int

    @property
    def changed(self) -> list[str]:
        return self.added + self.modified


def diff_paragraphs(prev: list[str], curr: list[str]) -> ParagraphDiff:
    """문단 리스트를 정렬해 added/modified/removed 로 분류한다."""
    sm = difflib.SequenceMatcher(None, prev, curr, autojunk=False)
    added: list[str] = []
    modified: list[str] = []
    removed: list[str] = []
    n_same = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            n_same += i2 - i1
        elif tag == "insert":
            added.extend(curr[j1:j2])
        elif tag == "delete":
            removed.extend(prev[i1:i2])
        elif tag == "replace":
            modified.extend(curr[j1:j2])
            removed.extend(prev[i1:i2])
    return ParagraphDiff(added, modified, removed, n_same)


# ---------------------------------------------------------------------------
# MinHash 로 기업 간 공통 변경 문단 찾기 (P0-b 작업 4)
# ---------------------------------------------------------------------------

def minhash_pairs(
    items: list[tuple[str, str]],
    *,
    num_perm: int = 128,
    ngram: int = 3,
    threshold: float = 0.8,
    cross_group_only: bool = True,
) -> list[tuple[str, str, float]]:
    """items = [(group_id, paragraph_text), ...].

    문자 n-gram MinHash 로 유사도 threshold 이상인 쌍을 찾는다.
    cross_group_only=True 면 서로 다른 기업(group) 간의 쌍만 남긴다.
    Returns: [(idx_i, idx_j, est_jaccard)] — idx 는 items 인덱스 문자열.
    """
    from datasketch import MinHash, MinHashLSH

    if len(items) < 2:
        return []

    sigs: list[MinHash] = []
    for _, text in items:
        m = MinHash(num_perm=num_perm)
        for g in char_ngrams(text, ngram):
            m.update(g.encode("utf-8"))
        sigs.append(m)

    lsh = MinHashLSH(threshold=threshold, num_perm=num_perm)
    for i, m in enumerate(sigs):
        lsh.insert(str(i), m)

    seen: set[tuple[int, int]] = set()
    out: list[tuple[str, str, float]] = []
    for i, m in enumerate(sigs):
        for key in lsh.query(m):
            j = int(key)
            if j == i:
                continue
            pair = (min(i, j), max(i, j))
            if pair in seen:
                continue
            seen.add(pair)
            if cross_group_only and items[pair[0]][0] == items[pair[1]][0]:
                continue
            est = sigs[pair[0]].jaccard(sigs[pair[1]])
            if est >= threshold:
                out.append((str(pair[0]), str(pair[1]), float(est)))

    out.sort(key=lambda t: t[2], reverse=True)
    return out
