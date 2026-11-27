"""Session statistics, graphs and report formats shared by Results and exports."""

from __future__ import annotations

import base64
import csv
import html
import io
import json
import re
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

import numpy as np

from .plot_export import PlotImageOptions, PlotSnapshot, PlotTrace, render_plot_image
from .plot_style import DEFAULT_PLOT_STYLE, TRACE_COLORS, PlotStyle, axis_ticks
from .preferences import normalize_export
from .sessions import (
    MEASUREMENT_TYPES,
    METADATA_FIELDS,
    atomic_write,
    normalise_session,
    number,
    session_document_bytes,
)

GRAPH_FILENAMES = {
    "graph": "wow_flutter_graph",
    "drift": "speed_drift_graph",
    "fft": "spectrum_fft",
    "spectrogram": "flutter_spectrogram",
}
SAMPLE_HEADERS = ("Time", "Detector Peak %", "Raw Block Peak %", "RMS %", "Frequency Hz")
DISTORTION_HEADERS = ("Recorded at", "Source", "THD+N %", "THD %", "Right THD+N %", "Fundamental Hz", "HD3 %")
MEASUREMENT_LABELS = {
    "rpm": "Speed (RPM)", "crosstalk": "Crosstalk", "balance": "Channel balance", "thd": "THD+N",
    "noise": "Noise floor", "resonance": "Resonance", "tracking": "Tracking distortion", "alignment": "Alignment readings",
    "frequency-response": "Frequency response", "bench-imd": "Two-tone intermodulation",
    "bench-distortion": "Distortion sweep",
}
MEASUREMENT_COLUMNS = {
    "rpm": (("rpm", "Speed RPM"), ("deviation", "Deviation %"), ("testRecordRpm", "Test record RPM"),
            ("playbackRpm", "Playback reference RPM"), ("referenceHz", "Reference tone Hz"),
            ("frequencyHz", "Detected tone Hz")),
    "crosstalk": (("left", "Left dB"), ("right", "Right dB")),
    "balance": (("balance", "Balance dB"),),
    "thd": (("thd", "THD+N %"), ("thdOnly", "THD %"), ("fundamental", "Fundamental Hz"), ("hd3", "HD3 %")),
    "tracking": (("thd", "Left THD+N %"), ("thdOnly", "Left THD %"), ("fundamental", "Left fundamental Hz"),
                 ("rightThdn", "Right THD+N %"), ("rightThdOnly", "Right THD %"),
                 ("rightFundamental", "Right fundamental Hz")),
}
RESPONSE_COLUMNS = {
    "leftDb": "Left gain (dB)", "rightDb": "Right gain (dB)",
    "leftPhase": "Left phase (degrees)", "rightPhase": "Right phase (degrees)",
    "leftGroupDelayMs": "Left group delay (ms)", "rightGroupDelayMs": "Right group delay (ms)",
    "leftCoherence": "Left coherence", "rightCoherence": "Right coherence",
}
COMPARABLE_RESPONSE_KEYS = tuple(key for key in RESPONSE_COLUMNS if not key.endswith("Coherence"))


@dataclass
class GraphData:
    title: str
    x_label: str
    y_label: str
    traces: list[dict] = field(default_factory=list)
    heatmap: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None
    log_x: bool = False
    empty_message: str = ""

    def description(self):
        if self.heatmap is not None:
            x, y, levels = self.heatmap
            finite = np.isfinite(levels)
            if not finite.any():
                return "No usable spectral measurements."
            row, col = np.unravel_index(np.argmax(np.where(finite, levels, -np.inf)), levels.shape)
            latest = " No level measurement in the latest column." if not finite[:, -1].any() else ""
            return (f"{len(x)} time points and {len(y)} frequency bins. "
                    f"Peak {levels[row, col]:.1f} dB at {y[row]:.1f} Hz, {x[col]:.2f} s.{latest}")
        entries = []
        for trace in self.traces:
            x, y = trace["x"], trace["y"]
            valid = np.isfinite(x) & np.isfinite(y)
            if valid.any():
                peak = int(np.argmax(y[valid]))
                entries.append(f"{trace['label']} has {np.count_nonzero(valid)} points. "
                               f"Range {np.min(y[valid]):.4g} to {np.max(y[valid]):.4g}. "
                               f"Maximum at {self.x_label.lower()} {x[valid][peak]:.4g}.")
        return " ".join(entries) or self.empty_message or "No data available."


@dataclass
class ReportData:
    session: dict
    options: dict
    graph_source: str
    comparison: dict | None
    comparison_name: str
    graphs: dict[str, GraphData]
    live_spectrum: dict | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    plot_style: PlotStyle = DEFAULT_PLOT_STYLE
    run_label: str = ""

    @property
    def date_label(self):
        return f"Generated {time_text(self.created_at.timestamp())}"

    @property
    def metadata(self):
        return self.session["metadata"]

    @property
    def history(self):
        return self.session["history"]

    @property
    def measurements(self):
        flags = self.options["turntableData"]
        return [entry for entry in self.session["measurementLog"] if flags.get(entry["type"])]


def time_text(timestamp, *, iso=False, milliseconds=False):
    try:
        date = datetime.fromtimestamp(number(timestamp), UTC)
    except (OverflowError, OSError, ValueError):
        return "Unknown"
    timespec = "milliseconds" if iso or milliseconds else "seconds"
    if iso:
        return date.isoformat(timespec=timespec).replace("+00:00", "Z")
    return date.astimezone().isoformat(sep=" ", timespec=timespec)[:23 if milliseconds else 19]


def _trace(label, x, y, color=None, style="line"):
    item = {"label": label, "x": np.asarray(x, dtype=float), "y": np.asarray(y, dtype=float), "style": style}
    if color:
        item["color"] = color
    return item


def _elapsed(history):
    origin = number(history[0].get("timestamp")) if history else 0
    return np.array([number(sample.get("timestamp"), origin) - origin for sample in history])


def graph_source_label(graph_source):
    return "Raw block peak" if graph_source == "rawBlockPeak" else "Detector peak"


def _peak(entry, graph_source):
    if not _sample_usable(entry):
        return None
    if graph_source == 'rawBlockPeak':
        value = number(entry.get('bufferPeak'), number(entry.get('peak'), None))
    elif entry.get('flutterBand') == 'scrape':
        value = number(entry.get('scrapePeak'), None)
    elif entry.get('weighting') == 'aes6':
        value = number(entry.get('aes6Peak'), None)
    else:
        value = number(entry.get('peak'), None)
    return min(100, max(0, value)) if value is not None else None


def _sample_usable(entry):
    # Older sessions lack the lock flag. Keep those readings and exclude only
    # reported errors, which can contain zero readings or a stray tone.
    return (entry.get("locked") is not False and entry.get("valid") is not False
            and not (entry.get("invalidFrames") or entry.get("clipped") or entry.get("referenceAmbiguous")))


def _usable_samples(history):
    return [entry for entry in history if _sample_usable(entry)]


def session_statistics(history, graph_source):
    if not history:
        return []
    usable = _usable_samples(history)
    data = [("Samples", str(len(history)))]
    if len(usable) != len(history):
        invalid_locked = any(not _sample_usable(row) and row.get("locked") is not False for row in history)
        label = "Unusable samples excluded" if invalid_locked else "Unlocked samples excluded"
        data.append((label, str(len(history) - len(usable))))
    if not usable:
        return data
    peaks = [value for entry in usable if (value := _peak(entry, graph_source)) is not None]
    rms = [value for entry in usable if (value := number(entry.get("rms"), None)) is not None]
    frequencies = [value for entry in usable if (value := number(entry.get("currentFreq"))) > 0]
    if peaks:
        source = graph_source_label(graph_source).lower()
        data.extend(((f"Minimum {source}", f"{min(peaks):.4f} %"),
                     (f"Maximum {source}", f"{max(peaks):.4f} %"),
                     (f"Average {source}", f"{np.mean(peaks):.4f} %")))
    if rms:
        data.append(("Average RMS", f"{np.mean(rms):.4f} %"))
    for key, label, units, method in (
        ('aes6Peak', 'Maximum AES6 weighted peak', '%', max),
        ('rmsOneSecond', 'Maximum one-second RMS', '%', max),
        ('rollingPeakMax', 'Last rolling 10-second peak maximum', '%', None),
        ('rollingRmsMax', 'Last rolling 10-second RMS maximum', '%', None),
        ('driftRms', 'Maximum slow drift RMS', '%', max),
        ('scrapePeak', 'Maximum scrape flutter peak', '%', max),
        ('scrapeRms', 'Maximum scrape flutter RMS', '%', max),
        ('flutterPeriodHz', 'Last dominant flutter repetition', 'Hz', None),
    ):
        measurements = [value for entry in usable if (value := number(entry.get(key), None)) is not None]
        if measurements:
            result = method(measurements) if method else measurements[-1]
            data.append((label, f'{result:.5g} {units}'))
    if frequencies:
        start, end = frequencies[0], frequencies[-1]
        average = np.mean(frequencies)
        data.extend((
            ("Average frequency", f"{average:.2f} Hz"),
            ("Frequency range", f"{min(frequencies):.2f} to {max(frequencies):.2f} Hz"),
            ("Start frequency", f"{start:.2f} Hz"),
            ("End frequency", f"{end:.2f} Hz"),
            ("Drift", f"{end - start:+.2f} Hz ({(end - start) / start * 100:+.4f} %)"),
        ))
        if len(frequencies) > 1:
            variation = np.std(frequencies)
            data.append(("Frequency standard deviation", f"{variation:.4f} Hz ({1e6 * variation / average:.1f} ppm)"))
    return data


