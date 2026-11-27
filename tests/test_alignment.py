"""Known signals for the alignment tools, including fractional delay and products."""

from math import pi

import numpy as np
import pytest

from fluttery.audio.alignment import (
    azimuth_reading,
    band_curve,
    compare_distortion,
    levels,
    normalize_alignment_options,
    resolve_azimuth_delay,
    tone_reading,
    two_tone_reading,
    unwrap_delay,
)


def times(rate=48_000, duration=0.6):
    return np.arange(round(rate * duration)) / rate


@pytest.mark.parametrize('rate', (44_100, 48_000, 96_000))
def test_test_record_levels_and_individual_third_harmonic(rate):
    t = times(rate)
    radians = 2 * pi * 314.75 * t
    left = 0.2 * np.sin(radians) + 0.002 * np.cos(3 * radians) + 0.04
    right = 0.4 * np.cos(radians) + 0.002 * np.sin(2 * radians) + 0.004 * np.cos(3 * radians)
    measured = tone_reading(np.column_stack((left, right)), rate, 315)
    l, r = measured.channels
    assert measured.frequency == pytest.approx(314.75, abs=0.007)
    assert l.hd3_percent == pytest.approx(1, abs=0.02)
    assert l.thdn_percent == pytest.approx(1, abs=0.019)
    assert r.thd_percent == pytest.approx(np.hypot(0.5, 1), abs=0.018)
    assert r.tone_dbfs - l.tone_dbfs == pytest.approx(20 * np.log10(2), abs=0.01)


def test_fractional_tape_azimuth_sign_wrap_and_shared_speed_variations():
    rate, hz, delay = 48_000, 10037.2, 7.31
    t = times(rate)
    instantaneous_phase = 2 * pi * hz * t + 0.9 * np.sin(2 * pi * 4 * t)
    left = 0.28 * np.sin(instantaneous_phase)
    right = 0.41 * np.sin(instantaneous_phase - 2 * pi * hz * delay / 1e6)
    azimuth = azimuth_reading(np.column_stack((left, right)), rate, 10000)
    assert azimuth.delay_us == pytest.approx(delay, abs=0.035)
    assert azimuth.jitter_us < 0.02
    assert azimuth.coherence > 0.9999
    assert unwrap_delay(azimuth.delay_us, 3.1 + 1e6 / azimuth.frequency, azimuth.frequency) == pytest.approx(4.21, abs=0.035)
    assert not azimuth.clipped


@pytest.mark.parametrize('rate', (44_100, 48_000, 96_000))
def test_azimuth_follows_a_tape_carrier_with_strong_shared_frequency_modulation(rate):
    hz, flutter_hz, depth, delay = 10_000, 4, .0125, 12e-6
    t = times(rate, 1)

    def carrier(positions):
        phase = 2 * pi * hz * positions - hz * depth / flutter_hz * np.cos(2 * pi * flutter_hz * positions)
        return np.sin(phase)

    rng = np.random.default_rng(83)
    frames = np.c_[.3 * carrier(t), .54 * carrier(t - delay)] + rng.normal(0, .0005, (len(t), 2))
    reading = azimuth_reading(frames, rate, hz)
    assert reading.frequency == pytest.approx(hz, abs=.03)
    assert reading.delay_us == pytest.approx(delay * 1e6, abs=.02)
    assert min(reading.confidence_db) > 45
    assert reading.jitter_us < .02


@pytest.mark.parametrize('delay', (-127.35, -50.1, 0, 49.9, 174.5))
def test_a_lower_frequency_resolves_the_high_frequency_azimuth_cycle(delay):
    high_hz, low_hz = 10000, 1000
    fine = unwrap_delay(delay, 0, high_hz)
    assert resolve_azimuth_delay(fine, high_hz, 0.15, delay + 0.2, low_hz, 0.3) == pytest.approx(delay)


def test_azimuth_cycle_is_unresolved_for_inconsistent_or_ambiguous_tones():
    assert resolve_azimuth_delay(10, 10000, 0.25, 130, 1000, 1.0) is None
    assert resolve_azimuth_delay(49.7, 10000, 0.25, 499.8, 1000, 1.0) is None
    assert resolve_azimuth_delay(0, 10000, 30, 100, 1000, 25) is None


def test_intermodulation_selects_products_without_counting_harmonics():
    t = times()
    f1, f2 = 299.5, 3011.4
    def tone(frequency):
        return np.sin(2 * pi * frequency * t)
    left = 0.2 * tone(f1) + 0.08 * tone(f2) + 0.016 * tone(2 * f1) + 0.004 * tone(f2 + f1)
    right = 0.1 * tone(f1) + 0.04 * tone(f2) + 0.006 * tone(3 * f1) + 0.001 * tone(f2 - f1)
    reading = two_tone_reading(np.column_stack((left, right)), 48_000, 300, 3000)
    assert reading.frequencies == pytest.approx((f1, f2), abs=0.01)
    assert reading.distortion_percent == pytest.approx((5.0, 2.5), abs=0.02)
    with pytest.raises(ValueError, match='overlapping'):
        two_tone_reading(np.column_stack((left, right)), 48_000, 300, 310)


