"""Native windows, menus and toolbars for the shared Fluttery workspace."""

from __future__ import annotations

import sys
from pathlib import Path

import wx

from .keyboard_access import plain_label
from .preferences import TAB_VALUES, TURNTABLE_LABELS, TURNTABLE_VALUES
from .toolbar_icons import toolbar_icon

TOOL_TITLES = {
    'measure': 'Wow & Flutter',
    'scope': 'Oscilloscope',
    'turntable': 'Turntable',
    'thd': 'Distortion Analyzer',
    'generate': 'Generator',
    'results': 'Session Results',
    'settings': 'Settings',
}
TOOL_KEYS = TAB_VALUES[:-1]
INSTRUMENT_MENUS = {
    'measure': 'Wow && Flutter', 'scope': 'Scope', 'turntable': 'Turntable',
    'thd': 'Distortion', 'generate': 'Generator', 'results': 'Results',
}
WINDOW_COMMANDS = (
    'Wow && Flutter', 'Oscilloscope', 'Turntable', 'Distortion Analyzer', 'Signal Generator', 'Session Results', 'Settings',
)
MEASURE_TOGGLE = 'Start or Stop Wow and Flutter'
HISTORY_CLEAR = 'Clear Wow and Flutter History'
MEASURE_SHORTCUT = 'Ctrl+Shift+Space' if sys.platform == 'darwin' else 'Ctrl+Space'
HISTORY_SHORTCUT = 'Ctrl+R'
COUNTER_SHORTCUT = 'Ctrl+Shift+T'
CLEAR_SESSION_SHORTCUT = 'Ctrl+Shift+E'
ICON = Path(__file__).resolve().parent / 'assets' / 'icon.png'
EXPORTS = (
    ('csv', 'CSV data'), ('json', 'JSON data'), ('pdf', 'PDF report'),
    ('html', 'Interactive HTML report'), ('zip', 'Complete ZIP archive'), ('png', 'Graph image'),
)
TAB_CAPTIONS = ('Measure', 'Scope', 'Turntable', 'THD', 'Generator', 'Results')
WINDOW_SIZES = {
    'scope': (820, 620), 'turntable': (780, 575), 'thd': (720, 505),
    'generate': (660, 560), 'results': (850, 620), 'settings': (730, 570),
}

