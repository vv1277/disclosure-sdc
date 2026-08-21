"""Phase 2 — 전량 파싱 (병렬).

설계
  - 문서 단위로 **완전히 독립**이다. 워커 사이에 공유 상태가 없다.
    각 워커는 rcept_no 하나를 받아 자기 파일만 쓴다.
  - 산출물을 rcept_no 단위로 저장하므로 중단하면 그 문서만 다시 하면 된다.
  - 병렬화가 결과를 바꾸면 안 된다. `--verify` 로 단일 프로세스 결과와
    바이트 단위로 비교한다.

Phase 0 에서 확정된 사항이 전부 적용된다
  - 편집기 잔재 3종 제거 (위젯 라벨 / &cr 엔티티 / .dsl 파일명) — clean_text()
  - 잔재 제거 **전** 텍스트도 raw_text/ 에 보관 (논문 Appendix A-11)
  - 커스텀 컨테이너 태그 안의 <table> 도 표로 분리 — iter_blocks()
  - 10자 미만 문단 병합 — merge_short_paragraphs()
  - 표 분리 시 원래 문단 위치(block_index) 기록, 캡션과 표를 연결 — TableRef

산출 (data/corpus/)
  sections/{rcept_no}.json     섹션별 텍스트·문단·표 메타
  raw_text/{rcept_no}.json     잔재 제거 전 텍스트
  tables/{rcept_no}.json       표 HTML·캡션·위치
  parse_meta.parquet           문서별 진단 지표

실행
  python -m src.parse.run_parse --limit 200 --verify   # 병렬=단일 검증
  python -m src.parse.run_parse                        # 전량
"""
from __future__ import annotations

import argparse
import json
import logging
import multiprocessing as mp
import os
import re
import warnings
from pathlib import Path
from typing import Any

import pandas as pd
from tqdm import tqdm

from src.parse.body import pick_body_file
from src.parse.paragraphs import merge_short_paragraphs
from src.parse.sections import END_REASON_EOF, extract_sections
from src.utils.textnorm import strip_artifacts
from src.utils.config import PROJECT_ROOT, Config, load_config, set_seed
from src.utils.logging_utils import setup_logging

log = logging.getLogger("parse")

# 잔재 3종 — 제거 후 0건이어야 한다 (grep 검증용과 같은 패턴)
ARTIFACT_PATTERNS = {
    "widget": re.compile(r"◆\s*click\s*◆", re.I),
    "cr_entity": re.compile(r"&cr(?![A-Za-z0-9])", re.I),
    "dsl_file": re.compile(r"\d{4,6}\s*#\s*\*?\s*_\S+?\.dsl", re.I),
}

# 레이아웃 표: 1~2행 또는 1열이면서 셀이 길다 -> 표가 아니라 서식용 껍데기
LAYOUT_CELL_MIN_CHARS = 200


# 문서 1건 파싱의 실측 최대 메모리(가장 큰 문서 기준). 워커 수를 이걸로 정한다.
PEAK_MB_PER_DOC = 600


def default_workers(reserve_gb: float = 1.5) -> int:
    """가용 메모리로 워커 수를 정한다.

    코어 수만 보고 정하면 안 된다. 17코어로 돌렸다가 MemoryError 가 났다.
    이 작업은 CPU 가 아니라 메모리에 묶여 있다 (문서당 최대 480MB).
    """
    cores = max(1, (os.cpu_count() or 2) - 1)
    try:
        import psutil
        free_gb = psutil.virtual_memory().available / 1e9
    except Exception:
        free_gb = 4.0                      # psutil 이 없으면 보수적으로
    budget = max(1, int((free_gb - reserve_gb) * 1000 / PEAK_MB_PER_DOC))
    return max(1, min(cores, budget))


def _paths(cfg: Config) -> dict[str, Path]:
    base = PROJECT_ROOT / cfg["phase1"]["paths"]["corpus"]
    d = {k: base / k for k in ("sections", "raw_text", "tables")}
    d["base"] = base
    return d


