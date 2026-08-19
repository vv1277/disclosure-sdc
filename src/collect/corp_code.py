"""고유번호-종목코드-기업명 매핑 테이블 (프롬프트 P0 작업 1)."""
from __future__ import annotations

import logging
import zipfile
from pathlib import Path

import pandas as pd
from lxml import etree

log = logging.getLogger(__name__)

COLUMNS = ["corp_code", "corp_name", "stock_code", "modify_date"]


def parse_corp_code_zip(zip_path: Path) -> pd.DataFrame:
    """corpCode.zip -> DataFrame[corp_code, corp_name, stock_code, modify_date]."""
    with zipfile.ZipFile(zip_path) as zf:
        name = next(n for n in zf.namelist() if n.lower().endswith(".xml"))
        xml_bytes = zf.read(name)

    root = etree.fromstring(xml_bytes)
    rows = []
    for node in root.iter("list"):
        rows.append({c: (node.findtext(c) or "").strip() for c in COLUMNS})
    df = pd.DataFrame(rows, columns=COLUMNS)
    # 상장사만: stock_code가 6자리인 행
    df["is_listed"] = df["stock_code"].str.fullmatch(r"\d{6}").fillna(False)
    log.info("corp_code 매핑 %d건 (상장사 %d건)", len(df), int(df["is_listed"].sum()))
    return df


def build_mapping(zip_path: Path, out_path: Path) -> pd.DataFrame:
    df = parse_corp_code_zip(zip_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out_path, index=False)
    log.info("corp_code 매핑 저장 -> %s", out_path)
    return df


def lookup(mapping: pd.DataFrame, stock_code: str) -> dict[str, str] | None:
    """종목코드로 corp_code를 찾는다. 중복 시 modify_date가 가장 최근인 행."""
    hit = mapping.loc[mapping["stock_code"] == stock_code]
    if hit.empty:
        return None
    hit = hit.sort_values("modify_date", ascending=False)
    row = hit.iloc[0]
    return {
        "corp_code": row["corp_code"],
        "corp_name": row["corp_name"],
        "stock_code": row["stock_code"],
    }