def test_intermodulation_measures_a_third_order_product_reflected_through_dc():
    times_s = times()
    first, second = 310, 3_000
    products = second - first, abs(2 * first - second), 2 * second - first
    samples = .2 * np.sin(2 * pi * first * times_s) + .08 * np.sin(2 * pi * second * times_s)
    for product, amplitude in zip(products, (.002, .003, .004), strict=True):
        samples += amplitude * np.sin(2 * pi * product * times_s)
    reading = two_tone_reading(samples, 48_000, first, second, method='difference')
    assert reading.products == pytest.approx(products, abs=.01)
    assert reading.distortion_percent == pytest.approx((100 * np.linalg.norm([.002, .003, .004]) / .08,), abs=.01)


def test_levels_remove_dc_and_report_headroom_and_octave_bands():
    t = times()
    tone = 0.2 * np.sin(2 * pi * 1000 * t)
    values = np.column_stack((tone + 0.03, 2 * tone))
    left, right = levels(values)
    assert left[0] == pytest.approx(20 * np.log10(0.2 / np.sqrt(2)), abs=0.01)
    assert left[1] == pytest.approx(20 * np.log10(0.23), abs=0.01)
    assert right[0] - left[0] == pytest.approx(6.0206)
    centers, db = band_curve(values, 48_000)
    peak_index = np.argmax(db[0])
    assert centers[peak_index] == 1000
    assert db[0, peak_index] == pytest.approx(left[0], abs=0.02)
    assert db[1, peak_index] - db[0, peak_index] == pytest.approx(6.0206)


def test_distortion_balance_does_not_reward_mistracking_in_both_channels():
    assert compare_distortion(20, 20, 3, balance=True) is None
    assert compare_distortion(0.4, 0.6, 3, balance=True) < compare_distortion(0.9, 1.2, 3, balance=True)
    assert compare_distortion(0.4, 0.6, 3) == 0.6


def test_alignment_preferences_keep_only_valid_step_overrides():
    from fluttery.preferences import normalize_settings

    settings = {'head-azimuth': {'1': {'tolerance_us': 3.5, 'offset_us': 7, 'seconds': True}},
                'tape-bias': {'2': {'tone_hz': 8000, 'level_dbfs': -26}, '3': {'target_db': 3},
                              '5': {'noise': 'pink', 'seconds': float('nan')}, '9': {'tone_hz': 1500}},
                'vta': {'1': {'tone_hz': 4000, 'second_hz': 2000, 'method': 'difference'}},
                'tracking-force': {'1': {'minimum_force': 6, 'seconds': 0.8}}, 'unknown': {'1': {'seconds': 1}}}
    expected = {'head-azimuth': {'1': {'tolerance_us': 3.5}},
                'tape-bias': {'2': {'tone_hz': 8000, 'level_dbfs': -26}, '3': {'target_db': 3},
                              '5': {'noise': 'pink'}},
                'vta': {'1': {'method': 'difference'}}, 'tracking-force': {'1': {'seconds': 0.8}}}
    assert normalize_alignment_options(settings) == expected
    assert normalize_settings({'alignmentOptions': settings})['alignmentOptions'] == expected


def test_alignment_reports_keep_labels_units_and_comparable_graph_points(tmp_path):
    from fluttery.reporting import (
        build_report,
        measurement_graph,
        measurement_summary,
        measurement_table,
    )
    from fluttery.sessions import load_session, new_session, save_session

    session = new_session()
    trials = [
        {'step': 'Measure the delay', 'setting': 'Start', 'metric': 'Delay (µs)', 'comparison': '10,000 Hz',
         'left': 4.25, 'right': 6.25, 'score': 4.25},
        {'step': 'Verify', 'setting': 'Next frequency', 'metric': 'Delay (µs)', 'comparison': '8,000 Hz',
         'left': 8.5, 'right': 10.5, 'score': 8.5},
        {'step': 'Measure the delay', 'setting': 'After', 'metric': 'Delay (µs)', 'comparison': '10,000 Hz',
         'left': 2.25, 'right': 4.25, 'score': 2.25},
    ]
    session['measurementLog'].append({'type': 'alignment', 'timestamp': 42, 'data': {
        'title': 'Tape Head Azimuth', 'workflow': 'head-azimuth', 'trials': trials}})
    path = tmp_path / 'alignment.fluttery'
    save_session(path, session)
    report = build_report(load_session(path), {})
    entry, = report.measurements
    assert '3 captured readings' in measurement_summary(entry)
    graph = measurement_graph(entry)
    assert graph.x_label == 'Comparable reading number'
    assert [float(value) for value in graph.traces[0]['y']] == [4.25, 2.25]
    headers, rows = measurement_table(entry)
    assert headers[1] == 'Setting'
    assert [row[1] for row in rows] == ['Start', 'Next frequency', 'After']
    assert [row[-1] for row in rows] == ['4.25', '8.5', '2.25']
    empty = {'type': 'alignment'}
    assert measurement_summary(empty) == 'Alignment readings. 0 captured readings.'
    assert measurement_graph(empty).x_label == 'Reading number'


