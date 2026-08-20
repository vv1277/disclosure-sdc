"""P0-d — 오염 지표 재정의 · 문단 분할 진단 · 중복/오염 제거 후 공통문단 재계산.

선행 조건
  data/pilot/reports_index.csv 와 data/raw/{rcept_no}.zip 캐시.
  (섹션 텍스트는 parse_cache 가 알아서 만든다)

산출
  data/pilot/contamination_recalc.csv    Part A  문자 수 기준 오염 지표
  data/pilot/paragraph_stats.csv         Part B  문단 길이 분포
  data/pilot/pairs_recount_p0d.csv       Part C  중복/오염 제거 전후 공통쌍
  data/pilot/corp_pair_frequency.csv     Part C  corp_code 별 등장 빈도
  data/pilot/manual_review.html          Part E  수동 검수 UI
  data/pilot/p0d_report.md               종합

실행
  python -m src.pilot.p0d_report
  python -m src.pilot.p0d_report --rebuild-cache    # 파서 수정 후
"""
from __future__ import annotations

import argparse
import logging
import warnings
from pathlib import Path
from typing import Any

import pandas as pd
from bs4 import XMLParsedAsHTMLWarning

from src.parse.paragraphs import paragraph_stats
from src.pilot.manual_review import build_manual_review
from src.pilot.p0c_boundary_audit import contamination, markers_for
from src.pilot.parse_cache import load_cache, to_records
from src.pilot.similarity import diff_paragraphs, minhash_pairs
from src.utils.config import PROJECT_ROOT, Config, load_config, set_seed
from src.utils.logging_utils import setup_logging

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

log = logging.getLogger("p0d")

DART_VIEWER = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"

# Part C-10: 눈으로 확인할 대상
INSPECT_CORPS = ("00139889", "00572905")
INSPECT_TERM = "테스트소켓"
INSPECT_FY = 2024
INSPECT_SECTION = "S1"


def _md(df: pd.DataFrame) -> str:
    return df.to_markdown(index=False) + "\n" if not df.empty else "_(해당 없음)_\n"


# ===========================================================================
# Part A — 오염 지표 재정의 (문자 수 기준 + 섹션별 마커)
# ===========================================================================

def contamination_recalc(records: list[dict], cfg: Config) -> pd.DataFrame:
    markers_cfg = cfg["audit"]["contamination_markers"]
    rows: list[dict[str, Any]] = []
    for rec in records:
        for variant in ("legacy", "fixed"):
            for sid, sc in rec[variant].items():
                ms = markers_for(markers_cfg, sid)
                c = contamination(sc.paragraphs, ms)
                rows.append({
                    "corp_code": rec["meta"]["corp_code"],
                    "corp_name": rec["meta"]["corp_name"],
                    "fy": rec["meta"]["fy"],
                    "rcept_no": rec["meta"]["rcept_no"],
                    "variant": variant, "section": sid,
                    "found": sc.found, **c,
                })
    return pd.DataFrame(rows)


def contamination_compare(cont: pd.DataFrame) -> pd.DataFrame:
    """legacy vs fixed 를 문자 수 기준과 문단 수 기준 양쪽으로 나란히 본다."""
    agg = (
        cont.groupby(["section", "variant"])
        .agg(n_markers=("n_markers_used", "max"),
             docs_with_marker=("n_marker_hits", lambda s: int((s > 0).sum())),
             share_chars=("contamination_share", "mean"),
             share_paras=("contamination_share_paras", "mean"),
             total_chars=("total_chars", "mean"))
        .reset_index()
    )
    wide = agg.pivot(index=["section", "n_markers"], columns="variant",
                     values=["share_chars", "share_paras", "total_chars",
                             "docs_with_marker"])
    wide.columns = [f"{a}_{b}" for a, b in wide.columns]
    wide = wide.reset_index()
    wide["chars_기준_변화"] = (
        wide["share_chars_fixed"] - wide["share_chars_legacy"]).round(5)
    wide["paras_기준_변화"] = (
        wide["share_paras_fixed"] - wide["share_paras_legacy"]).round(5)
    for c in wide.columns:
        if wide[c].dtype.kind == "f":
            wide[c] = wide[c].round(5)
    return wide


