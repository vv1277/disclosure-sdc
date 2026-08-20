"""프롬프트 P0 — 사전 타당성 진단 스크립트.

30개 기업 x 3개 회계연도(2016/2020/2024)의 사업보고서를 받아
4개 섹션의 텍스트 볼륨과 파싱 성공률을 측정한다.

산출
  data/pilot/diagnostics.csv
      corp_code, corp_name, fy, section, char_len_text,
      char_len_table, n_paragraphs, parse_ok  (+ 진단에 필요한 부가 컬럼)
  data/pilot/report.md
  data/pilot/failures.csv
  data/pilot/sections/{corp_code}_{fy}_{section}.txt      (본문)
  data/pilot/tables/{corp_code}_{fy}_{section}.html       (표)
  data/pilot/reports_index.csv                            (rcept_no 매핑)

실행
  python -m src.pilot.p0_diagnostics --mock          # API 키 없이 파이프라인 점검
  python -m src.pilot.p0_diagnostics --limit 3       # 실데이터 소규모 시운전
  python -m src.pilot.p0_diagnostics                 # 전체 30개
  python -m src.pilot.p0_diagnostics --reuse-index   # 캐시만으로 재파싱 (API 0회)

TODO(API): OPENDART_KEY 가 없으면 --mock 로만 실행된다.
"""
from __future__ import annotations

import argparse
import logging
import sys
import warnings
from pathlib import Path
from typing import Any

import pandas as pd
from tqdm import tqdm

from src.collect.corp_code import build_mapping, lookup
from src.collect.dart_client import DartClient, MissingApiKey
from src.collect.report_select import search_window, select_annual_report
from src.parse.body import pick_body_file
from src.parse.sections import extract_sections, parse_ok
from src.pilot.mock_source import MockDartClient, build_mock_mapping
from src.utils.config import Config, load_config, set_seed
from src.utils.failures import FailureLog
from src.utils.logging_utils import setup_logging

from bs4 import XMLParsedAsHTMLWarning

# DART 원본은 XML 이지만 커스텀 태그 보존을 위해 html.parser 로 읽는다.
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

log = logging.getLogger("p0")

DIAG_COLUMNS = [
    "corp_code", "corp_name", "stock_code", "market", "size_tier",
    "fy", "rcept_no", "is_amendment", "section", "section_name",
    "found", "end_header", "end_reason",
    "char_len_text", "char_len_table", "n_paragraphs", "parse_ok",
]


# ---------------------------------------------------------------------------
# 수집
# ---------------------------------------------------------------------------

def resolve_mapping(cfg: Config, client, *, mock: bool, out_dir: Path) -> pd.DataFrame:
    """작업 1) 고유번호-종목코드-기업명 매핑 테이블."""
    if mock:
        mapping = build_mock_mapping(cfg)
        mapping.to_parquet(out_dir / "corp_code_map.parquet", index=False)
        log.info("[mock] corp_code 매핑 %d건", len(mapping))
        return mapping
    zip_path = client.fetch_corp_code_zip()
    return build_mapping(zip_path, out_dir / "corp_code_map.parquet")


