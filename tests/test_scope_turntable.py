from types import SimpleNamespace

import numpy as np
import pytest

from fluttery.audio import AudioEngine
from fluttery.audio.scope import ScopeBuffer, acquire_sweep, edge_positions
from fluttery.audio.turntable import (
    automatic_record_reference,
    display_rpm,
    nominal_speed,
    stereo_separation,
)
from fluttery.measurement_ui import ScopePanel, _turntable_speed


def test_scope_qualifies_a_complete_schmitt_transition_without_fabricating_gaps():
    wave = [-1, -.1, .1, -.1, .1, 1, .1, -.1, -1, np.nan, 1]
    np.testing.assert_allclose(edge_positions(wave, 0), [1.5, 3.5])
    np.testing.assert_allclose(edge_positions(wave, 0, hysteresis=.3), [3.5])
    np.testing.assert_allclose(edge_positions(wave, 0, rising=False, hysteresis=.3), [6.5])
    assert not len(edge_positions([-.4, -.1, .1, -.05, .04, -.4], 0, hysteresis=.3))
    assert not len(edge_positions([-.4, -.1, .1, np.nan, .4], 0, hysteresis=.3))


def test_scope_single_waits_for_post_trigger_and_preserves_fractional_sample_time():
    epoch = object()
    wave = np.array([-1.0, -1, -.25, .75, 1, -1, -.25, .75, 1, 1, 1])[:, None]
    partial = ScopeBuffer(1000, wave[:4], 104, epoch)
    entire = ScopeBuffer(1000, wave, 111, epoch)
    assert acquire_sweep(partial, 4, pretrigger=.25, after=101, first=True) is None
    first = acquire_sweep(entire, 4, pretrigger=.25, after=101, first=True)
    assert first.trigger_frame == 102.25
    np.testing.assert_allclose(first.time_ms, [-1.25, -.25, .75, 1.75, 2.75])
    np.testing.assert_equal(first.samples[:, 0], wave[1:6, 0])
    latest = acquire_sweep(entire, 4, pretrigger=.25, after=102.25)
    assert latest.trigger_frame == 106.25
    assert acquire_sweep(entire, 4, level=2) is None
    automatic = acquire_sweep(entire, 4, level=2, free_run=True)
    assert automatic.trigger_frame is None
    np.testing.assert_equal(automatic.samples, wave[-5:])


@pytest.mark.parametrize('rate', (44100, 96000))
def test_scope_engine_uses_sample_clock_for_uneven_chunks_and_excludes_old_signal(rate):
    engine = AudioEngine(settings={'correction_factor': 1.0002})
    samples = (.15 * np.cos(2 * np.pi * 713.3 * np.arange(702) / rate)).astype(np.float32)
    engine.feed(samples[:263], sample_rate=rate)
    first = engine.scope_frames(.001)
    engine.feed(samples[263:])
    second = engine.scope_frames(.001, after=first.end)
    assert second.rate == pytest.approx(rate * 1.0002)
    assert second.end == len(samples) and first.epoch is second.epoch
    np.testing.assert_equal(second.samples[:, 0], samples[second.start:])
    engine.processor.note_gap()
    empty = engine.scope_frames(1)
    assert empty.epoch is not first.epoch and empty.end == second.end and empty.start == empty.end
    engine.feed(np.zeros(4096, dtype=np.float32))
    demodulated = engine.scope_frames(.1, modulation=True)
    assert len(demodulated.samples) and np.isnan(demodulated.samples).all()


def test_scope_uses_one_channel_voltage_calibration_only_when_all_used_channels_are_calibrated():
    view = SimpleNamespace(_display_channels=lambda _channels: (0,),
                           trigger_source=SimpleNamespace(GetSelection=lambda: 0))
    factors = np.array([2.6, np.nan])
    voltage = ScopePanel._display_voltage(view, factors, 2)
    np.testing.assert_equal(voltage, [2.6, 1])
    assert ScopePanel._display_voltage(view, factors, 2, xy=True) is None
    view.trigger_source.GetSelection = lambda: 1
    assert ScopePanel._display_voltage(view, factors, 2) is None