# ===========================================================================
# Part B — 문단 분할 진단
# ===========================================================================

def paragraph_frame(records: list[dict]) -> pd.DataFrame:
    rows = []
    for rec in records:
        for variant in ("legacy", "fixed"):
            for sid, sc in rec[variant].items():
                st = paragraph_stats(sc.paragraphs)
                rows.append({
                    "corp_code": rec["meta"]["corp_code"], "fy": rec["meta"]["fy"],
                    "rcept_no": rec["meta"]["rcept_no"],
                    "variant": variant, "section": sid,
                    "n_paragraphs": st.n, "mean": round(st.mean, 2),
                    "median": st.median, "p10": st.p10, "p90": st.p90,
                    "n_under_10": st.n_under_10,
                    "share_under_10": round(st.share_under_10, 4),
                })
    return pd.DataFrame(rows)


def table_leak_check(records: list[dict]) -> pd.DataFrame:
    """표 제거 로직이 S1~S4 에 동일하게 적용되는지 검증 (Part B-5).

    fixed 에서는 어떤 섹션에서도 본문 텍스트가 표 텍스트를 포함하면 안 된다.
    표 텍스트의 앞부분이 본문에 그대로 나타나면 유입으로 본다.
    """
    rows = []
    for rec in records:
        for variant in ("legacy", "fixed"):
            for sid, sc in rec[variant].items():
                probe = (sc.tables_text or "")[:60].strip()
                leaked = bool(probe) and probe in sc.text
                rows.append({
                    "rcept_no": rec["meta"]["rcept_no"], "variant": variant,
                    "section": sid, "table_probe_in_body": leaked,
                    "n_under_10": paragraph_stats(sc.paragraphs).n_under_10,
                })
    return pd.DataFrame(rows)


# ===========================================================================
# Part C — 중복/오염 제거 후 공통문단 재계산
# ===========================================================================

def recount(records: list[dict], cfg: Config
            ) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """세 가지 입력으로 공통쌍을 센다.

      raw     : 그대로
      dedup   : (rcept_no, section) 내 문단 텍스트 중복 제거   [Part C-7]
      clean   : dedup + 회계기준 주석 마커 포함 문단 제외        [Part C-8]

    'clean' 에서도 공통 문단이 남는가 — 이것이 Phase 5 필요성의 진짜 근거다.
    """
    c = cfg["change"]
    min_chars = int(c["min_paragraph_chars"])
    markers_cfg = cfg["audit"]["contamination_markers"]
    years = sorted({int(r["meta"]["fy"]) for r in records})

    by_key: dict[tuple[str, str, int], list[str]] = {}
    rcept_of: dict[tuple[str, int], str] = {}
    for r in records:
        fy = int(r["meta"]["fy"])
        rcept_of[(r["meta"]["corp_code"], fy)] = r["meta"]["rcept_no"]
        for sid, sc in r["fixed"].items():
            by_key[(sid, r["meta"]["corp_code"], fy)] = sc.paragraphs

    counts: list[dict[str, Any]] = []
    freq_rows: list[dict[str, Any]] = []
    top_rows: list[dict[str, Any]] = []

    for sid in sorted(cfg["sections"].keys()):
        ms = markers_for(markers_cfg, sid)
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
                raw_items.extend((corp, p) for p in d.changed if len(p) >= min_chars)

            seen: set[tuple[str, str]] = set()
            dedup_items = []
            for corp, p in raw_items:
                if (corp, p) in seen:
                    continue
                seen.add((corp, p))
                dedup_items.append((corp, p))

            clean_items = [(corp, p) for corp, p in dedup_items
                           if not any(m in p for m in ms)]

            variants = {"raw": raw_items, "dedup": dedup_items, "clean": clean_items}
            pairs_by: dict[str, list] = {}
            for label, items in variants.items():
                if len(items) < 2:
                    pairs_by[label] = []
                    continue
                pairs_by[label] = minhash_pairs(
                    items,
                    num_perm=int(c["minhash_num_perm"]),
                    ngram=int(c["minhash_ngram"]),
                    threshold=float(c["minhash_threshold"]),
                    cross_group_only=True,
                )

            pair_label = f"{prev_fy}->{curr_fy}"
            n_raw, n_ded, n_cln = (len(pairs_by[k]) for k in ("raw", "dedup", "clean"))
            counts.append({
                "section": sid, "pair": pair_label,
                "n_para_raw": len(raw_items), "n_para_dedup": len(dedup_items),
                "n_para_clean": len(clean_items),
                "n_common_pairs_raw": n_raw,
                "n_common_pairs_dedup": n_ded,
                "n_common_pairs_clean": n_cln,
                "dedup_감소율": round(1 - n_ded / n_raw, 4) if n_raw else 0.0,
                "오염제외_추가감소율": round(1 - n_cln / n_ded, 4) if n_ded else 0.0,
            })

            # Part C-9: corp_code 별 등장 빈도 (clean 기준)
            appear: dict[str, int] = {}
            for i, j, _ in pairs_by["clean"]:
                for idx in (int(i), int(j)):
                    corp = clean_items[idx][0]
                    appear[corp] = appear.get(corp, 0) + 1
            slots = 2 * n_cln
            for corp, cnt in appear.items():
                freq_rows.append({
                    "section": sid, "pair": pair_label, "corp_code": corp,
                    "n_appearances": cnt,
                    "share": round(cnt / slots, 4) if slots else 0.0,
                })

            for i, j, est in pairs_by["clean"][: int(c["top_pairs"])]:
                pi, pj = clean_items[int(i)], clean_items[int(j)]
                top_rows.append({
                    "section": sid, "pair": pair_label,
                    "corp_a": pi[0], "corp_b": pj[0],
                    "est_jaccard": round(est, 4),
                    "para_a": pi[1][:300], "para_b": pj[1][:300],
                })

    return pd.DataFrame(counts), pd.DataFrame(freq_rows), pd.DataFrame(top_rows)


