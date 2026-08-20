"""연도별 유니버스 구성 (Phase 1).

생존편향 — 이 프로젝트에서 가장 흔한 치명적 실수
  "현재 상장된 기업 목록"으로 표본을 만들면 안 된다. 각 회계연도 말 **그 시점의**
  상장종목 스냅샷을 따로 만들고, 그 기준으로 표본을 구성한다. 2016년에 상장되어
  있었으나 이후 상장폐지된 기업이 반드시 들어가야 한다.

데이터 소스 — pykrx 대신 FinanceDataReader
  계획서는 pykrx 를 제안하지만, 설치된 pykrx 1.2.8 은 현재 KRX 사이트와 맞지
  않아 `get_market_ticker_list` 가 빈 목록을, `get_market_cap_by_ticker` 가
  KeyError 를 낸다. OHLCV 계열은 KRX_ID/KRX_PW 로그인까지 요구한다.
  대신 FinanceDataReader 를 쓴다 (계획서 A.2 는 'pykrx 또는 DataGuide' 로
  소스를 못박지 않았다).

    StockListing('KRX')            현재 상장 2,872종목 (Marcap, Stocks 포함)
    StockListing('KRX-DELISTING')  상장폐지 4,176종목
                                   (ListingDate, DelistingDate, ListingShares)

  상장폐지 목록에 상장일·폐지일이 있으므로 '연도 말 시점에 상장되어 있었는가' 를
  정확히 판정할 수 있다. 2016~2024 사이 폐지가 1,136건이다.

시가총액 산정 — krx_login (연구자 결정)
  '연도 말 시가총액 상위 800' 에는 과거 시점 시가총액이 필요한데 FDR 은 현재
  시총만 준다. 근사(연도말 종가 x 현재 주식수)는 유상증자·액면분할 기업에서
  과거 시총을 왜곡하므로, 정확한 KRX 시총 API 를 쓰기로 했다.

  .env 의 KRX_ID / KRX_PW 를 환경변수로 올리면 pykrx 가 로그인해 시총을 준다.
  자격증명은 .env 에만 둔다 (.env 는 gitignore 대상).

  유니버스를 다 만든 뒤 로그인 실패를 발견하면 시간이 크게 낭비되므로,
  `check_krx_login()` 으로 먼저 확인한 뒤 진행한다.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

import pandas as pd
from tqdm import tqdm

from src.utils.config import PROJECT_ROOT, Config

log = logging.getLogger(__name__)


class MarketCapSourceUnavailable(RuntimeError):
    """과거 시점 시가총액 소스가 정해지지 않았다. 유니버스를 확정할 수 없다."""


def _fdr():
    import FinanceDataReader as fdr
    return fdr


def load_listing_tables() -> tuple[pd.DataFrame, pd.DataFrame]:
    """(현재 상장, 상장폐지) 두 표. 둘 다 캐시 없이 매번 받는다 (가볍다)."""
    fdr = _fdr()
    cur = fdr.StockListing("KRX")
    dead = fdr.StockListing("KRX-DELISTING")
    for c in ("ListingDate", "DelistingDate"):
        if c in dead.columns:
            dead[c] = pd.to_datetime(dead[c], errors="coerce")
    log.info("현재 상장 %d, 상장폐지 %d", len(cur), len(dead))
    return cur, dead


def listed_at(cur: pd.DataFrame, dead: pd.DataFrame, date: str) -> pd.DataFrame:
    """해당 시점에 상장되어 있던 종목 (폐지 예정 기업 포함).

    생존편향 제거의 핵심. 현재 목록만 쓰면 그 시점 이후 폐지된 기업이 빠진다.
    """
    ts = pd.Timestamp(date)
    alive = cur.rename(columns={"Code": "stock_code", "Name": "name",
                                "Market": "market"}).copy()
    alive["is_delisted_later"] = False
    alive["delist_date"] = pd.NaT

    d = dead.rename(columns={"Symbol": "stock_code", "Name": "name",
                             "Market": "market"}).copy()
    was_listed = (d["ListingDate"].notna() & (d["ListingDate"] <= ts)
                  & (d["DelistingDate"].isna() | (d["DelistingDate"] > ts)))
    gone = d[was_listed].copy()
    gone["is_delisted_later"] = True
    gone["delist_date"] = gone["DelistingDate"]

    cols = ["stock_code", "name", "market", "is_delisted_later", "delist_date"]
    out = pd.concat([alive[cols], gone[cols]], ignore_index=True)
    out = out.drop_duplicates("stock_code")
    log.info("[%s] 상장 %d종목 (이후 폐지 예정 %d)", date, len(out),
             int(out["is_delisted_later"].sum()))
    return out


class KrxLoginMissing(RuntimeError):
    """KRX 계정이 없다. 시가총액 조회를 할 수 없다."""


def load_krx_credentials() -> tuple[str, str]:
    """.env 의 KRX_ID / KRX_PW 를 환경변수로 올린다.

    pykrx 는 os.environ 에서 이 값을 읽는다. 코드나 config 에 두지 않는다.
    """
    for name in ("KRX_ID", "KRX_PW"):
        if os.environ.get(name):
            continue
        dotenv = PROJECT_ROOT / ".env"
        if dotenv.exists():
            for line in dotenv.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() == name:
                    os.environ[name] = v.strip().strip('"').strip("'")
                    break
    kid, kpw = os.environ.get("KRX_ID", ""), os.environ.get("KRX_PW", "")
    if not kid or not kpw:
        raise KrxLoginMissing(
            "KRX 계정이 설정되지 않았습니다.\n"
            "  .env 에 KRX_ID / KRX_PW 두 줄을 채우세요 (data.krx.co.kr 계정).\n"
            "  값은 .env 에만 둡니다 — .env 는 gitignore 대상입니다."
        )
    return kid, kpw


def check_krx_login() -> str:
    """로그인이 실제로 되는지 가벼운 호출로 확인한다.

    유니버스를 다 만든 뒤에 실패하면 시간이 크게 낭비되므로 먼저 본다.
    """
    load_krx_credentials()
    stock = _pykrx()
    tickers = stock.get_market_ticker_list("20241230", market="KOSPI")
    if not tickers:
        raise KrxLoginMissing(
            "KRX 로그인은 됐으나 종목 목록이 비어 있습니다. "
            "계정 상태와 pykrx 버전을 확인하세요.")
    return f"KRX 로그인 확인 — KOSPI {len(tickers)}종목"


def _pykrx():
    from pykrx import stock
    return stock


def year_end_trading_day(year: int) -> str:
    """해당 연도의 마지막 영업일 (YYYYMMDD)."""
    stock = _pykrx()
    days = stock.get_previous_business_days(year=year, month=12)
    return days[-1].strftime("%Y%m%d")


def _compile_exclusions(patterns: list[str]) -> list[re.Pattern]:
    return [re.compile(p) for p in patterns]


def _financial_tickers(date: str, market: str) -> set[str]:
    """그 시점 금융업 지수 구성종목. 못 찾으면 빈 집합 (제외를 건너뛴다)."""
    stock = _pykrx()
    try:
        codes = stock.get_index_ticker_list(date, market=market)
    except Exception as exc:                      # pykrx/KRX 응답 변동
        log.warning("[%s %s] 업종지수 목록 조회 실패: %s", date, market, exc)
        return set()

    out: set[str] = set()
    for code in codes:
        try:
            name = stock.get_index_ticker_name(code)
        except Exception:
            continue
        if not name or "금융" not in name:
            continue
        try:
            members = stock.get_index_portfolio_deposit_file(code, date)
        except Exception as exc:
            log.warning("[%s] 지수 %s(%s) 구성종목 조회 실패: %s", date, code, name, exc)
            continue
        if members:
            log.info("[%s %s] 금융업 지수 '%s' 구성종목 %d개 제외",
                     date, market, name, len(members))
            out.update(members)
    if not out:
        log.warning("[%s %s] 금융업 지수를 찾지 못했습니다. 금융업 제외가 "
                    "적용되지 않습니다 — Gate 1 에서 확인할 것.", date, market)
    return out


def snapshot(year: int, cfg: Config) -> pd.DataFrame:
    """해당 연도 말 시점의 상장종목 스냅샷 (제외 규칙 적용 전 전체).

    주의: 과거 시점 시가총액 소스가 정해지기 전에는 유니버스를 확정하지 않는다.
    잘못된 유니버스로 원문 7,200건을 받으면 전부 다시 받아야 한다.
    """
    p1 = cfg["phase1"]
    method = p1.get("mcap_method", "unset")
    if method not in ("yearend_close_x_current_shares", "krx_login"):
        raise MarketCapSourceUnavailable(f"""phase1.mcap_method 가 '{method}' 입니다. 유니버스를 확정할 수 없습니다.
  '연도 말 시가총액 상위 {p1['top_n_by_mcap']}개' 를 뽑으려면 과거 시점 시가총액이
  필요한데, FinanceDataReader 는 현재 시총만 줍니다. 설치된 pykrx 1.2.8 은 현재
  KRX 사이트와 맞지 않아 쓸 수 없습니다 (ticker 목록 0건, 시총 KeyError,
  OHLCV 는 KRX 로그인 요구).

  config.yaml 의 phase1.mcap_method 에서 아래 중 하나를 고르세요.
    yearend_close_x_current_shares
        연도말 종가 x 현재 주식수. 종목당 가격 이력 1회 요청(약 4,000종목).
        유상증자·액면분할이 있었던 기업은 과거 시총이 왜곡됩니다.
    krx_login
        KRX_ID/KRX_PW 를 .env 에 넣고 pykrx 시총 API 를 씁니다.
        정확하지만 KRX 계정이 필요합니다.

  유니버스가 틀리면 원문 7,200건을 다시 받아야 하므로 여기서 멈춥니다.""")

    if method == "krx_login":
        load_krx_credentials()

    stock = _pykrx()
    date = year_end_trading_day(year)
    excl = _compile_exclusions(p1["exclude_name_patterns"])

    frames = []
    for market in p1["markets"]:
        tickers = stock.get_market_ticker_list(date, market=market)
        if not tickers:
            log.warning("[%s %s] 상장종목 0개", date, market)
            continue
        cap = stock.get_market_cap_by_ticker(date, market=market)
        fin = _financial_tickers(date, market) if p1.get("exclude_sectors") else set()

        rows: list[dict[str, Any]] = []
        for t in tickers:
            name = stock.get_market_ticker_name(t)
            mcap = int(cap.loc[t, "시가총액"]) if t in cap.index else 0
            excluded = None
            if t in fin:
                excluded = "금융업"
            elif any(p.search(name) for p in excl):
                excluded = "스팩/우선주/리츠"
            elif mcap <= 0:
                excluded = "시가총액 결측"
            rows.append({"snapshot_date": date, "fy": year, "stock_code": t,
                         "name": name, "market": market, "mcap": mcap,
                         "excluded_reason": excluded})
        frames.append(pd.DataFrame(rows))

    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    log.info("[%d] 상장종목 %d개 (제외 %d)", year, len(df),
             int(df["excluded_reason"].notna().sum()))
    return df


def build_universe(cfg: Config, years: list[int] | None = None,
                   *, limit: int | None = None) -> pd.DataFrame:
    """연도별 유니버스를 만들고 parquet 으로 저장한다."""
    p1 = cfg["phase1"]
    years = years or list(p1["fiscal_years"])
    top_n = int(p1["top_n_by_mcap"])
    meta_dir = PROJECT_ROOT / p1["paths"]["meta"]
    meta_dir.mkdir(parents=True, exist_ok=True)

    snaps: dict[int, pd.DataFrame] = {}
    for y in tqdm(years, desc="유니버스", unit="year"):
        path = meta_dir / f"universe_{y}.parquet"
        if path.exists():
            snaps[y] = pd.read_parquet(path)
            log.info("[%d] 캐시 사용 (%d행)", y, len(snaps[y]))
            continue
        df = snapshot(y, cfg)
        if df.empty:
            continue
        keep = df[df["excluded_reason"].isna()].copy()
        keep = keep.sort_values("mcap", ascending=False).head(
            limit or top_n).reset_index(drop=True)
        keep["rank_mcap"] = range(1, len(keep) + 1)
        keep.to_parquet(path, index=False)
        snaps[y] = keep
        log.info("[%d] 유니버스 확정 %d개 (시총 상위)", y, len(keep))

    if not snaps:
        return pd.DataFrame()

    # 생존편향 검증용: 이후 연도 스냅샷에서 사라진 종목을 표시한다.
    all_years = sorted(snaps)
    last_year = all_years[-1]
    latest_codes = set(snaps[last_year]["stock_code"])
    out = []
    for y in all_years:
        df = snaps[y].copy()
        later = set()
        for y2 in all_years:
            if y2 > y:
                later |= set(snaps[y2]["stock_code"])
        # 마지막 연도 이후는 알 수 없으므로 마지막 연도는 판정하지 않는다
        df["is_delisted_later"] = (
            (~df["stock_code"].isin(latest_codes)) & (y < last_year)
            if y < last_year else False
        )
        df["last_seen_fy"] = df["stock_code"].map(
            lambda c: max((yy for yy in all_years
                           if c in set(snaps[yy]["stock_code"])), default=y))
        out.append(df)
    universe = pd.concat(out, ignore_index=True)
    universe.to_parquet(meta_dir / "universe_all.parquet", index=False)
    log.info("유니버스 전체 %d행, 고유 종목 %d개",
             len(universe), universe["stock_code"].nunique())
    return universe


def attach_corp_code(universe: pd.DataFrame, mapping: pd.DataFrame) -> pd.DataFrame:
    """종목코드 -> DART 고유번호. 매핑 실패는 로그로 남기고 행은 유지한다."""
    m = (mapping[mapping["stock_code"].str.fullmatch(r"\d{6}").fillna(False)]
         .sort_values("modify_date", ascending=False)
         .drop_duplicates("stock_code")[["stock_code", "corp_code", "corp_name"]])
    out = universe.merge(m, on="stock_code", how="left")
    n_missing = int(out["corp_code"].isna().sum())
    if n_missing:
        log.warning("corp_code 매핑 실패 %d행 (고유 종목 %d개)",
                    n_missing, out.loc[out["corp_code"].isna(), "stock_code"].nunique())
    return out