MENU_HELP = {
    'Spectrum and Distortion Tests…': 'Open the live spectrum, two-tone intermodulation and stepped THD instruments.',
    'New Session': 'Open an empty session in its own window and keep the current session open.',
    'New Window': 'Open another tool window for the current session.',
    'Open Session…': 'Open a saved Fluttery session or a compatible JSON report.',
    'Save Session': 'Save the measurements and report details in a Fluttery session file.',
    'Save Session As…': 'Save the current session to a new location.',
    'Compare With Session…': 'Plot the measurements from another session alongside these results.',
    'Export': 'Choose a file format for the current session or report.',
    'CSV data…': 'Export the measurement history as spreadsheet rows.',
    'JSON data…': 'Export session data in a structured format that Fluttery can reopen.',
    'PDF report…': 'Write the selected report sections and charts to a printable PDF.',
    'Interactive HTML report…': 'Write a standalone report that can be viewed in a web browser.',
    'Complete ZIP archive…': 'Collect the available report and measurement files in one ZIP archive.',
    'Graph image…': 'Save a results plot as PNG, SVG or PDF with your chosen size and appearance.',
    'Print Report…': 'Prepare the current report and open the system print dialog.',
    'Close Window': 'Close this window. Other instruments can continue running.',
    'Quit Fluttery': 'Quit Fluttery. You will be asked about any unsaved changes first.',
    'Undo': 'Reverse the last edit in the focused control.',
    'Redo': 'Repeat the edit most recently reversed in the focused control.',
    'Cut': 'Move the selected text to the clipboard.',
    'Copy': 'Copy the selection to the clipboard.',
    'Paste': 'Insert text from the clipboard into the focused control.',
    'Select All': 'Select the contents of the focused control.',
    'Workspace Layout': 'Choose a tabbed workspace or separate windows.',
    'Single Tool per Window': 'Show one instrument per window. Use New Window to open another tool alongside it.',
    'Tabbed Workspace': 'Show instruments as tabs in the session window. Separate tool windows can stay open.',
    'Read Current Values…': 'Open a stable, screen-reader-friendly copy of the current instrument readings.',
    'Graph Settings…': 'Adjust trace weight, contrast, grid lines and the spectrogram colour scale.',
    'Larger Text': 'Increase the font size in Fluttery windows.',
    'Smaller Text': 'Reduce the font size in Fluttery windows.',
    'Reset Text Size': 'Restore the standard Fluttery font size.',
    'Toggle Full Screen': 'Enter or leave full screen in this window.',
    'Wow & Flutter': 'Open the speed, wow and flutter measurement window or tab.',
    'Oscilloscope': 'Open the oscilloscope window or tab.',
    'Waveform': 'Open the oscilloscope and plot the signal over time.',
    'XY Phase': 'Open the oscilloscope in XY mode to compare the phase of the left and right channels.',
    'Turntable': 'Open the turntable window or tab.',
    'Speed (RPM)': 'Measure the platter speed from a tone on a test record.',
    'Crosstalk': 'Measure how much signal leaks from one stereo channel into the other.',
    'Channel balance': 'Compare the relative levels of the left and right channels.',
    'Tracking distortion': 'Measure harmonic distortion while the stylus tracks a test tone.',
    'Noise floor': 'Measure the background level on the selected inputs.',
    'Resonance': 'Look for tonearm and cartridge resonance in the low-frequency spectrum.',
    'Distortion Analyzer': 'Measure THD and THD+N and view the FFT spectrum.',
    'Signal Tests': 'Open frequency response, spectrum, distortion, voltage and noise instruments.',
    'Cartridge Setup': 'Use a test record to set up the cartridge or phono stage.',
    'Tape Deck': 'Choose tape playback, recording, head alignment or bias measurement.',
    'Calibration': 'Open interface response, voltage and other calibration tools.',
    'Signal Generator': 'Open the test tone generator with waveform, frequency and modulation controls.',
    'Frequency Sweep…': 'Open the sweep generator to design, play or save a frequency sweep.',
    'Frequency Response…': 'Measure the frequency response of the return signal using a sweep or noise.',
    'Interface Response Calibration…': 'Capture and manage interface response correction profiles.',
    'Calibration Settings…': 'Open the Calibration section of Settings.',
    'AC Voltmeter…': 'Read the RMS and peak AC voltage for each channel.',
    'Voltage Calibration…': 'Set per-channel voltage calibration from a known reference.',
    'A-weighted Signal-to-Noise…': 'Compare a measured reference level with the A-weighted noise floor.',
    'Analyse Recorded Tone WAV…': 'Analyse a selected channel and time range of a test-tone recording.',
    'Save Unweighted Speed WAV…': 'Save the live demodulated speed signal before weighting.',
    'Tape Playback Response…': 'Measure playback using a frequency-response test tape.',
    'Tape Recording Response…': 'Measure a deck’s record-and-playback frequency response.',
    'Tape Head Azimuth…': 'Measure stereo phase alignment and delay from a head-alignment test tone.',
    'Tape Bias and Record Level…': 'Compare bias and recording-level settings using playback measurements.',
    'Tape End Settings…': 'Open the settings for stopping a measurement after a test tone ends.',
    'Anti-skate Assistant…': 'Compare the tracking distortion in the left and right channels of a test record.',
    'Tracking Force Assistant…': 'Compare tracking distortion at different measured tracking forces.',
    'Vertical Tracking Angle and Stylus Rake…': 'Compare intermodulation distortion while adjusting the tonearm height.',
    'Phono Loading…': 'Compare the phono stage’s response at different load settings.',
    'Phono Gain…': 'Compare phono-stage level, noise and headroom across gain settings.',
    'Session Results': 'Review measurements, plots, comparisons and report metadata for this session.',
    'Audio': 'Start or stop live capture or open Audio Settings.',
    'Start Live Input': 'Open the selected audio input and begin analysing its signal.',
    'Stop Live Input': 'Stop the live audio input. Any signal the generator is playing continues.',
    'Audio Settings…': 'Open Audio I/O in Settings to choose devices, channels and a sample rate.',
    MEASURE_TOGGLE: 'Start or stop a wow and flutter measurement run.',
    HISTORY_CLEAR: 'Remove the wow and flutter series while keeping the other measurements and report metadata.',
    'Clear Session…': 'Remove measurements, history and report metadata from the current session after confirmation.',
    'Minimize': 'Minimize this window to the Dock or taskbar.',
    'Zoom Window': 'Enlarge this window or return it to its previous size.',
    'Bring All to Front': 'Bring Fluttery’s open windows forward.',
    'Settings…': 'Open Fluttery preferences in a separate window.',
    'Settings': 'Open or return to the Settings window.',
    'Fluttery Help': 'Search the illustrated user manual for workflows, settings and instrument readings.',
    'Setup Wizard…': 'Review automatic updates and each settings pane in order.',
    'Keyboard Commands…': 'Find and run a command or focus a control using its two-letter shortcut.',
    'Describe Focused Control…': 'Read the help tag for the control with keyboard focus.',
    'Keyboard Shortcuts': 'Read the shortcuts that work throughout Fluttery and find those for this window.',
    'Settings Reference': 'Read the user manual’s reference tables for every Settings pane.',
    'Project Repository': 'Open the Fluttery project on GitHub in the default browser.',
    'Donate…': 'Open Fluttery’s optional donation page in the default browser.',
    'Send Feedback…': 'Start a new email to the Fluttery maintainer.',
    'Check for Updates…': 'Check the release feed for a newer version of Fluttery.',
    'About Fluttery': 'Show Fluttery’s version and project information.',
}


def title_for(key):
    return 'Fluttery' if key == 'workspace' else f'{TOOL_TITLES[key]} — Fluttery'


def _host(frame):
    root = wx.Panel(frame)
    contents = wx.BoxSizer(wx.VERTICAL)
    root.SetSizer(contents)
    holder = wx.BoxSizer(wx.VERTICAL)
    holder.Add(root, 1, wx.EXPAND)
    frame.SetSizer(holder)
    return root, contents


