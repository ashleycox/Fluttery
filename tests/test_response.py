"""Reference measurements for the transfer, calibration and tape workflows."""

import os
from dataclasses import replace
from wave import open as open_wave

import numpy as np
import pytest
from scipy import ndimage, signal
from scipy.io import wavfile

from fluttery.audio.engine import AudioEngine, FrameQueue
from fluttery.audio.response import (
    LEAD_SECONDS,
    BenchCapture,
    InterfaceStimulus,
    PlaybackTailDetector,
    ResponseCurve,
    ResponseStimulus,
    _playback_level,
    calibration_profile,
    compensate_phase,
    compensate_response,
    measure_response,
    normalize_response_calibrations,
    normalize_response_test_defaults,
    phase_group_delay,
    preview_transfer,
    read_wav_samples,
    response_balance,
    response_test_defaults,
    trace_playback,
)
from fluttery.reporting import build_report, measurement_graph, measurement_summary, measurement_table
from fluttery.sessions import new_session, save_session

RATE = 48_000


def test_tape_peak_hold_and_saved_95th_percentile_are_distinct():
    energy = np.array([[.2, .3]] * 99 + [[.5, .6]])
    np.testing.assert_array_equal(_playback_level(energy, 'maximum'), [.5, .6])
    np.testing.assert_array_equal(_playback_level(energy, 'peak'), [.2, .3])
    np.testing.assert_allclose(_playback_level(energy, 'mean'), np.sqrt(np.mean(energy ** 2, axis=0)))
    for key in ('peak', 'maximum'):
        saved = normalize_response_test_defaults({'tape_playback': {'playbackAggregate': key}})
        assert saved['tape_playback']['playbackAggregate'] == key


def test_response_defaults_are_independent_and_omit_pass_specific_offsets():
    saved = normalize_response_test_defaults({
        'device': {'source': 'pink', 'duration': '12.5', 'offset_ms': 50, 'drift_ppm': 23},
        'tape_playback': {'duration': 160, 'lowHz': 500, 'highHz': 580, 'channels': 'stereo'},
        'tape_recording': {'duration': 160, 'tailSeconds': '9', 'tapeHeads': 'three_head'},
        'interface': {'levelDb': 'nan', 'lowHz': 5000, 'highHz': 100},
        'unknown': {},
    })
    assert saved['device'] == response_test_defaults('device') | {'source': 'pink', 'duration': 12.5}
    assert saved['tape_playback']['duration'] == 160 and saved['tape_playback']['anchorHz'] == 580
    assert saved['tape_playback']['channels'] == 'stereo'
    assert saved['tape_recording']['duration'] == 10 and saved['tape_recording']['tailSeconds'] == 9
    assert saved['tape_recording']['tapeHeads'] == 'three_head'
    assert saved['interface'] == response_test_defaults('interface')
    assert 'unknown' not in saved

    oversized_playback_band = {'lowHz': 100_000, 'highHz': 120_000}
    assert normalize_response_test_defaults({'tape_playback': oversized_playback_band}) == {
        'tape_playback': response_test_defaults('tape_playback'),
    }


def test_response_balance_keeps_unmeasured_stereo_bands_missing():
    values = np.array([[-10., -9.], [2., -4.], [np.nan, 3.]])
    np.testing.assert_allclose(response_balance(values)[:, 0], [1, -6, np.nan])
    assert response_balance(values[:, :1]) is None


def test_bench_capture_publication_and_lead():
    reference = np.array([.15, -.3, .45], dtype=np.float32)
    capture = BenchCapture(1000, 1000, .4, reference, channels=2)
    lead = round(1000 * LEAD_SECONDS)
    assert not capture.output(lead - 1).any()
    assert capture.output_pending
    assert capture.output(5) == pytest.approx([0, .15, -.3, .45, 0])
    assert not capture.output_pending
    samples = np.column_stack((np.arange(550), -np.arange(550)))
    capture.append(samples[:123])
    assert capture.preview(.01) == pytest.approx(samples[113:123])
    capture.append(samples[123:])
    assert not capture.active
    assert capture.seconds == pytest.approx(.4)
    assert capture.read() == pytest.approx(samples[:400])
    capture.cancel('Connection lost.')
    assert capture.error == 'Connection lost.'
    assert not capture.output(17).any()


