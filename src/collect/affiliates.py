"""공정위 대규모기업집단 지정 자료 -> corp_code x 연도 -> 기업집단명 매핑.

왜 필요한가
  P0-d 에서 '기업 간 공통 변경 문단' 신호에 규제 서식 개정뿐 아니라
  **기업집단 내 모회사-자회사 텍스트 재사용**이 섞인다는 것이 확인됐다.
  SKC 가 2023년 ISC 를 인수한 뒤 SKC 사업보고서가 ISC 의 사업 서술을 그대로
  옮겨 담았고, 에코프로/에코프로비엠도 같은 유형이었다.
  Phase 5(Template Filter)가 이를 걸러내지 못하면 서식 개정이 아니라
  지배구조를 측정하게 된다. 계열사 쌍을 제외하려면 연도별 소속 정보가 있어야 한다.

왜 연도별 스냅샷인가
  대규모기업집단 지정은 매년 5월에 새로 이뤄지고, 편입·제외가 빈번하다.
  SKC/ISC 처럼 인수로 계열이 바뀐 사례가 표본 안에 실제로 있으므로,
  단일 시점 매핑을 쓰면 인수 전 연도까지 계열사로 잘못 묶인다.

데이터 소스
  TODO(API): data.go.kr 의 공정거래위원회 오픈API 키가 생기면 자동 수집으로
             교체한다. 그 전까지는 연도별 파일을 수동으로 배치한다.

      data/reference/fair_trade_groups_{year}.csv
      필수 컬럼: group_name, corp_name   (그 외 컬럼은 무시한다)

  공정위 기업집단포털(https://www.egroup.go.kr) 의 '소속회사 현황' 을
  연도별로 내려받아 위 두 컬럼만 남기면 된다.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import pandas as pd
from rapidfuzz import fuzz, process

from src.utils.config import PROJECT_ROOT, Config

log = logging.getLogger(__name__)

_YEAR_RE = re.compile(r"(\d{4})")

# 기업명 정규화: 법인격 표기와 공백을 제거해야 공정위 자료와 DART 가 맞는다.
_CORP_SUFFIX = re.compile(
    r"(주식회사|㈜|\(주\)|\(유\)|유한회사|합자회사|주\)|\(재\)|재단법인|사단법인)")
_NON_NAME = re.compile(r"[\s\.\,\-\_\(\)\[\]<>·ㆍ'\"]+")


def normalize_corp_name(name: str) -> str:
    """'주식회사 에코프로비엠' 과 '에코프로비엠(주)' 를 같은 키로 만든다."""
    s = _CORP_SUFFIX.sub("", str(name or ""))
    s = _NON_NAME.sub("", s)
    return s.upper()


class MissingAffiliateData(RuntimeError):
    """공정위 기업집단 자료가 없을 때. 조용히 넘어가면 안 되는 상황이다."""


def load_snapshots(cfg: Config, *, require: bool = True) -> pd.DataFrame:
    """data/reference/fair_trade_groups_{year}.csv 들을 하나로 모은다.

    require=True 면 자료가 없을 때 MissingAffiliateData 를 던진다.
    Phase 5 는 반드시 require=True 로 부른다.
    """
    p1 = cfg["phase1"]
    ref_dir = PROJECT_ROOT / p1["paths"]["reference"]
    pattern = p1["affiliates"]["source_glob"]
    files = sorted(ref_dir.glob(pattern))
    if not files:
        if not require:
            log.warning("공정위 기업집단 자료 없음 (require=False 이므로 계속): %s/%s",
                        ref_dir, pattern)
            return pd.DataFrame(columns=["year", "group_name", "corp_name"])
        raise MissingAffiliateData(
            f"공정위 기업집단 자료가 없습니다: {ref_dir}/{pattern}\n"
            f"  기업집단포털(https://www.egroup.go.kr)에서 연도별 소속회사 현황을\n"
            f"  내려받아 group_name, corp_name 두 컬럼으로 저장하세요.\n"
            f"    {ref_dir}/fair_trade_groups_2016.csv ... _2024.csv\n"
            f"  이 매핑 없이 Phase 5 를 돌리면 계열사 쌍이 통제되지 않은 채로\n"
            f"  '기업 간 공통 변경 문단'이 집계됩니다. 그것이 최악의 결과이므로\n"
            f"  조용히 빈 매핑을 반환하지 않고 여기서 중단합니다.\n"
            f"  Phase 5 이전 단계만 돌릴 것이라면 require=False 로 호출하세요.")

    frames = []
    for f in files:
        m = _YEAR_RE.search(f.stem)
        if not m:
            log.warning("파일명에서 연도를 찾지 못해 건너뜁니다: %s", f.name)
            continue
        df = pd.read_csv(f, dtype=str).rename(columns=str.strip)
        missing = {"group_name", "corp_name"} - set(df.columns)
        if missing:
            log.warning("%s: 필수 컬럼 없음 %s — 건너뜁니다", f.name, missing)
            continue
        df = df[["group_name", "corp_name"]].dropna()
        df["year"] = int(m.group(1))
        frames.append(df)
        log.info("[%s] 기업집단 %d개, 소속회사 %d개",
                 m.group(1), df["group_name"].nunique(), len(df))

    if not frames:
        if require:
            raise MissingAffiliateData(
                f"{ref_dir}/{pattern} 파일은 있으나 읽을 수 있는 것이 없습니다. "
                f"group_name, corp_name 컬럼과 파일명의 연도를 확인하세요.")
        return pd.DataFrame(columns=["year", "group_name", "corp_name"])
    out = pd.concat(frames, ignore_index=True)
    out["corp_name_norm"] = out["corp_name"].map(normalize_corp_name)
    return out


def map_to_corp_code(
    snapshots: pd.DataFrame,
    universe: pd.DataFrame,
    *,
    threshold: int = 92,
) -> pd.DataFrame:
    """(year, corp_code) -> group_name 매핑.

    universe: corp_code, corp_name 을 가진 표 (연도별 유니버스 또는 corp_code 매핑)
    정확 일치를 먼저 쓰고, 실패한 것만 퍼지 매칭한다.
    매칭 방식(exact/fuzzy)과 점수를 남겨 나중에 검증할 수 있게 한다.
    """
    if snapshots.empty or universe.empty:
        return pd.DataFrame(columns=["year", "corp_code", "corp_name", "group_name",
                                     "match_type", "match_score"])

    uni = universe.drop_duplicates(subset=["corp_code"]).copy()
    uni["corp_name_norm"] = uni["corp_name"].map(normalize_corp_name)

    rows: list[dict[str, Any]] = []
    for year, snap in snapshots.groupby("year"):
        exact = dict(zip(snap["corp_name_norm"], snap["group_name"]))
        choices = list(exact.keys())
        for u in uni.itertuples():
            key = u.corp_name_norm
            if key in exact:
                rows.append({"year": int(year), "corp_code": u.corp_code,
                             "corp_name": u.corp_name, "group_name": exact[key],
                             "match_type": "exact", "match_score": 100})
                continue
            if not choices:
                continue
            hit = process.extractOne(key, choices, scorer=fuzz.ratio,
                                     score_cutoff=threshold)
            if hit:
                rows.append({"year": int(year), "corp_code": u.corp_code,
                             "corp_name": u.corp_name, "group_name": exact[hit[0]],
                             "match_type": "fuzzy", "match_score": int(hit[1])})
    out = pd.DataFrame(rows)
    if not out.empty:
        n_fuzzy = int((out["match_type"] == "fuzzy").sum())
        log.info("기업집단 매핑 %d건 (정확 %d, 퍼지 %d)",
                 len(out), len(out) - n_fuzzy, n_fuzzy)
    return out


def same_group_pairs(mapping: pd.DataFrame, year: int) -> set[frozenset[str]]:
    """해당 연도에 같은 기업집단에 속한 corp_code 쌍 집합.

    Phase 5 에서 '기업 간 공통 변경 문단' 을 셀 때 이 쌍을 제외한다.
    """
    if mapping.empty:
        return set()
    y = mapping[mapping["year"] == year]
    pairs: set[frozenset[str]] = set()
    for _, g in y.groupby("group_name"):
        codes = sorted(g["corp_code"].unique())
        for i, a in enumerate(codes):
            for b in codes[i + 1:]:
                pairs.add(frozenset((a, b)))
    return pairs


def build(cfg: Config, universe: pd.DataFrame, *, require: bool = True
          ) -> pd.DataFrame:
    p1 = cfg["phase1"]
    snaps = load_snapshots(cfg, require=require)
    mapping = map_to_corp_code(
        snaps, universe,
        threshold=int(p1["affiliates"].get("name_match_threshold", 92)))
    out_path = PROJECT_ROOT / p1["affiliates"]["out"]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    mapping.to_parquet(out_path, index=False)
    log.info("기업집단 매핑 저장 -> %s (%d행)", out_path, len(mapping))

    # 기업집단명·기업명은 표기 변형이 많다. 퍼지 매칭 하위 N건은 사람이 봐야 한다.
    n_review = int(p1["affiliates"].get("review_bottom_n", 50))
    fuzzy = mapping[mapping["match_type"] == "fuzzy"] if not mapping.empty else mapping
    if not fuzzy.empty:
        review = fuzzy.nsmallest(n_review, "match_score")
        review_path = out_path.with_name("affiliate_match_review.csv")
        review.to_csv(review_path, index=False, encoding="utf-8-sig")
        log.warning("퍼지 매칭 하위 %d건 검수 필요 -> %s (최저 점수 %d)",
                    len(review), review_path, int(review["match_score"].min()))
    return mapping
