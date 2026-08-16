from __future__ import annotations

import importlib.util
from datetime import datetime
from pathlib import Path
from types import ModuleType

import pytest


def load_watchdog() -> ModuleType:
    path = Path(__file__).parents[3] / "scripts/korail_2p_watchdog.py"
    spec = importlib.util.spec_from_file_location("korail_2p_watchdog", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


watchdog = load_watchdog()


def success_summary(*, standard: str = "available", first: str = "sold_out") -> dict[str, object]:
    return {
        "outcome": "success",
        "query": {"passenger_count": 2},
        "trains": [
            {
                "train_number": "216",
                "train_type": "KTX",
                "departure_at": "2026-08-18T18:04:00+09:00",
                "arrival_at": "2026-08-18T20:00:00+09:00",
                "standard": standard,
                "first": first,
            }
        ],
    }


def test_first_two_passenger_availability_emits_actionable_notification() -> None:
    now = datetime.fromisoformat("2026-08-18T09:00:00+09:00")

    message, state = watchdog.evaluate_summary(success_summary(), {}, now)

    assert "코레일 성인 2명 좌석 가능" in message
    assert "KTX 216 18:04→20:00 일반실" in message
    assert "자동 예약·구매·결제는 하지 않았습니다." in message
    assert state["available_keys"] == ["216:standard"]


def test_unchanged_availability_is_silent() -> None:
    now = datetime.fromisoformat("2026-08-18T09:10:00+09:00")

    message, state = watchdog.evaluate_summary(
        success_summary(), {"available_keys": ["216:standard"]}, now
    )

    assert message == ""
    assert state["available_keys"] == ["216:standard"]


def test_sold_out_resets_episode_so_reopening_alerts_again() -> None:
    now = datetime.fromisoformat("2026-08-18T09:20:00+09:00")
    sold_out_message, sold_out_state = watchdog.evaluate_summary(
        success_summary(standard="sold_out"),
        {"available_keys": ["216:standard"]},
        now,
    )
    reopened_message, reopened_state = watchdog.evaluate_summary(
        success_summary(), sold_out_state, now
    )

    assert sold_out_message == ""
    assert sold_out_state["available_keys"] == []
    assert "KTX 216" in reopened_message
    assert reopened_state["available_keys"] == ["216:standard"]


def test_protection_response_opens_fifteen_minute_cooldown_once() -> None:
    now = datetime.fromisoformat("2026-08-18T09:30:00+09:00")

    first_message, state = watchdog.evaluate_summary(
        {"outcome": "provider_access_restricted"}, {}, now
    )
    repeated_message, _ = watchdog.evaluate_summary(
        {"outcome": "provider_access_restricted"}, state, now
    )

    assert "우회하지 않고 15분" in first_message
    assert watchdog._cooldown_active(state, now) is True
    assert repeated_message == ""


def test_maintenance_source_unavailable_opens_five_minute_cooldown() -> None:
    now = datetime.fromisoformat("2026-08-18T09:30:00+09:00")

    message, state = watchdog.evaluate_summary(
        {"outcome": "source_unavailable", "trigger": "maintenance_page"}, {}, now
    )

    assert "점검·서비스 중단" in message
    assert watchdog._cooldown_active(state, now) is True
    assert state["cooldown_until"] == "2026-08-18T09:35:00+09:00"


def test_rate_limit_opens_fifteen_minute_cooldown() -> None:
    now = datetime.fromisoformat("2026-08-18T09:30:00+09:00")

    message, state = watchdog.evaluate_summary({"outcome": "rate_limited"}, {}, now)

    assert "호출 제한" in message
    assert watchdog._cooldown_active(state, now) is True
    assert state["cooldown_until"] == "2026-08-18T09:45:00+09:00"


def test_transient_error_preserves_last_authoritative_availability() -> None:
    now = datetime.fromisoformat("2026-08-18T09:30:00+09:00")
    prior = {"available_keys": ["216:standard"]}

    _, failed_state = watchdog.evaluate_summary(
        {"outcome": "source_unavailable", "trigger": None}, prior, now
    )
    recovered_message, recovered_state = watchdog.evaluate_summary(
        success_summary(), failed_state, now
    )

    assert failed_state["available_keys"] == ["216:standard"]
    assert recovered_message == ""
    assert recovered_state["available_keys"] == ["216:standard"]


def test_minimum_interval_blocks_duplicate_attempts_but_allows_ten_minute_tick() -> None:
    state = {"last_attempt_at": "2026-08-18T09:00:00+09:00"}

    assert watchdog._minimum_interval_active(
        state, datetime.fromisoformat("2026-08-18T09:08:59+09:00")
    )
    assert not watchdog._minimum_interval_active(
        state, datetime.fromisoformat("2026-08-18T09:09:00+09:00")
    )


def test_exclusive_run_lock_rejects_concurrent_process(tmp_path: Path) -> None:
    lock_path = tmp_path / "watchdog.lock"

    with watchdog.exclusive_run_lock(lock_path) as first:
        with watchdog.exclusive_run_lock(lock_path) as second:
            assert first is True
            assert second is False


def test_main_skips_provider_call_inside_minimum_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime.fromisoformat("2026-08-18T09:08:59+09:00")
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(watchdog, "STATE_PATH", state_path)
    monkeypatch.setattr(watchdog, "LOCK_PATH", tmp_path / "watchdog.lock")
    watchdog.save_state({"last_attempt_at": "2026-08-18T09:00:00+09:00"}, state_path)

    def fail_query(_now: datetime) -> dict[str, object]:
        raise AssertionError("provider call must be skipped")

    monkeypatch.setattr(watchdog, "run_query", fail_query)

    assert watchdog.main(now) == 0


def test_watchdog_runs_only_in_requested_kst_window() -> None:
    assert watchdog.within_window(datetime.fromisoformat("2026-08-18T09:00:00+09:00"))
    assert watchdog.within_window(datetime.fromisoformat("2026-08-18T19:59:59+09:00"))
    assert not watchdog.within_window(datetime.fromisoformat("2026-08-18T08:59:59+09:00"))
    assert not watchdog.within_window(datetime.fromisoformat("2026-08-18T20:00:00+09:00"))
    assert not watchdog.within_window(datetime.fromisoformat("2027-08-18T09:00:00+09:00"))
