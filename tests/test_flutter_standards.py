"""Reference signals and flow regressions for statistical speed measurements."""

import json
import wave
from math import sin, sqrt

import numpy as np
import pytest
from scipy import signal

from fluttery.audio import AudioEngine, MeasurementProcessor
from fluttery.audio.flutter_detection import (
    RollingMaxima,
    SlidingRms,
    drift_band_rms,
    reference_weighting,
    repeating_frequency,
)


def modulated_carrier(carrier, modulation, duration, *, rate=48000, offset_percent=0, depth_percent=0.1):
    time = np.arange(round(duration * rate)) / rate
    phase_modulation = carrier * depth_percent / (100 * modulation)
    phase = 2 * np.pi * carrier * (1 + offset_percent / 100) * time
    return 0.4 * np.sin(phase - phase_modulation * np.cos(2 * np.pi * modulation * time))


@pytest.mark.parametrize("rate", [8000, 44100, 48000, 96000, 192000, 384000])
def test_reference_weighting_follows_published_curve(rate):
    frequencies = [0.2, 0.315, 0.4, 0.63, 0.8, 1, 1.6, 2, 4, 6.3, 10, 20, 40, 63, 100, 200]
    expected = [-30.6, -19.7, -15, -8.4, -6, -4.2, -1.8, -0.9, 0, -0.9, -2.1, -5.9, -10.4, -14.2, -17.3, -23]
    _, values = signal.sosfreqz(reference_weighting(rate), worN=frequencies, fs=rate)
    assert 20 * np.log10(np.abs(values)) == pytest.approx(expected, abs=0.45)
    assert abs(values[8]) == pytest.approx(1, abs=1e-12)


@pytest.mark.parametrize(("modulation", "reference_gain"), [(0.8, 0.50), (4, 1.0), (20, 0.508), (200, 0.071)])
def test_aes_statistical_peak_matches_known_carrier(modulation, reference_gain):
    processor = MeasurementProcessor({"weightingStandard": "aes6", "targetFreq": 3000})
    samples = modulated_carrier(3150, modulation, 9.17)
    processor.feed(samples[:4 * 48000])
    assert processor.metrics()["aes6Peak"] is None
    for part in np.array_split(samples[4 * 48000:], 41):
        processor.feed(part)
    completed = processor.wow_flutter_summary()
    assert completed["activeTarget"] == 3150
    assert completed["aes6Seconds"] == 5
    assert completed["aes6Peak"] == pytest.approx(0.1 * reference_gain * sin(0.95 * np.pi / 2), rel=0.045)
    if modulation >= 0.8:
        assert completed["flutterPeriodHz"] == pytest.approx(modulation, rel=0.01)
    assert completed["timestamp"] == pytest.approx(9.17, abs=1 / 48000)
    processor.feed(np.zeros(48000))
    assert processor.metrics()["aes6Peak"] is None
    assert processor.metrics()["aes6Seconds"] == 0


def test_unweighted_capture_preserves_signed_offset_and_is_independent_of_display_weighting():
    data = modulated_carrier(3000, 20, 1.32, offset_percent=-0.24)
    raw = []
    for standard in ("iec", "unweighted"):
        processor = MeasurementProcessor({"weightingStandard": standard, "useAutoTarget": False})
        processor.feed(data)
        saved = processor.last_unweighted_speed
        mask = processor.last_speed_valid
        processor.feed(np.zeros(11000))
        assert saved.shape == mask.shape == data.shape
        assert not saved.flags.writeable
        assert not mask.flags.writeable
        assert not mask[0] and mask[-1]
        assert np.mean(saved[-24000:]) == pytest.approx(-0.24, abs=0.001)
        assert np.std(saved[-24000:]) == pytest.approx(0.1 / sqrt(2), abs=0.001)
        raw.append(saved)
    assert np.array_equal(*raw)


