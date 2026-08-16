from __future__ import annotations

import importlib.util
from datetime import datetime
from pathlib import Path
from types import ModuleType


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


def test_watchdog_runs_only_in_requested_kst_window() -> None:
    assert watchdog.within_window(datetime.fromisoformat("2026-08-18T09:00:00+09:00"))
    assert watchdog.within_window(datetime.fromisoformat("2026-08-18T19:59:59+09:00"))
    assert not watchdog.within_window(datetime.fromisoformat("2026-08-18T08:59:59+09:00"))
    assert not watchdog.within_window(datetime.fromisoformat("2026-08-18T20:00:00+09:00"))
    assert not watchdog.within_window(datetime.fromisoformat("2027-08-18T09:00:00+09:00"))