def resolve_reports(cfg: Config, client, mapping: pd.DataFrame,
                    universe: list[dict], fails: FailureLog) -> pd.DataFrame:
    """작업 2) (기업, 회계연도) -> rcept_no."""
    sample = cfg["sample"]
    rows: list[dict[str, Any]] = []

    for u in tqdm(universe, desc="공시검색", unit="corp"):
        hit = lookup(mapping, u["stock_code"])
        if hit is None:
            fails.add(stage="lookup", key=u["stock_code"],
                      reason="corp_code 매핑 없음", detail=u["name"])
            continue
        for fy in sample["fiscal_years"]:
            bgn, end = search_window(fy)
            try:
                found = client.search_reports(hit["corp_code"], bgn, end)
            except Exception as exc:
                fails.add(stage="search", key=f"{u['stock_code']}/{fy}",
                          reason=type(exc).__name__, detail=str(exc))
                continue
            chosen = select_annual_report(
                found, fy,
                include=sample["report_name_include"],
                exclude=sample["report_name_exclude"],
                prefer_original=sample["prefer_original_over_amendment"],
            )
            if chosen is None:
                fails.add(stage="select", key=f"{u['stock_code']}/{fy}",
                          reason="해당 연도 사업보고서 없음", detail=u["name"])
                continue
            rows.append({
                "corp_code": hit["corp_code"],
                "corp_name": hit["corp_name"],
                "stock_code": u["stock_code"],
                "market": u["market"],
                "size_tier": u["size_tier"],
                "fy": fy,
                "rcept_no": chosen["rcept_no"],
                "report_nm": chosen.get("report_nm", ""),
                "rcept_dt": chosen.get("rcept_dt", ""),
                "is_amendment": bool(chosen.get("is_amendment", False)),
                "n_amendments": int(chosen.get("n_amendments", 0)),
            })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 파싱
# ---------------------------------------------------------------------------

def parse_documents(cfg: Config, client, reports: pd.DataFrame,
                    out_dir: Path, fails: FailureLog) -> pd.DataFrame:
    """작업 3~5) 원본 ZIP -> 본문 -> 섹션 -> 표/텍스트 분리 -> 파일 저장."""
    sec_dir = out_dir / "sections"
    tab_dir = out_dir / "tables"
    sec_dir.mkdir(parents=True, exist_ok=True)
    tab_dir.mkdir(parents=True, exist_ok=True)
    spec = cfg["sections"]
    pcfg = cfg.get("parse", {}) or {}

    rows: list[dict[str, Any]] = []
    for r in tqdm(list(reports.itertuples(index=False)), desc="파싱", unit="doc"):
        key = f"{r.stock_code}/{r.fy}"
        try:
            zip_path = client.download_document(r.rcept_no)
        except Exception as exc:
            fails.add(stage="download", key=key, reason=type(exc).__name__, detail=str(exc))
            rows.extend(_empty_rows(r, spec))
            continue
        try:
            body_name, html = pick_body_file(Path(zip_path))
        except Exception as exc:
            fails.add(stage="body", key=key, reason=type(exc).__name__, detail=str(exc))
            rows.extend(_empty_rows(r, spec))
            continue
        try:
            sections = extract_sections(
                html, spec, parser=pcfg.get("parser", "html.parser"),
                require_terminator=bool(pcfg.get("require_terminator", True)),
                merge_min_chars=int(pcfg.get("merge_min_chars", 10)),
            )
        except Exception as exc:
            fails.add(stage="section", key=key, reason=type(exc).__name__, detail=str(exc))
            rows.extend(_empty_rows(r, spec))
            continue

        doc_ok = parse_ok(sections.values(), min_found=1)
        if not doc_ok:
            fails.add(stage="section", key=key, reason="섹션 0개 추출",
                      detail=f"body={body_name}")

        for sid, sc in sections.items():
            stem = f"{r.corp_code}_{r.fy}_{sid}"
            if sc.found and sc.text:
                (sec_dir / f"{stem}.txt").write_text(sc.text, encoding="utf-8")
            if sc.tables_html:
                (tab_dir / f"{stem}.html").write_text(
                    "\n".join(sc.tables_html), encoding="utf-8"
                )
            rows.append({
                "corp_code": r.corp_code, "corp_name": r.corp_name,
                "stock_code": r.stock_code, "market": r.market,
                "size_tier": r.size_tier, "fy": r.fy, "rcept_no": r.rcept_no,
                "is_amendment": r.is_amendment,
                "section": sid, "section_name": sc.name,
                "found": sc.found,
                "end_header": sc.end_header or sc.end_reason,
                "end_reason": sc.end_reason,
                "char_len_text": sc.char_len_text,
                "char_len_table": sc.char_len_table,
                "n_paragraphs": sc.n_paragraphs,
                "parse_ok": doc_ok,
            })
    return pd.DataFrame(rows, columns=DIAG_COLUMNS)


