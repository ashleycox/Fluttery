"""Run from an active desktop with FLUTTERY_GUI_TESTS=1."""

import os
import time

import pytest

pytestmark = pytest.mark.skipif(os.getenv('FLUTTERY_GUI_TESTS') != '1', reason='requires an active desktop')


def test_gui_runs_loopback_measurement_and_reopens_session(monkeypatch, tmp_path):
    import wx

    driver = wx.GetApp() or wx.App(False)
    # Keep native toolkit diagnostics in the test output instead of queuing
    # a system log dialog at interpreter cleanup.
    wx.Log.SetActiveTarget(wx.LogStderr())
    frame = make_desktop_frame(driver, monkeypatch)
    frame._initial_setup_pending = True
    frame.Show()
    errors, completed = [], []

    def exercise():
        try:
            exercise_desktop(frame, driver, monkeypatch, tmp_path)
            completed.append(True)
        except BaseException as error:  # noqa: BLE001 - Propagate test failures out of the native callback.
            errors.append(error)
        finally:
            driver.ExitMainLoop()

    # The native loop lets Cocoa finish activation and focus changes when the
    # smoke test opens the first-run dialog, Settings and the manual in turn.
    wx.CallAfter(exercise)
    driver.MainLoop()
    if errors:
        raise errors[0]
    assert completed


def make_desktop_frame(driver, monkeypatch):
    from fluttery import app as application
    from fluttery.audio import AudioEngine

    class SoftwareAudio(AudioEngine):
        """Exercise the normal hardware failure fallback without playing a tone in the room."""

        def devices(self):
            return []

        def start(self, *args, input_enabled=True, output_enabled=True, **kwargs):
            if input_enabled or output_enabled:
                raise RuntimeError('No physical sound devices in the smoke test.')
            return super().start(*args, input_enabled=False, output_enabled=False, **kwargs)

    monkeypatch.setattr(application, 'save_settings', lambda settings: None)
    frame = application.FlutteryFrame(audio=SoftwareAudio(), settings={
        'targetFreq': 3150, 'channels': 2, 'useAutoTarget': False,
        'enableUISounds': False, 'checkForUpdates': False, 'announceStatus': False, 'autoStartInput': True,
    }, automatic_updates=False)
    driver.SetTopWindow(frame)
    return frame


def exercise_desktop(frame, driver, monkeypatch, tmp_path):
    import numpy as np
    import wx

    from fluttery import app as application
    from fluttery.desktop import TAB_CAPTIONS, TOOL_KEYS
    from fluttery.preferences import TAB_VALUES
    from fluttery.sessions import load_session

    frame.show_startup(offer_setup=True)
    try:
        verify_setup_wizard(frame, driver, monkeypatch)
        assert frame.workspace.layout == 'windows'
        assert frame.GetToolBar().GetToolsCount() > 0
        assert get_menu(frame, 'Tools')
        if wx.Platform == '__WXMAC__':
            common = frame.workspace._common_bar
            help_menu = common.GetMenu(common.FindMenu('Help'))
            checked = []
            with monkeypatch.context() as patch:
                patch.setattr(frame, 'check_updates', lambda *, manual: checked.append(manual))
                item = get_item(help_menu, 'Check for Updates…')
                help_menu.ProcessEvent(wx.CommandEvent(wx.EVT_MENU.typeId, item.GetId()))
            assert checked == [True]
        invoke(frame, get_item(get_menu(frame, 'Window'), 'Oscilloscope'))
        assert frame.current_page == 'scope'
        assert frame.pages['scope'].GetTopLevelParent() is frame
        invoke(frame, get_item(get_menu(frame, 'Scope'), 'XY Phase'))
        assert frame.pages['scope'].mode.GetSelection() == 1
        for tab in TAB_VALUES:
            frame.navigate(tab)
            assert frame.current_page == tab
            frame._tick(None)
            driver.Yield()
        settings_window = frame.workspace.windows['settings']
        assert settings_window is not frame and settings_window.IsShown()
        assert frame.settings_page.GetTopLevelParent() is settings_window
        assert settings_window.GetStatusBar() is None
        frame.announce('Settings action completed.')
        assert frame.GetStatusBar().GetStatusText() == 'Settings action completed.'
        verify_keyboard_and_help(frame, settings_window, driver, monkeypatch)
        verify_legacy_settings_workflows(frame, settings_window, driver, monkeypatch)
        verify_manual_update_reuses_background_check(frame, driver, monkeypatch)
        verify_input_without_playback(frame, monkeypatch)
        verify_deferred_audio_actions(frame, monkeypatch)
        verify_settings_actions_keep_unrelated_drafts(frame, monkeypatch)
        settings_window.Close()
        driver.Yield()
        assert not settings_window.IsShown()
        frame.navigate('generate')
        generator = frame.pages['generate']
        generator.loopback.SetValue(True)
        generator._toggle_loopback()
        generator._toggle_generator()
        assert frame.audio.running and not frame.audio.output_enabled
        feedback = []
        monkeypatch.setattr(frame.sounds, 'play', lambda appearance, name: feedback.append(name))
        assert frame.apply_settings(enableUISounds=True)
        frame.navigate('measure')
        action = frame._toolbar_ids['measure']
        frame.ProcessWindowEvent(wx.CommandEvent(wx.EVT_TOOL.typeId, int(action)))
        assert frame.is_measuring and 'start' in feedback
        verify_cancelled_quit_keeps_measuring(frame, monkeypatch)
        focus = wx.FocusEvent(wx.wxEVT_SET_FOCUS, frame.settings_page.categories.GetId())
        focus.SetEventObject(frame.settings_page.categories)
        frame._sound_filter.FilterEvent(focus)
        assert 'focus' in feedback
        assert frame.apply_settings(enableUISounds=False)
        current_session = frame.session
        original_measure = frame.pages['measure']
        original_generator = frame.pages['generate']
        layouts = get_item(get_menu(frame, 'View'), 'Workspace Layout').GetSubMenu()
        invoke(frame, get_item(layouts, 'Tabbed Workspace'))
        driver.Yield()
        assert frame.workspace.layout == 'tabs'
        assert frame.book.GetPageCount() == len(TOOL_KEYS)
        assert [frame.book.GetPageText(i) for i in range(frame.book.GetPageCount())] == list(TAB_CAPTIONS)
        assert frame.pages['measure'] is original_measure and frame.pages['generate'] is original_generator
        assert original_generator.GetTopLevelParent() is frame
        assert frame.session is current_session and frame.is_measuring
        assert frame.audio.running and frame.audio.snapshot()['generator']['enabled']
        scope_window = frame.workspace.new_window(frame)
        assert frame.pages['scope'].GetTopLevelParent() is scope_window and scope_window is not frame
        if wx.Platform == '__WXMAC__':
            frame.workspace.navigate('scope', 'xy', frame=scope_window)
            driver.frame = frame
            application.FlutteryApp.MacReopenApp(driver)
            assert frame.current_page == 'scope'
            assert frame.book.GetPageText(frame.book.GetSelection()) == 'Measure'
        frame.navigate('settings', 'audio')
        assert frame.settings_page.GetTopLevelParent() is settings_window and settings_window.IsShown()
        assert frame.settings_page.book.GetSelection() == 1
        layouts = get_item(get_menu(settings_window, 'View'), 'Workspace Layout').GetSubMenu()
        invoke(settings_window, get_item(layouts, 'Single Tool per Window'))
        driver.Yield()
        assert frame.workspace.layout == 'windows'
        assert original_generator.GetParent() is frame.workspace.parking
        assert frame.session is current_session and frame.is_measuring
        settings_window.Close()
        wait_for_steady_tone(frame, driver)
        frame._tick(None)
        assert frame.session['history']
        frame.workspace.navigate('results', 'graph', frame=scope_window)
        displayed = frame.results._plot_keys['graph']
        deadline = time.monotonic() + 2
        while frame.results._plot_keys['graph'] == displayed and time.monotonic() < deadline:
            wx.MilliSleep(25)
            driver.Yield()
            frame._tick(None)
        assert frame.results._plot_keys['graph'] != displayed and frame.is_measuring
        sample_before = frame.session['history'][-1]['timestamp']
        assert sample_before > 1_700_000_000
        assert frame.apply_settings(sampleRate=96000)
        wait_for_steady_tone(frame, driver)
        frame._tick(None)
        assert frame.audio.snapshot()['sample_rate'] == 96000
        assert frame.session['history'][-1]['timestamp'] > sample_before
        frame.stop_measurement()
        locked = [row for row in frame.session['history'] if row['locked']]
        assert locked and abs(locked[-1]['currentFreq'] - 3150) < 1
        filename = tmp_path / 'loopback.fluttery'
        frame.results.session_path = filename

        def report_save_error(_title, error):
            raise error

        with monkeypatch.context() as patch:
            patch.setattr(frame.results, '_show_error', report_save_error)
            assert frame.results.save_session()
        assert load_session(filename)['history'][-1]['locked']
        from fluttery.session_setup import SessionSetupDialog

        with monkeypatch.context() as patch:
            patch.setattr(SessionSetupDialog, 'ShowModal', lambda _dialog: wx.ID_CANCEL)
            frame.open_path(filename)
        assert frame.current_page == 'results'
        assert frame.results.session_path == filename
        assert frame.session['history'][-1]['activeTarget'] == 3150
        verify_comparison_navigation(frame, driver, monkeypatch, filename)
        verify_plot_image_actions(frame, driver, monkeypatch, tmp_path)
        frame.confirm = lambda *args: True
        previous_history = frame.session['history']
        assert frame.start_measurement()
        assert frame.session['history'] is not previous_history and previous_history
        assert frame.results.session_path is None
        generator._toggle_generator()
        frame._tick(None)
        assert not frame.is_measuring
        assert 'input was lost' in frame.GetStatusBar().GetStatusText()
        verify_concurrent_measurement_readings(frame, monkeypatch)
        assert frame.apply_settings(useAutoTarget=True)
        target_field = frame.settings_page.fields['targetFreq']
        target_field[1](1234)
        rate = 48000
        frame.audio.feed(0.4 * np.sin(2 * np.pi * 3000 * np.arange(rate) / rate), sample_rate=rate)
        frame._tick(None)
        assert frame.settings['targetFreq'] == 3000
        assert frame.pages['generate'].preset.GetSelection() == 0
        assert target_field[0]() == 1234  # The open Settings window keeps its unrelated draft.

        frame.session['history'].append({'timestamp': time.time(), 'peak': 0.1})
        frame.log_measurement('thd', [{'timestamp': time.time(), 'thd': 0.2, 'thdn': 0.3}])
        frame.session['metadata']['equipment'] = 'Reference deck'
        frame.results.session_path = filename
        frame.workspace.navigate('measure', frame=frame)
        command = get_item(get_menu(frame, 'Wow & Flutter'), 'Clear Wow and Flutter History')
        invoke(frame, command)
        assert not frame.session['history'] and frame.session['measurementLog']
        assert frame.session['metadata']['equipment'] == 'Reference deck' and frame.results.session_path == filename
        verify_rolling_sample_table(frame, monkeypatch)
        frame.navigate('measure')
        verify_bundled_manual(frame, driver, monkeypatch)
        verify_frequency_sweep(frame, driver, monkeypatch, tmp_path)
        verify_bench_instruments(frame, driver, monkeypatch, tmp_path)
        verify_spectrum_and_distortion(frame)
        verify_session_setup(frame, monkeypatch)
    finally:
        if frame.is_measuring:
            frame.stop_measurement()
        frame.session_saved()
        frame.quit()
        driver.Yield()


