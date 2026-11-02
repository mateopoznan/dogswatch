#!/usr/bin/env bash
# Pies Watch installer: asks a few questions, writes .env and starts the container.
set -euo pipefail
cd "$(dirname "$0")"

if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  echo "Docker with Compose is required: https://docs.docker.com/get-docker/" >&2
  exit 1
fi

ask() { # ask VAR "question" default
  local answer
  read -r -p "$2 [$3]: " answer
  printf -v "$1" '%s' "${answer:-$3}"
}

ask_secret() { # ask_secret VAR "question"
  local answer
  read -r -s -p "$2: " answer
  echo
  printf -v "$1" '%s' "$answer"
}

ask LANGUAGE "Język / Language (pl/en)" "pl"
[[ "$LANGUAGE" == "en" ]] || LANGUAGE=pl
say() { if [[ "$LANGUAGE" == "en" ]]; then echo "$2"; else echo "$1"; fi; }
q() { if [[ "$LANGUAGE" == "en" ]]; then echo "$2"; else echo "$1"; fi; }

if [[ -f .env ]]; then
  ask OVERWRITE "$(q 'Plik .env już istnieje. Nadpisać? (t/n)' '.env already exists. Overwrite? (y/n)')" "n"
  if [[ ! "$OVERWRITE" =~ ^[tTyY] ]]; then
    say "Zostawiam .env. Uruchamiam: ${COMPOSE[*]} up -d --build" "Keeping .env. Starting: ${COMPOSE[*]} up -d --build"
    "${COMPOSE[@]}" up -d --build
    exit 0
  fi
fi

while :; do
  ask PORT "$(q 'Port panelu' 'Panel port')" "1717"
  [[ "$PORT" =~ ^[0-9]+$ ]] && (( PORT >= 1 && PORT <= 65535 )) && break
  say "Podaj liczbę od 1 do 65535." "Enter a number from 1 to 65535."
done

HOST_TZ="$(cat /etc/timezone 2>/dev/null || true)"
if [[ -z "$HOST_TZ" ]] && command -v timedatectl >/dev/null 2>&1; then HOST_TZ="$(timedatectl show -p Timezone --value 2>/dev/null || true)"; fi
if [[ -z "$HOST_TZ" && -L /etc/localtime ]]; then HOST_TZ="$(readlink /etc/localtime | sed 's#.*zoneinfo/##')"; fi
ask TIMEZONE "$(q 'Strefa czasowa' 'Time zone')" "${HOST_TZ:-Europe/Warsaw}"
ask PLACE_NAME "$(q 'Nazwa miejsca lub kamery (może być pusta)' 'Place or camera name (may be empty)')" ""

echo
say "Skąd brać dźwięk?" "Where should the sound come from?"
say "  1) Kamera lub rejestrator Dahua (RTSP)" "  1) Dahua camera or recorder (RTSP)"
say "  2) Dowolny strumień: RTSP, HTTP, RTMP… (inne kamery)" "  2) Any stream: RTSP, HTTP, RTMP… (other cameras)"
say "  3) Mikrofon podłączony do tego komputera (tylko Linux)" "  3) Microphone connected to this computer (Linux only)"
say "  4) Ustawię później w przeglądarce" "  4) I will set it later in the browser"
ask CHOICE "$(q 'Wybór' 'Choice')" "4"

SOURCE_TYPE=none SOURCE_URL="" SOURCE_USER="" SOURCE_PASSWORD="" DAHUA_HOST="" DAHUA_RTSP_PORT=554
DAHUA_CHANNEL=1 DAHUA_SUBTYPE=0 ALSA_DEVICE=default COMPOSE_LINE=""
case "$CHOICE" in
  1)
    SOURCE_TYPE=dahua
    ask DAHUA_HOST "$(q 'Adres IP kamery lub rejestratora' 'Camera or recorder IP address')" ""
    ask DAHUA_RTSP_PORT "$(q 'Port RTSP' 'RTSP port')" "554"
    ask DAHUA_CHANNEL "$(q 'Kanał' 'Channel')" "1"
    ask DAHUA_SUBTYPE "$(q 'Strumień: 0 główny, 1 pomocniczy' 'Stream: 0 main, 1 sub')" "0"
    ask SOURCE_USER "$(q 'Użytkownik kamery' 'Camera user')" "admin"
    ask_secret SOURCE_PASSWORD "$(q 'Hasło kamery' 'Camera password')"
    ;;
  2)
    SOURCE_TYPE=url
    ask SOURCE_URL "$(q 'Adres strumienia, np. rtsp://192.168.1.10:554/stream1' 'Stream address, e.g. rtsp://192.168.1.10:554/stream1')" ""
    ask SOURCE_USER "$(q 'Użytkownik (puste, jeśli nie trzeba)' 'User (empty if not needed)')" ""
    if [[ -n "$SOURCE_USER" ]]; then ask_secret SOURCE_PASSWORD "$(q 'Hasło' 'Password')"; fi
    ;;
  3)
    SOURCE_TYPE=microphone
    if command -v arecord >/dev/null 2>&1; then arecord -l || true; fi
    ask ALSA_DEVICE "$(q 'Urządzenie ALSA (np. default albo hw:1,0)' 'ALSA device (e.g. default or hw:1,0)')" "default"
    COMPOSE_LINE="COMPOSE_FILE=docker-compose.yml:docker-compose.mic.yml"
    ;;
