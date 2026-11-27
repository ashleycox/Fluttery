"""Signal and stream regressions without requiring capture hardware."""

from concurrent.futures import ThreadPoolExecutor
from math import sqrt
from threading import Event
from time import monotonic, sleep
from types import SimpleNamespace

import numpy as np
import pytest

import fluttery.audio.engine as engine_module
from fluttery.audio import AudioEngine, MeasurementProcessor
from fluttery.audio.engine import FrameQueue
from fluttery.audio.generator import GeneratorSettings, MonitorResampler, SignalGenerator, monitor_route
from fluttery.audio.session_capture import SessionAudioCapture
from fluttery.audio.sweep import SweepSettings


def tone(frequency=3000, rate=48_000, seconds=2, amplitude=0.5, modulation=0, modulation_hz=4):
    elapsed = np.arange(round(rate * seconds)) / rate
    instantaneous = frequency * (1 + modulation / 100 * np.sin(2 * np.pi * modulation_hz * elapsed))
    return amplitude * np.sin(2 * np.pi * np.cumsum(instantaneous / rate))


@pytest.mark.parametrize("rate", [44_100, 48_000, 96_000, 192_000])
def test_steady_reference_has_low_unweighted_flutter(rate):
    processor = MeasurementProcessor({"weightingStandard": "unweighted"}, sample_rate=rate)
    for block in np.array_split(tone(rate=rate, seconds=1.2), 37):
        processor.feed(block)
    metrics = processor.metrics()
    assert metrics["locked"]
    assert metrics["currentFreq"] == pytest.approx(3000, abs=0.05)
    assert metrics["bufferPeak"] < 0.001


@pytest.mark.parametrize("rate", [44_100, 48_000, 96_000, 192_000])
def test_frequency_counter_accepts_dc_offset_and_unequal_half_cycles(rate):
    frequency = 3001.17
    elapsed = np.arange(5 * 4096) / rate
    phase = 2 * np.pi * frequency * elapsed + 0.23
    # This asymmetric waveform stays positive for its entire cycle. An input
    # offset should have no effect on measured period or on clock calibration.
    samples = 0.54 + 0.31 * np.sin(phase) + 0.07 * np.sin(2 * phase + 0.45)
    processor = MeasurementProcessor({"useAutoTarget": False}, sample_rate=rate)
    processor.feed(samples)
    assert processor.metrics()["locked"]
    assert processor.clock_readings() == pytest.approx([frequency] * 5, abs=0.015)


@pytest.mark.parametrize("rate, fraction", [(8_000, 0.488), (44_100, 0.48), (48_000, 0.488)])
def test_steady_reference_near_nyquist_does_not_create_flutter(rate, fraction):
    frequency = fraction * rate
    processor = MeasurementProcessor({
        "targetFreq": frequency, "weightingStandard": "unweighted", "useAutoTarget": False,
    }, sample_rate=rate)
    processor.feed(tone(frequency, rate=rate, seconds=1.2))
    result = processor.metrics()
    assert result["locked"]
    assert result["currentFreq"] == pytest.approx(frequency, abs=0.05)
    assert result["bufferPeak"] < 0.0002


def test_demodulated_scope_preserves_speed_direction_and_clock_correction():
    engine = AudioEngine({"targetFreq": 3000, "weightingStandard": "unweighted",
                          "useDCBlocker": False, "useAutoTarget": False})
    samples = tone(frequency=2997, seconds=1)
    scope = engine.feed(samples)["scope"]["modulation"]
    assert np.mean(scope[-128:]) == pytest.approx(-0.1, abs=0.0001)

    engine.configure({"correctionFactor": 3000 / 2997})
    result = engine.feed(samples)
    assert result["metrics"]["currentFreq"] == pytest.approx(3000, abs=0.005)
    assert np.mean(result["scope"]["modulation"][-128:]) == pytest.approx(0, abs=0.0001)


def test_known_modulation_preserves_state_across_callback_lengths():
    signal = tone(seconds=4, modulation=0.1)
    expected = MeasurementProcessor()
    expected.feed(signal)
    actual = MeasurementProcessor()
    for chunk in np.array_split(signal, 173):
        actual.feed(chunk)
    metrics = actual.metrics()
    assert metrics["bufferPeak"] == pytest.approx(0.1, abs=0.01)
    assert metrics["rms"] == pytest.approx(0.1 / sqrt(2), abs=0.004)
    assert metrics["rms"] == pytest.approx(expected.metrics()["rms"], abs=1e-8)
    assert metrics["wow"]["peak"] == pytest.approx(expected.metrics()["wow"]["peak"], abs=1e-8)
    readings = actual.drain_measurements()
    assert len(readings) == len(signal) // 4096
    assert actual.drain_measurements() == []
    assert readings[0]["timestamp"] < readings[-1]["timestamp"]


