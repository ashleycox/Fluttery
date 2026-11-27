"""Verify session and instrument window commands without physical audio."""

import os

import pytest
import wx

from fluttery import app as application
from fluttery import staging
from fluttery.audio import AudioEngine
from fluttery.keyboard_access import plain_label

pytestmark = pytest.mark.skipif(os.getenv('FLUTTERY_GUI_TESTS') != '1', reason='requires an active desktop')


@pytest.fixture
def desktop_session(monkeypatch):
    class SoftwareAudio(AudioEngine):
        def devices(self):
            return []

        def start(self, *args, input_enabled=True, output_enabled=True, **kwargs):
            assert not input_enabled and not output_enabled, 'This test must only start software loopback.'
            return super().start(*args, input_enabled=False, output_enabled=False, **kwargs)

    def unexpected_error(_controller, title, error, **_options):
        pytest.fail(f'{title}: {error}')

    driver = wx.GetApp() or wx.App(False)
    # A new session creates its own engine through the application's normal
    # entry point, so the software-only policy must cover both documents.
    monkeypatch.setattr(application, 'AudioEngine', SoftwareAudio)
    monkeypatch.setattr(application, 'save_settings', lambda _settings: None)
    monkeypatch.setattr(application.FlutteryFrame, 'show_error', unexpected_error)
    monkeypatch.setattr(staging, 'cleanup_session_staging', lambda: None)
    documents = application._SessionDocuments.current_app()
    original_frames = tuple(documents.frames)
    frame = application.FlutteryFrame(settings={
        'autoStartInput': False, 'enableUISounds': False, 'announceStatus': False,
        'checkForUpdates': False, 'targetFreq': 3150, 'useAutoTarget': False,
    }, automatic_updates=False)
    frame.show_startup()
    driver.Yield()
    try:
        yield frame, driver
    finally:
        for document in reversed(tuple(documents.frames)):
            if document not in original_frames:
                document.quit(confirmed=True)
        driver.Yield()


def _invoke(frame, menu_name, command, *, shortcut=None):
    bar = frame.GetMenuBar()
    menu = next(bar.GetMenu(index) for index in range(bar.GetMenuCount())
                if plain_label(bar.GetMenuLabel(index)) == menu_name)
    item = next(item for item in menu.GetMenuItems() if item.GetItemLabelText() == command)
    assert item.IsEnabled()
    if shortcut is not None:
        assert item.GetItemLabel().partition('\t')[2] == shortcut
    assert frame.ProcessWindowEvent(wx.CommandEvent(wx.EVT_MENU.typeId, item.GetId()))


def _shown_tools(frame):
    return [window for slot, window in frame.workspace.windows.items() if slot != 'settings' and window.IsShown()]


def _start_loopback_run(frame):
    frame.audio.set_loopback(True)
    frame.audio.set_generator(enabled=True, frequency=3150, level=.25)
    assert frame.start_measurement()
    assert frame.audio.running and not frame.audio.input_enabled and not frame.audio.output_enabled


def test_window_menu_replaces_content_and_remembers_instrument(desktop_session):
    frame, _ = desktop_session
    scope = frame.pages['scope']
    session = frame.session
    assert _shown_tools(frame) == [frame]
    _invoke(frame, 'Window', 'Oscilloscope')
    assert _shown_tools(frame) == [frame]
    assert scope.GetTopLevelParent() is frame and scope.IsShownOnScreen()
    assert 'Oscilloscope' in frame.GetTitle()
    assert frame.window_entries['scope'].IsChecked()
    _invoke(frame, 'Scope', 'XY Phase')
    _invoke(frame, 'Window', 'Session Results')
    assert _shown_tools(frame) == [frame]
    assert frame.pages['results'].GetTopLevelParent() is frame and not scope.IsShownOnScreen()
    _invoke(frame, 'Window', 'Oscilloscope')
    assert frame.pages['scope'] is scope and scope.mode.GetSelection() == 1
    assert _shown_tools(frame) == [frame] and frame.session is session


