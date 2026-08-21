"""Phase 2 파싱 결과 보고서 (7가지 요구 항목).

주의 — 성공률만 보고 통과 판정하지 않는다.
  Phase 0 에서 found_rate = 1.0 이면서 실제로는 표가 본문으로 새어 들어와
  S4 문단당 8.3자가 나온 전례가 있다. 그래서 아래를 함께 본다.
    3) 문단당 문자 수와 10자 미만 비중
    4) 잔재 3종 잔존 (직접 grep)
    5) 고아 캡션 비중
    6) 레이아웃 표 비중
  그리고 무작위 20건 수동 검수 HTML 을 함께 낸다.

실행
  python -m src.parse.parse_report
"""
from __future__ import annotations

import argparse
import json
import logging
import re
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.parse.run_parse import ARTIFACT_PATTERNS, _paths
from src.utils.config import PROJECT_ROOT, Config, load_config, set_seed
from src.utils.logging_utils import setup_logging

log = logging.getLogger("parse_report")

SECTIONS = ("S1", "S2", "S3", "S4")
DART_VIEWER = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"


def _md(df: pd.DataFrame) -> str:
    return df.to_markdown(index=False) + "\n" if not df.empty else "_(해당 없음)_\n"


def load_meta(cfg: Config) -> tuple[pd.DataFrame, pd.DataFrame]:
    dirs = _paths(cfg)
    meta = pd.read_parquet(dirs["base"] / "parse_meta.parquet")
    idx = pd.read_parquet(
        PROJECT_ROOT / cfg["phase1"]["paths"]["meta"] / "filings_index.parquet")
    m = meta.merge(idx[["rcept_no", "corp_code", "corp_name", "fy", "market"]],
                   on="rcept_no", how="left")
    return m, idx


# ---------------------------------------------------------------- 1~3

def section_success(m: pd.DataFrame) -> pd.DataFrame:
    """1) 섹션 추출 성공률 — 시작+종료 헤더를 **모두** 찾은 경우만 성공."""
    rows = []
    for s in SECTIONS:
        found = m[f"{s}_found"]
        eof = m[f"{s}_end_reason"] == "EOF"
        rows.append({
            "section": s, "n_docs": len(m),
            "found_rate": round(float(found.mean()), 4),
            "n_eof": int(eof.sum()),
            "n_not_found": int((~found).sum()),
        })
    return pd.DataFrame(rows)


def chars_by_year(m: pd.DataFrame) -> pd.DataFrame:
    """2) 섹션별 문자 수 분포 (연도별)."""
    rows = []
    for s in SECTIONS:
        g = m[m[f"{s}_found"]].groupby("fy")[f"{s}_chars"]
        for fy, v in g:
            rows.append({"section": s, "fy": int(fy), "n": len(v),
                         "mean": round(v.mean()), "median": round(v.median()),
                         "p10": round(v.quantile(.10)), "p90": round(v.quantile(.90))})
    return pd.DataFrame(rows)


def paragraph_stats(m: pd.DataFrame) -> pd.DataFrame:
    """3) 문단당 문자 수와 10자 미만 문단 비중 (0 이어야 한다)."""
    rows = []
    for s in SECTIONS:
        sub = m[m[f"{s}_found"] & (m[f"{s}_paras"] > 0)]
        if sub.empty:
            continue
        cpp = sub[f"{s}_chars"] / sub[f"{s}_paras"]
        rows.append({
            "section": s, "n_docs": len(sub),
            "chars_per_para_mean": round(cpp.mean(), 1),
            "chars_per_para_median": round(cpp.median(), 1),
            "n_under10_total": int(sub[f"{s}_under10"].sum()),
            "share_docs_with_under10": round(
                float((sub[f"{s}_under10"] > 0).mean()), 4),
        })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- 4