class DesktopWorkspace:
    """Place existing instrument panels in windows or tabs without recreating them."""

    def __init__(self, main, create_measurement_pages, results_page, settings_page):
        self.main = main
        root, contents = _host(main)
        self.main_root = root
        self.main_contents = contents
        self.book = wx.Notebook(root, name='Fluttery workspace')
        self.book.Hide()
        # Instruments have one panel per session. Hidden panels retain their
        # controls and analysis state, independently of the window showing them.
        self.parking = wx.Panel(root)
        self.parking.Hide()
        controls = {key: panel for key, (_, panel) in zip(
            TOOL_KEYS[:-1], create_measurement_pages(self.parking, main), strict=True,
        )}
        controls['results'] = results_page(self.parking, main)
        self.windows = {'measure': main}
        self.hosts = {'measure': (root, contents)}
        for key in TAB_VALUES[1:]:
            frame = wx.Frame(None, title=title_for(key), size=main.FromDIP(WINDOW_SIZES[key]))
            self.windows[key] = frame
            self.hosts[key] = _host(frame)
        controls['settings'] = settings_page(self.hosts['settings'][0], main)
        self.hosts['settings'][1].Add(controls['settings'], 1, wx.EXPAND)
        self.pages = controls
        self.layout = 'windows'
        self.active_key = 'measure'
        self.last_tool_key = 'measure'
        self._last_window = 'measure'
        self._tool_at = {}
        self._tab_keys = []
        self._moving = False
        self._workspace_menu_state = None
        self._window_menu_title = '&Window'
        self._menu_details = {}
        self._focus = {}
        self._message = 'Ready.'
        self._health = 'Audio stopped'
        self._place_tool('measure', 'measure')
        self.book.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGING, self._changing_tab)
        self.book.Bind(wx.EVT_NOTEBOOK_PAGE_CHANGED, self._selected_tab)

    def prepare_frames(self):
        if sys.platform == 'darwin':
            from .macos import keep_window_menu

            if not keep_window_menu():
                self._window_menu_title = 'Instruments'
        for slot, frame in self.windows.items():
            frame.SetIcon(wx.Icon(str(ICON), wx.BITMAP_TYPE_PNG))
            frame.SetTitle(title_for(self._instrument_key(slot) or slot))
            if slot != 'settings':
                frame.CreateStatusBar(2)
                frame.SetStatusWidths([-1, self.main.FromDIP(185)])
                frame.SetStatusText(self._message)
            frame.Bind(wx.EVT_ACTIVATE, lambda event, slot=slot: self._activate(event, slot))
            frame.Bind(wx.EVT_CLOSE, lambda event, slot=slot: self._close_window(event, slot))
            self._menu_for(frame, slot)
        self.sync_audio_status(self._health)
        if sys.platform == 'darwin':
            self._make_common_menubar()
        self.update_commands()

    def _instrument_key(self, slot):
        if slot == 'settings':
            return slot
        if slot == 'measure' and self.layout == 'tabs':
            index = self.book.GetSelection()
            if 0 <= index < len(self._tab_keys):
                return self._tab_keys[index]
            return self._tab_keys[0] if self._tab_keys else None
        return self._tool_at.get(slot)

    def _active_panel(self, slot):
        return self.pages.get(self._instrument_key(slot))

    def _slot_for_frame(self, frame):
        return next((slot for slot, window in self.windows.items() if window is frame), None)

    def _slot_for_tool(self, key):
        if key in self._tab_keys:
            return 'measure'
        return next((slot for slot, tool in self._tool_at.items() if tool == key), None)

    def _target_slot(self, frame=None):
        slot = self._slot_for_frame(frame or wx.GetActiveWindow())
        if slot in TOOL_KEYS and self.windows[slot].IsShown():
            return slot
        if self.windows[self._last_window].IsShown():
            return self._last_window
        return next((slot for slot, _ in self._open_windows() if slot in TOOL_KEYS), 'measure')

    def _place_panel(self, key, parent, contents=None):
        panel = self.pages[key]
        if (previous := panel.GetContainingSizer()) is not None:
            previous.Detach(panel)
        if panel.GetParent() is not parent:
            panel.Reparent(parent)
        if contents is not None:
            contents.Add(panel, 1, wx.EXPAND)
            panel.Show()
        return panel

    def _place_tool(self, slot, key):
        parent, contents = self.hosts[slot]
        self._place_panel(key, parent, contents)
        self._tool_at[slot] = key

    def _park_tool(self, slot):
        if (key := self._tool_at.pop(slot, None)) is not None:
            self._place_panel(key, self.parking)

    def _insert_tab(self, key, index=None):
        if index is None:
            index = sum(TOOL_KEYS.index(other) < TOOL_KEYS.index(key) for other in self._tab_keys)
        # GTK needs a native direct child created with the notebook as parent.
        host = wx.Panel(self.book)
        contents = wx.BoxSizer(wx.VERTICAL)
        host.SetSizer(contents)
        self._place_panel(key, host, contents)
        self._tab_keys.insert(index, key)
        self.book.InsertPage(index, host, TAB_CAPTIONS[TOOL_KEYS.index(key)])

    def _remove_tab(self, key):
        index = self._tab_keys.index(key)
        host = self.book.GetPage(index)
        self._place_panel(key, self.parking)
        self.book.RemovePage(index)
        self._tab_keys.pop(index)
        host.Destroy()
        return index

    def _select_tab(self, key):
        index = self._tab_keys.index(key)
        if self.book.GetSelection() != index:
            self.book.ChangeSelection(index)

    def _remember_focus(self, slot):
        panel = self._active_panel(slot)
        focus = wx.Window.FindFocus()
        if panel is not None and focus is not None and panel.IsDescendant(focus):
            self._focus[self._instrument_key(slot)] = focus

    def _restore_focus(self, slot):
        key = self._instrument_key(slot)
        focus = self._focus.get(key)
        if focus and focus.IsShownOnScreen() and focus.IsEnabled():
            focus.SetFocus()
        elif slot == 'measure' and self.layout == 'tabs':
            self.book.SetFocus()
        elif (panel := self.pages.get(key)) is not None:
            panel.SetFocus()

    def _measurement_to_stop(self, previous, key, view=None):
        panel = self.pages.get(previous)
        if previous == 'turntable' and (previous != key or (view and view != panel.active_tool)):
            if panel.recording is not None:
                return lambda: panel._toggle_recording() if panel.recording is not None else None
            if panel._capture_request is not None:
                return panel._cancel_separation
        if previous != key and previous == 'measure' and self.main.is_measuring:
            return self.main.stop_measurement
        if previous != key and previous == 'thd' and panel.running:
            return lambda: panel._toggle() if panel.running else None
        return None

    def _confirm_switch(self, slot, key, *, view=None):
        previous = self._instrument_key(slot)
        stops = [self._measurement_to_stop(previous, key, view)]
        other = self._slot_for_tool(key)
        if other is not None and other != slot and self.windows[other].IsShown() and self._instrument_key(other) == key:
            # Swapping tools also replaces the other window's visible tool.
            stops.append(self._measurement_to_stop(key, previous))
        stops = [stop for stop in stops if stop is not None]
        if not stops:
            return True
        message = ('A measurement is running in one of the windows affected by this change.'
                   if other is not None and other != slot else 'A measurement is running in this window.')
        with wx.MessageDialog(self.windows[slot], message, 'Switch Tool',
                              wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION) as dialog:
            dialog.SetYesNoLabels('Stop and Switch', 'Keep Current Tool')
            if dialog.ShowModal() != wx.ID_YES:
                return False
        for stop in stops:
            stop()
        return True

    def _changing_tab(self, event):
        if not self._moving and 0 <= event.GetSelection() < len(self._tab_keys):
            target = self._tab_keys[event.GetSelection()]
            if not self._confirm_switch('measure', target):
                event.Veto()
                # The toolkit finishes rolling back the native selection
                # after the event returns. Sync the menu to the retained tab.
                self._workspace_menu_state = None
                wx.CallAfter(self.update_commands)
                return
            self._remember_focus('measure')
        event.Skip()

    def _selected_tab(self, event):
        index = event.GetSelection()
        if (event.GetEventObject() == self.book and self.layout == 'tabs' and not self._moving
                and 0 <= index < len(self._tab_keys)):
            self._set_active('measure')
            self.update_commands(('measure',))
            self.main.play_sound('switch')
        event.Skip()

    def _set_active(self, slot):
        key = self._instrument_key(slot)
        if key is None:
            return
        self.active_key = key
        if slot != 'settings':
            self.last_tool_key = key
            self._last_window = slot
        self.main.maybe_start_live_input(key)

    def _activate(self, event, slot):
        if event.GetActive() and not self._moving and self.windows[slot].IsShown():
            self.main.activate_session()
            self._set_active(slot)
        event.Skip()

    def _open_windows(self):
        return [(slot, frame) for slot, frame in self.windows.items() if frame.IsShown()]

    def owns(self, frame):
        return frame in self.windows.values()

    @property
    def parent(self):
        active = wx.GetActiveWindow()
        if self.owns(active) and active.IsShown():
            return active
        preferred = self.frame_for_key(self.active_key)
        if preferred.IsShown():
            return preferred
        windows = self._open_windows()
        return windows[-1][1] if windows else None

    @staticmethod
    def _bring_forward(frame):
        frame.Show()
        if frame.IsIconized():
            frame.Iconize(False)
        frame.Raise()

    def frame_for_key(self, key):
        if key == 'settings':
            return self.windows[key]
        return self.windows[self._slot_for_tool(key) or self._target_slot()]

    def reopen(self):
        if (frame := self.parent) is not None:
            self._bring_forward(frame)
        else:
            self.navigate(self.last_tool_key, sound=False)

    def _replace_tool(self, slot, key):
        previous = self._instrument_key(slot)
        if key == previous:
            return
        source = self._slot_for_tool(key)
        tabbed = self.layout == 'tabs'
        if source == slot:
            self._select_tab(key)
            return
        if source == 'measure' and tabbed:
            selected = self._instrument_key('measure')
            index = self._remove_tab(key)
            self._park_tool(slot)
            if previous is not None:
                self._insert_tab(previous, index)
            if selected == key:
                selected = previous
            if selected in self._tab_keys:
                self._select_tab(selected)
        elif source is not None:
            self._park_tool(source)
            if slot == 'measure' and tabbed:
                index = self._remove_tab(previous)
                self._place_tool(source, previous)
                self._insert_tab(key, index)
                self._select_tab(key)
                return
            self._park_tool(slot)
            if previous is not None:
                self._place_tool(source, previous)
        else:
            self._park_tool(slot)
            if slot == 'measure' and tabbed:
                self._insert_tab(key)
                self._select_tab(key)
                return
        self._place_tool(slot, key)

    def navigate(self, key, view=None, *, sound=True, frame=None):
        if key not in self.pages:
            return None
        panel = self.pages[key]
        previous = self.active_key
        if key == 'settings':
            window = self.windows[key]
            focus_categories = not window.IsShown() or view is not None
            self._bring_forward(window)
            if view is not None:
                panel.select_section(view)
            if focus_categories:
                panel.categories.SetFocus()
            self._set_active(key)
        else:
            slot = self._target_slot(frame)
            if not self._confirm_switch(slot, key, view=view):
                # The native radio command may already have changed its mark.
                self._workspace_menu_state = None
                self._sync_workspace_menus()
                return None
            changed_tool = self._instrument_key(slot) != key
            if changed_tool:
                self._remember_focus(slot)
            self._moving = True
            try:
                self._replace_tool(slot, key)
                if view is not None and callable(set_view := getattr(panel, 'set_view', None)):
                    set_view(view)
                self._bring_forward(self.windows[slot])
                self._set_active(slot)
                self.update_commands()
                if changed_tool:
                    self._restore_focus(slot)
            finally:
                self._moving = False
        if previous != key and sound:
            self.main.play_sound('switch')
        return panel

    def new_window(self, frame=None):
        current = self._target_slot(frame)
        if not self.windows[current].IsShown():
            return self.navigate(self.last_tool_key, frame=frame)
        slots = TOOL_KEYS[1:] if self.layout == 'tabs' else TOOL_KEYS
        free = next((slot for slot in slots if slot not in self._tool_at), None)
        used = set(self._tool_at.values())
        if self.layout == 'tabs':
            candidate = next((key for key in self._tab_keys if key != self._instrument_key('measure')), None)
        else:
            candidate = next((key for key in TOOL_KEYS if key not in used), None)
        if free is None or candidate is None:
            self.main.announce('Every instrument in this session already has its own window.')
            return None
        self._moving = True
        try:
            self._replace_tool(free, candidate)
            window = self.windows[free]
            window.CentreOnScreen()
            self._bring_forward(window)
            self._set_active(free)
            self.update_commands()
            self._restore_focus(free)
        finally:
            self._moving = False
        return window

    def switch_layout(self, layout, *, show=True):
        if layout == self.layout:
            return
        previous = self._instrument_key('measure') or 'measure'
        self._moving = True
        try:
            if layout == 'tabs':
                self._park_tool('measure')
                detached = set(self._tool_at.values())
                for key in TOOL_KEYS:
                    if key not in detached:
                        self._insert_tab(key)
                self.main_contents.Add(self.book, 1, wx.EXPAND)
                self.book.Show()
                if previous in self._tab_keys:
                    self._select_tab(previous)
            else:
                self.main_contents.Detach(self.book)
                self.book.Hide()
                for key in tuple(self._tab_keys):
                    self._remove_tab(key)
                self._place_tool('measure', previous)
            self.layout = layout
            self.main.Layout()
        finally:
            self._moving = False
        if show:
            self._set_active(self._slot_for_frame(self.parent) or self._target_slot())
            self.update_commands()

    def _close_window(self, event, slot):
        if self.main._closing:
            event.Skip()
            return
        if not event.CanVeto():
            self.main.quit(event)
            return
        visible = [(name, frame) for name, frame in self._open_windows() if name != slot and name in TOOL_KEYS]
        if slot != 'settings' and not visible and (sys.platform != 'darwin' or self.main.has_other_sessions):
            self.main.quit(event)
            return
        event.Veto()
        self.windows[slot].Hide()
        if slot != 'settings' and (slot != 'measure' or (visible and self.layout == 'windows')):
            self._moving = True
            try:
                key = self._instrument_key(slot)
                self._park_tool(slot)
                if key is not None and self.layout == 'tabs':
                    selected = self._instrument_key('measure')
                    self._insert_tab(key)
                    if selected is not None:
                        self._select_tab(selected)
            finally:
                self._moving = False
        if visible:
            next_slot, window = next((entry for entry in visible if entry[0] == self._last_window), visible[-1])
            self._bring_forward(window)
            self._set_active(next_slot)
        elif slot == 'settings':
            self.navigate(self.last_tool_key, sound=False)
        else:
            self.main.maybe_start_live_input(None)
        self.update_commands()

    def set_message(self, text):
        self._message = text
        for frame in self.windows.values():
            if (status := frame.GetStatusBar()) is not None:
                status.SetStatusText(text)

    def sync_audio_status(self, text):
        self._health = text
        for frame in self.windows.values():
            if (status := frame.GetStatusBar()) is not None:
                status.SetStatusText(text, 1)

    def update_commands(self, keys=None):
        if self.main._closing:
            return
        open_windows = self._open_windows()
        self._sync_workspace_menus()
        for slot in keys or tuple(name for name, _ in open_windows):
            frame = self.windows[slot]
            instrument = self._instrument_key(slot)
            if instrument not in TOOL_KEYS:
                continue
            title = title_for('workspace' if slot == 'measure' and self.layout == 'tabs' else instrument)
            path = self.main.results.session_path
            label = path.stem if path else f'Untitled Session {self.main.session_number}' if self.main.session_number > 1 else ''
            if label:
                title = f'{title} — {label}'
            if frame.GetTitle() != title:
                frame.SetTitle(title)
            panel = self.pages[instrument]
            primary = getattr(panel, 'desktop_actions', tuple)()
            secondary = getattr(panel, 'desktop_options', tuple)()
            self._toolbar(frame, panel, primary)
            self._tool_menu(frame, instrument, panel, primary, secondary)
            frame.Layout()

    @staticmethod
    def _tool_help(panel, key, label):
        help_for = getattr(panel, 'desktop_help', None)
        if callable(help_for) and (details := help_for(key)):
            return details
        return MENU_HELP.get(plain_label(label), plain_label(label) + '.')

    def _toolbar(self, frame, panel, actions):
        signature = panel, tuple((key, label) for key, label, _ in actions)
        if getattr(frame, 'toolbar_signature', ()) == signature:
            return
        toolbar = frame.GetToolBar()
        if toolbar is None:
            toolbar = frame.CreateToolBar(style=wx.TB_HORIZONTAL | wx.TB_FLAT | wx.TB_TEXT | wx.TB_HORZ_LAYOUT)
            frame._toolbar_dispatch = {}
            frame.Bind(wx.EVT_TOOL, lambda event: self._tool_clicked(event, frame))
        toolbar.ClearTools()
        frame._toolbar_dispatch.clear()
        frame._toolbar_help = {}
        # Keep stable IDs for a transport action as its icon and title change.
        ids = getattr(frame, '_toolbar_ids', {})
        for key, label, callback in actions:
            if key not in ids:
                ids[key] = wx.NewIdRef()
            identifier = ids[key]
            bitmap = toolbar_icon(key, label)
            details = self._tool_help(panel, key, label)
            frame._toolbar_help[int(identifier)] = details
            toolbar.AddTool(int(identifier), plain_label(label), bitmap,
                            shortHelp=details if self.main.settings['showHelpTags'] else '')
            frame._toolbar_dispatch[int(identifier)] = (key, callback)
        frame._toolbar_ids = ids
        frame.toolbar_signature = signature
        toolbar.Realize()
        frame.Layout()

    def _tool_clicked(self, event, frame):
        command = frame._toolbar_dispatch.get(event.GetId())
        if command is None:
            event.Skip()
            return
        key, callback = command
        self._invoke(key, callback)

    def _invoke(self, key, callback):
        if key not in {'measure', 'counter', 'reset_counter', 'analysis', 'generate', 'record'}:
            self.main.play_sound('button')
        callback()
        self.update_commands()

    def menu_description(self, identifier):
        return self._menu_details.get(identifier, '')

    def _set_menu_help(self, item):
        enabled = self.main.settings['showHelpTags']
        item.SetHelp(self.menu_description(item.GetId()) if enabled else '')
        if not enabled and item.GetHelp():
            # A stock wx command gets the stock description when its help string is empty.
            item.SetHelp(' ')

    def _submenu(self, menu, label, submenu):
        item = menu.AppendSubMenu(submenu, label)
        self._menu_details[item.GetId()] = MENU_HELP[plain_label(label)]
        self._set_menu_help(item)
        return item

    def _item(self, frame, menu, title, callback, identifier=wx.ID_ANY, *, kind=wx.ITEM_NORMAL, help_text=''):
        label = plain_label(title)
        details = help_text or MENU_HELP.get(label, label.rstrip('.') + '.')
        item = menu.Append(identifier, title, kind=kind)
        self._menu_details[item.GetId()] = details
        self._set_menu_help(item)
        frame.Bind(wx.EVT_MENU, lambda event: callback(), item)
        return item

    def _remove_menu_item(self, frame, menu, item):
        identifier = item.GetId()
        self._menu_details.pop(identifier, None)
        frame.Unbind(wx.EVT_MENU, id=identifier)
        menu.Delete(item)

    def _instrument_entries(self, frame, instrument, primary, secondary):
        if instrument == 'measure':
            yield 'measure', f'{MEASURE_TOGGLE}\t{MEASURE_SHORTCUT}', self.main.toggle_measurement
            for key, label, callback in primary:
                if key == 'reset_counter':
                    yield key, f'{label}\t{COUNTER_SHORTCUT}', callback
                elif key != 'measure':
                    yield key, label, callback
            yield 'clear_history', f'{HISTORY_CLEAR}\t{HISTORY_SHORTCUT}', self.main.clear_history
            yield 'tape_end_settings', 'Tape End Settings…', lambda: self.navigate('settings', 'measurement')
        elif instrument in ('scope', 'turntable'):
            views = (('waveform', 'Waveform'), ('xy', 'XY Phase')) if instrument == 'scope' else zip(
                TURNTABLE_VALUES, TURNTABLE_LABELS, strict=True,
            )
            for mode, title in views:
                yield f'view_{mode}', title, lambda mode=mode: self.navigate(instrument, mode, frame=frame)
            yield from primary
        else:
            yield from primary
        for key, label, callback in secondary:
            if instrument != 'results' or key not in {'new', 'save-as', 'compare', 'print'}:
                yield key, label, callback

    def _tool_menu(self, frame, instrument, panel, primary, secondary):
        entries = tuple(self._instrument_entries(frame, instrument, primary, secondary))
        frame.instrument_commands = entries
        if frame.instrument_key != instrument:
            frame.GetMenuBar().SetMenuLabel(frame.instrument_menu_index, INSTRUMENT_MENUS[instrument])
            frame.instrument_key = instrument
            self._set_measurement_accelerators(frame, instrument)

        # Reuse existing native menu items for label changes and keep the menu
        # snapshot ready before AppKit or a screen reader asks to inspect it.
        menu = frame.instrument_menu
        for entry in frame.instrument_items[len(entries):]:
            self._remove_menu_item(frame, menu, entry)
        del frame.instrument_items[len(entries):]
        for index, (key, caption, _) in enumerate(entries):
            label, tab, shortcut = caption.partition('\t')
            native_caption = plain_label(label).replace('&', '&&') + tab + shortcut
            details = self._tool_help(panel, key, label)
            if index < len(frame.instrument_items):
                entry = frame.instrument_items[index]
                if entry.GetItemLabel() != native_caption:
                    entry.SetItemLabel(native_caption)
                if self.menu_description(entry.GetId()) != details:
                    self._menu_details[entry.GetId()] = details
                    self._set_menu_help(entry)
            else:
                entry = self._item(frame, menu, native_caption,
                                   lambda index=index: self._invoke_instrument(frame, index), help_text=details)
                frame.instrument_items.append(entry)

    def _invoke_instrument(self, frame, index):
        if index < len(frame.instrument_commands):
            key, _, callback = frame.instrument_commands[index]
            self._invoke(key, callback)

    def background_measurement_shortcuts(self, frame):
        if getattr(frame, 'instrument_key', None) != 'measure':
            yield MEASURE_TOGGLE, MEASURE_SHORTCUT, self.main.toggle_measurement, MENU_HELP[MEASURE_TOGGLE]
            yield HISTORY_CLEAR, HISTORY_SHORTCUT, self.main.clear_history, MENU_HELP[HISTORY_CLEAR]

    @staticmethod
    def _set_measurement_accelerators(frame, instrument):
        # Preserve the existing measurement and history shortcuts in other
        # instruments without adding a second command to their menus.
        if frame.hidden_measurement_items:
            for entry in frame.hidden_measurement_items:
                entry.Enable(instrument != 'measure')
            return
        extra = wx.NullAcceleratorTable if instrument == 'measure' else frame.measurement_accelerators
        frame.SetAcceleratorTable(extra)

    def _bind_measurement_accelerators(self, frame, instrument, file):
        frame.measurement_ids = (wx.NewIdRef(), wx.NewIdRef())
        toggle, clear = (int(identifier) for identifier in frame.measurement_ids)
        frame.hidden_measurement_items = ()
        if sys.platform == 'darwin':
            from .macos import hide_menu_shortcuts

            # Some native controls consume Space before wx's frame-level
            # accelerators see it. Cocoa menu key equivalents run first.
            entries = (
                self._item(frame, file, f'{MEASURE_TOGGLE}\t{MEASURE_SHORTCUT}', lambda: self.main.toggle_measurement(), toggle),
                self._item(frame, file, f'{HISTORY_CLEAR}\t{HISTORY_SHORTCUT}', lambda: self.main.clear_history(), clear),
            )
            if hide_menu_shortcuts(file, (MEASURE_TOGGLE, HISTORY_CLEAR)):
                frame.hidden_measurement_items = entries
                self._set_measurement_accelerators(frame, instrument)
                return
            for entry in entries:
                self._remove_menu_item(frame, file, entry)
        modifiers = wx.ACCEL_CMD | (wx.ACCEL_SHIFT if sys.platform == 'darwin' else 0)
        frame.measurement_accelerators = wx.AcceleratorTable([
            (modifiers, wx.WXK_SPACE, toggle), (wx.ACCEL_CMD, ord('R'), clear),
        ])
        frame.Bind(wx.EVT_MENU, lambda event: self.main.toggle_measurement(), id=toggle)
        frame.Bind(wx.EVT_MENU, lambda event: self.main.clear_history(), id=clear)
        self._set_measurement_accelerators(frame, instrument)

    def _tools_menu(self, item):
        tools = wx.Menu()
        cartridge = wx.Menu()
        for key, caption in (('antiskate', 'Anti-skate Assistant…'), ('tracking-force', 'Tracking Force Assistant…'),
                             ('vta', 'Vertical Tracking Angle and Stylus Rake…'), ('phono-loading', 'Phono Loading…'),
                             ('phono-gain', 'Phono Gain…')):
            item(cartridge, caption, lambda key=key: self.main.show_alignment(key))
        self._submenu(tools, '&Cartridge Setup', cartridge)
        signals = wx.Menu()
        item(signals, 'Frequency Response…', self.main.show_response)
        item(signals, 'Spectrum and Distortion Tests…', self.main.show_bench_analyzer)
        item(signals, 'AC Voltmeter…', self.main.show_level_meter)
        item(signals, 'A-weighted Signal-to-Noise…', lambda: self.main.show_level_meter('snr'))
        self._submenu(tools, '&Signal Tests', signals)
        tape = wx.Menu()
        item(tape, 'Tape Playback Response…', lambda: self.main.show_response('tape_playback'))
        item(tape, 'Tape Recording Response…', lambda: self.main.show_response('tape_recording'))
        item(tape, 'Tape Head Azimuth…', lambda: self.main.show_alignment('head-azimuth'))
        item(tape, 'Tape Bias and Record Level…', lambda: self.main.show_alignment('tape-bias'))
        self._submenu(tools, 'Tape &Deck', tape)
        calibration = wx.Menu()
        item(calibration, 'Interface Response Calibration…', lambda: self.main.show_response('interface'))
        item(calibration, 'Voltage Calibration…', lambda: self.main.show_level_meter('calibration'))
        item(calibration, 'Calibration Settings…', lambda: self.navigate('settings', 'calibration'))
        self._submenu(tools, '&Calibration', calibration)
        audio = wx.Menu()
        item(audio, 'Start Live Input', lambda: self.main.ensure_audio(require_physical=True))
        item(audio, 'Stop Live Input', self.main.stop_input)
        item(audio, 'Audio Settings…', lambda: self.navigate('settings', 'audio'))
        self._submenu(tools, '&Audio', audio)
        return tools

    def _window_menu(self, item, frame=None):
        windows = wx.Menu()
        if frame is not None:
            minimize = '&Minimize\tCtrl+M' if sys.platform == 'darwin' else '&Minimize'
            item(windows, minimize, frame.Iconize)
            item(windows, '&Zoom Window', lambda: frame.Maximize(not frame.IsMaximized()))
            if sys.platform == 'darwin' and self._window_menu_title == '&Window':
                from .macos import arrange_windows

                item(windows, 'Bring All to Front', arrange_windows)
            windows.AppendSeparator()
        # Keep these launch commands available even when their windows are
        # closed. The same command selects the instrument in tabbed layout.
        for number, (key, label) in enumerate(zip(TAB_VALUES, WINDOW_COMMANDS, strict=True), 1):
            command = item(windows, f'{label}\tCtrl+{number}', lambda key=key: self.navigate(key, frame=frame),
                           kind=wx.ITEM_RADIO if frame is not None else wx.ITEM_NORMAL)
            if frame is not None:
                frame.window_entries[key] = command
        return windows

    def _make_common_menubar(self):
        # macOS leaves the app running when its last window closes. The common
        # menu gives users a way to open an instrument again from the menu bar.
        controller = self.main
        bar = wx.MenuBar()

        def item(menu, title, callback, identifier=wx.ID_ANY, *, kind=wx.ITEM_NORMAL):
            details = MENU_HELP[plain_label(title)]
            command = menu.Append(identifier, title, kind=kind)
            self._menu_details[command.GetId()] = details
            self._set_menu_help(command)
            menu.Bind(wx.EVT_MENU, lambda event: callback(), command)
            return command

        file = wx.Menu()
        item(file, '&New Session\tCtrl+N', controller.new_session_window, wx.ID_NEW)
        item(file, 'New &Window\tCtrl+Shift+N', self.new_window)
        item(file, '&Open Session…\tCtrl+O', controller.results.open_session, wx.ID_OPEN)
        item(file, '&Settings…\tCtrl+,', lambda: self.navigate('settings'), wx.ID_PREFERENCES)
        item(file, '&Quit Fluttery\tCtrl+Q', controller.quit_application, wx.ID_EXIT)
        bar.Append(file, '&File')
        bar.Append(self._tools_menu(item), '&Tools')
        bar.Append(self._window_menu(item), self._window_menu_title)
        help_menu = wx.Menu()
        item(help_menu, 'Fluttery Help\tF1', controller._show_guide, wx.ID_HELP)
        item(help_menu, 'Setup Wizard…', controller.show_setup_wizard)
        item(help_menu, 'Keyboard Commands…\tCtrl+Shift+K', controller.keyboard.show_commands)
        item(help_menu, 'Keyboard Shortcuts', controller._show_shortcuts)
        item(help_menu, 'Settings Reference', controller._show_settings_reference)
        item(help_menu, 'Check for Updates…', lambda: controller.check_updates(manual=True))
        item(help_menu, 'Project Repository', controller.open_project_repository)
        item(help_menu, 'Donate…', controller.open_donation_page)
        item(help_menu, 'Send Feedback…', controller.open_feedback)
        item(help_menu, 'About Fluttery', controller._show_about, wx.ID_ABOUT)
        bar.Append(help_menu, '&Help')
        self._common_bar = bar

    def _menu_for(self, frame, key):
        controller = self.main
        bar = wx.MenuBar()
        def item(menu, title, action, identifier=wx.ID_ANY, **options):
            return self._item(frame, menu, title, action, identifier, **options)

        file = wx.Menu()
        item(file, '&New Session\tCtrl+N', controller.new_session_window, wx.ID_NEW)
        item(file, 'New &Window\tCtrl+Shift+N', lambda: self.new_window(frame))
        item(file, '&Open Session…\tCtrl+O', controller.results.open_session, wx.ID_OPEN)
        item(file, '&Save Session\tCtrl+S', controller.results.save_session, wx.ID_SAVE)
        item(file, 'Save Session &As…\tCtrl+Shift+S', lambda: controller.results.save_session(save_as=True), wx.ID_SAVEAS)
        item(file, 'Compare With Session…', controller.results.open_comparison)
        exports = wx.Menu()
        for extension, label in EXPORTS:
            item(exports, f'{label}…', lambda extension=extension: controller.results.export_report(extension))
        self._submenu(file, '&Export', exports)
        item(file, '&Print Report…\tCtrl+P', controller.results.print_report, wx.ID_PRINT)
        item(file, f'Clear Session…\t{CLEAR_SESSION_SHORTCUT}', controller.clear_session)
        self._bind_measurement_accelerators(frame, key, file)
        file.AppendSeparator()
        item(file, '&Close Window\tCtrl+W', frame.Close, wx.ID_CLOSE)
        item(file, '&Quit Fluttery\tCtrl+Q', controller.quit_application, wx.ID_EXIT)
        bar.Append(file, '&File')

        edit = wx.Menu()
        for identifier, label, method in (
            (wx.ID_UNDO, '&Undo\tCtrl+Z', 'Undo'), (wx.ID_REDO, '&Redo\tCtrl+Shift+Z', 'Redo'),
            (wx.ID_CUT, 'Cu&t\tCtrl+X', 'Cut'), (wx.ID_COPY, '&Copy\tCtrl+C', 'Copy'),
            (wx.ID_PASTE, '&Paste\tCtrl+V', 'Paste'), (wx.ID_SELECTALL, 'Select &All\tCtrl+A', 'SelectAll'),
        ):
            item(edit, label, lambda method=method: controller._edit_action(method), identifier)
        edit.AppendSeparator()
        item(edit, '&Settings…\tCtrl+,', lambda: self.navigate('settings'), wx.ID_PREFERENCES)
        bar.Append(edit, '&Edit')

        view = wx.Menu()
        layouts = wx.Menu()
        frame.layout_items = {}
        for setting, title in (('windows', 'Single Tool per Window'), ('tabs', 'Tabbed Workspace')):
            frame.layout_items[setting] = item(
                layouts, title, lambda setting=setting: controller.apply_settings(workspaceLayout=setting),
                kind=wx.ITEM_RADIO,
            )
        self._submenu(view, '&Workspace Layout', layouts)
        item(view, 'Read Current Values…\tCtrl+Shift+R', controller.read_current)
        item(view, 'Graph Settings…', lambda: self.navigate('settings', 'graphs'))
        view.AppendSeparator()
        item(view, 'Larger Text\tCtrl+=', lambda: controller._move_text_scale(10), wx.ID_ZOOM_IN)
        item(view, 'Smaller Text\tCtrl+-', lambda: controller._move_text_scale(-10), wx.ID_ZOOM_OUT)
        item(view, 'Reset Text Size\tCtrl+0', lambda: controller.apply_settings(textScale=100), wx.ID_ZOOM_100)
        item(view, 'Toggle Full Screen\tF11', lambda: frame.ShowFullScreen(not frame.IsFullScreen()))
        bar.Append(view, '&View')

        bar.Append(self._tools_menu(item), '&Tools')
        if key in INSTRUMENT_MENUS:
            frame.instrument_key = key
            frame.instrument_menu = wx.Menu()
            frame.instrument_items = []
            frame.instrument_menu_index = bar.GetMenuCount()
            bar.Append(frame.instrument_menu, INSTRUMENT_MENUS[key])
        frame.window_entries = {}
        frame.window_menu = self._window_menu(item, frame)
        bar.Append(frame.window_menu, self._window_menu_title)

        help_menu = wx.Menu()
        item(help_menu, 'Fluttery Help\tF1', controller._show_guide, wx.ID_HELP)
        item(help_menu, 'Setup Wizard…', controller.show_setup_wizard)
        item(help_menu, 'Keyboard Commands…\tCtrl+Shift+K', lambda: controller.keyboard.show_commands(frame))
        item(help_menu, 'Describe Focused Control…\tCtrl+Shift+H', controller.keyboard.show_focused_help)
        item(help_menu, 'Keyboard Shortcuts', controller._show_shortcuts)
        item(help_menu, 'Settings Reference', controller._show_settings_reference)
        item(help_menu, 'Check for Updates…', lambda: controller.check_updates(manual=True))
        help_menu.AppendSeparator()
        item(help_menu, 'Project Repository', controller.open_project_repository)
        item(help_menu, 'Donate…', controller.open_donation_page)
        item(help_menu, 'Send Feedback…', controller.open_feedback)
        item(help_menu, 'About Fluttery', controller._show_about, wx.ID_ABOUT)
        bar.Append(help_menu, '&Help')
        frame.SetMenuBar(bar)

    def _sync_workspace_menus(self):
        # On macOS, VoiceOver can make AppKit simulate a menu opening just to
        # inspect its items. Keep Python callbacks and menu rebuilds out of that
        # native event path. Update the snapshot when the workspace changes.
        state = self.layout, tuple(self._instrument_key(slot) for slot in self.windows)
        if state == self._workspace_menu_state:
            return
        for slot, frame in self.windows.items():
            frame.layout_items[self.layout].Check()
            if (key := self._instrument_key(slot)) is not None:
                frame.window_entries[key].Check()
        self._workspace_menu_state = state

    def refresh_help_tags(self):
        enabled = self.main.settings['showHelpTags']

        def update_menu(menu):
            for item in menu.GetMenuItems():
                if item.IsSeparator():
                    continue
                self._set_menu_help(item)
                if item.GetSubMenu() is not None:
                    update_menu(item.GetSubMenu())

        bars = [frame.GetMenuBar() for frame in self.windows.values()]
        if getattr(self, '_common_bar', None) is not None:
            bars.append(self._common_bar)
        for bar in bars:
            for index in range(bar.GetMenuCount()):
                update_menu(bar.GetMenu(index))
        for frame in self.windows.values():
            toolbar = frame.GetToolBar()
            if toolbar is not None:
                for identifier, details in getattr(frame, '_toolbar_help', {}).items():
                    toolbar.SetToolShortHelp(identifier, details if enabled else '')

    def destroy_secondary(self):
        for key, frame in self.windows.items():
            if key != 'measure':
                frame.Destroy()
