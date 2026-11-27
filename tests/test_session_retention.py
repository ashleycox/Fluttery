"""Round trips for retained runs, original samples, and missing readings."""

import csv
import io
import json
import os
import struct
import wave
from types import SimpleNamespace
from zipfile import ZipFile

import numpy as np
import pytest

from fluttery import staging
from fluttery.audio.processor import ANALYSIS_FRAMES
from fluttery.audio.recordings import RawSpeedCapture, analyse_wav, inspect_wav
from fluttery.audio.session_capture import SessionAudioCapture, write_input_wav
from fluttery.preferences import DEFAULTS, make_profile, normalize_settings
from fluttery.reporting import (
    build_report,
    csv_report,
    history_graph,
    html_report,
    json_report,
    measurement_graph,
    measurement_summary,
    measurement_table,
    pdf_report,
    spectrogram_graph,
)
from fluttery.session_setup import (
    RESET,
    recorded_setup,
    selected_setup,
    setting_differences,
    with_values,
)
from fluttery.sessions import archive_run, clear_run, load_session, new_session, save_session


@pytest.fixture(autouse=True)
def pending_writes(monkeypatch, tmp_path):
    directory = tmp_path / 'pending-writes'
    monkeypatch.setattr(staging, '_staging_directory', lambda: directory)
    return directory


def test_archived_run_keeps_input_blocks_and_original_settings(tmp_path):
    first = np.arange(512, dtype=np.float32).reshape(-1, 2) / 512
    second = -first[:, :1]
    recorder = SessionAudioCapture()
    recorder.append(first, 48_000)
    recorder.mark_gap('A capture interruption')
    recorder.append(second, 96_000)
    audio = recorder.finish()
    session = new_session()
    session.update(history=[{'timestamp': 1700000001, 'rms': .07}], inputAudio=audio,
                   runSettings={'targetFreq': 3150}, runRetention={'keepCompleteHistory': True})
    archive_run(session, settings={'targetFreq': 3000})
    clear_run(session)
    session.update(history=[{'timestamp': 1700000100, 'rms': .12}], settings={'targetFreq': 3000})
    path = tmp_path / 'retained.fluttery'
    save_session(path, session)

    with ZipFile(path) as archive:
        manifest = json.loads(archive.read('session.json'))
        assert manifest['version'] == '2.0' and 'sessionLog' not in manifest
        assert set(archive.namelist()) == {'session.json', 'input/1/0.pcmz', 'input/1/1.pcmz'}
    reopened = load_session(path)
    assert reopened['settings'] == {'targetFreq': 3000}
    assert reopened['history'][0]['rms'] == .12 and 'inputAudio' not in reopened
    previous = reopened['previousRuns'][0]
    assert previous['runSettings'] == {'targetFreq': 3150} and previous['history'][0]['rms'] == .07
    previous_audio = previous['inputAudio']
    assert previous_audio.incomplete_reason == 'A capture interruption'
    assert [block.decode() for block in previous_audio.blocks] == [first.tobytes(), second.tobytes()]
    for index, (blocks, samples) in enumerate(zip(previous_audio.segments(), (first, second), strict=True)):
        raw = io.BytesIO()
        write_input_wav(raw, blocks)
        wav = tmp_path / f'input-{index}.wav'
        wav.write_bytes(raw.getvalue())
        info = inspect_wav(wav)
        assert (info.rate, info.channels, info.frames) == ((48_000, 2, 256), (96_000, 1, 256))[index]
        assert raw.getvalue()[info.offset:] == samples.tobytes()


def test_historical_detector_values_apply_individually_or_as_a_complete_preset():
    setup = normalize_settings({})
    recorded = recorded_setup({'detectorTiming': {'preset': 'slow'}, 'duration': 30})
    choices = dict(setting_differences(recorded, setup))
    assert choices['detectorTiming', 'peakAttackTime'] == .05
    assert choices['duration',] == '30'

    active, warning = selected_setup(setup, {('detectorTiming', 'peakAttackTime'): .05})
    assert not warning
    assert active['detectorTiming'] == {'preset': 'custom', 'peakAttackTime': .05,
                                        'peakReleaseTime': 2.0, 'rmsTime': .3}
    assert setup['detectorTiming']['preset'] == 'nak'
    active, warning = selected_setup(setup, {('detectorTiming', 'preset'): 'slow'})
    assert active is None and 'preset' in warning
    active, warning = selected_setup(setup, choices)
    assert not warning and active['detectorTiming'] == recorded['detectorTiming']


