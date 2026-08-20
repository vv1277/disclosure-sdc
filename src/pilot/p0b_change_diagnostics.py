"""프롬프트 P0-b — 변화율 및 서식개정 스파이크 진단.

선행 조건: data/pilot/sections/ 아래에 P0 가 저장한 섹션 텍스트가 있어야 한다.

산출
  data/pilot/change_rates.csv            (기업 x 섹션 x 연도쌍 유사도 3종)
  data/pilot/common_paragraphs.csv       (기업 간 유사 변경문단 쌍 상위 N)
  data/pilot/change_diagnostics.md
  data/pilot/fig1_change_by_year.png     연도별 평균 변화율, 섹션별 라인
  data/pilot/fig2_common_paragraphs.png  기업 간 공통 변경 문단 수, 연도별 바

실행
  python -m src.pilot.p0b_change_diagnostics --mock
  python -m src.pilot.p0b_change_diagnostics
"""

from __future__ import annotations

import argparse
import logging
import re
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.pilot.similarity import (
    diff_paragraphs,
    difflib_ratio,
    jaccard,
    levenshtein_sim,
    minhash_pairs,
    tfidf_cosine_matrix,
)
from src.utils.config import Config, load_config, set_seed
from src.utils.logging_utils import setup_logging
from src.utils.plotting import plt, setup_korean_font
from src.utils.textnorm import split_paragraphs

log = logging.getLogger("p0b")

_STEM_RE = re.compile(r"^(?P<corp>[^_]+)_(?P<fy>\d{4})_(?P<sec>S\d+)$")


# ---------------------------------------------------------------------------
# 로딩
# ---------------------------------------------------------------------------


def load_section_texts(pilot_dir: Path) -> pd.DataFrame:
    sec_dir = pilot_dir / "sections"
    if not sec_dir.exists():
        raise SystemExit(
            f"선행 조건 미충족: {sec_dir} 가 없습니다. "
            f"먼저 `python -m src.pilot.p0_diagnostics` 를 실행하세요."
        )
    rows: list[dict[str, Any]] = []
    for p in sorted(sec_dir.glob("*.txt")):
        m = _STEM_RE.match(p.stem)
        if not m:
            log.warning("파일명 규칙 불일치, 건너뜀: %s", p.name)
            continue
        rows.append(
            {
                "corp_code": m["corp"],
                "fy": int(m["fy"]),
                "section": m["sec"],
                "text": p.read_text(encoding="utf-8"),
            }
        )
    df = pd.DataFrame(rows)
    if df.empty:
        raise SystemExit(f"{sec_dir} 에 섹션 텍스트가 없습니다.")
    log.info(
        "섹션 텍스트 %d건 로드 (기업 %d, 연도 %s)",
        len(df),
        df["corp_code"].nunique(),
        sorted(df["fy"].unique()),
    )
    return df


def attach_names(df: pd.DataFrame, pilot_dir: Path) -> pd.DataFrame:
    idx_path = pilot_dir / "reports_index.csv"

    if not idx_path.exists():
        return df.assign(corp_name=df["corp_code"], market="", size_tier="")

    idx = pd.read_csv(idx_path, dtype={"stock_code": str})

    meta = idx.drop_duplicates("corp_code")[
        ["corp_code", "corp_name", "market", "size_tier"]
    ]

    df = df.copy()
    meta = meta.copy()

    df["corp_code"] = df["corp_code"].astype(str).str.zfill(8)
    meta["corp_code"] = meta["corp_code"].astype(str).str.zfill(8)

    return df.merge(meta, on="corp_code", how="left")


# ---------------------------------------------------------------------------
# 1) 인접 연도 쌍 유사도 3종
# ---------------------------------------------------------------------------


