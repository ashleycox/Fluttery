"""Exercise live native menu state with a desktop session."""

import os

import pytest
import wx

from fluttery.app import FlutteryFrame
from fluttery.desktop import COUNTER_SHORTCUT, HISTORY_SHORTCUT, MEASURE_SHORTCUT, MEASURE_TOGGLE
from fluttery.keyboard_access import MODIFIER, plain_label

pytestmark = pytest.mark.skipif(os.getenv('FLUTTERY_GUI_TESTS') != '1', reason='requires an active desktop')


def _menu(frame, title):
    bar = frame.GetMenuBar()
    return next(bar.GetMenu(index) for index in range(bar.GetMenuCount()) if plain_label(bar.GetMenuLabel(index)) == title)


def _items(menu):
    return {item.GetItemLabelText(): item for item in menu.GetMenuItems() if not item.IsSeparator()}


def _invoke(frame, item):
    frame.ProcessWindowEvent(wx.CommandEvent(wx.EVT_MENU.typeId, item.GetId()))


def test_saved_response_comparison_in_results_menu():
    driver = wx.GetApp() or wx.App(False)
    frame = FlutteryFrame(settings={'autoStartInput': False, 'enableUISounds': False}, automatic_updates=False)
    try:
        frame.record_bench_result('frequency-response', {
            'frequencyHz': [100, 1000], 'profile': 'Bench interface', 'correctionApplied': False,
            'leftDb': [-8, -6], 'rawLeftDb': [-8, -6], 'correctedLeftDb': [-5, -3],
        })
        frame.navigate('results', 'measurements')
        panel = frame.pages['results'].measurements
        assert panel.detail_view.GetStringSelection() == 'Details'
        options = _items(_menu(frame, 'Results'))
        assert options['Compare with Raw'].GetItemLabel().endswith('\tCtrl+Shift+B')
        _invoke(frame, options['Compare with Raw'])
        assert panel.detail_view.GetStringSelection() == 'Graph'
        assert panel.compare_raw.IsShown() and panel.compare_raw.GetValue()
        assert [item['label'] for item in panel.graph.traces] == ['Corrected left gain (dB)', 'Raw left gain (dB)']
        assert panel.points.rows[0] == ('100', '-5', '-8')
        _invoke(frame, _items(_menu(frame, 'Results'))['Hide Raw Comparison'])
        assert not panel.compare_raw.GetValue() and panel.points.rows[0] == ('100', '-8')
    finally:
        frame.session_saved()
        frame.quit()
        driver.Yield()


def test_window_launchers_and_context_menus(monkeypatch):
    driver = wx.GetApp() or wx.App(False)
    frame = FlutteryFrame(settings={
        'autoStartInput': False, 'enableUISounds': False, 'announceStatus': False,
    }, automatic_updates=False)
    try:
        frame.navigate('measure')
        titles = ['Wow & Flutter', 'Oscilloscope', 'Turntable', 'Distortion Analyzer', 'Signal Generator', 'Session Results', 'Settings']
        listing = tuple(frame.window_entries.values())
        assert [item.GetItemLabelText() for item in listing] == titles
        assert all(item.IsEnabled() for item in listing)
        assert [item.GetItemLabel().split('\t')[1] for item in listing] == [f'Ctrl+{n}' for n in range(1, 8)]
        assert set(_items(_menu(frame, 'Tools'))) == {'Cartridge Setup', 'Signal Tests', 'Tape Deck', 'Calibration', 'Audio'}
        measure = _items(_menu(frame, 'Wow & Flutter'))
        assert [label for label in measure if label.startswith(('Start', 'Stop')) and 'Wow and Flutter' in label] == [MEASURE_TOGGLE]
        assert measure[MEASURE_TOGGLE].GetItemLabel().endswith('\t' + MEASURE_SHORTCUT)
        assert measure['Reset Counter'].GetItemLabel().endswith('\t' + COUNTER_SHORTCUT)
        assert measure['Clear Wow and Flutter History'].GetItemLabel().endswith('\t' + HISTORY_SHORTCUT)
        reset, = (command for command in frame.keyboard.commands(frame).values() if command.label == 'Reset Counter')
        assert reset.native_shortcut == f'{MODIFIER}+Shift+T'
        launch_scope, = (cmd for cmd in frame.keyboard.commands(frame).values() if cmd.label == 'Oscilloscope')
        assert launch_scope.available and launch_scope.native_shortcut == f'{MODIFIER}+2'
        launch_scope.action()
        assert frame.pages['scope'].GetTopLevelParent() is frame
        assert [window for _, window in frame.workspace._open_windows()] == [frame]
        _invoke(frame, _items(_menu(frame, 'File'))['New Window'])
        scope = frame.workspace.frame_for_key('measure')
        assert scope.IsShown()
        _invoke(frame, frame.window_entries['measure'])
        assert frame.pages['measure'].GetTopLevelParent() is frame
        assert frame.pages['scope'].GetTopLevelParent() is scope
        _invoke(scope, _items(_menu(scope, 'Scope'))['XY Phase'])
        assert frame.pages['scope'].mode.GetSelection() == 1
        _invoke(scope, _items(_menu(scope, 'Scope'))['Waveform'])
        assert frame.pages['scope'].mode.GetSelection() == 0
        assert frame.window_entries['measure'].IsChecked() and scope.window_entries['scope'].IsChecked()

        # A native menu-open event may come from VoiceOver inspecting the
        # menu, even when the user has not picked a command.
        frame.ProcessWindowEvent(wx.MenuEvent(wx.EVT_MENU_OPEN.typeId, wx.ID_ANY, frame.window_menu))
        assert tuple(frame.window_entries.values()) == listing

        scope.Close()
        assert not scope.IsShown() and frame.window_entries['scope'].IsEnabled()
        assert tuple(frame.window_entries.values()) == listing

        frame.workspace.switch_layout('tabs')
        assert frame.layout_items['tabs'].IsChecked()
        _invoke(frame, frame.window_entries['turntable'])
        _invoke(frame, _items(_menu(frame, 'Turntable'))['Crosstalk'])
        assert frame.current_page == 'turntable'
        assert frame.pages['turntable'].active_tool == 'crosstalk' and not frame.workspace.windows['turntable'].IsShown()
        assert frame.window_entries['turntable'].IsChecked() and not frame.window_entries['measure'].IsChecked()
        toggle = []
        monkeypatch.setattr(frame, 'toggle_measurement', lambda: toggle.append('toggle'))
        frame.ProcessWindowEvent(wx.CommandEvent(wx.EVT_MENU.typeId, int(frame.measurement_ids[0])))
        _invoke(frame, frame.window_entries['measure'])
        _invoke(frame, _items(_menu(frame, 'Wow & Flutter'))[MEASURE_TOGGLE])
        assert toggle == ['toggle', 'toggle']
        clear_session = _items(_menu(frame, 'File'))['Clear Session…']
        assert clear_session.GetItemLabel().endswith('\tCtrl+Shift+E')
        frame.session['history'].append({'peak': 1.0})
        monkeypatch.setattr(frame, 'confirm', lambda *_: False)
        _invoke(frame, clear_session)
        assert frame.session['history']
        monkeypatch.setattr(frame, 'confirm', lambda *_: True)
        _invoke(frame, clear_session)
        assert not frame.session['history']
        frame.session_saved()
    finally:
        frame.quit()
        driver.Yield()