def comparison_statistics(history, comparison, graph_source):
    """Display both values and an explicit current-minus-comparison difference."""
    def metrics(log):
        usable = _usable_samples(log)
        speeds = [value for row in usable if (value := number(row.get("currentFreq"))) > 0]
        return (max((value for row in usable if (value := _peak(row, graph_source)) is not None), default=None),
                float(np.mean(speeds)) if speeds else None, len(log))

    def value_text(value, precision, unit, *, signed=False):
        if value is None:
            return "Unavailable"
        return format(value, f"{'+' if signed else ''}.{precision}f") + unit

    specifications = (
        (f"Maximum {graph_source_label(graph_source).lower()}", 4, " %", " percentage points"),
        ("Average frequency", 2, " Hz", " Hz"),
        ("Samples", 0, "", ""),
    )
    return [
        (label, value_text(current, precision, unit), value_text(other, precision, unit),
         value_text(current - other if current is not None and other is not None else None,
                    precision, delta_unit, signed=True))
        for (label, precision, unit, delta_unit), current, other
        in zip(specifications, metrics(history), metrics(comparison), strict=True)
    ]


def history_graph(kind, history, graph_source, comparison=(), comparison_name="Comparison", *, run_label=""):
    if kind == "drift":
        graph = GraphData("Speed drift", "Elapsed time (s)", "Detected frequency (Hz)")
    else:
        graph = GraphData(f"Wow and flutter ({graph_source_label(graph_source)})", "Elapsed time (s)", "Deviation (%)")
    for index, (label, log) in enumerate(((run_label or "Current", history), (comparison_name, comparison))):
        if not log:
            continue
        elapsed = _elapsed(log)
        if kind == "drift":
            values = [frequency if _sample_usable(row) and (frequency := number(row.get("currentFreq"))) > 0
                      else np.nan for row in log]
        else:
            values = [value if (value := _peak(row, graph_source)) is not None else np.nan for row in log]
        if np.isfinite(values).any():
            graph.traces.append(_trace(label, elapsed, values, TRACE_COLORS[index], "dashed" if index else "line"))
    return graph


def _spectrum(sample):
    """Return bin centres and levels, decoding Electron's unsigned Web Audio bins."""
    bins = np.asarray(sample.get("spectrum", ()), dtype=float)
    if sample.get("spectrumEncoding") != "db":
        bins = -100 + np.clip(bins, 0, 255) * (70 / 255)
    width = max(0.00001, number(sample.get("spectrumBinHz"), 48000 / 4096))
    start = number(sample.get("spectrumStartHz"))
    return start + np.arange(bins.size) * width, np.nan_to_num(bins, nan=-120, neginf=-120, posinf=0)


def spectrum_graph(history, live_spectrum=None, *, spectra=None):
    live_spectrum = live_spectrum or {}
    rows = history if spectra is None else spectra
    sample = live_spectrum if len(live_spectrum.get("spectrum", [])) else next(
        (item for item in reversed(rows) if _sample_usable(item) and len(item.get("spectrum", []))), {},
    )
    frequencies, values = _spectrum(sample)
    title = "Flutter spectrum (FFT)"
    is_legacy = values.size and sample.get("spectrumEncoding") != "db"
    if is_legacy:
        title += " — legacy scale" if "spectrumBinHz" in sample else " — legacy scale, 48 kHz assumed"
    level = "Level (dBFS)" if is_legacy else "Level (dB)"
    # The first FFT bin records the constant component of the demodulated
    # signal. A linear frequency axis keeps that bin visible at 0 Hz.
    graph = GraphData(title, "Frequency (Hz)", level)
    if values.size:
        graph.traces.append(_trace("Spectrum", frequencies, values))
    return graph


def spectrogram_graph(history, *, spectra=None, signal_loss=(), use_sample_times=False):
    graph = GraphData("Flutter spectrogram", "Elapsed time (s)", "Frequency (Hz)")
    independent = spectra is not None
    rows = spectra if independent else history
    spectra = [entry for entry in rows if _sample_usable(entry) and len(entry.get("spectrum", [])) >= 2]
    if not spectra:
        return graph

    # Both apps store demodulated flutter here. Their detectors and amplitude
    # references differ, so avoid displaying the two scales as one time series.
    encoding = spectra[-1].get("spectrumEncoding")
    spectra = [entry for entry in spectra if entry.get("spectrumEncoding") == encoding]
    if encoding != "db":
        graph.title = "Flutter spectrogram — legacy scale"
        if any("spectrumBinHz" not in entry for entry in spectra):
            graph.title += " — 48 kHz assumed"

    # Current files include the exact bin spacing. Resample older or mixed-rate
    # spectra to a common grid so a frequency keeps the same vertical position.
    origin = number((history or spectra)[0].get("timestamp"))
    spectra.sort(key=lambda item: number(item.get("timestamp")))
    time = np.array([number(entry.get("timestamp")) - origin for entry in spectra])
    frequencies, _ = _spectrum(spectra[-1])
    if time.size > 1:
        # Keep the latest captured column for duplicate timestamps in an import.
        distinct = np.r_[np.diff(time) > 0, True]
        spectra = [item for item, keep in zip(spectra, distinct, strict=True) if keep]
        time = time[distinct]
    # Bound the displayed raster without discarding any saved spectral rows.
    # Never paint a long break with a copied column from either side.
    last = number(history[-1].get("timestamp")) - origin if independent and history else time[-1]
    samples = max(1, min(1600, len(time) if not independent else max(len(time), round(last) + 1)))
    if use_sample_times and history:
        # The rolling live window already bounds the number of rows. Preserve
        # their times so sampled gaps survive even between regular grid points.
        grid = np.unique(_elapsed(history))
    else:
        grid = np.linspace(0 if independent else time[0], max(last, time[-1]), samples) if samples > 1 else time
    right = np.minimum(time.size - 1, np.searchsorted(time, grid))
    left = np.maximum(0, right - 1)
    nearest = np.where(abs(grid - time[left]) <= abs(time[right] - grid), left, right)
    values = np.stack([np.interp(frequencies, *_spectrum(spectra[index]), left=np.nan, right=np.nan)
                       for index in nearest])
    if independent:
        spacing = float(np.median(np.diff(time))) if time.size > 1 else 1.0
        values[np.abs(grid - time[nearest]) > min(2.0, max(0.1, 1.75 * spacing))] = np.nan
    if history and any(not _sample_usable(item) for item in history):
        measured_times = _elapsed(history)
        # Old imported timestamps can be unordered. The plotted time lookup
        # must refer to the same rows as the sorted timestamps.
        order = np.argsort(measured_times, kind="stable")
        positions = np.searchsorted(measured_times[order], grid, side="right") - 1
        invalid = np.asarray([not _sample_usable(history[item]) for item in order])
        values[(positions >= 0) & invalid[np.maximum(0, positions)]] = np.nan
    for start, end, _ in _loss_ranges(signal_loss):
        values[(grid >= start - origin) & (grid <= end - origin)] = np.nan
    time = grid
    graph.heatmap = time, frequencies, values.T
    return graph


def history_table(history, *, iso=False):
    def formatted(entry, value, precision):
        return f"{value:.{precision}f}" if _sample_usable(entry) and value is not None else ""

    return [(
        time_text(entry.get("timestamp"), iso=iso),
        formatted(entry, _peak(entry, "detectorPeak"), 4),
        formatted(entry, _peak(entry, "rawBlockPeak"), 4),
        formatted(entry, number(entry.get('rms'), None), 4),
        formatted(entry, frequency if (frequency := number(entry.get('currentFreq'))) > 0 else None, 2),
    ) for entry in history]


def _loss_ranges(events):
    if not isinstance(events, (list, tuple)):
        return
    for event in events:
        if isinstance(event, Mapping) and (start := number(event.get("start"), None)) is not None:
            end = number(event.get("end"), start)
            yield start, max(start, end), str(event.get("reason", "Signal unavailable"))


def signal_loss_table(events):
    return [(time_text(start, milliseconds=True), time_text(end, milliseconds=True), reason)
            for start, end, reason in _loss_ranges(events)]


def _measurement_points(entry):
    data = entry.get("data")
    points = data if isinstance(data, list) else [data]
    return [point for point in points if isinstance(point, Mapping)]


