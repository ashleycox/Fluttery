"""Fluttery's session document and compatibility with Electron session files."""

from __future__ import annotations

import json
import math
import time
import zlib
from collections.abc import Mapping
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, ZipFile

from .audio.session_capture import (
    ENCODINGS,
    MAX_AUDIO_BYTES,
    MAX_AUDIO_CHANNELS,
    MAX_BLOCK_FRAMES,
    AudioBlock,
    CapturedInput,
)
from .staging import atomic_output

# Full-resolution native histories can be larger than old Electron sessions.
MAX_SESSION_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
RUN_KEYS = ("history", "spectra", "recordedAudio", "inputAudio", "signalLoss", "runSettings", "runRetention")
MEASUREMENT_TYPES = ("rpm", "crosstalk", "balance", "thd", "noise", "resonance", "tracking", "alignment",
                     "frequency-response", "bench-imd", "bench-distortion")
METADATA_FIELDS = ("technician", "equipment", "notes")


def number(value: object, fallback: float | None = 0.0) -> float | None:
    """Treat non-numeric and non-finite input as missing data."""
    try:
        parsed = float(value)
    except (ValueError, TypeError, OverflowError):
        return fallback
    return parsed if math.isfinite(parsed) else fallback


def new_session(technician: str = "") -> dict:
    return {
        "version": "1.0",
        "timestamp": time.time_ns() // 1_000_000,
        "history": [],
        "measurementLog": [],
        "metadata": {"technician": technician, "equipment": "", "notes": ""},
        "settings": {},
    }


def _object(value: object) -> dict:
    return dict(value) if isinstance(value, Mapping) else {}


def _samples(value: object) -> list:
    return value if isinstance(value, list) else []


def _peak_values(value: object) -> dict:
    source = _object(value)
    peak = number(source.get("peak"), None)
    return {
        **source,
        "peak": peak,
        "bufferPeak": number(source.get("bufferPeak"), peak),
        "rms": number(source.get("rms"), None),
    }


def _spectrum_values(raw):
    spectrum = _samples(raw.get("spectrum"))
    if raw.get("spectrumEncoding") == "db":
        return [number(value, -120) for value in spectrum]
    return [min(255, max(0, math.floor(number(value) + 0.5))) for value in spectrum]


def _sample_rows(source, default_time):
    return [{
        **_peak_values(raw),
        "currentFreq": number(raw.get("currentFreq"), None),
        "inputLevel": number(raw.get("inputLevel"), -100),
        "activeTarget": number(raw.get("activeTarget"), 3000),
        "wow": _peak_values(raw.get("wow")),
        "flutter": _peak_values(raw.get("flutter")),
        "spectrum": _spectrum_values(raw),
        "timestamp": number(raw.get("timestamp"), default_time),
    } for raw in _samples(source) if isinstance(raw, Mapping)]


def _run_data(source, default_time):
    run = {key: source[key] for key in RUN_KEYS if key in source}
    if "inputAudio" in run and not isinstance(run["inputAudio"], CapturedInput):
        raise ValueError("Original input audio must be stored inside a Fluttery session archive.")
    run["history"] = _sample_rows(source.get("history"), default_time)
    if "spectra" in source:
        run["spectra"] = [{**row, "spectrum": _spectrum_values(row)} for row in _samples(source.get("spectra"))
                          if isinstance(row, Mapping) and number(row.get("timestamp"), None) is not None]
    if "signalLoss" in source:
        run["signalLoss"] = [event for event in _samples(source["signalLoss"]) if isinstance(event, Mapping)]
    for key in ("recordedAudio", "runSettings", "runRetention"):
        if key in run:
            run[key] = _object(run[key])
    return run


def archive_run(session, *, settings=None):
    """Retain the current measurements when the next test replaces live history."""
    if any(session.get(key) for key in ("history", "inputAudio", "spectra", "signalLoss")):
        run = {key: session[key] for key in RUN_KEYS if key in session}
        if "runSettings" not in run:
            run["runSettings"] = _object(settings if settings is not None else session.get("settings"))
        session.setdefault("previousRuns", []).append(run)


def clear_run(session):
    for key in RUN_KEYS:
        session.pop(key, None)
    session["history"] = []