def test_turntable_rpm_does_not_round_references_or_confuse_cutting_and_playback_speed():
    assert nominal_speed(33) == 100 / 3
    assert nominal_speed(50 / 3) == 50 / 3
    assert nominal_speed(78) == 78.26 and nominal_speed(78, 78.0) == 78.0
    assert nominal_speed(0, custom=80.25) == 80.25
    assert display_rpm(nominal_speed(33)) == '33⅓ RPM'
    at_45 = 3150 * 45 / (100 / 3)
    assert automatic_record_reference(at_45, 100 / 3, 45) == 3150
    assert automatic_record_reference(3075, 100 / 3, 100 / 3) is None
    assert automatic_record_reference(2962.4, 78.26, 78.26) == 3000
    assert _turntable_speed({'currentFreq': at_45}, 3150, 100 / 3, 45) == pytest.approx((45, 0))
    assert _turntable_speed({'currentFreq': 3000}, 3000, 78.26, 78.0) == (
        78.26, pytest.approx((78.26 / 78.0 - 1) * 100),
    )
    assert _turntable_speed({'currentFreq': 3126, 'clipped': True}, 3150, 33 + 1 / 3, 33 + 1 / 3) is None


def separation_signal(rate, channel, frequency, separation, degrees, noise):
    time = np.arange(round(.8 * rate)) / rate
    # Include record eccentricity and an off-bin carrier, plus uncorrelated
    # noise in each channel. The complex transfer should follow that carrier.
    phase = 2 * np.pi * frequency * time + .5 * np.sin(2 * np.pi * 4.3 * time)
    random = np.random.default_rng(0)
    values = random.normal(0, noise, (len(time), 2))
    values[:, channel] += .75 * np.sin(phase)
    values[:, 1 - channel] += .75 * 10 ** (-separation / 20) * np.sin(phase + np.deg2rad(degrees))
    return values


@pytest.mark.parametrize(('rate', 'channel', 'frequency', 'separation', 'degrees'), (
    (44100, 0, 1003.27, 48, 37), (96000, 1, 3151.31, 64, -102),
))
def test_crosstalk_recovers_independent_magnitude_and_phase_from_varying_tones(rate, channel, frequency,
                                                                           separation, degrees):
    samples = separation_signal(rate, channel, frequency, separation, degrees, 0.00001)
    result = stereo_separation(samples, rate, channel, expected_hz=frequency if channel else 0)
    assert result.direction == ('Left to right', 'Right to left')[channel]
    assert not result.limited
    assert result.frequency == pytest.approx(frequency, abs=.1)
    assert result.separation_db == pytest.approx(separation, abs=.02)
    assert result.phase_deg == pytest.approx(degrees, abs=.15)
    assert result.leakage_dbfs < result.driven_dbfs and result.resolution_dbfs < result.leakage_dbfs
    trims = (1.0, -1.0)
    corrected = stereo_separation(samples, rate, channel, trims_db=trims)
    assert corrected.separation_db == pytest.approx(separation + trims[channel] - trims[1 - channel], abs=.02)


def test_crosstalk_honors_measurement_floor_and_rejects_invalid_tones():
    rate = 48000
    weak = separation_signal(rate, 0, 1000, 180, 0, 0.01)
    result = stereo_separation(weak, rate, 0)
    assert result.limited and result.phase_deg is None
    assert result.resolution_dbfs >= result.leakage_dbfs
    assert result.separation_db == pytest.approx(result.driven_dbfs - result.resolution_dbfs)
    real = separation_signal(rate, 0, 1000, 30, 31, .00003)
    with pytest.raises(ValueError, match='Both channels'):
        stereo_separation(real, rate, 1)
    with pytest.raises(ValueError, match='clipping'):
        stereo_separation(real * 2, rate, 0)
    with pytest.raises(ValueError, match='stable test tone|No usable tone'):
        stereo_separation(real, rate, 0, expected_hz=1300)