def distortion_history(entries):
    """Combine standalone analyzer and tracking readings in recorded time order."""
    points = []
    for entry in entries:
        kind = entry.get("type")
        if kind not in ("thd", "tracking"):
            continue
        for index, point in enumerate(_measurement_points(entry)):
            thdn = number(point.get("thd"), None)
            right_thdn = number(point.get("rightThdn"), None) if kind == "tracking" else None
            usable = _sample_usable(point)
            if thdn is None and right_thdn is None and usable:
                continue
            quantities = (thdn, number(point.get("thdOnly"), None), right_thdn,
                          number(point.get("fundamental"), None), number(point.get("hd3"), None))
            points.append((
                number(point.get("timestamp"), number(entry.get("timestamp")) + index / 60),
                "Distortion analyzer" if kind == "thd" else "Tracking distortion",
                *(quantities if usable else (None,) * len(quantities)),
            ))
    points.sort(key=lambda point: point[0])
    return points


def distortion_results(points):
    graph = GraphData("Session distortion history", "Elapsed time (s)", "Distortion (%)")
    if not points:
        return "There are no THD+N readings. Record a measurement in Distortion Analyzer or Tracking distortion.", graph, []
    origin = points[0][0]
    for index, label, colour in ((2, "THD+N", TRACE_COLORS[0]), (3, "THD", TRACE_COLORS[1]),
                                 (4, "Right tracking THD+N", TRACE_COLORS[2]), (6, "HD3", TRACE_COLORS[3])):
        # Missing optional metrics are normal across the two analyzers.
        # Keep complete invalid readings as breaks in every displayed trace.
        series = [(point[0] - origin, point[index]) for point in points
                  if point[index] is not None or (point[2] is None and point[4] is None)]
        if any(value is not None for _, value in series):
            x, y = zip(*series, strict=True)
            graph.traces.append(_trace(label, x, y, colour))
    summaries = []
    for index, source in ((2, "the distortion analyzer and tracking tool"), (4, "the right tracking channel")):
        values = np.array([point[index] for point in points if point[index] is not None])
        if values.size:
            noun = "reading" if values.size == 1 else "readings"
            summaries.append(f"{len(values)} THD+N {noun} from {source}.\n"
                             f"Average {values.mean():.3f} %. Minimum {values.min():.3f} %. Peak {values.max():.3f} %.")
    summary = "\n".join(summaries) or "No usable THD+N readings."
    rows = [(time_text(timestamp, milliseconds=True), source,
             *(f"{value:.3f}" if value is not None else "—" for value in values))
            for timestamp, source, *values in points]
    return summary, graph, rows


def _measurement_spectrum(entry):
    data = entry.get("data")
    if not isinstance(data, list) or not data or isinstance(data[0], Mapping):
        return np.array([]), np.array([])
    stats = entry.get("stats") or {}
    return _spectrum({
        "spectrum": [number(item) for item in data],
        "spectrumEncoding": stats.get("spectrumEncoding", "db" if any(number(n) < 0 for n in data) else "byte"),
        "spectrumBinHz": stats.get("binHz", 48000 / 2048),
        "spectrumStartHz": stats.get("startHz", 0),
    })


def _point_times(points):
    first_time = number(points[0].get("timestamp"))
    return np.array([number(point.get("timestamp"), first_time + index / 60) - first_time
                     for index, point in enumerate(points)])


def _alignment_trials(entry):
    data = entry.get("data")
    if not isinstance(data, Mapping) or not isinstance(data.get("trials"), list):
        return MEASUREMENT_LABELS["alignment"], []
    title = data.get("title")
    return (title if isinstance(title, str) else MEASUREMENT_LABELS["alignment"],
            [trial for trial in data["trials"] if isinstance(trial, Mapping)])


def _response_columns(entry):
    data = entry.get("data")
    if not isinstance(data, Mapping):
        return {}, np.array([]), {}
    values = data.get("frequencyHz")
    if not isinstance(values, list) or not values:
        return data, np.array([]), {}
    frequencies = np.array([number(value, np.nan) for value in values])
    columns = {key: column for key in RESPONSE_COLUMNS
               if (column := _response_values(data, key, len(values))) is not None}
    return data, frequencies, columns


def _response_values(data, key, count):
    values = data.get(key)
    if isinstance(values, list) and len(values) == count:
        return np.array([number(value, np.nan) for value in values])
    return None


def _response_comparison_columns(data, frequencies, columns):
    # Raw values alone are saved without a profile. A comparison needs the
    # saved correction and at least one usable pair on the same channel.
    if not data.get("profile"):
        return {}
    legacy = "correctionApplied" not in data
    valid_frequency = np.isfinite(frequencies) & (frequencies > 0)
    pairs = {}
    for key in COMPARABLE_RESPONSE_KEYS:
        suffix = key[0].upper() + key[1:]
        raw = _response_values(data, f"raw{suffix}", len(frequencies))
        if raw is None:
            continue
        corrected = _response_values(data, f"corrected{suffix}", len(frequencies))
        # Before explicit corrected arrays, the primary view always used the
        # recorded profile. A newer primary view can instead be bypassed.
        if corrected is None and legacy:
            corrected = columns.get(key)
        if corrected is not None and (valid_frequency & np.isfinite(corrected) & np.isfinite(raw)).any():
            pairs[key] = corrected, raw
    return pairs


def has_raw_response_comparison(entry):
    """Whether a captured response has saved raw and corrected values to compare."""
    return entry["type"] == "frequency-response" and bool(_response_comparison_columns(*_response_columns(entry)))


def _separation_points(entry):
    return [row for row in _measurement_points(entry) if row.get("direction") in ("Left to right", "Right to left")]


def _separation_value(row):
    value = number(row.get("separationDb"), None)
    if value is None:
        driven = number(row.get("drivenDbfs"), None)
        resolution = number(row.get("resolutionDbfs"), None)
        if driven is not None and resolution is not None:
            return max(0.0, driven - resolution)
    return value


def _bench_rows(entry):
    payload = entry.get("data")
    if not isinstance(payload, Mapping) or not isinstance(payload.get("data"), list):
        return {}, []
    return payload, [row for row in payload["data"] if isinstance(row, Mapping)]


def _bench_channel(index):
    return {1: "Left", 2: "Right"}.get(index, f"Channel {index}")


def _bench_reading(value):
    return f"{value:.5g}" if (value := number(value, None)) is not None else "Unavailable"


def _imd_reading(row):
    if row.get("clipping") or row.get("error"):
        return "Unavailable"
    if row.get("imdLimited") and (limit := number(row.get("imdLimitPercent"), None)) is not None:
        return f"≤{limit:.5g}"
    return _bench_reading(row.get("imdPercent"))


def _usable_distortion(row):
    return not row.get("clipping") and any(number(row.get(key), None) is not None for key in ("thd", "thdn"))