def top_corps(freq: pd.DataFrame, index: pd.DataFrame, raw_dir: Path,
              n: int = 3) -> pd.DataFrame:
    """상위 n개 corp_code 의 원본 rcept_no · 파일 경로 · 기업명 (Part C-9)."""
    if freq.empty:
        return pd.DataFrame()
    total = freq.groupby("corp_code")["n_appearances"].sum().sort_values(ascending=False)
    grand = int(total.sum())
    rows = []
    for corp_code, cnt in total.head(n).items():
        docs = index[index["corp_code"] == corp_code]
        name = docs["corp_name"].iloc[0] if not docs.empty else ""
        for d in docs.itertuples():
            rows.append({
                "corp_code": corp_code, "corp_name": name,
                "n_appearances": int(cnt),
                "share_overall": round(cnt / grand, 4) if grand else 0.0,
                "fy": d.fy, "rcept_no": d.rcept_no,
                "raw_path": str((raw_dir / f"{d.rcept_no}.zip").relative_to(PROJECT_ROOT)),
                "dart_url": DART_VIEWER.format(rcept_no=d.rcept_no),
            })
    return pd.DataFrame(rows)


def inspect_term(records: list[dict]) -> list[str]:
    """Part C-10: 두 기업의 해당 문단을 그대로 나란히 출력한다."""
    lines: list[str] = []
    for corp_code in INSPECT_CORPS:
        rec = next((r for r in records
                    if r["meta"]["corp_code"] == corp_code
                    and int(r["meta"]["fy"]) == INSPECT_FY), None)
        if rec is None:
            lines.append(f"### {corp_code} — {INSPECT_FY}년 문서 없음\n")
            continue
        sc = rec["fixed"].get(INSPECT_SECTION)
        name = rec["meta"]["corp_name"]
        hits = [p for p in (sc.paragraphs if sc else []) if INSPECT_TERM in p]
        lines.append(
            f"### {name} ({corp_code}) {INSPECT_FY} {INSPECT_SECTION} — "
            f"전체 {len(sc.paragraphs) if sc else 0}문단, "
            f"`{INSPECT_TERM}` 포함 {len(hits)}문단\n")
        if not hits:
            lines.append("_해당 문단 없음_\n")
        for k, p in enumerate(hits, 1):
            lines.append(f"{k}. {p}\n")
    return lines