def verify_concurrent_measurement_readings(frame, monkeypatch):
    # Another analysis block can arrive after the GUI fetched its spectrum.
    # Keep the spectrum on the corresponding block and assess each peak using
    # the weighting in effect when that particular block was measured.
    first = {'sequence': 1, 'timestamp': 1., 'locked': True, 'rmsSeconds': 1.,
             'weighting': 'aes6', 'peak': .10, 'aes6Peak': .24}
    second = {**first, 'sequence': 2, 'timestamp': 1.1, 'locked': False}
    session = {'history': []}
    with monkeypatch.context() as patch:
        for name, value in {
            'session': session, 'is_measuring': True, '_last_sequence': None, '_run_options': {},
            '_run_clock_origin': 100., '_run_stream_offset': 0., '_run_spectrum_time': -float('inf'),
            '_run_worst': 0., '_run_valid': False, '_run_lost_tone': False, '_tape_end_detector': None,
        }.items():
            patch.setattr(frame, name, value)
        patch.setattr(frame.audio, 'drain_measurements', lambda: [first, second])
        frame._record({'metrics': first, 'flutter_spectrum': {'frequencies': [1, 2], 'db': [-40, -30]}})
        assert session['spectra'][0]['timestamp'] == session['history'][0]['timestamp'] == 101.
        assert session['history'][1]['timestamp'] == 101.1
        assert frame._run_worst == .24


def verify_spectrum_and_distortion(frame):
    import numpy as np

    from fluttery.audio.bench import two_tone_imd
    from fluttery.bench_ui import BenchTest

    dialog = frame.show_bench_analyzer()
    rate = 48000
    time = np.arange(rate) / rate
    carrier = np.sin(2 * np.pi * 7000 * time)
    lower = np.sin(2 * np.pi * 60 * time)
    samples = .25 * lower + .10 * carrier * (1 + .025 * lower)
    frame.audio.feed(samples, sample_rate=rate)
    dialog._spectrum(force=True)
    assert dialog.average.count == 1
    dialog._toggle_peak()
    assert dialog.hold.GetMinSize().width >= dialog.hold.GetBestSize().width
    assert len(dialog.plot.snapshot()['traces']) == 2

    dialog.mode.SetSelection(2)
    dialog._mode_changed(None)
    dialog.start.ChangeValue('55')
    dialog.mode.SetSelection(3)
    dialog._mode_changed(None)
    assert dialog.start.GetValue() == '-60'
    dialog.mode.SetSelection(2)
    dialog._mode_changed(None)
    assert dialog.start.GetValue() == '55'

    dialog.mode.SetSelection(1)
    dialog._mode_changed(None)
    rows = [two_tone_imd(samples, rate, 60, 7000)]
    dialog._complete(BenchTest(1, 1, 'smpte', 60, 7000, None), rows, '')
    assert dialog.save.IsEnabled()
    dialog._store()
    assert frame.session['measurementLog'][0]['type'] == 'bench-imd'
    dialog.Hide()


