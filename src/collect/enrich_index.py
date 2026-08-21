"""filings_index 에 보고서 유형·정정 이력·연장신고 플래그를 붙인다 (Phase 1 결정 2 후속).

원시 검색 결과를 그대로 보관한 뒤 거기서 전부 파생시킨다. 그래야 나중에
"왜 이 문서를 원본으로 봤는가" 를 재구성할 수 있다.

산출
  data/meta/filings_search_raw.parquet   기업별 공시검색 원시 결과
  data/meta/filings_index.parquet        아래 컬럼이 추가된다
      report_type              original / attachment_added / material_amendment
      type_reason              판정 사유 (사람이 읽는 문장)
      bracket_tags             원문 대괄호 표기
      has_material_amendment   [기재정정]이 존재하는가 (Phase 8 R7 용)
      n_material_amendments
      first_amendment_dt
      amendment_lag_days       원본 제출일 -> 첫 정정일
      had_deadline_extension   제출기한 연장 신고가 있었는가

실행
  python -m src.collect.enrich_index --fetch    # 원시 검색 수집 후 붙이기
  python -m src.collect.enrich_index            # 이미 받은 원시 결과로 붙이기만
"""
from __future__ import annotations

import argparse
import logging
import re
from typing import Any

import pandas as pd
from tqdm import tqdm

from src.collect.dart_client import DartClient, NoData
from src.collect.quota import QuotaExceeded
from src.collect.report_type import MATERIAL_AMENDMENT, classify_report
from src.utils.config import PROJECT_ROOT, Config, load_config, set_seed
from src.utils.logging_utils import setup_logging

log = logging.getLogger("enrich")

_PERIOD = re.compile(r"\((\d{4})\.(\d{1,2})\)")
RAW_NAME = "filings_search_raw.parquet"


def fetch_raw(cfg: Config, idx: pd.DataFrame, client: DartClient) -> pd.DataFrame:
    """기업별 공시검색 결과를 그대로 저장한다."""
    p1 = cfg["phase1"]
    years = sorted(int(y) for y in p1["fiscal_years"])
    bgn, end = f"{min(years) + 1}0101", f"{max(years) + 2}0630"
    corps = idx.drop_duplicates("corp_code")[["corp_code", "corp_name"]]

    rows: list[dict[str, Any]] = []
    for c in tqdm(list(corps.itertuples(index=False)), desc="원시 검색", unit="corp"):
        try:
            found = client.search_reports(c.corp_code, bgn, end)
        except QuotaExceeded:
            log.error("일일 한도 소진 — 여기까지 저장한다. 내일 이어서 실행하면 된다.")
            break
        except NoData:
            found = []
        except Exception as exc:
            log.warning("검색 실패 %s: %s", c.corp_code, exc)
            continue
        for f in found:
            nm = f.get("report_nm", "") or ""
            m = _PERIOD.search(nm)
            rows.append({
                "corp_code": c.corp_code, "corp_name": c.corp_name,
                "report_nm": nm, "rcept_no": f.get("rcept_no", ""),
                "rcept_dt": str(f.get("rcept_dt", "")),
                "period_year": int(m.group(1)) if m else None,
                "period_month": int(m.group(2)) if m else None,
            })
    raw = pd.DataFrame(rows)
    out = PROJECT_ROOT / cfg["phase1"]["paths"]["meta"] / RAW_NAME
    raw.to_parquet(out, index=False)
    log.info("원시 검색 결과 %d행 -> %s", len(raw), out)
    return raw


def enrich(cfg: Config, idx: pd.DataFrame, raw: pd.DataFrame) -> pd.DataFrame:
    p1 = cfg["phase1"]
    rules = p1["report_types"]
    ext_name = p1["deadline_extension_name"]

    # 1) 선택된 문서 자체의 유형
    cls = idx["report_nm"].map(lambda n: classify_report(n, rules))
    for col in ("report_type", "type_reason", "bracket_tags"):
        idx[col] = [c[col] for c in cls]

    # 2) (corp, fy) 별 정정 이력과 연장신고
    r = raw.copy()
    r["is_report"] = r["report_nm"].str.contains("사업보고서", na=False)
    r = r[r["is_report"] & r["period_year"].notna() & (r["period_month"] == 12)]
    r["fy"] = r["period_year"].astype(int)
    rc = r["report_nm"].map(lambda n: classify_report(n, rules))
    r["report_type"] = [c["report_type"] for c in rc]
    r["is_extension"] = r["report_nm"].str.contains(ext_name, na=False)

    mat = r[(r["report_type"] == MATERIAL_AMENDMENT) & ~r["is_extension"]]
    agg = (mat.groupby(["corp_code", "fy"])
           .agg(n_material_amendments=("rcept_no", "size"),
                first_amendment_dt=("rcept_dt", "min")).reset_index())

    ext = (r[r["is_extension"]].groupby(["corp_code", "fy"]).size()
           .rename("n_extension").reset_index())

    idx = idx.merge(agg, on=["corp_code", "fy"], how="left")
    idx = idx.merge(ext, on=["corp_code", "fy"], how="left")
    idx["n_material_amendments"] = idx["n_material_amendments"].fillna(0).astype(int)
    idx["has_material_amendment"] = idx["n_material_amendments"] > 0
    idx["had_deadline_extension"] = idx["n_extension"].fillna(0).astype(int) > 0
    idx = idx.drop(columns=["n_extension"])

    # 3) 정정 지연 일수 — 원본 제출일에서 첫 정정일까지
    base = pd.to_datetime(idx["rcept_dt"], format="%Y%m%d", errors="coerce")
    amd = pd.to_datetime(idx["first_amendment_dt"], format="%Y%m%d", errors="coerce")
    idx["amendment_lag_days"] = (amd - base).dt.days
    # 정정이 원본보다 앞설 수는 없다 (있으면 원본 선택이 틀린 것)
    bad = idx["amendment_lag_days"].notna() & (idx["amendment_lag_days"] < 0)
    if bad.any():
        log.warning("정정일이 원본일보다 이른 행 %d건 — 원본 선택을 확인할 것",
                    int(bad.sum()))
    return idx


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="filings_index 보강")
    ap.add_argument("--config", default=None)
    ap.add_argument("--fetch", action="store_true", help="원시 검색을 다시 받는다")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    meta = PROJECT_ROOT / cfg["phase1"]["paths"]["meta"]
    setup_logging(meta / "phase1.log", level=getattr(logging, args.log_level.upper()))
    set_seed(cfg)

    idx = pd.read_parquet(meta / "filings_index.parquet")
    raw_path = meta / RAW_NAME
    if args.fetch or not raw_path.exists():
        raw = fetch_raw(cfg, idx, DartClient(cfg))
    else:
        raw = pd.read_parquet(raw_path)
        log.info("원시 검색 결과 재사용 (%d행)", len(raw))

    out = enrich(cfg, idx, raw)
    out.to_parquet(meta / "filings_index.parquet", index=False)
    log.info("보강 완료: %s", {
        "행": len(out),
        "original": int((out.report_type == "original").sum()),
        "attachment_added": int((out.report_type == "attachment_added").sum()),
        "material_amendment": int((out.report_type == "material_amendment").sum()),
        "has_material_amendment": int(out.has_material_amendment.sum()),
        "had_deadline_extension": int(out.had_deadline_extension.sum()),
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
