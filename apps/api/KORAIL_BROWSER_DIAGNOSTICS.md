# KORAIL 브라우저 화면 진단

기본 KORAIL adapter는 내부 Xvfb에서 non-headless Chrome을 실행하지만 화면 listener와 host 포트를
열지 않습니다. Chromium 화면이 꼭 필요할 때만 로컬 noVNC 구성을 사용합니다. 이 화면은 읽기 전용이며
`127.0.0.1`에만 열립니다. 보호 응답을 우회하거나 원격 운영 화면으로 공개하는 용도가 아닙니다.

PowerShell에서 저장소 루트를 기준으로 실행합니다.

```powershell
New-Item -ItemType Directory -Force secrets | Out-Null
$bytes = New-Object byte[] 6
$rng = [Security.Cryptography.RandomNumberGenerator]::Create()
try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
$password = [Convert]::ToBase64String($bytes)
[IO.File]::WriteAllText(
  (Join-Path (Get-Location) "secrets/korail-novnc-password.txt"),
  $password,
  [Text.UTF8Encoding]::new($false)
)
docker compose --profile experimental-rail -f compose.yml -f compose.korail-gui.yml config --quiet
docker compose --profile experimental-rail -f compose.yml -f compose.korail-gui.yml up -d --build --force-recreate korail-browser-adapter
```

브라우저에서 `http://127.0.0.1:6080/vnc.html`을 열고 방금 만든 8자 비밀번호를 입력합니다.
고전 VNC 인증은 8바이트만 사용하므로 이 비밀번호를 다른 서비스와 공유하지 말고 진단할 때마다
새로 만드세요. 화면과 캡처에는 계정·세션 정보가 보일 수 있으므로 외부에 공유하지 않습니다.

Linux에서는 host 사용자와 컨테이너의 `pwuser` 숫자 UID가 다를 수 있습니다. 다음처럼 8바이트
비밀번호를 만든 뒤 adapter UID 1001만 읽을 수 있도록 소유권과 권한을 맞춥니다. 비밀번호 값은
터미널이나 채팅에 출력하지 않습니다.

```bash
mkdir -p secrets
chmod 0700 secrets
python3 - <<'PY'
from pathlib import Path
import secrets

alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
Path("secrets/korail-novnc-password.txt").write_text(
    "".join(secrets.choice(alphabet) for _ in range(8)),
    encoding="ascii",
)
PY
sudo chown 1001:1001 secrets/korail-novnc-password.txt
sudo chmod 0600 secrets/korail-novnc-password.txt
docker compose --profile experimental-rail -f compose.yml -f compose.korail-gui.yml config --quiet
docker compose --profile experimental-rail -f compose.yml -f compose.korail-gui.yml up -d --build --force-recreate --no-deps korail-browser-adapter
```

OCI 같은 원격 서버의 6080 포트를 외부에 열지 않습니다. 로컬 PC에서 `ssh -L
6080:127.0.0.1:6080 <서버>`로 전달한 뒤 `http://127.0.0.1:6080/vnc.html`을 엽니다.

진단이 끝나면 noVNC listener가 없는 기본 내부 Xvfb 구성으로 되돌립니다.

```powershell
docker compose --profile experimental-rail -f compose.yml up -d --force-recreate korail-browser-adapter
Remove-Item -LiteralPath secrets/korail-novnc-password.txt
```

Linux에서는 같은 Compose 명령을 실행한 뒤 `rm -f secrets/korail-novnc-password.txt`로 제거합니다.

## 로컬 읽기 전용 스모크

호스트에 Chrome과 Xvfb가 설치된 Linux에서는 전체 Compose 스택 없이 공식 검색 화면을 한 번 읽을 수
있습니다. `--passenger-count`는 1~9만 허용하며, 공식 결과 화면의 `총 N명`이 요청과 일치하지 않으면
fail-closed합니다. 2명 이상 요청은 HTTP replay를 만들거나 재사용하지 않고 매 실행마다 GUI 공식 URL을
한 번만 엽니다. 이 명령은 로그인·예약·결제를 수행하지 않습니다.

```bash
export KORAIL_BROWSER_CHROMIUM_EXECUTABLE_PATH=/usr/bin/google-chrome
xvfb-run -a -s '-screen 0 1600x900x24 -nolisten tcp' \
  uv run --project apps/api --locked --extra browser \
  python -m rail_waitlist.korail_browser_mode_smoke \
  --mode gui --origin 서대구 --destination 서울 \
  --travel-date 2026-08-18 --departure-from 17:00 --departure-to 20:00 \
  --passenger-count 2 --output-dir /tmp/korail-smoke \
  --timeout-seconds 80 --overall-timeout-seconds 120
```

요약 JSON의 `trains`에는 알림 판단에 필요한 열차번호·출도착 시각·일반실/특실 상태만 포함되며,
계정·승객 신원·결제 정보는 포함하지 않습니다. 날짜 지정 watchdog은 9분 영속 최소 조회 간격과
단일 프로세스 잠금을 적용합니다. 보호·호출 제한은 15분, 점검·서비스 중단은 5분 cooldown을
적용하고 마지막 성공 좌석 상태를 보존해 복구 후 중복 알림을 막습니다.
