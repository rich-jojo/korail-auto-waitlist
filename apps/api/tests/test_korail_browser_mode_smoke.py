from __future__ import annotations

import os
import stat
from datetime import datetime
from pathlib import Path

import pytest

from rail_waitlist.korail_browser_mode_smoke import (
    parser,
    require_private_output_platform,
    secure_output_file,
    train_evidence,
)
from rail_waitlist.korail_sidecar.browser_contracts import BrowserTrainSnapshot


def test_smoke_help_warns_that_captures_may_be_sensitive() -> None:
    assert "Captures may contain sensitive data" in parser().description


def test_smoke_parser_accepts_two_adult_passengers(tmp_path: Path) -> None:
    args = parser().parse_args(
        [
            "--mode",
            "gui",
            "--origin",
            "서대구",
            "--destination",
            "서울",
            "--travel-date",
            "2026-08-18",
            "--departure-from",
            "17:00",
            "--departure-to",
            "20:00",
            "--passenger-count",
            "2",
            "--output-dir",
            str(tmp_path),
        ]
    )

    assert args.passenger_count == 2


def test_train_evidence_is_sanitized_and_actionable() -> None:
    evidence = train_evidence(
        [
            BrowserTrainSnapshot(
                train_number="216",
                train_type="KTX",
                departure_at=datetime.fromisoformat("2026-08-18T18:04:00+09:00"),
                arrival_at=datetime.fromisoformat("2026-08-18T20:00:00+09:00"),
                adult_fare=43_500,
                standard="limited",
                first="sold_out",
            )
        ]
    )

    assert evidence == [
        {
            "train_number": "216",
            "train_type": "KTX",
            "departure_at": "2026-08-18T18:04:00+09:00",
            "arrival_at": "2026-08-18T20:00:00+09:00",
            "standard": "limited",
            "first": "sold_out",
        }
    ]


def test_private_output_platform_fails_closed_outside_posix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "name", "nt")

    with pytest.raises(RuntimeError, match="POSIX runtime"):
        require_private_output_platform()


@pytest.mark.skipif(os.name == "nt", reason="Windows chmod does not expose POSIX mode bits")
def test_secure_output_file_restricts_permissions(tmp_path: Path) -> None:
    output = tmp_path / "capture.png"
    output.write_bytes(b"capture")
    output.chmod(0o666)

    secure_output_file(output)

    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_secure_output_file_removes_capture_when_chmod_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "capture.png"
    output.write_bytes(b"capture")

    def fail_chmod(self: Path, mode: int) -> None:
        raise OSError("permission update failed")

    monkeypatch.setattr(Path, "chmod", fail_chmod)

    with pytest.raises(OSError, match="permission update failed"):
        secure_output_file(output)

    assert not output.exists()