def _is_layout_table(html: str) -> bool:
    """1~2행 또는 1열이면서 셀 200자 이상이면 레이아웃 표."""
    from src.parse.officers import table_to_grid
    grid = table_to_grid(html, max_rows=8)
    if not grid:
        return False
    n_rows, n_cols = len(grid), max(len(r) for r in grid)
    if n_rows > 2 and n_cols > 1:
        return False
    return any(len(c) >= LAYOUT_CELL_MIN_CHARS for r in grid for c in r)


def _derive_clean(raw_secs: dict, merge_min_chars: int) -> dict:
    """잔재를 남긴 추출 결과에서 정제본을 만든다.

    잔재 제거는 문단 단위 문자열 치환이라 블록 경계를 바꾸지 않는다.
    따라서 다시 파싱하지 않고 여기서 파생시켜도 결과가 같다
    (tests/test_run_parse.py 가 이 동치를 고정한다).
    """
    from copy import copy

    out = {}
    for sid, sc in raw_secs.items():
        keep_p, keep_i = [], []
        for p, i in zip(sc.paragraphs, sc.paragraph_indices):
            c = strip_artifacts(p)
            if c:
                keep_p.append(c)
                keep_i.append(i)
        if merge_min_chars:
            merged = merge_short_paragraphs(keep_p, min_chars=merge_min_chars)
            # 병합으로 문단 수가 줄면 위치는 각 병합 그룹의 첫 블록으로 맞춘다
            keep_i = keep_i[:len(merged)] if len(merged) <= len(keep_i) else keep_i
            keep_p = merged
        new = copy(sc)
        new.paragraphs = keep_p
        new.paragraph_indices = keep_i
        new.text = "\n".join(keep_p)
        new.tables = [copy(t) for t in sc.tables]
        for t in new.tables:
            t.text = strip_artifacts(t.text)
            t.caption = strip_artifacts(t.caption)
        new.tables_text = "\n".join(t.text for t in new.tables if t.text)
        new.tables_html = [t.html for t in new.tables]
        new.has_body = bool(new.text)
        new.found = sc.found and bool(new.text)
        out[sid] = new
    return out


