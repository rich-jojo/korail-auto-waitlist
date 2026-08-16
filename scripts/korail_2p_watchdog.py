#!/usr/bin/env python3
"""Low-frequency, read-only KORAIL two-passenger availability watchdog."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
TARGET_DATE = "2026-08-18"
START_HOUR = 9
END_HOUR = 20
REPO = Path("/home/jojo/projects/korail-auto-waitlist")
API_PROJECT = REPO / "apps/api"
STATE_PATH = Path.home() / ".local/state/korail-2p-watchdog/state.json"
CAPTURE_ROOT = Path.home() / ".cache/railwait/watch"
OFFICIAL_URL = "https://www.korail.com/ticket/search"
AVAILABLE_STATUSES = {"available", "limited"}
PROTECTION_COOLDOWN_SECONDS = 900
OUTAGE_COOLDOWN_SECONDS = 300


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


def within_window(now: datetime) -> bool:
    return now.astimezone(KST).date().isoformat() == TARGET_DATE and START_HOUR <= now.astimezone(KST).hour < END_HOUR


def available_entries(summary: dict[str, Any]) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    trains = summary.get("trains")
    if not isinstance(trains, list):
        return entries
    for train in trains:
        if not isinstance(train, dict):
            continue
        for seat_class, label in (("standard", "일반실"), ("first", "특실")):
            status = train.get(seat_class)
            if status not in AVAILABLE_STATUSES:
                continue
            train_number = str(train.get("train_number", "")).strip()
            departure_at = str(train.get("departure_at", "")).strip()
            arrival_at = str(train.get("arrival_at", "")).strip()
            if not train_number or not departure_at or not arrival_at:
                continue
            entries.append(
                {
                    "key": f"{train_number}:{seat_class}",
                    "train_number": train_number,
                    "train_type": str(train.get("train_type", "KTX")).strip() or "KTX",
                    "departure_at": departure_at,
                    "arrival_at": arrival_at,
                    "seat_label": label,
                    "status": str(status),
                }
            )
    return entries


def _clock(value: str) -> str:
    try:
        return datetime.fromisoformat(value).astimezone(KST).strftime("%H:%M")
    except ValueError:
        return value


def availability_message(entries: list[dict[str, str]]) -> str:
    lines = [
        "🚄 코레일 성인 2명 좌석 가능",
        "서대구→서울 · 2026-08-18 · 출발 17:00~20:00",
    ]
    for entry in entries:
        qualifier = "여유" if entry["status"] == "available" else "한정"
        lines.append(
            f"- {entry['train_type']} {entry['train_number']} "
            f"{_clock(entry['departure_at'])}→{_clock(entry['arrival_at'])} "
            f"{entry['seat_label']} ({qualifier}, 2명 조건 조회)"
        )
    lines.extend((f"공식 예매: {OFFICIAL_URL}", "자동 예약·구매·결제는 하지 않았습니다."))
    return "\n".join(lines)


def evaluate_summary(
    summary: dict[str, Any], state: dict[str, Any], now: datetime
) -> tuple[str, dict[str, Any]]:
    outcome = str(summary.get("outcome", "invalid_summary"))
    new_state = dict(state)
    new_state["last_checked_at"] = now.astimezone(KST).isoformat()
    new_state["last_outcome"] = outcome

    if outcome == "success":
        entries = available_entries(summary)
        current_keys = sorted(entry["key"] for entry in entries)
        previous_keys = {
            str(value) for value in state.get("available_keys", []) if isinstance(value, str)
        }
        newly_available = [entry for entry in entries if entry["key"] not in previous_keys]
        new_state["available_keys"] = current_keys
        new_state.pop("cooldown_until", None)
        new_state.pop("last_error", None)
        return (availability_message(newly_available) if newly_available else ""), new_state

    new_state["available_keys"] = []
    previous_error = str(state.get("last_error", ""))
    new_state["last_error"] = outcome
    if outcome == "provider_access_restricted":
        new_state["cooldown_until"] = (
            now.astimezone(KST) + timedelta(seconds=PROTECTION_COOLDOWN_SECONDS)
        ).isoformat()
        message = "⚠️ 코레일 접근 제한을 감지해 우회하지 않고 15분 동안 조회를 중단합니다."
    elif outcome == "provider_unavailable":
        new_state["cooldown_until"] = (
            now.astimezone(KST) + timedelta(seconds=OUTAGE_COOLDOWN_SECONDS)
        ).isoformat()
        message = "⚠️ 코레일 점검·서비스 중단을 감지해 5분 동안 조회를 중단합니다."
    else:
        message = f"⚠️ 코레일 2명 좌석 조회 실패: {outcome}"
    return ("" if previous_error == outcome else message), new_state


def _cooldown_active(state: dict[str, Any], now: datetime) -> bool:
    raw = state.get("cooldown_until")
    if not isinstance(raw, str):
        return False
    try:
        return datetime.fromisoformat(raw) > now.astimezone(KST)
    except ValueError:
        return False


def _latest_summary(stdout: str, output_dir: Path) -> dict[str, Any]:
    summary_path = output_dir / "gui-summary.json"
    if summary_path.is_file():
        try:
            value = json.loads(summary_path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                return value
        except (json.JSONDecodeError, OSError):
            pass
    for line in reversed(stdout.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and "outcome" in value:
            return value
    return {"outcome": "invalid_summary"}


def run_query(now: datetime) -> dict[str, Any]:
    stamp = now.astimezone(KST).strftime("%Y%m%d-%H%M%S")
    output_dir = CAPTURE_ROOT / stamp
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    command = [
        "xvfb-run",
        "-a",
        "-s",
        "-screen 0 1600x900x24 -nolisten tcp",
        "uv",
        "run",
        "--project",
        str(API_PROJECT),
        "--locked",
        "--extra",
        "browser",
        "python",
        "-m",
        "rail_waitlist.korail_browser_mode_smoke",
        "--mode",
        "gui",
        "--origin",
        "서대구",
        "--destination",
        "서울",
        "--travel-date",
        TARGET_DATE,
        "--departure-from",
        "17:00",
        "--departure-to",
        "20:00",
        "--passenger-count",
        "2",
        "--output-dir",
        str(output_dir),
        "--timeout-seconds",
        "80",
        "--overall-timeout-seconds",
        "120",
    ]
    env = os.environ.copy()
    env["KORAIL_BROWSER_CHROMIUM_EXECUTABLE_PATH"] = "/usr/bin/google-chrome"
    try:
        completed = subprocess.run(
            command,
            cwd=REPO,
            env=env,
            capture_output=True,
            text=True,
            timeout=150,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"outcome": "local_timeout"}
    summary = _latest_summary(completed.stdout, output_dir)
    summary["process_exit_code"] = completed.returncode
    return summary


def prune_captures(keep: int = 5) -> None:
    try:
        directories = sorted(
            (path for path in CAPTURE_ROOT.iterdir() if path.is_dir()), reverse=True
        )
    except OSError:
        return
    for directory in directories[keep:]:
        shutil.rmtree(directory, ignore_errors=True)


def main() -> int:
    now = datetime.now(KST)
    if not within_window(now):
        return 0
    state = load_state()
    if _cooldown_active(state, now):
        return 0
    summary = run_query(now)
    message, new_state = evaluate_summary(summary, state, now)
    save_state(new_state)
    prune_captures()
    if message:
        print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