def verify_session_setup(frame, monkeypatch):
    import wx

    from fluttery.session_setup import SessionSetupDialog
    from fluttery.sessions import new_session

    saved_preferences = frame._persistent_settings['qcLimit']
    timing = frame._persistent_settings['detectorTiming'].copy()
    captured = []
    permanent = False

    def choose(self):
        captured.append(self.rows)
        for index, row in enumerate(self.rows):
            if row.path in (('qcLimit',), ('detectorTiming', 'peakAttackTime')):
                assert not row.warning
                self.values.Check(index, True)
        self._accept(permanent)
        return wx.ID_OK

    # Exercise the same selection reader using a real native checklist. The
    # test returns the modal result itself to keep font focus deterministic.
    def capture_selection(self, is_permanent):
        self.selection = {self.rows[index].path: self.rows[index].saved for index in self.values.GetCheckedItems()}
        assert not self.validate(self.selection)
        self.permanent = is_permanent

    with monkeypatch.context() as patch:
        patch.setattr(SessionSetupDialog, 'ShowModal', choose)
        patch.setattr(SessionSetupDialog, '_accept', capture_selection)
        session = new_session()
        session['settings'] = {'qcLimit': .43, 'inputDevice': 'Missing adapter', 'outputDevice': None,
                               'sampleRate': 0, 'channels': 2, 'correctionFactor': 1.01, 'futureFeature': 4,
                               'detectorTiming': {'preset': 'slow'}}
        frame.session_loaded(session)
        warnings = {row.path[0]: row.warning for row in captured[-1]}
        assert warnings['inputDevice'] and warnings['futureFeature']
        assert 'did not record the interface route' in warnings['correctionFactor']
        assert frame.settings['qcLimit'] == .43 and frame._persistent_settings['qcLimit'] == saved_preferences
        assert frame.settings['detectorTiming'] == {**timing, 'preset': 'custom', 'peakAttackTime': .05}
        assert frame._persistent_settings['detectorTiming'] == timing
        assert frame.apply_settings(enableQC=True)
        assert frame.settings['qcLimit'] == .43 and frame._persistent_settings['qcLimit'] == saved_preferences
        frame.session_loaded(new_session())
        assert frame.settings['qcLimit'] == saved_preferences
        assert frame.settings['detectorTiming'] == timing
        permanent = True
        frame.session_loaded(session)
        frame.session_loaded(new_session())
        assert frame.settings['qcLimit'] == frame._persistent_settings['qcLimit'] == .43
        assert frame.settings['detectorTiming'] == frame._persistent_settings['detectorTiming']
        assert frame.settings['detectorTiming']['peakAttackTime'] == .05

        assert frame.apply_settings(sampleRate=32_000)
        assert frame.settings_page.fields['sampleRate'][0]() == 32_000


def verify_plot_image_actions(frame, driver, monkeypatch, tmp_path):
    import io

    import wx
    from PIL import Image

    from fluttery import plot_export_ui

    frame.navigate('results', 'graph')
    plot = frame.results._current_plot()
    assert plot.snapshot()['traces']
    assert frame.apply_settings(plotCanvas='dark', plotGridDetail='minor', plotLegend='always')
    assert plot.appearance.colors(False).panel != (255, 255, 255)
    commands = {label: callback for label, callback, _ in plot.keyboard_actions()}
    native_show = plot_export_ui.ImageOptionsDialog.ShowModal
    destination = tmp_path / 'custom.svg'

    def choose_options(dialog):
        def accept():
            dialog.format.SetStringSelection('SVG vector image')
            dialog.width.SetValue(640)
            dialog.height.SetValue(360)
            dialog.background.SetStringSelection('Transparent')
            dialog.title.ChangeValue('Reference trace')
            dialog.remember.SetValue(True)
            dialog._accept(None)

        timer = wx.CallLater(30, accept)
        try:
            return native_show(dialog)
        finally:
            timer.Stop()

    def wait_for_image():
        # The initial export can build Matplotlib's font cache on a cold VM.
        deadline = time.monotonic() + 30
        while plot._image_export_running and time.monotonic() < deadline:
            wx.MilliSleep(15)
            driver.Yield()
        assert not plot._image_export_running

    with monkeypatch.context() as patch:
        patch.setattr(plot_export_ui.ImageOptionsDialog, 'ShowModal', choose_options)
        patch.setattr(plot_export_ui, '_destination', lambda _parent, _title, kind: tmp_path / f'custom.{kind}')
        assert 'Save Plot Image…' in commands
        assert frame.results.export_report('png')
        wait_for_image()
        svg = destination.read_text(encoding='utf-8')
        assert '<svg' in svg and 'Reference trace' in svg
        assert frame.settings['plotImageExport']['format'] == 'svg'
        assert 'title' not in frame.settings['plotImageExport']
        assert commands['Quick Save PNG…']()
        wait_for_image()
        with Image.open(tmp_path / 'custom.png') as image:
            assert image.size == (640, 360) and image.getpixel((0, 0))[3] == 0
        copied = []
        patch.setattr(plot_export_ui, '_clipboard_image', copied.append)
        assert commands['Copy Plot Image']()
        wait_for_image()
        with Image.open(io.BytesIO(copied[0])) as image:
            assert image.size == (640, 360)


