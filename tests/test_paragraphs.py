"""P0-c Part 3-8 — 10자 미만 문단 병합 규칙이 실제로 적용되는지 검증."""
import pytest

from src.parse.paragraphs import merge_short_paragraphs, paragraph_stats
from src.parse.sections import extract_sections


def test_short_paragraph_merged_into_previous():
    got = merge_short_paragraphs(
        ["당사는 반도체 부문을 영위하고 있습니다.", "성명", "성별", "직위"],
        min_chars=10,
    )
    assert len(got) == 1, got
    assert got[0] == "당사는 반도체 부문을 영위하고 있습니다. 성명 성별 직위"


def test_paragraph_at_or_above_min_chars_stays_separate():
    """10자 이상이면 병합하지 않는다 (경계값 확인)."""
    got = merge_short_paragraphs(
        ["당사는 반도체 부문을 영위하고 있습니다.", "(단위 : 백만원)"],
        min_chars=10,
    )
    assert len(got) == 2


def test_leading_short_fragments_attach_to_first_real_paragraph():
    """앞 문단이 없으면 뒤 문단 앞에 붙인다."""
    got = merge_short_paragraphs(
        ["(기준일 :", "2024년", ")", "보고기간 종료일 현재 직원 수는 100명입니다."],
        min_chars=10,
    )
    assert len(got) == 1
    assert got[0].startswith("(기준일 : 2024년 )")
    assert "직원 수는 100명" in got[0]


def test_long_paragraphs_are_not_touched():
    src = ["가" * 40, "나" * 50, "다" * 60]
    assert merge_short_paragraphs(src, min_chars=10) == src


def test_punctuation_only_line_merged_regardless_of_length():
    got = merge_short_paragraphs(
        ["본문 문단입니다. 충분히 깁니다.", "( ) [ ] ㆍ · , . : ;"],
        min_chars=3,   # 길이 기준으로는 병합되지 않을 길이
    )
    assert len(got) == 1


def test_merge_stops_at_max_merged_chars():
    """표가 통째로 새어 들어와도 한 문단이 무한정 커지지 않는다."""
    got = merge_short_paragraphs(
        ["가" * 95] + ["짧"] * 20, min_chars=10, max_merged_chars=100
    )
    assert len(got) > 1
    assert all(len(p) <= 100 for p in got[:-1])


def test_empty_input():
    assert merge_short_paragraphs([], min_chars=10) == []
    assert merge_short_paragraphs(["", "   "], min_chars=10) == []


def test_merge_rule_is_actually_applied_by_extract_sections():
    """단위 함수만이 아니라 실제 추출 경로에서 병합이 적용되는지 확인한다."""
    html = """<html><body>
      <p>II. 사업의 내용</p>
      <p>당사는 반도체 부문을 중심으로 사업을 영위하고 있습니다.</p>
      <p>(기준일 :</p><p>2024년</p><p>)</p>
      <p>III. 재무에 관한 사항</p><p>재무 내용</p>
      <p>IV. 이사의 경영진단 및 분석의견</p><p>경영진단 내용입니다. 충분히 깁니다.</p>
    </body></html>"""
    spec = {"S1": {"name": "사업의 내용", "aliases": []}}

    merged = extract_sections(html, spec, merge_min_chars=10)["S1"]
    unmerged = extract_sections(html, spec, merge_min_chars=0)["S1"]

    assert unmerged.n_paragraphs == 4      # 본문 1 + 조각 3
    assert merged.n_paragraphs == 1        # 조각이 앞 문단에 붙는다
    assert "(기준일 : 2024년 )" in merged.text


@pytest.mark.parametrize("lens,expect_median", [([10, 20, 30], 20), ([5], 5)])
def test_paragraph_stats_percentiles(lens, expect_median):
    st = paragraph_stats(["가" * n for n in lens])
    assert st.n == len(lens)
    assert st.median == expect_median
    assert st.p10 <= st.median <= st.p90


def test_paragraph_stats_counts_short_paragraphs():
    st = paragraph_stats(["가" * 5, "나" * 9, "다" * 40])
    assert st.n_under_10 == 2
    assert st.share_under_10 == pytest.approx(2 / 3)


def test_paragraph_stats_empty():
    st = paragraph_stats([])
    assert st.n == 0 and st.mean == 0 and st.share_under_10 == 0