def compute_change_rates(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    ngram = tuple(cfg["change"]["tfidf_char_ngram"])
    out: list[dict[str, Any]] = []

    for section, sub in df.groupby("section"):
        sub = sub.reset_index(drop=True)
        texts = sub["text"].tolist()
        log.info("[%s] TF-IDF 문자 %d-gram, 문서 %d건", section, ngram[0], len(texts))
        cos = tfidf_cosine_matrix(texts, ngram=ngram)
        pos = {(r.corp_code, r.fy): i for i, r in enumerate(sub.itertuples())}

        for corp, g in sub.groupby("corp_code"):
            years = sorted(g["fy"].tolist())
            for prev_fy, curr_fy in zip(years, years[1:]):
                i, j = pos[(corp, prev_fy)], pos[(corp, curr_fy)]
                a, b = texts[i], texts[j]
                sim_cos = float(cos[i, j])
                sim_jac = jaccard(a, b)
                sim_lev = levenshtein_sim(a, b)
                sim_dl = difflib_ratio(a, b)
                out.append(
                    {
                        "corp_code": corp,
                        "section": section,
                        "fy_prev": prev_fy,
                        "fy_curr": curr_fy,
                        "pair": f"{prev_fy}->{curr_fy}",
                        "len_prev": len(a),
                        "len_curr": len(b),
                        "sim_tfidf_cos": sim_cos,
                        "sim_jaccard": sim_jac,
                        "sim_levenshtein": sim_lev,
                        "sim_difflib": sim_dl,
                        "change_tfidf_cos": 1 - sim_cos,
                        "change_jaccard": 1 - sim_jac,
                        "change_levenshtein": 1 - sim_lev,
                        "change_difflib": 1 - sim_dl,
                    }
                )
    return pd.DataFrame(out)


# ---------------------------------------------------------------------------
# 3) 스파이크 진단
# ---------------------------------------------------------------------------


def spike_table(rates: pd.DataFrame, metric: str = "change_tfidf_cos") -> pd.DataFrame:
    """기업별 변화율을 기업 평균으로 표준화한 뒤 연도쌍 평균을 본다.

    특정 연도에 전 기업의 변화율이 동시에 상승하면 표준화 후에도 양의 값이 남는다.
    """
    df = rates.copy()
    firm_mean = df.groupby(["corp_code", "section"])[metric].transform("mean")
    firm_std = (
        df.groupby(["corp_code", "section"])[metric].transform("std").replace(0, np.nan)
    )
    df["demeaned"] = df[metric] - firm_mean
    df["zscore"] = df["demeaned"] / firm_std
    agg = (
        df.groupby(["section", "pair"])
        .agg(
            n=("demeaned", "size"),
            mean_change=(metric, "mean"),
            mean_demeaned=("demeaned", "mean"),
            mean_z=("zscore", "mean"),
            share_positive=("demeaned", lambda s: float((s > 0).mean())),
        )
        .round(4)
        .reset_index()
    )
    return agg


# ---------------------------------------------------------------------------
# 4) 기업 간 공통 변경 문단
# ---------------------------------------------------------------------------


def common_changed_paragraphs(
    df: pd.DataFrame, cfg: Config
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """연도쌍 x 섹션별로 added/modified 문단을 모아 MinHash 로 유사쌍을 찾는다.

    Returns: (상위 쌍 테이블, 연도쌍별 공통문단 수 집계)
    """
    c = cfg["change"]
    min_chars = int(c["min_paragraph_chars"])
    # 서식개정 연도에는 유사쌍이 수만 개 나온다. CSV 폭발을 막기 위해 저장분만 자른다
    # (집계 n_common_pairs 는 자르기 전 전체 개수를 그대로 쓴다).
    max_stored = int(c.get("max_pairs_stored", 500))
    pairs_rows: list[dict[str, Any]] = []
    counts: list[dict[str, Any]] = []

    for (section,), sub in df.groupby(["section"]):
        by_corp = {
            corp: g.set_index("fy")["text"].to_dict()
            for corp, g in sub.groupby("corp_code")
        }
        years = sorted(sub["fy"].unique())
        for prev_fy, curr_fy in zip(years, years[1:]):
            items: list[tuple[str, str]] = []  # (corp_code, paragraph)
            for corp, byyear in by_corp.items():
                if prev_fy not in byyear or curr_fy not in byyear:
                    continue
                d = diff_paragraphs(
                    split_paragraphs(byyear[prev_fy], min_chars),
                    split_paragraphs(byyear[curr_fy], min_chars),
                )
                items.extend((corp, p) for p in d.changed if len(p) >= min_chars)

            if len(items) < 2:
                counts.append(
                    {
                        "section": section,
                        "pair": f"{prev_fy}->{curr_fy}",
                        "n_changed_paragraphs": len(items),
                        "n_common_pairs": 0,
                        "n_corps_involved": 0,
                    }
                )
                continue

            found = minhash_pairs(
                items,
                num_perm=int(c["minhash_num_perm"]),
                ngram=int(c["minhash_ngram"]),
                threshold=float(c["minhash_threshold"]),
                cross_group_only=True,
            )
            corps_involved = {items[int(i)][0] for i, j, _ in found} | {
                items[int(j)][0] for i, j, _ in found
            }
            counts.append(
                {
                    "section": section,
                    "pair": f"{prev_fy}->{curr_fy}",
                    "n_changed_paragraphs": len(items),
                    "n_common_pairs": len(found),
                    "n_corps_involved": len(corps_involved),
                }
            )
            for i, j, est in found[:max_stored]:
                pi, pj = items[int(i)], items[int(j)]
                pairs_rows.append(
                    {
                        "section": section,
                        "pair": f"{prev_fy}->{curr_fy}",
                        "corp_a": pi[0],
                        "corp_b": pj[0],
                        "est_jaccard": round(est, 4),
                        "para_a": pi[1][:300],
                        "para_b": pj[1][:300],
                    }
                )

    pairs_df = pd.DataFrame(pairs_rows)
    if not pairs_df.empty:
        pairs_df = pairs_df.sort_values("est_jaccard", ascending=False)
    return pairs_df, pd.DataFrame(counts)


# ---------------------------------------------------------------------------
# 그래프
# ---------------------------------------------------------------------------


def fig_change_by_year(
    rates: pd.DataFrame, out: Path, metric: str = "change_tfidf_cos"
) -> Path:
    g = rates.groupby(["section", "pair"])[metric].mean().reset_index()
    fig, ax = plt.subplots(figsize=(8, 5))
    for section, sub in g.groupby("section"):
        sub = sub.sort_values("pair")
        ax.plot(sub["pair"], sub[metric], marker="o", label=section)
    ax.set_title("연도별 평균 텍스트 변화율 (섹션별)")
    ax.set_xlabel("회계연도 쌍")
    ax.set_ylabel("변화율 (1 - TF-IDF 코사인)")
    ax.set_ylim(0, 1)
    ax.grid(alpha=0.3)
    ax.legend(title="섹션")
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    plt.close(fig)
    return out


def fig_common_paragraphs(counts: pd.DataFrame, out: Path) -> Path:
    fig, ax = plt.subplots(figsize=(8, 5))
    if counts.empty:
        ax.text(0.5, 0.5, "데이터 없음", ha="center", va="center")
    else:
        pivot = counts.pivot_table(
            index="pair", columns="section", values="n_common_pairs", aggfunc="sum"
        ).fillna(0)
        pivot = pivot.sort_index()
        x = np.arange(len(pivot.index))
        n_series = max(len(pivot.columns), 1)
        width = 0.8 / n_series
        for k, col in enumerate(pivot.columns):
            ax.bar(x + k * width - 0.4 + width / 2, pivot[col].values, width, label=col)
        ax.set_xticks(x)
        ax.set_xticklabels(pivot.index)
        ax.legend(title="섹션")
    ax.set_title("기업 간 거의 동일한 변경 문단 쌍 수 (서식 개정의 직접 증거)")
    ax.set_xlabel("회계연도 쌍")
    ax.set_ylabel("유사쌍 개수")
    ax.grid(alpha=0.3, axis="y")
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    plt.close(fig)
    return out


# ---------------------------------------------------------------------------
# 리포트
# ---------------------------------------------------------------------------


def _md(df: pd.DataFrame) -> str:
    return df.to_markdown(index=False) + "\n" if not df.empty else "_(데이터 없음)_\n"


def write_report(
    cfg: Config,
    rates: pd.DataFrame,
    spikes: pd.DataFrame,
    pairs: pd.DataFrame,
    counts: pd.DataFrame,
    out_dir: Path,
    *,
    mock: bool,
) -> Path:
    g0 = cfg["gate0"]
    top_n = int(cfg["change"]["top_pairs"])
    lines: list[str] = []
    add = lines.append

    add("# Phase 0 변화율 · 서식개정 스파이크 진단 (P0-b)\n")
    if mock:
        add(
            "> **경고 — MOCK 실행입니다.** 합성 데이터 결과이며 Gate 0 판정 근거가 아닙니다.\n"
        )
    add(
        f"- seed = {cfg['seed']}, 문자 {cfg['change']['tfidf_char_ngram'][0]}-gram TF-IDF"
    )
    add(
        f"- MinHash: num_perm={cfg['change']['minhash_num_perm']}, "
        f"문자 {cfg['change']['minhash_ngram']}-gram, "
        f"임계값 {cfg['change']['minhash_threshold']}"
    )
    add("")

    add("## 1. 연도쌍 x 섹션별 변화율 분포 (D1)\n")
    dist = (
        rates.groupby(["section", "pair"])
        .agg(
            n=("change_tfidf_cos", "size"),
            cos_mean=("change_tfidf_cos", "mean"),
            cos_median=("change_tfidf_cos", "median"),
            cos_q25=("change_tfidf_cos", lambda s: s.quantile(0.25)),
            cos_q75=("change_tfidf_cos", lambda s: s.quantile(0.75)),
            jac_median=("change_jaccard", "median"),
            lev_median=("change_levenshtein", "median"),
        )
        .round(4)
        .reset_index()
    )
    dist["IQR"] = (dist["cos_q75"] - dist["cos_q25"]).round(4)
    add(_md(dist))

    med = float(rates["change_tfidf_cos"].median()) if not rates.empty else 0.0
    ok = med >= g0["change_rate_median_min"]
    add(
        f"- 전체 변화율 중앙값 **{med:.3f}**\n"
    )
    add(
        f"> **판정하지 않는다.** 계획서의 변화율 중앙값 기준 "
        f"{g0['change_rate_median_min']} 은 **인접 연도**(t-1 → t) 를 전제로 한 값이다. "
        f"현재 표본은 {', '.join(str(y) for y in sorted(rates['fy_prev'].unique()))} "
        f"등 4년 간격이라 한 쌍에 4년치 변화가 누적된다. "
        f"같은 기준을 적용하면 반드시 통과하므로 판정 자체가 무의미하다. "
        f"이 기준은 Phase 1 에서 연속 연도 패널을 확보한 뒤 적용한다.\n"
    )

    add("## 2. 서식개정 스파이크 진단 (D2)\n")
    add(
        "기업별 변화율을 기업 평균으로 표준화(demean)한 뒤 연도쌍 평균을 본다. "
        "특정 연도에 전 기업이 동시에 상승했다면 `mean_demeaned` 가 뚜렷한 양수가 된다.\n"
    )
    add(_md(spikes))

    n_pairs_per_firm = (
        int(rates.groupby(["corp_code", "section"]).size().max())
        if not rates.empty
        else 0
    )
    if n_pairs_per_firm < 3:
        add(
            f"> **주의 — 이 통계는 현재 표본에서 검정력이 없습니다.** 이유는 두 가지입니다.\n"
        )
        add(
            f"> 1. 기업당 연도쌍이 {n_pairs_per_firm}개뿐이라, 기업 평균으로 demean 하면 "
            f"두 쌍의 값이 부호만 반대인 같은 크기가 되어 스파이크가 서로 상쇄됩니다.\n"
        )
        add(
            "> 2. **연도별 분해가 불가능합니다.** 각 페어가 4년 간격이라 그 사이에 일어난 "
            "여러 차례의 서식 개정이 한 값으로 뭉뚱그려집니다. 어느 해에 무엇이 바뀌어 "
            "변화율이 올라갔는지 이 표본으로는 원리적으로 분리할 수 없습니다. "
            "스파이크 탐지는 '특정 연도'를 짚어내는 검정인데, 표본이 그 해상도를 "
            "갖고 있지 않습니다.\n"
        )
        add(
            "> 따라서 D2 판정은 아래 **3절(기업 간 공통 변경 문단)** 을 1차 근거로 삼으세요. "
            "demean 기반 스파이크 검정은 Phase 1에서 연속 연도 패널을 확보한 뒤 "
            "다시 수행합니다.\n"
        )
    elif not spikes.empty:
        top = spikes.sort_values("mean_demeaned", ascending=False).iloc[0]
        add(
            f"- 가장 강한 상승: **{top['pair']} / {top['section']}** "
            f"(demeaned {top['mean_demeaned']:+.4f}, "
            f"상승 기업 비율 {top['share_positive']:.0%})\n"
        )

    add("## 3. 기업 간 공통 변경 문단 (서식 개정의 직접 증거)\n")
    add(
        "서로 다른 기업의 added/modified 문단을 모아 문자 "
        f"{cfg['change']['minhash_ngram']}-gram MinHash 로 유사도 "
        f"{cfg['change']['minhash_threshold']} 이상인 쌍을 찾는다. "
        "같은 기업 내부의 쌍은 제외한다.\n"
    )
    add(_md(counts))
    add(
        f"_`common_paragraphs.csv` 에는 (섹션 x 연도쌍)당 최대 "
        f"{cfg['change']['max_pairs_stored']}쌍만 저장한다. 위 표의 `n_common_pairs` 는 "
        f"자르기 전 전체 개수다._\n"
    )
    add(f"### 상위 {top_n}개 유사쌍\n")
    add(
        "> **이 표는 대표 표본이 아니다.** `est_jaccard` 내림차순 정렬이라 상단은 거의 "
        "전부 유사도 1.0 에 붙는 boilerplate 로 채워진다. 짧은 정형 문구일수록 완전 "
        "일치하기 쉬우므로 **boilerplate 가 과대표집**되고, 실질적으로 의미 있는 부분 "
        "일치 문단은 밀려난다. 분포를 보려면 정렬된 상위 N 이 아니라 "
        "`common_paragraphs*.csv` 전체를 쓸 것.\n"
    )
    if pairs.empty:
        add("_기업 간 거의 동일한 변경 문단이 발견되지 않았습니다._\n")
        add(
            "→ 스파이크가 없다면 **Phase 5(Template Filter)를 축소**할 수 있습니다. "
            "다만 이 경우 논문의 방법론 기여 하나가 사라집니다.\n"
        )
    else:
        show = pairs.head(top_n)[
            ["section", "pair", "corp_a", "corp_b", "est_jaccard", "para_a"]
        ].copy()
        show["para_a"] = show["para_a"].str.slice(0, 120) + "…"
        add(_md(show))
        add(
            "→ 기업 간 공통 변경 문단이 관측되었습니다. "
            "**Phase 5(Template Filter)의 필요성이 입증**됩니다.\n"
        )

    add("## 4. Gate 0 관련 판정\n")
    checks = pd.DataFrame(
        [
            {
                "항목": f"변화율 중앙값 {g0['change_rate_median_min']} 이상",
                "결과": "판정 불가",
                "값": f"{med:.3f} (4년 간격 표본 — 인접 연도 기준을 적용할 수 없음)",
            },
            {
                "항목": "기업 간 공통 변경 문단 관측",
                "결과": "PASS" if not pairs.empty else "FAIL",
                "값": f"{len(pairs)}쌍",
            },
        ]
    )
    add(_md(checks))
    add("![연도별 평균 변화율](fig1_change_by_year.png)\n")
    add("![기업 간 공통 변경 문단](fig2_common_paragraphs.png)\n")

    path = out_dir / "change_diagnostics.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    log.info("리포트 -> %s", path)
    return path


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Phase 0 변화율/스파이크 진단 (P0-b)")
    ap.add_argument("--config", default=None)
    ap.add_argument("--mock", action="store_true", help="P0 --mock 산출물을 사용")
    ap.add_argument("--limit", type=int, default=None, help="기업 수 제한")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    out_dir = cfg.dir("pilot", mock=args.mock)
    setup_logging(out_dir / "p0b.log", level=getattr(logging, args.log_level.upper()))
    set_seed(cfg)
    setup_korean_font()

    df = load_section_texts(out_dir)
    if args.limit:
        keep = sorted(df["corp_code"].unique())[: args.limit]
        df = df[df["corp_code"].isin(keep)]
    df = attach_names(df, out_dir)

    rates = compute_change_rates(df, cfg)
    rates.to_csv(out_dir / "change_rates.csv", index=False, encoding="utf-8-sig")
    log.info("change_rates.csv -> %d행", len(rates))

    spikes = spike_table(rates)
    pairs, counts = common_changed_paragraphs(df, cfg)
    pairs.to_csv(out_dir / "common_paragraphs.csv", index=False, encoding="utf-8-sig")
    counts.to_csv(
        out_dir / "common_paragraph_counts.csv", index=False, encoding="utf-8-sig"
    )

    fig_change_by_year(rates, out_dir / "fig1_change_by_year.png")
    fig_common_paragraphs(counts, out_dir / "fig2_common_paragraphs.png")

    write_report(cfg, rates, spikes, pairs, counts, out_dir, mock=args.mock)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