def verify_bench_instruments(frame, driver, monkeypatch, tmp_path):
    import numpy as np
    import wx
    from scipy.io import wavfile

    rate = 48000
    positions = np.arange(rate) / rate
    reference = .5 * np.sin(2 * np.pi * 1000 * positions)
    frame.audio.reset()
    frame.audio.feed(np.column_stack((reference, reference * .8)), rate)
    window = frame.workspace.frame_for_key('measure')
    tools_menu = get_menu(window, 'Tools')
    calibration = get_item(tools_menu, 'Calibration').GetSubMenu()
    invoke(window, get_item(calibration, 'Voltage Calibration…'))
    meter = frame.instrument_dialogs['levels-calibration']
    meter.cal_channel.SetSelection(2)
    meter._capture_voltage()
    assert abs(float(meter.left_factor.GetValue()) - 2 * np.sqrt(2)) < .001
    meter._save_voltage()
    settings = frame.settings['voltageCalibrations'][0]
    assert settings['left'] < settings['right'] and settings['rate'] == rate
    labels = {command.label for command in frame.keyboard.commands(meter).values()}
    assert {'Capture Reference', 'Save Calibration', 'Audio Settings…', 'Measurement Options…'} <= labels
    meter.Close()

    snr = frame.show_level_meter('snr')
    stereo = np.column_stack((reference, reference * .8))
    frame.audio.feed(stereo, rate)
    snr._capture_reference()
    assert not snr.noise_button.IsEnabled()
    quiet = np.random.default_rng(31).normal(0, 1e-4, stereo.shape)
    frame.audio.feed(quiet, rate)
    snr._tick(None)
    assert not snr.noise_button.IsEnabled()
    frame.audio.feed(quiet, rate)
    snr._tick(None)
    assert snr.noise_button.IsEnabled()
    snr._capture_noise()
    assert 'SNR:' in snr.activity and snr.plot.title == 'Captured noise spectrum'
    frame.audio.reset()
    frame.audio.feed(np.vstack((quiet, quiet)), rate)
    snr._tick(None)
    assert not snr.noise_button.IsEnabled() and 'input changed or was interrupted' in snr.activity.lower()
    frame.audio.feed(np.vstack((stereo, stereo)), rate)
    snr._capture_reference()
    frame.audio.feed(np.zeros((2 * rate, 2)), rate)
    snr._capture_noise()
    assert 'SNR unavailable' in snr.activity and 'digital muting' in snr.activity
    snr.Close()

    path = tmp_path / 'test tone.wav'
    t = np.arange(rate * 6) / rate
    wavfile.write(path, rate, np.asarray(.5 * np.sin(2 * np.pi * 3150 * t - .7875 * np.cos(2 * np.pi * 4 * t)),
                                        dtype=np.float32))
    frame.open_path(path)
    offline = frame.instrument_dialogs['offline-wav']
    offline.method.SetSelection(1)
    offline._analyse()
    deadline = time.monotonic() + 6
    while offline._worker is not None and time.monotonic() < deadline:
        wx.MilliSleep(20)
        driver.Yield()
    assert offline.result is not None and .096 < offline.result['metrics']['aes6Peak'] < .104
    offline.plot_view.SetSelection(3)
    offline._render_result_plot()
    assert 'Modulation spectrum' in offline.plot.title
    monkeypatch.setattr(frame, 'confirm', lambda *_args: True)
    offline._use()
    assert frame.current_page == 'results' and frame.session['recordedAudio']['file'] == path.name
    offline.Close()


def verify_setup_wizard(frame, driver, monkeypatch):
    import wx

    from fluttery import app as application
    from fluttery.setup_wizard import DEVELOPER_SITE
    from fluttery.updates import REPOSITORY

    errors, saves = [], []

    def click(button):
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
        event.SetEventObject(button)
        button.ProcessWindowEvent(event)

    def assert_default(wizard):
        assert wizard.GetAffirmativeId() == wizard.next.GetId()
        if wx.Platform == '__WXMAC__':
            from fluttery import macos

            assert wizard.GetDefaultItem() == wizard.next
            view = wizard.next.GetHandle()
            window = macos._call(view, b'window')
            # Verify the AppKit cell as well as wx's platform-neutral default.
            assert macos._call(window, b'defaultButtonCell') == macos._call(view, b'cell')

    def walk(first_launch):
        wizard = frame._setup_wizard
        try:
            driver.Yield()  # Let the native dialog apply its initial keyboard focus.
            assert wizard.IsModal() and wizard.step == 0 and wizard.heading.GetLabel() == 'Welcome to Fluttery'
            assert ('Later' if first_launch else 'Close') in wizard.cancel.GetLabel()
            assert wizard.manual.IsShownOnScreen()
            assert_default(wizard)
            settings_menu = 'Fluttery > Settings' if wx.Platform == '__WXMAC__' else 'Edit > Settings'
            assert settings_menu in wizard._instruction_text
            if not first_launch:
                original_font = wizard.next.GetFont().GetFractionalPointSize()
                original_minimum = wizard.GetMinSize()
                waveform = frame.pages['generate'].waveform
                assert waveform.GetSelection() == 0
                waveform.SetSelection(3)
                assert frame.apply_settings(textScale=150)
                driver.Yield()
                assert waveform.GetSelection() == 3
                assert wizard.next.GetFont().GetFractionalPointSize() == pytest.approx(original_font * 1.5, abs=1)
                assert wizard.GetMinSize().width > original_minimum.width
                assert wizard.next.GetRect().height > 0 and wizard.next.IsShownOnScreen()
                assert frame.apply_settings(textScale=100)
                assert waveform.GetSelection() == 3
                waveform.SetSelection(0)
                click(wizard.cancel)
                return
            original_steps = wizard.total_steps
            wizard.SetSize((wizard.GetSize().width, wizard.GetMinSize().height))
            wizard.refit_steps()
            assert wizard.total_steps > original_steps
            opened = []
            with monkeypatch.context() as patch:
                patch.setattr(wx, 'LaunchDefaultBrowser', lambda url: opened.append(url) or True)
                for link in wizard.welcome.GetChildren():
                    if isinstance(link, wx.Button):
                        click(link)
            assert opened == [REPOSITORY, DEVELOPER_SITE]
            assert frame._initial_setup_pending and not frame._auto_input_pending
            click(wizard.next)
            assert wizard.heading.GetLabel() == 'Automatic updates'
            assert not wizard.manual.IsShownOnScreen()
            assert not wizard.enable_updates.GetValue()
            wizard.enable_updates.SetValue(True)
            click(wizard.back)
            click(wizard.next)
            assert wizard.enable_updates.GetValue()
            save_errors = []

            def failed_write(_settings):
                raise OSError('Preferences temporarily unavailable')

            with monkeypatch.context() as patch:
                patch.setattr(application, 'save_settings', failed_write)
                patch.setattr(frame, 'show_error', lambda title, error: save_errors.append(str(error)))
                click(wizard.next)
            assert save_errors == ['Preferences temporarily unavailable'] and wizard.step == 1
            click(wizard.next)
            assert frame.settings['checkForUpdates'] and saves[-1]['checkForUpdates']
            visited = []
            plot_draft = {'plotGrid': False, 'plotTraceStyle': 'bold', 'spectrogramRange': 60}
            while wizard.IsShown():
                driver.Yield()  # Give the native book time to show the new page's controls.
                part = wizard.settings_step
                section = part.section
                index = frame.settings_page.SECTIONS.index(section)
                pane = wizard.form.book.GetPage(index)
                assert wizard.form.book.GetSelection() == index
                assert wizard.heading.GetLabel().startswith(frame.settings_page.book.GetPageText(index))
                assert wizard.manual.IsShownOnScreen() == (wizard.step == wizard.total_steps - 1)
                assert_default(wizard)
                assert list(part.groups) == [group for group in pane.groups if group.GetStaticBox().IsShown()]
                visited.extend(id(group) for group in part.groups)
                if section == 'audio' and part.part == 1:
                    wizard.form.fields['autoStartInput'][1](False)
                if section == 'graphs' and part.part == 1:
                    commands = [command.label for command in frame.keyboard.commands(wizard).values()]
                    assert 'Focus Plot background' in commands and 'Focus Audio input' not in commands
                    for key, value in plot_draft.items():
                        wizard.form.fields[key][1](value)
                    click(wizard.back)
                    click(wizard.next)
                    assert frame.settings['plotTraceStyle'] == 'standard'
                    assert wizard.form.fields['plotTraceStyle'][0]() == 'bold'
                    baseline = {key: frame.settings[key] for key in plot_draft}
                    click(wizard.next)
                    for key, value in plot_draft.items():
                        assert frame.settings[key] == (value if wizard.form._field_groups[key] in part.groups else baseline[key])
                    continue
                click(wizard.next)
            assert len(visited) == len(set(visited)) == sum(
                len(wizard.form.book.GetPage(index).groups) for index in range(len(frame.settings_page.SECTIONS))
            )
            assert all(frame.settings[key] == value for key, value in plot_draft.items())
        except Exception as error:  # noqa: BLE001 - Re-raise toolkit callback errors in the test.
            errors.append(error)
            # Some native toolkits stay modal until the event callback returns.
            # Cancel here only when a failed assertion would leave the test open.
            if wizard is not None and wizard.IsModal():
                wizard.EndModal(wx.ID_CANCEL)

    with monkeypatch.context() as patch:
        patch.setattr(application, 'save_settings', lambda settings: saves.append(settings.copy()))
        wx.CallAfter(walk, True)
        driver.Yield()
    if errors:
        raise errors[0]
    assert saves[-1]['setupWizardSeen'] and not frame.settings['autoStartInput']
    assert not frame.settings['plotGrid'] and frame.settings['plotTraceStyle'] == 'bold'
    assert frame.settings['spectrogramRange'] == 60

    frame.show_startup(offer_setup=True)
    driver.Yield()
    assert not frame._initial_setup_pending and frame._setup_wizard is None
    reopen = wx.CallLater(80, walk, False)
    try:
        invoke(frame, get_item(get_menu(frame, 'Help'), 'Setup Wizard…'))
    finally:
        reopen.Stop()
    if errors:
        raise errors[0]
    assert frame._setup_wizard is None
    assert frame.apply_settings(checkForUpdates=False)


