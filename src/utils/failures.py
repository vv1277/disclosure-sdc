"""실패 로그 수집기. 부록 B.2: '실패 로그를 CSV로 남기기'."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pandas as pd

log = logging.getLogger(__name__)


class FailureLog:
    """개별 실패가 전체 실행을 중단시키지 않게 하고, 원인을 파일로 남긴다."""

    def __init__(self, path: Path):
        self.path = path
        self._rows: list[dict[str, Any]] = []

    def add(self, *, stage: str, key: str, reason: str, detail: str = "") -> None:
        self._rows.append(
            {"stage": stage, "key": key, "reason": reason, "detail": detail[:500]}
        )
        log.warning("[fail] %s | %s | %s", stage, key, reason)

    def __len__(self) -> int:
        return len(self._rows)

    def to_frame(self) -> pd.DataFrame:
        cols = ["stage", "key", "reason", "detail"]
        return pd.DataFrame(self._rows, columns=cols)

    def save(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.to_frame().to_csv(self.path, index=False, encoding="utf-8-sig")
        log.info("failure log -> %s (%d rows)", self.path, len(self._rows))
        return self.path

    def top_reasons(self, n: int = 10) -> pd.DataFrame:
        df = self.to_frame()
        if df.empty:
            return df
        return (
            df.groupby("reason").size().sort_values(ascending=False).head(n).reset_index(name="n")
        )
