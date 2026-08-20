"""일일 호출 한도와 이어받기 (Phase 1 [B])."""
import json

import pytest

from src.collect.quota import DailyQuota, QuotaExceeded


def test_counter_persists_across_instances(tmp_path):
    """프로세스를 죽였다 켜도 같은 날의 사용량이 이어져야 한다."""
    p = tmp_path / "counter.json"
    q1 = DailyQuota(p, limit=100)
    for _ in range(7):
        q1.consume()
    q2 = DailyQuota(p, limit=100)          # 새 프로세스를 흉내
    assert q2.used == 7
    assert q2.remaining == 93


def test_check_raises_at_limit(tmp_path):
    q = DailyQuota(tmp_path / "c.json", limit=3)
    for _ in range(3):
        q.check()
        q.consume()
    with pytest.raises(QuotaExceeded):
        q.check()


def test_check_accounts_for_batch_size(tmp_path):
    q = DailyQuota(tmp_path / "c.json", limit=10)
    q.consume(8)
    q.check(2)                              # 8 + 2 = 10, 아직 통과
    with pytest.raises(QuotaExceeded):
        q.check(3)


def test_corrupted_counter_file_does_not_crash(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("{ 깨진 json", encoding="utf-8")
    q = DailyQuota(p, limit=5)
    assert q.used == 0                      # 0 에서 다시 시작
    q.consume()
    assert json.loads(p.read_text(encoding="utf-8"))[q.today] == 1


def test_limit_is_15000_not_19000():
    """계획서는 19,000 이지만 재시도 여지를 위해 15,000 으로 낮췄다."""
    from src.utils.config import load_config
    cfg = load_config()
    assert cfg["phase1"]["api_quota"]["daily_limit"] == 15_000