def test_weighting_and_wow_flutter_split_change_response():
    modulation = tone(seconds=3, modulation=0.1, modulation_hz=40)
    iec = MeasurementProcessor({"weighting": "iec"})
    unweighted = MeasurementProcessor({"weighting": "unweighted"})
    iec.feed(modulation)
    unweighted.feed(modulation)
    # The nominal CCIR/IEC curve is −10.4 dB at 40 Hz. The former single
    # narrow band-pass attenuated this signal much more than the standard.
    weighted_ratio_db = 20 * np.log10(iec.metrics()["rms"] / unweighted.metrics()["rms"])
    assert weighted_ratio_db == pytest.approx(-10.4, abs=0.3)

    near_split = tone(seconds=3, modulation=0.1)
    low = MeasurementProcessor({"splitFrequency": 2})
    high = MeasurementProcessor({"splitFrequency": 15, "weightingStandard": "jis"})
    low.feed(near_split)
    high.feed(near_split)
    assert high.metrics()["wow"]["rms"] > high.metrics()["flutter"]["rms"]
    assert low.metrics()["wow"]["rms"] < low.metrics()["flutter"]["rms"]
    assert high.metrics()["weighting"] == "jis"


def test_peak_detector_release_responds_to_profile():
    fast = MeasurementProcessor({"detectorTiming": {"preset": "fast"}})
    slow = MeasurementProcessor({"detectorTiming": {"preset": "slow"}})
    history = np.concatenate((tone(modulation=1), tone(seconds=1.5)))
    fast.feed(history)
    slow.feed(history)
    assert slow.metrics()["peak"] > 1.5 * fast.metrics()["peak"]


def test_auto_target_switch_and_loss_of_lock():
    processor = MeasurementProcessor()
    processor.feed(tone(frequency=3150, seconds=2, amplitude=0.01))
    assert processor.metrics()["activeTarget"] == 3150
    assert processor.metrics()["locked"]
    processor.feed(np.zeros(8192))
    assert processor.metrics()["currentFreq"] == 0
    assert not processor.metrics()["locked"]
    assert processor.metrics()["dropouts"] == 1


def test_ambiguous_and_clipped_reference_readings_do_not_count_as_locked():
    processor = MeasurementProcessor()
    processor.feed(tone(frequency=3075, seconds=1))
    metrics = processor.metrics()
    assert metrics['currentFreq'] == pytest.approx(3075, abs=.5)
    assert metrics['referenceAmbiguous'] and not metrics['locked']
    processor.feed(np.clip(tone(frequency=3000, seconds=1, amplitude=1.8), -1, 1))
    metrics = processor.metrics()
    assert metrics['clipped'] and not metrics['locked']
    assert metrics['rmsSeconds'] == 0
    assert type(metrics['invalidFrames']) is int


def test_detector_dropouts_count_active_windows_without_a_frequency():
    engine = AudioEngine({"useAutoTarget": False})
    # Two full-scale-referenced but unpitched windows count as misses. Silence
    # and an off-speed tone with a valid frequency do not.
    snapshot = engine.feed(np.full(8192, 0.1, dtype=np.float32))
    assert snapshot["metrics"]["detectorDropouts"] == snapshot["health"]["detectorDropouts"] == 2
    assert snapshot["metrics"]["dropouts"] == 0
    snapshot = engine.feed(tone(frequency=1500, seconds=4096 / 48_000))
    assert snapshot["metrics"]["currentFreq"] == pytest.approx(1500, abs=1)
    assert not snapshot["metrics"]["locked"]
    assert engine.feed(np.zeros(4096))["metrics"]["detectorDropouts"] == 2


def test_scope_exposes_demodulated_modulation_as_percent_speed_variation():
    engine = AudioEngine({"targetFreq": 3000, "weightingStandard": "unweighted", "useAutoTarget": False})
    result = engine.feed(tone(seconds=3, modulation=0.1))
    scope = result["scope"]
    samples = scope["modulation"]
    window_rate = scope["modulationSampleRate"]
    assert 1000 <= window_rate <= 1100
    assert len(samples) / window_rate == pytest.approx(2, abs=0.002)
    assert np.sqrt(np.mean(samples**2)) == pytest.approx(0.1 / sqrt(2), abs=0.002)
    assert not samples.flags.writeable


