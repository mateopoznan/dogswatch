import csv
import io
import json
import math
import os
import secrets
import subprocess
import sys
import threading
import time
import wave
import zipfile
from collections import deque
from datetime import datetime, timedelta, time as datetime_time
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import (Flask, Response, has_request_context, jsonify, redirect, render_template, request,
                   send_file, session)
from werkzeug.security import check_password_hash, generate_password_hash

from i18n import LANGUAGES, TEXTS, text

VERSION = "1.0"
AUTHOR_URL = "https://hub.mateopoznan.pl"

app = Flask(__name__)
DATA = Path(os.getenv("DATA_DIR", "/app/data"))
MODEL_PATH = os.getenv("MODEL_PATH", "/app/yamnet.tflite")
EVENTS = DATA / "events.jsonl"
ARCHIVES = DATA / "archives"
SETTINGS_FILE = DATA / "settings.json"
SECRET_FILE = DATA / ".secret_key"
DATA.mkdir(parents=True, exist_ok=True)
ARCHIVES.mkdir(parents=True, exist_ok=True)
CSV_FIELDS = ["started", "ended", "dlugosc_bloku_s", "szczekanie_model_szac_s", "epizod_reczny_s", "epizod_uwaga",
              "ponad_1_min", "ponad_5_min", "ponad_10_min",
              "blok_ponad_1_min", "blok_ponad_5_min", "blok_ponad_10_min",
              "noc_22_7", "classification", "label", "is_bark", "false_positive",
              "reviewed_at", "manual_confirmed", "manual_confirmed_at", "bark_score", "peak_rms", "file"]
# Names inside monthly archives stay fixed so later runs can merge into an existing archive.
ARCHIVE_MEMBERS = ("events.jsonl", "raport.csv", "raport-szczekania.csv")
SOURCE_TYPES = ("none", "dahua", "url", "microphone")
URL_SCHEMES = ("rtsp", "rtsps", "http", "https", "rtmp", "rtmps", "srt")
sample_rate = 16000
frame_seconds = 0.1
frame_bytes = int(sample_rate * frame_seconds) * 2
window_samples = 15600
hop_samples = 7800
window_seconds = window_samples / sample_rate
hop_seconds = hop_samples / sample_rate
classifying_count = 0
state = {"connected": False, "last_error": "", "rms": 0.0, "recording": False, "candidate_seconds": 0, "classifying": False,
         "backfill_running": False, "backfill_done": 0, "backfill_total": 0}
lock = threading.Lock()
events_lock = threading.RLock()
settings_lock = threading.RLock()
classifier = None
classifier_lock = threading.RLock()
login_failures = {}


def env_float(name, default):
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return float(default)


def env_flag(name):
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


def default_settings():
    """Initial values, taken from .env so older installations keep working without a settings file."""
    dahua_host = os.getenv("DAHUA_HOST", "").strip()
    source_type = os.getenv("SOURCE_TYPE", "").strip() or ("dahua" if dahua_host else "none")
    password = os.getenv("AUTH_PASSWORD", "")
    return {
        "language": os.getenv("LANGUAGE", "pl"),
        "place_name": os.getenv("PLACE_NAME", ""),
        "timezone": os.getenv("TZ", "Europe/Warsaw"),
        "source_type": source_type,
        "source_url": os.getenv("SOURCE_URL", ""),
        "source_user": os.getenv("SOURCE_USER", "") or os.getenv("DAHUA_USER", ""),
        "source_password": os.getenv("SOURCE_PASSWORD", "") or os.getenv("DAHUA_PASSWORD", ""),
        "dahua_host": dahua_host,
        "dahua_port": int(env_float("DAHUA_RTSP_PORT", "554")),
        "dahua_channel": int(env_float("DAHUA_CHANNEL", "1")),
        "dahua_subtype": int(env_float("DAHUA_SUBTYPE", "0")),
        "alsa_device": os.getenv("ALSA_DEVICE", "default"),
        "audio_threshold": env_float("AUDIO_THRESHOLD", "0.008"),
        "min_event_seconds": env_float("MIN_EVENT_SECONDS", "1.2"),
        "pre_roll_seconds": env_float("PRE_ROLL_SECONDS", "3"),
        "merge_gap_seconds": env_float("MERGE_GAP_SECONDS", "15"),
        "bark_padding_seconds": env_float("BARK_PADDING_SECONDS", "3"),
        "min_event_block_seconds": env_float("MIN_EVENT_BLOCK_SECONDS", "3"),
        "max_candidate_seconds": env_float("MAX_CANDIDATE_SECONDS", "1800"),
        "retention_days": int(env_float("RETENTION_DAYS", "30")),
        "bark_threshold": env_float("YAMNET_BARK_THRESHOLD", "0.30"),
        "bark_high_threshold": env_float("YAMNET_BARK_HIGH_THRESHOLD", "0.55"),
        "auth_enabled": env_flag("AUTH_ENABLED") and bool(password),
        "auth_user": os.getenv("AUTH_USER", "admin"),
        "auth_password_hash": generate_password_hash(password) if password else "",
    }


# key: (type, minimum, maximum) for the numeric settings shown in the panel.
NUMBER_FIELDS = {
    "dahua_port": (int, 1, 65535, "settings_port"),
    "dahua_channel": (int, 0, 64, "settings_channel"),
    "dahua_subtype": (int, 0, 1, "settings_subtype"),
    "audio_threshold": (float, 0.0005, 0.5, "settings_audio_threshold"),
    "min_event_seconds": (float, 0.1, 2.0, "settings_min_event"),
    "pre_roll_seconds": (float, 0, 10, "settings_pre_roll"),
    "merge_gap_seconds": (float, 1, 120, "settings_merge_gap"),
    "bark_padding_seconds": (float, 0, 15, "settings_padding"),
    "min_event_block_seconds": (float, 0, 60, "settings_min_block"),
    "max_candidate_seconds": (float, 60, 7200, "settings_max_candidate"),
    "retention_days": (int, 1, 3650, "settings_retention"),
    "bark_threshold": (float, 0.05, 0.95, "settings_bark_threshold"),
    "bark_high_threshold": (float, 0.05, 0.99, "settings_high_threshold"),
}
TEXT_FIELDS = {"place_name": "settings_place", "source_url": "settings_url", "source_user": "settings_user",
               "dahua_host": "settings_host", "alsa_device": "settings_device", "auth_user": "settings_auth_user"}
SOURCE_FIELDS = ("source_type", "source_url", "source_user", "source_password", "dahua_host", "dahua_port",
                 "dahua_channel", "dahua_subtype", "alsa_device")
SECRET_FIELDS = ("source_password", "auth_password_hash")


def load_settings():
    values = default_settings()
    if SETTINGS_FILE.is_file():
        try:
            stored = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            values.update({key: value for key, value in stored.items() if key in values})
        except (OSError, json.JSONDecodeError):
            pass
    return values