esac

echo
ask LOGIN "$(q 'Włączyć logowanie do panelu? Konieczne, jeśli panel będzie dostępny z internetu (t/n)' 'Turn on login for the panel? Needed if the panel will be reachable from the internet (y/n)')" "n"
LOGIN_USER="" LOGIN_PASSWORD=""
if [[ "$LOGIN" =~ ^[tTyY] ]]; then
  ask LOGIN_USER "$(q 'Użytkownik panelu' 'Panel user')" "admin"
  while :; do
    ask_secret LOGIN_PASSWORD "$(q 'Hasło panelu (min. 8 znaków)' 'Panel password (min. 8 characters)')"
    ask_secret LOGIN_REPEAT "$(q 'Powtórz hasło' 'Repeat the password')"
    if [[ ${#LOGIN_PASSWORD} -ge 8 && "$LOGIN_PASSWORD" == "$LOGIN_REPEAT" ]]; then break; fi
    say "Hasła są różne albo za krótkie." "The passwords differ or are too short."
  done
fi

# Values go in single quotes so that characters like $ or # in passwords stay as typed.
for value in "$TIMEZONE" "$PLACE_NAME" "$SOURCE_URL" "$SOURCE_USER" "$SOURCE_PASSWORD" "$DAHUA_HOST" "$ALSA_DEVICE"; do
  if [[ "$value" == *"'"* ]]; then
    say "Wartości nie mogą zawierać apostrofu ('). Wpisz je później w panelu." "Values cannot contain an apostrophe ('). Enter them later in the panel."
    exit 1
  fi
done
quote() { printf "'%s'" "$1"; }
umask 077
{
  echo "PORT=$PORT"
  echo "LANGUAGE=$LANGUAGE"
  echo "TZ=$(quote "$TIMEZONE")"
  echo "PLACE_NAME=$(quote "$PLACE_NAME")"
  echo "SOURCE_TYPE=$SOURCE_TYPE"
  echo "SOURCE_URL=$(quote "$SOURCE_URL")"
  echo "SOURCE_USER=$(quote "$SOURCE_USER")"
  echo "SOURCE_PASSWORD=$(quote "$SOURCE_PASSWORD")"
  echo "DAHUA_HOST=$(quote "$DAHUA_HOST")"
  echo "DAHUA_RTSP_PORT=$DAHUA_RTSP_PORT"
  echo "DAHUA_CHANNEL=$DAHUA_CHANNEL"
  echo "DAHUA_SUBTYPE=$DAHUA_SUBTYPE"
  echo "ALSA_DEVICE=$(quote "$ALSA_DEVICE")"
  [[ -n "$COMPOSE_LINE" ]] && echo "$COMPOSE_LINE"
  echo "ALLOW_PROXY_WITHOUT_LOGIN=0"
} > .env
mkdir -p data

say "Buduję i uruchamiam kontener (pierwszy raz potrwa kilka minut)…" "Building and starting the container (the first time takes a few minutes)…"
"${COMPOSE[@]}" up -d --build

if [[ -n "$LOGIN_USER" ]]; then
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    "${COMPOSE[@]}" exec -T pies-watch true >/dev/null 2>&1 && break
    sleep 2
  done
  printf '%s\n' "$LOGIN_PASSWORD" | "${COMPOSE[@]}" exec -T pies-watch python app.py --set-login "$LOGIN_USER"
  "${COMPOSE[@]}" restart pies-watch >/dev/null
fi

ADDRESS="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
if [[ -z "$ADDRESS" ]] && command -v ipconfig >/dev/null 2>&1; then ADDRESS="$(ipconfig getifaddr en0 2>/dev/null || true)"; fi
echo
say "Gotowe. Panel: http://${ADDRESS:-localhost}:$PORT" "Done. Panel: http://${ADDRESS:-localhost}:$PORT"
say "Pozostałe ustawienia zmienisz w panelu, w zakładce Ustawienia." "Change everything else in the panel under Settings."