def test_aes_peak_uses_average_speed_and_relocks_without_a_constant_offset_transient():
    processor = MeasurementProcessor({"weightingStandard": "aes6"})
    data = modulated_carrier(3150, 4, 6, offset_percent=2.0, depth_percent=0)
    processor.feed(data)
    first = processor.wow_flutter_summary()
    assert first["aes6Peak"] < 0.0001
    assert first["speedDrift"] == pytest.approx(2.0, abs=0.01)
    processor.feed(np.zeros(48000))
    data = modulated_carrier(3150, 4, 7, offset_percent=2.0)
    processor.feed(data)
    result = processor.wow_flutter_summary()
    assert result["aes6Peak"] == pytest.approx(0.1 * sin(0.95 * np.pi / 2) / 1.02, abs=0.001)


def test_timed_rms_uses_exact_window_and_rolling_maxima_expire():
    stats = SlidingRms(100, 1)
    old = np.arange(100, dtype=float)[:, np.newaxis]
    stats.add(old[:29])
    assert stats.values()[0] is None
    stats.add(old[29:])
    assert stats.values()[0] == pytest.approx(np.sqrt(np.mean(old**2)))
    stats.add(np.zeros((30, 1)))
    assert stats.values()[0] == pytest.approx(np.sqrt(np.sum(old[30:]**2) / 100))
    stats.add(np.zeros((200, 1)))
    assert stats.values()[0] == pytest.approx(0)

    maxima = RollingMaxima()
    assert maxima.add(0, 1, 2) == (1, 2)
    assert maxima.add(5, 0.03, 0.30) == (1, 2)
    assert maxima.add(10, 0.11, 0.10) == (0.11, 0.30)
    assert maxima.add(15, None, None) == (0.11, 0.10)
    assert maxima.add(20, None, None) == (None, None)


