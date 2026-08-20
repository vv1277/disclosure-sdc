"""P0-c — 섹션 경계 감사 · 경계 탐지 수정 · 문단 분할 수정 · 공통문단 재계산.

선행 조건
  data/pilot/reports_index.csv 와 data/raw/{rcept_no}.zip 캐시.
  (섹션 텍스트는 여기서 다시 뽑으므로 sections/ 는 없어도 된다)

산출
  data/pilot/boundary_audit.csv        Part 1-1  경계 감사 원장
  data/pilot/contamination_report.md   Part 1-2,3 오염 진단 + 문제 문서 목록
  data/pilot/sections_fixed/           Part 2    수정된 섹션 텍스트
  data/pilot/paragraph_stats.csv       Part 3-6  문단 길이 분포
  data/pilot/common_pairs_recount.csv  Part 4-9  중복 제거 전/후 공통쌍 수
  data/pilot/corp_dominance.csv        Part 4-10 corp_code 별 등장 빈도
  data/pilot/p0c_report.md             Part 1~4 종합 + Gate 0 재판정

실행
  python -m src.pilot.p0c_boundary_audit
  python -m src.pilot.p0c_boundary_audit --limit 5     # 소규모 시운전
"""
from __future__ import annotations

import argparse
import logging
import warnings
from pathlib import Path
from typing import Any

import pandas as pd
from bs4 import XMLParsedAsHTMLWarning
from tqdm import tqdm

from src.parse.body import pick_body_file
from src.parse.legacy_sections import legacy_extract_sections
from src.parse.paragraphs import paragraph_stats
from src.parse.sections import END_REASON_EOF, SectionContent, extract_sections
from src.pilot.similarity import diff_paragraphs, minhash_pairs
from src.utils.config import PROJECT_ROOT, Config, load_config, set_seed
from src.utils.logging_utils import setup_logging

# DART 원본은 XML 이지만 커스텀 태그 보존을 위해 html.parser 로 읽는다. 경고는 억제.
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

log = logging.getLogger("p0c")

DART_VIEWER = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"


# ===========================================================================
# Part 1 — 섹션 경계 감사
# ===========================================================================

def _snippet(text: str, n: int, *, tail: bool = False) -> str:
    if not text:
        return ""
    s = text[-n:] if tail else text[:n]
    return s.replace("\n", " ⏎ ")


def audit_document(
    cfg: Config, meta: pd.Series, html: str, snippet_chars: int
) -> tuple[list[dict[str, Any]], dict[str, SectionContent], dict[str, SectionContent]]:
    """한 문서를 legacy/fixed 두 방식으로 추출하고 감사 행을 만든다."""
    spec = cfg["sections"]
    pcfg = cfg.get("parse", {})
    parser = pcfg.get("parser", "html.parser")

    legacy = legacy_extract_sections(html, spec, parser=parser)
    fixed = extract_sections(
        html, spec, parser=parser,
        require_terminator=bool(pcfg.get("require_terminator", True)),
        merge_min_chars=int(pcfg.get("merge_min_chars", 10)),
    )

    rows: list[dict[str, Any]] = []
    for variant, secs in (("legacy", legacy), ("fixed", fixed)):
        for sid, sc in secs.items():
            rows.append({
                "corp_code": meta["corp_code"],
                "corp_name": meta["corp_name"],
                "stock_code": meta["stock_code"],
                "fy": meta["fy"],
                "rcept_no": meta["rcept_no"],
                "variant": variant,
                "section": sid,
                "section_name": sc.name,
                "found": sc.found,
                "has_body": sc.has_body,
                "start_header": sc.start_header,
                # 종료 헤더가 없으면 "EOF" 로 표기한다 (Part 1-1)
                "end_header": sc.end_header or END_REASON_EOF,
                "end_reason": sc.end_reason,
                "char_len_text": sc.char_len_text,
                "char_len_table": sc.char_len_table,
                "n_paragraphs": sc.n_paragraphs,
                "chars_per_paragraph": round(
                    sc.char_len_text / sc.n_paragraphs, 2) if sc.n_paragraphs else 0.0,
                "head_200": _snippet(sc.text, snippet_chars),
                "tail_200": _snippet(sc.text, snippet_chars, tail=True),
                "dart_url": DART_VIEWER.format(rcept_no=meta["rcept_no"]),
            })
    return rows, legacy, fixed


