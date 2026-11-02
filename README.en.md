# Pies Watch

**[Wersja polska](README.md)**

Pies Watch listens to a camera, a stream or a microphone and logs a dog's barking. Google's YAMNet model recognises barking, and the app cuts short recordings from the audio with a small margin before and after. A browser dashboard shows a live view, statistics, a weekly chart by hour, recordings to listen to, and CSV/ZIP exports. "Pies" is Polish for "dog".

Everything runs locally on your own computer. No audio is sent anywhere.

## Features

- **Sound sources:** a Dahua camera or recorder, any stream FFmpeg can read (RTSP, HTTP, RTMP, SRT, so most IP cameras), or a microphone on a Linux computer.
- **Recordings of barking only:** louder sound starts listening, and after the silence the model scores it every half second. Only the parts with barking are kept, plus a few seconds of margin. Short pauses stay in one recording, longer ones split it.
- **Three separate measures:** recording length (with pauses), barking detected by the model (an estimate), and episodes confirmed manually after listening.
- **Weekly chart:** a 7 days × 24 hours grid; colour shows the amount of barking, and hovering shows time, event count and confirmations.
- **Review:** listen, mark false detections, confirm manually, delete.
- **Reports:** CSV and ZIP with recordings; monthly archives once every event of the month is reviewed.
- **Settings in the dashboard:** source, model thresholds, margins, time zone, language (Polish or English), login.
- **Optional login:** needed if the dashboard will be reachable from outside your home network.

## Requirements

- Docker with Compose (Docker Desktop on macOS or Windows, Docker Engine on Linux).
- A computer that runs all the time: Raspberry Pi 4/5, a mini PC, a NAS with Docker, or a Mac. The model runs on the CPU; 1 GB of free RAM is enough.
- A camera or another source that carries sound.

## Installation

```bash
git clone https://github.com/mateopoznan/dogswatch.git
cd dogswatch
./install.sh
```

The installer asks for the language, the dashboard port, the time zone, the sound source and whether to turn on login. It writes the answers to `.env`, builds the image (a few minutes the first time, as it downloads the model) and starts the container. At the end it prints the dashboard address, e.g. `http://192.168.1.20:1717`.

You can skip the source in the installer and set it later in the dashboard under **Settings**.

### Without the installer

```bash
cp .env.example .env      # fill in the source and port
docker compose up -d --build
```

## Sound sources

| Type | What to enter |
|---|---|
| Dahua camera or recorder | IP address, RTSP port (usually 554), channel, stream (0 main, 1 sub), user and password. |
| Any stream | The full address, e.g. `rtsp://192.168.1.10:554/stream1` (Hikvision: `/Streaming/Channels/101`, Reolink: `/h264Preview_01_main`). The user and password can go in separate fields. |
| Microphone (Linux) | An ALSA device, e.g. `default` or `hw:1,0` (list them with `arecord -l`). The container needs `/dev/snd`, so the installer adds `COMPOSE_FILE=docker-compose.yml:docker-compose.mic.yml` to `.env`. |

A microphone works only with Docker on Linux. On macOS and Windows Docker cannot see the microphone, so use a stream. With a microphone there is no live preview, because a microphone can have only one listener at a time.

**macOS with Colima:** if the container cannot reach a camera on the local network, `extras/macos-rtsp-relay` has a small TCP relay started by a LaunchAgent. Enter the Mac's address and the relay port in the dashboard.

## Login and access from the internet

Without login, anyone on the local network can open the dashboard. If you want to open it from outside (e.g. through a reverse proxy such as Caddy, Nginx or Cloudflare Tunnel), turn on login first in **Settings → Access**.

The dashboard blocks itself when it receives traffic through a reverse proxy while login is off. If your proxy already handles login, set `ALLOW_PROXY_WITHOUT_LOGIN=1` in `.env`.

The password is stored only as a hash (scrypt). After 5 failed attempts, login from that address is blocked for 5 minutes.

Forgotten password:

```bash
docker compose exec pies-watch python app.py --disable-login
docker compose restart
```

New password without the dashboard (type it on the next line):

```bash
docker compose exec -T pies-watch python app.py --set-login admin
docker compose restart
```

## Settings

On the first start, settings come from `.env`. After the first save in the dashboard they are kept in `data/settings.json`, and those values apply. Changes to thresholds, the source or the time zone work at once, without a restart. The exception is the dashboard port, which is set in `.env` (`PORT`) followed by `docker compose up -d`.

| Setting | Default | Meaning |
|---|---|---|
| Model threshold | 0.30 | Windows scoring at least this count as barking. Higher = fewer false detections but more missed barking. |
| “High score” threshold | 0.55 | From this score a recording is labelled “High score”. |
| Loudness threshold | 0.008 | RMS level (0–1) that starts listening. |
| Gap that joins barks | 15 s | A shorter silence keeps barks in one recording. |
| Margin | 3 s | Sound added before and after the detected barking. |
| Shortest recording | 3 s | Shorter parts are skipped. |
| Longest buffer | 1800 s | Longer continuous sound is analysed in parts. |
| Clean up after | 30 days | Applies to helper files. Events in the dashboard stay until archived. |

The model score is not a probability, only an indication. Recordings labelled “Possible barking” are worth a listen.

## Data

Everything lives in the `data/` folder next to `docker-compose.yml`:

- `events.jsonl`: the events, one JSON line per recording,
- `szczekanie_YYYYMMDD_HHMMSS.mp3`: recordings (mono, 64 kb/s; "szczekanie" means barking),
- `archives/YYYY-MM.zip`: monthly archives with reports and recordings,
- `settings.json`: settings from the dashboard (includes the source password, permissions 600).

A backup is a copy of the `data/` folder.

A month is archived after it ends (from the 2nd of the next month), once every event is confirmed or marked as false. The recordings then move into the ZIP and leave the current list.

## Updating

```bash
git pull
docker compose up -d --build
```

## API

| Address | Description |
|---|---|
| `GET /api/status` | Detector state and today's statistics. |
| `GET /api/events` | Current events. |
| `GET /api/week-heatmap?day=YYYY-MM-DD` | Weekly chart data. |
| `GET /download/report.csv`, `/download/bark-report.csv` | CSV reports (headers in the dashboard language). |

Requests that change data need the header `X-Requested-With: PiesWatch`.

## Tests

The `tests` folder is not part of the image, so copy it into the container first:

```bash
docker compose cp tests pies-watch:/app/tests
docker compose exec pies-watch python -m unittest discover -s tests
```

## Privacy and the law

Recording sound is covered by privacy law. Point the camera at your own property only, and do not share recordings with other people's voices. A model score is not proof. Before using recordings in a dispute, listen to them and confirm them manually.

## Licence

Code: [MIT](LICENSE). YAMNet model: Google, Apache 2.0, downloaded while building the image (see [NOTICE](NOTICE)).

Pies Watch 1.0 by [@mateopoznan](https://hub.mateopoznan.pl)
