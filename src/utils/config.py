"""config.yaml 로딩과 시드 고정 (부록 A.3)."""
from __future__ import annotations

import logging
import os
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_ROOT / "config.yaml"

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Config:
    raw: dict[str, Any]
    path: Path

    def __getitem__(self, key: str) -> Any:
        return self.raw[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)

    def dir(self, key: str, *, mock: bool = False) -> Path:
        """paths.* 항목을 프로젝트 루트 기준 절대경로로 돌려주고 생성한다."""
        if mock and key == "pilot":
            key = "pilot_mock"
        p = PROJECT_ROOT / self.raw["paths"][key]
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def section_ids(self) -> list[str]:
        return sorted(self.raw["sections"].keys())


def load_config(path: str | Path | None = None) -> Config:
    p = Path(path) if path else DEFAULT_CONFIG
    with open(p, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return Config(raw=raw, path=p)


def set_seed(cfg: Config) -> int:
    """난수 시드를 고정하고 로그에 남긴다. 재현성 원칙 1번."""
    seed = int(cfg["seed"])
    random.seed(seed)
    np.random.seed(seed % (2**32))
    os.environ["PYTHONHASHSEED"] = str(seed)
    log.info("random seed fixed: %d (from %s)", seed, cfg.path.name)
    return seed