def measurement_summary(entry):
    kind, stats = entry["type"], entry.get("stats") or {}
    if kind not in MEASUREMENT_TYPES:
        return "Recorded measurement"
    if kind == "alignment":
        title, trials = _alignment_trials(entry)
        count = len(trials)
        summary = f"{title}. {count} captured {'reading' if count == 1 else 'readings'}."
        verified = next((delay for trial in reversed(trials)
                         if (delay := number(trial.get("verified_delay_us"), None)) is not None), None)
        if verified is not None:
            summary += f" Latest verified right-channel delay {verified:+.3f} µs."
        return summary
    if kind == "bench-imd":
        payload, rows = _bench_rows(entry)
        parts = []
        for channel, row in enumerate(rows, 1):
            value = _imd_reading(row)
            if row.get("clipping"):
                reading = "input clipped"
            elif row.get("error"):
                reading = f"unavailable ({row['error']})"
            elif value.startswith("≤"):
                reading = f"at most {value[1:]}% (limited by noise)"
            elif value == "Unavailable":
                reading = "unavailable"
            else:
                reading = f"{value}%" + (" (some products out of band)" if row.get("incomplete") else "")
            parts.append(f"{_bench_channel(channel)} {reading}.")
        return " ".join((f"{str(payload.get('method', 'Two-tone')).upper()} IMD.", *parts))
    if kind == "bench-distortion":
        payload, rows = _bench_rows(entry)
        axis = "Frequency" if payload.get("axis") == "frequencyHz" else "Level"
        usable = [row for row in rows if _usable_distortion(row)]
        return f"{axis} sweep. {len(usable)} of {len(rows)} channel readings are usable."
    if kind == "frequency-response":
        data, frequencies, columns = _response_columns(entry)
        if not columns:
            return "No response data."
        mode = str(data.get("mode", "device")).replace("_", " ").capitalize()
        summary = f"{mode}. {len(frequencies)} bands. {number(data.get('sampleRate')):g} Hz capture."
        if data.get("profile") and data.get("correctionApplied", True):
            summary += f" Gain was corrected with {data['profile']}."
        elif data.get("profile"):
            summary += " Fluttery saved the raw response."
            if _response_comparison_columns(data, frequencies, columns):
                summary += f" Compare it with the response corrected using {data['profile']}."
        if data.get("clipping"):
            summary += " Input clipped."
        return summary
    if kind == "rpm" and "avg" in stats:
        summary = (f"Average {number(stats['avg']):.3f} RPM. "
                   f"Range {number(stats.get('min')):.3f} to {number(stats.get('max')):.3f} RPM.")
        if (points := _measurement_points(entry)) and (target := number(points[0].get("playbackRpm"), None)) is not None:
            summary += f" Playback reference {target:.8g} RPM."
        return summary
    if kind == "crosstalk" and (directions := _separation_points(entry)):
        details = []
        for row in directions:
            value = _separation_value(row)
            if value is None:
                text = "unavailable"
            elif row.get("limited"):
                text = f"at least {value:.1f} dB (limited by noise)"
            else:
                text = f"{value:.1f} dB"
            details.append(f"{row['direction']}: {text}.")
        return " ".join(details)
    if kind == "crosstalk" and {"avgL", "avgR"} <= stats.keys():
        return f"Average left {number(stats['avgL']):.1f} dB, right {number(stats['avgR']):.1f} dB."
    if kind == "balance":
        value = number(stats.get("avg"), None)
        label = "Average balance" if value is not None else "Balance"
        if value is None and len(points := _measurement_points(entry)) == 1 and _sample_usable(points[0]):
            value = number(points[0].get("balance"), None)
        if value == 0:
            return f"{label} centred (0.00 dB)."
        if value is not None:
            # The instrument and saved samples use left minus right in dB.
            return f"{label} {abs(value):.2f} dB toward the {'left' if value > 0 else 'right'} channel."
    if kind == "tracking":
        summaries = []
        for label, avg_key, max_key in (("Left", "avg", "max"), ("Right", "rightAvg", "rightMax")):
            if (average := number(stats.get(avg_key), None)) is not None:
                peak = number(stats.get(max_key), None)
                summaries.append(f"{label} average THD+N {average:.3f} %."
                                 + (f" Peak {peak:.3f} %." if peak is not None else ""))
        if summaries:
            return " ".join(summaries)
    if kind == "thd" and (average := number(stats.get("avg"), None)) is not None:
        peak = number(stats.get("max"), None)
        return f"Average THD+N {average:.3f} %." + (f" Peak {peak:.3f} %." if peak is not None else "")
    frequencies, values = _measurement_spectrum(entry)
    if values.size:
        peak = int(np.argmax(values))
        summary = f"Peak {values[peak]:.1f} dB at {frequencies[peak]:.1f} Hz."
        for name, label in (("rmsDb", "RMS"), ("rumbleDb", "Rumble")):
            if name in stats:
                summary += f" {label} {number(stats[name]):.1f} dB."
        return summary
    points = _measurement_points(entry)
    if len(points) == 1:
        readings = [f"{label} {value:.3f}" for key, label in MEASUREMENT_COLUMNS.get(kind, ())
                    if (value := number(points[0].get(key), None)) is not None and _sample_usable(points[0])]
        return ". ".join(readings) or "No usable reading."
    return f"{len(points)} recorded points." if points else "Recorded measurement"


def measurement_graph(entry, *, compare_raw=False):
    kind = entry["type"]
    title = MEASUREMENT_LABELS.get(kind, kind)
    if kind == "bench-distortion":
        payload, rows = _bench_rows(entry)
        by_frequency = payload.get("axis") == "frequencyHz"
        axis = "frequencyHz" if by_frequency else "levelDbfs"
        traces = []
        channels = {int(number(row.get("channel"), 1)) for row in rows}
        for channel in sorted(channels):
            points = [row for row in rows if int(number(row.get("channel"), 1)) == channel
                      and number(row.get(axis), None) is not None]
            x = [number(point.get(axis)) for point in points]
            for key, label in (("thd", "THD"), ("thdn", "THD+N"), ("hd3", "HD3")):
                y = [np.nan if row.get("clipping") else number(row.get(key), np.nan) for row in points]
                if np.isfinite(y).any():
                    traces.append(_trace(f"{_bench_channel(channel)} {label}", x, y,
                                         TRACE_COLORS[len(traces) % len(TRACE_COLORS)],
                                         "dashed" if key == "thdn" else "line"))
        return GraphData(title, "Frequency (Hz)" if by_frequency else "Generator level (dBFS)", "Distortion (%)",
                         traces, log_x=by_frequency)
    if kind == "bench-imd":
        _, rows = _bench_rows(entry)
        traces = []
        for channel, row in enumerate(rows, 1):
            if not row.get("clipping") and not row.get("error"):
                products = row.get("products") or ()
                for limited, suffix in ((False, ""), (True, " (near noise floor)")):
                    points = [(x, y) for product in products if isinstance(product, Mapping)
                              and bool(product.get("limited")) == limited
                              and (x := number(product.get("frequencyHz"), None)) is not None and x > 0
                              and (y := number(product.get("levelDbc"), None)) is not None]
                    if points:
                        x, y = zip(*sorted(points), strict=True)
                        traces.append(_trace(_bench_channel(channel) + suffix, x, y,
                                             TRACE_COLORS[(channel - 1) % len(TRACE_COLORS)], "points"))
        return GraphData(title, "Frequency (Hz)", "Product level (dBc)", traces, log_x=True)
    if kind == "crosstalk" and (directions := _separation_points(entry)):
        traces = []
        for index, row in enumerate(directions):
            value = _separation_value(row)
            frequency = number(row.get("frequencyHz"), None)
            if value is not None and frequency is not None and frequency > 0:
                label = row["direction"] + (" (lower bound)" if row.get("limited") else "")
                traces.append(_trace(label, [frequency], [value], TRACE_COLORS[index % len(TRACE_COLORS)], "points"))
        return GraphData("Stereo separation", "Frequency (Hz)", "Separation (dB)", traces)
    if kind == "frequency-response":
        data, frequencies, columns = _response_columns(entry)
        unit = "Relative level (dB)" if data.get("relative") else "Gain (dB)"
        style = "points" if data.get("relative") else "line"
        pairs = _response_comparison_columns(data, frequencies, columns) if compare_raw else {}
        keys = ("leftDb", "rightDb")
        if pairs:
            # An incomplete imported capture may only have usable paired
            # phase or group delay. Plot the first available paired quantity.
            for candidate_keys, candidate_unit in ((keys, unit), (("leftPhase", "rightPhase"), "Phase (degrees)"),
                                                   (("leftGroupDelayMs", "rightGroupDelayMs"), "Group delay (ms)")):
                if any(key in pairs for key in candidate_keys):
                    keys, unit = candidate_keys, candidate_unit
                    break
            if keys[0] == "leftPhase":
                style = "points"
            corrected = {key: pair[0] for key, pair in pairs.items()}
            raw = {key: pair[1] for key, pair in pairs.items()}
            traces = _response_traces(frequencies, corrected, keys, style, 0, "Corrected ")
            traces.extend(_response_traces(frequencies, raw, keys, "dashed" if style == "line" else style, 2, "Raw "))
            # An older record can lack a raw trace for one channel. Keep its
            # primary trace available without calling it a member of the pair.
            unpaired = {key: columns[key] for key in keys if key not in pairs and key in columns}
            traces.extend(_response_traces(frequencies, unpaired, keys, style, 4, "Saved "))
        else:
            traces = _response_traces(frequencies, columns, keys, style)
        return GraphData(title, "Frequency (Hz)", unit, traces, log_x=True)
    if kind == "alignment":
        title, trials = _alignment_trials(entry)
        if not trials:
            return GraphData(title, "Reading number", "Measurement")
        last = trials[-1]
        curve = last.get("curve")
        if (isinstance(curve, (list, tuple)) and len(curve) == 3 and
                all(isinstance(part, (list, tuple)) for part in curve) and
                len(curve[0]) == len(curve[1]) == len(curve[2]) and len(curve[0])):
            x, left, right = ([number(value, np.nan) for value in part] for part in curve)
            return GraphData(f"{title} — {last.get('setting', '')}", "Frequency (Hz)",
                             str(last.get("curve_unit") or "Band level (dB)"),
                             [_trace("Left", x, left), _trace("Right", x, right)], log_x=True)
        trials = [trial for trial in trials if all(trial.get(key) == last.get(key)
                                                  for key in ("step", "comparison", "metric"))]
        units = str(last.get("metric") or "Measurement")
        labels = ("Corrected delay", "Raw delay") if str(last.get("metric")).startswith("Delay") else ("Left", "Right")
        x = np.arange(1, len(trials) + 1)
        return GraphData(f"{title} — {last.get('step', '')}", "Comparable reading number", units,
                         [_trace(label, x, [number(trial.get(key), np.nan) for trial in trials], TRACE_COLORS[index], "points")
                          for index, (label, key) in enumerate(zip(labels, ("left", "right"), strict=True))])
    frequencies, values = _measurement_spectrum(entry)
    if values.size:
        return GraphData(title, "Frequency (Hz)", "Level (dBFS)",
                         [_trace("Level", frequencies, values)])
    points = _measurement_points(entry)
    graph = GraphData(title, "Elapsed time (s)", "Value")
    if not points:
        return graph
    elapsed = _point_times(points)
    keys = {
        "rpm": (("rpm", "Speed (RPM)"),),
        "balance": (("balance", "Balance (dB)"),),
        "crosstalk": (("left", "Left (dB)"), ("right", "Right (dB)")),
        "thd": (("thd", "THD+N (%)"), ("thdOnly", "THD (%)")),
        "tracking": (("thd", "Left THD+N (%)"), ("thdOnly", "Left THD (%)"),
                     ("rightThdn", "Right THD+N (%)"), ("rightThdOnly", "Right THD (%)")),
    }.get(kind, ())
    for index, (key, label) in enumerate(keys):
        values = [number(point.get(key), np.nan) if _sample_usable(point) else np.nan for point in points]
        if np.isfinite(values).any():
            graph.traces.append(_trace(label, elapsed, values, TRACE_COLORS[index]))
    if keys:
        graph.y_label = {"crosstalk": "Level (dB)", "thd": "Distortion (%)", "tracking": "Distortion (%)"}.get(kind, keys[0][1])
    return graph


