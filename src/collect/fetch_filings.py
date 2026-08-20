"""사업보고서 인덱스 구축 (Phase 1 [A] 1단계) 과 원문 다운로드 (3단계).

수집 순서 — 한 번에 전량 받지 않는다
  1) `--index`  : 공시검색 API 만 써서 filings_index 를 만든다. 원문은 받지 않는다.
  2) 보고서 검토 : 유니버스가 잘못 짜여 있으면 7,200건을 다시 받아야 한다.
                  인덱스는 싸고 원문은 비싸다.
  3) `--download`: 확인 후 원문을 받는다. 연도 역순(2024 -> 2015)으로 받는다.
                  최근 연도가 파싱 품질이 좋아 문제를 일찍 발견한다.

호출 절약
  기업당 1회 검색으로 전체 기간을 한 번에 훑는다 (연도별로 나눠 부르면
  기업 수 x 연도 수 만큼 호출이 늘어난다). 회계연도는 report_nm 의 (YYYY.MM)
  으로 로컬에서 가른다. 12월 결산 여부도 이 값으로 판정하므로
  기업개황 API 를 따로 부르지 않는다.

실행
  python -m src.collect.fetch_filings --index
  python -m src.collect.fetch_filings --index --limit 30      # 소규모 시운전
  python -m src.collect.fetch_filings --download              # 확인 후
  python -m src.collect.fetch_filings --download --max-docs 100
"""
from __future__ import annotations

import argparse
import logging
import re
from pathlib import Path
from typing import Any

import pandas as pd
from tqdm import tqdm

from src.collect.corp_code import build_mapping, parse_corp_code_zip
from src.collect.dart_client import DartClient, MissingApiKey, NoData
from src.collect.quota import QuotaExceeded
from src.collect.report_select import is_amendment, period_year
from src.collect.universe import attach_corp_code, build_universe
from src.utils.config import PROJECT_ROOT, Config, load_config, set_seed
from src.utils.failures import FailureLog
from src.utils.logging_utils import setup_logging

log = logging.getLogger("filings")

_PERIOD = re.compile(r"\((\d{4})\.(\d{1,2})\)")
_TIME = re.compile(r"^\d{2}:?\d{2}$")

INDEX_COLUMNS = [
    "corp_code", "stock_code", "corp_name", "market", "fy", "rcept_no",
    "report_nm", "rcept_dt", "rcept_time", "is_correction",
    "is_correction_exists", "orig_rcept_no", "flr_nm", "period_month",
]


# ===========================================================================
# 1단계 — 인덱스
# ===========================================================================

def _annual_rows(rows: list[dict[str, Any]], years: set[int],
                 december_only: bool) -> list[dict[str, Any]]:
    """공시검색 결과에서 대상 회계연도의 사업보고서만 남긴다."""
    out = []
    for r in rows:
        nm = r.get("report_nm", "") or ""
        if "사업보고서" not in nm:
            continue
        stripped = re.sub(r"\[[^\]]*\]", "", nm)
        if any(tok in stripped for tok in ("분기", "반기")):
            continue
        m = _PERIOD.search(nm)
        if not m:
            continue
        fy, month = int(m.group(1)), int(m.group(2))
        if fy not in years:
            continue
        if december_only and month != 12:
            continue
        out.append(dict(r, fy=fy, period_month=month,
                        is_correction=is_amendment(nm)))
    return out


def _pick_original(cands: list[dict[str, Any]]) -> dict[str, Any]:
    """원본 우선. 정정본은 플래그만 남기고 텍스트를 소급 사용하지 않는다."""
    originals = [c for c in cands if not c["is_correction"]]
    pool = originals or cands
    pool = sorted(pool, key=lambda c: str(c.get("rcept_dt", "")))
    chosen = dict(pool[0])
    chosen["is_correction_exists"] = any(c["is_correction"] for c in cands)
    chosen["orig_rcept_no"] = "" if originals else chosen["rcept_no"]
    return chosen