def test_calibration_mute_clears_queued_audio_and_blocks_unrelated_playback():
    engine = AudioEngine()
    engine.set_monitor(True)
    engine._output_queue = FrameQueue(4800, channels=2)
    monitor = FrameQueue(4800, channels=2)
    engine._output_queue.write(np.ones((100, 2), dtype=np.float32))
    monitor.write(np.ones((4800, 2), dtype=np.float32))
    engine.set_interface_loopback_interlock(True)
    assert engine._output_queue.count == 0
    assert not engine.snapshot()['monitor']['enabled']
    engine.set_generator(True)
    assert not engine.snapshot()['generator']['enabled']
    with pytest.raises(ValueError, match='Disconnect'):
        engine.start_bench_capture(.6)
    engine._fill_output(RATE, monitor)
    returned = np.empty((engine._output_queue.count, 2), dtype=np.float32)
    engine._output_queue.read_into(returned)
    assert not returned.any()
    engine.set_interface_loopback_interlock(False)
    assert engine.snapshot()['monitor']['enabled']
    with pytest.raises(ValueError, match='Prepare'):
        engine.start_bench_capture(.6, interface_loopback=True)


@pytest.mark.parametrize('kind', ('sweep', 'white', 'pink'))
def test_known_stereo_filter_response_with_latency(kind):
    stimulus = ResponseStimulus(kind=kind, low_hz=30, high_hz=15_000, duration=6)
    source = stimulus.render(RATE)
    b, a = signal.butter(1, 2500, fs=RATE)
    left = signal.lfilter(b, a, source)
    captured = np.pad(np.column_stack((left, left / 2)), ((2300, RATE // 3), (0, 0)))
    curve = measure_response(source, captured, RATE, RATE, stimulus)
    frequencies = curve.frequencies
    good_band = (frequencies >= 40) & (frequencies <= 12_000)
    _, expected = signal.freqz(b, a, worN=frequencies, fs=RATE)
    expected_gain = 20 * np.log10(np.abs(expected))
    assert np.nanpercentile(np.abs(curve.gain_db[good_band, 0] - expected_gain[good_band]), 95) < 0.09
    assert np.nanmax(np.abs(curve.gain_db[good_band, 1] - curve.gain_db[good_band, 0] + 20 * np.log10(2))) < 0.002
    assert curve.delay_ms == pytest.approx(2300 * 1000 / RATE, abs=.1)
    assert curve.drift_ppm == pytest.approx(0, abs=1)
    assert not curve.clipping and not curve.relative
    assert np.isfinite(curve.phase_deg[good_band]).all()
    if kind != 'sweep':
        assert np.nanquantile(curve.coherence[good_band], .1) > .985
    else:
        assert curve.coherence is None


def test_sweep_recovers_clock_drift_and_accepts_manual_offsets():
    stimulus = ResponseStimulus(low_hz=40, high_hz=8000, duration=6)
    reference = stimulus.render(RATE)
    delay, offset_ppm = 5200, 520
    ratio = 1 + offset_ppm / 1e6
    samples = np.pad(reference * .7, (delay, RATE // 2))
    positions = np.arange(round(len(samples) * ratio)) / ratio
    captured = ndimage.map_coordinates(samples, (positions,), order=3)
    automatic = measure_response(reference, captured, RATE, RATE, stimulus)
    manual = measure_response(reference, captured, RATE, RATE, stimulus,
                              offset_ms=delay * ratio * 1000 / RATE, drift_ppm=offset_ppm)
    assert automatic.drift_ppm == pytest.approx(offset_ppm, abs=7)
    assert automatic.delay_ms == pytest.approx(manual.delay_ms, abs=.04)
    band = (automatic.frequencies >= 65) & (automatic.frequencies <= 6500)
    assert np.nanmedian(np.abs(automatic.gain_db[band, 0] - manual.gain_db[band, 0])) < .04
    assert np.nanmedian(manual.gain_db[band, 0]) == pytest.approx(20 * np.log10(.7), abs=.02)
    with pytest.raises(ValueError, match='start marker'):
        measure_response(reference, np.zeros_like(captured), RATE, RATE, stimulus)


def test_sweep_separates_the_fundamental_response_from_nonlinear_returns():
    stimulus = ResponseStimulus(low_hz=30, high_hz=9000, duration=4)
    reference = stimulus.render(RATE)
    sos = signal.butter(2, (170, 2900), btype='bandpass', fs=RATE, output='sos')
    offset = 1700
    response = signal.sosfilt(sos, .7 * reference + .9 * reference**2)
    captured = np.pad(response, (offset, RATE // 3))
    curve = measure_response(reference, captured, RATE, RATE, stimulus,
                             offset_ms=1000 * offset / RATE, drift_ppm=0)
    _, linear = signal.sosfreqz(sos, worN=curve.frequencies, fs=RATE)
    band = (curve.frequencies >= 70) & (curve.frequencies <= 8000)
    expected_gain = 20 * np.log10(.7 * np.abs(linear[band]))
    error = np.abs(curve.gain_db[band, 0] - expected_gain)
    assert np.nanpercentile(error, 95) < .04
    assert np.max(np.abs(curve.phase_deg[band, 0] - np.angle(linear[band], deg=True))) < .35


def test_stimulus_and_capture_can_have_different_sample_rates():
    stimulus = ResponseStimulus(kind='pink', duration=5, high_hz=15_000)
    source = stimulus.render(44_100)
    expected = 20 * np.log10(.52)
    captured = np.pad(signal.resample_poly(source, 160, 147) * .52, (1920, 18_000))
    curve = measure_response(source, captured, 44_100, RATE, stimulus)
    usable = (curve.frequencies > 80) & (curve.frequencies < 10_000)
    assert np.nanquantile(np.abs(curve.gain_db[usable] - expected), .95) < .02
    assert curve.delay_ms == pytest.approx(40, abs=.03)


def test_tape_sweep_gain_remains_valid_while_the_transport_wows():
    stimulus = ResponseStimulus(low_hz=100, high_hz=8000, duration=7)
    reference = stimulus.render(RATE)
    delayed = np.pad(reference * .46, (7000, RATE // 2))
    base = np.arange(len(delayed))
    warp = base + (RATE * .00013) * np.sin(2 * np.pi * 4 * base / RATE)
    captured = ndimage.map_coordinates(delayed, (warp,), order=3)
    curve = measure_response(reference, captured, RATE, RATE, stimulus, track_recorded_sweep=True)
    selected = (curve.frequencies >= 150) & (curve.frequencies <= 7200)
    assert curve.tracked and curve.phase_deg is None
    assert np.nanquantile(np.abs(curve.gain_db[selected, 0] - 20 * np.log10(.46)), .95) < .2


def test_separate_tape_playback_leaves_unmeasured_bins_empty():
    time = np.arange(RATE // 3) / RATE
    tones = []
    for frequency, amplitude in ((100, .2), (250, .25), (500, .3), (1000, .4), (4000, .24), (8000, .2)):
        pulse = np.sin(2 * np.pi * frequency * time) * amplitude
        tones.append(np.column_stack((pulse, pulse / 2)))
    curve = trace_playback(np.concatenate(tones), RATE, low_hz=50, high_hz=10_000, anchor_hz=1000)
    assert curve.relative and curve.tracked and curve.anchor_hz == 1000
    assert np.isnan(curve.gain_db).sum() > len(curve.frequencies)
    anchor = int(np.argmin(np.abs(curve.frequencies - 1000)))
    assert curve.gain_db[anchor].mean() == pytest.approx(0, abs=.02)
    mask = np.isfinite(curve.gain_db[:, 0]) & (curve.frequencies >= 200)
    assert np.nanmedian(curve.gain_db[mask, 0] - curve.gain_db[mask, 1]) == pytest.approx(20 * np.log10(2), abs=.05)


def test_playback_band_limits_do_not_change_the_levels_of_steady_tones():
    time = np.arange(RATE // 2) / RATE
    frequencies = (300, 500, 1000, 1500, 1800, 2000)
    pcm = np.concatenate([.2 * np.sin(2 * np.pi * hz * time) for hz in frequencies])
    narrow = trace_playback(pcm, RATE, low_hz=300, high_hz=2000, anchor_hz=1000)
    for hz in frequencies:
        band = np.argmin(abs(narrow.frequencies - hz))
        assert narrow.gain_db[band, 0] == pytest.approx(0, abs=.025), hz


def test_playback_keeps_clean_channel_when_the_other_is_louder_broadband_noise():
    from fluttery.response_ui import ResponseDialog, ResponseRoute

    time = np.arange(RATE // 2) / RATE
    tones = np.concatenate([amplitude * np.sin(2 * np.pi * hz * time)
                            for hz, amplitude in ((500, .06), (1000, .03), (5000, .02))])
    noise = np.random.default_rng(5255).normal(0, .15, len(tones))
    curve = trace_playback(np.column_stack((noise, tones)), RATE, low_hz=250, high_hz=10_000)
    for hz, expected in ((500, 20 * np.log10(2)), (1000, 0), (5000, 20 * np.log10(2 / 3))):
        band = np.argmin(np.abs(curve.frequencies - hz))
        assert curve.gain_db[band, 1] == pytest.approx(expected, abs=.05)
        assert np.isnan(curve.gain_db[band, 0])
    assert curve.anchor_channels == (1,)
    calibration = ResponseCurve(np.array([250, 1000, 10_000]), np.array([[0, 0], [0, 0], [-4, 2]]))
    profile = calibration_profile('Loopback', calibration, RATE, input_device='Input', output_device='Output',
                                  output_rate=RATE)
    for route in (ResponseRoute('Input', '', None), ResponseRoute('Input', 'Output', RATE)):
        effective = ResponseDialog._profile_for(curve, RATE, profile, route)
        assert effective is None
        assert ResponseDialog._gain(curve, effective) is curve.gain_db
        assert 'Tape playback' in ResponseDialog._correction_problem(curve, RATE, profile, route)


def test_bench_capture_watchdog_allows_steady_input_and_stops_a_dead_stream(monkeypatch):
    from fluttery.audio import response

    now = 0.0
    monkeypatch.setattr(response, 'monotonic', lambda: now)
    capture = BenchCapture(RATE, None, 12, channels=1)
    now = 2.9
    capture.abort_stalled_input()
    assert capture.active
    capture.append(np.ones(12, dtype=np.float32))
    now = 4
    capture.abort_stalled_input()
    assert capture.active
    now = 6
    capture.abort_stalled_input()
    assert not capture.active and 'stopped delivering samples' in capture.error


def test_playback_auto_stop_waits_for_tones_and_omits_the_long_silent_tail():
    capture = BenchCapture(RATE, None, 12, channels=1)
    detector = PlaybackTailDetector(RATE, 20, 20000, 1, -45, stereo=False)
    random = np.random.default_rng(808)
    capture.append(random.normal(0, .03, RATE))
    capture.append(np.zeros(2 * RATE))
    assert detector.observe(capture) is None

    time = np.arange(RATE) / RATE
    reference = .2 * np.sin(2 * np.pi * 1000 * time)
    sweep = .12 * signal.chirp(time, 3000, 1, 10_000, method='logarithmic')
    capture.append(reference)
    capture.append(np.zeros(RATE // 4))
    assert detector.observe(capture) is None
    capture.append(sweep)
    expected_cut = round(capture.seconds * RATE)
    capture.append(random.normal(0, .0008, RATE // 2))
    assert detector.observe(capture) is None
    capture.append(random.normal(0, .0008, RATE))
    cutoff = detector.observe(capture)
    assert cutoff == pytest.approx(expected_cut, abs=.15 * RATE)
    result = trace_playback(capture.section(3 * RATE, cutoff), RATE, anchor_hz=1000)
    assert np.isnan(result.gain_db[result.frequencies < 750]).all()
    assert np.isfinite(result.gain_db[result.frequencies >= 3500]).any()


def test_playback_tail_arms_on_either_channel_but_waits_for_both_to_quiet():
    capture = BenchCapture(RATE, None, 8, channels=2)
    detector = PlaybackTailDetector(RATE, 20, 20000, 1, -45)
    time = np.arange(RATE) / RATE
    noise = np.random.default_rng(40).normal(0, .08, RATE)
    capture.append(np.column_stack((noise, .035 * np.sin(2 * np.pi * 1000 * time))))
    assert detector.observe(capture) is None
    capture.append(np.column_stack((np.tile(noise, 2), np.zeros(2 * RATE))))
    assert detector.observe(capture) is None
    capture.append(np.zeros((2 * RATE, 2)))
    assert detector.observe(capture) == 3 * RATE


def test_calibration_subtracts_stereo_baseline_only_in_measured_band():
    baseline = ResponseCurve(np.array([100, 1000]), np.array([[-2, -4], [0, -5]]))
    profile = calibration_profile('Bench', baseline, RATE, output_device='USB audio', offset_db=1)
    assert len(normalize_response_calibrations([profile, {**profile, 'name': 'bench'}])) == 1
    test = ResponseCurve(np.array([30, 100, np.sqrt(100 * 1000), 1000, 2000]), np.ones((5, 2)) * -10)
    gain = compensate_response(test, profile)
    assert np.isnan(gain[[0, 4]]).all()
    assert gain[1:-1] == pytest.approx(np.array([[-9, -7], [-10, -6.5], [-11, -6]]))


@pytest.mark.parametrize('rate', (44_100, 48_000))
def test_interface_correction_uses_only_verified_roundtrip_routes(rate):
    from fluttery.response_ui import ResponseDialog, ResponseRoute

    frequencies = np.geomspace(65, 17_000, 100)
    slope = np.log2(frequencies / 1000)[:, None]
    interface_gain = slope * np.array([.7, -.6]) + np.array([2, -3])
    device_gain = slope * np.array([.2, .3]) + np.array([-5, -7])
    interface_delay_ms = np.array([.18, .11])
    device_delay_ms = np.array([.16, .09])

    def wrapped_phase(delay_ms):
        return (180 - .36 * frequencies[:, None] * delay_ms) % 360 - 180

    route = ResponseRoute('Input', 'Output', rate)
    loopback = ResponseCurve(frequencies, interface_gain, phase_deg=wrapped_phase(interface_delay_ms))
    profile = calibration_profile('Loopback', loopback, rate, input_device=route.input_device,
                                  output_device=route.output_device, output_rate=route.output_rate)
    curve = ResponseCurve(frequencies, interface_gain + device_gain,
                          phase_deg=wrapped_phase(interface_delay_ms + device_delay_ms))
    effective = ResponseDialog._profile_for(curve, rate, profile, route)
    assert effective is profile
    timed = ResponseDialog._timed_profile(effective, route)
    assert ResponseDialog._gain(curve, effective) == pytest.approx(device_gain, abs=1e-10)
    phase = compensate_phase(curve, timed)
    assert phase == pytest.approx(wrapped_phase(device_delay_ms), abs=1e-10)
    delay = phase_group_delay(frequencies, phase)
    assert delay == pytest.approx(np.broadcast_to(device_delay_ms, delay.shape), abs=1e-9)

    incompatible = (
        (rate, profile, None, 'audio route is unknown'),
        (rate, profile, replace(route, output_device='', output_rate=None), 'output route is unknown'),
        (rate, profile, replace(route, input_device='<default input>'), 'did not identify both'),
        (rate, profile, replace(route, output_device='Different output'), 'different audio devices'),
        (rate, profile, replace(route, output_rate=rate * 2), 'different output sample rates'),
        (rate * 2, profile, route, 'different input sample rates'),
        (rate, {**profile, 'outputRate': 0}, route, 'missing audio route details'),
        (rate, {**profile, 'gainDb': [[row[0]] for row in profile['gainDb']]}, route, 'fewer measured channels'),
    )
    for capture_rate, candidate, capture_route, reason in incompatible:
        assert reason in ResponseDialog._correction_problem(curve, capture_rate, candidate, capture_route)
        effective = ResponseDialog._profile_for(curve, capture_rate, candidate, capture_route)
        assert effective is None
        assert ResponseDialog._gain(curve, effective) is curve.gain_db
        assert compensate_phase(curve, ResponseDialog._timed_profile(effective, capture_route)) is curve.phase_deg


def test_interface_profile_measures_and_keeps_the_actual_noise_and_distortion_floor():
    stimulus = InterfaceStimulus(duration=2, low_hz=100, high_hz=18000)
    reference = stimulus.render(RATE)
    _, tone_start, tone_end = stimulus.baseline_segments(RATE)
    phase = np.arange(tone_end - tone_start) * 2 * np.pi * 997 / RATE
    generator = np.random.default_rng(7)
    returns = reference[:, None] * np.array([.8, .6])
    returns[tone_start:] += .1 * np.sin(2 * phase)[:, None] * np.array([.002, .003])
    returns += generator.normal(0, 6e-5, returns.shape)
    captured = np.pad(returns, ((2220, 2400), (0, 0)))
    curve = measure_response(reference, captured, RATE, RATE, stimulus)
    assert curve.baseline is not None
    assert curve.baseline.thd_percent == pytest.approx((.25, .50), abs=.008)
    expected_noise = 20 * np.log10(6e-5) + 10 * np.log10((20_000 - 20) / (RATE / 2))
    assert curve.baseline.noise_dbfs == pytest.approx((expected_noise, expected_noise), abs=.25)
    assert curve.baseline.high_hz == 20_000
    profile = calibration_profile('Loopback', curve, RATE, input_device='Fixture', output_device='Fixture', output_rate=RATE)
    assert profile['baseline']['toneHz'] == 997 and profile['baseline']['thdPercent'] == pytest.approx((.25, .50), abs=.008)
    assert profile['markerOffsetMs'] == pytest.approx(2220 * 1000 / RATE, abs=.1)
    assert len(profile['phaseDeg']) == len(profile['gainDb'])


def test_transfer_correction_removes_phase_slope_and_group_delay_without_filling_gaps():
    frequency = np.geomspace(65, 17_000, 200)
    wrap = lambda deg: (deg + 180) % 360 - 180
    interface = wrap(-360 * frequency * .00018)[:, None]
    device = wrap(-360 * frequency * .00034)[:, None]
    interface[30] = np.nan
    profile = calibration_profile('Delay', ResponseCurve(frequency, np.zeros_like(interface), phase_deg=interface), RATE)
    curve = ResponseCurve(frequency, np.zeros_like(device), phase_deg=device)
    phase = compensate_phase(curve, profile)
    delay = phase_group_delay(curve.frequencies, phase)
    assert np.isnan(phase[30, 0])
    assert np.nanmax(np.abs(delay - .16)) < 1e-9


def test_low_coherence_calibration_gap_survives_json_profile_round_trip():
    frequency = np.array([100, 200, 300, 400, 500, 600])
    shape = (len(frequency), 1)
    coherence = np.ones(shape)
    coherence[2:4] = .6
    baseline = ResponseCurve(frequency, np.full(shape, -3.), np.full(shape, -8.), coherence)
    profile = calibration_profile('Coherent bands', baseline, RATE)
    assert profile['bandsHz'] == [[100, 200], [500, 600]]
    assert normalize_response_calibrations([profile]) == [profile]
    assert normalize_response_calibrations([{**profile, 'bandsHz': [[100, 250]]}]) == []
    curve = ResponseCurve(frequency, np.zeros(shape), np.zeros(shape))
    assert compensate_response(curve, profile)[:, 0] == pytest.approx([3, 3, np.nan, np.nan, 3, 3], nan_ok=True)
    assert compensate_phase(curve, profile)[:, 0] == pytest.approx([8, 8, np.nan, np.nan, 8, 8], nan_ok=True)


def test_calibration_keeps_gain_and_phase_on_the_other_channel_through_a_gap():
    frequency = np.array([100, 200, 300, 400, 500, 600])
    gain = np.tile([-4., -6.], (len(frequency), 1))
    phase = np.tile([20., -30.], (len(frequency), 1))
    coherence = np.ones_like(gain)
    coherence[2:4, 1] = .5
    profile = calibration_profile('Asymmetric loopback', ResponseCurve(frequency, gain, phase, coherence), RATE)
    assert profile['gainDb'][2:4] == [[-4, None], [-4, None]]
    [profile] = normalize_response_calibrations([profile])
    requested = np.array([100, 350, 400, 600])
    observed = ResponseCurve(requested, np.zeros((4, 2)), np.zeros((4, 2)))
    corrected = compensate_response(observed, profile)
    phase_corrected = compensate_phase(observed, profile)
    assert corrected[:, 0] == pytest.approx([4] * 4)
    assert phase_corrected[:, 0] == pytest.approx([-20] * 4)
    assert np.isnan(corrected[1:3, 1]).all() and np.isnan(phase_corrected[1:3, 1]).all()


@pytest.mark.parametrize('kind', ('sweep', 'pink'))
def test_partial_response_preview_uses_the_return_already_captured(kind):
    stimulus = ResponseStimulus(kind=kind, duration=4, low_hz=100, high_hz=16_000)
    reference = stimulus.render(RATE)
    capture = BenchCapture(RATE, RATE, 6, reference, channels=1)
    received = np.pad(reference * .70, (3100, 1000))
    capture.append(received[:RATE // 2])
    assert preview_transfer(capture, stimulus) is None
    capture.append(received[RATE // 2:2 * RATE])
    curve = preview_transfer(capture, stimulus)
    assert curve is not None
    usable = np.isfinite(curve.gain_db[:, 0])
    assert np.count_nonzero(usable) > 15
    assert np.nanmedian(curve.gain_db) == pytest.approx(20 * np.log10(.70), abs=.01)
    if kind == 'sweep':
        assert curve.frequencies[-1] < 2000


@pytest.mark.skipif(os.environ.get('FLUTTERY_GUI_TESTS') != '1', reason='requires a native desktop')
def test_interface_prepare_close_and_recovery_require_explicit_disconnect():
    import wx

    from fluttery.response_ui import ResponseDialog

    app = wx.GetApp() or wx.App(False)
    frame = wx.Frame(None)

    class Controller:
        def __init__(self):
            self.settings = {'interfaceLoopbackPendingDisconnect': False, 'channels': 2}
            self.confirm_allowed = True

        def hold_interface_loopback(self):
            self.settings['interfaceLoopbackPendingDisconnect'] = True
            return True

        def confirm_interface_disconnected(self):
            if self.confirm_allowed:
                self.settings['interfaceLoopbackPendingDisconnect'] = False
            return self.confirm_allowed

        def bench_start_capture(self, _duration, *, on_ready, still_wanted, **_options):
            self.pending = on_ready, still_wanted

    controller = Controller()
    dialog = ResponseDialog(frame, controller, 'interface')
    recovery = None
    try:
        dialog.Show()
        assert dialog.run.GetLabel() == 'Prepare Loopback'
        dialog._run()
        assert controller.settings['interfaceLoopbackPendingDisconnect']
        assert dialog.run.GetLabel() == 'Measure Loopback'
        assert not dialog.select_mode('device')
        dialog._hide()
        assert dialog.IsShown()
        assert dialog.run.GetLabel() == 'Confirm Cable Disconnected'
        assert 'Unplug the loopback cable' in dialog.guide.GetLabel()
        controller.confirm_allowed = False
        dialog._run()
        assert dialog.IsShown() and controller.settings['interfaceLoopbackPendingDisconnect']
        controller.confirm_allowed = True
        dialog._run()
        assert not dialog.IsShown()
        assert not controller.settings['interfaceLoopbackPendingDisconnect']

        dialog.Show()
        dialog._run()
        ticket = dialog._request = object()
        dialog._started(None, dialog._parameters(), ticket)
        assert 'did not start' in dialog.status.GetValue()
        assert dialog.run.GetLabel() == 'Confirm Cable Disconnected'
        assert controller.settings['interfaceLoopbackPendingDisconnect']
        dialog._run()

        controller.settings['interfaceLoopbackPendingDisconnect'] = True
        recovery = ResponseDialog(frame, controller, 'device')
        assert recovery.mode == 'interface'
        assert recovery.run.GetLabel() == 'Confirm Cable Disconnected'
        recovery._run()
        assert not controller.settings['interfaceLoopbackPendingDisconnect']
        assert dialog.select_mode('device')
        named = {'name': 'No interface correction'}
        another = {'name': 'Bench interface'}
        controller.settings['responseCalibrations'] = [named, another]
        dialog.refresh_display_settings()
        dialog.calibration.SetSelection(2)
        assert dialog.calibration.GetStringSelection() == 'No interface correction (saved profile)'
        controller.settings['responseCalibrations'] = [another, named]
        dialog.refresh_display_settings()
        assert dialog._selected_profile() == named
        dialog._run()
        reply, wanted = controller.pending
        assert wanted() and dialog.stop.IsEnabled() and not dialog.mode_choice.IsEnabled()
        dialog._stop_capture()
        assert not wanted() and dialog.mode_choice.IsEnabled()
        reply(None)
        assert 'cancelled' in dialog.status.GetValue()

        dialog._run()
        reply, wanted = controller.pending
        assert wanted()
        dialog._hide()
        assert not wanted()
        reply(None)
        dialog.Show()
        assert dialog.run.IsEnabled() and not dialog.stop.IsEnabled()
    finally:
        for window in (recovery, dialog):
            if window is not None:
                window.shutdown()
                window.Destroy()
        frame.Destroy()
        app.Yield()


@pytest.mark.skipif(os.environ.get('FLUTTERY_GUI_TESTS') != '1', reason='requires a native desktop')
def test_response_comparison_keeps_bypassed_and_corrected_captures(tmp_path, monkeypatch):
    import csv
    from contextlib import nullcontext
    from types import SimpleNamespace

    import wx

    from fluttery.response_ui import ResponseDialog, ResponseRoute

    app = wx.GetApp() or wx.App(False)
    frame = wx.Frame(None)
    frequency = np.geomspace(200, 8000, 60)
    gain = np.full((len(frequency), 1), -8.)
    phase = ((-.1 * frequency + 180) % 360 - 180)[:, None]
    curve = ResponseCurve(frequency, gain, phase)
    baseline = ResponseCurve(frequency, gain + 5, phase / 2)
    route = ResponseRoute('Line In', 'Line Out', RATE)
    profile = calibration_profile('Bench interface', baseline, RATE, input_device=route.input_device,
                                  output_device=route.output_device, output_rate=RATE)
    settings = {'channels': 1, 'responseCalibrations': [profile]}
    recorded = []
    controller = SimpleNamespace(settings=settings, record_bench_result=lambda kind, data: recorded.append(data))
    dialog = ResponseDialog(frame, controller)
    path = tmp_path / 'response.csv'
    monkeypatch.setattr(wx, 'FileDialog', lambda *_args, **_kwargs: nullcontext(
        SimpleNamespace(ShowModal=lambda: wx.ID_OK, GetPath=lambda: str(path))))
    monkeypatch.setattr(dialog, '_background', lambda _message, operation, done: done(operation()))
    try:
        state = dialog._parameters()
        dialog._save_result(curve, state, RATE, route=route)
        assert recorded[-1]['correctionApplied'] is True
        assert recorded[-1]['leftDb'] == pytest.approx([-5.] * len(frequency))
        assert dialog.compare_raw.IsEnabled()
        dialog.plot.set_view(xlim=(400, 2500), ylim=(-12, 0))
        view = dialog.plot._xlim, dialog.plot._ylim
        dialog.compare_raw.SetValue(True)
        dialog._correction_changed()
        assert {trace.label.split()[0] for trace in dialog.plot._series} == {'Corrected', 'Raw'}
        assert (dialog.plot._xlim, dialog.plot._ylim) == view
        dialog.bypass.SetValue(True)
        dialog._correction_changed()
        assert 'Raw is the active response' in dialog._activity
        dialog._save_result(curve, state, RATE, route=route)
        assert recorded[-1]['correctionApplied'] is False
        assert recorded[-1]['leftDb'] == recorded[-1]['rawLeftDb']
        assert recorded[-1]['correctedLeftDb'] == recorded[0]['leftDb']
        assert recorded[-1]['leftGroupDelayMs'] == recorded[-1]['rawLeftGroupDelayMs']
        assert recorded[-1]['correctedLeftGroupDelayMs'] == recorded[0]['leftGroupDelayMs']
        dialog._export()
        with path.open(newline='') as stream:
            exported = next(csv.DictReader(stream))
        assert exported['Active response'] == 'Raw' and exported['Correction profile'] == 'Bench interface'
        assert float(exported['Measured gain (dB)']) == -8 and float(exported['Corrected gain (dB)']) == -5

        capture = SimpleNamespace(input_rate=RATE)
        dialog._capture, dialog._capture_route = capture, route
        dialog._live_selection = 0
        dialog.metric.Set(['Return Waveform', 'Live Gain', 'Live Phase'])
        dialog.metric.SetSelection(1)
        dialog._plot_selection_changed()
        ticket = dialog._live_task = object()
        dialog._live_preview_ready(ticket, capture, 1, curve, False)
        assert {trace.label.split()[0] for trace in dialog.plot._series} == {'Corrected', 'Raw'}
        assert 'Raw is the active response' in dialog._live_correction_status
        dialog._capture = dialog._capture_route = None
        dialog._restore_plot_selection()

        settings['responseCalibrations'] = [profile, {**profile, 'name': 'Second match'}]
        dialog.refresh_display_settings()
        assert 'More than one profile matches' in dialog._activity
        dialog._reload_profiles('Bench interface')
        assert dialog._comparison_profile(curve, RATE, route)[0] == profile
        dialog._save_result(curve, state, RATE)
        assert recorded[-1]['correctedLeftDb'] == []
        assert 'route is unknown' in dialog._activity

        assert dialog.select_mode('tape_playback')
        assert not dialog.calibration.IsShown()
        dialog._save_result(replace(curve, relative=True), dialog._parameters(), RATE, route=route)
        assert recorded[-1]['correctionApplied'] is False
        assert recorded[-1]['correctedLeftDb'] == []
        assert 'tape supplies the reference' in ' '.join(dialog.guide.GetLabel().lower().split())
    finally:
        dialog._capture = None
        dialog.shutdown()
        dialog.Destroy()
        frame.Destroy()
        app.Yield()


def test_wav_reference_files_and_response_reporting(tmp_path):
    path = tmp_path / 'capture.wav'
    impulse = np.tile(np.array([[-32768, 32767], [8192, -8192]], dtype=np.int16), (RATE // 2, 1))
    wavfile.write(path, RATE, impulse)
    rate, samples = read_wav_samples(path)
    assert rate == RATE
    assert samples[:2] == pytest.approx(impulse[:2] / 32768)
    with pytest.raises(ValueError, match='highest|high frequency'):
        replace(ResponseStimulus(), high_hz=30_000).render(RATE)

    entry = {'type': 'frequency-response', 'timestamp': 0, 'data': {
        'mode': 'tape_playback', 'frequencyHz': [20, 100, 1000], 'leftDb': [None, -4, 0], 'rightDb': [None, -5, -1],
        'leftPhase': [None, -4, 0], 'sampleRate': RATE, 'relative': True,
    }}
    graph = measurement_graph(entry)
    assert graph.log_x and len(graph.traces) == 2
    np.testing.assert_array_equal(graph.traces[0]['x'], [100, 1000])
    np.testing.assert_array_equal(graph.traces[0]['y'], [-4, 0])
    continuous = entry | {'data': entry['data'] | {
        'mode': 'device', 'relative': False, 'leftDb': [-15, None, 0],
    }}
    graph = measurement_graph(continuous)
    np.testing.assert_array_equal(graph.traces[0]['x'], [20, 100, 1000])
    np.testing.assert_array_equal(graph.traces[0]['y'], [-15, np.nan, 0])
    labels, rows = measurement_table(entry)
    assert 'Left phase (degrees)' in labels and rows[-1] == ('1000', '0', '-1', '0')
    assert 'Tape playback' in measurement_summary(entry)
    session = new_session()
    session['measurementLog'].append(entry)
    save_session(tmp_path / 'test.fluttery', session)
    assert build_report(session, {}).measurements == [entry | {'stats': None}]


def test_24_bit_wav_reads_only_the_selected_pair(tmp_path):
    path = tmp_path / 'four-channel-reference.wav'
    samples = np.array([[-8388608, 8388607, 1500, -1700], [455000, -455000, 7000, -8000]], dtype=np.int32)
    packed = ((samples[..., None] >> [0, 8, 16]) & 255).astype(np.uint8).tobytes()
    with open_wave(str(path), 'wb') as stream:
        stream.setparams((4, 3, RATE, 0, 'NONE', 'not compressed'))
        stream.writeframes(packed * (RATE // 2))
    rate, values = read_wav_samples(path)
    assert rate == RATE and values.shape == (RATE, 2)
    assert values[:2] == pytest.approx(samples[:, :2] / 0x800000)
