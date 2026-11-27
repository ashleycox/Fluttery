"""Known-signal regression checks for the secondary audio instruments."""

from math import ceil, pi, sqrt

import numpy as np
import pytest

from fluttery.audio import AudioEngine, MeasurementProcessor
from fluttery.audio.generator import GeneratorSettings, SignalGenerator
from fluttery.audio.spectrum import distortion, power_spectrum


@pytest.mark.parametrize("sample_rate", (44_100, 48_000, 96_000, 192_000))
def test_distortion_separates_harmonics_and_close_in_interference(sample_rate):
    # Match the full-band capture span used by the streaming analyzer. A
    # fractional FFT bin exposes leakage from even a perfectly clean tone.
    count = max(8192, ceil(8192 * sample_rate / 48_000))
    time = np.arange(count) / sample_rate
    frequency = 1000.37
    fundamental = 0.4 * np.sin(2 * pi * frequency * time)
    second = 0.004 * np.sin(4 * pi * frequency * time + 0.5)
    close_to_second = 0.002 * np.sin(2 * pi * (2 * frequency + 20) * time + 0.5)
    close_to_fundamental = 0.002 * np.sin(2 * pi * (frequency + 9) * time + 0.5)

    def analyze(values):
        samples = values.astype(np.float32)
        return distortion(samples, power_spectrum(samples, sample_rate), sample_rate)

    clean = analyze(fundamental)
    assert clean["available"]
    assert clean["fundamentalFrequency"] == pytest.approx(frequency, abs=0.0001)
    assert clean["thdn"] < 0.0001

    mixed = analyze(fundamental + second + close_to_second)
    assert mixed["thd"] == pytest.approx(1.0, abs=0.001)
    assert mixed["harmonicDbc"][:2] == pytest.approx((0, -40), abs=0.01)
    assert mixed["noisePercent"] == pytest.approx(0.5, abs=0.001)
    assert mixed["thdn"] == pytest.approx(sqrt(1.25), abs=0.001)

    near = analyze(fundamental + close_to_fundamental)
    # Some energy is inseparable from a fitted tone at this finite capture
    # length. A broad bin notch previously lost nearly all of the component.
    assert near["noisePercent"] > 0.39
    assert near["thd"] < 0.002


def test_low_band_edge_distortion_ignores_input_offset():
    sample_rate = 48_000
    time = np.arange(8192) / sample_rate
    samples = 0.2 + 0.4 * np.sin(2 * pi * 20 * time + 0.25)
    reading = MeasurementProcessor(sample_rate=sample_rate)
    reading.feed(samples)
    result = reading.snapshot()["thd"]
    assert result["available"]
    assert result["fundamentalFrequency"] == pytest.approx(20, abs=0.0001)
    assert result["thdn"] < 0.0001


def test_long_distortion_capture_fits_all_orders_with_bounded_working_memory(monkeypatch):
    from fluttery.audio import spectrum

    original = spectrum._harmonic_basis
    blocks = []

    def harmonic_basis(phase, count):
        blocks.append(len(phase))
        return original(phase, count)

    monkeypatch.setattr(spectrum, "_harmonic_basis", harmonic_basis)
    rate, frequency = 192_000, 723.37
    phase = 2 * pi * frequency * np.arange(rate + 43) / rate
    samples = 0.25 * np.sin(phase) + 0.001 * np.cos(3 * phase) + 0.0002 * np.sin(24 * phase)
    result = distortion(samples, power_spectrum(samples, rate), rate, harmonic_count=24)
    assert result["harmonicsMeasured"] == 23
    assert result["fundamentalFrequency"] == pytest.approx(frequency, abs=0.0001)
    assert result["thd"] == pytest.approx(100 * sqrt(.001 ** 2 + .0002 ** 2) / .25, abs=1e-5)
    assert result["hd3"] == pytest.approx(.4, abs=1e-5)
    assert result["noisePercent"] < 1e-5
    assert len(blocks) > 2 and max(blocks) < rate // 4