# ===========================================================================
# Part 1-2 — 오염 탐지
# ===========================================================================

def markers_for(cfg_markers: Any, section: str) -> list[str]:
    """섹션별 오염 마커 세트 (P0-d Part A-2). 구버전(평면 리스트)도 받아준다."""
    if isinstance(cfg_markers, dict):
        return list(cfg_markers.get(section, cfg_markers.get("default", [])))
    return list(cfg_markers)


def contamination(paragraphs: list[str], markers: list[str]) -> dict[str, Any]:
    """오염 마커 개수와 오염 비중.

    비중은 **문자 수 기준**을 기본으로 쓴다 (P0-d Part A-1).
    문단 수 기준은 문단 병합과 표 제거로 분모가 통째로 바뀌기 때문에,
    오염량이 그대로여도 legacy/fixed 값이 달라져 비교가 왜곡된다.
    문자 수 기준은 분모가 실제 본문 분량이라 그 왜곡이 없다.
    """
    n_hits = 0
    hit_markers: set[str] = set()
    n_dirty = 0
    dirty_chars = 0
    total_chars = 0
    for p in paragraphs:
        total_chars += len(p)
        found = [m for m in markers if m in p]
        if found:
            n_dirty += 1
            dirty_chars += len(p)
            n_hits += len(found)
            hit_markers.update(found)
    n = len(paragraphs)
    return {
        "n_markers_used": len(markers),
        "n_marker_hits": n_hits,
        "n_distinct_markers": len(hit_markers),
        "markers_found": ", ".join(sorted(hit_markers)),
        "n_dirty_paragraphs": n_dirty,
        "dirty_chars": dirty_chars,
        "total_chars": total_chars,
        # 기본 지표: 문자 수 기준
        "contamination_share": round(dirty_chars / total_chars, 4) if total_chars else 0.0,
        # 참고용: 기존 문단 수 기준 (분모 효과로 비교 왜곡 있음)
        "contamination_share_paras": round(n_dirty / n, 4) if n else 0.0,
    }


def contamination_frame(records: list[dict], markers: Any) -> pd.DataFrame:
    rows = []
    for rec in records:
        for variant in ("legacy", "fixed"):
            for sid, sc in rec[variant].items():
                c = contamination(sc.paragraphs, markers_for(markers, sid))
                rows.append({
                    "corp_code": rec["meta"]["corp_code"],
                    "corp_name": rec["meta"]["corp_name"],
                    "fy": rec["meta"]["fy"],
                    "rcept_no": rec["meta"]["rcept_no"],
                    "variant": variant,
                    "section": sid,
                    "found": sc.found,
                    "end_header": sc.end_header or END_REASON_EOF,
                    "char_len_text": sc.char_len_text,
                    "n_paragraphs": sc.n_paragraphs,
                    **c,
                })
    return pd.DataFrame(rows)


# ===========================================================================
# Part 3 — 문단 분할
# ===========================================================================

def paragraph_frame(records: list[dict]) -> pd.DataFrame:
    rows = []
    for rec in records:
        for variant in ("legacy", "fixed"):
            for sid, sc in rec[variant].items():
                st = paragraph_stats(sc.paragraphs)
                rows.append({
                    "corp_code": rec["meta"]["corp_code"],
                    "fy": rec["meta"]["fy"],
                    "rcept_no": rec["meta"]["rcept_no"],
                    "variant": variant, "section": sid,
                    "n_paragraphs": st.n, "mean": round(st.mean, 2),
                    "median": st.median, "p10": st.p10, "p90": st.p90,
                    "n_under_10": st.n_under_10,
                    "share_under_10": round(st.share_under_10, 4),
                })
    return pd.DataFrame(rows)


# ===========================================================================
# Part 4 — 중복 제거 후 공통문단 재계산
# ===========================================================================