def verify_bundled_manual(frame, driver, monkeypatch):
    import wx

    from fluttery.manual_content import CONTENTS, MANUAL_DIRECTORY, SETTINGS, SHORTCUTS
    from fluttery.manual_ui import ManualDialog

    visited, failures = [], []

    def inspect(dialog):
        dialog.Show()
        driver.Yield()
        try:
            visited.append(CONTENTS)
            if not (MANUAL_DIRECTORY / CONTENTS).is_file():
                assert not dialog.matches and dialog.title.GetLabel() == 'User manual unavailable'
                return wx.ID_CANCEL
            assert dialog.topic.file == CONTENTS
            dialog.search.SetValue('distortion')
            assert any(topic.file == '06-signals.md' for topic in dialog.matches)
            dialog.search.SetValue('qzxnotatopic')
            assert dialog.topics.GetItemCount() == 0 and dialog.title.GetLabel() == 'No matching topics'
            dialog.search.ChangeValue('')
            dialog._populate('03-measure.md#record-a-run-and-review-the-readings')
            assert dialog.topic.illustrations
            dialog.mode.SetStringSelection('Illustrations')
            dialog._choose_mode(None)
            deadline = time.monotonic() + 2
            while not dialog.bitmap.GetBitmap().IsOk() and time.monotonic() < deadline:
                wx.MilliSleep(15)
                driver.Yield()
            assert dialog.bitmap.GetBitmap().IsOk()
            dialog._populate(SHORTCUTS + '#open-tools-and-manage-windows')
            dialog.mode.SetStringSelection('Reference tables')
            dialog._choose_mode(None)
            assert dialog.table.GetColumnCount() == 2
            assert len(set(dialog.table_choice.GetStrings())) == dialog.table_choice.GetCount()
            assert '+1' in dialog.table_detail.GetValue()
        except AssertionError as error:
            # wx can report a callback exception without failing the test.
            failures.append(error)
        finally:
            dialog.Hide()
        return wx.ID_CANCEL

    menu = get_menu(frame, 'Help')
    with monkeypatch.context() as patch:
        # Let the real widgets lay out in the test event loop. The native
        # capture harness exercises actual modal entry on all three platforms.
        patch.setattr(ManualDialog, 'ShowModal', inspect)
        invoke(frame, get_item(menu, 'Fluttery Help'))
    if failures:
        raise failures[0]
    driver.Yield()
    with monkeypatch.context() as patch:
        patch.setattr(frame, '_show_guide', visited.append)
        invoke(frame, get_item(menu, 'Keyboard Shortcuts'))
        invoke(frame, get_item(menu, 'Settings Reference'))
    assert visited == [CONTENTS, SHORTCUTS, SETTINGS]


def verify_frequency_sweep(frame, driver, monkeypatch, tmp_path):
    import wave

    import wx

    from fluttery import sweep_ui
    from fluttery.audio.sweep import SweepRule, SweepSettings

    def click(button):
        event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
        event.SetEventObject(button)
        button.ProcessWindowEvent(event)

    def wait_until_done(waiting):
        deadline = time.monotonic() + 3
        while waiting() and time.monotonic() < deadline:
            wx.MilliSleep(15)
            driver.Yield()
            frame._tick(None)
        assert not waiting()
        frame._tick(None)

    generator = frame.navigate('generate')
    window = frame.workspace.frame_for_key('generate')
    invoke(window, get_item(get_menu(window, 'Generator'), 'Frequency Sweep…'))
    dialog = frame._sweep_dialog
    driver.Yield()
    assert dialog.IsShownOnScreen() and frame.keyboard._owner(dialog) is dialog
    assert any(command.label == 'Read Sweep Activity…' for command in frame.keyboard.commands(dialog).values())
    sweep = SweepSettings(120, 1200, 0.24, rules=(SweepRule(400, waveform='triangle', level=0.5, level_curve='linear'),))
    dialog._load_sweep(sweep, {'sample_rate': 96000, 'channels': 2, 'bit_depth': 24})
    generator.loopback.SetValue(True)
    generator._toggle_loopback()
    generator._toggle_generator()
    assert generator.generating and frame.audio.snapshot()['generator']['enabled']

    click(dialog.play_button)
    assert frame.audio.snapshot()['sweep']['active'] and not generator.generating
    wait_until_done(lambda: frame.audio.snapshot()['sweep']['active'])
    assert frame.audio.snapshot()['sweep']['status'] == 'completed'
    assert dialog.progress.GetValue() == 1000 and not frame.audio.running
    generator._toggle_generator()
    assert frame.audio.snapshot()['generator']['enabled']
    generator._toggle_generator()

    click(dialog.play_button)
    click(dialog.stop_button)
    wait_until_done(lambda: frame.audio.snapshot()['sweep']['active'])
    assert frame.audio.snapshot()['sweep']['status'] == 'stopped'
    with monkeypatch.context() as patch:
        patch.setattr(sweep_ui, '_validated_name', lambda *_args: 'Bench test')
        dialog._save_preset()
    assert frame.settings['sweepPresets'][0]['sweep'] == sweep.to_dict()
    dialog.start_frequency.ChangeValue('80')
    dialog._choose_preset()
    assert dialog._read_sweep() == sweep

    filename = tmp_path / 'Frequency sweep.wav'

    class SaveFile:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            pass

        def ShowModal(self):
            return wx.ID_OK

        def GetPath(self):
            return str(filename)

    with monkeypatch.context() as patch:
        patch.setattr(wx, 'FileDialog', SaveFile)
        click(dialog.export_button)
    wait_until_done(lambda: dialog._export_cancel is not None)
    with wave.open(str(filename), 'rb') as audio:
        assert (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) == (96000, 2, 3)
        assert audio.getnframes() == 23040
    assert 'WAV saved' in dialog._progress_text
    dialog.Close()
    assert not dialog.IsShown()


