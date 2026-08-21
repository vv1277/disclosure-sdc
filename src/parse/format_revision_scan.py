"""서식 개정 연도 특정 진단 (Phase 5 설계 근거 확보용).

이건 Phase 5 를 미리 하는 게 아니다. Template Filter 를 어느 연도에 맞춰
설계해야 하는지 근거를 얻는 것이고, **필터는 구현하지 않는다.**

배경
  parse_report 2절에서 S2 평균 문자 수가 2021년 4,468 -> 2022년 5,791 로
  한 해에 30% 뛰고 이후 유지된다. 점진 증가가 아니라 계단이다.
  S4 도 2017->2018 에 +35% 계단이 보인다. 서식 개정으로 항목이 강제
  추가된 신호다.

  Phase 0 에서는 표본이 2016/2020/2024 3개 연도뿐이라 이 검정을 할 수
  없었다 (기업당 연도쌍 2개 -> demean 하면 상쇄, 4년 간격이라 연도별 분해 불가).
  이제 N≈790/년, T=10 연속 패널이므로 식별이 된다.

실행
  python -m src.parse.format_revision_scan
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import re
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.parse.run_parse import _paths
from src.pilot.similarity import minhash_pairs
from src.utils.config import PROJECT_ROOT, Config, load_config, set_seed
from src.utils.logging_utils import setup_logging
from src.utils.plotting import plt, setup_korean_font

log = logging.getLogger("fmtscan")

SECTIONS = ("S1", "S2", "S3", "S4")
_NUM = re.compile(r"[\d][\d,\.]*")
MIN_PARA_CHARS = 30


def mask_numbers(text: str) -> str:
    """숫자를 <NUM> 으로 치환한다.

    금액·연도만 다른 같은 문장을 '다른 문단' 으로 세면 서식 개정 신호가
    희석된다. 서식 개정은 문장 틀이 같이 들어오는 것이므로 숫자는 지운다.
    """
    return _NUM.sub("<NUM>", text)


# --------------------------------------------------------------------------
# 1) 기업 고정효과 제거 후 연도별 문자 수
# --------------------------------------------------------------------------

def build_panel(cfg: Config) -> pd.DataFrame:
    dirs = _paths(cfg)
    m = pd.read_parquet(dirs["base"] / "parse_meta.parquet")
    idx = pd.read_parquet(
        PROJECT_ROOT / cfg["phase1"]["paths"]["meta"] / "filings_index.parquet")
    m = m.merge(idx[["rcept_no", "corp_code", "fy"]], on="rcept_no", how="inner")

    rows = []
    for s in SECTIONS:
        sub = m[m[f"{s}_found"] & (m[f"{s}_chars"] > 0)]
        rows.append(pd.DataFrame({
            "corp_code": sub["corp_code"], "fy": sub["fy"].astype(int),
            "section": s, "chars": sub[f"{s}_chars"],
            "log_chars": np.log(sub[f"{s}_chars"]),
        }))
    return pd.concat(rows, ignore_index=True)


def demeaned_by_year(panel: pd.DataFrame) -> pd.DataFrame:
    """기업 고정효과 제거 후 연도별 평균과 95% CI."""
    p = panel.copy()
    p["demeaned"] = p["log_chars"] - p.groupby(
        ["corp_code", "section"])["log_chars"].transform("mean")
    g = p.groupby(["section", "fy"])["demeaned"]
    out = g.agg(n="size", mean="mean", sd="std").reset_index()
    out["se"] = out["sd"] / np.sqrt(out["n"])
    out["ci_lo"] = out["mean"] - 1.96 * out["se"]
    out["ci_hi"] = out["mean"] + 1.96 * out["se"]
    return out.round(4)


# --------------------------------------------------------------------------
# 2) 계단 탐지
# --------------------------------------------------------------------------

def yoy_steps(panel: pd.DataFrame, z_threshold: float = 1.5) -> pd.DataFrame:
    """연도 간 log 차분의 횡단면 평균. 표본 평균에서 크게 벗어난 연도를 표시."""
    p = panel.sort_values(["corp_code", "section", "fy"]).copy()
    g = p.groupby(["corp_code", "section"])
    p["d_log"] = g["log_chars"].diff()
    p["prev_fy"] = g["fy"].shift()
    # 연속 연도만 (상장폐지·편입으로 끊긴 구간 제외)
    p = p[(p["fy"] - p["prev_fy"]) == 1]

    out = (p.groupby(["section", "fy"])["d_log"]
           .agg(n="size", mean_d_log="mean", sd="std",
                share_positive=lambda s: float((s > 0).mean()))
           .reset_index())
    out["se"] = out["sd"] / np.sqrt(out["n"])
    out["ci_lo"] = out["mean_d_log"] - 1.96 * out["se"]
    out["ci_hi"] = out["mean_d_log"] + 1.96 * out["se"]

    # 섹션별로 표본 평균 대비 z
    res = []
    for s, sub in out.groupby("section"):
        sub = sub.copy()
        mu, sd = sub["mean_d_log"].mean(), sub["mean_d_log"].std(ddof=1)
        sub["z_vs_own_history"] = ((sub["mean_d_log"] - mu) / sd).round(2) if sd else 0.0
        # 서식 개정은 항목이 **추가**될 때만 일어나지 않는다. 삭제되면 길이가
        # 한 해에 뚝 떨어진다. 실제로 S3 2021 이 z=-2.59, 증가 기업 비율 0.31 로
        # 강한 하락 계단인데, 상승만 보는 규칙으로는 놓친다.
        sub["is_step_up"] = (sub["z_vs_own_history"] >= z_threshold) & (
            sub["share_positive"] >= 0.65)
        sub["is_step_down"] = (sub["z_vs_own_history"] <= -z_threshold) & (
            sub["share_positive"] <= 0.35)
        sub["is_step"] = sub["is_step_up"] | sub["is_step_down"]
        sub["step_dir"] = np.where(sub["is_step_up"], "증가",
                                   np.where(sub["is_step_down"], "감소", ""))
        res.append(sub)
    return pd.concat(res, ignore_index=True).round(4)


# --------------------------------------------------------------------------
# 3) 계단 연도의 신규 문단 중 기업 간 공통도 높은 것
# --------------------------------------------------------------------------

def new_common_paragraphs(cfg: Config, section: str, fy: int,
                          top_n: int = 30, sample_n: int = 30,
                          max_corps: int = 300) -> tuple[list, list, dict]:
    """그 해 새로 등장한 문단 중 기업 간 공통도가 높은 것 상위 N + 무작위 N."""
    dirs = _paths(cfg)
    idx = pd.read_parquet(
        PROJECT_ROOT / cfg["phase1"]["paths"]["meta"] / "filings_index.parquet")
    cur = idx[idx.fy == fy].set_index("corp_code")["rcept_no"].to_dict()
    prev = idx[idx.fy == fy - 1].set_index("corp_code")["rcept_no"].to_dict()
    corps = sorted(set(cur) & set(prev))
    rng = random.Random(int(cfg["seed"]))
    if len(corps) > max_corps:
        corps = rng.sample(corps, max_corps)

    def _paras(rc: str) -> set[str]:
        f = dirs["sections"] / f"{rc}.json"
        if not f.exists():
            return set()
        d = json.loads(f.read_text(encoding="utf-8")).get(section, {})
        return {mask_numbers(p) for p in d.get("paragraphs", [])
                if len(p) >= MIN_PARA_CHARS}

    items: list[tuple[str, str]] = []
    for c in corps:
        new = _paras(cur[c]) - _paras(prev[c])
        items.extend((c, p) for p in new)

    stats = {"corps": len(corps), "new_paragraphs": len(items)}
    if len(items) < 2:
        return [], [], stats

    ch = cfg["change"]
    pairs = minhash_pairs(items, num_perm=int(ch["minhash_num_perm"]),
                          ngram=int(ch["minhash_ngram"]),
                          threshold=float(ch["minhash_threshold"]),
                          cross_group_only=True)
    # 문단별 '몇 개 기업과 겹치는가'
    partners: dict[int, set[str]] = {}
    for i, j, _ in pairs:
        i, j = int(i), int(j)
        partners.setdefault(i, set()).add(items[j][0])
        partners.setdefault(j, set()).add(items[i][0])
    stats["pairs"] = len(pairs)
    stats["paragraphs_with_partner"] = len(partners)

    ranked = sorted(partners.items(), key=lambda kv: -len(kv[1]))
    top = [{"n_corps": len(v), "corp": items[k][0], "text": items[k][1][:300]}
           for k, v in ranked[:top_n]]
    # 상위만 보면 편향되므로 무작위 표본도 낸다
    sample_idx = rng.sample(range(len(items)), min(sample_n, len(items)))
    sample = [{"n_corps": len(partners.get(k, set())), "corp": items[k][0],
               "text": items[k][1][:300]} for k in sample_idx]
    return top, sample, stats


# --------------------------------------------------------------------------
# 그래프
# --------------------------------------------------------------------------

def fig_lengths(dm: pd.DataFrame, out: Path) -> Path:
    fig, ax = plt.subplots(figsize=(9, 5.5))
    for s, sub in dm.groupby("section"):
        sub = sub.sort_values("fy")
        ax.plot(sub["fy"], sub["mean"], marker="o", label=s)
        ax.fill_between(sub["fy"], sub["ci_lo"], sub["ci_hi"], alpha=0.15)
    ax.axhline(0, color="grey", lw=0.8)
    ax.set_title("기업 고정효과 제거 후 연도별 섹션 길이 (log)")
    ax.set_xlabel("회계연도")
    ax.set_ylabel("log(문자수) — 기업 평균 대비")
    ax.legend(title="섹션")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    plt.close(fig)
    return out


def fig_yoy(steps: pd.DataFrame, out: Path) -> Path:
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(9, 7), sharex=True)
    for s, sub in steps.groupby("section"):
        sub = sub.sort_values("fy")
        a1.plot(sub["fy"], sub["mean_d_log"], marker="o", label=s)
        a2.plot(sub["fy"], sub["share_positive"], marker="o", label=s)
    a1.axhline(0, color="grey", lw=0.8)
    a1.set_ylabel("전년 대비 log 차분 평균")
    a1.set_title("연도별 텍스트 길이 변화와 증가 기업 비율")
    a1.grid(alpha=0.3); a1.legend(title="섹션", ncol=4)
    a2.axhline(0.5, color="grey", lw=0.8, ls="--")
    a2.axhline(0.8, color="crimson", lw=0.8, ls=":")
    a2.set_ylabel("전년 대비 증가 기업 비율")
    a2.set_xlabel("회계연도")
    a2.grid(alpha=0.3)
    a2.text(steps["fy"].min(), 0.81, "서식 개정 의심선 0.8", color="crimson", fontsize=9)
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    plt.close(fig)
    return out


# --------------------------------------------------------------------------

def _md(df: pd.DataFrame) -> str:
    return df.to_markdown(index=False) + "\n" if not df.empty else "_(없음)_\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="서식 개정 연도 진단")
    ap.add_argument("--config", default=None)
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    out_dir = PROJECT_ROOT / "results" / "phase2"
    out_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(out_dir / "format_scan.log",
                  level=getattr(logging, args.log_level.upper()))
    set_seed(cfg)
    setup_korean_font()

    panel = build_panel(cfg)
    dm = demeaned_by_year(panel)
    steps = yoy_steps(panel)
    dm.to_csv(out_dir / "section_length_demeaned.csv", index=False, encoding="utf-8-sig")
    steps.to_csv(out_dir / "yoy_steps.csv", index=False, encoding="utf-8-sig")

    fig_lengths(dm, out_dir / "fig_section_length_by_year.png")
    fig_yoy(steps, out_dir / "fig_yoy_change_share_positive.png")

    lines: list[str] = []
    add = lines.append
    add("# 서식 개정 연도 진단 (Phase 5 설계 근거)\n")
    add("Phase 5 를 미리 하는 게 아니다. Template Filter 를 **어느 연도에 맞춰** "
        "설계해야 하는지 근거를 얻는 것이고, 필터는 구현하지 않는다.\n")
    add(f"- 패널: 기업 {panel.corp_code.nunique():,}개 x 연도 "
        f"{panel.fy.min()}~{panel.fy.max()} (관측 {len(panel):,})")
    add("- Phase 0 에서는 3개 연도(4년 간격)뿐이라 이 검정이 불가능했다. "
        "이제 T=10 연속 패널이므로 식별이 된다.\n")

    add("## 1. 기업 고정효과 제거 후 연도별 길이\n")
    add("각 (기업, 섹션)의 log(문자수)를 기업 평균으로 demean 한 값의 연도별 평균과 95% CI.\n")
    add(_md(dm[["section", "fy", "n", "mean", "ci_lo", "ci_hi"]]))
    add("![섹션별 길이](fig_section_length_by_year.png)\n")

    add("## 2. 계단 탐지\n")
    add("연도 간 log 차분의 횡단면 평균. `share_positive` 는 전년 대비 길이가 "
        "늘어난 기업 비율이다. 서식 개정으로 항목이 강제 추가되면 이 값이 "
        "0.8 근처로 치솟는다.\n")
    add(_md(steps[["section", "fy", "n", "mean_d_log", "ci_lo", "ci_hi",
                   "share_positive", "z_vs_own_history", "step_dir"]]))
    add("![연도별 변화](fig_yoy_change_share_positive.png)\n")

    flagged = steps[steps["is_step"]].sort_values(["section", "fy"])
    add(f"### 계단으로 지목된 (섹션, 연도): {len(flagged)}건\n")
    add(_md(flagged[["section", "fy", "mean_d_log", "share_positive",
                     "z_vs_own_history", "step_dir"]]))
    add("> 상승 계단은 항목이 **추가**된 것, 하강 계단은 **삭제**된 것이다. "
        "둘 다 서식 개정 신호이므로 양쪽을 본다.\n")

    add("## 3. 계단 연도의 신규 공통 문단\n")
    add("그 해 새로 등장한 문단(전년에 없던 것) 중 **기업 간 공통도가 높은** 것. "
        "숫자는 `<NUM>` 으로 치환했다 — 금액·연도만 다른 같은 문장을 다른 문단으로 "
        "세면 신호가 희석되기 때문이다.\n")
    add("> 상위만 보면 편향되므로 **무작위 표본 30개**를 함께 낸다.\n")

    for r in flagged.itertuples():
        top, sample, st = new_common_paragraphs(cfg, r.section, int(r.fy))
        add(f"### {r.section} · {int(r.fy)}년 [{r.step_dir}] "
            f"(기업 {st.get('corps', 0)}개, 신규 문단 {st.get('new_paragraphs', 0):,}개, "
            f"유사쌍 {st.get('pairs', 0):,})\n")
        if top:
            add("**공통도 상위 30**\n")
            add(_md(pd.DataFrame(top).head(30)))
            add("**무작위 30**\n")
            add(_md(pd.DataFrame(sample).head(30)))
        else:
            add("_공통 문단 없음_\n")

    path = out_dir / "format_revision_scan.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    log.info("진단 리포트 -> %s", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