def parse_one(task: tuple[str, str, dict]) -> dict[str, Any]:
    """워커 진입점. 공유 상태 없이 자기 문서만 처리하고 자기 파일만 쓴다."""
    warnings.filterwarnings("ignore")
    rcept_no, zip_path, opt = task
    out: dict[str, Any] = {"rcept_no": rcept_no, "ok": False, "error": ""}
    try:
        _, html = pick_body_file(Path(zip_path))
    except Exception as exc:
        out["error"] = f"body:{type(exc).__name__}:{exc}"
        return out

    spec = opt["sections"]
    try:
        # **한 번만 파싱한다.** 잔재를 남긴 채로 뽑은 뒤, 정제본은 거기서 파생시킨다.
        #   - 두 번 파싱하면 메모리와 시간이 두 배가 된다 (문서당 최대 480MB).
        #   - 그리고 두 번 파싱하면 '잔재 제거 전' 이 실제로는 제거된 텍스트가 된다.
        #     둘 다 clean_text 를 거치기 때문이다. 실제로 그 버그가 있었다.
        raw_secs = extract_sections(
            html, spec, parser=opt["parser"],
            require_terminator=opt["require_terminator"],
            merge_min_chars=0, remove_artifacts=False)
    except Exception as exc:
        out["error"] = f"section:{type(exc).__name__}:{exc}"
        return out
    finally:
        html = ""       # 4MB 문자열을 빨리 놓아준다

    secs = _derive_clean(raw_secs, opt["merge_min_chars"])

    dirs = {k: Path(v) for k, v in opt["dirs"].items()}
    sec_payload, tbl_payload, raw_payload = {}, {}, {}
    diag: dict[str, Any] = {"rcept_no": rcept_no}
    n_layout = n_caption_orphan = 0

    for sid, sc in secs.items():
        tables = []
        for t in sc.tables:
            layout = _is_layout_table(t.html)
            n_layout += int(layout)
            tables.append({
                "block_index": t.block_index, "order": t.order_in_section,
                "caption": t.caption, "caption_block_index": t.caption_block_index,
                "n_chars": t.n_chars, "is_layout": layout, "html": t.html,
            })
        # 고아 캡션: 표로 넘어간 캡션이 본문 문단에도 그대로 남아 있는 경우
        caps = {t.caption for t in sc.tables if t.caption}
        orphan_chars = sum(len(p) for p in sc.paragraphs if p in caps)
        n_caption_orphan += orphan_chars

        sec_payload[sid] = {
            "name": sc.name, "found": sc.found, "has_body": sc.has_body,
            "start_header": sc.start_header,
            "end_header": sc.end_header or END_REASON_EOF,
            "end_reason": sc.end_reason,
            "text": sc.text, "paragraphs": sc.paragraphs,
            "paragraph_indices": sc.paragraph_indices,
        }
        tbl_payload[sid] = tables
        if opt["keep_raw_text"]:
            raw_payload[sid] = {"text": raw_secs[sid].text,
                                "n_paragraphs": len(raw_secs[sid].paragraphs)}

        diag[f"{sid}_found"] = bool(sc.found)
        diag[f"{sid}_chars"] = sc.char_len_text
        diag[f"{sid}_paras"] = sc.n_paragraphs
        diag[f"{sid}_table_chars"] = sc.char_len_table
        diag[f"{sid}_n_tables"] = len(sc.tables)
        diag[f"{sid}_end_reason"] = sc.end_reason
        diag[f"{sid}_under10"] = sum(1 for p in sc.paragraphs if len(p) < 10)

    body_all = "\n".join(s["text"] for s in sec_payload.values())
    for name, pat in ARTIFACT_PATTERNS.items():
        diag[f"artifact_{name}"] = len(pat.findall(body_all))
    diag["n_layout_tables"] = n_layout
    diag["caption_orphan_chars"] = n_caption_orphan
    diag["n_found"] = sum(1 for s in sec_payload.values() if s["found"])

    def _dump(d: Path, payload: dict) -> None:
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{rcept_no}.json").write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=1),
            encoding="utf-8")

    _dump(dirs["sections"], sec_payload)
    _dump(dirs["tables"], tbl_payload)
    if raw_payload:
        _dump(dirs["raw_text"], raw_payload)

    out.update({"ok": True, "diag": diag})
    return out


def build_tasks(cfg: Config, idx: pd.DataFrame, dirs: dict[str, Path],
                *, force: bool) -> list[tuple[str, str, dict]]:
    p1 = cfg["phase1"]
    pcfg = cfg.get("parse", {}) or {}
    filings = PROJECT_ROOT / p1["paths"]["filings"]
    opt = {
        "sections": cfg["sections"],
        "parser": pcfg.get("parser", "html.parser"),
        "require_terminator": bool(pcfg.get("require_terminator", True)),
        "merge_min_chars": int(pcfg.get("merge_min_chars", 10)),
        "keep_raw_text": bool(p1.get("keep_raw_text", True)),
        "dirs": {k: str(v) for k, v in dirs.items() if k != "base"},
    }
    tasks = []
    for r in idx.itertuples(index=False):
        zp = filings / f"{r.rcept_no}.zip"
        if not zp.exists():
            continue
        if not force and (dirs["sections"] / f"{r.rcept_no}.json").exists():
            continue                      # 이미 처리됨 -> 재개
        tasks.append((str(r.rcept_no), str(zp), opt))
    return tasks


