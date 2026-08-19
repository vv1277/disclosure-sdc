import math

from src.pilot.similarity import (
    diff_paragraphs,
    difflib_ratio,
    jaccard,
    levenshtein_sim,
    minhash_pairs,
    tfidf_cosine_matrix,
)


def test_identical_texts_have_similarity_one():
    a = "당사는 반도체 부문을 중심으로 사업을 영위하고 있습니다."
    assert jaccard(a, a) == 1.0
    assert levenshtein_sim(a, a) == 1.0
    assert math.isclose(difflib_ratio(a, a), 1.0)
    m = tfidf_cosine_matrix([a, a])
    assert math.isclose(m[0, 1], 1.0, rel_tol=1e-6)


def test_change_rate_ordering():
    base = "당사는 반도체 부문을 중심으로 사업을 영위하고 있으며 매출액은 1000억원입니다."
    near = "당사는 반도체 부문을 중심으로 사업을 영위하고 있으며 매출액은 1200억원입니다."
    far = "회사는 소송 3건이 계류중이며 감독기관 제재는 없습니다."
    m = tfidf_cosine_matrix([base, near, far])
    assert m[0, 1] > m[0, 2]
    assert jaccard(base, near) > jaccard(base, far)
    assert levenshtein_sim(base, near) > levenshtein_sim(base, far)


def test_difflib_gives_nan_for_very_long_text():
    long = "가" * 30_000
    assert math.isnan(difflib_ratio(long, long, max_chars=20_000))


def test_diff_paragraphs_classifies_added_and_modified():
    prev = ["문단 A", "문단 B", "문단 C"]
    curr = ["문단 A", "문단 B 수정본", "문단 C", "문단 D"]
    d = diff_paragraphs(prev, curr)
    assert "문단 D" in d.added
    assert "문단 B 수정본" in d.modified
    assert "문단 B" in d.removed
    assert d.n_same == 2
    assert set(d.changed) == {"문단 D", "문단 B 수정본"}


def test_minhash_finds_cross_firm_boilerplate():
    boiler = ("본 보고서는 기업공시서식 작성기준 개정에 따라 해당 서식에 맞추어 "
              "작성되었습니다. 항목별 기재 순서를 조정하였습니다.")
    items = [
        ("A", boiler),
        ("B", boiler),
        ("C", boiler + " 일부 문구만 다릅니다."),
        ("A", "당사 고유의 사업 내용으로 다른 기업과 전혀 겹치지 않는 문단입니다."),
    ]
    pairs = minhash_pairs(items, threshold=0.8, cross_group_only=True)
    assert pairs, "기업 간 동일 문단을 찾지 못했다"
    groups = {(items[int(i)][0], items[int(j)][0]) for i, j, _ in pairs}
    assert ("A", "B") in groups or ("B", "A") in groups


def test_minhash_skips_same_firm_pairs():
    same = "완전히 동일한 문단이 같은 기업 안에서 두 번 나옵니다. 이것은 세면 안 됩니다."
    pairs = minhash_pairs([("A", same), ("A", same)], threshold=0.8, cross_group_only=True)
    assert pairs == []