def build_index(cfg: Config, universe: pd.DataFrame, client: DartClient,
                fails: FailureLog) -> pd.DataFrame:
    p1 = cfg["phase1"]
    years = set(int(y) for y in p1["fiscal_years"])
    dec_only = bool(p1.get("december_fiscal_only", True))
    bgn = f"{min(years) + 1}0101"
    end = f"{max(years) + 2}0630"

    corps = (universe.dropna(subset=["corp_code"])
             .drop_duplicates("corp_code")[
                 ["corp_code", "stock_code", "corp_name", "market"]])
    log.info("공시검색 대상 고유 기업 %d개 (기간 %s~%s)", len(corps), bgn, end)

    rows: list[dict[str, Any]] = []
    for c in tqdm(list(corps.itertuples(index=False)), desc="공시검색", unit="corp"):
        try:
            found = client.search_reports(c.corp_code, bgn, end)
        except QuotaExceeded:
            log.error("일일 한도 소진 — 여기까지 저장하고 중단합니다. "
                      "내일 같은 명령을 다시 실행하면 이어집니다.")
            break
        except NoData:
            found = []
        except Exception as exc:
            fails.add(stage="search", key=c.corp_code,
                      reason=type(exc).__name__, detail=str(exc))
            continue

        annual = _annual_rows(found, years, dec_only)
        by_fy: dict[int, list[dict[str, Any]]] = {}
        for a in annual:
            by_fy.setdefault(a["fy"], []).append(a)
        for fy, cands in by_fy.items():
            chosen = _pick_original(cands)
            rows.append({
                "corp_code": c.corp_code, "stock_code": c.stock_code,
                "corp_name": c.corp_name, "market": c.market, "fy": fy,
                "rcept_no": chosen["rcept_no"],
                "report_nm": chosen.get("report_nm", ""),
                "rcept_dt": chosen.get("rcept_dt", ""),
                "rcept_time": str(chosen.get("rcept_tm", "") or ""),
                "is_correction": bool(chosen["is_correction"]),
                "is_correction_exists": bool(chosen["is_correction_exists"]),
                "orig_rcept_no": chosen.get("orig_rcept_no", ""),
                "flr_nm": chosen.get("flr_nm", ""),
                "period_month": chosen["period_month"],
            })

    idx = pd.DataFrame(rows, columns=INDEX_COLUMNS)
    out = PROJECT_ROOT / p1["paths"]["meta"] / "filings_index.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    idx.to_parquet(out, index=False)
    log.info("filings_index 저장 -> %s (%d행)", out, len(idx))
    return idx


# ===========================================================================
# 2단계 — 보고
# ===========================================================================