def run(tasks: list, workers: int) -> list[dict[str, Any]]:
    if workers <= 1:
        return [parse_one(t) for t in tqdm(tasks, desc="파싱(단일)", unit="doc")]
    with mp.Pool(workers) as pool:
        return list(tqdm(pool.imap_unordered(parse_one, tasks, chunksize=8),
                         total=len(tasks), desc=f"파싱({workers}코어)", unit="doc"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Phase 2 전량 파싱")
    ap.add_argument("--config", default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--force", action="store_true", help="이미 처리된 문서도 다시")
    ap.add_argument("--verify", action="store_true",
                    help="병렬 결과가 단일 프로세스와 바이트 단위로 같은지 검증")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    dirs = _paths(cfg)
    dirs["base"].mkdir(parents=True, exist_ok=True)
    setup_logging(dirs["base"] / "parse.log",
                  level=getattr(logging, args.log_level.upper()))
    set_seed(cfg)

    meta = PROJECT_ROOT / cfg["phase1"]["paths"]["meta"]
    idx = pd.read_parquet(meta / "filings_index.parquet")
    if args.limit:
        idx = idx.sort_values(["fy", "corp_code"], ascending=[False, True]).head(args.limit)

    workers = args.workers or default_workers()
    log.info("워커 %d (코어 %s)", workers, os.cpu_count())

    if args.verify:
        return _verify(cfg, idx, dirs, workers)

    tasks = build_tasks(cfg, idx, dirs, force=args.force)
    log.info("대상 %d건 (이미 처리된 것은 건너뜀), 워커 %d", len(tasks), workers)
    results = run(tasks, workers)

    ok = [r for r in results if r["ok"]]
    bad = [r for r in results if not r["ok"]]
    if ok:
        diag = pd.DataFrame([r["diag"] for r in ok])
        path = dirs["base"] / "parse_meta.parquet"
        if path.exists() and not args.force:
            diag = pd.concat([pd.read_parquet(path), diag]).drop_duplicates(
                "rcept_no", keep="last")
        diag.to_parquet(path, index=False)
        log.info("parse_meta -> %s (%d행)", path, len(diag))
    if bad:
        pd.DataFrame(bad).to_csv(dirs["base"] / "parse_failures.csv",
                                 index=False, encoding="utf-8-sig")
        log.warning("실패 %d건 -> parse_failures.csv", len(bad))
    log.info("완료: 성공 %d / 실패 %d", len(ok), len(bad))
    return 0


def _verify(cfg: Config, idx: pd.DataFrame, dirs: dict[str, Path],
            workers: int) -> int:
    """병렬 결과와 단일 프로세스 결과를 바이트 단위로 비교한다."""
    import hashlib
    import shutil
    import tempfile

    def _digests(base: Path) -> dict[str, str]:
        out = {}
        for f in sorted((base / "sections").glob("*.json")):
            out[f.name] = hashlib.sha256(f.read_bytes()).hexdigest()
        return out

    results = {}
    for label, w in (("serial", 1), ("parallel", workers)):
        tmp = Path(tempfile.mkdtemp(prefix=f"parseverify_{label}_"))
        d = {k: tmp / k for k in ("sections", "raw_text", "tables")}
        d["base"] = tmp
        tasks = build_tasks(cfg, idx, d, force=True)
        log.info("[%s] %d건, 워커 %d", label, len(tasks), w)
        run(tasks, w)
        results[label] = (_digests(tmp), tmp)

    a, b = results["serial"][0], results["parallel"][0]
    same_keys = set(a) == set(b)
    diff = [k for k in a if k in b and a[k] != b[k]]
    log.info("파일 수 단일 %d / 병렬 %d, 키 일치 %s, 내용 불일치 %d건",
             len(a), len(b), same_keys, len(diff))
    for _, tmp in results.values():
        shutil.rmtree(tmp, ignore_errors=True)

    if same_keys and not diff:
        log.info("검증 통과 — 병렬 결과가 단일 프로세스와 바이트 단위로 동일하다.")
        return 0
    log.error("검증 실패 — 병렬화가 결과를 바꾼다. 불일치 예: %s", diff[:5])
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