def _empty_rows(r, spec: dict) -> list[dict[str, Any]]:
    return [{
        "corp_code": r.corp_code, "corp_name": r.corp_name,
        "stock_code": r.stock_code, "market": r.market, "size_tier": r.size_tier,
        "fy": r.fy, "rcept_no": r.rcept_no, "is_amendment": r.is_amendment,
        "section": sid, "section_name": s["name"], "found": False,
        "end_header": "", "end_reason": "추출 실패",
        "char_len_text": 0, "char_len_table": 0, "n_paragraphs": 0,
        "parse_ok": False,
    } for sid, s in spec.items()]


# ---------------------------------------------------------------------------
# 리포트
# ---------------------------------------------------------------------------

def _md_table(df: pd.DataFrame) -> str:
    if df.empty:
        return "_(데이터 없음)_\n"
    return df.to_markdown(index=False) + "\n"


def _common_paragraph_check(out_dir: Path) -> tuple[str, bool | None, str]:
    """Gate 0 의 '기업 간 공통 변경 문단' 칸을 실제 값으로 채운다 (P0-d Part D-11).

    P0-d 의 clean 기준(문단 중복 제거 + 회계기준 주석 문단 제외)을 1순위로 쓰고,
    없으면 P0-b 값으로 물러난다. 둘 다 없으면 '미측정'을 명시한다.
    """
    p0d = out_dir / "pairs_recount_p0d.csv"
    if p0d.exists():
        n = int(pd.read_csv(p0d)["n_common_pairs_clean"].sum())
        return ("기업 간 공통 변경 문단 관측", n > 0,
                f"{n:,}쌍 (중복·회계주석 문단 제외 후)")
    p0b = out_dir / "common_paragraph_counts.csv"
    if p0b.exists():
        n = int(pd.read_csv(p0b)["n_common_pairs"].sum())
        return ("기업 간 공통 변경 문단 관측", n > 0, f"{n:,}쌍 (P0-b, 중복 제거 전)")
    return ("기업 간 공통 변경 문단 관측", None,
            "미측정 — `python -m src.pilot.p0d_report` 를 먼저 실행하세요")