def test_live_one_second_choice_and_new_observation_reset():
    processor = MeasurementProcessor({"rmsDetector": "oneSecond", "weightingStandard": "unweighted"})
    data = modulated_carrier(3000, 4, 2)
    processor.feed(data)
    metrics = processor.metrics()
    assert metrics["rmsOneSecond"] == pytest.approx(0.1 / sqrt(2), abs=0.001)
    assert metrics["rmsSeconds"] == 1
    assert metrics["rms"] == metrics["rmsOneSecond"]
    assert metrics["rollingRmsMax"] >= metrics["rmsOneSecond"]
    processor.reset_statistics()
    assert processor.metrics()["rollingRmsMax"] is None
    assert processor.metrics()["rmsSeconds"] == 0
    assert processor.drain_measurements()[-1]["rmsSeconds"] == 1
    processor.feed(data[:48000 // 2])
    assert processor.metrics()["rmsOneSecond"] is None
    assert 0.4 < processor.metrics()["rmsSeconds"] < 0.6


def test_very_slow_drift_is_separate_from_speed_error_and_wow_meter():
    processor = MeasurementProcessor({"weightingStandard": "unweighted", "useAutoTarget": False})
    data = modulated_carrier(3000, 0.14, 29, offset_percent=0.28)
    processor.feed(data[:48000 * 19])
    assert processor.metrics()["driftRms"] is None
    processor.feed(data[48000 * 19:])
    result = processor.wow_flutter_summary()
    assert result["driftRms"] == pytest.approx(0.1 / sqrt(2), abs=0.005)
    assert abs(result["driftRms"]) > abs(result["rmsOneSecond"])
    assert result["speedDrift"] > 0.17


@pytest.mark.parametrize(('frequency', 'in_band'), [(.04, False), (.05, True), (.50, True), (.51, False)])
def test_drift_band_resolves_spectral_leakage_at_both_cutoffs(frequency, in_band):
    rate = 1024
    t = np.arange(22 * rate) / rate
    for phase in (0, .7, 1.57):
        samples = .5 + .02 * t + .6 * np.cos(2 * np.pi * 4 * t) + .1 * np.sin(2 * np.pi * frequency * t + phase)
        measured = drift_band_rms(samples, rate)
        assert measured == pytest.approx(.1 / sqrt(2) if in_band else 0, abs=.0005)


def test_repeating_period_finds_a_resolved_fundamental_and_rejects_a_chirp():
    rate = 1024
    t = np.arange(4 * rate) / rate
    recurring = .03 * np.sin(2 * np.pi * 3.2 * t) + .12 * np.sin(2 * np.pi * 6.4 * t + .2)
    frequency, period = repeating_frequency(recurring, rate, .2, 200)
    assert frequency == pytest.approx(3.2, abs=.01)
    assert period == pytest.approx(1 / 3.2, abs=.001)
    chirp = .12 * np.sin(2 * np.pi * (2 * t + 1.4 * t ** 2))
    assert repeating_frequency(chirp, rate, .2, 200) == (None, None)


def test_invalid_clipped_fm_cannot_seed_a_false_peak_after_clean_relock():
    rate, block = 48_000, 4096
    p = MeasurementProcessor({'useAutoTarget': False, 'weightingStandard': 'unweighted', 'useDCBlocker': True})
    previous = modulated_carrier(3000, 4, 12 * block / rate, depth_percent=2)
    p.feed(np.clip(3.5 * previous, -1, 1))
    rejected = p.drain_measurements()[-1]
    assert rejected['clipped'] and not rejected['locked'] and rejected['bufferPeak'] == 0
    p.feed(.4 * np.sin(2 * np.pi * 3000 * np.arange(4 * block) / rate + .9))
    settled = [row for row in p.drain_measurements() if row['locked'] and not row['invalidFrames']]
    assert settled and max(row['peak'] for row in settled) < .002


def test_complete_measurement_queue_keeps_all_rows_without_crossing_a_clock_reset(monkeypatch):
    from fluttery.audio import processor

    monkeypatch.setattr(processor, 'ANALYSIS_QUEUE_LIMIT', 2)
    p = MeasurementProcessor()
    p.feed(np.zeros(3 * processor.ANALYSIS_FRAMES))
    assert len(p.drain_measurements()) == 2
    p.retain_measurements(True)
    p.feed(np.zeros(processor.ANALYSIS_FRAMES))
    p.reset()
    p.feed(np.zeros(7 * processor.ANALYSIS_FRAMES))
    assert len(p.drain_measurements()) == 7
    p.retain_measurements(False)
    p.feed(np.zeros(5 * processor.ANALYSIS_FRAMES))
    assert len(p.drain_measurements()) == 2


@pytest.mark.parametrize('unusable', ['clipped', 'ambiguous', 'awaiting reference confirmation'])
def test_original_speed_wav_marks_complete_unusable_blocks_and_keeps_the_tail(unusable, tmp_path):
    rate, length = 48_000, 4096
    device = AudioEngine()
    device.start(input_enabled=False, output_enabled=False)
    try:
        def tone(frequency, frames, level=.4, start=0):
            time = (start + np.arange(frames)) / rate
            return np.clip(level * np.sin(2 * np.pi * frequency * time), -1, 1)

        warmup = 4 * length + 257
        device.feed(tone(3003, warmup))
        target = tmp_path / 'speed.wav'
        capture = device.begin_raw_speed_capture(target)
        first = length - 257
        device.feed(tone(3003, first, start=warmup))
        bad = tone(3003, length, level=1.3) if unusable == 'clipped' else tone(
            3075 if unusable == 'ambiguous' else 3150, length,
        )
        device.feed(bad[:1913])
        device.feed(bad[1913:])
        tail = 3 * length + 975
        device.feed(tone(3003, tail))
        device.finish_raw_speed_capture(capture)
        assert capture.done.wait(5) and capture.saved, capture.error
    finally:
        device.stop()

    with wave.open(str(target), 'rb') as source:
        assert source.getnframes() == first + length + tail
        pcm = np.frombuffer(source.readframes(source.getnframes()), dtype='<i4')
    assert np.count_nonzero(pcm[first:first + length]) == 0
    assert np.count_nonzero(pcm[-975:]) == 975
    payload = target.read_bytes().split(b'flty', maxsplit=1)[1]
    gaps = json.loads(payload[4:].rstrip(b'\0'))['invalidFrames']
    assert any(start <= first and stop >= first + length for start, stop in gaps)


@pytest.mark.parametrize("rate", [44100, 48000, 96000])
def test_high_band_scrape_carrier_and_diagnostic_spectrum(rate):
    processor = MeasurementProcessor({"flutterBand": "scrape"}, sample_rate=rate)
    rotor = modulated_carrier(12500, 1000, 2.6, rate=rate)
    processor.feed(rotor)
    result = processor.wow_flutter_summary()
    assert result["activeTarget"] == 12500 and result["locked"]
    assert result["scrapeRms"] == pytest.approx(0.1 / sqrt(2), abs=0.001)
    assert result["flutterPeriodHz"] == pytest.approx(1000, abs=1)
    view = processor.snapshot()["flutter_spectrum"]
    assert view["peakFrequency"] == pytest.approx(1000, abs=1)
    assert view["frequencies"][-1] > 4999
    snapshot = processor.snapshot()["scope"]
    assert snapshot["modulationSampleRate"] >= 12000


def test_scrape_refuses_insufficient_sample_rate_and_rejects_slow_variation():
    processor = MeasurementProcessor({"flutterBand": "scrape"}, sample_rate=32000)
    processor.feed(modulated_carrier(12500, 1000, 0.7, rate=32000))
    assert processor.metrics()["scrapeUnsupported"]
    assert processor.metrics()["scrapeRms"] is None
    processor.feed(modulated_carrier(12500, 80, 2.5), 48000)
    assert not processor.metrics()["scrapeUnsupported"]
    assert processor.metrics()["scrapeRms"] < 0.002


@pytest.mark.parametrize(('band', 'carrier', 'modulation', 'rejection_hz'), [
    ('rotational', 3000, 4, 20), ('scrape', 12500, 1200, 100),
])
def test_flutter_spectrum_and_scope_reacquire_after_unlocked_input(band, carrier, modulation, rejection_hz):
    p = MeasurementProcessor({'flutterBand': band, 'useAutoTarget': False})
    test_tone = modulated_carrier(carrier, modulation, 3)
    p.feed(test_tone)
    initial = p.snapshot()
    before = initial['flutter_spectrum']
    assert before['peakFrequency'] == pytest.approx(modulation, abs=.15)
    before_scope = p.scope_frames(.050, modulation=True)
    assert np.isfinite(before_scope.samples).all()

    p.feed(np.zeros(2 * 48_000))
    invalid = p.snapshot()
    assert not invalid['metrics']['locked'] and not len(invalid['flutter_spectrum']['db'])
    gap = p.scope_frames(.050, modulation=True)
    assert gap.epoch is not before_scope.epoch and np.isnan(gap.samples).all()

    p.feed(test_tone[:12_000])
    relocked = p.snapshot()
    assert relocked['metrics']['locked'] and not len(relocked['flutter_spectrum']['db'])
    p.feed(test_tone[12_000:])
    after = p.snapshot()['flutter_spectrum']
    assert after['peakFrequency'] == pytest.approx(modulation, abs=.15)
    assert after['peakDb'] == pytest.approx(before['peakDb'], abs=.5)
    outside = np.abs(after['frequencies'] - modulation) > rejection_hz
    assert max(after['db'][outside]) < after['peakDb'] - 55
    assert p.snapshot()['spectrum']['peakFrequency'] == pytest.approx(initial['spectrum']['peakFrequency'], abs=.001)