def verify_cancelled_quit_keeps_measuring(frame, monkeypatch):
    import wx

    class CancelDialog:
        def __init__(self, _parent, message, *_args):
            assert 'Closing this session will stop its measurements' in message

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def SetYesNoCancelLabels(self, *_labels):
            pass

        def ShowModal(self):
            return wx.ID_CANCEL

    with monkeypatch.context() as patch:
        patch.setattr(wx, 'MessageDialog', CancelDialog)
        frame.quit()
    assert frame.is_measuring and frame.audio.running and not frame._closing


def verify_comparison_navigation(frame, driver, monkeypatch, filename):
    original_layout = frame.workspace.layout
    with monkeypatch.context() as patch:
        patch.setattr(frame.results, '_file_path', lambda *_args: filename)
        for layout in ('windows', 'tabs'):
            assert frame.apply_settings(workspaceLayout=layout)
            frame.navigate('scope')
            driver.Yield()
            tool = frame.workspace.frame_for_key('scope')
            invoke(tool, get_item(get_menu(tool, 'File'), 'Compare With Session…'))
            driver.Yield()
            assert frame.current_page == 'results' and frame.results.IsShownOnScreen()
            assert frame.results.view == 'graph' and frame.results.comparison_name == filename.name
            assert len(frame.results._graph_cache['graph'].traces) == 2
    frame.results.clear_comparison(announce=False)
    assert frame.apply_settings(workspaceLayout=original_layout)


def verify_manual_update_reuses_background_check(frame, driver, monkeypatch):
    from http.client import IncompleteRead

    import wx

    from fluttery import app as application

    checks, dialogs = [], []

    class DeferredCheck:
        def __init__(self, target, **_kwargs):
            self.target = target

        def start(self):
            checks.append(self.target)

    frame.navigate('settings', 'general')
    with monkeypatch.context() as patch:
        patch.setattr(application, 'Thread', DeferredCheck)
        patch.setattr(application.updates, 'latest_release', lambda _version: None)
        patch.setattr(wx, 'MessageBox', lambda message, *_args: dialogs.append(message))
        frame.check_updates()
        frame.check_updates(manual=True)
        assert len(checks) == 1 and not dialogs
        assert frame.settings_page.feedback.GetLabel() == 'Checking for updates…'
        checks.pop()()
        driver.Yield()
        assert dialogs == ['No update is available.']
        assert frame.settings_page.feedback.GetLabel() == dialogs[0]

        def interrupted_read(_version):
            raise IncompleteRead(b'', 100)

        patch.setattr(application.updates, 'latest_release', interrupted_read)
        patch.setattr(frame, 'show_error', lambda title, _message: dialogs.append(title))
        frame.check_updates(manual=True)
        checks.pop()()
        driver.Yield()
        assert dialogs[-1] == 'Could not check for updates'
        assert frame._update_stage is None

        confirmations = []
        patch.setattr(frame, 'confirm', lambda *_args: confirmations.append(True))
        assert frame.apply_settings(checkForUpdates=False)
        frame._show_update(application.updates.Release('9.0.0', application.updates.RELEASES_PAGE, None), '')
        assert not confirmations


def verify_input_without_playback(frame, monkeypatch):
    from types import SimpleNamespace

    from fluttery.preferences import normalize_settings

    previous = frame.settings
    errors = []
    audio = SimpleNamespace(running=False, input_enabled=False, output_enabled=False, configure=lambda _settings: None,
                            set_monitor=lambda *_args: None, snapshot=lambda: {'health': {'outputWarning': 'No default audio output.'}})

    def start(input_enabled, output_enabled):
        assert input_enabled and output_enabled
        audio.running = audio.input_enabled = True

    def stop():
        audio.running = audio.input_enabled = False

    audio.stop = stop
    with monkeypatch.context() as patch:
        patch.setattr(frame, 'audio', audio)
        patch.setattr(frame, 'settings', previous)
        patch.setattr(frame, '_allow_microphone', lambda *_args, **_kwargs: True)
        patch.setattr(frame, '_start_audio', start)
        patch.setattr(frame, 'show_error', lambda _title, error: errors.append(str(error)))
        assert not frame.apply_settings(monitoring=True)
        assert errors == ['No default audio output.']
        assert not audio.running and not frame.settings['monitoring']
        # A remembered monitor route must never prevent capture on a system with no output.
        frame.settings = normalize_settings({**previous, 'monitoring': True})
        assert frame.ensure_audio()
        assert audio.running and audio.input_enabled and not audio.output_enabled


def verify_deferred_audio_actions(frame, monkeypatch):
    from types import SimpleNamespace

    pending, routes, captures = [], [], []
    audio = SimpleNamespace(running=False, input_enabled=False, output_enabled=False, configure=lambda _settings: None,
                            set_monitor=lambda *_args: None, snapshot=dict)
    allowed = False

    def permission(resume, **_options):
        if not allowed:
            pending.append(resume)
        return allowed

    def start(input_enabled, output_enabled):
        routes.append((input_enabled, output_enabled))
        audio.input_enabled, audio.output_enabled = input_enabled, output_enabled
        audio.running = input_enabled or output_enabled

    with monkeypatch.context() as patch:
        patch.setattr(frame, 'audio', audio)
        patch.setattr(frame, 'settings', frame.settings.copy())
        patch.setattr(frame, '_persistent_settings', frame._persistent_settings.copy())
        patch.setattr(frame, '_session_overrides', frame._session_overrides.copy())
        patch.setattr(frame, '_allow_microphone', permission)
        patch.setattr(frame, '_start_audio', start)
        patch.setattr(frame, '_refresh_settings_views', lambda: None)
        wanted = True
        assert frame.bench_start_capture(1, on_ready=captures.append, still_wanted=lambda: wanted) is None
        wanted = False
        pending.pop()()
        assert not routes and not captures and not pending

        assert not frame.apply_settings(monitoring=True)
        new_grid = not frame.settings['plotGrid']
        assert frame.apply_settings(plotGrid=new_grid)
        allowed = True
        pending.pop()()
        assert routes == [(True, True)] and frame.settings['monitoring']
        assert frame.settings['plotGrid'] == frame._persistent_settings['plotGrid'] == new_grid

        audio.running, audio.input_enabled, audio.output_enabled = True, False, False
        audio.snapshot = lambda: {'loopback': True, 'generator': {'enabled': True}}
        frame.settings['monitoring'] = False
        frame.stop_input()
        assert audio.running and 'loopback is active' in frame.GetStatusBar().GetStatusText()
        assert frame.ensure_audio(require_physical=True)
        assert routes[-1] == (True, False)