def write_report(cfg: Config, diag: pd.DataFrame, reports: pd.DataFrame,
                 fails: FailureLog, out_dir: Path, *, mock: bool) -> Path:
    """작업 7) data/pilot/report.md"""
    g = cfg["gate0"]
    lines: list[str] = []
    add = lines.append

    add("# Phase 0 진단 리포트 (P0)\n")
    if mock:
        add("> **경고 — MOCK 실행입니다.** 합성 데이터로 파이프라인만 점검한 결과이며,\n"
            "> Gate 0 판정 근거로 쓸 수 없습니다. OPENDART_KEY 발급 후 재실행하세요.\n")
    add(f"- 설정 파일: `{cfg.path.name}` / seed = {cfg['seed']}")
    add(f"- 표본: {reports['stock_code'].nunique() if not reports.empty else 0}개 기업 "
        f"x {len(cfg['sample']['fiscal_years'])}개 연도 "
        f"= 문서 {len(reports)}건")
    add(f"- 대상 섹션: " + ", ".join(f"{k}({v['name']})" for k, v in cfg["sections"].items()))
    add("")

    if diag.empty:
        add("수집된 문서가 없습니다. 실패 로그를 확인하세요.\n")
        path = out_dir / "report.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    # --- 1) 섹션별 x 연도별 문자 수
    add("## 1. 섹션별 · 연도별 문자 수 (D1)\n")
    pivot = (
        diag.groupby(["section", "section_name", "fy"])["char_len_text"]
        .agg(mean="mean", median="median", n="size")
        .round(0).reset_index()
    )
    add(_md_table(pivot))

    add("### 섹션 전체 요약\n")
    add("`found_rate` 는 **시작 헤더와 종료 헤더를 모두 찾은** 경우만 성공으로 센다. "
        "종료 헤더를 못 찾으면(EOF) 섹션이 문서 끝까지 흘렀을 수 있으므로 실패로 "
        "강등한다 (`config.yaml` 의 `parse.require_terminator`).\n")
    summary = (
        diag.groupby(["section", "section_name"])
        .agg(
            n_docs=("char_len_text", "size"),
            found_rate=("found", "mean"),
            n_eof=("end_reason", lambda x: int((x == "EOF").sum())),
            mean_chars=("char_len_text", "mean"),
            median_chars=("char_len_text", "median"),
            mean_table_chars=("char_len_table", "mean"),
            mean_paragraphs=("n_paragraphs", "mean"),
        ).round(1).reset_index()
    )
    summary["D1_통과(평균2000자)"] = summary["mean_chars"] >= g["section_mean_chars_min"]
    add(_md_table(summary))

    # --- 2) 파싱 성공률
    add("## 2. 파싱 성공률 (D3)\n")
    doc_level = diag.drop_duplicates(subset=["corp_code", "fy"])
    overall = float(doc_level["parse_ok"].mean()) if len(doc_level) else 0.0
    add(f"- **전체 문서 파싱 성공률: {overall:.1%}** "
        f"(기준 {g['parse_success_rate_min']:.0%}) → "
        f"{'통과' if overall >= g['parse_success_rate_min'] else '미달'}")
    if overall < g["parse_rate_reset_period"]:
        add(f"- 성공률이 {g['parse_rate_reset_period']:.0%} 미만입니다. "
            f"표본 시작연도를 2018년으로 올리는 안을 검토하세요.")
    add("")
    sec_rate = (
        diag.groupby(["section", "section_name"])["found"].mean().round(3)
        .reset_index(name="section_found_rate")
    )
    add(_md_table(sec_rate))

    year_rate = (
        doc_level.groupby("fy")["parse_ok"].agg(["mean", "size"]).round(3)
        .reset_index().rename(columns={"mean": "parse_ok_rate", "size": "n_docs"})
    )
    add(_md_table(year_rate))

    # --- 3) 실패 사례
    add("## 3. 실패 사례 상위 10건\n")
    fdf = fails.to_frame()
    if fdf.empty:
        add("_실패 없음_\n")
    else:
        add(_md_table(fdf.head(10)))
        add("### 원인별 집계\n")
        add(_md_table(fails.top_reasons(10)))

    # --- 4) Gate 0 체크리스트
    add("## 4. Gate 0 체크리스트\n")
    s1_mean = float(diag.loc[diag["section"] == "S1", "char_len_text"].mean() or 0)
    s2_mean = float(diag.loc[diag["section"] == "S2", "char_len_text"].mean() or 0)
    checks = [
        (f"파싱 성공률 {g['parse_success_rate_min']:.0%} 이상",
         overall >= g["parse_success_rate_min"], f"{overall:.1%}"),
        (f"S1(사업의 내용) 평균 {g['s1_mean_chars_min']:,}자 이상",
         s1_mean >= g["s1_mean_chars_min"], f"{s1_mean:,.0f}자"),
        (f"S2(경영진단) {g['s2_drop_threshold_chars']:,}자 이상 (미만이면 MVP 제외)",
         s2_mean >= g["s2_drop_threshold_chars"], f"{s2_mean:,.0f}자"),
        _common_paragraph_check(out_dir),
    ]
    add(_md_table(pd.DataFrame(
        [{"항목": c[0],
          "결과": "—" if c[1] is None else ("PASS" if c[1] else "FAIL"),
          "값": c[2]} for c in checks]
    )))

    add("## 5. 다음 단계\n")
    if s2_mean < g["s2_drop_threshold_chars"]:
        add("- S2(이사의 경영진단 및 분석의견)를 MVP 섹션에서 **제외**하고, "
            "S1 + S3 2개 섹션으로 축소하는 안을 확정하세요.")
    drop = summary.loc[~summary["D1_통과(평균2000자)"], "section"].tolist()
    if drop:
        add(f"- D1 미달 섹션: {', '.join(drop)} → Phase 2 섹션 목록에서 제외 검토")
    else:
        add("- 4개 섹션 모두 D1 기준을 충족합니다. Phase 2 섹션 목록을 그대로 확정하세요.")
    add("- `python -m src.pilot.p0b_change_diagnostics` 를 실행해 D2(스파이크)를 판정하세요.")
    add("")

    path = out_dir / "report.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    log.info("리포트 -> %s", path)
    return path


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

