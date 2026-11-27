"""Known signals for the bench level, monitor and WAV workflows."""

import wave
from struct import pack_into

import numpy as np
import pytest
from scipy import signal
from scipy.io import wavfile

from fluttery.audio.engine import AudioEngine
from fluttery.audio.generator import NoiseSource, monitor_route
from fluttery.audio.levels import (
    a_weighting_power,
    ac_levels,
    band_rms,
    calibration_factor,
    measure_noise,
    noise_levels,
    voltage_reference,
)
from fluttery.audio.recordings import (
    RawSpeedCapture,
    TapeEndDetector,
    _plot_samples,
    analyse_wav,
    inspect_wav,
)
from fluttery.audio.spectrum import distortion, power_spectrum
from fluttery.preferences import normalize_settings

RATE = 48000


def sine(hz, seconds=1):
    return np.sin(2 * np.pi * hz * np.arange(round(seconds * RATE)) / RATE)


def test_voltage_a_weighting_and_third_harmonic():
    frames = np.column_stack((.5 * sine(1000), .25 * sine(100)))
    rms, _, peak = ac_levels(frames + .01, RATE)
    np.testing.assert_allclose(rms, [.5 / np.sqrt(2), .25 / np.sqrt(2)], rtol=3e-5)
    assert calibration_factor(.75, rms[0], peak[0]) == pytest.approx(1.5 * np.sqrt(2), rel=3e-5)
    with pytest.raises(ValueError, match='clipping'):
        calibration_factor(1, rms[0], 1.0)
    flat, weighted = noise_levels(frames, RATE)[1]
    assert 20 * np.log10(weighted / flat) == pytest.approx(-19.1428, abs=.02)
    assert a_weighting_power([1000])[0] == 1
    test = .4 * sine(1000, .2) + .004 * sine(3000, .2) + .008 * sine(2000, .2)
    assert distortion(test, power_spectrum(test, RATE), RATE)['hd3'] == pytest.approx(1, rel=.005)


def test_bench_options_store_each_instrument_and_keep_named_bands_consistent():
    options = normalize_settings({'benchLevelOptions': {
        'voltage': {'band': 'low', 'window': .5, 'lowHz': 100, 'highHz': 1, 'units': 'millivolts'},
        'snr': {'band': 'custom', 'lowHz': 80, 'highHz': 4500, 'window': 'invalid', 'units': 'volts'},
        'calibration': {'band': 'custom', 'lowHz': 200, 'highHz': 160},
    }})['benchLevelOptions']
    assert options['voltage'] == {'band': 'low', 'window': .5, 'lowHz': 20, 'highHz': 300, 'units': 'millivolts'}
    assert options['snr'] == {'band': 'custom', 'window': 2, 'lowHz': 80, 'highHz': 4500, 'units': 'auto'}
    assert options['calibration']['lowHz'] == 20


def test_noise_spectrum_keeps_the_shape_and_level_of_a_weighted_measurement():
    frame = .001 * sine(100, 2)
    reading, = measure_noise(frame, RATE, 20, 20000)
    main_band = np.argmax(reading.density_db)
    assert reading.frequency[main_band] == pytest.approx(100, rel=.035)
    assert reading.weighted_density_db[main_band] - reading.density_db[main_band] == pytest.approx(-19.14, abs=.2)
    assert len(reading.frequency) < 150
    assert np.all(np.diff(reading.frequency) > 0)
    assert reading.rms == pytest.approx(.001 / np.sqrt(2), rel=.001)


@pytest.mark.parametrize('rate', [44100, 48000, 96000])
def test_voltage_reference_is_unbiased_at_the_meter_cutoff(rate):
    frequency = 3399.72
    expected_rms = .31 / np.sqrt(2)
    time = np.arange(round(.317 * rate)) / rate
    frame = .08 + .31 * np.cos(2 * np.pi * frequency * time + .7)
    measured, detected = voltage_reference(frame, rate, 300, 3400)
    assert detected == pytest.approx(frequency, abs=.002)
    assert measured == pytest.approx(expected_rms, rel=1e-5)
    assert ac_levels(frame, rate, 300, 3400)[0][0] < .73 * expected_rms  # The meter intentionally applies its filter.
    with pytest.raises(ValueError, match='steady sine'):
        voltage_reference(frame + .03 * np.sin(2 * np.pi * 4770 * time), rate, 300, 3400)