def test_auto_target_acknowledgment_preserves_live_detector_state():
    samples = tone(frequency=3150, seconds=2, modulation=0.1)
    acknowledged = MeasurementProcessor()
    reference = MeasurementProcessor()
    split = len(samples) - 4080
    for processor in (acknowledged, reference):
        processor.feed(samples[:split])
    prior = acknowledged.metrics()
    assert prior["activeTarget"] == 3150
    assert prior["requestedTarget"] == 3000
    assert prior["peak"] > 0.04
    assert prior["locked"]

    acknowledged.configure(targetFreq=3150)
    assert acknowledged.metrics() == {**prior, "requestedTarget": 3150}
    for processor in (acknowledged, reference):
        processor.feed(samples[split:])
    assert acknowledged.metrics() == {**reference.metrics(), "requestedTarget": 3150}

    acknowledged.configure(targetFreq=3100)
    edited = acknowledged.metrics()
    assert edited["activeTarget"] == edited["requestedTarget"] == 3100
    assert edited["peak"] == edited["rms"] == 0
    assert not edited["locked"]


def test_distortion_distinguishes_harmonic_and_broadband_energy():
    processor = MeasurementProcessor()
    samples = tone(1000, amplitude=0.4) + tone(2000, amplitude=0.01) + tone(750, amplitude=0.01)
    processor.feed(samples)
    snapshot = processor.snapshot()
    measured = snapshot["thd"]
    assert measured["available"]
    assert measured["fundamentalFrequency"] == pytest.approx(1000, abs=0.1)
    assert measured["thd"] == pytest.approx(2.5, abs=0.01)
    assert measured["thdn"] == pytest.approx(2.5 * sqrt(2), abs=0.01)
    assert snapshot["scope"]["correlation"] == pytest.approx(1)


def test_clock_and_stereo_channel_calibration():
    engine = AudioEngine({"targetFreq": 1000, "correctionFactor": 1.2})
    left = tone(999, amplitude=0.4)
    right = tone(999, amplitude=0.2)
    engine.feed(np.column_stack((left, right)))
    assert engine.snapshot()["metrics"]["currentFreq"] == pytest.approx(1198.8, abs=0.01)
    assert engine.calibrate_clock(1000) == pytest.approx(1000 / 999, abs=1e-5)
    correction = engine.calibrate_channels(1000)
    assert correction["imbalanceDb"] == pytest.approx(6.02, abs=0.01)
    engine.configure(channelCalibration=correction, correctionFactor=1000 / 999)
    engine.feed(np.column_stack((left, right)))
    snapshot = engine.snapshot()
    assert snapshot["stereo"]["balanceDb"] == pytest.approx(0, abs=0.01)
    assert snapshot["stereo"]["available"]
    engine.reset()
    assert len(engine.snapshot()["scope"]["left"]) == 0
    assert not engine.drain_measurements()
    with pytest.raises(RuntimeError, match="steady reference"):
        engine.calibrate_clock(1000)


def test_clock_and_channel_calibrations_reject_clipped_reference():
    engine = AudioEngine({'targetFreq': 1000})
    reference = np.clip(tone(1000, seconds=.4, amplitude=1.4), -1, 1)
    engine.feed(np.column_stack((reference, reference * .4)))
    with pytest.raises(RuntimeError, match='steady reference'):
        engine.calibrate_clock(1000)
    with pytest.raises(RuntimeError, match='steady tone'):
        engine.calibrate_channels(1000)


def test_low_frequency_lock_cannot_be_reused_during_a_static_input():
    processor = MeasurementProcessor({'targetFreq': 1})
    processor.feed(tone(1, seconds=4, amplitude=.3))
    assert processor.metrics()['locked']
    processor.feed(np.full(8192, .1))
    assert not processor.metrics()['locked']


def test_malformed_nested_presets_fall_back_to_numeric_settings():
    processor = MeasurementProcessor({
        'detectorTiming': {'preset': ['fast'], 'rmsTime': .75},
        'weightingProfile': {'preset': {'kind': 'iec'}, 'q': 3},
    })
    assert processor.settings['detector_timing'] == (.75, .01, 2.)
    assert processor.settings['weighting_profile'] == (4., 3., 1.)
    assert processor.settings['weighting_profile_name'] == 'custom'


@pytest.mark.parametrize("drive_right", [False, True])
def test_live_stereo_reading_tracks_a_slightly_off_speed_reference(drive_right):
    engine = AudioEngine({"targetFreq": 3150, "useAutoTarget": False, "channels": 2})
    carrier = tone(3194, amplitude=0.32)
    left, right = (carrier / 100, carrier) if drive_right else (carrier, carrier / 100)
    reading = engine.feed(np.column_stack((left, right)))["stereo"]
    assert reading["available"]
    assert reading["targetFrequency"] == 3150
    assert reading["toneFrequency"] == pytest.approx(3194, abs=0.2)
    assert max(reading["leftDb"], reading["rightDb"]) == pytest.approx(20 * np.log10(0.32 / sqrt(2)), abs=0.1)
    assert reading["balanceDb"] == pytest.approx(-40 if drive_right else 40, abs=0.05)
    assert reading["crosstalkDb"] == pytest.approx(-40, abs=0.05)
    assert not engine.channel_readings(3150)["referencePresent"]