def grep_artifacts(cfg: Config, sample: int | None = None) -> pd.DataFrame:
    """4) 잔재 3종 잔존 — 저장된 파일을 직접 훑는다 (집계값을 믿지 않는다)."""
    dirs = _paths(cfg)
    rows = []
    files = sorted((dirs["sections"]).glob("*.json"))
    if sample:
        files = files[:sample]
    counts = {k: 0 for k in ARTIFACT_PATTERNS}
    docs_hit = {k: 0 for k in ARTIFACT_PATTERNS}
    raw_counts = {k: 0 for k in ARTIFACT_PATTERNS}
    for f in files:
        payload = json.loads(f.read_text(encoding="utf-8"))
        body = "\n".join(v.get("text", "") for v in payload.values())
        for k, pat in ARTIFACT_PATTERNS.items():
            n = len(pat.findall(body))
            counts[k] += n
            docs_hit[k] += int(n > 0)
        rf = dirs["raw_text"] / f.name
        if rf.exists():
            raw_body = "\n".join(
                v.get("text", "") for v in
                json.loads(rf.read_text(encoding="utf-8")).values())
            for k, pat in ARTIFACT_PATTERNS.items():
                raw_counts[k] += len(pat.findall(raw_body))
    for k in ARTIFACT_PATTERNS:
        rows.append({"잔재": k, "정제본 잔존 건수": counts[k],
                     "잔존 문서 수": docs_hit[k],
                     "raw_text 내 건수(제거 전)": raw_counts[k]})
    return pd.DataFrame(rows), len(files)


# ---------------------------------------------------------------- 5~6