def test_snr_uses_identical_band_integration_for_reference_and_noise():
    at_cutoff = .22 * sine(300, 1.25)
    adjacent_band = at_cutoff + .13 * sine(3000, 1.25)
    stereo = np.column_stack((at_cutoff, adjacent_band))
    band = (300.23, 8000)
    actual = band_rms(stereo, RATE, *band)
    floor = measure_noise(stereo * 0.001, RATE, *band)
    ratios = 20 * np.log10(actual / [reading.rms for reading in floor])
    np.testing.assert_allclose(ratios, [60., 60.], atol=1e-9)
    # Changing the cutoff within one bin changes the contribution smoothly.
    assert band_rms(at_cutoff, RATE, 300.01, 8000)[0] > actual[0] > band_rms(at_cutoff, RATE, 300.41, 8000)[0]
    with pytest.raises(ValueError, match='finite audio'):
        measure_noise(at_cutoff * np.nan, RATE, *band)


def test_ac_meter_preserves_peaks_at_the_start_of_the_observation():
    preroll = np.zeros(RATE)
    observed = np.zeros(RATE)
    # This burst would have fallen entirely inside the discarded filter settling interval.
    observed[200:1400] = .5 * sine(1000, .025)
    _, peak, raw_peak = ac_levels(np.r_[preroll, observed], RATE, observation_frames=RATE)
    assert peak[0] > .45 and raw_peak[0] == pytest.approx(.5)
    _, _, raw_peak = ac_levels(np.r_[np.ones(RATE) * .99, observed], RATE, observation_frames=RATE)
    assert raw_peak[0] == pytest.approx(.5)