def _response_traces(frequencies, columns, keys, style, color_index=0, prefix=""):
    traces = []
    visible = np.isfinite(frequencies) & (frequencies > 0)
    for key in keys:
        if key in columns:
            values = columns[key]
            measured = visible & np.isfinite(values)
            if measured.any():
                # Connected traces preserve unmeasured bins as breaks.
                # Discrete test tones only contain the measured points.
                selected = measured if style == "points" else visible
                label = RESPONSE_COLUMNS[key]
                traces.append(_trace(prefix + (label[:1].lower() + label[1:] if prefix else label),
                                     frequencies[selected], values[selected], TRACE_COLORS[color_index + len(traces)],
                                     style))
    return traces


def measurement_table(entry, *, compare_raw=False):
    if entry["type"] == "bench-imd":
        payload, rows = _bench_rows(entry)
        samples = []
        for channel, row in enumerate(rows, 1):
            for product in row.get("products") or [{}]:
                if not isinstance(product, Mapping):
                    continue
                if row.get("clipping"):
                    status = "Clipped"
                elif row.get("error"):
                    status = f"Unavailable ({row['error']})"
                elif product.get("limited"):
                    status = "Near noise floor"
                else:
                    status = "Measured" if product else "Unavailable"
                samples.append((_bench_channel(channel), str(payload.get("method", "")).upper(),
                                _bench_reading(row.get("firstHz")), _bench_reading(row.get("secondHz")), _imd_reading(row),
                                str(product.get("product", "")),
                                *(_bench_reading(product.get(key)) for key in ("frequencyHz", "levelDbc", "floorDbc")), status))
        return ("Channel", "Method", "First tone (Hz)", "Second tone (Hz)", "IMD (%)", "Product",
                "Product (Hz)", "Level (dBc)", "Floor (dBc)", "Status"), samples
    if entry["type"] == "bench-distortion":
        _, rows = _bench_rows(entry)
        samples = [(_bench_channel(int(number(row.get("channel"), 1))),
                    *(_bench_reading(row.get(key)) for key in ("frequencyHz", "levelDbfs", "detectedHz", "signalDbfs",
                                                               "thd", "thdn", "hd3")),
                    "Clipped" if row.get("clipping") else "Unavailable" if not _usable_distortion(row) else
                    "THD+N only" if row.get("thd") is None else "Measured")
                   for row in rows]
        return ("Channel", "Generator frequency (Hz)", "Generator level (dBFS)", "Return frequency (Hz)",
                "Return level (dBFS)", "THD (%)", "THD+N (%)", "HD3 (%)", "Status"), samples
    if entry["type"] == "crosstalk" and (directions := _separation_points(entry)):
        columns = (("frequencyHz", "Frequency (Hz)"), ("drivenDbfs", "Driven (dBFS)"),
                   ("leakageDbfs", "Leakage (dBFS)"), ("noiseDbfs", "Noise floor (dBFS)"),
                   ("resolutionDbfs", "Resolution (dBFS)"), ("phaseDegrees", "Relative phase (°)"))
        rows = []
        for row in directions:
            readings = [f"{value:.5g}" if (value := number(row.get(key), None)) is not None else ""
                        for key, _ in columns]
            separation = _separation_value(row)
            result = "" if separation is None else f"{'≥' if row.get('limited') else ''}{separation:.3f}"
            status = "Unavailable" if separation is None else "Noise-limited" if row.get("limited") else "Measured"
            rows.append((row["direction"], *readings, result, status))
        return ("Direction", *(label for _, label in columns), "Separation (dB)", "Status"), rows
    if entry["type"] == "frequency-response":
        data, frequencies, saved = _response_columns(entry)
        pairs = _response_comparison_columns(data, frequencies, saved) if compare_raw else {}
        columns = []
        for key, label in RESPONSE_COLUMNS.items():
            if key in pairs:
                corrected, raw = pairs[key]
                label = label[:1].lower() + label[1:]
                columns.extend(((f"Corrected {label}", corrected), (f"Raw {label}", raw)))
            elif key in saved:
                columns.append((label, saved[key]))
        rows = [(f"{frequency:g}", *(f"{values[index]:.4g}" if np.isfinite(values[index]) else "—"
                                      for _, values in columns))
                for index, frequency in enumerate(frequencies) if np.isfinite(frequency) and frequency > 0]
        return ("Frequency (Hz)", *(label for label, _ in columns)), rows
    if entry["type"] == "alignment":
        _, trials = _alignment_trials(entry)
        columns = (("step", "Step"), ("setting", "Setting"), ("comparison", "Test"), ("metric", "Measurement"),
                   ("left", "Left or corrected"), ("right", "Right or raw"), ("score", "Score"))
        extra = (("delay_margin_us", "Delay repeatability (µs)"), ("verified_delay_us", "Verified delay (µs)"),
                 ("verification_fine_hz", "Fine tone (Hz)"))
        columns += tuple((key, label) for key, label in extra if any(trial.get(key) is not None for trial in trials))
        numeric = {"left", "right", "score", *(key for key, _ in extra)}
        rows = [tuple("—" if trial.get(key) is None else f"{number(trial[key]):.5g}"
                      if key in numeric else str(trial[key]) for key, _ in columns) for trial in trials]
        return tuple(label for _, label in columns), rows
    frequencies, values = _measurement_spectrum(entry)
    if values.size:
        return ("Frequency Hz", "Level dB"), [(f"{x:.2f}", f"{y:.2f}") for x, y in zip(frequencies, values)]
    points = _measurement_points(entry)
    keys = tuple((key, label) for key, label in MEASUREMENT_COLUMNS.get(entry["type"], ())
                 if any(key in point for point in points))
    headers = ("Elapsed time s", *(label for _, label in keys))
    rows = [(f"{seconds:.3f}", *(f"{value:.3f}" if (value := number(point.get(key), None)) is not None
                                 and _sample_usable(point) else "—" for key, _ in keys))
            for seconds, point in zip(_point_times(points), points, strict=True)] if points else []
    return headers, rows


def session_graphs(history, graph_source, *, comparison=(), comparison_name="Comparison", live_spectrum=None,
                   spectra=None, signal_loss=(), keys=GRAPH_FILENAMES, run_label=""):
    """Render graph data without requiring any native plot window to be open."""
    graphs = {}
    for key in keys:
        if key == "fft":
            graph = spectrum_graph(history, live_spectrum, spectra=spectra)
        elif key == "spectrogram":
            graph = spectrogram_graph(history, spectra=spectra, signal_loss=signal_loss)
        else:
            graph = history_graph(key, history, graph_source, comparison, comparison_name, run_label=run_label)
        if graph.traces or graph.heatmap is not None:
            graphs[key] = graph
    return graphs


def build_report(session, settings, *, comparison=None, comparison_name="Comparison", live_spectrum=None,
                 plot_style=None, run_label=""):
    current = normalise_session(session)
    other = normalise_session(comparison) if comparison is not None else None
    options = normalize_export(settings.get("exportSettings"))
    graph_source = settings.get("graphSource", "detectorPeak")
    keys = (key for key, enabled in options["sessionData"].items() if enabled and key in GRAPH_FILENAMES)
    graphs = session_graphs(current["history"], graph_source, comparison=other["history"] if other else (),
                            comparison_name=comparison_name, live_spectrum=live_spectrum,
                            spectra=current.get("spectra"), signal_loss=current.get("signalLoss") or (), keys=keys,
                            run_label=run_label)
    return ReportData(current, options, graph_source, other, comparison_name, graphs, live_spectrum=live_spectrum,
                      plot_style=plot_style or PlotStyle.from_settings(settings), run_label=run_label)


def report_filename(template, metadata, *, now=None, include_metadata=True):
    if not include_metadata:
        metadata = {}
    now = now or datetime.now()
    replacements = {
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H-%M-%S"),
        "technician": re.sub(r"[^a-zA-Z0-9]", "_", metadata.get("technician") or "unknown"),
        "equipment": re.sub(r"[^a-zA-Z0-9]", "_", metadata.get("equipment") or "unknown"),
    }
    name = template
    for key, value in replacements.items():
        name = name.replace("{" + key + "}", value)
    name = re.sub(r'[<>:"/\\|?*\s\x00-\x1f]+', "_", name)
    name = re.sub(r"_+", "_", name).strip("_.")[:180].rstrip(". ")
    # Windows device names are reserved even when a file extension is present.
    if re.fullmatch(r"(?i:con|prn|aux|nul|com[1-9]|lpt[1-9])", name.partition(".")[0]):
        name = "_" + name
    return name or "fluttery_report"


