"""OpenDART API 클라이언트.

TODO(API): 인증키가 아직 없다. 키가 발급되면 프로젝트 루트의 `.env` 에
    OPENDART_KEY=발급받은키
한 줄만 추가하면 된다. 이 파일은 수정할 필요가 없다.
키가 없으면 `MissingApiKey` 예외를 던지고, 파일럿 스크립트는 --mock 로 안내한다.

문서 0.2 / 프롬프트 P0 제약 반영:
  - 호출 사이 0.3초 sleep
  - 실패 시 3회 재시도 (지수 백오프)
  - 원본 ZIP은 data/raw/{rcept_no}.zip 으로 캐시, 있으면 재다운로드하지 않음
"""
from __future__ import annotations

import io
import logging
import os
import time
import zipfile
from pathlib import Path
from typing import Any, Iterable

import requests

from src.collect.quota import DailyQuota, QuotaExceeded
from src.utils.config import PROJECT_ROOT, Config

log = logging.getLogger(__name__)

# OpenDART가 status 필드로 돌려주는 코드 중 재시도해도 소용없는 것들
_FATAL_STATUS = {
    "010": "등록되지 않은 키",
    "011": "사용할 수 없는 키",
    "012": "접근할 수 없는 IP",
    "013": "조회된 데이터 없음",
    "020": "요청 제한 초과 (일 20,000건)",
    "021": "조회 가능한 회사 개수 초과",
    "100": "필드의 부적절한 값",
    "101": "부적절한 접근",
    "800": "시스템 점검 중",
    "900": "정의되지 않은 오류",
    "901": "사용자 계정의 개인정보 보유기간 만료",
}


class MissingApiKey(RuntimeError):
    """OPENDART_KEY 환경변수가 비어 있을 때."""


class DartApiError(RuntimeError):
    def __init__(self, status: str, message: str):
        self.status = status
        self.message = message
        super().__init__(f"OpenDART status={status}: {message}")


class NoData(DartApiError):
    """status 013 — 조회된 데이터 없음. 정상적인 '없음'이므로 실패로 세지 않는다."""


def load_api_key(cfg: Config) -> str:
    """환경변수 또는 .env 에서 키를 읽는다. 키를 코드/설정파일에 넣지 않는다."""
    env_name = cfg["api"]["key_env"]
    key = os.environ.get(env_name, "").strip()
    if not key:
        dotenv = PROJECT_ROOT / ".env"
        if dotenv.exists():
            for line in dotenv.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() == env_name:
                    key = v.strip().strip('"').strip("'")
                    break
    if not key:
        raise MissingApiKey(
            f"환경변수 {env_name} 가 비어 있습니다.\n"
            f"  TODO(API): opendart.fss.or.kr 에서 키를 발급한 뒤\n"
            f"    cp .env.example .env  &&  {env_name}=... 를 채우세요.\n"
            f"  키 없이 파이프라인만 점검하려면 --mock 옵션으로 실행하세요."
        )
    return key


