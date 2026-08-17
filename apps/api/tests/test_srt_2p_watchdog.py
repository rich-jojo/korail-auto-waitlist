from __future__ import annotations

import importlib.util
from datetime import datetime
from pathlib import Path
from types import ModuleType


def load_watchdog() -> ModuleType:
    path = Path(__file__).parents[3] / "scripts/srt_2p_watchdog.py"
    spec = importlib.util.spec_from_file_location("srt_2p_watchdog", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


watchdog = load_watchdog()


def success_summary(*, general: bool = False, special: bool = False) -> dict[str, object]:
    return {
        "outcome": "success",
        "query": {"passenger_count": 2},
        "trains": [
            {
                "train_number": "354",
                "departure_time": "173600",
                "arrival_time": "192600",
                "general_available": general,
                "special_available": special,
            }
        ],
    }


def test_official_search_payload_requests_two_adults() -> None:
    payload = watchdog.build_search_payload("queue-key")

    assert payload["psgNum"] == 2
    assert payload["dptRsStnCd"] == "0015"
    assert payload["arvRsStnCd"] == "0551"
    assert payload["dptDt"] == "20260818"


def test_first_two_passenger_availability_alerts_then_stays_silent() -> None:
    now = datetime.fromisoformat("2026-08-17T12:30:00+09:00")

    message, state = watchdog.evaluate_summary(success_summary(general=True), {}, now)
    repeated_message, repeated_state = watchdog.evaluate_summary(
        success_summary(general=True), state, now
    )

    assert "SRT 성인 2명 좌석 가능" in message
    assert "SRT 354 17:36→19:26 일반실" in message
    assert state["available_keys"] == ["354:general_available"]
    assert repeated_message == ""
    assert repeated_state["available_keys"] == ["354:general_available"]


def test_sold_out_resets_episode_for_reopening() -> None:
    now = datetime.fromisoformat("2026-08-17T12:40:00+09:00")
    _, available_state = watchdog.evaluate_summary(success_summary(special=True), {}, now)
    sold_out_message, sold_out_state = watchdog.evaluate_summary(
        success_summary(), available_state, now
    )
    reopened_message, _ = watchdog.evaluate_summary(
        success_summary(special=True), sold_out_state, now
    )

    assert sold_out_message == ""
    assert sold_out_state["available_keys"] == []
    assert "특실" in reopened_message


def test_watchdog_runs_only_until_four_pm_on_monitoring_day() -> None:
    assert watchdog.within_window(datetime.fromisoformat("2026-08-17T12:00:00+09:00"))
    assert watchdog.within_window(datetime.fromisoformat("2026-08-17T15:59:59+09:00"))
    assert not watchdog.within_window(datetime.fromisoformat("2026-08-17T16:00:00+09:00"))
    assert not watchdog.within_window(datetime.fromisoformat("2026-08-18T12:00:00+09:00"))