@pytest.mark.parametrize("sample_rate", (48_000, 96_000))
def test_live_crosstalk_ignores_a_nearby_tone_on_the_quiet_channel(sample_rate):
    time = np.arange(sample_rate // 3) / sample_rate
    frequency = 3150.37
    reference = 0.35 * np.sin(2 * pi * frequency * time)
    interference = 0.0035 * np.sin(2 * pi * (frequency + 20) * time + 0.5)
    capture = np.column_stack((reference, 0.001 * reference + interference))
    engine = AudioEngine({"targetFreq": 3150, "useAutoTarget": False, "channels": 2})
    reading = engine.feed(capture, sample_rate=sample_rate)["stereo"]
    assert reading["available"]
    assert reading["toneFrequency"] == pytest.approx(frequency, abs=0.05)
    assert reading["crosstalkDb"] == pytest.approx(-60, abs=0.15)


def test_scope_phase_correlation_uses_ac_components():
    tone = 0.1 * np.sin(2 * pi * 1000 * np.arange(8192) / 48_000)
    engine = AudioEngine({"channels": 2})
    reading = engine.feed(np.column_stack((0.25 + tone, 0.25 - tone)))["scope"]
    assert reading["correlation"] == pytest.approx(-1, abs=1e-6)
    engine.reset()
    assert engine.feed(np.full((8192, 2), 0.25))["scope"]["correlation"] == 0


def test_resonance_measures_the_entire_peak_at_the_band_edge():
    time = np.arange(4 * 48_000) / 48_000
    expected_level = 20 * np.log10(0.02 / sqrt(2))
    processor = MeasurementProcessor()
    processor.feed(0.02 * np.sin(2 * pi * 29.85 * time))
    resonance = processor.snapshot()["resonance"]
    assert resonance["frequency"] == pytest.approx(29.85, abs=0.005)
    assert resonance["levelDb"] == pytest.approx(expected_level, abs=0.005)

    processor.reset()
    processor.feed(0.02 * np.sin(2 * pi * 30.6 * time) + 0.003 * np.sin(2 * pi * 10 * time))
    assert processor.snapshot()["resonance"]["frequency"] == pytest.approx(10, abs=0.005)


def test_noise_floor_excludes_the_converters_dc_offset():
    sample_rate = 48_000
    time = np.arange(sample_rate) / sample_rate
    frequency = 170 * sample_rate / 8192
    signal = 0.2 + 0.002 * np.sin(2 * pi * frequency * time)
    processor = MeasurementProcessor()
    processor.feed(signal)
    assert processor.snapshot()["noise"]["rmsDb"] == pytest.approx(-56.9897, abs=0.001)


def test_triangle_excludes_harmonics_above_nyquist_without_folding():
    sample_rate, frequency = 48_000, 10_000
    settings = GeneratorSettings(True, frequency, "triangle", 0.5)
    generator = SignalGenerator()
    generator.amplitude = settings.level
    signal = generator.render(sample_rate, sample_rate, settings)[:, 0]
    spectrum = 2 * np.abs(np.fft.rfft(signal)) / sample_rate
    assert spectrum[frequency] == pytest.approx(settings.level * 8 / pi ** 2, abs=1e-7)
    # The first omitted harmonic would fold from 30 kHz back to 18 kHz.
    assert spectrum[18_000] < 1e-6 * spectrum[frequency]


@pytest.mark.parametrize(("sample_rate", "frequency"), ((44_100, 10_000), (48_000, 10_000), (96_000, 19_000)))
@pytest.mark.parametrize(("waveform", "second_harmonic"), (("square", 0), ("sawtooth", 0.5)))
def test_high_square_and_sawtooth_keep_only_the_expected_harmonics(
    sample_rate, frequency, waveform, second_harmonic
):
    settings = GeneratorSettings(True, frequency, waveform, 0.5)
    generator = SignalGenerator()
    generator.amplitude = settings.level
    signal = generator.render(sample_rate, sample_rate, settings)[:, 0]
    spectrum = np.abs(np.fft.rfft(signal))
    assert np.max(np.abs(signal)) == pytest.approx(settings.level, abs=0.001)
    assert spectrum[2 * frequency] / spectrum[frequency] == pytest.approx(second_harmonic, abs=1e-7)
    # With the first two harmonic positions removed, all the remaining bins
    # should be quiet, including the third harmonic folded back from Nyquist.
    tolerance = 1e-6 * spectrum[frequency]
    spectrum[[frequency, 2 * frequency]] = 0
    assert np.max(spectrum) < tolerance


def test_frequency_modulator_matches_the_continuous_time_phase_integral():
    sample_rate, frequency, modulation = 8_000, 2500, 100
    depth, level = 0.1, 0.35
    settings = GeneratorSettings(True, frequency, "sine", level, modulation, depth * 100)
    generator = SignalGenerator()
    generator.amplitude = level
    generated = np.concatenate([generator.render(n, sample_rate, settings) for n in (127, 321, 552)])
    time = np.arange(len(generated)) / sample_rate
    integral = depth * (1 - np.cos(2 * pi * modulation * time)) / (2 * pi * modulation)
    expected = level * np.sin(2 * pi * frequency * (time + integral))
    np.testing.assert_allclose(generated[:, 0], expected, atol=1e-7)
