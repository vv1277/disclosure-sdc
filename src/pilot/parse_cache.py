"""파싱 결과 캐시.

P0-c/P0-d 는 같은 89건 문서를 legacy/fixed 두 방식으로 반복해서 쓴다.
DART 원본은 문서당 수 MB 라 매번 다시 파싱하면 실행마다 10분 이상 걸린다.
한 번 파싱한 결과를 parquet 로 남기고, 다음 실행부터는 그것을 읽는다.

주의: 파서를 고쳤으면 반드시 --rebuild-cache 로 다시 만들어야 한다.
      캐시에는 파서 버전 표식(PARSER_FINGERPRINT)이 함께 저장되며,
      표식이 다르면 자동으로 다시 파싱한다.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

import pandas as pd
from tqdm import tqdm

from src.parse import body, legacy_sections, paragraphs, sections
from src.parse.body import pick_body_file
from src.parse.legacy_sections import legacy_extract_sections
from src.parse.sections import SectionContent, extract_sections
from src.utils import textnorm
from src.utils.config import Config

log = logging.getLogger(__name__)

CACHE_NAME = "parsed_cache.parquet"

_COLUMNS = [
    "corp_code", "corp_name", "stock_code", "market", "size_tier", "fy",
    "rcept_no", "variant", "section", "section_name", "found", "has_body",
    "start_header", "end_header", "end_reason",
    "char_len_table", "text", "tables_text",
]


def parser_fingerprint() -> str:
    """파싱 결과에 영향을 주는 소스가 하나라도 바뀌면 값이 바뀐다.

    주의: 여기 빠진 모듈이 있으면 캐시가 조용히 낡은 채로 재사용된다.
    실제로 `textnorm` 을 빼두었다가 `clean_text()` 를 고쳤는데도 캐시가
    무효화되지 않아 옛 결과가 그대로 리포트에 실린 적이 있다.
    **파싱 경로에 모듈을 추가하면 이 목록에도 반드시 넣을 것.**
    """
    h = hashlib.sha256()
    for mod in (sections, legacy_sections, paragraphs, body, textnorm):
        h.update(Path(mod.__file__).read_bytes())
    return h.hexdigest()[:16]


def _to_row(meta: pd.Series, variant: str, sid: str, sc: SectionContent) -> dict[str, Any]:
    return {
        "corp_code": str(meta["corp_code"]), "corp_name": str(meta["corp_name"]),
        "stock_code": str(meta["stock_code"]), "market": str(meta.get("market", "")),
        "size_tier": str(meta.get("size_tier", "")), "fy": int(meta["fy"]),
        "rcept_no": str(meta["rcept_no"]), "variant": variant,
        "section": sid, "section_name": sc.name,
        "found": bool(sc.found), "has_body": bool(sc.has_body),
        "start_header": sc.start_header, "end_header": sc.end_header,
        "end_reason": sc.end_reason,
        "char_len_table": int(sc.char_len_table),
        "text": sc.text, "tables_text": sc.tables_text,
    }


def _from_row(row: pd.Series) -> SectionContent:
    """캐시 행 -> SectionContent. 문단은 text 를 개행으로 나눠 복원한다."""
    text = row["text"] or ""
    paras = [p for p in text.split("\n") if p]
    sc = SectionContent(
        section_id=row["section"], name=row["section_name"], found=bool(row["found"]),
        text=text, paragraphs=paras, tables_html=[],
        tables_text=row["tables_text"] or "",
        start_header=row["start_header"] or "", end_header=row["end_header"] or "",
        end_reason=row["end_reason"] or "", has_body=bool(row["has_body"]),
    )
    return sc


def build_cache(cfg: Config, index: pd.DataFrame, raw_dir: Path,
                out_dir: Path) -> pd.DataFrame:
    spec = cfg["sections"]
    pcfg = cfg.get("parse", {}) or {}
    parser = pcfg.get("parser", "html.parser")
    rows: list[dict[str, Any]] = []

    for meta in tqdm(list(index.itertuples(index=False)), desc="파싱(캐시 생성)", unit="doc"):
        meta = pd.Series(meta._asdict())
        zip_path = raw_dir / f"{meta['rcept_no']}.zip"
        if not zip_path.exists():
            log.warning("원본 ZIP 없음, 건너뜀: %s", zip_path.name)
            continue
        try:
            _, html = pick_body_file(zip_path)
        except Exception as exc:
            log.warning("본문 식별 실패 %s: %s", meta["rcept_no"], exc)
            continue
        legacy = legacy_extract_sections(html, spec, parser=parser)
        fixed = extract_sections(
            html, spec, parser=parser,
            require_terminator=bool(pcfg.get("require_terminator", True)),
            merge_min_chars=int(pcfg.get("merge_min_chars", 10)),
        )
        for variant, secs in (("legacy", legacy), ("fixed", fixed)):
            for sid, sc in secs.items():
                rows.append(_to_row(meta, variant, sid, sc))

    df = pd.DataFrame(rows, columns=_COLUMNS)
    df.attrs["fingerprint"] = parser_fingerprint()
    path = out_dir / CACHE_NAME
    df.assign(_fingerprint=parser_fingerprint()).to_parquet(path, index=False)
    log.info("파싱 캐시 저장 -> %s (%d행)", path, len(df))
    return df


def load_cache(cfg: Config, index: pd.DataFrame, raw_dir: Path, out_dir: Path,
               *, rebuild: bool = False) -> pd.DataFrame:
    path = out_dir / CACHE_NAME
    if not rebuild and path.exists():
        df = pd.read_parquet(path)
        cached_fp = df["_fingerprint"].iloc[0] if "_fingerprint" in df and len(df) else ""
        if cached_fp == parser_fingerprint():
            log.info("파싱 캐시 사용: %s (%d행)", path, len(df))
            return df.drop(columns=["_fingerprint"])
        log.warning("파서가 바뀌었다 (캐시 %s != 현재 %s). 다시 파싱한다.",
                    cached_fp, parser_fingerprint())
    return build_cache(cfg, index, raw_dir, out_dir)


def to_records(df: pd.DataFrame) -> list[dict[str, Any]]:
    """p0c 계열 함수들이 쓰는 [{meta, legacy, fixed}, ...] 형태로 되돌린다."""
    records: list[dict[str, Any]] = []
    meta_cols = ["corp_code", "corp_name", "stock_code", "market", "size_tier",
                 "fy", "rcept_no"]
    for rcept_no, g in df.groupby("rcept_no", sort=False):
        meta = pd.Series(g.iloc[0][meta_cols].to_dict())
        rec: dict[str, Any] = {"meta": meta, "legacy": {}, "fixed": {}}
        for row in g.itertuples(index=False):
            rec[row.variant][row.section] = _from_row(pd.Series(row._asdict()))
        records.append(rec)
    return records