def index_report(cfg: Config, universe: pd.DataFrame, idx: pd.DataFrame,
                 client: DartClient) -> Path:
    p1 = cfg["phase1"]
    lines: list[str] = []
    add = lines.append

    add("# Phase 1 — filings_index 검토용 보고\n")
    add("원문 다운로드 전에 유니버스가 제대로 짜였는지 확인한다. "
        "인덱스는 싸고 원문은 비싸다.\n")

    # 1) 연도별 대상 기업 수와 확보율
    add("## 1. 연도별 대상 기업 수 · 사업보고서 확보율\n")
    uni_n = universe.groupby("fy")["stock_code"].nunique().rename("유니버스 기업 수")
    got = idx.groupby("fy")["corp_code"].nunique().rename("사업보고서 확보")
    t1 = pd.concat([uni_n, got], axis=1).fillna(0).astype(int).reset_index()
    t1["확보율"] = (t1["사업보고서 확보"] / t1["유니버스 기업 수"]).round(4)
    add(t1.to_markdown(index=False) + "\n")

    # 2) 상장폐지 예정 기업 수 — 0 이면 유니버스 구성 버그
    add("## 2. 연도별 '이후 상장폐지' 기업 수 (생존편향 점검)\n")
    if "is_delisted_later" in universe:
        t2 = (universe.groupby("fy")["is_delisted_later"]
              .agg(상폐예정="sum", 전체="size").reset_index())
        t2["비율"] = (t2["상폐예정"] / t2["전체"]).round(4)
        add(t2.to_markdown(index=False) + "\n")
        zero_years = t2[(t2["상폐예정"] == 0) & (t2["fy"] < t2["fy"].max())]["fy"].tolist()
        if zero_years:
            add(f"> **경고 — 유니버스 구성 버그 의심.** {zero_years} 연도에 "
                f"'이후 상장폐지' 기업이 0건이다. 그 시점 스냅샷이 아니라 현재 "
                f"상장 목록으로 표본을 만들었을 때 나타나는 증상이다.\n")
        else:
            add("> 모든 연도에 상장폐지 예정 기업이 포함되어 있다. "
                "생존편향 처리가 작동한다.\n")
    else:
        add("_유니버스에 is_delisted_later 컬럼이 없다._\n")

    # 3) 정정보고서 존재 비율
    add("## 3. 정정보고서 존재 비율\n")
    if not idx.empty:
        t3 = (idx.groupby("fy")
              .agg(문서수=("rcept_no", "size"),
                   정정존재=("is_correction_exists", "sum"),
                   정정본사용=("is_correction", "sum")).reset_index())
        t3["정정존재율"] = (t3["정정존재"] / t3["문서수"]).round(4)
        add(t3.to_markdown(index=False) + "\n")
        add("> `정정본사용` 은 원본이 아예 없어 정정본을 쓴 건수다. "
            "0 이어야 정상이며, 0 이 아니면 해당 건을 따로 확인한다.\n")

    # 4) 접수시각 결측률
    add("## 4. 접수시각(시:분) 결측률\n")
    if not idx.empty:
        has_time = idx["rcept_time"].astype(str).str.match(_TIME).fillna(False)
        t4 = (idx.assign(has_time=has_time).groupby("fy")["has_time"]
              .agg(있음="sum", 전체="size").reset_index())
        t4["결측률"] = (1 - t4["있음"] / t4["전체"]).round(4)
        add(t4.to_markdown(index=False) + "\n")
        miss = float(1 - has_time.mean())
        if miss > 0:
            add(f"> **접수시각 결측 {miss:.1%}.** 공시검색 API(list.json)는 "
                f"`rcept_dt`(일자)만 주고 시각은 주지 않는다. look-ahead 통제에 "
                f"시각이 필요하면 공시 상세 페이지를 따로 긁어야 한다. "
                f"이 작업은 문서당 1회 추가 요청이므로 원문 다운로드와 함께 "
                f"수행할지 결정해야 한다.\n")

    # 5) 예상 다운로드 건수와 일수
    add("## 5. 예상 다운로드 건수와 일수\n")
    n_docs = len(idx)
    raw_dir = PROJECT_ROOT / p1["paths"]["filings"]
    cached = len(list(raw_dir.glob("*.zip"))) if raw_dir.exists() else 0
    todo = max(0, n_docs - cached)
    limit = client.quota.limit
    days = (todo + limit - 1) // limit if limit else 0
    t5 = pd.DataFrame([{
        "인덱스 문서 수": n_docs, "이미 캐시": cached, "받을 문서": todo,
        "일일 한도": limit, "오늘 잔여": client.quota.remaining,
        "예상 소요 일수": days,
    }])
    add(t5.to_markdown(index=False) + "\n")
    add(f"- 호출 사용량: {client.quota.summary()}")
    add("- 다운로드는 연도 역순(최근 -> 과거)으로 진행한다. "
        "최근 연도가 파싱 품질이 좋아 문제를 일찍 발견할 수 있다.\n")

    add("## 6. 다음 단계\n")
    add("위 수치를 확인한 뒤 원문 다운로드를 시작한다.\n")
    add("```bash\npython -m src.collect.fetch_filings --download\n```\n")

    out = PROJECT_ROOT / "results" / "phase1" / "filings_index_report.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    log.info("인덱스 보고 -> %s", out)
    return out