@pytest.mark.parametrize('layout', ('windows', 'tabs'))
def test_new_window_shares_session_and_exchanges_open_tools(desktop_session, layout):
    frame, driver = desktop_session
    assert frame.apply_settings(workspaceLayout=layout)
    session = frame.session
    scope = frame.pages['scope']
    measure = frame.pages['measure']
    tab_count = frame.workspace.book.GetPageCount()

    _invoke(frame, 'File', 'New Window', shortcut='Ctrl+Shift+N')
    first, second = _shown_tools(frame)
    assert first is frame and second is not frame
    assert frame._documents.frames == [frame] and frame.session is session
    assert measure.GetTopLevelParent() is first
    _invoke(second, 'Window', 'Oscilloscope')
    assert scope.GetTopLevelParent() is second
    assert 'Oscilloscope' in second.GetTitle()
    assert second.window_entries['scope'].IsChecked()
    if layout == 'tabs':
        assert frame.workspace.book.GetPageCount() == tab_count - 1
        assert frame.workspace.book.IsShownOnScreen()
    else:
        assert 'Wow & Flutter' in first.GetTitle()

    _invoke(second, 'Scope', 'XY Phase')
    _invoke(second, 'Window', 'Wow & Flutter')
    assert _shown_tools(frame) == [first, second]
    assert scope.GetTopLevelParent() is first and scope.mode.GetSelection() == 1
    assert measure.GetTopLevelParent() is second
    assert 'Wow & Flutter' in second.GetTitle()
    assert first.window_entries['scope'].IsChecked() and second.window_entries['measure'].IsChecked()
    _invoke(first, 'Scope', 'Waveform')
    assert scope.mode.GetSelection() == 0

    _invoke(second, 'File', 'Close Window')
    driver.Yield()
    assert _shown_tools(frame) == [frame] and frame.session is session
    if layout == 'tabs':
        assert frame.workspace.book.GetPageCount() == tab_count
        assert measure.GetTopLevelParent() is first


def test_new_session_leaves_dirty_recording_in_original_document(desktop_session, monkeypatch, tmp_path):
    frame, driver = desktop_session
    _start_loopback_run(frame)
    old_session = frame.session
    old_session['metadata']['equipment'] = 'First deck'
    old_path = tmp_path / 'original.fluttery'
    frame.results.session_path = old_path
    started = frame._run_started
    engine = frame.audio

    def unexpected_replace(**_options):
        pytest.fail('Opening a new session must not ask to replace the existing one.')

    monkeypatch.setattr(frame, 'confirm_replace_session', unexpected_replace)
    _invoke(frame, 'File', 'New Session', shortcut='Ctrl+N')
    driver.Yield()

    original, new = frame._documents.frames
    assert original is frame and new is not frame
    assert _shown_tools(original) == [original] and _shown_tools(new) == [new]
    assert new.session is not old_session and new.audio is not engine
    assert new.session['metadata']['equipment'] == '' and not new.session['history']
    assert new.results.session_path is None and not new._session_dirty and not new.is_measuring
    assert 'Untitled Session' in new.GetTitle() and new.GetTitle() != original.GetTitle()
    assert frame.session is old_session and frame.results.session_path == old_path
    assert old_session['metadata']['equipment'] == 'First deck' and frame._session_dirty
    assert frame._run_started == started and frame.is_measuring and engine.running

    _invoke(new, 'File', 'Close Window')
    driver.Yield()
    assert frame._documents.frames == [frame] and frame.is_measuring and engine.running


@pytest.mark.parametrize('layout', ('windows', 'tabs'))
def test_switching_live_measurement_can_keep_tool_or_stop_run(desktop_session, monkeypatch, layout):
    frame, driver = desktop_session
    assert frame.apply_settings(workspaceLayout=layout)
    _start_loopback_run(frame)
    started = frame._run_started
    labels = []
    answers = iter((wx.ID_NO, wx.ID_YES))

    class SwitchDialog:
        def __init__(self, parent, message, _title, _style):
            assert parent is frame and 'measurement' in message

        def __enter__(self):
            return self

        def __exit__(self, *_exception):
            return False

        def SetYesNoLabels(self, yes, no):
            labels.append((yes, no))

        def ShowModal(self):
            return next(answers)

    monkeypatch.setattr(wx, 'MessageDialog', SwitchDialog)

    def select_scope():
        if layout == 'tabs':
            book = frame.workspace.book
            scope_tab = next(index for index in range(book.GetPageCount()) if book.GetPageText(index) == 'Scope')
            book.SetSelection(scope_tab)
        else:
            # The native radio item checks itself before delivering the menu event.
            frame.window_entries['scope'].Check()
            _invoke(frame, 'Window', 'Oscilloscope')
        driver.Yield()

    select_scope()
    assert frame.current_page == 'measure' and frame.is_measuring and frame._run_started == started
    assert frame.window_entries['measure'].IsChecked() and not frame.window_entries['scope'].IsChecked()
    assert _shown_tools(frame) == [frame]

    select_scope()
    assert frame.current_page == 'scope' and not frame.is_measuring
    assert frame.window_entries['scope'].IsChecked() and _shown_tools(frame) == [frame]
    assert labels == [('Stop and Switch', 'Keep Current Tool')] * 2