def normalise_session(source: object) -> dict:
    """Read both session and export envelopes and retain their additional fields."""
    if not isinstance(source, Mapping) or not ({"sessionLog", "history", "measurementLog"} & source.keys()):
        raise ValueError("This file is not a Fluttery session or JSON export.")
    session = new_session()
    settings = _object(source.get("settings"))
    if "exportSettings" not in settings and ("filenameTemplate" in settings or "paperSize" in settings):
        # JSON exports used the settings object for report options directly.
        settings["exportSettings"] = dict(settings)
    metadata = _object(source.get("metadata"))
    # Early sessions kept the technician in settings. An explicitly empty
    # session field takes precedence over that legacy default.
    if "technician" not in metadata:
        metadata["technician"] = settings.get("technician", "")
    for field in METADATA_FIELDS:
        if not isinstance(metadata.get(field), str):
            metadata[field] = ""

    # Electron gives sessionLog priority, even if it is an empty list.
    history_source = source.get("sessionLog")
    if not isinstance(history_source, list):
        history_source = source.get("history")
    default_time = number(source.get("timestamp"), session["timestamp"]) / 1000

    measurements = []
    for raw in _samples(source.get("measurementLog")):
        if not isinstance(raw, Mapping) or not isinstance(raw.get("type"), str):
            continue
        data = raw.get("data")
        if data is None or data == []:
            continue
        measurements.append({
            **raw,
            "timestamp": number(raw.get("timestamp"), default_time),
            "stats": _object(raw.get("stats")) or None,
        })

    session.update(source)
    # The app owns a single mutable history list. Write the legacy alias when
    # saving instead of letting the two copies drift apart in memory.
    session.pop("sessionLog", None)
    session.update(_run_data({**source, "history": history_source}, default_time))
    session.update(measurementLog=measurements, metadata=metadata, settings=settings)
    if "previousRuns" in source:
        session["previousRuns"] = [_run_data(run, default_time) for run in _samples(source.get("previousRuns"))
                                   if isinstance(run, Mapping)]
    return session


def load_session(path: str | Path) -> dict:
    with Path(path).open("rb") as stream:
        if stream.read(4) == b"PK\x03\x04":
            stream.seek(0)
            return _load_archive(stream)
        stream.seek(0)
        content = stream.read(MAX_SESSION_BYTES + 1)
    if len(content) > MAX_SESSION_BYTES:
        raise ValueError("This session is too large to open.")
    return normalise_session(_read_document(content))


def _read_document(content):
    # Older exports could contain non-finite measurements. Treat them as
    # unavailable throughout the document so a reopened file can be saved.
    return json.loads(content, parse_float=lambda value: number(value, None), parse_constant=lambda _value: None)


def session_payload(session: Mapping, settings: Mapping | None = None) -> dict:
    settings = _object(session.get("settings")) if settings is None else _object(settings)
    saved = normalise_session({**session, "settings": settings})
    saved["timestamp"] = time.time_ns() // 1_000_000
    saved["sessionLog"] = saved["history"]
    return saved


def atomic_write(path: str | Path, data: bytes) -> None:
    """Replace a completed file in one move, preserving the prior file on failure."""
    with atomic_output(path) as stream:
        stream.write(data)


def session_document_bytes(payload: Mapping, *, readable=False) -> bytes:
    def encode(**options):
        return (json.dumps(payload, ensure_ascii=False, allow_nan=False, **options) + "\n").encode("utf-8")

    if readable and len(content := encode(indent=2)) <= MAX_SESSION_BYTES:
        return content
    # Spectrum rows contain many numbers, and .fluttery stores both legacy
    # history keys. Formatting can put valid sessions over the reopen limit.
    content = encode(separators=(",", ":"))
    if len(content) > MAX_SESSION_BYTES:
        raise ValueError("This session is too large to save as a Fluttery file.")
    return content


def save_session(path: str | Path, session: Mapping, *, settings: Mapping | None = None) -> None:
    saved = session_payload(session, settings)
    if (saved.get("previousRuns") or saved.get("inputAudio")
            or _object(saved.get("runRetention")).get("keepCompleteHistory")):
        saved.pop("sessionLog", None)
        _save_archive(path, saved)
    else:
        atomic_write(path, session_document_bytes(saved))