def test_native_assistant_reads_the_live_source_and_saves_a_reading(monkeypatch):
    import os
    import time

    if os.getenv('FLUTTERY_GUI_TESTS') != '1':
        pytest.skip('requires an active desktop')
    import wx

    from fluttery import app as application
    from fluttery.alignment_ui import Trial
    from fluttery.audio import AudioEngine

    app = wx.App(False)
    wx.Log.SetActiveTarget(wx.LogStderr())
    monkeypatch.setattr(application, 'save_settings', lambda settings: None)
    frame = application.FlutteryFrame(audio=AudioEngine(), settings={
        'channels': 2, 'autoStartInput': False, 'enableUISounds': False, 'announceStatus': False,
    }, automatic_updates=False)
    app.SetTopWindow(frame)
    errors = []
    finished = []

    def exercise():
        try:
            frame.show_startup()
            assistant = frame.show_alignment('head-azimuth')
            assert (assistant._options_for_step(1).tone_hz, assistant._options_for_step(2).tone_hz) == (10_000, 1_000)
            assistant._move_step(1)
            assistant.setting.SetValue('Example tape deck')
            assistant.track.SetValue('10 kHz')
            # Exercise the actual device PCM queue contract using a synthetic
            # sound-card return and the native event/worker path.
            t = times(duration=0.62)
            frequency, delay = 10000, 4.2e-6
            waveform = np.column_stack((0.23 * np.sin(2 * pi * frequency * t),
                                        0.24 * np.sin(2 * pi * frequency * (t - delay))))
            deadline = time.monotonic() + 2.5
            while not assistant.capture_button.IsEnabled() and time.monotonic() < deadline:
                frame.audio.feed(waveform, 48_000)
                app.Yield()
                time.sleep(0.035)
            assert assistant.capture_button.IsEnabled()
            assert assistant.capture_button.GetLabel() == 'Capture Reading'
            assert assistant.trials_button.GetLabel() == 'Captured Readings…'
            assistant.capture_button.Command(wx.CommandEvent(wx.EVT_BUTTON.typeId, assistant.capture_button.GetId()))
            assistant.session_button.Command(wx.CommandEvent(wx.EVT_BUTTON.typeId, assistant.session_button.GetId()))
            entry, = frame.session['measurementLog']
            assert entry['type'] == 'alignment'
            capture, = entry['data']['trials']
            assert capture['left'] == pytest.approx(delay * 1e6, abs=0.05)
            assert any(command.label == 'Read Alignment Values' for command in frame.keyboard.commands(assistant).values())
            assistant._capture_trial()  # Repeat the same setting and test.
            assistant.track.ChangeValue('Other band')
            assistant._capture_trial()
            assert assistant.view.SetStringSelection('Captured Readings')
            assistant._change_graph()
            assert assistant.comparison_choice.IsShown()
            assert assistant.comparison_choice.GetCount() == 2
            assert assistant.plot.x_formatter(1) == 'Example tape deck'
            assistant.comparison_choice.SetSelection(0)
            assistant._choose_comparison(None)
            assert assistant.plot.x_ticks == ((1, 'Example tape deck'),)
            assert assistant.plot.x_formatter(1) == 'Example tape deck (2 readings)'
            assert len(assistant.plot._series[0].y) == 1
            assert assistant.plot._series[-1].label == 'Upper tolerance'

            loading = frame.show_alignment('phono-loading')
            loading._move_step(2)
            loading.setting.ChangeValue('47 kΩ')
            loading.trials.extend([
                Trial(loading.step.name, '47 kΩ', band, 'Relative response (dB)', left, right, None, '',
                      comparison=band, nominal_hz=5_000, target_db=0, reference_hz=1_000)
                for band, left, right in (('Band A', -1, -2), ('Band B', 4, 5), ('Band A', -3, -4))
            ])
            loading.view.SetStringSelection('Loading Response')
            loading._change_graph()
            assert loading.plot._series[0].y == pytest.approx([-2])
            assert loading.plot._series[1].y == pytest.approx([-3])

            frame.apply_settings(alignmentOptions={'tape-bias': {
                '1': {'tone_hz': 480, 'level_dbfs': -27}, '2': {'tone_hz': 8000, 'level_dbfs': -25},
            }})
            bias = frame.show_alignment('tape-bias')
            assert (bias._options_for_step(3).tone_hz, bias._options_for_step(3).level_dbfs) == (8000, -25)
            assert (bias._options_for_step(4).tone_hz, bias._options_for_step(4).level_dbfs) == (480, -27)
            assert bias._options_for_step(5).seconds == 1.5
            finished.append(True)
        except BaseException as error:  # noqa: BLE001 - Surface a failure outside wx's callback.
            errors.append(error)
        finally:
            frame.session_saved()
            frame.quit()
            app.ExitMainLoop()

    wx.CallAfter(exercise)
    app.MainLoop()
    if errors:
        raise errors[0]
    assert finished