def test_low_frequency_resonance_is_resolved_in_a_separate_spectrum():
    processor = MeasurementProcessor()
    processor.feed(tone(10, seconds=4, amplitude=0.02))
    snapshot = processor.snapshot()
    assert snapshot["resonance"]["frequency"] == pytest.approx(10, abs=0.05)
    assert snapshot["resonance"]["levelDb"] == pytest.approx(-36.99, abs=0.05)
    assert snapshot["noise"]["rumbleDb"] == pytest.approx(-36.99, abs=0.05)
    assert snapshot["resonance_spectrum"]["frequencies"][-1] <= 30


@pytest.mark.parametrize("waveform", ["sine", "square", "triangle", "sawtooth"])
def test_generator_output_is_phase_continuous_across_blocks(waveform):
    settings = GeneratorSettings(True, 3000, waveform, 0.25, 4.0, 0.1, 1.0)
    single = SignalGenerator().render(10_000, 48_000, settings)
    generator = SignalGenerator()
    blocks = np.concatenate([generator.render(size, 48_000, settings) for size in (1000, 322, 8678)])
    assert np.array_equal(single[:, 0], np.zeros(len(single)))
    assert np.allclose(single, blocks, atol=1e-6)
    assert 0.2 < np.max(np.abs(blocks[:, 1])) <= 0.25


def test_monitor_routes_channels_and_resamples_independently():
    matrix = np.column_stack([np.arange(100), -np.arange(100)])
    left = monitor_route(matrix, None, "left")
    assert np.array_equal(left[:, 0], left[:, 1])
    assert not np.any(monitor_route(matrix, None, "mono"))
    resampler = MonitorResampler(48_000, 44_100)
    chunks = np.concatenate([resampler.process(chunk) for chunk in np.array_split(matrix, 9)])
    single = MonitorResampler(48_000, 44_100).process(matrix)
    assert np.allclose(chunks, single)


@pytest.mark.parametrize(('input_rate', 'output_rate'), [(32_000, 48_000), (48_000, 32_000), (48_000, 44_100)])
def test_monitor_resampling_has_exact_frame_count_at_arbitrary_block_boundaries(input_rate, output_rate):
    signal = np.random.default_rng(30).standard_normal((1097, 2))
    resampler = MonitorResampler(input_rate, output_rate)
    split = np.concatenate([resampler.process(block) for block in np.array_split(signal, 997)])
    count = ((len(signal) - 1) * output_rate + input_rate - 1) // input_rate
    assert len(split) == count
    np.testing.assert_array_equal(split, MonitorResampler(input_rate, output_rate).process(signal))


def test_monitor_sums_or_averages_stereo_and_isolates_the_real_right_input():
    stereo = np.array([[0.75, 0.5], [-0.5, 0.25]], dtype=np.float32)
    mono = stereo[:, :1]
    np.testing.assert_array_equal(monitor_route(stereo, None, "stereo"), stereo)
    np.testing.assert_array_equal(monitor_route(stereo, None, "right"), [[0.5, 0.5], [0.25, 0.25]])
    np.testing.assert_array_equal(monitor_route(stereo, None, "mono"), [[0.625, 0.625], [-0.125, -0.125]])
    np.testing.assert_array_equal(monitor_route(stereo, None, "mono_sum"), [[1.25, 1.25], [-0.25, -0.25]])
    np.testing.assert_array_equal(monitor_route(mono, None, "mono_sum"), [[0.75, 0.75], [-0.5, -0.5]])
    assert not np.any(monitor_route(mono, None, "right"))
    engine = AudioEngine()
    engine.set_monitor(True, level=0.5, mode="mono_sum")
    assert engine.snapshot()["monitor"] == {"enabled": True, "level": 0.5, "mode": "mono_sum"}


def test_software_loopback_runs_without_opening_any_device(monkeypatch):
    def unavailable_backend():
        pytest.fail("Software loopback attempted to open an audio device.")

    engine = AudioEngine()
    monkeypatch.setattr(engine, "_backend", unavailable_backend)
    engine.set_generator(True, frequency=3000)
    engine.set_loopback(True)
    engine.start(input_enabled=False, output_enabled=False)
    try:
        limit = monotonic() + 2
        while not engine.snapshot()["metrics"]["locked"] and monotonic() < limit:
            sleep(0.01)
        snapshot = engine.snapshot()
        assert snapshot["metrics"]["locked"]
        assert snapshot["health"]["running"]
        assert not snapshot["health"]["inputActive"]
        assert not snapshot["health"]["outputActive"]
    finally:
        engine.stop()
    assert not engine.running