class CachedOnlyClient:
    """data/raw 캐시만 읽는 클라이언트. --reuse-index 전용, 네트워크를 쓰지 않는다."""

    def __init__(self, cfg: Config, *, mock: bool = False):
        self.raw_dir = cfg.dir("raw") / ("mock" if mock else "")
        self.n_calls = 0

    def download_document(self, rcept_no: str) -> Path:
        dest = self.raw_dir / f"{rcept_no}.zip"
        if not dest.exists():
            raise FileNotFoundError(f"원본 ZIP 캐시 없음: {dest.name}")
        return dest


def build_client(cfg: Config, *, mock: bool, seed: int):
    if mock:
        log.warning("MOCK 모드: 합성 공시로 실행합니다. 결과는 Gate 0 판정에 쓸 수 없습니다.")
        return MockDartClient(cfg, seed)
    try:
        return DartClient(cfg)
    except MissingApiKey as exc:
        log.error("%s", exc)
        sys.exit(2)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Phase 0 사전 진단 (P0)")
    ap.add_argument("--config", default=None, help="config.yaml 경로")
    ap.add_argument("--limit", type=int, default=None,
                    help="표본 기업 수 제한 (소규모 시운전용)")
    ap.add_argument("--mock", action="store_true",
                    help="API 키 없이 합성 공시로 파이프라인 점검")
    ap.add_argument("--reuse-index", action="store_true",
                    help="기존 reports_index.csv 와 data/raw 캐시만 사용해 파싱만 다시 한다 "
                         "(API 호출 0회. 파서를 고친 뒤 재추출할 때 쓴다)")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    out_dir = cfg.dir("pilot", mock=args.mock)
    setup_logging(out_dir / "p0.log", level=getattr(logging, args.log_level.upper()))
    seed = set_seed(cfg)
    log.info("출력 디렉토리: %s", out_dir)

    universe = list(cfg["universe"])
    if args.limit:
        universe = universe[: args.limit]
    log.info("표본 %d개 기업 x %s", len(universe), cfg["sample"]["fiscal_years"])

    fails = FailureLog(out_dir / "failures.csv")

    idx_path = out_dir / "reports_index.csv"
    if args.reuse_index:
        if not idx_path.exists():
            raise SystemExit(f"--reuse-index 인데 {idx_path} 가 없습니다.")
        reports = pd.read_csv(idx_path, dtype={"stock_code": str, "rcept_no": str,
                                               "corp_code": str})
        keep = {u["stock_code"] for u in universe}
        reports = reports[reports["stock_code"].isin(keep)]
        client = CachedOnlyClient(cfg, mock=args.mock)
        log.info("기존 인덱스 재사용: %d건 (API 호출 없음)", len(reports))
    else:
        client = build_client(cfg, mock=args.mock, seed=seed)
        mapping = resolve_mapping(cfg, client, mock=args.mock, out_dir=out_dir)
        reports = resolve_reports(cfg, client, mapping, universe, fails)
        reports.to_csv(idx_path, index=False, encoding="utf-8-sig")
    log.info("공시 %d건 확정 (정정본 사용 %d건)",
             len(reports), int(reports["is_amendment"].sum()) if not reports.empty else 0)

    diag = parse_documents(cfg, client, reports, out_dir, fails)
    diag.to_csv(out_dir / "diagnostics.csv", index=False, encoding="utf-8-sig")
    log.info("diagnostics.csv -> %d행", len(diag))

    fails.save()
    write_report(cfg, diag, reports, fails, out_dir, mock=args.mock)
    log.info("API 호출 %d회", getattr(client, "n_calls", -1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
