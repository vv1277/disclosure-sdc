"""공시 원본 ZIP에서 본문 파일을 식별하고 디코딩한다 (프롬프트 P0 작업 3)."""
from __future__ import annotations

import logging
import re
import zipfile
from pathlib import Path

log = logging.getLogger(__name__)

_BODY_EXT = (".html", ".htm", ".xml")
# 첨부 성격이 강한 파일은 본문 후보에서 뺀다
_SKIP_HINT = re.compile(r"(감사보고서|연결감사|attach|첨부)", re.IGNORECASE)

_ENCODINGS = ("utf-8", "cp949", "euc-kr", "utf-16")


def decode_bytes(data: bytes) -> str:
    """DART 원본은 EUC-KR/CP949가 흔하다. 선언된 인코딩보다 실측을 우선한다."""
    m = re.search(rb'encoding\s*=\s*["\']([\w\-]+)["\']', data[:400], re.IGNORECASE)
    order = list(_ENCODINGS)
    if m:
        declared = m.group(1).decode("ascii", "ignore").lower()
        if declared in order:
            order.remove(declared)
        order.insert(0, declared)
    for enc in order:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def pick_body_file(zip_path: Path) -> tuple[str, str]:
    """ZIP 안에서 가장 큰 HTML/XML 파일을 본문으로 식별한다.

    Returns: (파일명, 디코딩된 문자열)
    Raises: ValueError — 후보가 없을 때
    """
    with zipfile.ZipFile(zip_path) as zf:
        cands = [
            i
            for i in zf.infolist()
            if not i.is_dir() and i.filename.lower().endswith(_BODY_EXT)
        ]
        if not cands:
            raise ValueError(f"HTML/XML 본문 후보 없음: {zip_path.name}")

        preferred = [i for i in cands if not _SKIP_HINT.search(i.filename)]
        pool = preferred or cands
        best = max(pool, key=lambda i: i.file_size)
        raw = zf.read(best.filename)

    return best.filename, decode_bytes(raw)