@pytest.fixture
def simulated_devices(monkeypatch):
    engine = AudioEngine()
    streams = []

    class Stream:
        def __init__(self, direction, device, channels, samplerate, dtype, callback):
            self.direction = direction
            self.device = device
            self.channels = channels
            self.samplerate = samplerate
            self.dtype = dtype
            self.callback = callback
            self.active = False
            streams.append(self)

        def start(self):
            self.active = True

        def abort(self):
            self.active = False

        def close(self):
            self.active = False

    devices = {
        (3, "input"): {"name": "Test ADC", "max_input_channels": 2, "default_samplerate": 48_000},
        (4, "output"): {"name": "Test DAC", "max_output_channels": 2, "default_samplerate": 44_100},
    }
    monkeypatch.setattr(
        engine,
        "_backend",
        lambda: SimpleNamespace(
            query_devices=lambda device, direction: devices[device, direction],
            check_output_settings=lambda **kwargs: None,
            InputStream=lambda **kwargs: Stream("input", **kwargs),
            OutputStream=lambda **kwargs: Stream("output", **kwargs),
        ),
    )
    return engine, streams


def test_device_open_direction_and_capture_frames(simulated_devices):
    engine, streams = simulated_devices
    engine.set_loopback(True)
    assert engine.start(input_device=3, output_device=4, channels=2)
    try:
        assert not engine.start(input_device=3, output_device=4, channels=2)
        capture, playback = streams
        assert capture.direction == "input" and capture.device == 3 and capture.channels == 2
        assert playback.direction == "output" and playback.device == 4
        assert capture.samplerate == playback.samplerate == 48_000
        left = tone(seconds=0.1).astype(np.float32)
        stereo = np.column_stack([left, left])
        capture.callback(stereo, len(left), None, SimpleNamespace(input_overflow=False))
        limit = monotonic() + 2
        while not engine.snapshot()["metrics"]["locked"] and monotonic() < limit:
            sleep(0.01)
        assert engine.snapshot()["metrics"]["locked"]
        assert engine.snapshot()["health"]["channels"] == 2
        tone_reading = engine.channel_readings(3000)
        assert tone_reading["available"]
        assert tone_reading["rawLeftDb"] == pytest.approx(-9.03, abs=0.2)
    finally:
        engine.stop()
    assert not any(stream.active for stream in streams)


@pytest.mark.parametrize('internal_loopback', [False, True])
def test_stopping_physical_input_keeps_playback_sweep_and_internal_analysis(simulated_devices, internal_loopback):
    engine, streams = simulated_devices
    engine.set_loopback(internal_loopback)
    engine.start(input_device=3, output_device=4, channels=2)
    retained = SessionAudioCapture()
    engine.start_session_audio(retained)
    try:
        capture, playback = streams
        stereo = np.column_stack([tone(seconds=.04), tone(seconds=.04)]).astype(np.float32)
        capture.callback(stereo, len(stereo), None, SimpleNamespace(input_overflow=False))
        identifier = engine.start_sweep(SweepSettings(start_frequency=2990, end_frequency=3010, duration=.4))
        target = np.empty((480, 2), dtype=np.float32)
        status = SimpleNamespace(output_underflow=False)
        deadline = monotonic() + 1
        while monotonic() < deadline:
            playback.callback(target, len(target), None, status)
            if np.any(target) and len(engine.recent_pcm(.1)[1]):
                break
            sleep(.005)
        assert np.any(target) and len(engine.recent_pcm(.1)[1])
        scope = engine.scope_frames(.02)
        assert engine.stop_capture()
        assert not engine.stop_capture()
        assert not capture.active and playback.active and engine.output_enabled
        assert engine.snapshot()['sweep']['id'] == identifier and engine.snapshot()['sweep']['active']
        assert not engine.start(input_device=3, output_device=4, channels=2, input_enabled=False)
        assert len(streams) == 2 and engine.input_observation(.02)[3] is None
        if internal_loopback:
            after = engine.scope_frames(.02)
            assert scope.epoch is after.epoch and after.end >= scope.end
        else:
            assert len(engine.scope_frames(.02).samples) == 0
        deadline = monotonic() + 2
        while engine.snapshot()['sweep']['active'] and monotonic() < deadline:
            playback.callback(target, len(target), None, status)
            sleep(.005)
        assert engine.snapshot()['sweep']['status'] == 'completed'
    finally:
        physical = engine.finish_session_audio(retained)
        engine.stop()
    assert physical.blocks and 'Live input was stopped' in physical.incomplete_reason