def _archive_audio(archive, run, index):
    audio = run.get("inputAudio")
    if not isinstance(audio, CapturedInput):
        return
    blocks = []
    for position, block in enumerate(audio.blocks):
        name = f"input/{index}/{position}.pcmz"
        # The worker already compressed the input in continuous blocks.
        archive.writestr(name, block.compressed, compress_type=ZIP_STORED)
        blocks.append([block.rate, block.channels, block.frames, block.segment, block.encoding])
    run["inputAudio"] = {"format": "pcm-zlib", "blocks": blocks, "incompleteReason": audio.incomplete_reason}


def _save_archive(path, payload):
    payload["version"] = "2.0"
    runs = [payload, *payload.get("previousRuns", [])]
    audio_total = sum(audio.stored_bytes for run in runs if isinstance(audio := run.get("inputAudio"), CapturedInput))
    if audio_total > MAX_ARCHIVE_BYTES - MAX_MANIFEST_BYTES:
        raise ValueError("The session contains too much input audio. Remove an older capture or save a smaller session.")
    encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    with atomic_output(path) as stream, ZipFile(stream, "w", compression=ZIP_DEFLATED, compresslevel=5) as archive:
        for index, run in enumerate(runs):
            _archive_audio(archive, run, index)
        with archive.open("session.json", "w", force_zip64=True) as manifest:
            size = 0
            for chunk in encoder.iterencode(payload):
                data = chunk.encode("utf-8")
                size += len(data)
                if size > MAX_MANIFEST_BYTES:
                    raise ValueError("This session contains too many measurements to save as one file.")
                manifest.write(data)


def _restore_audio(archive, run, index):
    audio = run.get("inputAudio")
    if audio is None:
        return 0
    if not isinstance(audio, dict) or audio.get("format") not in ("pcm-zlib", "f32le-zlib") or not isinstance(audio.get("blocks"), list):
        raise ValueError("The session uses an unsupported input audio format.")
    blocks, size = [], 0
    for position, values in enumerate(audio["blocks"]):
        if not isinstance(values, list) or len(values) not in (4, 5) or any(type(value) is not int for value in values[:4]):
            raise ValueError("The session has invalid input audio metadata.")
        rate, channels, frames, segment = values[:4]
        encoding = values[4] if len(values) == 5 else "f32le"
        if not (8_000 <= rate <= 384_000 and 1 <= channels <= MAX_AUDIO_CHANNELS
                and 1 <= frames <= MAX_BLOCK_FRAMES and 0 <= segment
                and isinstance(encoding, str) and encoding in ENCODINGS):
            raise ValueError("The captured audio has invalid format information.")
        suffix = "pcmz" if audio.get("format") == "pcm-zlib" else "f32z"
        member = archive.getinfo(f"input/{index}/{position}.{suffix}")
        size += member.file_size
        if (member.compress_type != ZIP_STORED or member.file_size > frames * channels * ENCODINGS[encoding][1] + 65_536
                or size > MAX_AUDIO_BYTES):
            raise ValueError("The captured audio exceeds the storage limit for a single session.")
        blocks.append(AudioBlock(rate, channels, frames, segment, archive.read(member), encoding))
    run["inputAudio"] = CapturedInput(tuple(blocks), str(audio.get("incompleteReason", ""))[:500])
    return size


def _load_archive(stream):
    if stream.seek(0, 2) > MAX_ARCHIVE_BYTES:
        raise ValueError("This session is too large to open.")
    try:
        with ZipFile(stream) as archive:
            manifest = archive.getinfo("session.json")
            if manifest.file_size > MAX_MANIFEST_BYTES or len(set(archive.namelist())) != len(archive.namelist()):
                raise ValueError("The session archive is too large or contains duplicate files.")
            payload = _read_document(archive.read(manifest))
            if not isinstance(payload, dict) or payload.get("version") != "2.0":
                raise ValueError("This Fluttery session archive version is unsupported.")
            runs = [payload, *_samples(payload.get("previousRuns"))]
            audio_total = 0
            for index, run in enumerate(runs):
                if isinstance(run, dict):
                    audio_total += _restore_audio(archive, run, index)
                    if audio_total > MAX_ARCHIVE_BYTES - MAX_MANIFEST_BYTES:
                        raise ValueError("The session archive contains too much input audio.")
    except (BadZipFile, EOFError, KeyError, NotImplementedError, RuntimeError, zlib.error) as error:
        raise ValueError("This Fluttery session archive is incomplete or damaged.") from error
    return normalise_session(payload)