# ===========================================================================
# 3단계 — 원문 다운로드
# ===========================================================================

def download_filings(cfg: Config, idx: pd.DataFrame, client: DartClient,
                     fails: FailureLog, *, max_docs: int | None = None) -> dict:
    """연도 역순으로 받는다. 캐시가 있으면 네트워크를 쓰지 않는다."""
    p1 = cfg["phase1"]
    dest = PROJECT_ROOT / p1["paths"]["filings"]
    dest.mkdir(parents=True, exist_ok=True)

    ordered = idx.sort_values(["fy", "corp_code"], ascending=[False, True])
    if max_docs:
        ordered = ordered.head(max_docs)

    stats = {"대상": len(ordered), "성공": 0, "캐시히트": 0, "실패": 0}
    for r in tqdm(list(ordered.itertuples(index=False)), desc="원문", unit="doc"):
        path = dest / f"{r.rcept_no}.zip"
        if path.exists() and path.stat().st_size > 0:
            stats["캐시히트"] += 1
            continue
        try:
            client.download_document(r.rcept_no, dest_dir=dest)
            stats["성공"] += 1
        except QuotaExceeded:
            log.error("일일 한도 소진 — 여기까지 받고 중단합니다. "
                      "내일 다시 실행하면 캐시를 건너뛰고 이어받습니다.")
            break
        except Exception as exc:
            stats["실패"] += 1
            fails.add(stage="download", key=str(r.rcept_no),
                      reason=type(exc).__name__, detail=str(exc))
    return stats


# ===========================================================================
# main
# ===========================================================================

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Phase 1 공시 수집")
    ap.add_argument("--config", default=None)
    ap.add_argument("--index", action="store_true", help="1단계: 인덱스만 만든다")
    ap.add_argument("--download", action="store_true", help="3단계: 원문 다운로드")
    ap.add_argument("--limit", type=int, default=None, help="유니버스 기업 수 제한")
    ap.add_argument("--max-docs", type=int, default=None, help="다운로드 문서 수 제한")
    ap.add_argument("--years", type=int, nargs="*", default=None)
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)

    if not (args.index or args.download):
        ap.error("--index 또는 --download 중 하나를 지정하세요.")

    cfg = load_config(args.config)
    p1 = cfg["phase1"]
    meta_dir = PROJECT_ROOT / p1["paths"]["meta"]
    meta_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(meta_dir / "phase1.log",
                  level=getattr(logging, args.log_level.upper()))
    set_seed(cfg)

    try:
        client = DartClient(cfg)
    except MissingApiKey as exc:
        log.error("%s", exc)
        return 2
    log.info("일일 호출 한도: %s", client.quota.summary())

    fails = FailureLog(meta_dir / "phase1_failures.csv")

    if args.index:
        universe = build_universe(cfg, args.years, limit=args.limit)
        if universe.empty:
            log.error("유니버스가 비었습니다.")
            return 1
        zip_path = client.fetch_corp_code_zip()
        mapping = parse_corp_code_zip(zip_path)
        universe = attach_corp_code(universe, mapping)
        universe.to_parquet(meta_dir / "universe_all.parquet", index=False)

        idx = build_index(cfg, universe, client, fails)
        index_report(cfg, universe, idx, client)
        fails.save()
        log.info("인덱스 단계 완료. 호출 사용량 %s", client.quota.summary())
        return 0

    idx_path = meta_dir / "filings_index.parquet"
    if not idx_path.exists():
        log.error("filings_index 가 없습니다. 먼저 --index 를 실행하세요.")
        return 1
    idx = pd.read_parquet(idx_path)
    stats = download_filings(cfg, idx, client, fails, max_docs=args.max_docs)
    fails.save()
    log.info("다운로드 결과: %s | 네트워크 호출 %d, 캐시 히트 %d | 한도 %s",
             stats, client.n_calls, client.n_cache_hits, client.quota.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