def write_settings(values):
    temporary = SETTINGS_FILE.with_name("settings.json.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(values, stream, ensure_ascii=False, indent=2)
    os.chmod(temporary, 0o600)
    os.replace(temporary, SETTINGS_FILE)


def validate_settings(current, incoming, language):
    """Return (new settings, error). Empty password fields keep the stored password."""
    def fail(key, **values):
        return None, text(language, key, **values)

    result = dict(current)
    for key, label in TEXT_FIELDS.items():
        if key in incoming:
            value = str(incoming[key] or "").strip()
            if len(value) > 500:
                return fail("val_text", field=text(language, label))
            result[key] = value
    for key, (kind, minimum, maximum, label) in NUMBER_FIELDS.items():
        if key not in incoming:
            continue
        try:
            number = kind(float(str(incoming[key]).replace(",", ".")))
            if not minimum <= number <= maximum or (kind is float and not math.isfinite(number)):
                raise ValueError
        except (TypeError, ValueError):
            return fail("val_number", field=text(language, label), min=minimum, max=maximum)
        result[key] = number
    if "language" in incoming:
        if incoming["language"] not in LANGUAGES:
            return fail("val_language")
        result["language"] = incoming["language"]
    if "timezone" in incoming:
        try:
            ZoneInfo(str(incoming["timezone"]).strip())
        except (ZoneInfoNotFoundError, ValueError):
            return fail("val_timezone")
        result["timezone"] = str(incoming["timezone"]).strip()
    if "source_type" in incoming:
        if incoming["source_type"] not in SOURCE_TYPES:
            return fail("val_source_type")
        result["source_type"] = incoming["source_type"]
    if incoming.get("source_password"):
        result["source_password"] = str(incoming["source_password"])
    if incoming.get("clear_source_password"):
        result["source_password"] = ""
    if result["source_type"] == "url" and urlsplit(result["source_url"]).scheme.lower() not in URL_SCHEMES:
        return fail("val_url")
    if result["source_type"] == "dahua" and not result["dahua_host"]:
        return fail("val_host")
    if result["source_type"] == "microphone" and not result["alsa_device"]:
        return fail("val_device")
    if result["bark_high_threshold"] < result["bark_threshold"]:
        return fail("val_thresholds")
    if "auth_enabled" in incoming:
        result["auth_enabled"] = bool(incoming["auth_enabled"])
    new_password = str(incoming.get("auth_password") or "")
    if new_password:
        if len(new_password) < 8:
            return fail("val_auth_password")
        result["auth_password_hash"] = generate_password_hash(new_password)
    if result["auth_enabled"]:
        if not result["auth_user"]:
            return fail("val_auth_user")
        if not result["auth_password_hash"]:
            return fail("val_auth_password")
    return result, None


settings = load_settings()
source_version = 0
threshold = min_event = pre_roll = merge_gap = min_event_block_seconds = 0.0
bark_padding = max_candidate_seconds = bark_score_threshold = bark_high_threshold = 0.0
retention = 30
local_tz = ZoneInfo("UTC")
recent_audio = deque(maxlen=1)


def apply_settings(values):
    """Make saved settings take effect; a changed source makes the detector reconnect."""
    global settings, threshold, min_event, pre_roll, merge_gap, min_event_block_seconds, bark_padding
    global max_candidate_seconds, bark_score_threshold, bark_high_threshold, retention, local_tz, recent_audio
    global source_version
    with settings_lock:
        if any(settings.get(key) != values.get(key) for key in SOURCE_FIELDS):
            source_version += 1
        settings = dict(values)
        threshold = float(values["audio_threshold"])
        min_event = float(values["min_event_seconds"])
        merge_gap = float(values["merge_gap_seconds"])
        min_event_block_seconds = float(values["min_event_block_seconds"])
        bark_padding = float(values["bark_padding_seconds"])
        max_candidate_seconds = float(values["max_candidate_seconds"])
        bark_score_threshold = float(values["bark_threshold"])
        bark_high_threshold = float(values["bark_high_threshold"])
        retention = int(values["retention_days"])
        try:
            local_tz = ZoneInfo(values["timezone"])
        except (ZoneInfoNotFoundError, ValueError):
            local_tz = ZoneInfo("UTC")
        if float(values["pre_roll_seconds"]) != pre_roll or recent_audio.maxlen == 1:
            pre_roll = float(values["pre_roll_seconds"])
            recent_audio = deque(maxlen=max(1, int(pre_roll / frame_seconds)))


apply_settings(settings)


def current_language():
    if has_request_context():
        chosen = request.cookies.get("lang")
        if chosen in LANGUAGES:
            return chosen
    return settings.get("language") if settings.get("language") in LANGUAGES else "pl"


def t(key, **values):
    return text(current_language(), key, **values)


def with_credentials(url, user, password):
    """Put the source login into the URL unless the URL already has one."""
    if not user:
        return url
    parts = urlsplit(url)
    if "@" in parts.netloc:
        return url
    login = quote(user, safe="") + (":" + quote(password, safe="") if password else "")
    return urlunsplit((parts.scheme, f"{login}@{parts.netloc}", parts.path, parts.query, parts.fragment))


def source_input():
    """FFmpeg input arguments for the configured source, or None when nothing is set up."""
    with settings_lock:
        values = dict(settings)
    kind = values["source_type"]
    if kind == "dahua" and values["dahua_host"]:
        url = with_credentials(
            f"rtsp://{values['dahua_host']}:{values['dahua_port']}/cam/realmonitor"
            f"?channel={values['dahua_channel']}&subtype={values['dahua_subtype']}",
            values["source_user"], values["source_password"])
        return ["-rtsp_transport", "tcp", "-i", url]
    if kind == "url" and values["source_url"]:
        url = with_credentials(values["source_url"], values["source_user"], values["source_password"])
        prefix = ["-rtsp_transport", "tcp"] if url.lower().startswith(("rtsp://", "rtsps://")) else []
        return prefix + ["-i", url]
    if kind == "microphone" and values["alsa_device"]:
        return ["-f", "alsa", "-i", values["alsa_device"]]
    return None


def hide_secrets(message):
    """Remove the source address and password from error messages shown in the panel."""
    with settings_lock:
        password = settings.get("source_password") or ""
    arguments = source_input() or []
    for value in arguments:
        if "://" in value:
            message = message.replace(value, "[source]")
    if password:
        message = message.replace(quote(password, safe=""), "***").replace(password, "***")
    return message


def run_ffmpeg(args):
    return subprocess.Popen(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error"] + args,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)


def read_exact(stream, size):
    data = bytearray()
    while len(data) < size:
        part = stream.read(size - len(data))
        if not part:
            break
        data.extend(part)
    return bytes(data)


def encode_mp3(source, destination):
    temporary = destination.with_name(f".{destination.stem}.converting.mp3")
    temporary.unlink(missing_ok=True)
    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
         "-vn", "-ac", "1", "-ar", "22050", "-codec:a", "libmp3lame", "-b:a", "64k", str(temporary)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode != 0 or not temporary.is_file() or temporary.stat().st_size == 0:
        temporary.unlink(missing_ok=True)
        raise RuntimeError(t("err_mp3"))
    temporary.replace(destination)


def event_is_night(started, ended):
    try:
        start = datetime.fromisoformat(started)
        end = datetime.fromisoformat(ended)
        if start.tzinfo is None:
            start = start.replace(tzinfo=local_tz)
        else:
            start = start.astimezone(local_tz)
        if end.tzinfo is None:
            end = end.replace(tzinfo=local_tz)
        else:
            end = end.astimezone(local_tz)
        day = start.date() - timedelta(days=1)
        while day <= end.date():
            night_start = datetime.combine(day, datetime_time(22), tzinfo=local_tz)
            night_end = datetime.combine(day + timedelta(days=1), datetime_time(7), tzinfo=local_tz)
            if start < night_end and end > night_start:
                return True
            day += timedelta(days=1)
    except (TypeError, ValueError):
        pass
    return False


def create_interpreter(num_threads):
    from ai_edge_litert.interpreter import Interpreter

    model = Interpreter(model_path=MODEL_PATH, num_threads=num_threads)
    model.resize_tensor_input(model.get_input_details()[0]["index"], [window_samples], strict=True)
    model.allocate_tensors()
    return model


def window_scores(model, samples):
    """Return the bark score of each YAMNet window; windows start every hop_seconds."""
    import numpy as np

    input_index = model.get_input_details()[0]["index"]
    output_index = model.get_output_details()[0]["index"]
    bark_classes = [70, 71, 73]  # Bark, Yip, Bow-wow in the YAMNet AudioSet class map.
    result = []
    for start in range(0, len(samples), hop_samples):
        frame = samples[start:start + window_samples]
        if len(frame) < window_samples:
            frame = np.pad(frame, (0, window_samples - len(frame)))
        model.set_tensor(input_index, frame)
        model.invoke()
        scores = model.get_tensor(output_index).reshape(-1)
        result.append(float(np.max(scores[bark_classes])))
    return result


def bark_segments(scores, total_seconds):
    """Group windows above the threshold into recordings: gaps up to merge_gap stay together."""
    segments = []
    for index, score in enumerate(scores):
        if score < bark_score_threshold:
            continue
        start = index * hop_seconds
        end = min(start + window_seconds, total_seconds)
        if segments and start - segments[-1]["bark_end"] <= merge_gap:
            segments[-1]["bark_end"] = max(segments[-1]["bark_end"], end)
            segments[-1]["windows"].append(index)
        else:
            segments.append({"bark_start": start, "bark_end": end, "windows": [index]})
    for segment in segments:
        segment["start"] = max(0.0, segment["bark_start"] - bark_padding)
        segment["end"] = min(total_seconds, segment["bark_end"] + bark_padding)
        segment["score"] = max(scores[index] for index in segment["windows"])
        segment["bark_seconds"] = round(len(segment["windows"]) * hop_seconds, 1)
    return segments


def bark_hours(audio_start, windows):
    """Spread detected windows over local clock hours, e.g. {"2026-11-01T17": 12.2}."""
    hours = {}
    for index in windows:
        moment = (audio_start + timedelta(seconds=index * hop_seconds)).astimezone(local_tz)
        key = moment.strftime("%Y-%m-%dT%H")
        hours[key] = hours.get(key, 0.0) + hop_seconds
    return {key: round(value, 1) for key, value in hours.items()}


def read_candidate_samples(path):
    import numpy as np

    with wave.open(str(path), "rb") as audio:
        if audio.getframerate() != sample_rate or audio.getnchannels() != 1 or audio.getsampwidth() != 2:
            raise RuntimeError(t("err_bad_format"))
        return np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2").astype(np.float32) / 32768.0


def peak_rms(samples):
    import numpy as np

    size = int(sample_rate * frame_seconds)
    count = len(samples) // size
    if count == 0:
        return 0.0
    frames = samples[:count * size].reshape(count, size)
    return float(np.sqrt(np.mean(frames * frames, axis=1)).max())


def save_segment(samples, segment, audio_start):
    """Encode one segment and log it. Safe to repeat after a restart for the same candidate."""
    import numpy as np

    started = audio_start + timedelta(seconds=segment["start"])
    ended = audio_start + timedelta(seconds=segment["end"])
    name = f"szczekanie_{started.strftime('%Y%m%d_%H%M%S')}.mp3"
    if any(event.get("file") == name for event in get_events()):
        return name
    part = samples[int(segment["start"] * sample_rate):int(segment["end"] * sample_rate)]
    temporary = DATA / f".slice_{time.time_ns()}.wav"
    try:
        with wave.open(str(temporary), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(sample_rate)
            audio.writeframes((part * 32768.0).clip(-32768, 32767).astype(np.int16).tobytes())
        encode_mp3(temporary, DATA / name)
    finally:
        temporary.unlink(missing_ok=True)
    log_event(started, ended, name, peak_rms(part), segment["score"], segment["bark_seconds"],
              bark_hours(audio_start, segment["windows"]))
    return name


def classify_score(bark_score):
    if bark_score >= bark_high_threshold:
        return "high"
    if bark_score >= bark_score_threshold:
        return "possible"
    return "other"


def log_event(started, ended, filename, peak, bark_score, bark_seconds, hours):
    classification = classify_score(bark_score)
    row = {"started": started.isoformat(timespec="seconds"), "ended": ended.isoformat(timespec="seconds"),
           "duration_seconds": round((ended - started).total_seconds(), 1), "bark_seconds": bark_seconds, "file": filename,
           "peak_rms": round(peak, 4), "classification": classification, "label": text("pl", f"label_{classification}"),
           "is_bark": classification in ("high", "possible"), "false_positive": False,
           "manual_confirmed": False, "bark_score": round(bark_score, 3),
           "bark_hours": hours, "bark_hours_source": "detekcja"}
    with events_lock, EVENTS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def record_candidate_result(started, ended, bark_score=None, bark_seconds=None, peak_rms=None, saved=False,
                            reason="", reason_values=None, filename=None):
    """Keep the reason as a text key, so the panel can show it in the viewer's language."""
    with lock:
        state["last_candidate"] = {
            "checked_at": datetime.now(local_tz).isoformat(timespec="seconds"),
            "started": started.isoformat(timespec="seconds"),
            "ended": ended.isoformat(timespec="seconds"),
            "duration_seconds": round((ended - started).total_seconds(), 1),
            "bark_seconds": bark_seconds,
            "bark_score": round(bark_score, 3) if bark_score is not None else None,
            "peak_rms": round(peak_rms, 4) if peak_rms is not None else None,
            "saved": saved,
            "reason_key": reason,
            "reason_values": reason_values or {},
            "file": filename,
        }


def classify_candidate(path, started):
    """Cut a buffered candidate into recordings around the moments the model detected barking."""
    global classifier, classifying_count
    ended = started
    peak = None
    try:
        samples = read_candidate_samples(path)
        total_seconds = len(samples) / sample_rate
        ended = started + timedelta(seconds=total_seconds)
        peak = peak_rms(samples)
        with classifier_lock:
            if classifier is None:
                classifier = create_interpreter(2)
            scores = window_scores(classifier, samples)
        best = max(scores, default=0.0)
        segments = [segment for segment in bark_segments(scores, total_seconds)
                    if segment["end"] - segment["start"] >= min_event_block_seconds]
        bark_seconds = round(sum(segment["bark_seconds"] for segment in segments), 1)
        if not segments:
            record_candidate_result(started, ended, best, bark_seconds, peak_rms=peak, reason="reason_rejected",
                                    reason_values={"best": f"{best:.3f}", "threshold": f"{bark_score_threshold:.3f}",
                                                   "min_block": f"{min_event_block_seconds:.0f}"})
        else:
            names = [save_segment(samples, segment, started) for segment in segments]
            record_candidate_result(started, ended, best, bark_seconds, peak_rms=peak, saved=True,
                                    reason="reason_saved",
                                    reason_values={"count": len(names), "best": f"{best:.3f}",
                                                   "threshold": f"{bark_score_threshold:.3f}"},
                                    filename=names[-1])
        path.unlink(missing_ok=True)
    except Exception as exc:
        path.unlink(missing_ok=True)
        with lock:
            state["classifier_error"] = str(exc)[:240]
        record_candidate_result(started, ended, peak_rms=peak, saved=False,
                                reason="reason_error", reason_values={"error": str(exc)[:180]})
    finally:
        with lock:
            classifying_count = max(0, classifying_count - 1)
            state["classifying"] = classifying_count > 0


class SourceChanged(Exception):
    pass


def detector():
    active = None
    clip = None
    above = 0
    quiet = 0
    activity = deque(maxlen=int(2 / frame_seconds))

    def finish_candidate():
        global classifying_count
        nonlocal active, clip, quiet
        if active is None or clip is None:
            return
        worker_args = None
        try:
            with clip.open("r+b") as f:
                size = clip.stat().st_size - 44
                f.seek(4); f.write((size + 36).to_bytes(4, "little"))
                f.seek(40); f.write(size.to_bytes(4, "little"))
            worker_args = (clip, active)
        except Exception as exc:
            clip.unlink(missing_ok=True)
            with lock:
                state["classifier_error"] = str(exc)[:240]
        finally:
            active, clip, quiet = None, None, 0
            with lock:
                if worker_args:
                    classifying_count += 1
                state.update(recording=False, candidate_seconds=0, classifying=classifying_count > 0)
        if worker_args:
            threading.Thread(target=classify_candidate, args=worker_args, daemon=True).start()

    while True:
        version = source_version
        source = source_input()
        if not source:
            with lock:
                state.update(connected=False, last_error="", unconfigured=True)
            time.sleep(2)
            continue
        with lock:
            state["unconfigured"] = False
        proc = None
        try:
            proc = run_ffmpeg(source + ["-vn", "-ac", "1", "-ar", str(sample_rate), "-f", "s16le", "-"])
            with lock:
                state.update(connected=True, last_error="")
            while True:
                if version != source_version:
                    raise SourceChanged()
                chunk = read_exact(proc.stdout, frame_bytes)
                if len(chunk) < frame_bytes:
                    raise RuntimeError(t("err_stream_ended"))
                vals = memoryview(chunk).cast("h")
                rms = math.sqrt(sum(v * v for v in vals) / len(vals)) / 32768
                now = datetime.now(local_tz)
                activity.append(rms >= threshold)
                above = sum(activity)
                recent_audio.append(chunk)
                with lock:
                    state["rms"] = round(rms, 4)
                    state["connected"] = True
                if rms >= threshold:
                    quiet = 0
                    if active is None and above * frame_seconds >= min_event:
                        # The clip begins with the pre-roll, so its first sample defines the start.
                        active = now - timedelta(seconds=len(recent_audio) * frame_seconds)
                        stamp = active.strftime("%Y%m%d_%H%M%S")
                        clip = DATA / f".pending_{stamp}.wav"
                        with clip.open("wb") as f:
                            f.write(b"RIFF\x00\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00" + sample_rate.to_bytes(4, "little") + (sample_rate * 2).to_bytes(4, "little") + b"\x02\x00\x10\x00data\x00\x00\x00\x00")
                            for old in list(recent_audio):
                                f.write(old)
                        with lock:
                            state.update(recording=True, candidate_seconds=0)
                    elif active is not None and clip:
                        with clip.open("ab") as f:
                            f.write(chunk)
                else:
                    if active is not None:
                        quiet += 1
                        if clip:
                            with clip.open("ab") as f:
                                f.write(chunk)
                        if quiet * frame_seconds >= merge_gap:
                            finish_candidate()
                if active is not None and (now - active).total_seconds() >= max_candidate_seconds:
                    finish_candidate()
                if active is not None:
                    with lock:
                        state["candidate_seconds"] = round((now - active).total_seconds(), 1)

        except Exception as exc:
            changed = isinstance(exc, SourceChanged)
            detail = ""
            if proc:
                proc.kill()
                try:
                    proc.wait(timeout=2)
                    detail = proc.stderr.read(4096).decode("utf-8", "replace").strip()
                except Exception:
                    pass
            with lock:
                state.update(connected=False, last_error="" if changed else hide_secrets(detail or str(exc))[:240])
            if active is not None:
                finish_candidate()
            if not changed:
                time.sleep(3)


def cleanup():
    cutoff = datetime.now().timestamp() - retention * 86400
    active_files = {Path(e.get("file", "")).name for e in get_events()
                    if not e.get("archived") and float(e.get("duration_seconds", 0) or 0) >= min_event_block_seconds}
    for p in DATA.iterdir():
        if (p.is_file() and p.suffix.lower() in (".mp3", ".wav") and p.name not in active_files
                and p.stat().st_mtime < cutoff):
            p.unlink(missing_ok=True)


def get_events():
    if not EVENTS.exists():
        return []
    rows = []
    with events_lock:
        for line in EVENTS.read_text(encoding="utf-8").splitlines():
            try: rows.append(json.loads(line))
            except json.JSONDecodeError: continue
    return rows


def write_events(rows):
    temporary = EVENTS.with_name("events.jsonl.tmp")
    with events_lock:
        with temporary.open("w", encoding="utf-8") as stream:
            for event in rows:
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        os.replace(temporary, EVENTS)


def event_month(event):
    try:
        ended = datetime.fromisoformat(event["ended"])
        if ended.tzinfo is None:
            ended = ended.replace(tzinfo=local_tz)
        return ended.astimezone(local_tz).strftime("%Y-%m")
    except (KeyError, TypeError, ValueError):
        return ""


def csv_text(rows, language=None):
    """CSV with headers in the chosen language; the columns are the same in every language."""
    language = language or current_language()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(text(language, "csv_headers"))
    for row in rows:
        annotated = annotate_event(row, language)
        writer.writerow(["" if annotated.get(field) is None else annotated.get(field) for field in CSV_FIELDS])
    return "﻿" + output.getvalue()


def archive_completed_months():
    now = datetime.now(local_tz)
    with lock:
        backfill_running = state["backfill_running"]
    # Archiving removes source MP3s, which the hourly re-analysis still needs.
    if now.day < 2 or backfill_running:
        return
    language = settings.get("language", "pl")
    current_month = now.strftime("%Y-%m")
    all_events = get_events()
    months = sorted({event_month(e) for e in all_events
                     if event_month(e) and event_month(e) < current_month
                     and not e.get("archived")
                     and float(e.get("duration_seconds", 0) or 0) >= min_event_block_seconds
                     and score_meets_threshold(e)})
    for month in months:
        month_events = [annotate_event(e) for e in all_events
                        if event_month(e) == month
                        and not e.get("archived")
                        and float(e.get("duration_seconds", 0) or 0) >= min_event_block_seconds
                        and score_meets_threshold(e)]
        if not month_events or any(not event_reviewed(event) for event in month_events):
            continue
        archive_path = ARCHIVES / f"{month}.zip"
        existing_rows = []
        existing_media = set()
        if archive_path.is_file():
            try:
                with zipfile.ZipFile(archive_path, "r") as old_zip:
                    existing_media = {name for name in old_zip.namelist() if name.startswith("nagrania/")}
                    existing_rows = [json.loads(line) for line in old_zip.read("events.jsonl").decode("utf-8-sig").splitlines() if line]
            except Exception:
                with lock:
                    state["last_error"] = text(language, "err_archive_read", month=month)
                continue
        combined = {e.get("file"): dict(e) for e in existing_rows if e.get("file")}
        combined.update({e.get("file"): dict(e) for e in month_events if e.get("file")})
        combined_rows = sorted(combined.values(), key=lambda e: e.get("started", ""))
        for event in combined_rows:
            event["archived"] = True
            event["archive_month"] = month
        bark_rows = [e for e in combined_rows if e.get("classification") in ("high", "possible") and not e.get("false_positive")]
        temporary = ARCHIVES / f".{month}.zip.tmp"
        try:
            with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=4) as new_zip:
                if archive_path.is_file():
                    with zipfile.ZipFile(archive_path, "r") as old_zip:
                        for info in old_zip.infolist():
                            if info.filename not in ARCHIVE_MEMBERS:
                                new_zip.writestr(info, old_zip.read(info.filename))
                new_zip.writestr("raport.csv", csv_text(combined_rows, language))
                new_zip.writestr("raport-szczekania.csv", csv_text(bark_rows, language))
                new_zip.writestr("events.jsonl", "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in combined_rows))
                for event in month_events:
                    name = Path(event.get("file", "")).name
                    member = f"nagrania/{name}"
                    source = DATA / name
                    if name and member not in existing_media and source.is_file() and source.suffix.lower() in (".wav", ".mp3"):
                        new_zip.write(source, member)
            with zipfile.ZipFile(temporary, "r") as check:
                if check.testzip() is not None:
                    raise RuntimeError(text(language, "err_archive_check"))
            os.replace(temporary, archive_path)
            latest = get_events()
            month_files = {e.get("file") for e in combined_rows}
            for event in latest:
                if event.get("file") in month_files:
                    event["archived"] = True
                    event["archive_month"] = month
            write_events(latest)
            for event in combined_rows:
                name = Path(event.get("file", "")).name
                if name:
                    (DATA / name).unlink(missing_ok=True)
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            with lock:
                state["last_error"] = text(language, "err_archive_failed", month=month, error=str(exc)[:160])


def migrate_legacy_wavs():
    events = get_events()
    converted = {}
    for source in DATA.glob("szczekanie_*.wav"):
        destination = source.with_suffix(".mp3")
        try:
            if not destination.is_file() or destination.stat().st_size == 0:
                encode_mp3(source, destination)
            converted[source.name] = destination.name
        except Exception:
            continue
    if not converted:
        return
    for event in events:
        if event.get("file") in converted:
            event["file"] = converted[event["file"]]
    write_events(events)
    for old_name in converted:
        (DATA / old_name).unlink(missing_ok=True)


def archive_scheduler():
    while True:
        try:
            archive_completed_months()
            cleanup()
        except Exception as exc:
            with lock:
                state["last_error"] = text(settings.get("language", "pl"), "err_archive_loop", error=str(exc)[:160])
        time.sleep(3600)


def confirmed_bark_events():
    return [e for e in eligible_events() if (e.get("classification") in ("high", "possible") or e.get("manual_confirmed")) and not e.get("false_positive")]


def eligible_events(include_archived=False):
    return [annotate_event(e) for e in get_events()
            if float(e.get("duration_seconds", 0) or 0) >= min_event_block_seconds
            and score_meets_threshold(e)
            and (include_archived or not e.get("archived", False))]


def score_meets_threshold(event):
    try:
        score = float(event.get("bark_score"))
    except (TypeError, ValueError):
        return False
    return score >= bark_score_threshold


def event_reviewed(event):
    return bool(event.get("manual_confirmed") or event.get("false_positive") or event.get("reviewed_at"))


def purge_below_score_events():
    """Permanently remove stored events and archived material below the active model cutoff."""
    language = settings.get("language", "pl")
    with events_lock:
        rows = get_events()
        keep = [event for event in rows if score_meets_threshold(event)]
        removed = [event for event in rows if not score_meets_threshold(event)]
        if len(keep) != len(rows):
            write_events(keep)
        for event in removed:
            filename = Path(event.get("file", "")).name
            if filename and filename == event.get("file"):
                (DATA / filename).unlink(missing_ok=True)

    for archive_path in ARCHIVES.glob("????-??.zip"):
        temporary = archive_path.with_name(f".{archive_path.name}.tmp")
        try:
            with zipfile.ZipFile(archive_path, "r") as old_zip:
                infos = old_zip.infolist()
                archived_rows = [json.loads(line) for line in old_zip.read("events.jsonl").decode("utf-8-sig").splitlines() if line]
                valid_rows = [event for event in archived_rows if score_meets_threshold(event)]
                if len(valid_rows) == len(archived_rows):
                    continue
                valid_files = {Path(event.get("file", "")).name for event in valid_rows}
                bark_rows = [event for event in valid_rows
                             if event.get("classification") in ("high", "possible") and not event.get("false_positive")]
                with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=4) as new_zip:
                    for info in infos:
                        name = info.filename
                        if name in ARCHIVE_MEMBERS:
                            continue
                        if name.startswith("nagrania/") and Path(name).name not in valid_files:
                            continue
                        new_zip.writestr(info, old_zip.read(name))
                    new_zip.writestr("raport.csv", csv_text(valid_rows, language))
                    new_zip.writestr("raport-szczekania.csv", csv_text(bark_rows, language))
                    new_zip.writestr("events.jsonl", "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in valid_rows))
            with zipfile.ZipFile(temporary, "r") as check:
                if check.testzip() is not None:
                    raise RuntimeError(text(language, "err_archive_check"))
            os.replace(temporary, archive_path)
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            with lock:
                state["last_error"] = text(language, "err_archive_clean", file=archive_path.name, error=str(exc)[:140])


def decode_recording(path):
    import numpy as np

    result = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(path),
         "-vn", "-ac", "1", "-ar", str(sample_rate), "-f", "s16le", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    return np.frombuffer(result.stdout, dtype="<i2").astype(np.float32) / 32768.0


def hours_from_recording(event, model):
    """Spread an existing event's estimate over clock hours without changing the estimate itself."""
    started = datetime.fromisoformat(event["started"])
    stored = float(event.get("bark_seconds") or 0)
    fallback = ({started.astimezone(local_tz).strftime("%Y-%m-%dT%H"): round(stored, 1)} if stored else {}, "start_bloku")
    path = DATA / Path(event.get("file", "")).name
    if not path.is_file():
        return fallback
    scores = window_scores(model, decode_recording(path))
    windows = [index for index, score in enumerate(scores) if score >= bark_score_threshold]
    if not windows:
        return fallback
    hours = bark_hours(started, windows)
    if stored:
        factor = stored / (len(windows) * hop_seconds)
        hours = {key: round(value * factor, 1) for key, value in hours.items()}
    return hours, "reanaliza"


def backfill_bark_hours():
    """Add the hourly breakdown to events saved before detections were tracked per hour."""
    model = None
    try:
        pending = [event.get("file") for event in get_events() if "bark_hours" not in event]
        for number, filename in enumerate(pending, start=1):
            event = next((e for e in get_events() if e.get("file") == filename), None)
            if event is not None and "bark_hours" not in event:
                if model is None:
                    model = create_interpreter(1)
                try:
                    hours, source = hours_from_recording(event, model)
                except Exception:
                    # A damaged file must not hold up archiving or the remaining events.
                    stored = float(event.get("bark_seconds") or 0)
                    start_hour = datetime.fromisoformat(event["started"]).astimezone(local_tz).strftime("%Y-%m-%dT%H")
                    hours, source = ({start_hour: round(stored, 1)} if stored else {}), "start_bloku"
                with events_lock:
                    rows = get_events()
                    target = next((e for e in rows if e.get("file") == filename), None)
                    if target is not None:
                        target["bark_hours"] = hours
                        target["bark_hours_source"] = source
                        write_events(rows)
            with lock:
                state["backfill_done"] = number
    except Exception as exc:
        with lock:
            state["last_error"] = text(settings.get("language", "pl"), "err_backfill", error=str(exc)[:140])
    finally:
        with lock:
            state["backfill_running"] = False


def recover_pending_candidates():
    """Finalize and classify audio buffered when the container was restarted."""
    for path in DATA.glob(".pending_*.wav"):
        try:
            started = datetime.strptime(path.stem.removeprefix(".pending_"), "%Y%m%d_%H%M%S").replace(tzinfo=local_tz)
            size = path.stat().st_size
            if size <= 44:
                path.unlink(missing_ok=True)
                continue
            data_size = size - 44
            with path.open("r+b") as stream:
                stream.seek(4)
                stream.write((size - 8).to_bytes(4, "little"))
                stream.seek(40)
                stream.write(data_size.to_bytes(4, "little"))
            classify_candidate(path, started)
        except Exception as exc:
            with lock:
                state["last_error"] = text(settings.get("language", "pl"), "err_recover", file=path.name, error=str(exc)[:140])


def normalize_event(event):
    row = dict(event)
    classification = row.get("classification")
    score = row.get("bark_score")
    try:
        score = float(score) if score is not None else None
    except (TypeError, ValueError):
        score = None
    if classification not in ("high", "possible", "other", "unknown"):
        if row.get("is_bark") is True or row.get("label") == "Szczekanie" or (score is not None and score >= bark_score_threshold):
            classification = "high" if score is not None and score >= bark_high_threshold else "possible"
        else:
            classification = "other" if row.get("label") or score is not None else "unknown"
    row["classification"] = classification
    row["false_positive"] = bool(row.get("false_positive", False))
    row["manual_confirmed"] = bool(row.get("manual_confirmed", False)) and not row["false_positive"]
    if score is not None:
        row["bark_score"] = score
    return row


def annotate_event(event, language=None):
    row = normalize_event(event)
    language = language or current_language()
    block_duration = float(row.get("duration_seconds", 0) or 0)
    bark_duration = float(row.get("bark_seconds", 0) or 0)
    row["label"] = text(language, f"label_{row['classification']}")
    row["dlugosc_bloku_s"] = block_duration
    row["szczekanie_model_szac_s"] = bark_duration
    row["epizod_reczny_s"] = block_duration if row["manual_confirmed"] else ""
    row["epizod_uwaga"] = text(language, "episode_note") if row["manual_confirmed"] else ""
    row["ponad_1_min"] = bark_duration > 60
    row["ponad_5_min"] = bark_duration > 300
    row["ponad_10_min"] = bark_duration > 600
    row["blok_ponad_1_min"] = block_duration > 60
    row["blok_ponad_5_min"] = block_duration > 300
    row["blok_ponad_10_min"] = block_duration > 600
    row["noc_22_7"] = event_is_night(row.get("started"), row.get("ended"))
    return row


# --- Access control ---------------------------------------------------------------------------

def secret_key():
    try:
        return SECRET_FILE.read_bytes()
    except OSError:
        key = secrets.token_bytes(32)
        SECRET_FILE.write_bytes(key)
        os.chmod(SECRET_FILE, 0o600)
        return key


app.secret_key = secret_key()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  PERMANENT_SESSION_LIFETIME=timedelta(days=30))
PUBLIC_ENDPOINTS = {"login", "set_language", "static"}
PROXY_HEADERS = ("X-Forwarded-For", "X-Real-IP", "Forwarded", "CF-Connecting-IP", "True-Client-IP")


def via_proxy():
    return any(request.headers.get(name) for name in PROXY_HEADERS)


def is_logged_in():
    return bool(session.get("user")) and session.get("user") == settings.get("auth_user")


def wants_json():
    return request.path.startswith(("/api/", "/data/", "/download/", "/live"))


@app.before_request
def guard():
    if request.endpoint in PUBLIC_ENDPOINTS:
        return None
    if settings.get("auth_enabled"):
        if not is_logged_in():
            if wants_json():
                return jsonify(error=t("api_login_required")), 401
            return redirect(f"/login?next={quote(request.full_path.rstrip('?'), safe='/?=&')}")
    elif via_proxy() and not env_flag("ALLOW_PROXY_WITHOUT_LOGIN"):
        return render_template("message.html", title=t("proxy_title"), message=t("proxy_text"), **page_context()), 403
    # Changes need a header that other websites cannot add, which stops cross-site requests.
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("X-Requested-With") != "PiesWatch":
        return jsonify(error=t("api_bad_request")), 403
    return None


def client_address():
    return request.remote_addr or "unknown"


def too_many_failures(address):
    now = time.time()
    recent = [moment for moment in login_failures.get(address, []) if now - moment < 300]
    login_failures[address] = recent
    return len(recent) >= 5


def page_context():
    language = current_language()
    return {"T": TEXTS[language], "lang": language, "version": VERSION, "author_url": AUTHOR_URL,
            "auth_enabled": bool(settings.get("auth_enabled")), "logged_in": is_logged_in()}


def safe_next(value):
    return value if value and value.startswith("/") and not value.startswith("//") else "/"


@app.route("/login", methods=["GET", "POST"])
def login():
    if not settings.get("auth_enabled"):
        return redirect("/")
    error = ""
    if request.method == "POST":
        address = client_address()
        if too_many_failures(address):
            error = t("login_locked")
        elif (request.form.get("user", "") == settings.get("auth_user")
              and check_password_hash(settings.get("auth_password_hash", ""), request.form.get("password", ""))):
            login_failures.pop(address, None)
            session.clear()
            session.permanent = True
            session["user"] = settings["auth_user"]
            return redirect(safe_next(request.form.get("next")))
        else:
            login_failures.setdefault(address, []).append(time.time())
            error = t("login_failed")
    return render_template("login.html", error=error, next=safe_next(request.args.get("next") or request.form.get("next")),
                           **page_context()), (401 if error else 200)


@app.post("/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


@app.get("/lang/<code>")
def set_language(code):
    response = redirect(safe_next(request.args.get("next")))
    if code in LANGUAGES:
        response.set_cookie("lang", code, max_age=365 * 86400, samesite="Lax")
    return response


# --- Panel -------------------------------------------------------------------------------------

@app.get("/")
def index():
    return render_template("index.html", place_name=settings.get("place_name", ""),
                           source_type=settings.get("source_type"), **page_context())


def public_settings():
    values = {key: value for key, value in settings.items() if key not in SECRET_FIELDS}
    values["source_password_set"] = bool(settings.get("source_password"))
    values["auth_password_set"] = bool(settings.get("auth_password_hash"))
    values["port"] = os.getenv("PORT", "1717")
    return values


@app.get("/settings")
def settings_page():
    return render_template("settings.html", **page_context())


@app.get("/api/settings")
def api_settings():
    return jsonify(public_settings())


@app.post("/api/settings")
def save_settings():
    incoming = request.get_json(silent=True)
    if not isinstance(incoming, dict):
        return jsonify(error=t("api_bad_request")), 400
    with settings_lock:
        values, error = validate_settings(settings, incoming, current_language())
        if error:
            return jsonify(error=error), 400
        write_settings(values)
        apply_settings(values)
    if values["auth_enabled"]:
        # Whoever just turned on login (or changed it) stays signed in.
        session.permanent = True
        session["user"] = values["auth_user"]
    else:
        session.pop("user", None)
    response = jsonify(public_settings())
    if "language" in incoming:
        response.set_cookie("lang", values["language"], max_age=365 * 86400, samesite="Lax")
    return response


@app.get("/api/status")
def api_status():
    cleanup()
    events = eligible_events()
    now = datetime.now(local_tz)
    today = [e for e in events if e.get("started", "")[:10] == now.date().isoformat()]
    week = [e for e in events if datetime.fromisoformat(e["started"]) >= now - timedelta(days=7)]
    today_high = [e for e in today if e.get("classification") == "high" and not e.get("false_positive")]
    today_possible = [e for e in today if e.get("classification") == "possible" and not e.get("false_positive")]
    all_high = [e for e in events if e.get("classification") == "high" and not e.get("false_positive")]
    all_possible = [e for e in events if e.get("classification") == "possible" and not e.get("false_positive")]
    today_barks = [e for e in today if (e.get("classification") in ("high", "possible") or e.get("manual_confirmed")) and not e.get("false_positive")]
    week_barks = [e for e in week if (e.get("classification") in ("high", "possible") or e.get("manual_confirmed")) and not e.get("false_positive")]
    bark_events = [e for e in events if (e.get("classification") in ("high", "possible") or e.get("manual_confirmed")) and not e.get("false_positive")]
    manual_today = sum(1 for e in today if e.get("manual_confirmed") and not e.get("false_positive"))
    manual_total = sum(1 for e in events if e.get("manual_confirmed") and not e.get("false_positive"))
    night_total = sum(1 for e in events if e.get("noc_22_7") and not e.get("false_positive"))
    manual_episode_today = sum(e.get("epizod_reczny_s") or 0 for e in today if not e.get("false_positive"))
    block_today = sum(e.get("duration_seconds", 0) or 0 for e in today_barks)
    with lock:
        info = dict(state)
    candidate = info.get("last_candidate")
    if candidate:
        candidate = dict(candidate)
        candidate["reason"] = t(candidate.pop("reason_key"), **candidate.pop("reason_values"))
        info["last_candidate"] = candidate
    info.update(events_total=len(bark_events), events_today=len(today_barks), events_week=len(week_barks),
                candidates_total=len(events),
                high_today=len(today_high), possible_today=len(today_possible),
                high_total=len(all_high), possible_total=len(all_possible),
                manual_today=manual_today, manual_total=manual_total,
                night_total=night_total,
                duration_today=round(sum(e.get("bark_seconds") or 0 for e in today_barks) / 60, 1),
                block_today=round(block_today / 60, 1),
                manual_episode_today=round(manual_episode_today / 60, 1),
                last_event=events[-1] if events else None, threshold=threshold,
                bark_threshold=bark_score_threshold, bark_high_threshold=bark_high_threshold,
                merge_gap_seconds=merge_gap,
                min_event_block_seconds=min_event_block_seconds,
                source_type=settings.get("source_type"), version=VERSION)
    return jsonify(info)


@app.get("/api/events")
def api_events():
    return jsonify(eligible_events()[::-1])


def week_heatmap(day):
    monday = day - timedelta(days=day.weekday())
    sunday = monday + timedelta(days=6)
    cells = {}
    week_seconds = 0.0
    week_events = set()
    week_confirmed = set()
    missing = 0
    first_day = None
    for event in eligible_events(include_archived=True):
        started_day = datetime.fromisoformat(event["started"]).astimezone(local_tz).date()
        first_day = min(first_day, started_day) if first_day else started_day
        if event.get("false_positive"):
            continue
        if "bark_hours" not in event:
            missing += 1
            continue
        for key, seconds in event["bark_hours"].items():
            moment = datetime.strptime(key, "%Y-%m-%dT%H")
            if not monday <= moment.date() <= sunday:
                continue
            cell = cells.setdefault(((moment.date() - monday).days, moment.hour), {"seconds": 0.0, "events": 0, "confirmed": 0})
            cell["seconds"] += seconds
            cell["events"] += 1
            cell["confirmed"] += 1 if event.get("manual_confirmed") else 0
            week_seconds += seconds
            week_events.add(event.get("file"))
            if event.get("manual_confirmed"):
                week_confirmed.add(event.get("file"))
    today = datetime.now(local_tz).date()
    return {"monday": monday.isoformat(), "sunday": sunday.isoformat(), "today": today.isoformat(),
            "days": [(monday + timedelta(days=offset)).isoformat() for offset in range(7)],
            "cells": [{"day": key[0], "hour": key[1], "seconds": round(value["seconds"], 1),
                       "events": value["events"], "confirmed": value["confirmed"]} for key, value in sorted(cells.items())],
            "total_seconds": round(week_seconds, 1), "total_events": len(week_events), "total_confirmed": len(week_confirmed),
            "events_without_hours": missing,
            "previous_week": (monday - timedelta(days=7)).isoformat() if first_day and first_day < monday else None,
            "next_week": (monday + timedelta(days=7)).isoformat() if sunday < today else None}


@app.get("/api/week-heatmap")
def api_week_heatmap():
    value = request.args.get("day")
    try:
        day = datetime.strptime(value, "%Y-%m-%d").date() if value else datetime.now(local_tz).date()
    except ValueError:
        return jsonify(error=t("api_bad_date")), 400
    return jsonify(week_heatmap(day))


@app.get("/api/archives")
def api_archives():
    archived = get_events()
    result = []
    for path in sorted(ARCHIVES.glob("????-??.zip"), reverse=True):
        month = path.stem
        count = sum(1 for event in archived if event.get("archived") and event.get("archive_month") == month)
        result.append({"month": month, "events": count, "size_mb": round(path.stat().st_size / (1024 * 1024), 2),
                       "url": f"/download/archives/{path.name}"})
    return jsonify(result)


@app.get("/download/archives/<path:name>")
def download_monthly_archive(name):
    if Path(name).name != name or len(name) != 11 or not name.endswith(".zip"):
        return Response(t("api_bad_archive"), status=400)
    try:
        datetime.strptime(name[:7], "%Y-%m")
    except ValueError:
        return Response(t("api_bad_archive"), status=400)
    path = (ARCHIVES / name).resolve()
    if path.parent != ARCHIVES.resolve() or not path.is_file():
        return Response(t("api_no_archive"), status=404)
    return send_file(path, mimetype="application/zip", as_attachment=True, download_name=name)


def valid_recording_name(filename):
    return filename == Path(filename).name and Path(filename).suffix.lower() in (".wav", ".mp3")


@app.post("/api/events/<path:filename>/review")
def review_event(filename):
    if not valid_recording_name(filename):
        return jsonify(error=t("api_bad_file")), 400
    payload = request.get_json(silent=True) or {}
    false_positive = payload.get("false_positive")
    if not isinstance(false_positive, bool):
        return jsonify(error=t("api_missing_value", field="false_positive")), 400
    with events_lock:
        rows = get_events()
        target = next((e for e in rows if e.get("file") == filename), None)
        if target is None:
            return jsonify(error=t("api_no_event")), 404
        target["false_positive"] = false_positive
        if false_positive:
            target["manual_confirmed"] = False
            target.pop("manual_confirmed_at", None)
            target["reviewed_at"] = datetime.now(local_tz).isoformat(timespec="seconds")
        else:
            target.pop("reviewed_at", None)
        write_events(rows)
    return jsonify(annotate_event(target))


@app.post("/api/events/<path:filename>/confirm")
def confirm_event(filename):
    if not valid_recording_name(filename):
        return jsonify(error=t("api_bad_file")), 400
    payload = request.get_json(silent=True) or {}
    manual_confirmed = payload.get("manual_confirmed")
    if not isinstance(manual_confirmed, bool):
        return jsonify(error=t("api_missing_value", field="manual_confirmed")), 400
    with events_lock:
        rows = get_events()
        target = next((e for e in rows if e.get("file") == filename), None)
        if target is None:
            return jsonify(error=t("api_no_event")), 404
        if target.get("archived"):
            return jsonify(error=t("api_archived_change")), 409
        target["manual_confirmed"] = manual_confirmed
        if manual_confirmed:
            target["false_positive"] = False
            target["manual_confirmed_at"] = datetime.now(local_tz).isoformat(timespec="seconds")
        else:
            target.pop("manual_confirmed_at", None)
        write_events(rows)
    return jsonify(annotate_event(target))


@app.delete("/api/events/<path:filename>")
def delete_event(filename):
    if not valid_recording_name(filename):
        return jsonify(error=t("api_bad_file")), 400
    with events_lock:
        rows = get_events()
        target = next((e for e in rows if e.get("file") == filename), None)
        if target is None:
            return jsonify(error=t("api_no_event")), 404
        if target.get("archived"):
            return jsonify(error=t("api_archived_delete")), 409
        source = DATA / filename
        staged = DATA / f".deleting_{time.time_ns()}_{filename}"
        moved = False
        try:
            if source.is_file():
                source.replace(staged)
                moved = True
            write_events([event for event in rows if event.get("file") != filename])
        except Exception:
            if moved and staged.exists():
                staged.replace(source)
            return jsonify(error=t("api_delete_failed")), 500
        if moved:
            staged.unlink(missing_ok=True)
    return jsonify(deleted=True, file=filename)


@app.get("/data/<path:filename>")
def download_event(filename):
    path = (DATA / filename).resolve()
    if DATA.resolve() not in path.parents or not path.is_file() or path.suffix.lower() not in (".wav", ".mp3"):
        return Response(t("api_no_recording"), status=404)
    mimetype = "audio/mpeg" if path.suffix.lower() == ".mp3" else "audio/wav"
    return send_file(path, mimetype=mimetype, as_attachment=False, conditional=True)


def preview_source():
    """Live preview opens its own connection, which a microphone cannot share with the detector."""
    if settings.get("source_type") == "microphone":
        return None
    return source_input()


@app.get("/live.mjpg")
def live():
    source = preview_source()
    if not source:
        return Response(t("api_no_source"), status=503)
    def frames():
        proc = run_ffmpeg(source + ["-an", "-vf", "fps=5,scale=960:-1", "-f", "mjpeg", "-"])
        buf = b""
        try:
            while True:
                part = proc.stdout.read(4096)
                if not part: break
                buf += part
                while b"\xff\xd8" in buf and b"\xff\xd9" in buf:
                    start, end = buf.find(b"\xff\xd8"), buf.find(b"\xff\xd9") + 2
                    frame, buf = buf[start:end], buf[end:]
                    yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
        finally:
            proc.kill()
    return Response(frames(), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.get("/live-audio.mp3")
def live_audio():
    source = preview_source()
    if not source:
        return Response(t("api_no_source"), status=503)
    def audio_stream():
        proc = run_ffmpeg(source + ["-vn", "-ac", "1", "-ar", "44100", "-b:a", "96k", "-f", "mp3", "pipe:1"])
        try:
            while True:
                part = proc.stdout.read(4096)
                if not part:
                    break
                yield part
        finally:
            proc.kill()
    return Response(audio_stream(), mimetype="audio/mpeg", headers={"Cache-Control": "no-store"})


def recordings_zip(events, with_report=False):
    memory = io.BytesIO()
    folder = t("zip_recordings")
    with zipfile.ZipFile(memory, "w", zipfile.ZIP_DEFLATED) as z:
        if with_report:
            z.writestr(t("zip_bark_report"), csv_text(events))
        for event in events:
            name = Path(event.get("file", "")).name
            path = DATA / name
            if path.suffix.lower() in (".wav", ".mp3") and path.is_file():
                z.write(path, f"{folder}/{name}")
        z.writestr("events.jsonl", "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events))
    memory.seek(0)
    return memory


def csv_response(rows, filename):
    return Response(csv_text(rows), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


@app.get("/download/report.csv")
def report():
    return csv_response(eligible_events(), t("file_report"))


@app.get("/download/sources.zip")
def sources():
    return send_file(recordings_zip(eligible_events()), mimetype="application/zip", as_attachment=True,
                     download_name=t("file_sources"))


@app.get("/download/bark-report.csv")
def bark_report():
    return csv_response(confirmed_bark_events(), t("file_bark_report"))


@app.get("/download/bark-sources.zip")
def bark_sources():
    return send_file(recordings_zip(confirmed_bark_events()), mimetype="application/zip", as_attachment=True,
                     download_name=t("file_bark_sources"))


@app.get("/download/bark-pack.zip")
def bark_pack():
    return send_file(recordings_zip(confirmed_bark_events(), with_report=True), mimetype="application/zip",
                     as_attachment=True, download_name=t("file_bark_pack"))


def update_stored_settings(changes):
    """Change only the given keys in settings.json, leaving values that still come from .env alone."""
    stored = {}
    if SETTINGS_FILE.is_file():
        stored = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    stored.update(changes)
    write_settings(stored)


def command_line(arguments):
    """Maintenance commands, run with: docker compose exec pies-watch python app.py <command>"""
    if arguments[:1] == ["--disable-login"]:
        update_stored_settings({"auth_enabled": False})
        print("Logowanie wyłączone / Login turned off. Run: docker compose restart")
    elif arguments[:1] == ["--set-login"] and len(arguments) == 2:
        password = sys.stdin.readline().rstrip("\n")
        if len(password) < 8 or not arguments[1].strip():
            print("Hasło musi mieć co najmniej 8 znaków / The password needs at least 8 characters")
            return 1
        update_stored_settings({"auth_enabled": True, "auth_user": arguments[1].strip(),
                                "auth_password_hash": generate_password_hash(password)})
        print("Logowanie włączone / Login turned on. Run: docker compose restart")
    else:
        print("Usage: python app.py --disable-login | --set-login USER  (password on standard input)")
        return 2
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1:
        sys.exit(command_line(sys.argv[1:]))
    migrate_legacy_wavs()
    recover_pending_candidates()
    purge_below_score_events()
    backfill_total = sum(1 for event in get_events() if "bark_hours" not in event)
    state.update(backfill_running=backfill_total > 0, backfill_total=backfill_total)
    archive_completed_months()
    if backfill_total:
        threading.Thread(target=backfill_bark_hours, daemon=True).start()
    threading.Thread(target=archive_scheduler, daemon=True).start()
    threading.Thread(target=detector, daemon=True).start()
    app.run(host="0.0.0.0", port=1717, threaded=True)
