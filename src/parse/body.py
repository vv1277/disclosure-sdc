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

# 사업보고서 본문이면 이 중 하나는 반드시 들어 있다.
# 첨부(감사보고서)만 든 ZIP 을 가려내는 데 쓴다.
_BODY_MARKERS = ("사업의 내용", "회사의 개요", "재무에 관한 사항")


def decode_bytes(data: bytes) -> str:
    """DART 원본은 EUC-KR/CP949가 흔하다. 선언된 인코딩보다 실측을 우선한다."""
    m = re.search(rb'encoding\s*=\s*["\']([\w\-]+)["\']', data[:400], re.IGNORECASE)
    order = list(_ENCODINGS)
    # utf-16 은 임의의 바이트열도 치환 없이 '성공'시킨다 (거의 모든 바이트 쌍이
    # 유효한 코드포인트로 매핑된다). 그대로 두면 아래의 '치환 최소' 비교에서
    # 항상 이겨 한글이 통째로 깨진 결과를 고르게 된다. BOM 이 있을 때만 쓴다.
    if not data.startswith((b"\xff\xfe", b"\xfe\xff")):
        order = [e for e in order if e != "utf-16"]
    if m:
        declared = m.group(1).decode("ascii", "ignore").lower()
        if declared in order:
            order.remove(declared)
        order.insert(0, declared)
    # '처음 성공하는 인코딩'을 쓰면 안 된다. 1.9MB 파일에서 바이트 2개만 깨져
    # 있어도 strict 디코딩은 실패하는데, 그러면 뒤에 있는 올바른 인코딩까지
    # 건너뛰고 utf-8 errors=replace 로 떨어져 **9.5%가 통째로 깨진다**.
    # 실제로 하나투어 2020 이 그랬고 섹션 헤더를 하나도 못 찾았다
    # (선언은 utf-8 인데 실제로는 cp949, 깨진 바이트는 2개뿐이었다).
    # 그래서 후보를 전부 시도해 **치환문자가 가장 적은 것**을 고른다.
    best: tuple[str, int] | None = None
    for enc in order:
        try:
            return data.decode(enc)          # 완전 성공이면 즉시 채택
        except LookupError:
            continue
        except UnicodeDecodeError:
            pass
        try:
            t = data.decode(enc, errors="replace")
        except LookupError:
            continue
        n_bad = t.count("�")
        if best is None or n_bad < best[1]:
            best = (t, n_bad)

    if best is None:
        return data.decode("utf-8", errors="replace")
    log.warning("완전한 디코딩 실패 — 치환 %d자 (%.4f%%)",
                best[1], 100 * best[1] / max(1, len(best[0])))
    return best[0]


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

        # 가장 큰 파일이 본문일 확률이 높지만, 첨부(감사보고서)만 들어 있는
        # ZIP 도 있다 (첨부정정 문서가 그렇다). 표준 섹션명이 실제로 들어 있는
        # 파일을 우선하고, 없으면 크기로 정한다.
        for info in sorted(pool, key=lambda i: -i.file_size):
            text = decode_bytes(zf.read(info.filename))
            if any(k in text for k in _BODY_MARKERS):
                return info.filename, text

        biggest = max(pool, key=lambda i: i.file_size)
        return biggest.filename, decode_bytes(zf.read(biggest.filename))