def test_explicit_resets_remove_running_overrides_without_inventing_absent_legacy_settings():
    setup = normalize_settings({'alignmentOptions': {'head-azimuth': {'1': {'tone_hz': 3200, 'seconds': 1.25}}},
                                'benchLevelOptions': {'snr': {'band': 'voice'}}})
    saved = {'alignmentOptions': {'head-azimuth': {'1': {'tone_hz': 3200}}}, 'benchLevelOptions': {}}
    choices = dict(setting_differences(saved, setup))
    assert choices[('alignmentOptions', 'head-azimuth', '1', 'seconds')] is RESET
    assert all(value is RESET for value in choices.values())
    active, warning = selected_setup(setup, choices)
    assert not warning and active['benchLevelOptions'] == {}
    assert active['alignmentOptions'] == {'head-azimuth': {'1': {'tone_hz': 3200}}}
    assert not list(setting_differences({'targetFreq': setup['targetFreq']}, setup))

    updated = normalize_settings({**setup, 'alignmentOptions': {}})
    delta = dict(setting_differences(updated, setup, replace=True))
    assert normalize_settings(with_values(setup, delta)) == updated


def test_reset_can_restore_an_implicit_response_default_and_leave_other_values():
    from fluttery.audio.response import response_test_defaults

    setup = normalize_settings({'responseTestDefaults': {'tape_playback': {'duration': 9, 'lowHz': 30}}})
    active, warning = selected_setup(setup, {('responseTestDefaults', 'tape_playback', 'duration'): RESET})
    assert not warning
    assert active['responseTestDefaults']['tape_playback']['duration'] == response_test_defaults('tape_playback')['duration']
    assert active['responseTestDefaults']['tape_playback']['lowHz'] == 30


def test_negotiated_rate_round_trips_beside_common_audio_choices():
    assert normalize_settings({'sampleRate': 32_000.0})['sampleRate'] == 32_000
    assert normalize_settings({'sampleRate': '384000'})['sampleRate'] == 384_000
    for rate in (None, 0, 42, 20_000.5, float('inf'), 384_001):
        assert normalize_settings({'sampleRate': rate})['sampleRate'] == 0


def test_calibration_interlock_does_not_let_temporary_route_escape_its_session():
    from fluttery.app import FlutteryFrame

    messages = []
    permanent = normalize_settings({})
    frame = SimpleNamespace(_calibrating=False, _persistent_settings=permanent,
                            settings={**permanent, 'sampleRate': 32_000, 'interfaceLoopbackPendingDisconnect': True},
                            announce=messages.append)
    assert not FlutteryFrame._can_restore_working_setup(frame)
    assert 'temporary audio route' in messages[-1]
    frame.settings['interfaceLoopbackPendingDisconnect'] = False
    assert FlutteryFrame._can_restore_working_setup(frame)


def test_documents_save_only_their_own_preference_and_profile_changes(monkeypatch):
    from fluttery import app

    settings = normalize_settings({})
    documents = app._SessionDocuments(None)
    documents.preferences = settings
    writes = []
    monkeypatch.setattr(app, 'save_settings', writes.append)
    audio = SimpleNamespace(persisted_loopback=lambda _frame: True)
    first = SimpleNamespace(_persistent_settings={**settings, 'targetFreq': 3150,
                                                  'measurementProfiles': [make_profile('A', {}, 'alpha')]},
                            _last_saved_settings=settings, _audio_ownership=audio)
    second = SimpleNamespace(_persistent_settings={**settings, 'meterDamping': 1.25,
                                                   'measurementProfiles': [make_profile('B', {}, 'beta')]},
                             _last_saved_settings=settings, _audio_ownership=audio)

    documents.save_preferences(first)
    documents.save_preferences(second)
    shared = writes[-1]
    assert shared['targetFreq'] == 3150 and shared['meterDamping'] == 1.25
    assert [record['id'] for record in shared['measurementProfiles']] == ['alpha', 'beta']
    assert shared['interfaceLoopbackPendingDisconnect']
    assert second._persistent_settings['targetFreq'] == settings['targetFreq']

    first._persistent_settings['measurementProfiles'] = []
    documents.save_preferences(first)
    assert [record['id'] for record in writes[-1]['measurementProfiles']] == ['beta']