def verify_settings_actions_keep_unrelated_drafts(frame, monkeypatch):
    settings = frame.settings_page
    get_draft, set_draft = settings.fields['plotGrid']
    current = frame.settings['plotGrid']
    set_draft(not current)
    assert settings.apply(feedback=False, section='calibration')
    assert frame.settings['plotGrid'] == current and get_draft() != current
    set_draft(current)

    live = {'id': 0, 'name': 'Draft interface', 'host_api': 'Driver', 'input_channels': 2,
            'output_channels': 0, 'sample_rate': 48000}
    saved = 'Missing interface'
    with monkeypatch.context() as patch:
        patch.setattr(frame, 'settings', {**frame.settings, 'inputDevice': saved})
        patch.setattr(frame.audio, 'devices', lambda: [live])
        settings.refresh_devices()
        settings.input_choice.SetSelection(1)
        draft = settings.fields['inputDevice'][0]()
        patch.setattr(frame.audio, 'devices', list)
        settings.refresh_devices()
        get_device, set_device = settings.fields['inputDevice']
        assert draft and get_device() == draft
        set_device(saved)
        assert get_device() == saved
    settings.refresh_settings()


def verify_rolling_sample_table(frame, monkeypatch):
    from fluttery.results import TABLE_PAGE_SIZE

    history = frame.session['history']
    history.extend({'timestamp': 1_700_000_000 + i, 'peak': i / 100} for i in range(100))
    table = frame.results.history_table
    formatted = []
    original_format = table._format_rows

    def format_page(rows):
        formatted.append(len(rows))
        return original_format(rows)

    monkeypatch.setattr(table, '_format_rows', format_page)
    frame.navigate('results', 'table')
    table._move(1)
    assert table.table._rows[-1][1] == '0.9900'
    # At the cap, a new sample replaces the oldest without changing the count.
    history.append({'timestamp': 1_700_000_100, 'peak': 1})
    history.pop(0)
    frame.results.refresh_results()
    assert len(history) == 100 and table.page == 1
    assert table.table._rows[-1][1] == '1.0000'
    assert formatted == [TABLE_PAGE_SIZE, 100 - TABLE_PAGE_SIZE, 100 - TABLE_PAGE_SIZE]


def verify_keyboard_and_help(frame, window, driver, monkeypatch):
    import wx
    from wx import dataview

    from fluttery.keyboard_access import CommandDialog, widget_tree

    native_focus = True
    if wx.Platform == '__WXMAC__':
        import ctypes as ct

        from fluttery import macos

        # A test launched from the shell needs to become the foreground app for native focus checks.
        native_app = macos._call(macos._class(b'NSApplication'), b'sharedApplication')
        macos._call(native_app, b'setActivationPolicy:', 0, result=ct.c_bool, argtypes=(ct.c_long,))
        macos._call(native_app, b'activateIgnoringOtherApps:', True, argtypes=(ct.c_bool,), result=None)
        window.Raise()
        handle = window.GetHandle()
        is_window = macos._call(handle, b'isKindOfClass:', macos._class(b'NSWindow'),
                                result=ct.c_bool, argtypes=(macos.Pointer,))
        native_window = handle if is_window else macos._call(handle, b'window')
        macos._call(native_window, b'makeKeyAndOrderFront:', None, result=None, argtypes=(macos.Pointer,))
        driver.Yield()
        # A shell-launched process can be denied foreground activation. Still
        # test selection and command dispatch when Cocoa cannot assign focus.
        native_focus = (macos._call(native_app, b'isActive', result=ct.c_bool)
                        and macos._call(native_window, b'isKeyWindow', result=ct.c_bool))
    settings = frame.settings_page
    sidebar = settings.categories
    sidebar.SetFocus()

    def sidebar_focused(focus):
        return focus == sidebar or sidebar.IsDescendant(focus)

    if native_focus:
        wait_for_focus(driver, sidebar_focused, 'Settings categories')
    sidebar.SelectRow(1)
    changed = dataview.DataViewEvent(dataview.EVT_DATAVIEW_SELECTION_CHANGED.typeId, sidebar, sidebar.RowToItem(1))
    sidebar.ProcessWindowEvent(changed)
    driver.Yield()
    assert settings.book.GetSelection() == 1
    if native_focus:
        wait_for_focus(driver, sidebar_focused, 'Settings categories')

    controls = tuple(widget_tree(settings))
    apply, = (control for control in controls if isinstance(control, wx.Button) and control.GetLabel() == 'Apply Settings')
    assert apply.GetToolTipText()
    footer = apply.GetParent()
    assert footer.GetParent() is settings and not settings.book.IsDescendant(footer)
    assert [button.GetLabel() for button in footer.GetChildren()] == ['Revert edits', 'Apply Settings', 'Close']
    if wx.Platform == '__WXMAC__':
        for group, label in ((settings.book, b'Settings pane'), (footer, b'Settings actions')):
            view = group.GetHandle()
            assert macos._call(view, b'isAccessibilityElement', result=ct.c_bool)
            role = macos._call(view, b'accessibilityRole')
            assert macos._call(role, b'UTF8String', result=ct.c_char_p) == b'AXGroup'
            name = macos._call(view, b'accessibilityLabel')
            assert macos._call(name, b'UTF8String', result=ct.c_char_p) == label
    assert not any(isinstance(control, wx.Button) and 'sound' in control.GetLabel().lower() for control in controls)
    visible_commands = frame.keyboard.commands(window)
    assert sum(command.label == 'Apply Settings' for command in visible_commands.values()) == 1
    assert any(command.label == 'Oscilloscope' and command.category == 'Window' and command.native_shortcut.endswith('+2')
               for command in visible_commands.values())
    code, = (code for code, command in visible_commands.items()
             if command.label == 'Focus Default technician' and command.available)

    visited = []

    def enter_letters(dialog):
        try:
            label = dialog.commands[code].label if code in dialog.commands else None
            focus = wx.Window.FindFocus()
            in_table = focus is not None and (focus == dialog.table or dialog.table.IsDescendant(focus))
            visited.append((dialog.GetParent(), in_table, label))
            if dialog.GetParent() != window or label != 'Focus Default technician':
                return
            if native_focus:
                for letter in code:
                    event = wx.KeyEvent(wx.EVT_CHAR_HOOK.typeId)
                    event.SetKeyCode(ord(letter))
                    dialog.ProcessWindowEvent(event)
            else:
                dialog._choose(code)
        finally:
            if dialog.chosen is None and dialog.IsModal():
                dialog.EndModal(wx.ID_CANCEL)

    native_show = CommandDialog.ShowModal

    def show_and_enter_letters(dialog):
        timer = wx.CallLater(80, enter_letters, dialog)
        try:
            return native_show(dialog)
        finally:
            timer.Stop()

    monkeypatch.setattr(CommandDialog, 'ShowModal', show_and_enter_letters)
    invoke_dialog = wx.KeyEvent(wx.EVT_CHAR_HOOK.typeId)
    invoke_dialog.SetKeyCode(ord('K'))
    invoke_dialog.SetControlDown(True)
    invoke_dialog.SetShiftDown(True)
    invoke_dialog.SetEventObject(sidebar)
    assert frame.keyboard.FilterEvent(invoke_dialog) == wx.EventFilter.Event_Processed
    driver.Yield()
    assert len(visited) == 1 and visited[0][0] == window and visited[0][2] == 'Focus Default technician'
    if native_focus:
        assert visited[0][1]
        wait_for_focus(driver, lambda focus: focus.GetName() == 'Default technician', 'Default technician')
    assert settings.book.GetSelection() == settings.SECTIONS.index('reports')

    save = get_item(get_menu(window, 'File'), 'Save Session')
    save_help = next(command.description for command in visible_commands.values() if command.label == 'Save Session')
    assert 'Fluttery session file' in save_help and save.GetHelp() == save_help
    assert frame.apply_settings(showHelpTags=False)
    assert not apply.GetHelpText() and not apply.GetToolTipText() and not save.GetHelp().strip()
    assert frame.apply_settings(showHelpTags=True)
    assert apply.GetHelpText() and apply.GetToolTipText() and save.GetHelp() == save_help


