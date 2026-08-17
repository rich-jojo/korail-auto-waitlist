#!/usr/bin/env python3
"""Low-frequency, read-only SRT two-passenger availability watchdog."""

from __future__ import annotations

import fcntl
import io
import json
import sys
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
TARGET_DATE = "20260818"
MONITOR_DATE = "2026-08-17"
START_HOUR = 12
END_HOUR = 16
DEPARTURE_FROM = "170000"
DEPARTURE_TO = "200000"
ORIGIN = "동대구"
DESTINATION = "수서"
ORIGIN_CODE = "0015"
DESTINATION_CODE = "0551"
STATE_ROOT = Path.home() / ".local/state/srt-2p-watchdog/dongdaegu-suseo"
STATE_PATH = STATE_ROOT / "state.json"
LOCK_PATH = STATE_ROOT / "watchdog.lock"
MIN_QUERY_INTERVAL_SECONDS = 540
BOOKING_URL = "https://etk.srail.kr/hpg/hra/01/selectScheduleList.do?pageId=TK0101010000"


def load_state(path: Path = STATE_PATH) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def save_state(state: dict[str, Any], path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    temporary.replace(path)


@contextmanager
def exclusive_run_lock(path: Path = LOCK_PATH) -> Iterator[bool]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a+", encoding="utf-8") as handle:
        path.chmod(0o600)
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def within_window(now: datetime) -> bool:
    kst_now = now.astimezone(KST)
    return (
        kst_now.date().isoformat() == MONITOR_DATE
        and START_HOUR <= kst_now.hour < END_HOUR
    )


def minimum_interval_active(state: dict[str, Any], now: datetime) -> bool:
    value = state.get("last_attempt_at")
    if not isinstance(value, str):
        return False
    try:
        previous = datetime.fromisoformat(value)
    except ValueError:
        return False
    return (now - previous).total_seconds() < MIN_QUERY_INTERVAL_SECONDS


def build_search_payload(netfunnel_key: str) -> dict[str, object]:
    return {
        "chtnDvCd": "1",
        "arriveTime": "N",
        "seatAttCd": "015",
        "psgNum": 2,
        "trnGpCd": 109,
        "stlbTrnClsfCd": "05",
        "dptDt": TARGET_DATE,
        "dptTm": DEPARTURE_FROM,
        "arvRsStnCd": DESTINATION_CODE,
        "dptRsStnCd": ORIGIN_CODE,
        "netfunnelKey": netfunnel_key,
    }


def available_entries(summary: dict[str, Any]) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    trains = summary.get("trains")
    if not isinstance(trains, list):
        return entries
    for train in trains:
        if not isinstance(train, dict):
            continue
        number = str(train.get("train_number", "")).strip()
        departure = str(train.get("departure_time", "")).strip()
        arrival = str(train.get("arrival_time", "")).strip()
        for field, label in (("general_available", "일반실"), ("special_available", "특실")):
            if train.get(field) is not True:
                continue
            entries.append(
                {
                    "key": f"{number}:{field}",
                    "number": number,
                    "departure": departure,
                    "arrival": arrival,
                    "seat_label": label,
                }
            )
    return entries


def availability_message(entries: list[dict[str, str]]) -> str:
    lines = [
        "🚄 SRT 성인 2명 좌석 가능",
        "동대구→수서 · 2026-08-18 · 출발 17:00~20:00",
    ]
    for entry in entries:
        lines.append(
            f"- SRT {entry['number']} "
            f"{entry['departure'][:2]}:{entry['departure'][2:4]}→"
            f"{entry['arrival'][:2]}:{entry['arrival'][2:4]} {entry['seat_label']}"
        )
    lines.extend(
        [
            "성인 2명 동시 조회 결과입니다.",
            f"예매: {BOOKING_URL}",
            "자동 예약·구매·결제는 하지 않았습니다.",
        ]
    )
    return "\n".join(lines)


def evaluate_summary(
    summary: dict[str, Any], state: dict[str, Any], now: datetime
) -> tuple[str, dict[str, Any]]:
    entries = available_entries(summary)
    current_keys = sorted(entry["key"] for entry in entries)
    previous_keys = set(state.get("available_keys", []))
    newly_available = [entry for entry in entries if entry["key"] not in previous_keys]
    new_state = dict(state)
    new_state.update(
        {
            "available_keys": current_keys,
            "last_checked_at": now.isoformat(),
            "last_outcome": str(summary.get("outcome", "unknown")),
        }
    )
    return (
        availability_message(newly_available) if newly_available else "",
        new_state,
    )


def query_srt() -> dict[str, Any]:
    from SRT import SRT, constants
    from SRT.response_data import SRTResponseData
    from SRT.train import SRTTrain

    client = SRT("", "", auto_login=False, verbose=False)
    try:
        # SRTrain prints queue progress to stdout. Suppress it because cron stdout is
        # reserved exclusively for actionable seat alerts.
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            key = client.netfunnel_helper.generate_netfunnel_key(True)
            response = client._session.post(  # noqa: SLF001 - official read-only client seam
                url=constants.API_ENDPOINTS["search_schedule"],
                data=build_search_payload(key),
                timeout=30,
            )
        parsed = SRTResponseData(response.text)
        if not parsed.success():
            raise RuntimeError(f"SRT search failed: {parsed.message_code()} {parsed.message()}")
        rows = parsed.get_all().get("outDataSets", {}).get("dsOutput1", [])
        trains: list[dict[str, object]] = []
        for row in rows:
            train = SRTTrain(row)
            if train.train_name != "SRT":
                continue
            if not (DEPARTURE_FROM <= train.dep_time < DEPARTURE_TO):
                continue
            trains.append(
                {
                    "train_number": train.train_number,
                    "departure_time": train.dep_time,
                    "arrival_time": train.arr_time,
                    "general_available": bool(train.general_seat_available()),
                    "special_available": bool(train.special_seat_available()),
                    "general_state": train.general_seat_state,
                    "special_state": train.special_seat_state,
                }
            )
        return {
            "outcome": "success",
            "query": {
                "origin": ORIGIN,
                "destination": DESTINATION,
                "travel_date": TARGET_DATE,
                "passenger_count": 2,
            },
            "trains": trains,
        }
    finally:
        client._session.close()  # noqa: SLF001 - release official read-only client session


def main(now: datetime | None = None) -> int:
    current = now.astimezone(KST) if now is not None else datetime.now(KST)
    if not within_window(current):
        return 0
    with exclusive_run_lock() as acquired:
        if not acquired:
            return 0
        state = load_state()
        if minimum_interval_active(state, current):
            return 0
        state["last_attempt_at"] = current.isoformat()
        save_state(state)
        try:
            summary = query_srt()
            message, state = evaluate_summary(summary, state, current)
            state.pop("last_error", None)
            save_state(state)
            if message:
                print(message)
            return 0
        except Exception as error:
            first_failure = state.get("last_outcome") != "source_unavailable"
            state["last_checked_at"] = current.isoformat()
            state["last_outcome"] = "source_unavailable"
            state["last_error"] = type(error).__name__
            save_state(state)
            if first_failure:
                print("⚠️ SRT 2명 좌석 조회가 일시 실패했습니다. 10분 간격으로 계속 재시도합니다.")
            return 0


if __name__ == "__main__":
    sys.exit(main())