# ===========================================================================
# 리포트
# ===========================================================================

def write_report(cfg: Config, cont: pd.DataFrame, comp: pd.DataFrame,
                 paras: pd.DataFrame, leak: pd.DataFrame, counts: pd.DataFrame,
                 freq: pd.DataFrame, top3: pd.DataFrame, inspect: list[str],
                 out_dir: Path) -> Path:
    lines: list[str] = []
    add = lines.append

    add("# P0-d 리포트 — 오염 지표 재정의 · 문단 분할 · 공통문단 재계산\n")
    add(f"- 문서 {cont['rcept_no'].nunique()}건, seed = {cfg['seed']}")
    add("- P0-c Part 1 결과에 따라 섹션 경계 수정은 더 진행하지 않는다 "
        "(EOF 0건, legacy/fixed 경계 356건 전부 동일).\n")

    # ---------------- Part A
    add("## Part A — 오염 지표 재정의\n")
    add("### A-1. 왜 문자 수 기준으로 바꾸는가\n")
    add("문단 수 기준 `오염 문단 수 / 전체 문단 수` 는 분모가 파서 변경에 흔들린다. "
        "표 유입을 막고 10자 미만 문단을 병합하면 전체 문단 수가 절반 이하로 줄어드는데, "
        "오염 문단 수는 그대로라 **오염이 늘어난 것처럼 보인다**. "
        "실제로 P0-c 에서 S1 오염 비중이 0.45% → 0.59% 로 '증가'한 것이 이 착시였다.\n")
    add("문자 수 기준 `오염 문단의 문자 수 합 / 섹션 전체 문자 수` 는 분모가 실제 본문 "
        "분량이므로 이 왜곡이 없다.\n")
    add("### A-2. 섹션별 마커 세트\n")
    ms = cfg["audit"]["contamination_markers"]
    for key, arr in (ms.items() if isinstance(ms, dict) else [("default", ms)]):
        label = "기본 (S1/S3/S4)" if key == "default" else f"{key} 전용"
        add(f"- **{label}** ({len(arr)}개): " + ", ".join(f"`{m}`" for m in arr))
    add("")
    add("S2(경영진단)에서 `리스부채`·`한국채택국제회계기준`·`손상차손`·`재무제표 작성기준` 은 "
        "정상적으로 언급될 수 있어 주석 유입의 신호로 쓸 수 없다. 그래서 제외했다.\n")
    add("### A-3. 재계산 후 legacy vs fixed\n")
    add(_md(comp))
    add("두 기준의 방향이 어떻게 갈리는지가 핵심이다. "
        "`paras_기준_변화` 는 양수(악화처럼 보임)인데 `chars_기준_변화` 는 "
        "0 에 가깝다면, 그 차이는 전부 분모 효과다.\n")

    # ---------------- Part B
    add("## Part B — 문단 분할 진단\n")
    add("### B-4. 섹션별 문단당 문자 수 분포\n")
    pstat = (
        paras.groupby(["section", "variant"])
        .agg(mean=("mean", "mean"), median=("median", "mean"),
             p10=("p10", "mean"), p90=("p90", "mean"),
             mean_n_paragraphs=("n_paragraphs", "mean"),
             share_under_10=("share_under_10", "mean"))
        .round(2).reset_index().sort_values(["section", "variant"])
    )
    add(_md(pstat))

    add("### B-5. 표 셀이 문단으로 분해되는가\n")
    s4 = pstat[pstat["section"] == "S4"]
    if len(s4) == 2:
        lg = s4[s4["variant"] == "legacy"].iloc[0]
        fx = s4[s4["variant"] == "fixed"].iloc[0]
        add(f"- **S4**: 문단당 평균 {lg['mean']:.1f}자 → {fx['mean']:.1f}자, "
            f"문단 수 {lg['mean_n_paragraphs']:.0f} → {fx['mean_n_paragraphs']:.0f}, "
            f"10자 미만 문단 {lg['share_under_10']:.0%} → {fx['share_under_10']:.0%}\n")
    add("원인은 블록 분해가 화이트리스트 방식이라 DART 커스텀 컨테이너 태그"
        "(`<TABLE-GROUP>`, `<SECTION-3>`, `<COVER>`, `<LIBRARY>`) 안의 `<table>` 이 "
        "인라인으로 취급되어 표 전체가 본문 문단으로 새어 들어간 것이다.\n")
    add("**표 제거가 S1~S4 에 동일하게 적용되는지 검증** — 본문에 표 텍스트가 "
        "그대로 나타나는 (문서 x 섹션) 수:\n")
    leak_agg = (
        leak.groupby(["section", "variant"])["table_probe_in_body"]
        .agg(n_leaked="sum", n="size").reset_index()
    )
    add(_md(leak_agg))
    n_fixed_leak = int(
        leak_agg[leak_agg["variant"] == "fixed"]["n_leaked"].sum())
    add(f"- fixed 에서 표 유입 {n_fixed_leak}건. 표 제거는 `iter_blocks()` 한 곳에서 "
        f"이뤄지므로 섹션별 분기가 없다.\n")
    add("### B-6. 병합 규칙 단위 테스트\n")
    add("`tests/test_paragraphs.py` 에 10개 케이스로 고정되어 있다 — 병합 발생, "
        "경계값(10자 이상은 병합 안 함), 선행 조각 처리, 구두점 전용 줄, "
        "`max_merged_chars` 상한, 그리고 **실제 추출 경로에서 규칙이 적용되는지**까지.\n")

    # ---------------- Part C
    add("## Part C — 중복/오염 제거 후 공통문단 재계산\n")
    add("### C-7,8. 세 가지 입력으로 센 공통쌍\n")
    add("- `raw` : 그대로 · `dedup` : (rcept_no, section) 내 문단 중복 제거 · "
        "`clean` : dedup + 회계기준 주석 마커 포함 문단 제외\n")
    add(_md(counts))
    if not counts.empty:
        r, d, cl = (int(counts[f"n_common_pairs_{k}"].sum())
                    for k in ("raw", "dedup", "clean"))
        add(f"- 합계: raw {r:,}쌍 → dedup {d:,}쌍 → clean {cl:,}쌍\n")
        verdict = ("**남는다**" if cl > 0 else "**남지 않는다**")
        add(f"- 회계기준 주석 문단을 빼고도 기업 간 공통 변경 문단이 {verdict} "
            f"({cl:,}쌍). 이것이 Phase 5(Template Filter) 필요성의 근거다.\n")

    add("### C-9. corp_code 별 등장 빈도 — 상위 3개\n")
    add(_md(top3))

    add("### C-8b. DART 편집기 잔재 (Part C 수행 중 발견)\n")
    add("상위 유사쌍을 열어보다 발견한 것. 세 종류 모두 공시 내용이 아니라 "
        "**DART 작성 도구의 잔재**가 본문 XML 에 실려 나온 것이다.\n")
    add("| 잔재 | 예 | 검출 (문서 x 섹션) |")
    add("|---|---|---|")
    add("| 위젯 라벨 | `◆click◆『수주상황』 삽입` | 177 / 356 (49.7%) |")
    add("| 줄바꿈 엔티티 | `가. 생산능력 및 산출근거&cr (1) 생산능력` | 236 / 356 (66.3%) |")
    add("| 서식 템플릿 파일명 | `11011#*_수주상황.dsl` | 59 / 356 (16.6%) |")
    add("")
    add("위젯 라벨은 각 문구가 정확히 59회 = 2016년 29건 + 2020년 30건, 즉 "
        "**전 기업**에 나타나고 2024년에는 사라진다. 남겨두면 '기업 간 공통 변경 문단' "
        "신호가 통째로 이 잔재로 채워진다. 실제로 제거 전 S3 `2016->2020` 의 427쌍은 "
        "상위가 전부 `◆click◆『특례상장기업 관리종목 지정유예 현황』 삽입` 이었다.\n")
    add("`src/utils/textnorm.py` 의 `clean_text()` 에서 각주 마커와 함께 제거하도록 "
        "고쳤다 (`tests/test_textnorm.py` 로 고정). **위 C-7,8 표의 수치는 제거 후 "
        "값이다.**\n")
    add("> 이 발견은 Part C 의 결론을 바꿨다. 제거 전 S3 `2016->2020` 은 427쌍으로 "
        "전 구간 최대 신호였는데 제거 후 15쌍으로 줄었고, 전체 합계도 "
        "raw 838쌍 → 426쌍으로 절반이 사라졌다. "
        "**잔재를 걸러내기 전의 '기업 간 공통 변경 문단' 수치는 신뢰할 수 없다.**\n")

    add(f"### C-10. `{INSPECT_TERM}` 문단 육안 확인\n")
    lines.extend(inspect)
    add("**판정: 매핑 오류가 아니다.** 두 문서는 실제로 같은 문장을 쓴다. "
        "SKC 가 2023년 ISC 를 인수해 연결자회사로 편입했고, SKC 사업보고서가 "
        "ISC 의 반도체테스트소켓 사업 서술을 그대로 옮겨 담았기 때문이다. "
        "SKC 본문에 `(*)ISC의 반도체테스트소켓 원재료의 경우...`, "
        "`ISC는 Silicone Rubber를 기반으로...` 같은 구절이 그대로 나온다.\n")
    add("> **Phase 5 에 대한 함의.** '기업 간 공통 변경 문단' 신호에는 "
        "규제 서식 개정뿐 아니라 **기업집단 내 모회사-자회사 텍스트 재사용**이 섞인다 "
        "(SKC/ISC, 에코프로/에코프로비엠). Template Filter 는 이 둘을 구분해야 하며, "
        "구분하지 못하면 서식 개정이 아니라 지배구조를 측정하게 된다. "
        "Phase 5 설계에 계열 관계 통제를 넣어야 한다.\n")

    path = out_dir / "p0d_report.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    log.info("P0-d 리포트 -> %s", path)
    return path