def graph_png(graph, *, plot_style=DEFAULT_PLOT_STYLE):
    """Render report graphs at a light, print-friendly default size."""
    traces = tuple(PlotTrace(trace["label"], trace["x"], trace["y"],
                             trace.get("color", TRACE_COLORS[index % len(TRACE_COLORS)]),
                             trace.get("style", "line")) for index, trace in enumerate(graph.traces))
    snapshot = PlotSnapshot(graph.title, graph.x_label, graph.y_label, traces, graph.heatmap, log_x=graph.log_x,
                            appearance=plot_style)
    return render_plot_image(snapshot, PlotImageOptions(width=1148, height=406, dpi=140))


def report_logo_png(options):
    logo = options.get("logoDataURL")
    if not isinstance(logo, str):
        return None
    match = re.fullmatch(r"data:image/(?:png|jpe?g|webp|gif);base64,([\w+/=\s]+)", logo, re.IGNORECASE)
    if not match:
        return None

    from PIL import Image

    try:
        image = Image.open(io.BytesIO(base64.b64decode(re.sub(r"\s", "", match[1]), validate=True)))
        with image, io.BytesIO() as output:
            image.thumbnail((1600, 600))
            image.convert("RGBA").save(output, format="PNG")
            return output.getvalue()
    except (Image.DecompressionBombError, OSError, ValueError):
        return None


def _data_png(data):
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def _csv_cell(value):
    # Preserve text as data when a spreadsheet opens the file.
    return "'" + value if isinstance(value, str) and value.lstrip(" \t\r\n")[:1] in "=+-@" and value.strip() else value


def _csv_measurement_value(entry):
    """Keep the Electron summary column useful to existing spreadsheet imports."""
    kind = entry["type"]
    stats = entry.get("stats") or {}
    data = entry.get("data")
    point = data if isinstance(data, Mapping) else {}
    if kind == "tracking" and ("rightAvg" in stats or any("rightThdn" in row for row in _measurement_points(entry))):
        return measurement_summary(entry)
    if kind == "crosstalk":
        left, right = (number(stats.get(key), number(point.get(fallback), None))
                       for key, fallback in (("avgL", "left"), ("avgR", "right")))
        if left is not None and right is not None:
            return f"L:{left:.1f} R:{right:.1f}"
    elif kind in ("rpm", "balance", "thd", "tracking"):
        key, unit = {"rpm": ("rpm", " RPM"), "balance": ("balance", " dB"),
                     "thd": ("thd", "%"), "tracking": ("thd", "%")}[kind]
        average = number(stats.get("avg"), None)
        value = average if average is not None else number(point.get(key), None)
        if value is not None:
            precision = 3 if key == "thd" and average is None else 2
            return f"{value:.{precision}f}{unit}"
    return "Spectrum Data" if isinstance(data, list) and data and isinstance(data[0], (int, float)) else measurement_summary(entry)


