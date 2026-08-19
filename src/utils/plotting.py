"""matplotlib 한글 폰트 설정. 제목/축라벨을 한글로 쓰기 위해 필요하다."""
from __future__ import annotations

import logging

import matplotlib

matplotlib.use("Agg")
import matplotlib.font_manager as fm  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

log = logging.getLogger(__name__)

# 우선순위: 나눔고딕 -> 윈도우 기본 -> macOS -> 리눅스
_CANDIDATES = (
    "NanumGothic", "NanumBarunGothic", "Malgun Gothic", "맑은 고딕",
    "AppleGothic", "Noto Sans CJK KR", "Noto Sans KR", "Gulim", "Batang",
)


def setup_korean_font() -> str | None:
    available = {f.name for f in fm.fontManager.ttflist}
    for name in _CANDIDATES:
        if name in available:
            plt.rcParams["font.family"] = name
            plt.rcParams["axes.unicode_minus"] = False   # 마이너스 기호 깨짐 방지
            log.info("한글 폰트: %s", name)
            return name
    log.warning(
        "한글 폰트를 찾지 못했습니다. 그래프의 한글이 깨질 수 있습니다. "
        "NanumGothic 설치를 권장합니다."
    )
    plt.rcParams["axes.unicode_minus"] = False
    return None