def test_application_quit_checks_every_session_before_closing_any():
    from fluttery.app import _SessionDocuments

    events = []

    def document(label, *, consent):
        def confirm(*, closing):
            assert closing
            events.append(('confirm', label))
            return consent

        def quit(*, confirmed):
            assert confirmed
            events.append(('close', label))

        return SimpleNamespace(confirm_replace_session=confirm, quit=quit)

    documents = _SessionDocuments(None)
    first = document('first', consent=True)
    documents.frames = [first, document('second', consent=False)]
    assert not documents.quit()
    assert events == [('confirm', 'first'), ('confirm', 'second')]

    events.clear()
    documents.frames[1] = document('second', consent=True)
    assert documents.quit()
    assert events == [('confirm', 'first'), ('confirm', 'second'), ('close', 'first'), ('close', 'second')]


def test_closing_saves_live_turntable_and_distortion_after_choosing_a_file(monkeypatch, tmp_path):
    from fluttery import app
    from fluttery.results import ResultsPage

    events = []
    answer = app.wx.ID_CANCEL

    class CloseDialog:
        def __init__(self, parent, message, title, style):
            assert 'stop its measurements' in message

        def __enter__(self):
            return self

        def __exit__(self, *_error):
            return False

        def SetYesNoCancelLabels(self, *_labels):
            pass

        def ShowModal(self):
            return answer

    monkeypatch.setattr(app.wx, 'MessageDialog', CloseDialog)
    distortion = SimpleNamespace(running=True, refresh_measurement=lambda _snapshot: events.append('capture THD'))
    distortion._toggle = lambda: events.append('pause THD')
    turntable = SimpleNamespace(is_capturing=True, has_pending_readings=True)
    session = new_session()

    def finish_turntable():
        events.append('finish Turntable')
        session['history'].append({'timestamp': 1700000001, 'rms': .07})

    turntable.finish_session_readings = finish_turntable
    frame = SimpleNamespace(is_measuring=False, _session_dirty=False, dialog_parent=None, session=session,
                            pages={'turntable': turntable, 'thd': distortion},
                            audio=SimpleNamespace(snapshot=dict), session_settings=dict,
                            session_saved=lambda: events.append('save session'), announce=lambda _message: None)
    frame._finalize_session_measurements = lambda: app.FlutteryFrame._finalize_session_measurements(frame)
    path = None
    results = SimpleNamespace(controller=frame, session_path=None, _file_path=lambda *_args, **_kwargs: path,
                              _refresh_status=lambda: None, _update_commands=lambda: events.append('update title'))
    results.save_session = lambda **kwargs: ResultsPage.save_session(results, **kwargs)
    frame.results = results

    assert not app.FlutteryFrame.confirm_replace_session(frame, closing=True)
    assert events == [] and session['history'] == []
    answer = app.wx.ID_YES
    assert not app.FlutteryFrame.confirm_replace_session(frame, closing=True)
    assert events == [] and session['history'] == []

    path = tmp_path / 'recording.fluttery'
    assert app.FlutteryFrame.confirm_replace_session(frame, closing=True)
    assert events == ['capture THD', 'pause THD', 'finish Turntable', 'save session', 'update title']
    assert load_session(path)['history'][0]['rms'] == .07


def test_replacing_session_rejects_active_instruments_and_clears_staged_readings():
    from fluttery.app import FlutteryFrame

    events = []
    turntable = SimpleNamespace(is_capturing=True, reset_session_readings=lambda: events.append('clear readings'))
    distortion = SimpleNamespace(running=False)
    original = new_session()
    replacement = new_session()
    frame = SimpleNamespace(pages={'turntable': turntable, 'thd': distortion}, is_measuring=False,
                            _can_restore_working_setup=lambda: True, _session_overrides={},
                            session=original, results=SimpleNamespace(refresh_results=lambda: None),
                            announce=events.append)

    def check_blocked():
        # File > Open preflights the replacement, while File > Clear uses the
        # same final guard before resetting the document.
        assert not FlutteryFrame.confirm_replace_session(frame)
        assert not FlutteryFrame.session_loaded(frame, replacement)
        assert frame.session is original
        assert all('Stop the current measurement' in event for event in events)

    check_blocked()
    turntable.is_capturing = False
    distortion.running = True
    check_blocked()
    distortion.running = False
    assert FlutteryFrame.session_loaded(frame, replacement)
    assert frame.session is replacement and events[-1] == 'clear readings'