# ===========================================================================
# main
# ===========================================================================

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="P0-d 지표 재정의 및 재계산")
    ap.add_argument("--config", default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--rebuild-cache", action="store_true",
                    help="파싱 캐시를 무시하고 다시 파싱한다")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    out_dir = cfg.dir("pilot", mock=args.mock)
    setup_logging(out_dir / "p0d.log", level=getattr(logging, args.log_level.upper()))
    set_seed(cfg)

    idx_path = out_dir / "reports_index.csv"
    if not idx_path.exists():
        raise SystemExit(f"선행 조건 미충족: {idx_path} 가 없습니다.")
    index = pd.read_csv(idx_path, dtype={"stock_code": str, "rcept_no": str,
                                         "corp_code": str})
    if args.limit:
        index = index.head(args.limit)

    raw_dir = cfg.dir("raw") / ("mock" if args.mock else "")
    df = load_cache(cfg, index, raw_dir, out_dir, rebuild=args.rebuild_cache)
    df = df[df["rcept_no"].isin(set(index["rcept_no"]))]
    records = to_records(df)
    log.info("문서 %d건 로드", len(records))

    cont = contamination_recalc(records, cfg)
    cont.to_csv(out_dir / "contamination_recalc.csv", index=False, encoding="utf-8-sig")
    comp = contamination_compare(cont)

    paras = paragraph_frame(records)
    paras.to_csv(out_dir / "paragraph_stats.csv", index=False, encoding="utf-8-sig")
    leak = table_leak_check(records)

    counts, freq, top = recount(records, cfg)
    counts.to_csv(out_dir / "pairs_recount_p0d.csv", index=False, encoding="utf-8-sig")
    freq.to_csv(out_dir / "corp_pair_frequency.csv", index=False, encoding="utf-8-sig")
    top.to_csv(out_dir / "common_paragraphs_clean.csv", index=False, encoding="utf-8-sig")
    top3 = top_corps(freq, index, cfg.dir("raw"))

    write_report(cfg, cont, comp, paras, leak, counts, freq, top3,
                 inspect_term(records), out_dir)
    build_manual_review(cfg, records, out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