def test_pcm_queue_preserves_frames_on_wrap_and_partial_transfer():
    queue = FrameQueue(4, 1)
    samples = np.arange(6, dtype=np.float32).reshape(-1, 1)
    assert queue.write(samples[:3]) == 3
    first = np.zeros((2, 1))
    assert queue.read_into(first) == 2
    assert np.array_equal(first[:, 0], [0, 1])
    assert queue.write(samples[3:]) == 3
    remaining = np.zeros((6, 1))
    assert queue.read_into(remaining) == 4
    assert np.array_equal(remaining[:4, 0], [2, 3, 4, 5])


def test_response_capture_excludes_pending_monitor_audio_and_cancels_queued_stimulus():
    engine = AudioEngine()
    engine._input_stream = engine._output_stream = SimpleNamespace(samplerate=48_000)
    output = engine._output_queue = FrameQueue(4800, 2)
    controller_monitor = FrameQueue(4800, 2)
    output.write(np.ones((100, 2)))
    controller_monitor.write(np.ones((4800, 2)))
    stimulus = SimpleNamespace(render=lambda _rate: np.ones(48_000, dtype=np.float32))

    capture = engine.start_bench_capture(1.3, stimulus)
    assert output.count == 100  # Replacing input monitoring cannot starve the callback.
    engine._fill_output(48_000, controller_monitor)
    delivered = np.empty((1920, 2), dtype=np.float32)
    assert output.read_into(delivered) == len(delivered)
    assert not delivered.any()
    assert controller_monitor.count == 4800  # A worker block already in flight is also gated.

    output.write(np.full((100, 2), .75))
    engine.finish_bench_capture(capture, cancel=True)
    assert capture.error
    assert output.read_into(delivered) == 100
    assert not delivered[:100].any()


def test_explicit_output_sample_rate_does_not_fall_back_to_another_rate(simulated_devices, monkeypatch):
    engine, streams = simulated_devices
    backend = engine._backend()

    def check_rate(**options):
        if options['samplerate'] != 44_100:
            raise OSError('The DAC requires 44.1 kHz.')

    backend.PortAudioError = OSError
    backend.check_output_settings = check_rate
    monkeypatch.setattr(engine, '_backend', lambda: backend)
    with pytest.raises(RuntimeError, match='DAC requires 44.1 kHz'):
        engine.start(input_device=3, output_device=4, sample_rate=48_000)
    assert not engine.running and not any(stream.active for stream in streams)

    try:
        engine.start(input_device=3, output_device=4)
        health = engine.snapshot()['health']
        assert health['inputSampleRate'] == 48_000
        assert health['outputSampleRate'] == 44_100
    finally:
        engine.stop()


def test_stream_closes_after_abort_error_and_stopped_measurement_unlocks():
    engine = AudioEngine()
    closed = []

    def failed_abort():
        raise OSError('The input disconnected.')

    assert engine.feed(tone(seconds=.2))['metrics']['locked']
    engine._input_stream = SimpleNamespace(abort=failed_abort, close=lambda: closed.append(True))
    engine.stop()
    assert closed == [True]
    assert not engine.snapshot()['metrics']['locked']


@pytest.mark.parametrize("rate", [44_100, 48_000, 96_000, 192_000])
def test_continuous_internal_loopback_with_live_snapshot_polling(rate):
    engine = AudioEngine({"weightingStandard": "unweighted", "channels": 2})
    engine.set_generator(True, frequency=3000, modulation_frequency=4, modulation_depth=0.1)
    engine.set_loopback(True)
    engine.start(sample_rate=rate, input_enabled=False, output_enabled=False)
    try:
        limit = monotonic() + 2.5
        metrics_collected = []
        statistics = engine.snapshot()
        while statistics["metrics"]["timestamp"] < 1.1 and monotonic() < limit:
            sleep(0.02)
            statistics = engine.snapshot()
            metrics_collected.extend(engine.drain_measurements())
        metrics = statistics["metrics"]
        health = statistics["health"]
        assert metrics["timestamp"] >= 1.1
        assert metrics["locked"]
        recent_peak = max(point["bufferPeak"] for point in metrics_collected if point["timestamp"] > 0.8)
        assert recent_peak == pytest.approx(0.1, abs=0.01)
        assert metrics["rms"] == pytest.approx(0.1 / sqrt(2), abs=0.01)
        assert health["analysisMessages"] <= len(metrics_collected)
        assert health["averageAnalysisIntervalMs"] == pytest.approx(4096 * 1000 / rate, rel=0.2)
        assert health["discardedFrames"] == health["inputOverflow"] == health["outputUnderflow"] == 0
        assert health["channels"] == 2
        assert statistics["scope"]["correlation"] == pytest.approx(1)
    finally:
        engine.close()