def wait_for_focus(driver, matches, description):
    import wx

    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        focus = wx.Window.FindFocus()
        if focus is not None and matches(focus):
            return
        wx.MilliSleep(10)
        driver.Yield()
    pytest.fail(f'The app did not focus {description}: {focus}')


def verify_legacy_settings_workflows(frame, window, driver, monkeypatch):
    import base64
    import io
    from types import SimpleNamespace

    import wx
    from PIL import Image

    from fluttery import app as application
    from fluttery import macos

    settings = frame.settings_page
    selection = settings.book.GetSelection()
    invoke(window, get_item(get_menu(window, 'Window'), 'Settings'))
    assert settings.book.GetSelection() == selection
    links = []
    with monkeypatch.context() as patch:
        patch.setattr(application.webbrowser, 'open', lambda link: links.append(link) or True)
        help_menu = get_menu(window, 'Help')
        invoke(window, get_item(help_menu, 'Project Repository'))
        invoke(window, get_item(help_menu, 'Send Feedback…'))
        invoke(window, get_item(help_menu, 'Donate…'))
    assert links == [application.updates.REPOSITORY, application.FEEDBACK_ADDRESS, application.DONATION_PAGE]
    opened = []
    with monkeypatch.context() as patch:
        def unavailable(*, interactive):
            opened.append(interactive)
            return False

        patch.setattr(frame, 'ensure_audio', unavailable)
        assert frame.apply_settings(autoStartInput=True)
        frame.navigate('scope')
        driver.Yield()
        assert opened == [False] and not frame.is_measuring
        frame.navigate('measure')
        driver.Yield()
        assert opened == [False, False]
        frame.stop_input()
        frame.navigate('turntable')
        driver.Yield()
        assert opened == [False, False]
        assert frame.apply_settings(autoStartInput=False)
        frame.navigate('settings', 'general')

    if wx.Platform == '__WXMAC__':
        callbacks = []
        resumed = []
        with monkeypatch.context() as patch:
            patch.setattr(macos, 'microphone_authorization', lambda: macos.MICROPHONE_UNDETERMINED)
            patch.setattr(macos, 'request_microphone_access', callbacks.append)
            assert not frame._allow_microphone(lambda: resumed.append('background'), interactive=False)
            assert not frame._allow_microphone(lambda: resumed.append('manual'))
            assert len(callbacks) == 1
            callbacks.pop()(True)
            driver.Yield()
            assert resumed == ['manual']

            denied = []
            patch.setattr(frame, 'show_error', lambda _title, error: denied.append(str(error)))
            assert not frame._allow_microphone(lambda: resumed.append('denied'))
            callbacks.pop()(False)
            driver.Yield()
            assert resumed == ['manual'] and denied and 'Privacy & Security' in denied[0]
            assert not frame._allow_microphone(lambda: resumed.append('stopped'))
            frame.stop_input()
            callbacks.pop()(True)
            driver.Yield()
            assert resumed == ['manual']

    image = io.BytesIO()
    Image.new('RGBA', (120, 48), '#20a0c0').save(image, format='PNG')
    settings.logo = 'data:image/png;base64,' + base64.b64encode(image.getvalue()).decode('ascii')
    settings._logo_label()
    assert settings.logo_preview.IsShown() and '120 by 48' in settings.logo_status.GetLabel()
    settings._remove_logo()
    assert not settings.logo_preview.IsShown()
    settings._refresh_debug({
        'metrics': {'locked': True, 'inputLevel': -12.5, 'currentFreq': 3150, 'detectorDropouts': 3},
        'health': {'averageAnalysisIntervalMs': 40, 'maxAnalysisIntervalMs': 52},
    })
    diagnostics = settings.health.GetValue()
    assert 'Signal: Locked' in diagnostics and 'Input level: -12.5 dBFS' in diagnostics
    assert '3150.00 Hz' in diagnostics and '52.0 ms' in diagnostics and 'Detector dropouts: 3' in diagnostics
    frame.navigate('settings', 'diagnostics')
    copied = []
    clipboard = SimpleNamespace(Open=lambda: False, SetData=lambda _value: False,
                                Close=lambda: copied.append('closed'))
    with monkeypatch.context() as patch:
        patch.setattr(wx, 'TheClipboard', clipboard)
        frame.copy_text('Audio started.')
        assert settings.feedback.IsShown() and 'clipboard is busy' in settings.feedback.GetLabel()
        assert not copied
        clipboard.Open = lambda: True
        frame.copy_text('Audio started.')
        assert 'Could not copy' in settings.feedback.GetLabel() and copied == ['closed']
        clipboard.SetData = lambda value: copied.append(value.GetText()) or True
        frame.copy_text('Audio started.')
        assert settings.feedback.GetLabel() == 'Copied to clipboard.'
        assert copied == ['closed', 'Audio started.', 'closed']
    assert not frame.debug_log
    frame.export_debug()
    assert settings.feedback.GetLabel() == 'No diagnostic messages to save.'
    frame.navigate('settings', 'calibration')
    assert not settings.feedback.IsShown()
    settings.calibration_progress.SetValue(47)
    progress = next(cmd for cmd in frame.keyboard.commands(window).values() if cmd.label == 'Read Calibration progress')
    readout = []
    with monkeypatch.context() as patch:
        patch.setattr(frame, '_show_text', lambda _title, body: readout.append(body))
        progress.action()
    assert '47 percent complete' in readout[0]


def get_menu(frame, title):
    from fluttery.keyboard_access import plain_label

    bar = frame.GetMenuBar()
    return next(bar.GetMenu(index) for index in range(bar.GetMenuCount()) if plain_label(bar.GetMenuLabel(index)) == title)


def get_item(menu, title):
    return next(item for item in menu.GetMenuItems() if item.GetItemLabelText() == title)


def invoke(frame, item):
    import wx

    frame.ProcessWindowEvent(wx.CommandEvent(wx.EVT_MENU.typeId, item.GetId()))


def wait_for_steady_tone(frame, driver):
    import wx

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        metrics = frame.audio.snapshot()['metrics']
        if metrics['locked'] and abs(metrics['currentFreq'] - 3150) < 1.0 and metrics['timestamp'] > 0.25:
            return
        wx.MilliSleep(30)
        driver.Yield()
    pytest.fail(f'Software loopback did not settle at the reference frequency: {metrics}')