def test_auto_reference_tracking_stays_inside_a_temporary_session_setup():
    from fluttery.app import FlutteryFrame

    permanent = normalize_settings({'targetFreq': 1000, 'useAutoTarget': False})
    frame = SimpleNamespace(settings={**permanent, 'targetFreq': 3000, 'useAutoTarget': True},
                            _persistent_settings=permanent, _session_overrides={('useAutoTarget',): True},
                            announce=lambda _message: None)

    def set_working_setup(saved, overrides):
        assert saved is permanent
        frame.settings = with_values(saved, overrides)
        return True

    frame._change_settings = set_working_setup
    FlutteryFrame._remember_auto_target(frame, {'locked': True, 'requestedTarget': 3000, 'activeTarget': 3150})
    assert frame.settings['targetFreq'] == 3150
    assert frame._persistent_settings['targetFreq'] == 1000


def test_imported_wav_retains_analysis_provenance_without_inheriting_the_live_route():
    from pathlib import Path

    from fluttery.app import FlutteryFrame

    settings = normalize_settings({'inputDevice': 'Live input', 'outputDevice': 'Live output',
                                   'sampleRate': 32_000, 'correctionFactor': 1.0001, 'targetFreq': 3150})
    frame = SimpleNamespace(is_measuring=False, session=new_session(), results=SimpleNamespace(session_path=None),
                            _keep_previous_run=lambda _options: False, session_changed=lambda: None,
                            navigate=lambda *_args: None, announce=lambda _message: None)
    result = {'channel': 2, 'rate': 48_000, 'start': 0, 'metrics': {}, 'history': [], 'spectrum': {}}
    assert FlutteryFrame.use_offline_result(frame, Path('input.wav'), result, settings)
    assert frame.session['runSettings']['targetFreq'] == 3150
    assert not {'inputDevice', 'outputDevice', 'sampleRate', 'correctionFactor'} & frame.session['settings'].keys()
    assert frame.session['recordedAudio']['sampleRate'] == 48_000
    assert frame.session['recordedAudio']['correctionFactor'] == 1.0001


def test_archives_a_run_that_contains_only_signal_losses(tmp_path):
    session = new_session()
    losses = [{'start': 1700000000, 'end': 1700000001, 'reason': 'Input interruption'}]
    session['signalLoss'] = losses
    archive_run(session)
    clear_run(session)
    path = tmp_path / 'loss-only.fluttery'
    save_session(path, session)
    assert load_session(path)['previousRuns'][0]['signalLoss'] == losses


def test_cleanup_skips_active_writer_and_unrelated_files(tmp_path, pending_writes):
    destination = tmp_path / 'output.fluttery'
    with staging.atomic_output(destination) as stream:
        stream.write(b'completed capture')
        active = next(tmp_path.glob('.fluttery-stage-*.part'))
        staging.cleanup_session_staging()
        assert active.is_file()
    assert destination.read_bytes() == b'completed capture'
    assert not active.exists() and not list(pending_writes.iterdir())

    identifier = 'a' * 32
    abandoned = tmp_path / f'.fluttery-stage-{identifier}.part'
    journal = pending_writes / f'{identifier}.json'
    abandoned.write_bytes(b'partial output')
    journal.write_text(json.dumps({'stage': str(abandoned)}), encoding='utf-8')
    unrelated = tmp_path / f'.fluttery-stage-{"b" * 32}.part'
    unrelated.write_bytes(b'keep')
    staging.cleanup_session_staging()
    assert unrelated.read_bytes() == b'keep'
    assert not abandoned.exists() and not journal.exists()

    with pytest.raises(ValueError, match='failed'), staging.atomic_output(destination) as stream:
        stream.write(b'partial replacement')
        raise ValueError('failed')
    assert destination.read_bytes() == b'completed capture'
    assert list(tmp_path.glob('.fluttery-stage-*.part')) == [unrelated]
    assert not list(pending_writes.iterdir())