@pytest.mark.parametrize("rate", [44_100, 192_000])
def test_low_custom_target_and_low_frequency_stereo_calibration(rate):
    engine = AudioEngine({"targetFreq": 1})
    left = tone(1, rate=rate, seconds=4, amplitude=0.4)
    right = tone(1, rate=rate, seconds=4, amplitude=0.2)
    reading = engine.feed(np.column_stack((left, right)), sample_rate=rate)
    assert reading["metrics"]["locked"]
    assert reading["metrics"]["currentFreq"] == pytest.approx(1.0, abs=0.002)
    assert engine.calibrate_channels(1)["imbalanceDb"] == pytest.approx(6.02, abs=0.01)


def test_high_generator_request_has_fm_headroom_and_reports_effective_frequency():
    engine = AudioEngine()
    engine.set_generator(True, frequency=24_000, modulation_frequency=100, modulation_depth=5)
    engine.feed([], sample_rate=44_100)
    actual = engine.snapshot()["generator"]["effective_frequency"]
    assert actual * 1.05 == pytest.approx(0.49 * 44_100)
    assert engine.snapshot()["generator"]["frequency"] == 24_000
    engine.feed([], sample_rate=96_000)
    assert engine.snapshot()["generator"]["effective_frequency"] == 24_000


def test_zero_modulation_frequency_restores_the_carrier_after_a_running_sweep():
    generator = SignalGenerator()
    settings = GeneratorSettings(
        enabled=True, frequency=1000, modulation_frequency=7, modulation_depth=5
    )
    generator.render(128, 48_000, settings)
    without_fm = generator.render(48_000, 48_000, settings.updated(modulation_frequency=0))
    spectrum = np.abs(np.fft.rfft(without_fm[:, 0]))
    assert int(np.argmax(spectrum)) == 1000


def test_output_disconnection_preserves_capture(simulated_devices, monkeypatch):
    engine, streams = simulated_devices
    monkeypatch.setattr(engine_module, "DEVICE_CHECK_SECONDS", 0.01)
    engine.start(input_device=3, output_device=4, channels=2)
    try:
        capture, playback = streams
        playback.active = False
        limit = monotonic() + 0.7
        while engine.output_enabled and monotonic() < limit:
            sleep(0.01)
        assert engine.input_enabled
        assert not engine.output_enabled
        assert "disconnected" in engine.snapshot()["health"]["outputWarning"]
        left = tone(seconds=0.1).astype(np.float32)
        stereo = np.column_stack([left, left])
        capture.callback(stereo, len(left), None, SimpleNamespace(input_overflow=False))
        limit = monotonic() + 0.7
        while not engine.snapshot()["metrics"]["locked"] and monotonic() < limit:
            sleep(0.01)
        assert engine.snapshot()["metrics"]["locked"]
    finally:
        engine.stop()


def test_callback_watchdog_retains_healthy_playback(simulated_devices, monkeypatch):
    engine, streams = simulated_devices
    monkeypatch.setattr(engine_module, "DEVICE_CHECK_SECONDS", 0.01)
    monkeypatch.setattr(engine_module, "CALLBACK_TIMEOUT_SECONDS", 0.08)
    engine.start(input_device=3, output_device=4, channels=2)
    try:
        _, playback = streams
        target = np.empty((64, 2), dtype=np.float32)
        limit = monotonic() + 0.7
        while engine.input_enabled and monotonic() < limit:
            playback.callback(target, len(target), None, SimpleNamespace(output_underflow=False))
            sleep(0.01)
        assert engine.output_enabled
        assert not engine.input_enabled
        assert "stopped responding" in engine.snapshot()["health"]["inputWarning"]
        assert not engine.snapshot()["metrics"]["locked"]
    finally:
        engine.stop()


def test_loopback_uses_software_clock_while_capture_is_open(simulated_devices):
    engine, _ = simulated_devices
    engine.set_generator(True, frequency=3000)
    engine.set_loopback(True)
    engine.start(input_device=3, output_enabled=False, channels=2)
    try:
        limit = monotonic() + 0.8
        while not engine.snapshot()["metrics"]["locked"] and monotonic() < limit:
            sleep(0.01)
        snapshot = engine.snapshot()
        assert snapshot["health"]["loopbackActive"]
        assert snapshot["health"]["inputActive"]
        assert snapshot["metrics"]["locked"]
    finally:
        engine.stop()


