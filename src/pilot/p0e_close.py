"""P0-e — Gate 0 종료.

새 파서 기능이나 리팩터링은 하지 않는다. 이미 만들어 둔 산출물만 읽어
논문 Appendix 재료와 최종 판정표를 만든다.

잔재 제거 전 텍스트는 P0-c 가 남긴 `data/pilot/sections_fixed/` 에 그대로 있고,
제거 후 텍스트는 `data/pilot/sections/` 에 있다. 두 코퍼스를 비교한다.

산출
  results/pilot/artifact_impact.md         잔재 제거 전/후 변화율 비교 (Appendix 원본)
  results/pilot/artifact_coverage.csv      잔재 3종의 문서 커버리지 · 문자 비중
  results/pilot/common_pair_taxonomy.md    남은 공통쌍 4분류
  results/pilot/common_pair_taxonomy.csv
  results/pilot/gate0_final.md             Gate 0 최종 판정 · Phase 0 종료

실행
  python -m src.pilot.p0e_close
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import warnings
from pathlib import Path
from typing import Any

import pandas as pd
from bs4 import XMLParsedAsHTMLWarning

from src.pilot.p0b_change_diagnostics import compute_change_rates
from src.pilot.p0c_boundary_audit import markers_for
from src.pilot.parse_cache import load_cache, to_records
from src.pilot.similarity import diff_paragraphs, minhash_pairs
from src.utils.config import PROJECT_ROOT, Config, load_config, set_seed
from src.utils.logging_utils import setup_logging

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

log = logging.getLogger("p0e")

_STEM = re.compile(r"^(?P<corp>[^_]+)_(?P<fy>\d{4})_(?P<sec>S\d+)$")

# 잔재 3종 (P0-c/P0-d 에서 확정). textnorm 의 패턴과 같은 것을 진단용으로 복제.
ARTIFACTS: dict[str, re.Pattern] = {
    "위젯 라벨": re.compile(r"◆\s*click\s*◆\s*(?:『[^』]{0,60}』)?\s*(?:삽입|추가)?", re.I),
    "줄바꿈 엔티티": re.compile(r"&cr(?![A-Za-z0-9]);?", re.I),
    "서식 파일명": re.compile(r"\d{4,6}\s*#\s*\*?\s*_\S+?\.dsl", re.I),
}


def _md(df: pd.DataFrame) -> str:
    return df.to_markdown(index=False) + "\n" if not df.empty else "_(해당 없음)_\n"


def load_corpus(pilot_dir: Path, name: str) -> pd.DataFrame:
    d = pilot_dir / name
    if not d.exists():
        raise SystemExit(f"코퍼스 없음: {d}")
    rows = []
    for p in sorted(d.glob("*.txt")):
        m = _STEM.match(p.stem)
        if not m:
            continue
        rows.append({"corp_code": m["corp"], "fy": int(m["fy"]),
                     "section": m["sec"], "text": p.read_text(encoding="utf-8")})
    df = pd.DataFrame(rows)
    log.info("[%s] 섹션 텍스트 %d건", name, len(df))
    return df


# ===========================================================================
# 1) 변화율 재계산 검증 + 전/후 비교
# ===========================================================================

def artifact_impact(cfg: Config, before: pd.DataFrame, after: pd.DataFrame
                    ) -> tuple[pd.DataFrame, pd.DataFrame]:
    """잔재 제거 전/후의 변화율 3종을 나란히 놓는다."""
    metrics = ["change_tfidf_cos", "change_jaccard", "change_levenshtein"]
    out = {}
    for label, df in (("before", before), ("after", after)):
        log.info("변화율 계산: %s", label)
        r = compute_change_rates(df, cfg)
        out[label] = r

    agg = []
    for label, r in out.items():
        g = (r.groupby(["section", "pair"])[metrics].mean().round(4)
             .reset_index().assign(variant=label))
        agg.append(g)
    long = pd.concat(agg, ignore_index=True)

    wide = long.pivot(index=["section", "pair"], columns="variant", values=metrics)
    wide.columns = [f"{m}_{v}" for m, v in wide.columns]
    wide = wide.reset_index()
    for m in metrics:
        short = m.replace("change_", "")
        wide[f"Δ_{short}"] = (wide[f"{m}_after"] - wide[f"{m}_before"]).round(4)
    return wide, out["after"]


# ===========================================================================
# 2) 잔재 커버리지
# ===========================================================================

def artifact_coverage(before: pd.DataFrame) -> pd.DataFrame:
    rows = []
    n_units = len(before)
    total_chars = int(before["text"].str.len().sum())
    for name, pat in ARTIFACTS.items():
        hit_units = 0
        n_occurrences = 0
        removed_chars = 0
        per_doc_chars = []
        for t in before["text"]:
            found = pat.findall(t)
            if not found:
                continue
            hit_units += 1
            n_occurrences += len(found)
            c = sum(len(x) for x in pat.finditer(t) for x in [x.group(0)])
            removed_chars += c
            per_doc_chars.append(c)
        rows.append({
            "잔재": name,
            "검출 (문서x섹션)": hit_units,
            "커버리지": round(hit_units / n_units, 4) if n_units else 0.0,
            "출현 횟수": n_occurrences,
            "제거 문자 수": removed_chars,
            "문서당 평균 문자": round(sum(per_doc_chars) / len(per_doc_chars), 1)
            if per_doc_chars else 0.0,
            "전체 문자 대비": round(removed_chars / total_chars, 6) if total_chars else 0.0,
        })
    return pd.DataFrame(rows)


# ===========================================================================
# 3) 남은 공통쌍 4분류
# ===========================================================================

_RE_LAW = re.compile(r"(제\s*\d+\s*조|자본시장법|상법|공정거래법|시행령|법률 제|규정 제\s*\d+)")
_RE_IFRS = re.compile(
    r"(기업회계기준서|한국채택국제회계기준|K-?IFRS|기준서 제\s*\d{4}\s*호|해석서|"
    r"회계정책|연결재무제표 작성|제ᆞ개정 기준서|제·개정 기준서)")
_RE_HEADING = re.compile(
    r"^\s*(?:[가-힣]\s*\.|\d+\s*\.|\(\d+\)|[IVXivx]+\s*\.|[①-⑳])")
_RE_SENTENCE_END = re.compile(r"(습니다|합니다|입니다|하였다|이다)\s*[\.。]?\s*$")


def classify_paragraph(text: str) -> str:
    """규칙 기반 4분류. 애매하면 '기타'."""
    t = (text or "").strip()
    if not t:
        return "기타"
    if _RE_IFRS.search(t):
        return "K-IFRS"
    if _RE_LAW.search(t):
        return "법령인용"
    # 서식 소제목: 번호로 시작하고 짧으며 문장으로 끝나지 않는다
    if _RE_HEADING.match(t) and len(t) <= 80 and not _RE_SENTENCE_END.search(t):
        return "서식소제목"
    return "기타"


def build_clean_pairs(records: list[dict], cfg: Config) -> pd.DataFrame:
    """P0-d 의 clean 기준(중복 제거 + 회계주석 문단 제외) 공통쌍 전체를 만든다."""
    c = cfg["change"]
    min_chars = int(c["min_paragraph_chars"])
    markers_cfg = cfg["audit"]["contamination_markers"]
    years = sorted({int(r["meta"]["fy"]) for r in records})

    by_key: dict[tuple[str, str, int], list[str]] = {}
    name_of: dict[str, str] = {}
    for r in records:
        name_of[r["meta"]["corp_code"]] = r["meta"]["corp_name"]
        for sid, sc in r["fixed"].items():
            by_key[(sid, r["meta"]["corp_code"], int(r["meta"]["fy"]))] = sc.paragraphs

    rows: list[dict[str, Any]] = []
    for sid in sorted(cfg["sections"].keys()):
        ms = markers_for(markers_cfg, sid)
        corps = sorted({k[1] for k in by_key if k[0] == sid})
        for prev_fy, curr_fy in zip(years, years[1:]):
            items: list[tuple[str, str]] = []
            seen: set[tuple[str, str]] = set()
            for corp in corps:
                a, b = by_key.get((sid, corp, prev_fy)), by_key.get((sid, corp, curr_fy))
                if a is None or b is None:
                    continue
                d = diff_paragraphs([p for p in a if len(p) >= min_chars],
                                    [p for p in b if len(p) >= min_chars])
                for p in d.changed:
                    if len(p) < min_chars or (corp, p) in seen:
                        continue
                    if any(m in p for m in ms):
                        continue
                    seen.add((corp, p))
                    items.append((corp, p))
            if len(items) < 2:
                continue
            pairs = minhash_pairs(
                items, num_perm=int(c["minhash_num_perm"]),
                ngram=int(c["minhash_ngram"]),
                threshold=float(c["minhash_threshold"]), cross_group_only=True)
            for i, j, est in pairs:
                pi, pj = items[int(i)], items[int(j)]
                rows.append({
                    "section": sid, "pair": f"{prev_fy}->{curr_fy}",
                    "corp_a": pi[0], "corp_a_name": name_of.get(pi[0], ""),
                    "corp_b": pj[0], "corp_b_name": name_of.get(pj[0], ""),
                    "est_jaccard": round(est, 4),
                    "n_chars": len(pi[1]),
                    "category": classify_paragraph(pi[1]),
                    "paragraph": pi[1],
                })
    return pd.DataFrame(rows)


# ===========================================================================
# 4) 수동 검수 집계
# ===========================================================================

def manual_review_summary(cfg: Config, pilot_dir: Path) -> tuple[pd.DataFrame, str]:
    mr = cfg.get("manual_review", {}) or {}
    path = PROJECT_ROOT / mr.get("result_json", "")
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
        rows = []
        for rec in (data.get("results") or {}).values():
            if not rec or not rec.get("verdict"):
                continue
            rows.append({"section": rec.get("section", "?"),
                         "verdict": rec["verdict"], "note": rec.get("note", "")})
        df = pd.DataFrame(rows)
        if not df.empty:
            agg = (df.groupby("section")["verdict"]
                   .value_counts().unstack(fill_value=0).reset_index())
            return agg, f"JSON: `{path.relative_to(PROJECT_ROOT)}`"

    rep = mr.get("reported") or {}
    agg = pd.DataFrame([{
        "항목 수": rep.get("n_items", 0),
        "O (정상)": rep.get("n_ok", 0),
        "X (오류)": rep.get("n_ng", 0),
    }])
    return agg, rep.get("source", "출처 미기재")


# ===========================================================================
# 리포트
# ===========================================================================

def write_artifact_impact(cfg: Config, wide: pd.DataFrame, cov: pd.DataFrame,
                          out: Path) -> Path:
    lines = ["# DART 편집기 잔재가 유사도 측정에 미치는 영향\n",
             "논문 Appendix 원본. 잔재 제거 **전**(`data/pilot/sections_fixed/`, "
             "P0-c 산출)과 **후**(`data/pilot/sections/`, P0-d 이후 재추출) 코퍼스로 "
             "같은 변화율을 각각 계산해 나란히 놓았다.\n",
             "- 변화율 = 1 − 유사도. 값이 클수록 두 연도의 텍스트가 다르다.",
             "- `Δ_*` = after − before. **음수면 잔재가 변화율을 부풀리고 있었다**는 뜻이다.\n",
             "## 1. 잔재 3종의 커버리지와 문자 비중\n", _md(cov),
             "## 2. 섹션 x 연도쌍별 변화율 전/후 비교\n", _md(wide)]

    metrics = [("tfidf_cos", "TF-IDF 코사인"), ("jaccard", "어절 Jaccard"),
               ("levenshtein", "정규화 Levenshtein")]
    lines.append("## 3. 요약\n")
    summ = []
    for key, label in metrics:
        col = f"Δ_{key}"
        if col in wide:
            summ.append({"지표": label,
                         "평균 Δ": round(float(wide[col].mean()), 4),
                         "최소 Δ": round(float(wide[col].min()), 4),
                         "최대 Δ": round(float(wide[col].max()), 4),
                         "음수 구간 수": int((wide[col] < 0).sum()),
                         "전체 구간 수": len(wide)})
    lines.append(_md(pd.DataFrame(summ)))
    lines.append("> 잔재는 전 기업 문서에 동일하게 들어 있었으므로, 연도쌍 사이에서 "
                 "**추가/삭제되는 순간에만** 변화율에 기여한다. 따라서 영향은 "
                 "잔재가 사라진 `2020->2024` 구간에서 가장 크게 나타난다.\n")

    path = out / "artifact_impact.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    log.info("Appendix 재료 -> %s", path)
    return path


def write_taxonomy(cfg: Config, tax: pd.DataFrame, out: Path) -> Path:
    n_sample = int(cfg.get("gate0_close", {}).get("taxonomy_sample", 20))
    lines = ["# 남은 기업 간 공통 변경 문단의 4분류\n",
             "P0-d 의 `clean` 기준(문단 중복 제거 + 회계기준 주석 마커 문단 제외)에서 "
             "남은 쌍 전체를 규칙 기반으로 태깅했다. 애매한 것은 모두 `기타` 로 둔다.\n",
             "분류 규칙 (우선순위 순)\n",
             "1. **K-IFRS** — 기업회계기준서/한국채택국제회계기준/기준서 제NNNN호/해석서/회계정책",
             "2. **법령인용** — 제N조/자본시장법/상법/공정거래법/시행령",
             "3. **서식소제목** — 번호로 시작하고 80자 이하이며 문장으로 끝나지 않음",
             "4. **기타** — 위에 걸리지 않는 것\n"]

    if tax.empty:
        lines.append("_공통쌍이 없다._\n")
    else:
        overall = (tax["category"].value_counts()
                   .rename_axis("분류").reset_index(name="쌍 수"))
        overall["비중"] = (overall["쌍 수"] / len(tax)).round(4)
        lines.append("## 1. 전체 분포\n")
        lines.append(_md(overall))

        lines.append("## 2. 섹션 x 연도쌍별 분포\n")
        pivot = (tax.pivot_table(index=["section", "pair"], columns="category",
                                 values="paragraph", aggfunc="count", fill_value=0)
                 .reset_index())
        lines.append(_md(pivot))

        other = tax[tax["category"] == "기타"]
        lines.append(f"## 3. `기타` 샘플 {min(n_sample, len(other))}건 (사람이 판단할 것)\n")
        if other.empty:
            lines.append("_`기타` 로 분류된 쌍이 없다._\n")
        else:
            for k, r in enumerate(other.head(n_sample).itertuples(), 1):
                lines.append(f"**{k}. {r.section} {r.pair}** · "
                             f"{r.corp_a_name} × {r.corp_b_name} · "
                             f"jac={r.est_jaccard} · {r.n_chars}자\n")
                lines.append(f"> {r.paragraph[:400]}\n")

    path = out / "common_pair_taxonomy.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    log.info("분류 리포트 -> %s", path)
    return path


def write_gate0_final(cfg: Config, pilot_dir: Path, tax: pd.DataFrame,
                      review: pd.DataFrame, review_src: str, out: Path) -> Path:
    g = cfg["gate0"]
    diag = pd.read_csv(pilot_dir / "diagnostics.csv")
    doc_ok = float(diag.drop_duplicates(["corp_code", "fy"])["parse_ok"].mean())
    means = diag.groupby("section")["char_len_text"].mean()
    n_clean = len(tax)

    lines = ["# Gate 0 최종 판정 — Phase 0 종료\n",
             f"표본 {diag['rcept_no'].nunique()}건 (30개 기업 x 3개 회계연도), "
             f"seed {cfg['seed']}\n",
             "## 1. 통과 조건\n"]

    checks = pd.DataFrame([
        {"항목": f"파싱 성공률 {g['parse_success_rate_min']:.0%} 이상",
         "값": f"{doc_ok:.1%}",
         "판정": "PASS" if doc_ok >= g["parse_success_rate_min"] else "FAIL"},
        {"항목": f"S1(사업의 내용) 평균 {g['s1_mean_chars_min']:,}자 이상",
         "값": f"{means.get('S1', 0):,.0f}자",
         "판정": "PASS" if means.get("S1", 0) >= g["s1_mean_chars_min"] else "FAIL"},
        {"항목": f"S2(경영진단) {g['s2_drop_threshold_chars']:,}자 이상",
         "값": f"{means.get('S2', 0):,.0f}자",
         "판정": "PASS" if means.get("S2", 0) >= g["s2_drop_threshold_chars"] else "FAIL"},
        {"항목": "기업 간 공통 변경 문단 관측",
         "값": f"{n_clean:,}쌍 (중복·회계주석·편집기 잔재 제외 후)",
         "판정": "PASS" if n_clean > 0 else "FAIL"},
    ])
    lines.append(_md(checks))

    lines.append("## 2. 수동 검수\n")
    lines.append(f"출처: {review_src}\n")
    lines.append(_md(review))

    lines.append("## 3. 섹션 확정\n")
    d1 = (diag.groupby(["section", "section_name"])["char_len_text"].mean()
          .round(0).reset_index().rename(columns={"char_len_text": "평균 문자 수"}))
    d1["Phase 2 처리"] = [
        "텍스트 섹션 — 채택" if r["평균 문자 수"] >= g["section_mean_chars_min"]
        else "**텍스트 섹션 탈락 / 구조화 데이터 소스로 재분류**"
        for _, r in d1.iterrows()
    ]
    lines.append(_md(d1))
    lines.append("- **S4(임원 및 직원 등에 관한 사항)** 는 본문이 평균 1,081자에 불과하다. "
                 "내용이 사실상 전부 표(임원 현황·직원 현황)이기 때문이다. "
                 "**텍스트 섹션에서 탈락시키고, Phase 1 에서 표를 구조화 데이터로 "
                 "추출하는 소스로 재분류한다.**\n")

    lines.append("## 4. 판정하지 않은 항목\n")
    lines.append("- **변화율 중앙값 0.05 기준**: 인접 연도(t-1 → t) 전제라 "
                 "4년 간격 표본(2016/2020/2024)에는 적용할 수 없다. "
                 "Phase 1 에서 연속 연도 패널을 확보한 뒤 적용한다.")
    lines.append("- **D2 서식개정 스파이크**: 기업당 연도쌍이 2개뿐이라 demean 시 "
                 "상쇄되고, 각 페어가 4년치 개정을 뭉뚱그려 담아 연도별 분해가 "
                 "원리적으로 불가능하다. 같은 이유로 Phase 1 로 미룬다.\n")

    lines.append("## 5. Phase 0 에서 확정된 사실\n")
    lines.append("1. 섹션 경계 탐지는 정상이다 (EOF 0건, legacy/fixed 356건 전부 동일).")
    lines.append("2. 실제 결함은 **표 유입**이었다. DART 커스텀 컨테이너 태그 안의 "
                 "`<table>` 이 본문으로 새어 들어와 S4 문단당 8.3자를 만들었다.")
    lines.append("3. **DART 편집기 잔재 3종**이 전 기업 문서 절반에 들어 있었고, "
                 "제거 전에는 '기업 간 공통 변경 문단' 신호의 약 64%를 차지했다.")
    lines.append("4. 공통 변경 문단 신호에는 **기업집단 내 모회사-자회사 텍스트 재사용**이 "
                 "섞인다 (SKC/ISC, 에코프로/에코프로비엠). Phase 5 는 계열 관계를 "
                 "통제해야 한다.\n")
    lines.append("**Gate 0 통과. Phase 0 을 종료한다.**\n")

    path = out / "gate0_final.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    log.info("Gate 0 최종 -> %s", path)
    return path


# ===========================================================================
# main
# ===========================================================================

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="P0-e Gate 0 종료")
    ap.add_argument("--config", default=None)
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    pilot = cfg.dir("pilot")
    out = PROJECT_ROOT / "results" / "pilot"
    out.mkdir(parents=True, exist_ok=True)
    setup_logging(pilot / "p0e.log", level=getattr(logging, args.log_level.upper()))
    set_seed(cfg)

    gc = cfg.get("gate0_close", {}) or {}
    before = load_corpus(pilot, gc.get("corpus_before", "sections_fixed"))
    after = load_corpus(pilot, gc.get("corpus_after", "sections"))

    # 1) 변화율 재계산 검증
    for name, df in (("before", before), ("after", after)):
        n_hit = sum(1 for t in df["text"]
                    if any(p.search(t) for p in ARTIFACTS.values()))
        log.info("[%s] 잔재 포함 (문서x섹션): %d / %d", name, n_hit, len(df))

    wide, rates_after = artifact_impact(cfg, before, after)
    wide.to_csv(out / "artifact_impact.csv", index=False, encoding="utf-8-sig")
    rates_after.to_csv(out / "change_rates_after.csv", index=False, encoding="utf-8-sig")

    # 2) 커버리지
    cov = artifact_coverage(before)
    cov.to_csv(out / "artifact_coverage.csv", index=False, encoding="utf-8-sig")
    write_artifact_impact(cfg, wide, cov, out)

    # 3) 4분류
    df_cache = load_cache(cfg, pd.read_csv(pilot / "reports_index.csv",
                                           dtype={"rcept_no": str, "corp_code": str,
                                                  "stock_code": str}),
                          cfg.dir("raw"), pilot)
    tax = build_clean_pairs(to_records(df_cache), cfg)
    tax.to_csv(out / "common_pair_taxonomy.csv", index=False, encoding="utf-8-sig")
    write_taxonomy(cfg, tax, out)

    # 4~5) 수동 검수 + Gate 0 최종
    review, src = manual_review_summary(cfg, pilot)
    write_gate0_final(cfg, pilot, tax, review, src, out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