def _csv_measurement_data(data):
    # Legacy CSV is a summary for time-series captures. A single point or
    # spectrum fits in Extra Data, while JSON and the report tables keep each
    # sample of a longer series without a spreadsheet's single-cell limit.
    if isinstance(data, list) and data and isinstance(data[0], Mapping):
        return ""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def csv_report(report):
    with io.StringIO(newline="") as output:
        writer = csv.writer(output)
        if report.options["includeMetadata"]:
            writer.writerows((key.title(), _csv_cell(report.metadata.get(key, ""))) for key in METADATA_FIELDS)
            writer.writerow(("Date", report.created_at.astimezone().isoformat(timespec="seconds")))
            writer.writerow(())
        if report.run_label:
            writer.writerow(("Saved run", _csv_cell(report.run_label)))
            writer.writerow(())
        if report.history:
            writer.writerow(("Timestamp", "Detector Peak(%)", "Raw Block Peak(%)", "RMS(%)", "Freq(Hz)"))
            writer.writerows(history_table(report.history, iso=True))
        if losses := list(_loss_ranges(report.session.get("signalLoss") or ())):
            writer.writerow(())
            writer.writerow(("Signal losses",))
            writer.writerow(("Start", "End", "Reason"))
            writer.writerows((time_text(start, iso=True), time_text(end, iso=True), _csv_cell(reason))
                             for start, end, reason in losses)
        if report.measurements:
            writer.writerow(())
            writer.writerow(("Turntable Measurements",))
            # The historical Duration column contains the sample count.
            writer.writerow(("Timestamp", "Type", "Avg Value", "Duration", "Extra Data"))
            writer.writerows((
                time_text(entry.get("timestamp"), iso=True), entry["type"], _csv_cell(_csv_measurement_value(entry)),
                len(entry["data"]) if isinstance(entry["data"], list) else 1,
                _csv_measurement_data(entry["data"]),
            ) for entry in report.measurements)
            for entry in report.measurements:
                bidirectional = entry["type"] == "crosstalk" and _separation_points(entry)
                rpm_reference = entry["type"] == "rpm" and any("playbackRpm" in point for point in _measurement_points(entry))
                if bidirectional or rpm_reference or entry["type"] in ("bench-imd", "bench-distortion"):
                    headers, rows = measurement_table(entry)
                    writer.writerow(())
                    writer.writerow((MEASUREMENT_LABELS[entry["type"]], time_text(entry.get("timestamp"), iso=True)))
                    writer.writerow(headers)
                    writer.writerows((_csv_cell(cell) if not re.fullmatch(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", str(cell)) else cell
                                      for cell in row) for row in rows)
        return output.getvalue().encode("utf-8")


def json_report(report):
    include_metadata = report.options["includeMetadata"]
    metadata = report.metadata if include_metadata else {}
    saved = dict(report.session.get("runSettings") or report.session["settings"])
    if not include_metadata:
        for key in METADATA_FIELDS:
            saved.pop(key, None)
    payload = {
        "metadata": {**metadata, "exportDate": time_text(report.created_at.timestamp(), iso=True), "version": "1.0"},
        # Retain flat options for older readers and keep the measurement
        # setup available when reopening the export in Fluttery.
        "settings": {"graphSource": report.graph_source, **saved, **report.options, "exportSettings": report.options},
        "sessionLog": report.history,
        "measurementLog": report.session["measurementLog"],
    }
    for key in ("spectra", "signalLoss", "runRetention"):
        if report.session.get(key):
            payload[key] = report.session[key]
    if audio := report.session.get("recordedAudio"):
        payload["recordedAudio"] = {key: value for key, value in audio.items() if include_metadata or key != "file"}
    return session_document_bytes(payload, readable=True)


def _html_table(headers, rows):
    escape = html.escape
    heading = "".join(f'<th scope="col">{escape(str(text))}</th>' for text in headers)
    body = "".join("<tr>" + "".join(f"<td>{escape(str(value))}</td>" for value in row) + "</tr>" for row in rows)
    return f"<table><thead><tr>{heading}</tr></thead><tbody>{body}</tbody></table>"


def report_sections(report):
    """Yield reusable, ordered headings, readable notes, plots and tables."""
    if report.run_label:
        yield "text", "Saved run", report.run_label
    if report.options["includeMetadata"]:
        details = [(key.title(), value) for key in METADATA_FIELDS if (value := report.metadata.get(key))]
        if details:
            yield "table", "Session details", (("Field", "Value"), details)
    if report.options["sessionData"]["stats"]:
        if report.history:
            yield "table", "Session statistics", (("Metric", "Value"), session_statistics(report.history, report.graph_source))
        if report.comparison is not None:
            yield "table", f"Comparison session — {report.comparison_name}", (
                ("Metric", "Value"), session_statistics(report.comparison["history"], report.graph_source),
            )
            yield "table", "Session comparison", (
                ("Metric", "Current", "Comparison", "Difference"),
                comparison_statistics(report.history, report.comparison["history"], report.graph_source),
            )
    for key, graph in report.graphs.items():
        yield "graph", key, graph
    if report.options["includeDataTables"] and report.history:
        yield "table", "Session samples", (SAMPLE_HEADERS, history_table(report.history))
    if losses := signal_loss_table(report.session.get("signalLoss") or ()):
        yield "table", "Signal losses", (("Start", "End", "Reason"), losses)
    for index, entry in enumerate(report.measurements):
        label = MEASUREMENT_LABELS.get(entry["type"], entry["type"])
        yield "text", label, time_text(entry.get("timestamp")) + "\n" + measurement_summary(entry)
        graph = measurement_graph(entry)
        if graph.traces:
            yield "graph", f"{entry['type']}_{index + 1}", graph
        if report.options["includeDataTables"]:
            headers, rows = measurement_table(entry)
            if rows:
                table_title = "Captured alignment readings" if entry["type"] == "alignment" else f"{label} samples"
                yield "table", table_title, (headers, rows)


# An exported HTML file can be opened offline. The desktop app uses only wx
# controls. This tiny script enhances the report's already-readable figures.
_HTML_SCRIPT = """
for (const chart of document.querySelectorAll('figure[data-chart]')) {
  const image = chart.querySelector('img');
  let scale = 1;
  const output = chart.querySelector('output');
  for (const button of chart.querySelectorAll('button[data-factor]')) {
    button.addEventListener('click', () => {
      const factor = Number(button.dataset.factor);
      scale = factor === 0 ? 1 : Math.min(8, Math.max(1, scale * factor));
      image.style.width = (scale * 100) + '%';
      output.textContent = 'Zoom ' + Math.round(scale * 100) + ' percent';
    });
  }
  const range = chart.querySelector('input');
  const traces = chart.querySelector('select');
  if (!range) continue;
  const points = JSON.parse(chart.querySelector('script').textContent);
  const value = chart.querySelector('.point-value');
  const frequency = chart.querySelector('[data-frequency]');
  if (frequency) {
    function showCell() {
      const x = Number(range.value);
      const y = Number(frequency.value);
      const level = points.values[y][x];
      value.textContent = 'Time ' + points.x[x].toPrecision(5) + ' s. Frequency ' +
        points.y[y].toPrecision(5) + ' Hz. ' + (level === null ? 'No reading.' : 'Level ' + level.toFixed(1) + ' dB.');
    }
    frequency.addEventListener('input', showCell);
    range.addEventListener('input', showCell);
    showCell();
    continue;
  }
  function showPoint() {
    const trace = points[Number(traces.value)];
    range.max = trace.x.length - 1;
    const index = Math.min(Number(range.value), Number(range.max));
    value.textContent = 'Point ' + (index + 1) + '. ' +
      range.dataset.x + ' ' + (trace.x[index] === null ? 'unavailable' : trace.x[index].toPrecision(6)) + '. ' +
      range.dataset.y + ' ' + (trace.y[index] === null ? 'unavailable' : trace.y[index].toPrecision(6)) + '.';
  }
  traces.addEventListener('change', showPoint);
  range.addEventListener('input', showPoint);
  showPoint();
}
"""


def _html_graph(key, graph, image, accessible):
    escape = html.escape
    description = graph.description() if accessible else graph.title
    content = [
        f'<figure data-chart id="{escape(key)}"><figcaption>{escape(graph.title)}</figcaption>',
        f'<p>{escape(description)}</p>' if accessible else "",
        ('<div class="plot-controls"><button data-factor="1.35" type="button">Zoom in</button>'
        '<button data-factor="0.74" type="button">Zoom out</button><button data-factor="0" type="button">Reset zoom</button>'
        '<output aria-live="polite">Zoom 100 percent</output></div>'),
        (f'<div class="plot-view" tabindex="0" role="region" aria-label="{escape(graph.title)} viewport">'
        f'<img alt="{escape(description)}" src="{_data_png(image)}"></div>'),
    ]
    available = [item for item in graph.traces if item["x"].size]
    if available:
        traces = [{axis: np.where(np.isfinite(item[axis]), item[axis], None).tolist() for axis in ("x", "y")}
                  for item in available]
        data = json.dumps(traces, separators=(",", ":"), allow_nan=False).replace("<", "\\u003c")
        options = "".join(f'<option value="{i}">{escape(item["label"])}</option>' for i, item in enumerate(available))
        content.extend((
            '<details><summary>Explore data points</summary>',
            f'<label>Trace <select>{options}</select></label>',
            (f'<label>Point <input type="range" min="0" value="0" data-x="{escape(graph.x_label)}" '
            f'data-y="{escape(graph.y_label)}"></label>'),
            '<p class="point-value" aria-live="polite"></p>',
            f'<script type="application/json">{data}</script></details>',
        ))
    if graph.heatmap is not None:
        x, y, values = graph.heatmap
        cells = {"x": x.tolist(), "y": y.tolist(),
                 "values": np.where(np.isfinite(values), np.round(values, 3), None).tolist()}
        data = json.dumps(cells, separators=(",", ":"), allow_nan=False)
        content.extend((
            '<details><summary>Explore spectrum levels</summary>',
            f'<label>Time index <input type="range" min="0" max="{len(x) - 1}" value="0"></label>',
            f'<label>Frequency index <input type="range" data-frequency min="0" max="{len(y) - 1}" value="0"></label>',
            '<p class="point-value" aria-live="polite"></p>',
            f'<script type="application/json">{data}</script></details>',
        ))
    content.append("</figure>")
    return "".join(content)


def html_report(report, images=None, *, sections=None):
    rendered = ["<!doctype html><html lang=\"en\"><meta charset=\"utf-8\">",
                '<meta name="viewport" content="width=device-width, initial-scale=1">',
                "<title>Fluttery measurement report</title><style>",
                ("body{margin:2rem auto;padding:0 1rem;max-width:68rem;color:#152033;background:#fff;font:1rem/1.5 system-ui}"
                "h1,h2{line-height:1.15}h2{margin-top:2rem}figure{margin:1.75rem 0}"
                "figcaption{font-size:1.25rem;font-weight:bold}.plot-view{overflow:auto;border:1px solid #aab4c3}"
                "img{width:100%;height:auto;display:block}.logo{width:auto;max-width:100%;max-height:7rem}"
                "th,td{padding:.3rem .65rem;border-bottom:1px solid #c2cad7;text-align:left;white-space:pre-wrap;overflow-wrap:anywhere}"
                "table{width:100%;border-collapse:collapse;margin:0.7rem 0}th{background:#edf2f9}"
                "button,input,select{font:inherit;margin:.15rem}.plot-controls{margin:.4rem 0}"
                "output,label{display:inline-block;margin:.15rem .5rem}:focus-visible{outline:3px solid #326bb4}"
                "@media print{button,output,input,select{display:none}figure{break-inside:avoid}}"),
                f"</style><body><main><h1>Fluttery measurement report</h1><p>{report.date_label}</p>"]
    logo = report_logo_png(report.options)
    if logo:
        rendered.append(f'<img class="logo" alt="Report logo" src="{_data_png(logo)}">')
    images = {} if images is None else images
    sections = report_sections(report) if sections is None else sections
    for kind, name, content in sections:
        if kind == "graph":
            if name not in images:
                images[name] = graph_png(content, plot_style=report.plot_style)
            rendered.append(_html_graph(name, content, images[name], report.options["includeAccessibility"]))
        elif kind == "table":
            headers, rows = content
            table = _html_table(headers, rows)
            if len(rows) > 12:
                rendered.append(f"<details><summary>{html.escape(name)} ({len(rows)} rows)</summary>{table}</details>")
            else:
                rendered.append(f"<h2>{html.escape(name)}</h2>{table}")
        else:
            rendered.append(f"<h2>{html.escape(name)}</h2><p>{html.escape(content).replace(chr(10), '<br>')}</p>")
    rendered.append(f"</main><script>{_HTML_SCRIPT}</script></body></html>\n")
    return "".join(rendered).encode("utf-8")


def _vector_plot(graph, width, plot_style):
    """Draw report line plots as PDF paths with selectable axis text."""
    from reportlab.graphics.shapes import Circle, Drawing, Group, Line, PolyLine, Rect, String
    from reportlab.lib.colors import Color
    from reportlab.pdfbase.pdfmetrics import stringWidth

    height = width * 2.9 / 8.2
    drawing = Drawing(width, height)
    left, right = 58, width - 20
    legend = plot_style.legend == "always" or (plot_style.legend == "auto" and len(graph.traces) > 1)
    bottom, top = 34, height - (22 if legend else 12)
    paper = DEFAULT_PLOT_STYLE.colors()
    ink, grid, minor, edge = (Color(*(channel / 255 for channel in rgb))
                              for rgb in (paper.text, paper.grid, paper.minor_grid, paper.axis))
    font = "FlutteryBody"
    font_size = 7.5
    traces = [
        (trace, np.log10(trace["x"]) if graph.log_x else trace["x"], trace["y"])
        for trace in graph.traces
    ]

    def bounds(values):
        finite = [array[np.isfinite(array)] for array in values]
        limits = [(float(np.min(array)), float(np.max(array))) for array in finite if array.size]
        if not limits:
            return -0.01, 0.01
        low = min(item[0] for item in limits)
        high = max(item[1] for item in limits)
        extra = (high - low) * 0.035 if high > low else max(0.0001, abs(low) * 0.02)
        return low - extra, high + extra

    x0, x1 = bounds(x for _, x, _ in traces)
    y0, y1 = bounds(y for _, _, y in traces)

    def px(value):
        return left + (value - x0) / (x1 - x0) * (right - left)

    def py(value):
        return bottom + (value - y0) / (y1 - y0) * (top - bottom)

    def label(x, y, value, anchor="start"):
        return String(x, y, value, textAnchor=anchor, fontName=font, fontSize=font_size, fillColor=ink)

    x_ticks = axis_ticks(x0, x1, min(6, max(3, int((right - left) // 75))), logarithmic=graph.log_x)
    y_ticks = axis_ticks(y0, y1, min(5, max(2, int((top - bottom) // 19))))
    if plot_style.grid and plot_style.grid_detail == "minor":
        for (first, _), (last, _) in pairwise(x_ticks):
            position = px((first + last) / 2)
            drawing.add(Line(position, bottom, position, top, strokeColor=minor, strokeWidth=0.4))
        for (first, _), (last, _) in pairwise(y_ticks):
            position = py((first + last) / 2)
            drawing.add(Line(left, position, right, position, strokeColor=minor, strokeWidth=0.4))
    for x, text in x_ticks:
        x_position = px(x)
        if plot_style.grid:
            drawing.add(Line(x_position, bottom, x_position, top, strokeColor=grid, strokeWidth=0.5))
        drawing.add(label(x_position, bottom - 11, text, "middle"))
    for y, text in y_ticks:
        y_position = py(y)
        if plot_style.grid:
            drawing.add(Line(left, y_position, right, y_position, strokeColor=grid, strokeWidth=0.5))
        drawing.add(label(left - 4, y_position - 2, text, "end"))
    drawing.add(Rect(left, bottom, right - left, top - bottom, fillColor=None, strokeColor=edge, strokeWidth=0.7))
    drawing.add(label((left + right) / 2, 6, graph.x_label, "middle"))
    y_axis_label = Group(label(0, 0, graph.y_label, "middle"))
    y_axis_label.translate(11, (top + bottom) / 2)
    y_axis_label.rotate(90)
    drawing.add(y_axis_label)

    for index, (trace, x, y) in enumerate(traces):
        source = trace.get("color", TRACE_COLORS[index % len(TRACE_COLORS)])
        rgb = plot_style.trace_rgb(source, index, (255, 255, 255))
        color = Color(*(channel / 255 for channel in rgb))
        stroke_width = 0.6 * plot_style.line_width
        valid = np.isfinite(x) & np.isfinite(y)
        points = [(px(a), py(b)) for a, b in zip(x[valid], y[valid], strict=True)]
        style = trace.get("style", "line")
        dash = [4, 2] if style == "dashed" else None
        if style == "bars":
            for a, b in points:
                drawing.add(Line(a, bottom, a, b, strokeColor=color, strokeWidth=stroke_width))
        elif style == "points" or len(points) == 1:
            for a, b in points:
                drawing.add(Circle(a, b, 2.2, fillColor=color, strokeColor=None))
        else:
            # ReportLab's polyline connects every vertex. Split it at each
            # invalid reading so a dropout looks the same here as in Results.
            ends = np.flatnonzero(np.diff(np.r_[False, valid, False]))
            start = 0
            for length in ends[1::2] - ends[::2]:
                segment = points[start:start + length]
                start += length
                if len(segment) == 1:
                    drawing.add(Circle(*segment[0], 2.2, fillColor=color, strokeColor=None))
                else:
                    drawing.add(PolyLine([coordinate for point in segment for coordinate in point], strokeColor=color,
                                         strokeWidth=stroke_width, strokeDashArray=dash))
            if plot_style.point_markers:
                for a, b in points:
                    drawing.add(Circle(a, b, 1.45, fillColor=color, strokeColor=None))
        if legend:
            slot = (right - left) / len(traces)
            origin = left + index * slot
            text = trace["label"]
            if stringWidth(text, font, font_size) > slot - 21:
                while len(text) > 1 and stringWidth(text + "…", font, font_size) > slot - 21:
                    text = text[:-1]
                text += "…"
            drawing.add(Line(origin, top + 12, origin + 14, top + 12, strokeColor=color,
                             strokeWidth=stroke_width, strokeDashArray=dash))
            drawing.add(label(origin + 17, top + 9, text))
    return drawing


def pdf_report(report, images=None, *, sections=None):
    from matplotlib import get_data_path
    from reportlab.lib import colors, pagesizes
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import Image, LongTable, Paragraph, SimpleDocTemplate, TableStyle

    font_directory = Path(get_data_path()) / "fonts" / "ttf"
    for name, path in (("FlutteryBody", "DejaVuSans.ttf"), ("FlutteryBold", "DejaVuSans-Bold.ttf")):
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(name, str(font_directory / path)))
    page = getattr(pagesizes, report.options["paperSize"])
    size = report.options["scaleFactor"]
    margin = 34
    body_width = page[0] - 2 * margin
    styles = {
        "body": ParagraphStyle("body", fontName="FlutteryBody", fontSize=9 * size, leading=13 * size, spaceAfter=6),
        "table": ParagraphStyle("table", fontName="FlutteryBody", fontSize=8 * size, leading=11 * size),
        "header": ParagraphStyle("header", fontName="FlutteryBold", fontSize=8 * size, leading=11 * size),
        "title": ParagraphStyle("title", fontName="FlutteryBold", fontSize=19 * size, leading=23 * size, spaceAfter=15),
        "heading": ParagraphStyle("heading", fontName="FlutteryBold", fontSize=11 * size, leading=14 * size,
                                  spaceBefore=11, spaceAfter=5, keepWithNext=True),
    }

    def paragraph(text, style):
        return Paragraph(html.escape(str(text)).replace("\n", "<br/>"), styles[style])

    story = [paragraph("Fluttery measurement report", "title"), paragraph(report.date_label, "body")]
    logo = report_logo_png(report.options)
    if logo:
        width, height = ImageReader(io.BytesIO(logo)).getSize()
        scale = min(body_width / width, 58 * size / height)
        image = Image(io.BytesIO(logo), width * scale, height * scale)
        image.hAlign = "LEFT"
        story.append(image)
    images = {} if images is None else images
    sections = report_sections(report) if sections is None else sections
    for kind, name, content in sections:
        story.append(paragraph(content.title if kind == "graph" else name, "heading"))
        if kind == "graph":
            width = min(body_width, 535 * size)
            if content.heatmap is None:
                story.append(_vector_plot(content, width, report.plot_style))
            else:
                if name not in images:
                    images[name] = graph_png(content, plot_style=report.plot_style)
                story.append(Image(io.BytesIO(images[name]), width=width, height=width * 2.9 / 8.2))
            if report.options["includeAccessibility"]:
                story.append(paragraph(content.description(), "body"))
        elif kind == "table":
            headers, rows = content
            cells = [[paragraph(text, "header") for text in headers]]
            cells.extend([paragraph(text, "table") for text in row] for row in rows)
            widths = [body_width / len(headers)] * len(headers)
            if headers == ("Field", "Value") or headers == ("Metric", "Value"):
                widths = [body_width * 0.44, body_width * 0.56]
            elif headers == SAMPLE_HEADERS:
                widths = [body_width * 0.32] + [body_width * 0.17] * 4
            table = LongTable(cells, colWidths=widths, repeatRows=1, splitInRow=1, hAlign="LEFT")
            table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e5edf7")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f4f7fb")]),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LINEBELOW", (0, 0), (-1, 0), 0.6, colors.HexColor("#9cafc4")),
            ]))
            story.append(table)
        else:
            story.append(paragraph(content, "body"))

    def footer(pdf, document):
        pdf.setFont("FlutteryBody", 8)
        pdf.setFillColor(colors.HexColor("#44546b"))
        pdf.drawString(margin, 20, "Fluttery")
        pdf.drawRightString(page[0] - margin, 20, f"Page {document.page}")

    with io.BytesIO() as output:
        doc = SimpleDocTemplate(output, pagesize=page, title="Fluttery measurement report", author="Fluttery",
                                leftMargin=margin, rightMargin=margin, topMargin=margin, bottomMargin=margin)
        doc.build(story, onFirstPage=footer, onLaterPages=footer)
        return output.getvalue()


def zip_report(report, filename):
    images = {}
    sections = list(report_sections(report))
    with io.BytesIO() as output:
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr(f"{filename}.json", json_report(report))
            bundle.writestr(f"{filename}.csv", csv_report(report))
            bundle.writestr(f"{filename}.pdf", pdf_report(report, images, sections=sections))
            bundle.writestr(f"{filename}.html", html_report(report, images, sections=sections))
            # Complete bundles have always included the source graph images.
            # Section choices govern the reports, not these separate files.
            excluded = (key for key in GRAPH_FILENAMES if not report.options["sessionData"][key])
            additional = session_graphs(report.history, report.graph_source, live_spectrum=report.live_spectrum,
                                        spectra=report.session.get("spectra"), signal_loss=report.session.get("signalLoss") or (),
                                        comparison=report.comparison["history"] if report.comparison else (),
                                        comparison_name=report.comparison_name, keys=excluded, run_label=report.run_label)
            images.update((key, graph_png(graph, plot_style=report.plot_style)) for key, graph in additional.items())
            for key, image in images.items():
                bundle.writestr(f"images/{GRAPH_FILENAMES.get(key, key)}.png", image)
        return output.getvalue()


def write_export(path, format_name, report, *, graph=None, plot_style=DEFAULT_PLOT_STYLE):
    path = Path(path)
    if format_name == "zip":
        data = zip_report(report, path.stem)
    elif format_name == "png":
        if graph is None or not graph.traces and graph.heatmap is None:
            raise ValueError("Select a graph containing data before exporting a PNG.")
        data = graph_png(graph, plot_style=plot_style)
    else:
        data = {"csv": csv_report, "json": json_report, "html": html_report, "pdf": pdf_report}[format_name](report)
    atomic_write(path, data)