@pytest.mark.skipif(os.name != 'posix', reason='requires POSIX modes')
def test_atomic_output_preserves_existing_private_permissions(tmp_path):
    destination = tmp_path / 'private.fluttery'
    destination.touch(mode=0o600)
    with staging.atomic_output(destination) as stream:
        stream.write(b'replacement')
        stage, = tmp_path.glob('.fluttery-stage-*.part')
        assert stage.stat().st_mode & 0o777 == 0o600
    assert destination.stat().st_mode & 0o777 == 0o600


def test_report_preserves_lock_breaks_and_missing_values(tmp_path):
    base = 1700000000
    session = new_session()
    session.update(history=[{'timestamp': base, 'currentFreq': 3150, 'peak': 0, 'rms': 0},
                            {'timestamp': base + 1, 'locked': False, 'peak': 0, 'rms': 0, 'currentFreq': 1},
                            {'timestamp': base + 2, 'currentFreq': 3151, 'peak': .1}],
                   spectra=[{'timestamp': base + offset, 'spectrumEncoding': 'db', 'spectrumBinHz': 2,
                             'spectrum': [-60, -25, -80]} for offset in (0, 2)],
                   signalLoss=[{'start': base + .9, 'end': base + 1.1, 'reason': 'Reference tone unavailable'}])
    path = tmp_path / 'break.fluttery'
    save_session(path, session)
    reopened = load_session(path)
    history = reopened['history']
    assert history[-1]['rms'] is None
    plot = history_graph('graph', history, 'detectorPeak')
    np.testing.assert_equal(plot.traces[0]['y'], [0, np.nan, .1])
    plot = spectrogram_graph(history, spectra=reopened['spectra'], signal_loss=reopened['signalLoss'])
    times, _, levels = plot.heatmap
    np.testing.assert_equal(times, [0, 1, 2])
    assert np.isnan(levels[:, 1]).all() and np.isfinite(levels[:, [0, 2]]).all()

    settings = {**DEFAULTS, 'exportSettings': {**DEFAULTS['exportSettings'], 'sessionData': {
        'stats': True, 'table': True, 'graph': True, 'spectrogram': True,
    }}}
    report = build_report(reopened, settings)
    rows = list(csv.reader(io.StringIO(csv_report(report).decode('utf-8'))))
    assert any(row[0].startswith('2023-11-14T') and row[1:] == ['', '', '', ''] for row in rows if row)
    assert ['Signal losses'] in rows
    assert json.loads(json_report(report))['spectra'] == reopened['spectra']
    html = html_report(report)
    assert b'No reading.' in html and b'null' in html and b'Reference tone unavailable' in html
    assert pdf_report(report).startswith(b'%PDF')


