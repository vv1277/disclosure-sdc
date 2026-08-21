"""Phase 2 병렬 파싱 — 파생 정제본이 재파싱과 같은지, 워커 수가 안전한지."""
import pandas as pd
import pytest

from src.parse.run_parse import _derive_clean, _is_layout_table, default_workers
from src.parse.sections import extract_sections

SPEC = {"S1": {"name": "사업의 내용", "aliases": []}}

DOC = """<html><body>
  <p>II. 사업의 내용</p>
  <p>가. 생산능력 및 산출근거&cr (1) 생산능력</p>
  <p>◆click◆『수주상황』 삽입</p>
  <p>11011#*_수주상황.dsl 당사는 반도체를 생산합니다.</p>
  <p>짧음</p>
  <p>[표1] 매출</p><table><tr><td>구분</td><td>매출&cr</td></tr></table>
  <p>III. 재무에 관한 사항</p><p>재무</p>
</body></html>"""


def test_derived_clean_equals_reparsing_with_removal():
    """한 번만 파싱하고 파생시킨 정제본이, 제거를 켜고 다시 파싱한 것과 같아야 한다.

    두 번 파싱하면 메모리와 시간이 두 배가 되므로 파생시키는데,
    그 동치가 깨지면 조용히 다른 코퍼스가 만들어진다.
    """
    raw = extract_sections(DOC, SPEC, merge_min_chars=0, remove_artifacts=False)
    derived = _derive_clean(raw, merge_min_chars=10)["S1"]
    reparsed = extract_sections(DOC, SPEC, merge_min_chars=10)["S1"]
    assert derived.paragraphs == reparsed.paragraphs
    assert derived.text == reparsed.text
    assert derived.tables_text == reparsed.tables_text


def test_raw_text_actually_keeps_artifacts():
    """raw_text 는 잔재를 '보존' 해야 한다. 둘 다 제거하면 비교표를 못 만든다."""
    raw = extract_sections(DOC, SPEC, merge_min_chars=0, remove_artifacts=False)["S1"]
    assert "&cr" in raw.text
    assert "◆click◆" in raw.text
    assert ".dsl" in raw.text


def test_clean_version_has_no_artifacts():
    raw = extract_sections(DOC, SPEC, merge_min_chars=0, remove_artifacts=False)
    clean = _derive_clean(raw, merge_min_chars=10)["S1"]
    for token in ("&cr", "◆", ".dsl"):
        assert token not in clean.text


def test_artifact_only_paragraph_is_dropped():
    raw = extract_sections(DOC, SPEC, merge_min_chars=0, remove_artifacts=False)
    clean = _derive_clean(raw, merge_min_chars=0)["S1"]
    assert not any(p.startswith("◆") for p in clean.paragraphs)


def test_paragraph_indices_stay_aligned():
    raw = extract_sections(DOC, SPEC, merge_min_chars=0, remove_artifacts=False)
    clean = _derive_clean(raw, merge_min_chars=0)["S1"]
    assert len(clean.paragraphs) == len(clean.paragraph_indices)


def test_layout_table_detection():
    """1~2행 또는 1열이면서 셀 200자 이상이면 레이아웃 표."""
    long_cell = "가" * 250
    assert _is_layout_table(f"<table><tr><td>{long_cell}</td></tr></table>")
    assert not _is_layout_table(
        "<table><tr><th>구분</th><th>값</th></tr>"
        "<tr><td>a</td><td>1</td></tr><tr><td>b</td><td>2</td></tr>"
        "<tr><td>c</td><td>3</td></tr></table>")


def test_worker_count_is_memory_bounded_not_core_bounded():
    """코어 수만 보고 정하면 MemoryError 가 난다 (실제로 17코어에서 났다)."""
    import os
    w = default_workers()
    assert w >= 1
    assert w <= max(1, (os.cpu_count() or 2) - 1)