@pytest.mark.parametrize(('waveform', 'expected'), [('white', 3.01), ('pink', 0)])
def test_noise_shape_and_state(waveform, expected):
    frames = 240000
    source = NoiseSource(117)
    split = np.concatenate([source.render(1200, RATE, waveform) for _ in range(frames // 1200)])
    single = NoiseSource(117).render(frames, RATE, waveform)
    np.testing.assert_allclose(split, single, atol=2e-12)
    frequency, density = signal.welch(single, RATE, nperseg=16384)
    bands = np.array([density[(frequency >= low) & (frequency < low * 2)].sum()
                      for low in (250, 500, 1000, 2000, 4000)])
    assert np.mean(np.diff(10 * np.log10(bands))) == pytest.approx(expected, abs=.3)
    assert np.max(np.abs(split)) <= 1


def test_difference_monitor_and_nonconsuming_recent_capture():
    samples = np.array([[.2, -.2], [-.4, -.4]], dtype=np.float32)
    np.testing.assert_array_equal(monitor_route(samples, None, 'difference'), [[samples[0, 0]] * 2, [0, 0]])
    np.testing.assert_array_equal(monitor_route(samples[:, :1], None, 'difference'), 0)
    audio = AudioEngine()
    first = audio.recent_pcm(1)
    audio.feed(samples, RATE)
    second, third = audio.recent_pcm(1), audio.recent_pcm(1)
    assert first[2] < second[2] == third[2]
    np.testing.assert_array_equal(second[1], samples)
    np.testing.assert_array_equal(third[1], samples)
    _, _, last_frame, epoch = audio.input_observation(1)
    audio.reset()
    assert audio.input_observation(1)[3] is None
    audio.feed(samples, RATE)
    _, _, next_frame, next_epoch = audio.input_observation(1)
    assert next_frame > last_frame and next_epoch is not epoch


def test_streaming_wav_analysis_uses_selected_channel_interval_and_actual_rate(tmp_path):
    path = tmp_path / 'test.wav'
    t = np.arange(RATE * 8) / RATE
    carrier = .2 * np.sin(2 * np.pi * 3150 * t - (3.15 / 4) * np.cos(2 * np.pi * 4 * t))
    wavfile.write(path, RATE, np.column_stack((np.zeros(len(t)), carrier)).astype(np.float32))
    assert inspect_wav(path).duration == 8
    result = analyse_wav(path, {'weightingStandard': 'aes6'}, channel=1, start=.5, duration=6.5)
    assert result['channel'] == 2 and result['duration'] == 6.5 and result['start'] == .5
    assert result['metrics']['aes6Peak'] == pytest.approx(.0997, abs=.003)
    assert result['metrics']['flutterPeriodHz'] == pytest.approx(4, abs=.08)
    with pytest.raises(ValueError, match='No stable reference'):
        analyse_wav(path, {}, channel=0, duration=.25)
    assert analyse_wav(path, {}, cancelled=lambda: True) is None
    completion = []
    assert analyse_wav(path, {}, channel=1, duration=.5, cancelled=lambda: 1 in completion,
                       progress=completion.append) is None
    with pytest.raises(ValueError, match='finite'):
        analyse_wav(path, {}, start=float('nan'))
    wavfile.write(path, RATE, np.concatenate((carrier, np.zeros(RATE))).astype(np.float32))
    with_trailer = analyse_wav(path, {'weightingStandard': 'aes6'})
    assert with_trailer['metrics']['aes6Peak'] == pytest.approx(.0997, abs=.003)
    assert with_trailer['metrics']['currentFreq'] == pytest.approx(3150, abs=5)
    assert not with_trailer['history'][-1]['locked']
    corrected = analyse_wav(path, {'correctionFactor': 1.01}, duration=1.5)
    assert corrected['duration'] == pytest.approx(1.5 / 1.01)
    assert corrected['history'][-1]['timestamp'] == pytest.approx(corrected['duration'])


def test_wav_inspection_honours_riff_boundary_and_legacy_unpadded_pcm(tmp_path):
    path = tmp_path / 'original.wav'
    with wave.open(str(path), 'wb') as writer:
        writer.setparams((1, 3, 48000, 1, 'NONE', 'not compressed'))
        writer.writeframes(b'\x01\x02\x03')
    data = bytearray(path.read_bytes())
    assert inspect_wav(path).frames == 1
    path.write_bytes(data + b'Metadata outside RIFF')
    assert inspect_wav(path).frames == 1
    pack_into('<I', data, 4, len(data) - 8 + 20)
    path.write_bytes(data)
    with pytest.raises(ValueError, match='incomplete RIFF'):
        inspect_wav(path)
    pack_into('<I', data, 4, 28)  # The audio data chunk is only in the trailing bytes.
    path.write_bytes(data)
    with pytest.raises(ValueError, match='format and an audio data chunk'):
        inspect_wav(path)


def test_speed_pcm_has_explicit_scale_and_replaces_only_on_success(tmp_path):
    path = tmp_path / 'speed.wav'
    capture = RawSpeedCapture(path, 48000)
    capture.append(np.array([0., 1., -2., 3.]), np.array([True, True, True, False]))
    capture.finish()
    assert capture.done.wait(2) and capture.saved
    with wave.open(str(path), 'rb') as stream:
        assert stream.getframerate() == 48000 and stream.getsampwidth() == 4
        pcm = np.frombuffer(stream.readframes(4), dtype='<i4')
    np.testing.assert_allclose(pcm / 2 ** 31 * 100, [0, 1, -2, 0], atol=1e-7)
    original = path.read_bytes()
    rejected = RawSpeedCapture(path, 48000)
    rejected.finish(cancel=True)
    assert rejected.done.wait(2) and not rejected.saved and path.read_bytes() == original


def test_long_recording_plot_retains_detector_extremes_and_lock_gaps():
    rows = [{'sequence': index, 'locked': index not in (3, 8), 'flutterBand': 'rotational', 'weighting': 'aes6',
             'aes6Peak': .4 if index == 5 else .09, 'rms': .2 if index == 2 else .06,
             'speedDrift': {4: .2, 7: -.3}.get(index, 0.0)} for index in range(10)]
    assert [row['sequence'] for row in _plot_samples(rows)] == [0, 2, 3, 4, 5, 7, 8, 9]


def test_tape_stop_requires_an_established_test_and_sustained_quiet_loss():
    end = TapeEndDetector(4, -45)
    assert not end.observe(0, False, -120)
    assert not end.observe(10, False, -120)
    assert not end.observe(11, True, -20)
    assert not end.observe(14, True, -20)
    assert not end.observe(15, False, -120)
    assert not end.observe(18, True, -20)
    assert not end.observe(20, False, -25)
    assert not end.observe(21, False, -60)
    assert not end.observe(24.99, False, -60)
    assert end.observe(25, False, -60)


def test_tape_stop_treats_a_fixed_converter_offset_as_silence():
    engine = AudioEngine({'useAutoTarget': False})
    time = np.arange(RATE * 3) / RATE
    engine.feed(.32 * np.sin(2 * np.pi * 3000 * time))
    detector = TapeEndDetector(.3, -45)
    for row in engine.drain_measurements():
        assert not detector.observe(row['timestamp'], row['locked'], row['inputAcLevel'])
    assert detector.armed
    engine.feed(np.full(RATE, .12))
    assert any(detector.observe(row['timestamp'], row['locked'], row['inputAcLevel'])
               for row in engine.drain_measurements())
