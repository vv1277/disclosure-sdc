"""OpenDART 일일 호출 한도 관리.

OpenDART 는 하루 20,000건 제한이 있고, 초과하면 status 020 을 돌려준다.
한도를 다 쓰면 그날은 아무것도 못 하므로 여유를 두고 15,000 에서 멈춘다.
(계획서는 19,000 을 제안하지만, 재시도·재실행 여지를 더 남긴다)

카운터는 로컬 JSON 파일에 날짜별로 기록한다. 프로세스를 죽였다 다시 켜도
같은 날의 호출 수가 이어진다.
"""
from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

log = logging.getLogger(__name__)


class QuotaExceeded(RuntimeError):
    """오늘 몫을 다 썼다. 내일 이어서 하면 된다 (캐시가 있으므로 손실 없음)."""


class DailyQuota:
    def __init__(self, path: Path, limit: int = 15_000):
        self.path = Path(path)
        self.limit = int(limit)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._state = self._load()

    def _load(self) -> dict[str, int]:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                log.warning("호출 카운터 파일이 깨졌습니다. 0 에서 시작합니다: %s",
                            self.path)
        return {}

    def _save(self) -> None:
        self.path.write_text(json.dumps(self._state, indent=2), encoding="utf-8")

    @property
    def today(self) -> str:
        return date.today().isoformat()

    @property
    def used(self) -> int:
        return int(self._state.get(self.today, 0))

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    def check(self, n: int = 1) -> None:
        """n 건을 더 쓸 수 있는지 본다. 없으면 QuotaExceeded."""
        if self.used + n > self.limit:
            raise QuotaExceeded(
                f"오늘 OpenDART 호출 한도 {self.limit:,}건을 모두 사용했습니다 "
                f"(사용 {self.used:,}). 내일 같은 명령을 다시 실행하면 캐시를 건너뛰고 "
                f"남은 것부터 이어받습니다.")

    def consume(self, n: int = 1) -> int:
        self._state[self.today] = self.used + n
        self._save()
        return self._state[self.today]

    def summary(self) -> str:
        return f"{self.used:,} / {self.limit:,} (잔여 {self.remaining:,})"