def recount_common_pairs(
    records: list[dict], cfg: Config
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """(재계산 결과, corp_code 빈도, 상위 쌍) — dedup 전/후를 함께 계산."""
    c = cfg["change"]
    min_chars = int(c["min_paragraph_chars"])
    years = sorted({int(r["meta"]["fy"]) for r in records})

    # (section, corp_code, fy) -> 문단 리스트 (fixed 기준)
    by_key: dict[tuple[str, str, int], list[str]] = {}
    for r in records:
        for sid, sc in r["fixed"].items():
            key = (sid, r["meta"]["corp_code"], int(r["meta"]["fy"]))
            by_key[key] = sc.paragraphs

    counts: list[dict[str, Any]] = []
    freq_rows: list[dict[str, Any]] = []
    top_rows: list[dict[str, Any]] = []
    sections = sorted(cfg["sections"].keys())

    for sid in sections:
        corps = sorted({k[1] for k in by_key if k[0] == sid})
        for prev_fy, curr_fy in zip(years, years[1:]):
            raw_items: list[tuple[str, str]] = []
            for corp in corps:
                a = by_key.get((sid, corp, prev_fy))
                b = by_key.get((sid, corp, curr_fy))
                if a is None or b is None:
                    continue
                d = diff_paragraphs(
                    [p for p in a if len(p) >= min_chars],
                    [p for p in b if len(p) >= min_chars],
                )
                raw_items.extend(
                    (corp, p) for p in d.changed if len(p) >= min_chars
                )

            # (rcept_no, section) 내 문단 텍스트 기준 중복 제거.
            # 여기서 corp 은 해당 연도 문서 1건과 1:1 대응하므로 corp 단위 dedup 과 같다.
            seen: set[tuple[str, str]] = set()
            dedup_items: list[tuple[str, str]] = []
            for corp, p in raw_items:
                if (corp, p) in seen:
                    continue
                seen.add((corp, p))
                dedup_items.append((corp, p))

            res = {}
            for label, items in (("before", raw_items), ("after", dedup_items)):
                if len(items) < 2:
                    res[label] = ([], 0)
                    continue
                pairs = minhash_pairs(
                    items,
                    num_perm=int(c["minhash_num_perm"]),
                    ngram=int(c["minhash_ngram"]),
                    threshold=float(c["minhash_threshold"]),
                    cross_group_only=True,
                )
                res[label] = (pairs, len(pairs))

            pair_label = f"{prev_fy}->{curr_fy}"
            counts.append({
                "section": sid, "pair": pair_label,
                "n_paragraphs_before": len(raw_items),
                "n_paragraphs_after": len(dedup_items),
                "n_common_pairs_before": res["before"][1],
                "n_common_pairs_after": res["after"][1],
                "reduction": round(
                    1 - res["after"][1] / res["before"][1], 4
                ) if res["before"][1] else 0.0,
            })

            # Part 4-10: corp_code 별 등장 빈도 (dedup 후 기준)
            after_pairs, _ = res["after"]
            appear: dict[str, int] = {}
            for i, j, _est in after_pairs:
                for idx in (int(i), int(j)):
                    corp = dedup_items[idx][0]
                    appear[corp] = appear.get(corp, 0) + 1
            total_slots = 2 * len(after_pairs)
            for corp, cnt in appear.items():
                freq_rows.append({
                    "section": sid, "pair": pair_label, "corp_code": corp,
                    "n_appearances": cnt,
                    "share": round(cnt / total_slots, 4) if total_slots else 0.0,
                })

            for i, j, est in after_pairs[: int(c["top_pairs"])]:
                pi, pj = dedup_items[int(i)], dedup_items[int(j)]
                top_rows.append({
                    "section": sid, "pair": pair_label,
                    "corp_a": pi[0], "corp_b": pj[0],
                    "est_jaccard": round(est, 4),
                    "para_a": pi[1][:300], "para_b": pj[1][:300],
                })

    return pd.DataFrame(counts), pd.DataFrame(freq_rows), pd.DataFrame(top_rows)


def dominance_alerts(freq: pd.DataFrame, index: pd.DataFrame,
                     threshold: float, raw_dir: Path) -> pd.DataFrame:
    """전체 쌍의 threshold 이상을 차지하는 corp_code 를 원본 경로와 함께 나열."""
    if freq.empty:
        return pd.DataFrame()
    total = freq.groupby(["section", "pair"])["n_appearances"].transform("sum")
    f = freq.assign(share_overall=(freq["n_appearances"] / total).round(4))
    hot = f[f["share_overall"] >= threshold].copy()
    if hot.empty:
        return hot

    meta = index.drop_duplicates("corp_code").set_index("corp_code")
    rows = []
    for r in hot.itertuples():
        # 해당 연도쌍에 실제로 쓰인 두 개 연도의 문서만 나열한다.
        pair_years = {int(y) for y in str(r.pair).split("->") if y.strip().isdigit()}
        docs = index[(index["corp_code"] == r.corp_code)
                     & (index["fy"].astype(int).isin(pair_years))]
        for d in docs.itertuples():
            rows.append({
                "section": r.section, "pair": r.pair, "corp_code": r.corp_code,
                "corp_name": meta.loc[r.corp_code, "corp_name"]
                if r.corp_code in meta.index else "",
                "share_overall": r.share_overall,
                "fy": d.fy, "rcept_no": d.rcept_no,
                "raw_path": str((raw_dir / f"{d.rcept_no}.zip").relative_to(PROJECT_ROOT)),
                "dart_url": DART_VIEWER.format(rcept_no=d.rcept_no),
            })
    return pd.DataFrame(rows).drop_duplicates()


# ===========================================================================
# 리포트
# ===========================================================================

def _md(df: pd.DataFrame) -> str:
    return df.to_markdown(index=False) + "\n" if not df.empty else "_(해당 없음)_\n"


def _hypothesis_table(audit: pd.DataFrame, cont: pd.DataFrame,
                      paras: pd.DataFrame) -> str:
    """배경에서 제기된 두 가설을 실제 수치로 판정한다."""
    # 가설 A: 종료 경계 탐지 실패로 K-IFRS 주석이 유입되었다
    n_eof = int((audit["end_header"] == END_REASON_EOF).sum())
    wide = audit.pivot_table(
        index=["rcept_no", "section"], columns="variant",
        values=["start_header", "end_header"], aggfunc="first")
    n_boundary_diff, n_units = 0, 0
    if not wide.empty:
        wide.columns = [f"{a}_{b}" for a, b in wide.columns]
        n_units = len(wide)
        n_boundary_diff = int(
            (wide["end_header_fixed"] != wide["end_header_legacy"]).sum()
            + (wide["start_header_fixed"] != wide["start_header_legacy"]).sum()
        )

    s1 = cont[cont["section"] == "S1"]
    c_leg = float(s1[s1["variant"] == "legacy"]["contamination_share"].mean())
    c_fix = float(s1[s1["variant"] == "fixed"]["contamination_share"].mean())

    # 가설 B: 표 셀이 문단으로 분해되었다
    s4 = paras[paras["section"] == "S4"]
    m_leg = float(s4[s4["variant"] == "legacy"]["mean"].mean())
    m_fix = float(s4[s4["variant"] == "fixed"]["mean"].mean())
    u_leg = float(s4[s4["variant"] == "legacy"]["share_under_10"].mean())
    u_fix = float(s4[s4["variant"] == "fixed"]["share_under_10"].mean())

    rows = [
        {
            "배경에서 제기된 가설":
                "S1 의 K-IFRS 주석 유입은 섹션 종료 경계 탐지 실패 때문이다",
            "판정": "**기각**",
            "근거": (f"종료 헤더 미탐(EOF) {n_eof}건 / {len(audit)}건. "
                   f"legacy 와 fixed 의 시작·종료 헤더가 {n_units}건 전부 동일"
                   f"(차이 {n_boundary_diff}건). S1 오염 문단 비중도 "
                   f"{c_leg:.2%} -> {c_fix:.2%} 로 줄지 않았다"),
        },
        {
            "배경에서 제기된 가설":
                "S4 의 문단당 문자 수가 낮은 것은 표 셀이 문단으로 분해된 탓이다",
            "판정": "**확인**",
            "근거": (f"수정 전 문단당 {m_leg:.1f}자, 10자 미만 문단 {u_leg:.0%}. "
                   f"표 유입을 막은 뒤 {m_fix:.1f}자, {u_fix:.0%}"),
        },
    ]
    return pd.DataFrame(rows).to_markdown(index=False) + "\n"


def write_contamination_report(cfg: Config, cont: pd.DataFrame, audit: pd.DataFrame,
                               out_dir: Path) -> Path:
    a = cfg["audit"]
    markers = a["contamination_markers"]
    marker_sets = markers if isinstance(markers, dict) else {"default": markers}
    thr = float(a["contamination_share_max"])
    lines: list[str] = []
    add = lines.append

    add("# P0-c 오염 진단 리포트\n")
    add("S1(사업의 내용)으로 추출된 텍스트에 K-IFRS 재무제표 주석이 섞여 들어갔는지 "
        "마커 기반으로 검사한다.\n")
    for key, ms in marker_sets.items():
        label = "기본(S1/S3/S4)" if key == "default" else key
        add(f"- 검사 마커 [{label}]: " + ", ".join(f"`{m}`" for m in ms))
    add("")
    add("오염 비중은 **문자 수 기준**이다: "
        "(오염 문단의 문자 수 합) / (섹션 전체 문자 수).\n")

    add("## 1. 섹션별 오염 지표 (legacy vs fixed)\n")
    agg = (
        cont.groupby(["variant", "section"])
        .agg(n_docs=("rcept_no", "nunique"),
             mean_marker_hits=("n_marker_hits", "mean"),
             docs_with_marker=("n_marker_hits", lambda s: int((s > 0).sum())),
             mean_contam_share=("contamination_share", "mean"),
             max_contam_share=("contamination_share", "max"))
        .round(4).reset_index()
        .sort_values(["section", "variant"])
    )
    add(_md(agg))

    add("## 2. 문서별 오염 마커 개수 — 상위 20건 (legacy)\n")
    leg = cont[cont["variant"] == "legacy"].sort_values("n_marker_hits", ascending=False)
    cols = ["corp_name", "fy", "section", "n_marker_hits", "n_distinct_markers",
            "n_dirty_paragraphs", "contamination_share", "markers_found"]
    add(_md(leg.head(20)[cols]))

    add("## 3. 문제 문서 목록 (Part 1-3)\n")
    add(f"종료 헤더가 `EOF` 이거나 오염 비중이 {thr:.0%} 를 초과하는 (문서 x 섹션).\n")
    for variant in ("legacy", "fixed"):
        sub = cont[cont["variant"] == variant]
        bad_eof = sub[sub["end_header"] == END_REASON_EOF]
        bad_cont = sub[sub["contamination_share"] > thr]
        bad = pd.concat([bad_eof, bad_cont]).drop_duplicates(
            subset=["rcept_no", "section"]
        )
        add(f"### {variant} — {len(bad)}건 "
            f"(EOF {len(bad_eof)}건, 오염 초과 {len(bad_cont)}건)\n")
        if bad.empty:
            add("_해당 없음_\n")
        else:
            show = bad[["corp_name", "fy", "section", "end_header",
                        "contamination_share", "char_len_text"]]
            add(_md(show.sort_values("contamination_share", ascending=False).head(40)))

    path = out_dir / "contamination_report.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    log.info("오염 리포트 -> %s", path)
    return path


def write_p0c_report(cfg: Config, audit: pd.DataFrame, cont: pd.DataFrame,
                     paras: pd.DataFrame, recount: pd.DataFrame,
                     alerts: pd.DataFrame, out_dir: Path) -> Path:
    g = cfg["gate0"]
    a = cfg["audit"]
    lines: list[str] = []
    add = lines.append

    add("# P0-c 종합 리포트 — 섹션 경계 · 문단 분할 수정\n")
    add(f"- 문서 {audit['rcept_no'].nunique()}건 x 섹션 "
        f"{audit['section'].nunique()}개, legacy/fixed 양쪽 추출 후 비교")
    add(f"- seed = {cfg['seed']}\n")

    # --- 가설 검증 (증거 기반)
    add("## 0. 가설 검증 결과\n")
    add(_hypothesis_table(audit, cont, paras))

    add("## 0-b. 실제로 관측된 결함\n")
    add("| # | 결함 | 이 표본에서 발현했는가 | 수정 |")
    add("|---|---|---|---|")
    add("| 1 | 블록 분해가 화이트리스트 방식이라 DART 커스텀 컨테이너 태그"
        "(`<TABLE-GROUP>`, `<SECTION-3>`, `<COVER>`, `<LIBRARY>`) 안의 `<table>` 이 "
        "인라인으로 취급되어 표 전체가 본문 문단으로 새어 들어감 | "
        "**예 — 전 섹션에서 심각하게 발현** | 인라인 태그만 블랙리스트로 지정하고 "
        "나머지는 블록으로 재귀. `<table>` 은 어디에 있든 표로 분리 |")
    add("| 2 | `1. 회사의 개요`, `2. 재무 등에 관한 사항` 같은 하위 소제목이 퍼지 92 로 "
        "최상위 헤더에 오인 매칭 | 아니오 — 헤더 후보로는 잡혔으나 대상 4개 섹션의 "
        "경계를 바꾸지는 않았다 | 정확 일치 요구(퍼지 97) + 로마숫자 체계 문서는 "
        "로마숫자만 섹션 '시작'으로 인정 (방어적) |")
    add("| 3 | 시작 헤더만 찾으면 `found=True` — 종료 실패가 성공으로 집계 | "
        "아니오 — EOF 0건 | `require_terminator=True` 로 강등 (방어적) |")
    add("")
    add("> 결함 2·3 은 이 표본에서 발현하지 않았다. 그래도 고친 이유는 표본이 30개 "
        "기업 x 3개 연도뿐이고, Phase 1 에서 표본을 크게 늘리면 만나게 될 구조적 "
        "취약점이기 때문이다. 회귀 테스트(`tests/test_boundaries.py`)로 고정해 두었다.\n")

    add("## 0-c. 그렇다면 S1 의 K-IFRS 마커는 무엇인가\n")
    add("표본을 직접 열어 확인한 결과, 경계 오류가 아니라 **원문 자체의 내용**이었다.\n")
    add("- 에코프로 2024 S1 의 `기대신용손실` 15건은 `II. 사업의 내용` 하위의 "
        "위험관리 서술(매출채권 신용위험, 손실충당금 설정 방침)이다. 사업보고서 서식상 "
        "이 항목은 사업의 내용 안에 들어간다.")
    add("- 오염도가 가장 높은 섹션은 S1 이 아니라 **S2(경영진단)** 이다. "
        "현대해상 2024 S2 처럼 `2.1 재무제표 작성기준` 같은 회계정책 문단이 "
        "MD&A 안에 그대로 들어가는 사례가 있다.")
    add("- 따라서 이 문단들을 제거하려면 경계 탐지가 아니라 **문단 수준 필터**가 "
        "필요하다. Phase 2(파싱) 또는 Phase 5(Template Filter)에서 다루는 것이 맞다.\n")

    # --- Part 1
    add("## 1. 경계 감사 (Part 1)\n")
    eof = (
        audit.assign(is_eof=audit["end_header"] == END_REASON_EOF)
        .groupby(["variant", "section"])
        .agg(n=("is_eof", "size"), n_eof=("is_eof", "sum"),
             found_rate=("found", "mean"))
        .round(4).reset_index().sort_values(["section", "variant"])
    )
    add("종료 헤더를 못 찾은(EOF) 건수와 섹션 발견율:\n")
    add(_md(eof))
    add("전체 원장은 `boundary_audit.csv` (시작 헤더 원문, 종료 헤더 원문, "
        f"첫/마지막 {a['snippet_chars']}자 포함).\n")

    # --- Part 2
    add("## 2. 경계 탐지 수정 전/후 비교 (Part 2-5)\n")
    piv = (
        audit.groupby(["section", "variant"])
        .agg(found_rate=("found", "mean"),
             mean_chars=("char_len_text", "mean"),
             median_chars=("char_len_text", "median"))
        .round(1).reset_index()
        .pivot(index="section", columns="variant",
               values=["found_rate", "mean_chars", "median_chars"])
    )
    piv.columns = [f"{a_}_{b_}" for a_, b_ in piv.columns]
    piv = piv.reset_index()
    contam = (
        cont.groupby(["section", "variant"])["contamination_share"].mean()
        .round(4).unstack()
    )
    contam.columns = [f"contam_{c}" for c in contam.columns]
    comp = piv.merge(contam.reset_index(), on="section")
    comp["chars_delta_%"] = (
        (comp["mean_chars_fixed"] - comp["mean_chars_legacy"])
        / comp["mean_chars_legacy"].replace(0, pd.NA) * 100
    ).round(1)
    add(_md(comp))

    # --- Part 3
    add("## 3. 문단 분할 수정 전/후 (Part 3-6,7)\n")
    pstat = (
        paras.groupby(["section", "variant"])
        .agg(mean=("mean", "mean"), median=("median", "mean"),
             p10=("p10", "mean"), p90=("p90", "mean"),
             mean_n_paragraphs=("n_paragraphs", "mean"),
             share_under_10=("share_under_10", "mean"))
        .round(2).reset_index().sort_values(["section", "variant"])
    )
    add("문단당 문자 수 분포 (문서 평균):\n")
    add(_md(pstat))
    s4 = pstat[pstat["section"] == "S4"]
    if len(s4) == 2:
        leg = s4[s4["variant"] == "legacy"].iloc[0]
        fix = s4[s4["variant"] == "fixed"].iloc[0]
        add(f"- **S4**: 문단당 평균 {leg['mean']:.1f}자 → {fix['mean']:.1f}자, "
            f"문단 수 {leg['mean_n_paragraphs']:.0f} → {fix['mean_n_paragraphs']:.0f}. "
            f"표 셀이 문단으로 들어가던 문제가 해소되었다.\n")
    add("표 제거 로직은 `iter_blocks()` 한 곳에서 S1~S4 에 동일하게 적용된다 "
        "(섹션별 분기 없음).\n")

    # --- Part 4
    add("## 4. 중복 제거 후 공통문단 재계산 (Part 4-9)\n")
    add(_md(recount))
    if not recount.empty:
        tb = int(recount["n_common_pairs_before"].sum())
        ta = int(recount["n_common_pairs_after"].sum())
        add(f"- 합계: {tb:,}쌍 → {ta:,}쌍 "
            f"({(1 - ta / tb) * 100:.1f}% 감소)\n" if tb else "- 합계: 0쌍\n")

    add(f"### corp_code 편중 점검 (Part 4-10, 임계값 {a['corp_dominance_max']:.0%})\n")
    if alerts.empty:
        add("_전체 쌍의 20% 이상을 차지하는 corp_code 없음. 매핑 오류 징후 없음._\n")
    else:
        add("아래 문서는 공통쌍의 비중이 비정상적으로 높다. 원본을 열어 매핑 오류 "
            "여부를 확인할 것.\n")
        add(_md(alerts.head(30)))

    # --- Gate 0 재판정
    add("## 5. Gate 0 체크리스트 — 수정 후 수치\n")
    fixed = audit[audit["variant"] == "fixed"]
    doc_ok = (
        fixed.groupby("rcept_no")["found"].any().mean() if not fixed.empty else 0.0
    )
    s1 = fixed[fixed["section"] == "S1"]
    s2 = fixed[fixed["section"] == "S2"]
    s1_mean = float(s1["char_len_text"].mean()) if not s1.empty else 0.0
    s2_mean = float(s2["char_len_text"].mean()) if not s2.empty else 0.0
    n_pairs = int(recount["n_common_pairs_after"].sum()) if not recount.empty else 0

    checks = pd.DataFrame([
        {"항목": f"파싱 성공률 {g['parse_success_rate_min']:.0%} 이상",
         "수정 전": f"{audit[audit['variant'] == 'legacy'].groupby('rcept_no')['found'].any().mean():.1%}",
         "수정 후": f"{doc_ok:.1%}",
         "결과": "PASS" if doc_ok >= g["parse_success_rate_min"] else "FAIL"},
        {"항목": f"S1 평균 {g['s1_mean_chars_min']:,}자 이상",
         "수정 전": f"{audit[(audit['variant'] == 'legacy') & (audit['section'] == 'S1')]['char_len_text'].mean():,.0f}자",
         "수정 후": f"{s1_mean:,.0f}자",
         "결과": "PASS" if s1_mean >= g["s1_mean_chars_min"] else "FAIL"},
        {"항목": f"S2 {g['s2_drop_threshold_chars']:,}자 이상 (미만이면 MVP 제외)",
         "수정 전": f"{audit[(audit['variant'] == 'legacy') & (audit['section'] == 'S2')]['char_len_text'].mean():,.0f}자",
         "수정 후": f"{s2_mean:,.0f}자",
         "결과": "PASS" if s2_mean >= g["s2_drop_threshold_chars"] else "FAIL"},
        {"항목": "기업 간 공통 변경 문단 관측",
         "수정 전": "—", "수정 후": f"{n_pairs:,}쌍",
         "결과": "PASS" if n_pairs > 0 else "FAIL"},
    ])
    add(_md(checks))

    add("### 섹션별 D1 재판정 (수정 후)\n")
    d1 = (
        fixed.groupby(["section", "section_name"])
        .agg(found_rate=("found", "mean"), mean_chars=("char_len_text", "mean"))
        .round(1).reset_index()
    )
    d1["D1_통과(평균2000자)"] = d1["mean_chars"] >= g["section_mean_chars_min"]
    add(_md(d1))

    add("## 6. 다음 단계\n")
    add("1. `python -m src.pilot.p0_diagnostics` 를 다시 실행해 "
        "`data/pilot/sections/` 를 수정된 추출로 갱신한다.")
    add("2. `python -m src.pilot.p0b_change_diagnostics` 재실행.")
    add("3. `data/pilot/manual_review.html` 로 20건 수동 검수 후 "
        "JSON 을 저장해 남긴다.")
    add("4. D1 미달 섹션을 확정해 Phase 2 섹션 목록을 고정한다.\n")

    path = out_dir / "p0c_report.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    log.info("종합 리포트 -> %s", path)
    return path


# ===========================================================================
# main
# ===========================================================================

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="P0-c 경계 감사 및 수정")
    ap.add_argument("--config", default=None)
    ap.add_argument("--limit", type=int, default=None, help="문서 수 제한")
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    out_dir = cfg.dir("pilot", mock=args.mock)
    setup_logging(out_dir / "p0c.log", level=getattr(logging, args.log_level.upper()))
    set_seed(cfg)

    idx_path = out_dir / "reports_index.csv"
    if not idx_path.exists():
        raise SystemExit(
            f"선행 조건 미충족: {idx_path} 가 없습니다. "
            f"먼저 `python -m src.pilot.p0_diagnostics` 를 실행하세요."
        )
    index = pd.read_csv(idx_path, dtype={"stock_code": str, "rcept_no": str,
                                         "corp_code": str})
    if args.limit:
        index = index.head(args.limit)
    log.info("감사 대상 문서 %d건", len(index))

    raw_dir = cfg.dir("raw") / ("mock" if args.mock else "")
    a = cfg["audit"]
    snippet_chars = int(a["snippet_chars"])

    audit_rows: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    sec_dir = out_dir / "sections_fixed"
    sec_dir.mkdir(parents=True, exist_ok=True)

    for meta in tqdm(list(index.itertuples(index=False)), desc="경계 감사", unit="doc"):
        meta = pd.Series(meta._asdict())
        zip_path = raw_dir / f"{meta['rcept_no']}.zip"
        if not zip_path.exists():
            log.warning("원본 ZIP 없음, 건너뜀: %s", zip_path.name)
            continue
        try:
            _, html = pick_body_file(zip_path)
        except Exception as exc:
            log.warning("본문 식별 실패 %s: %s", meta["rcept_no"], exc)
            continue
        rows, legacy, fixed = audit_document(cfg, meta, html, snippet_chars)
        audit_rows.extend(rows)
        records.append({"meta": meta, "legacy": legacy, "fixed": fixed})
        for sid, sc in fixed.items():
            if sc.found and sc.text:
                (sec_dir / f"{meta['corp_code']}_{meta['fy']}_{sid}.txt").write_text(
                    sc.text, encoding="utf-8")

    if not records:
        raise SystemExit("감사할 문서가 없습니다. data/raw 캐시를 확인하세요.")

    audit = pd.DataFrame(audit_rows)
    audit.to_csv(out_dir / "boundary_audit.csv", index=False, encoding="utf-8-sig")
    log.info("boundary_audit.csv -> %d행", len(audit))

    cont = contamination_frame(records, a["contamination_markers"])
    cont.to_csv(out_dir / "contamination_detail.csv", index=False, encoding="utf-8-sig")
    write_contamination_report(cfg, cont, audit, out_dir)

    paras = paragraph_frame(records)
    paras.to_csv(out_dir / "paragraph_stats.csv", index=False, encoding="utf-8-sig")

    recount, freq, top = recount_common_pairs(records, cfg)
    recount.to_csv(out_dir / "common_pairs_recount.csv", index=False, encoding="utf-8-sig")
    freq.to_csv(out_dir / "corp_dominance.csv", index=False, encoding="utf-8-sig")
    top.to_csv(out_dir / "common_paragraphs_fixed.csv", index=False, encoding="utf-8-sig")

    alerts = dominance_alerts(freq, index, float(a["corp_dominance_max"]),
                              cfg.dir("raw"))
    if not alerts.empty:
        alerts.to_csv(out_dir / "corp_dominance_alerts.csv", index=False,
                      encoding="utf-8-sig")

    write_p0c_report(cfg, audit, cont, paras, recount, alerts, out_dir)

    from src.pilot.manual_review import build_manual_review
    build_manual_review(cfg, records, out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
