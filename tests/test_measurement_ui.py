from collections import deque
from math import nan
from types import MethodType, SimpleNamespace

import numpy as np
import pytest

from fluttery.audio import MeasurementProcessor
from fluttery.audio.generator import GeneratorSettings
from fluttery.audio.processor import ANALYSIS_FRAMES
from fluttery.audio.scope import ScopeBuffer
from fluttery.lab_ui import LevelsDialog
from fluttery.measurement_ui import (
    AutoMeterScale,
    InputLevelMeter,
    MeasurePanel,
    ScopePanel,
    THDPanel,
    TurntablePanel,
    _frequency_and_drift,
    _generator_preview,
    _measurement_frequency_text,
    _quality_control_status,
    _scope_signal,
    _turntable_speed,
    _valid_history_points,
    _window_reading,
)
from fluttery.preferences import normalize_settings
from fluttery.reporting import measurement_graph


def test_auto_meter_preserves_headroom_and_waits_before_reducing_scale():
    meter = AutoMeterScale()
    assert meter.update((0.0, None), 0) == 0.1
    assert meter.update((0.095, 0.0), 1) == 0.4
    assert meter.update((0.32, 0.38), 2) == 1.0
    assert meter.update((4.1, 5.0), 3) == 4.0
    assert meter.update((0.0, 0.03), 4) == 4.0
    assert meter.update((None, None), 7) == 4.0
    assert meter.update((0.05, None), 8) == 4.0
    assert meter.update((0.07, None), 10) == 4.0  # A near-boundary level restarts the hold.
    assert meter.update((0.02, 0.04), 11) == 4.0
    assert meter.update((0.02, 0.04), 14) == 0.1


def test_measure_window_readout_distinguishes_waiting_from_real_zero():
    assert _window_reading(None, False, 0, 1) == "Waiting for lock"
    assert _window_reading(0.05, False, 1, 1) == "Waiting for lock"
    assert _window_reading(None, True, 0.995, 1) == "0.9 of 1 s locked"
    assert _window_reading(None, True, 20, 20) == "Calculating"
    assert _window_reading(0, True, 1, 1) == "0.000%"


def test_live_quality_control_waits_for_a_usable_second_and_preserves_peak_limit():
    assert _quality_control_status({"rmsSeconds": .99}, True, 0.0, .1) == "Awaiting valid reading"
    assert _quality_control_status({"rmsSeconds": 1, "invalidFrames": 2}, True, .02, .1) == "Uncertain reading"
    assert _quality_control_status({"rmsSeconds": 1, "clipped": True}, True, 0.0, .1) == "Uncertain reading"
    assert _quality_control_status({"rmsSeconds": 1}, True, .1, .1) == "Pass"
    assert _quality_control_status({"rmsSeconds": 1}, True, .10001, .1) == "Fail"