def caption_and_layout(cfg: Config, m: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """5) 고아 캡션 비중, 6) 레이아웃 표 개수와 비중 (섹션별)."""
    dirs = _paths(cfg)
    rows = []
    for f in sorted(dirs["tables"].glob("*.json")):
        rcept = f.stem
        payload = json.loads(f.read_text(encoding="utf-8"))
        sec = json.loads((dirs["sections"] / f.name).read_text(encoding="utf-8"))
        for sid, tabs in payload.items():
            body_chars = len(sec.get(sid, {}).get("text", ""))
            paras = sec.get(sid, {}).get("paragraphs", [])
            caps = {t["caption"] for t in tabs if t.get("caption")}
            orphan = sum(len(p) for p in paras if p in caps)
            n_layout = sum(1 for t in tabs if t.get("is_layout"))
            layout_chars = sum(t.get("n_chars", 0) for t in tabs if t.get("is_layout"))
            table_chars = sum(t.get("n_chars", 0) for t in tabs)
            rows.append({
                "rcept_no": rcept, "section": sid,
                "n_tables": len(tabs), "n_layout": n_layout,
                "layout_chars": layout_chars, "table_chars": table_chars,
                "orphan_caption_chars": orphan, "body_chars": body_chars,
                "n_captioned": sum(1 for t in tabs if t.get("caption")),
            })
    d = pd.DataFrame(rows)
    if d.empty:
        return d, d

    cap = (d.groupby("section")
           .agg(문서수=("rcept_no", "nunique"),
                표=("n_tables", "sum"),
                캡션있는표=("n_captioned", "sum"),
                고아캡션문자=("orphan_caption_chars", "sum"),
                본문문자=("body_chars", "sum")).reset_index())
    cap["캡션보유율"] = (cap["캡션있는표"] / cap["표"]).round(4)
    cap["고아캡션_본문대비"] = (cap["고아캡션문자"] / cap["본문문자"]).round(5)

    lay = (d.groupby("section")
           .agg(표=("n_tables", "sum"), 레이아웃표=("n_layout", "sum"),
                레이아웃문자=("layout_chars", "sum"),
                표문자=("table_chars", "sum")).reset_index())
    lay["레이아웃표_비중"] = (lay["레이아웃표"] / lay["표"]).round(4)
    lay["레이아웃문자_비중"] = (lay["레이아웃문자"] / lay["표문자"]).round(4)
    return cap, lay


# ---------------------------------------------------------------- 7

def missing_top(m: pd.DataFrame, n: int = 30) -> pd.DataFrame:
    """7) 섹션 미발견 상위 N건 — 원인 추정 포함."""
    rows = []
    for s in SECTIONS:
        sub = m[~m[f"{s}_found"]]
        for r in sub.itertuples():
            reason = getattr(r, f"{s}_end_reason", "")
            chars = getattr(r, f"{s}_chars", 0)
            if reason == "시작 헤더 없음":
                why = "시작 헤더 미탐 — 서식 변형 또는 섹션 자체가 없음"
            elif reason == "EOF":
                why = "종료 헤더 미탐 — 문서 끝까지 흘러 강등"
            elif chars == 0:
                why = "본문 0자 — 내용이 전부 표"
            else:
                why = f"기타 ({reason})"
            rows.append({"section": s, "corp_name": r.corp_name, "fy": r.fy,
                         "rcept_no": r.rcept_no, "chars": chars,
                         "end_reason": reason, "원인 추정": why})
    d = pd.DataFrame(rows)
    return d.sort_values(["section", "fy"]).head(n) if not d.empty else d


# ---------------------------------------------------------------- 리포트

def write_report(cfg: Config, m: pd.DataFrame, out_dir: Path) -> Path:
    lines: list[str] = []
    add = lines.append
    add("# Phase 2 — 전량 파싱 결과\n")
    add(f"- 문서 {len(m):,}건, 회계연도 {int(m.fy.min())}~{int(m.fy.max())}\n")
    add("> **성공률만 보고 통과 판정하지 않는다.** Phase 0 에서 found_rate=1.0 "
        "이면서 표가 본문으로 새어 들어와 S4 문단당 8.3자가 나온 전례가 있다. "
        "3~6번을 함께 봐야 한다.\n")

    add("## 1. 섹션 추출 성공률\n")
    add("시작 헤더와 종료 헤더를 **모두** 찾은 경우만 성공으로 센다.\n")
    add(_md(section_success(m)))

    add("## 2. 섹션별 문자 수 분포 (연도별)\n")
    add(_md(chars_by_year(m)))

    add("## 3. 문단당 문자 수 · 10자 미만 문단\n")
    ps = paragraph_stats(m)
    add(_md(ps))
    tot_under10 = int(ps["n_under10_total"].sum()) if not ps.empty else 0
    add(f"- 10자 미만 문단 총 **{tot_under10}개** "
        f"({'0 이어야 정상 — 통과' if tot_under10 == 0 else '0 이 아니다 — 병합 규칙 확인 필요'})\n")

    art, n_files = grep_artifacts(cfg)
    add("## 4. 편집기 잔재 3종 잔존\n")
    add(f"저장된 섹션 JSON {n_files:,}건을 직접 훑었다 (집계값이 아니라 파일 내용).\n")
    add(_md(art))
    left = int(art["정제본 잔존 건수"].sum())
    add(f"- 정제본 잔존 **{left}건** "
        f"({'0 — 통과' if left == 0 else '0 이 아니다 — 제거 로직 확인 필요'})\n")

    cap, lay = caption_and_layout(cfg, m)
    add("## 5. 고아 캡션\n")
    add("표를 본문에서 떼어내면 '표 제목만 남은 문단' 이 본문에 고아로 남는다. "
        "`caption` 으로 표에 연결해 두었고, 아래는 그럼에도 본문에 그대로 남아 "
        "있는 문자 비중이다.\n")
    add(_md(cap))

    add("## 6. 레이아웃 표\n")
    add("1~2행 또는 1열이면서 셀이 200자 이상인 표. 자료를 담은 표가 아니라 "
        "서식용 껍데기이므로, 본문이 여기에 갇히면 텍스트가 손실된다.\n")
    add(_md(lay))
    if not lay.empty:
        for s in ("S2", "S3"):
            r = lay[lay["section"] == s]
            if not r.empty:
                r = r.iloc[0]
                add(f"- **{s}**: 표 {int(r['표']):,}개 중 레이아웃 "
                    f"{int(r['레이아웃표']):,}개 ({r['레이아웃표_비중']:.1%}), "
                    f"표 문자 기준 {r['레이아웃문자_비중']:.1%}")
        add("")

    add("## 7. 섹션 미발견 상위 30건\n")
    add(_md(missing_top(m)))

    path = out_dir / "parse_report.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    log.info("리포트 -> %s", path)
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Phase 2 파싱 보고서")
    ap.add_argument("--config", default=None)
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    out_dir = PROJECT_ROOT / "results" / "phase2"
    out_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(out_dir / "parse_report.log",
                  level=getattr(logging, args.log_level.upper()))
    set_seed(cfg)

    m, idx = load_meta(cfg)
    write_report(cfg, m, out_dir)

    from src.parse.parse_review import build_review
    build_review(cfg, m, out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
