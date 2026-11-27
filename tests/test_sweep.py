"""Exercise mathematical sweep landmarks and the streaming WAV path."""

import math
import wave
from dataclasses import replace
from struct import unpack_from

import numpy as np
import pytest

from fluttery.audio.sweep import SweepRenderer, SweepRule, SweepSettings, export_sweep


@pytest.mark.parametrize('rate', [8_000, 44_100, 96_000, 384_000])
@pytest.mark.parametrize('curve', ['linear', 'log'])
def test_sine_phase_matches_the_analytic_chirp(rate, curve):
    settings = SweepSettings(start_frequency=500, end_frequency=175, duration=.24, curve=curve, level=.5)
    renderer = SweepRenderer(settings, rate)
    samples = renderer.render(renderer.total_frames + 10)
    time = np.arange(renderer.total_frames) / rate
    if curve == 'linear':
        cycles = 500 * time + (175 - 500) * time ** 2 / (2 * .24)
    else:
        slope = math.log(175 / 500) / .24
        cycles = 500 * (np.exp(slope * time) - 1) / slope
    inside = slice(rate // 100, renderer.total_frames - rate // 100)
    np.testing.assert_allclose(samples[inside], .5 * np.sin(2 * np.pi * cycles[inside]), atol=2e-7)
    assert samples[0] == samples[renderer.total_frames - 1] == 0
    assert np.all(samples[renderer.total_frames:] == 0)
    assert renderer.finished and renderer.position == renderer.total_frames


def test_frequency_landmarks_and_level_curve_destinations_are_preserved():
    settings = SweepSettings(start_frequency=100, end_frequency=800, duration=3, rules=(
        SweepRule(400, level=1, level_curve='linear'),
        SweepRule(800, level=0, level_curve='log'),
        SweepRule(200, level=.5, frequency_curve='linear'),
    ))
    renderer = SweepRenderer(settings, 48_000)
    assert renderer.values_at(.5) == pytest.approx((100 * math.sqrt(2), .25))
    assert renderer.values_at(1) == pytest.approx((200, .5))
    assert renderer.values_at(1.5) == pytest.approx((300, .75))
    assert renderer.values_at(2) == pytest.approx((400, 1))
    assert renderer.values_at(2.5) == pytest.approx((400 * math.sqrt(2), .001))
    assert renderer.values_at(3) == pytest.approx((800, 0))
    times, frequencies, levels = settings.trajectory(samples=7)
    at_midpoint = np.flatnonzero(times == 1.5)[0]
    assert frequencies[at_midpoint] == pytest.approx(300)
    assert levels[at_midpoint] == pytest.approx(.75)
    assert SweepSettings.from_dict(settings.to_dict()) == settings


def test_waveform_switch_preserves_phase_and_chunk_independence():
    settings = SweepSettings(start_frequency=500, end_frequency=15_000, duration=.33, curve='linear', level=.75,
                             rules=(SweepRule(7750, waveform='square'),))
    renderer = SweepRenderer(settings, 44_100)
    count = renderer.total_frames
    whole = renderer.render(count)
    sine = SweepRenderer(replace(settings, rules=()), 44_100).render(count)
    square = SweepRenderer(replace(settings, waveform='square', rules=()), 44_100).render(count)
    crossing = math.ceil(.33 * 44_100 / 2)
    np.testing.assert_allclose(whole[:crossing], sine[:crossing], atol=1e-7)
    np.testing.assert_allclose(whole[crossing:], square[crossing:], atol=1e-7)
    assert np.max(np.abs(whole)) <= settings.level

    renderer = SweepRenderer(settings, 44_100)
    blocks = [renderer.render(241) for _ in range(math.ceil(count / 241))]
    np.testing.assert_array_equal(np.concatenate(blocks)[:count], whole)
    np.testing.assert_array_equal(renderer.render(11), np.zeros(11, dtype=np.float32))


@pytest.mark.parametrize(('bits', 'channels'), [(16, 1), (24, 2), (32, 1)])
def test_wav_export_matches_rendered_signal(tmp_path, bits, channels):
    path = tmp_path / 'sweep.wav'
    settings = SweepSettings(start_frequency=50, end_frequency=3300, duration=.2, waveform='sawtooth')
    rate = 8_000
    assert export_sweep(path, settings, sample_rate=rate, channels=channels, bit_depth=bits) == path
    renderer = SweepRenderer(settings, rate)
    reference = renderer.render(renderer.total_frames)
    with wave.open(str(path), 'rb') as wav:
        assert (wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes()) == (
            channels, bits // 8, rate, renderer.total_frames,
        )
        data = wav.readframes(wav.getnframes())
    if bits == 24:
        encoded = np.frombuffer(data, dtype=np.uint8).astype(np.int32).reshape(-1, 3)
        # Sign extend the high byte of the packed little-endian word.
        high = encoded[:, 2].astype(np.int8).astype(np.int32)
        decoded = encoded[:, 0] + 256 * encoded[:, 1] + 65536 * high
    else:
        decoded = np.frombuffer(data, dtype=f'<i{bits // 8}')
    maximum = 1 << (bits - 1)
    expected = np.rint(reference.astype(np.float64) * maximum).astype(np.int32)
    np.testing.assert_array_equal(decoded, np.repeat(expected, channels))


def test_cancelled_export_preserves_existing_file_and_reports_progress(tmp_path):
    path = tmp_path / 'sweep.wav'
    path.write_bytes(b'existing file')
    progress_updates = []
    settings = SweepSettings(start_frequency=100, end_frequency=1000, duration=1)
    with pytest.raises(InterruptedError, match='cancelled'):
        export_sweep(path, settings, sample_rate=48_000, channels=2, bit_depth=24, cancelled=lambda: bool(progress_updates),
                     progress=lambda position, total: progress_updates.append((position, total)))
    assert progress_updates == [(16_384, 48_000)]
    assert path.read_bytes() == b'existing file'
    assert list(tmp_path.iterdir()) == [path]


def test_wav_export_word_aligns_an_odd_24_bit_mono_chunk(tmp_path):
    path = tmp_path / 'odd.wav'
    settings = SweepSettings(start_frequency=50, end_frequency=1000, duration=801 / 8000)
    export_sweep(path, settings, sample_rate=8000, channels=1, bit_depth=24)
    raw = path.read_bytes()
    assert unpack_from('<I', raw, 4)[0] == len(raw) - 8
    assert unpack_from('<I', raw, 40)[0] == 801 * 3
    assert raw[-1:] == b'\x00' and len(raw) % 2 == 0
    with wave.open(str(path), 'rb') as wav:
        assert wav.getnframes() == 801


@pytest.mark.parametrize(('changes', 'message'), [
    ({'duration': float('nan')}, 'Duration'),
    ({'duration': 1 << 10000}, 'Duration'),
    ({'level': True}, 'Amplitude'),
    ({'rules': (SweepRule(20, 'triangle'),)}, 'rule frequency'),
    ({'rules': (SweepRule(20_000, level=.5),)}, 'end-frequency'),
    ({'rules': (SweepRule(500, 'sine'), SweepRule(500, 'triangle'))}, 'distinct'),
    ({'rules': (SweepRule(500),)}, 'must change'),
])
def test_rejects_invalid_sweep_settings(changes, message):
    with pytest.raises(ValueError, match=message):
        replace(SweepSettings(), **changes).validate()


def test_out_of_band_export_does_not_create_a_file(tmp_path):
    path = tmp_path / 'sweep.wav'
    with pytest.raises(ValueError, match='highest frequency'):
        export_sweep(path, SweepSettings(), sample_rate=32_000, channels=1, bit_depth=16)
    assert list(tmp_path.iterdir()) == []