class DartClient:
    def __init__(self, cfg: Config, *, api_key: str | None = None):
        api_cfg = cfg["api"]
        self.base_url: str = api_cfg["base_url"].rstrip("/")
        self.sleep_sec: float = float(api_cfg["sleep_sec"])
        self.max_retries: int = int(api_cfg["max_retries"])
        self.backoff_base: float = float(api_cfg["backoff_base_sec"])
        self.timeout: int = int(api_cfg["timeout_sec"])
        self.api_key = api_key if api_key is not None else load_api_key(cfg)
        self.raw_dir = cfg.dir("raw")
        self.session = requests.Session()
        self.n_calls = 0
        self.n_cache_hits = 0

        # 일일 한도. Phase 1 은 7,000건 이상을 받으므로 반드시 세야 한다.
        q = (cfg.get("phase1") or {}).get("api_quota") or {}
        self.quota = DailyQuota(
            PROJECT_ROOT / q.get("counter_file", "data/meta/dart_call_counter.json"),
            limit=int(q.get("daily_limit", api_cfg.get("daily_limit", 15_000))),
        )

    # ---------------- 저수준 ----------------

    def _request(self, endpoint: str, params: dict[str, Any]) -> requests.Response:
        url = f"{self.base_url}/{endpoint}"
        p = dict(params, crtfc_key=self.api_key)
        last_exc: Exception | None = None

        for attempt in range(self.max_retries):
            if attempt:
                wait = self.backoff_base * (2 ** (attempt - 1))
                log.info("retry %d/%d in %.1fs (%s)", attempt, self.max_retries - 1, wait, endpoint)
                time.sleep(wait)
            self.quota.check()
            try:
                resp = self.session.get(url, params=p, timeout=self.timeout)
                self.n_calls += 1
                self.quota.consume()
                time.sleep(self.sleep_sec)
                if resp.status_code >= 500:
                    last_exc = DartApiError(str(resp.status_code), "server error")
                    continue
                resp.raise_for_status()
                return resp
            except requests.RequestException as exc:  # 네트워크 계열만 재시도
                last_exc = exc
        raise DartApiError("network", f"{endpoint} 실패: {last_exc}")

    def _get_json(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        resp = self._request(endpoint, params)
        data = resp.json()
        status = str(data.get("status", "000"))
        if status == "013":
            raise NoData(status, _FATAL_STATUS["013"])
        if status != "000":
            raise DartApiError(status, _FATAL_STATUS.get(status, data.get("message", "")))
        return data

    # ---------------- 고수준 ----------------

    def fetch_corp_code_zip(self, dest: Path | None = None) -> Path:
        """고유번호 전체 파일(corpCode.zip)을 받아 캐시한다."""
        dest = dest or (self.raw_dir / "corpCode.zip")
        if dest.exists() and dest.stat().st_size > 0:
            log.info("corpCode.zip 캐시 사용: %s", dest)
            return dest
        resp = self._request("corpCode.xml", {})
        if resp.headers.get("content-type", "").startswith("application/json"):
            data = resp.json()
            raise DartApiError(str(data.get("status")), data.get("message", ""))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.content)
        log.info("corpCode.zip 저장: %s (%d bytes)", dest, dest.stat().st_size)
        return dest

    def search_reports(
        self,
        corp_code: str,
        bgn_de: str,
        end_de: str,
        *,
        pblntf_ty: str = "A",
        page_count: int = 100,
    ) -> list[dict[str, Any]]:
        """공시검색 API. 정기보고서(pblntf_ty='A') 목록을 돌려준다."""
        out: list[dict[str, Any]] = []
        page_no = 1
        while True:
            try:
                data = self._get_json(
                    "list.json",
                    {
                        "corp_code": corp_code,
                        "bgn_de": bgn_de,
                        "end_de": end_de,
                        "pblntf_ty": pblntf_ty,
                        "page_no": page_no,
                        "page_count": page_count,
                    },
                )
            except NoData:
                break
            out.extend(data.get("list", []))
            if page_no >= int(data.get("total_page", 1)):
                break
            page_no += 1
        return out

    def download_document(self, rcept_no: str, dest_dir: Path | None = None) -> Path:
        """공시서류원본파일 API. {dest_dir}/{rcept_no}.zip 으로 캐시한다."""
        base = Path(dest_dir) if dest_dir is not None else self.raw_dir
        base.mkdir(parents=True, exist_ok=True)
        dest = base / f"{rcept_no}.zip"
        if dest.exists() and dest.stat().st_size > 0:
            log.debug("원본 ZIP 캐시 사용: %s", dest)
            self.n_cache_hits += 1
            return dest
        resp = self._request("document.xml", {"rcept_no": rcept_no})
        body = resp.content
        # 실패 시 XML/JSON 에러 본문이 온다
        if not body[:2] == b"PK":
            raise DartApiError("document", f"ZIP이 아닌 응답: {body[:200]!r}")
        dest.write_bytes(body)
        return dest


def iter_zip_members(zip_path: Path) -> Iterable[tuple[str, bytes]]:
    """ZIP 안의 파일을 (이름, 바이트)로 순회한다."""
    with zipfile.ZipFile(io.BytesIO(Path(zip_path).read_bytes())) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            yield info.filename, zf.read(info)
