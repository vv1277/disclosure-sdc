"""페어링용 직전 연도 문서 추가 수집 (Gate 3).

왜 필요한가
  유니버스를 매년 시총 상위 800 으로 재산정하면서 문서는 유니버스 firm-year
  만 받았다. 그 결과 '올해는 유니버스인데 작년에는 아니었던' 기업이 직전
  연도 문서를 갖지 못해 페어를 만들 수 없다. 전체 페어링률 83.1% 로
  Gate 3(90%+) 미달이다.

  단순 결손이 아니라 **선택편향**이다. 유니버스 신규 진입은 시총 급등 기업,
  이탈은 급락 기업이므로, 수익률 예측 연구에서 가장 중요한 구간이 빠진다.

수집 범위 재정의
  (기존) 유니버스 firm-year
  (변경) 유니버스 firm-year **및 그 직전 연도**

  추가분은 sample_role='pair_only' 로 표시한다. 이 문서들은 t-1 기준
  문서로만 쓰이고 그 자체가 관측(t)이 되지 않는다. 학습 표본에 중복
  투입되면 안 된다.

실행
  python -m src.collect.fetch_pairs --search     # 부족분 검색
  python -m src.collect.fetch_pairs --download   # 다운로드
"""
from __future__ import annotations

import argparse
import logging

import pandas as pd
from tqdm import tqdm

from src.collect.dart_client import DartClient, MissingApiKey, NoData
from src.collect.fetch_filings import _annual_rows, _pick_original
from src.collect.quota import QuotaExceeded
from src.utils.config import PROJECT_ROOT, Config, load_config, set_seed
from src.utils.failures import FailureLog
from src.utils.logging_utils import setup_logging

log = logging.getLogger("pairs")

PAIR_INDEX = "pair_only_index.parquet"


def needed_pairs(cfg: Config) -> set[tuple[str, int]]:
    meta = PROJECT_ROOT / cfg["phase1"]["paths"]["meta"]
    uni = pd.read_parquet(meta / "universe_all.parquet")
    idx = pd.read_parquet(meta / "filings_index.parquet")
    uni["fy"] = uni["fy"].astype(int)
    idx["fy"] = idx["fy"].astype(int)
    have = set(zip(idx["corp_code"].astype(str), idx["fy"]))
    first = uni["fy"].min()
    need = set()
    for r in uni.itertuples():
        fy, cc = int(r.fy), str(r.corp_code)
        if fy == first:
            continue
        if (cc, fy - 1) not in have:
            need.add((cc, fy - 1))
    return need


def search_missing(cfg: Config, missing: list[tuple[str, int]],
                   client: DartClient, fails: FailureLog) -> pd.DataFrame:
    """원시 검색에 없던 조합만 DART 에 다시 묻는다."""
    p1 = cfg["phase1"]
    rows = []
    by_corp: dict[str, list[int]] = {}
    for cc, fy in missing:
        by_corp.setdefault(cc, []).append(fy)

    for cc, fys in tqdm(by_corp.items(), desc="부족분 검색", unit="corp"):
        bgn, end = f"{min(fys) + 1}0101", f"{max(fys) + 2}0630"
        try:
            found = client.search_reports(cc, bgn, end)
        except QuotaExceeded:
            log.error("일일 한도 소진 — 여기까지 저장한다.")
            break
        except NoData:
            continue
        except Exception as exc:
            fails.add(stage="pair_search", key=cc,
                      reason=type(exc).__name__, detail=str(exc))
            continue
        for fy in fys:
            cands = [a for a in _annual_rows(found, {fy}, True) if a["fy"] == fy]
            if not cands:
                continue
            ch = _pick_original(cands, p1.get("report_types"))
            rows.append({"corp_code": cc, "fy": fy, "rcept_no": ch["rcept_no"],
                         "report_nm": ch.get("report_nm", ""),
                         "rcept_dt": ch.get("rcept_dt", ""),
                         "report_type": ch["report_type"],
                         "sample_role": "pair_only"})
    return pd.DataFrame(rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="페어링용 직전 연도 문서 수집")
    ap.add_argument("--config", default=None)
    ap.add_argument("--search", action="store_true")
    ap.add_argument("--download", action="store_true")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    p1 = cfg["phase1"]
    meta = PROJECT_ROOT / p1["paths"]["meta"]
    setup_logging(meta / "phase1.log", level=getattr(logging, args.log_level.upper()))
    set_seed(cfg)
    try:
        client = DartClient(cfg)
    except MissingApiKey as exc:
        log.error("%s", exc)
        return 2
    fails = FailureLog(meta / "pair_failures.csv")

    path = meta / PAIR_INDEX
    have = pd.read_parquet(path) if path.exists() else pd.DataFrame()

    if args.search:
        need = needed_pairs(cfg)
        got = set(zip(have["corp_code"], have["fy"])) if not have.empty else set()
        missing = sorted(need - got)
        log.info("필요 %d, 확보 %d, 추가 검색 %d", len(need), len(got), len(missing))
        if missing:
            new = search_missing(cfg, missing, client, fails)
            have = pd.concat([have, new], ignore_index=True).drop_duplicates(
                subset=["corp_code", "fy"], keep="first")
            have.to_parquet(path, index=False)
            log.info("pair_only_index -> %d행 (신규 %d)", len(have), len(new))

    if args.download:
        dest = PROJECT_ROOT / p1["paths"]["filings"]
        dest.mkdir(parents=True, exist_ok=True)
        stats = {"대상": len(have), "성공": 0, "캐시": 0, "실패": 0}
        for r in tqdm(list(have.itertuples(index=False)), desc="페어 원문", unit="doc"):
            f = dest / f"{r.rcept_no}.zip"
            if f.exists() and f.stat().st_size > 0:
                stats["캐시"] += 1
                continue
            try:
                client.download_document(r.rcept_no, dest_dir=dest)
                stats["성공"] += 1
            except QuotaExceeded:
                log.error("일일 한도 소진 — 내일 이어받는다.")
                break
            except Exception as exc:
                stats["실패"] += 1
                fails.add(stage="pair_download", key=str(r.rcept_no),
                          reason=type(exc).__name__, detail=str(exc))
        log.info("다운로드: %s | 한도 %s", stats, client.quota.summary())

    fails.save()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