def test_offline_analysis_preserves_selected_input_bytes_and_loss_intervals(tmp_path):
    rate = 48_000
    timeline = np.arange(rate * 5) / rate
    reference = np.rint(2**28 * np.sin(2 * np.pi * 3150 * timeline)).astype('<i4')
    reference[2 * rate:3 * rate] = 0
    samples = np.column_stack((reference, np.arange(len(timeline), dtype='<i4')))
    path = tmp_path / 'tone.wav'
    with wave.open(str(path), 'wb') as output:
        output.setparams((2, 4, rate, 0, 'NONE', 'not compressed'))
        output.writeframes(samples.tobytes())
    settings = {**DEFAULTS, 'targetFreq': 3150, 'useAutoTarget': False,
                'keepCompleteHistory': True, 'keepSignalLossDetails': True}
    result = analyse_wav(path, settings, start=.25, duration=4.5, retain_input=True)
    assert abs(len(result['history']) - 4.5 * rate / ANALYSIS_FRAMES) <= 1
    assert len(result['spectra']) >= 2
    assert any(event['start'] < 2.5 < event['end'] for event in result['signalLoss'])
    audio = result['inputAudio']
    assert audio.seconds == pytest.approx(4.5) and all(chunk.encoding == 'pcm32' for chunk in audio.blocks)
    assert b''.join(block.decode() for block in audio.blocks) == samples[rate // 4:rate * 19 // 4].tobytes()


def test_raw_speed_wav_records_invalid_frame_ranges_inside_pcm(tmp_path):
    path = tmp_path / 'speed.wav'
    capture = RawSpeedCapture(path, 12_000)
    capture.append([0, 9], [True, False])
    capture.append([6, 3, 4], [False, True, True])
    capture.finish()
    assert capture.done.wait(5) and capture.saved, capture.error
    with wave.open(str(path), 'rb') as output:
        assert output.getnframes() == 5
        assert np.frombuffer(output.readframes(5), dtype='<i4')[:3].tolist() == [0, 0, 0]
    data = path.read_bytes()
    assert struct.unpack_from('<I', data, 4)[0] + 8 == len(data)
    tag = data.index(b'flty')
    size, = struct.unpack_from('<I', data, tag + 4)
    assert json.loads(data[tag + 8:tag + 8 + size]) == {'version': 1, 'invalidFrames': [[1, 3]]}


def test_new_instrument_reports_keep_bounds_and_invalid_measurements():
    separation = {'type': 'crosstalk', 'timestamp': 1700000000, 'data': [
        {'direction': 'Left to right', 'frequencyHz': 1000, 'separationDb': 70.25, 'limited': True},
        {'direction': 'Right to left', 'frequencyHz': 1000, 'separationDb': 39},
    ]}
    sweep = {'type': 'bench-distortion', 'timestamp': 1700000000, 'data': {'axis': 'frequencyHz', 'data': [
        {'channel': 1, 'frequencyHz': 200, 'thd': .1},
        {'channel': 1, 'frequencyHz': 1000, 'thd': None, 'clipping': True},
        {'channel': 1, 'frequencyHz': 6000, 'thd': .2},
    ]}}
    intermodulation = {'type': 'bench-imd', 'timestamp': 1700000000, 'data': {'method': 'ccif', 'data': [
        {'firstHz': 19000, 'secondHz': 20000, 'imdPercent': .0023, 'products': [
            {'product': 'Difference', 'frequencyHz': 1000, 'levelDbc': -82, 'floorDbc': -106},
        ]},
    ]}}
    assert 'at least 70.2 dB' in measurement_summary(separation)
    assert measurement_table(separation)[1][0][-2:] == ('≥70.250', 'Noise-limited')
    np.testing.assert_equal(measurement_graph(sweep).traces[0]['y'], [.1, np.nan, .2])
    assert measurement_table(sweep)[1][1][-1] == 'Clipped'
    assert '0.0023%' in measurement_summary(intermodulation)
    assert measurement_table(intermodulation)[1][0][-4:] == ('1000', '-82', '-106', 'Measured')

    session = new_session()
    session['measurementLog'] = [separation, sweep, intermodulation]
    report = build_report(session, DEFAULTS)
    csv = csv_report(report).decode()
    assert '≥70.250' in csv and 'Difference' in csv and 'Clipped' in csv


@pytest.mark.skipif(os.environ.get('FLUTTERY_GUI_TESTS') != '1', reason='requires a native desktop')
def test_results_reveals_retained_runs_and_uses_their_own_data():
    import wx

    from fluttery.results import ResultsPage

    app = wx.App(False)
    wx.Log.SetActiveTarget(wx.LogStderr())
    window = wx.Frame(None, title='Fluttery Results', size=(950, 620))
    session = new_session()
    reviews = []
    controller = SimpleNamespace(settings=DEFAULTS, session=session, session_settings=lambda: DEFAULTS,
                                 review_session_setup=lambda settings: reviews.append(settings) or True,
                                 workspace=SimpleNamespace(update_commands=lambda: None))
    panel = ResultsPage(window, controller)
    window.Show()
    app.Yield()
    try:
        assert not panel.run_choice.IsShownOnScreen()
        session['history'] = [{'timestamp': 1700000001, 'rms': .07, 'peak': .1, 'bufferPeak': .1}]
        session['signalLoss'] = [{'start': 1700000001, 'end': 1700000003, 'reason': 'No reference tone'}]
        archive_run(session, settings={'graphSource': 'rawBlockPeak'})
        clear_run(session)
        session['history'] = [{'timestamp': 1700000020, 'rms': .12, 'peak': .15, 'bufferPeak': .15}]
        panel.set_view('stats')
        app.Yield()
        assert panel.run_choice.IsShownOnScreen() and panel.run_choice.GetCount() == 2
        panel.run_choice.SetSelection(1)
        panel._choose_run(None)
        assert '0.0700 %' in panel.summary.GetValue()
        assert 'Read Signal Losses…' in [label for _, label, _ in panel.desktop_options()]
        assert 'Review Saved Run Settings…' in [label for _, label, _ in panel.desktop_options()]
        assert panel.review_run_settings()
        assert reviews == [{'graphSource': 'rawBlockPeak'}]
        report = panel._report()
        assert report.history[0]['rms'] == .07
        assert report.graph_source == 'rawBlockPeak'
        assert report.run_label.startswith('Run 1')
        assert b'Saved run,Run 1' in csv_report(report)
        assert json.loads(json_report(report))['settings']['graphSource'] == 'rawBlockPeak'
        panel.set_view('graph')
        plot = panel.plots['graph']
        plot._set_marker('A', (0, 0))
        assert plot.snapshot()['markers']
        panel.run_choice.SetSelection(0)
        panel._choose_run(None)
        assert not plot.snapshot()['markers']
        panel.set_view('stats')
        assert '0.1200 %' in panel.summary.GetValue()
    finally:
        window.Destroy()
        app.Yield()


@pytest.mark.skipif(os.environ.get('FLUTTERY_GUI_TESTS') != '1', reason='requires a native desktop')
def test_live_results_reveal_dropouts_and_keep_the_saved_spectrum(tmp_path, monkeypatch):
    import wx

    from fluttery import results

    app = wx.GetApp() or wx.App(False)
    window = wx.Frame(None, title='Synthetic Results', size=(950, 620))
    session = new_session()
    session['history'] = [{'timestamp': 1700000000, 'spectrum': [-79, -84], 'spectrumEncoding': 'db',
                           'spectrumBinHz': 1.0, 'spectrumStartHz': 0.0}]
    controller = SimpleNamespace(settings=DEFAULTS, session=session)
    panel = results.ResultsPage(window, controller)
    window.Show()
    app.Yield()
    current = 10.0
    monkeypatch.setattr(results, 'time', SimpleNamespace(monotonic=lambda: current, time=lambda: 1700000000 + current))

    def snapshot(levels=()):
        return {'health': {'inputActive': True},
                'flutter_spectrum': {'db': levels, 'frequencies': [0., 1.] if levels else []}}

    try:
        panel.view_choice.SetFocus()
        panel.refresh_live(snapshot([-40, -50]))
        plot = panel.plots['fft']
        np.testing.assert_array_equal(plot.snapshot()['traces'][0]['y'], [-40, -50])
        panel.refresh_live(snapshot())
        assert plot.snapshot() is None
        assert plot.readout.GetValue() == results.LIVE_SPECTRUM_WAITING

        panel.set_view('spectrogram')
        panel.refresh_live(snapshot([-39, -44]))
        current = 10.05
        panel.refresh_live(snapshot())
        current = 10.32
        panel.refresh_live(snapshot())
        panel.refresh_results()  # The normal refresh must keep the live gap visible.
        plot = panel.plots['spectrogram']
        time, _, values = plot.snapshot()['heatmap']
        np.testing.assert_allclose(time, [0, .05, .32])
        assert np.isnan(values[:, 1:]).all()
        assert 'No level measurement in the latest column.' in plot.readout.GetValue()

        current = 10.39
        panel.refresh_live(snapshot([-24, -33]))
        time, _, values = plot.snapshot()['heatmap']
        np.testing.assert_allclose(time, [0, .05, .32, .39])
        assert np.isnan(values[:, 1:3]).all()
        np.testing.assert_array_equal(values[:, -1], [-24, -33])

        panel.session_path = tmp_path / 'recorded.fluttery'
        panel.set_view('fft')
        panel.refresh_live(snapshot())
        np.testing.assert_array_equal(panel.plots['fft'].snapshot()['traces'][0]['y'], [-79, -84])
    finally:
        window.Destroy()
        app.Yield()