def test_loopback_bypasses_physical_clock_correction_for_speed_and_scope(monkeypatch):
    engine = AudioEngine({'correctionFactor': 1.01, 'useAutoTarget': False})
    engine.feed(tone(seconds=.25))
    assert engine.scope_frames(.1).rate == pytest.approx(48_480)

    assert engine.snapshot()['metrics']['currentFreq'] == pytest.approx(3030, abs=.01)
    monkeypatch.setattr(engine, '_backend', lambda: pytest.fail('Internal loopback opened a device.'))
    engine.set_generator(True)
    engine.set_loopback(True)
    engine.start(input_enabled=False, output_enabled=False)
    try:
        limit = monotonic() + 1.0
        while not engine.snapshot()['metrics']['locked'] and monotonic() < limit:
            sleep(.01)
        snapshot = engine.snapshot()
        assert snapshot['metrics']['locked']
        assert snapshot['metrics']['currentFreq'] == pytest.approx(3000, abs=.01)
        assert engine.scope_frames(.1).rate == 48_000
        assert engine.processor.settings['correction_factor'] == 1.01
    finally:
        engine.close()
    engine.feed(tone(seconds=.25))
    assert engine.scope_frames(.1).rate == pytest.approx(48_480)


def test_calibrated_physical_clock_sets_elapsed_time_statistics_and_frequency_axes():
    nominal_rate, actual_rate = 48_000, 48_480
    processor = MeasurementProcessor({'correctionFactor': actual_rate / nominal_rate, 'useAutoTarget': False,
                                      'weightingStandard': 'unweighted'}, sample_rate=nominal_rate)
    seconds = 2.7
    time = np.arange(round(actual_rate * seconds)) / actual_rate
    samples = .4 * np.sin(2 * np.pi * 3000 * time - .75 * np.cos(2 * np.pi * 4 * time))
    processor.feed(samples)
    readings = processor.wow_flutter_summary()
    assert readings['timestamp'] == pytest.approx(seconds, abs=1e-9)
    assert readings['analysisRate'] == pytest.approx(actual_rate / 4096)
    assert readings['rmsOneSecond'] == pytest.approx(.1 / np.sqrt(2), abs=.001)
    assert readings['flutterPeriodHz'] == pytest.approx(4.0, abs=.04)
    spectrum = processor.snapshot()['spectrum']
    assert spectrum['peakFrequency'] == pytest.approx(3000, abs=2)
    assert np.median(processor.clock_readings()) == pytest.approx(3000 * nominal_rate / actual_rate, abs=1)

    processor.configure(correctionFactor=1.0)
    assert processor.metrics()['timestamp'] == pytest.approx(seconds)
    processor.feed(np.zeros(nominal_rate // 4))
    assert processor.wow_flutter_summary()['timestamp'] == pytest.approx(seconds + .25)

    processor.configure(correctionFactor=100)
    processor.feed(np.zeros(4096))
    assert processor.metrics()['clockUnsupported'] and not processor.metrics()['locked']

def test_channel_calibration_rejects_correlated_broadband_noise():
    engine = AudioEngine()
    noise = np.random.default_rng(5).standard_normal(48_000) * 0.16
    engine.feed(np.column_stack((noise, noise * 0.5)))
    with pytest.raises(RuntimeError, match="steady tone"):
        engine.calibrate_channels(1000)


def test_calibration_uses_several_live_windows_and_rejects_changing_balance():
    engine = AudioEngine({"channels": 2})
    sample_count = 9 * 8192
    timeline = np.arange(sample_count, dtype=np.float64) / 48_000
    left = 0.4 * np.sin(2 * np.pi * 1000 * timeline)
    right = left * np.where(timeline < 0.6, 0.5, 0.8)
    stereo = np.column_stack([left, right])
    entered = engine._state

    def run_calibration():
        with entered:
            return engine.calibrate_channels(1000, duration=1.1)

    with ThreadPoolExecutor(1) as executor:
        result = executor.submit(run_calibration)
        for chunk in np.array_split(stereo, 18):
            sleep(0.011)
            engine.feed(chunk)
        with pytest.raises(RuntimeError, match="balance changed"):
            result.result(timeout=3)


def test_clock_calibration_retains_sixty_seconds_at_high_rates():
    rate = 192_000
    engine = AudioEngine()
    engine.feed([], sample_rate=rate)
    seconds = np.arange(rate) / rate
    started = Event()

    def calibrate():
        with engine._state:
            started.set()
            return engine.calibrate_clock(3000, duration=60)

    with ThreadPoolExecutor(1) as executor:
        result = executor.submit(calibrate)
        assert started.wait(1)
        for elapsed in range(61):
            frequency = 2998.0 if elapsed < 20 else 2999.5
            engine.feed(0.5 * np.sin(2 * np.pi * frequency * (elapsed + seconds)))
    assert result.result() == pytest.approx(3000 / 2999.5, abs=1e-5)