def test_unsettled_frequency_is_opt_in_and_leaves_measurements_unchanged():
    rate = 48_000
    settings = normalize_settings({"correctionFactor": 1.001})
    assert settings["showUnsettledFrequency"] is False
    processor = MeasurementProcessor(settings, sample_rate=rate)
    # Both parts have a strong tone, but the speeds are too far apart for
    # a single settled reading over the completed analysis block.
    frequencies = np.repeat([1_500, 6_000], ANALYSIS_FRAMES // 2)
    changing = .4 * np.sin(2 * np.pi * np.cumsum(frequencies) / rate)
    processor.feed(changing)
    baseline = processor.snapshot()
    recorded = processor.drain_measurements()
    assert baseline["unsettled_frequency"] == 0
    assert baseline["metrics"]["currentFreq"] == 0
    assert not baseline["metrics"]["locked"]
    assert _measurement_frequency_text(None, baseline["unsettled_frequency"]) == "No signal"

    enabled = normalize_settings({**settings, "showUnsettledFrequency": True})
    processor = MeasurementProcessor(enabled, sample_rate=rate)
    processor.feed(changing)
    estimate = processor.snapshot()["unsettled_frequency"]
    assert estimate == pytest.approx(6_006, abs=.1)
    assert _measurement_frequency_text(None, estimate) == "6006.00 Hz (estimate)"
    assert processor.metrics() == baseline["metrics"]
    assert processor.drain_measurements() == recorded
    assert processor.clock_readings() == [0]

    processor.configure(showUnsettledFrequency=False)
    assert processor.snapshot()["unsettled_frequency"] == 0
    assert processor.metrics() == baseline["metrics"]
    processor.configure(showUnsettledFrequency=True)
    processor.feed(np.zeros(ANALYSIS_FRAMES))
    assert processor.snapshot()["unsettled_frequency"] == 0
    processor.feed(.4 * np.sin(2 * np.pi * 3_000 * np.arange(ANALYSIS_FRAMES) / rate))
    assert processor.snapshot()["unsettled_frequency"] == 0
    assert processor.metrics()["locked"]
    measured, _ = _frequency_and_drift(processor.metrics(), 3_000)
    assert _measurement_frequency_text(measured, 6_006) == "3003.00 Hz"


def test_live_flutter_spectrum_clears_for_capture_gaps_and_returns_after_relock():
    traces, notices = [], []
    plot = SimpleNamespace(title="Flutter spectrum", set_series=traces.append)
    panel = SimpleNamespace(plot=plot, _spectrum_source=None, _set_spectrum_status=notices.append)
    spectrum = {"frequencies": [1, 4, 8], "db": [-40, -9, -55]}
    snapshot = {"flutter_spectrum": spectrum}
    refresh = MeasurePanel._refresh_flutter_spectrum
    assert refresh(panel, snapshot, True, False, "#084c88") == ""
    assert traces[-1][0]["y"].tolist() == [-40, -9, -55]
    status = refresh(panel, {**snapshot, "metrics": {"invalidFrames": 1}}, True, False, "#084c88")
    assert "continuous reference" in status and traces[-1] == []
    assert refresh(panel, {"flutter_spectrum": {}}, True, False, "#084c88") == status
    assert refresh(panel, snapshot, True, False, "#084c88") == ""
    assert traces[-1][0]["y"].tolist() == [-40, -9, -55]
    assert notices == ["", status, status, ""]


def test_history_distinguishes_an_internal_lock_gap_from_meter_startup():
    samples = [
        {"timestamp": 50, "aes6Peak": None, "locked": True},
        {"timestamp": 55, "aes6Peak": 0.0},
        {"timestamp": 56, "aes6Peak": 0.08, "locked": False},
        {"timestamp": 57, "aes6Peak": 0.09, "locked": True},
        {"timestamp": 58, "aes6Peak": None, "locked": False},
    ]
    elapsed, readings = _valid_history_points(samples, "aes6Peak")
    assert elapsed == [5, 7, 7]
    np.testing.assert_equal(readings, [0.0, nan, 0.09])
    assert _valid_history_points(samples[:3], "aes6Peak") == ([5], [0.0])


def test_generator_preview_is_a_steady_signal_with_selected_stereo_pan():
    sample_rate = 48000
    settings = GeneratorSettings(enabled=True, frequency=1000, level=0.5, pan=0.75)
    times_ms, stereo = _generator_preview(settings, sample_rate)
    expected = 0.5 * np.sin(2 * np.pi * times_ms)
    np.testing.assert_allclose(stereo[:, 1], expected, atol=1e-7)
    np.testing.assert_allclose(stereo[:, 0], expected * 0.25, atol=1e-7)
    assert times_ms[-1] == pytest.approx(4)


def test_input_peak_mark_is_held_then_decays_and_clears_when_input_stops():
    meter = SimpleNamespace(_reading=(None, None, "", ""), _hold_until=None, _previous_time=None,
                            _HOLD_SECONDS=1.5, _FALL_DB_PER_SECOND=24, Refresh=lambda _flag: None)
    colors = ("#003399", "#800000")
    for moment, peak, expected in ((0, -1, -1), (1.4, -20, -1), (1.6, -20, -3.4), (2.4, -20, -20)):
        InputLevelMeter.set_reading(meter, -25, peak, moment, colors)
        assert meter._reading[1] == pytest.approx(expected)
    InputLevelMeter.set_reading(meter, None, None, 2.5, colors)
    assert meter._reading[:2] == (None, None)


def test_turntable_speed_trend_keeps_gaps_and_restarts_with_a_different_reference():
    figures = []
    plot = SimpleNamespace(clear=lambda: None, set_default_view=lambda **_view: None, set_series=figures.append)
    panel = SimpleNamespace(plot=plot, _speed_history=deque(), _speed_context=None,
                            _speed_origin=None, _speed_chart_key=None)
    for moment, rpm in ((10, 33.1), (12, None), (13, 32.9), (42, 33.04)):
        TurntablePanel._render_speed_history(panel, moment, rpm, (33, 33, 3150), "#003399")
    visible, target = figures[-1]
    np.testing.assert_equal(visible["x"], [2, 3, 32])
    np.testing.assert_equal(visible["y"], [nan, 32.9, 33.04])
    assert target["x"] == (2, 32) and target["y"] == (33, 33)
    TurntablePanel._render_speed_history(panel, 43, 44.9, (45, 33, 3150), "#003399")
    np.testing.assert_equal(figures[-1][0]["x"], [0])
    assert figures[-1][1]["y"] == (45, 45)


@pytest.mark.parametrize(
    ("samples", "status"),
    [
        ([], "Low or no signal"),
        ([0.001, -0.001], "Low or no signal"),
        ([0.1, -0.1], "Good signal"),
        ([0.9, -0.9], "High signal"),
        ([0.99, 0.0], "Clipping"),
    ],
)
def test_scope_signal_uses_legacy_thresholds(samples, status):
    _, _, actual = _scope_signal(np.array(samples))
    assert actual == status


def test_scope_uses_the_engine_clock_once_for_the_timebase():
    nominal_rate, correction = 48_000, 1.01
    corrected_rate = nominal_rate * correction
    samples = np.sin(2 * np.pi * 1000 * np.arange(20_000) / nominal_rate)[:, None]
    buffer = ScopeBuffer(corrected_rate, samples, len(samples), object())
    calls = []

    def scope_frames(seconds, **kwargs):
        calls.append((seconds, kwargs))
        return buffer

    def selection(value):
        return SimpleNamespace(GetSelection=lambda: value)

    def value(reading):
        return SimpleNamespace(GetValue=lambda: reading)

    controller = SimpleNamespace(audio=SimpleNamespace(running=True, scope_frames=scope_frames),
                                 settings={"correctionFactor": correction})
    panel = SimpleNamespace(controller=controller, _source_epoch=buffer.epoch, _scanned_frame=None, _arming=False,
                            trigger_mode=selection(0), trigger_source=selection(0), units=selection(0),
                            trigger_slope=selection(0), trigger_level=value(0.0), pretrigger=value(20),
                            trigger_hysteresis=value(0.0))
    sweep, status = ScopePanel._acquire(panel, 200, False, {})
    assert calls == [(.4, {"modulation": False, "after": None})]
    assert status == "Triggered"
    assert sweep.rate == corrected_rate
    assert len(sweep.samples) == 9_697
    assert sweep.time_ms[-1] - sweep.time_ms[0] == pytest.approx(200, abs=1e-10)


def test_level_window_is_clock_corrected_without_changing_voltage_calibration_key():
    epoch = object()
    calls = []

    def observe(seconds):
        calls.append(seconds)
        return 48_000, np.zeros((48_480, 2)), 100_000, epoch

    controller = SimpleNamespace(settings={"correctionFactor": 1.01}, audio_input_identity=lambda: "Example input",
                                 audio=SimpleNamespace(input_observation=observe))
    meter = SimpleNamespace(controller=controller, _settings=lambda: (20, 20_000, 1))
    rate, frames, sequence, route, low, high = LevelsDialog._pcm(meter)
    assert calls == [1.01]
    assert rate == len(frames) == 48_480 and sequence == 100_000
    assert route == (("Example input", 48_000), 2, epoch, 1.01)
    assert (low, high) == (20, 20_000)


def test_distortion_logging_keeps_thd_and_noise_separate_after_clock_reset():
    entries = []
    logger = SimpleNamespace(
        log_interval=THDPanel.log_interval,
        last_reading_time=None,
        next_log_time=None,
        controller=SimpleNamespace(log_measurement=lambda kind, data: entries.append((kind, data))),
    )
    reading = {
        "thdn": 1.21, "thd": 0.04, "fundamentalFrequency": 3150, "noisePercent": 1.2, "signalDb": -12,
    }
    times = np.arange(1, 101) * 4096 / 48_000
    for reading_time in times:
        THDPanel._log_distortion(logger, reading, reading_time)
    assert len(entries) == 85

    THDPanel._log_distortion(logger, reading, 0.01)
    assert len(entries) == 86
    kind, point = entries[-1]
    assert kind == "thd"
    assert point["thd"] == 1.21
    assert point["thdOnly"] == 0.04
    assert point["fundamental"] == 3150
    assert point["noisePercent"] == 1.2


def test_thirty_three_rpm_record_remains_measurable_at_forty_five_rpm():
    sample_rate = 48_000
    reference = 3150
    frequency = reference * 45 / 33
    processor = MeasurementProcessor({"targetFreq": reference, "useAutoTarget": False}, sample_rate)
    seconds = np.arange(sample_rate) / sample_rate
    processor.feed(0.3 * np.sin(2 * np.pi * frequency * seconds))
    metrics = processor.metrics()

    assert not metrics["locked"]
    measured, drift = _frequency_and_drift(metrics, reference)
    assert measured == pytest.approx(frequency, abs=1)
    assert drift == pytest.approx((45 / 33 - 1) * 100, abs=0.05)
    rpm, deviation = _turntable_speed(metrics, reference, 33, 45)
    assert rpm == pytest.approx(45, abs=0.02)
    assert deviation == pytest.approx(0, abs=0.05)


def _turntable_recorder(tool):
    entries, views, announcements = [], [], []
    controller = SimpleNamespace(
        settings={}, session={"measurementLog": entries},
        ensure_audio=lambda **_kwargs: True, play_sound=lambda _sound: None, announce=announcements.append,
        log_measurement=lambda kind, data, stats: entries.insert(0, {
            "timestamp": len(entries) + 1, "type": kind, "data": data, "stats": stats,
        }),
        navigate=lambda *view: views.append(view),
        results=SimpleNamespace(review_measurements=lambda kind: views.append(("measurements", kind))),
    )
    control = SimpleNamespace(Enable=lambda: None, Disable=lambda: None)
    panel = SimpleNamespace(controller=controller, tool=control, speed=control, recording=None, active_tool=tool,
                            _capture_request=None,
                            theme_accents=lambda: ("#000000", "#ffffff"), refresh_settings=lambda: None,
                            _sync_turntable_controls=lambda: None)
    for name in ("_toggle_recording", "_present", "_cache_spectrum", "_recorded_spectrum"):
        setattr(panel, name, MethodType(getattr(TurntablePanel, name), panel))
    panel._recorded_stats = TurntablePanel._recorded_stats
    return panel, entries, views, announcements


@pytest.mark.parametrize("readings", ["unavailable", "valid", "stop between updates", "no input"])
def test_tracking_recording_keeps_spectrum_when_thd_is_unavailable(readings):
    panel, entries, views, announcements = _turntable_recorder("tracking")
    panel._toggle_recording()

    left = {"available": readings == "valid", "thdn": 0.4, "thd": 0.2, "fundamentalFrequency": 1000}
    spectrum = {"frequencies": [20, 40, 60], "db": [-60, -15, -65], "peakFrequency": 40, "peakDb": -15}
    right = {**spectrum, "db": [-30, -60, -20]}
    snapshot = {"metrics": {"sequence": 1}, "health": {"running": True}, "tracking": {"left": left},
                "stereo_spectrum": {"left": spectrum, "right": right}}
    if readings in ("unavailable", "valid"):
        previous = {**spectrum, "db": [-65, -10, -75]}
        panel._present({**snapshot, "stereo_spectrum": {"left": previous}}, draw=False, record=True)
        panel._present(snapshot, draw=False, record=True)
    # A missing spectrum at stop must leave the last complete capture intact.
    panel.controller.audio = SimpleNamespace(snapshot=lambda: snapshot if readings == "stop between updates" else {})
    panel._toggle_recording()
    if readings == "no input":
        assert not entries
        assert announcements[-1] == "No usable signal was captured."
        return

    entry, = entries
    assert entry["type"] == "tracking"
    TurntablePanel._view_results(panel)
    if readings == "valid":
        point, = entry["data"]
        assert point["thd"] == 0.4
        assert entry["stats"]["avg"] == 0.4
        assert views == [("results", "thd")]
    else:
        assert entry["data"] == spectrum["db"]
        assert entry["stats"] == {"spectrumEncoding": "db", "startHz": 20, "binHz": 20,
                                  "peakFrequency": 40, "peakDb": -15}
        graph = measurement_graph(entry)
        np.testing.assert_array_equal(graph.traces[0]["x"], spectrum["frequencies"])
        np.testing.assert_array_equal(graph.traces[0]["y"], spectrum["db"])
        assert views == [("measurements", "tracking")]


def test_tracking_recording_keeps_right_only_distortion_and_channel_identity():
    panel, entries, _views, _announcements = _turntable_recorder("tracking")
    panel._toggle_recording()
    snapshot = {"metrics": {"sequence": 1}, "health": {"running": True}, "tracking": {
        "right": {"available": True, "thdn": .31, "thd": .07, "fundamentalFrequency": 1000},
    }}
    panel._present(snapshot, draw=False, record=True)
    panel._toggle_recording()
    entry, = entries
    point, = entry["data"]
    assert set(point) == {"timestamp", "rightThdn", "rightThdOnly", "rightFundamental"}
    assert point["rightThdn"] == .31
    assert entry["stats"] == {"rightAvg": .31, "rightMin": .31, "rightMax": .31}


def test_crosstalk_shows_the_live_test_tone_before_either_separation_capture():
    labels = []
    panel = SimpleNamespace(
        _separation={}, separation_status=SimpleNamespace(GetLabel=lambda: ""),
        separation_live=SimpleNamespace(GetLabel=lambda: "", SetLabel=labels.append),
    )
    readings, lines = TurntablePanel._separation_readout(panel, {
        "available": True, "leftDb": -16.4, "rightDb": -180, "rawRightDb": -180, "balanceDb": 163.6,
    })
    assert readings[0][1] == readings[1][1] == "Awaiting capture"
    assert labels[-1] == "Live test tone: left -16.4 dBFS RMS · right below the measurement floor"
    assert labels[-1] + "." in lines
    TurntablePanel._separation_readout(panel, {"available": False, "clipped": True})
    assert labels[-1] == "Live test tone: input clipped"


@pytest.mark.parametrize(("tool", "spectrum_key", "levels"), [
    ("noise", "spectrum", {"rmsDb": -58.0, "rumbleDb": -65.0}),
    ("resonance", "resonance_spectrum", {"frequency": 10.0, "levelDb": -65.0}),
])
def test_turntable_spectrum_capture_can_stop_between_display_updates(tool, spectrum_key, levels):
    panel, entries, views, _ = _turntable_recorder(tool)
    spectrum = {"frequencies": [0, 10, 20], "db": [-88, -65, -78], "peakDb": -65, "peakFrequency": 10}
    panel.controller.audio = SimpleNamespace(snapshot=lambda: {
        "metrics": {"sequence": 12}, "health": {"running": True}, spectrum_key: spectrum, tool: levels,
    })
    panel._toggle_recording()
    panel._toggle_recording()
    entry, = entries
    assert entry["type"] == tool
    assert entry["data"] == spectrum["db"]
    assert entry["stats"] == {"spectrumEncoding": "db", "startHz": 0, "binHz": 10,
                              "peakDb": -65, "peakFrequency": 10, **levels}
    TurntablePanel._view_results(panel)
    assert views == [("measurements", tool)]
