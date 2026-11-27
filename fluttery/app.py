"""Fluttery's native desktop window and application controller."""

from __future__ import annotations

import ctypes
import sys
import time
import webbrowser
from collections import deque
from contextlib import ExitStack
from copy import deepcopy
from http.client import HTTPException
from pathlib import Path
from threading import Thread
from typing import ClassVar
from weakref import WeakKeyDictionary

import wx
import wx.adv

from . import __version__, macos, updates
from .audio import AudioEngine
from .audio.recordings import TapeEndDetector
from .audio_ownership import AudioOwnership
from .desktop import ICON, DesktopWorkspace
from .keyboard_access import KeyboardAccess, describe, widget_tree
from .measurement_ui import create_measurement_pages
from .preferences import (
    PROFILE_KEYS,
    load_settings,
    normalize_settings,
    save_settings,
)
from .results import ResultsPage
from .sessions import atomic_write, load_session, new_session
from .settings_ui import SettingsPage, device_options
from .setup_wizard import SetupWizard
from .ui_sounds import InterfaceSounds

SESSION_SETTING_KEYS = (*PROFILE_KEYS, 'meterDamping', 'correctionFactor', 'channelCalibration', 'exportSettings',
                        'inputDevice', 'outputDevice', 'sampleRate', 'headAzimuthOffsetUs', 'voltageCalibrations',
                        'responseCalibrations', 'benchLevelOptions', 'responseTestDefaults', 'alignmentOptions',
                        'maxSessionSamples', 'keepCompleteHistory', 'keepSignalLossDetails', 'includeInputAudio')
LIVE_PAGES = ('measure', 'scope', 'turntable', 'thd', 'generate')
AUTO_INPUT_PAGES = ('measure', 'scope', 'turntable')
FEEDBACK_ADDRESS = 'mailto:info@theblindmansworkshop.com'
DONATION_PAGE = 'https://www.paypal.com/donate/?hosted_button_id=WMLX4PLED9PRE'
CLOCK_WARNING = 'The clock correction is outside the usable range for this audio input.'
SHARED_PREFERENCES = frozenset(('colorMode', 'showHelpTags', 'checkForUpdates', 'setupWizardSeen'))
PREFERENCE_CATALOGS = {
    'measurementProfiles': lambda record: record['id'],
    'responseCalibrations': lambda record: record['name'].casefold(),
    'sweepPresets': lambda record: record['name'].casefold(),
    'voltageCalibrations': lambda record: (record['device'], record['rate']),
}


def _generated_signal_active(snapshot):
    return bool(snapshot.get('generator', {}).get('enabled') or snapshot.get('sweep', {}).get('active'))


def _loopback_signal_active(snapshot):
    sweep = snapshot.get('sweep', {})
    if sweep.get('active'):
        return bool(sweep.get('loopback'))
    return bool(snapshot.get('loopback') and snapshot.get('generator', {}).get('enabled'))


def _stored_spectrum(spectrum):
    frequencies = spectrum.get('frequencies', ())
    levels = spectrum.get('db', ())
    step = max(1, (len(levels) + 127) // 128)
    spacing = float(frequencies[1] - frequencies[0]) if len(frequencies) > 1 else 0.0
    # Retain peaks across the entire band, including scrape flutter well above
    # 200 Hz, while keeping long-session reports within their existing limits.
    start = float(frequencies[0]) + (step - 1) * spacing / 2 if len(frequencies) else 0.0
    return {'spectrum': [float(max(levels[position:position + step])) for position in range(0, len(levels), step)],
            'spectrumEncoding': 'db', 'spectrumStartHz': start, 'spectrumBinHz': spacing * step}


class _SessionDocuments:
    """Coordinate application services while each session owns its measurements."""

    def __init__(self, app):
        self.app = app
        self.frames = []
        self.next_session_number = 1
        self.active = None
        self.preferences = None
        self.audio = AudioOwnership()
        self.update_owner = None
        self.quitting = False

    @classmethod
    def current_app(cls):
        app = wx.GetApp()
        if (documents := getattr(app, '_fluttery_documents', None)) is None:
            documents = app._fluttery_documents = cls(app)
        return documents

    def add(self, frame):
        if not self.frames:
            self.preferences = deepcopy(frame._persistent_settings)
        self.frames.append(frame)
        if frame._automatic_updates:
            self.update_owner = frame
        if self.active is None:
            self.activate(frame)

    def activate(self, frame):
        if frame in self.frames and self.active is not frame:
            self.active = frame
            self.app.frame = frame
            self.app.SetTopWindow(frame)
            if sys.platform == 'darwin':
                wx.MenuBar.MacSetCommonMenuBar(frame.workspace._common_bar)

    def remove(self, frame):
        self.frames.remove(frame)
        if self.update_owner is frame:
            self.update_owner = None
            if self.frames and not self.quitting:
                replacement = self.active if self.active in self.frames else self.frames[-1]
                self.update_owner = replacement
                replacement._automatic_updates = True
                replacement._update_service_started = frame._update_service_started
                replacement._sparkle = frame._sparkle
                replacement._seen_update = frame._seen_update
                replacement.update_timer.Start(12 * 60 * 60 * 1000)
                if not replacement._update_service_started:
                    wx.CallAfter(replacement._start_update_service)
        if self.active is frame:
            self.active = None
            if self.frames:
                self.activate(self.frames[-1])
            else:
                self.app.frame = None
                if sys.platform == 'darwin':
                    wx.MenuBar.MacSetCommonMenuBar(None)

    @staticmethod
    def _merge_catalog(base, original, edited, identify):
        if base == original:
            return edited
        previous = {identify(record): record for record in original}
        desired = {identify(record): record for record in edited}
        latest = {identify(record): record for record in base}
        for key in previous.keys() - desired.keys():
            latest.pop(key, None)
        for key, record in desired.items():
            if previous.get(key) != record:
                latest[key] = record
        return list(latest.values())

    def save_preferences(self, frame):
        from .session_setup import setting_differences, with_values

        edited = frame._persistent_settings
        original = frame._last_saved_settings
        # Existing sessions keep their working setup. Saving in one session
        # writes its changed settings over the latest application defaults.
        changes = dict(setting_differences(edited, original, replace=True))
        for key, identify in PREFERENCE_CATALOGS.items():
            if (key,) in changes:
                changes[key,] = self._merge_catalog(self.preferences[key], original[key], edited[key], identify)
        preferences = normalize_settings(with_values(self.preferences, changes))
        # Only the process-wide owner can lift a pending physical interlock.
        preferences['interfaceLoopbackPendingDisconnect'] = frame._audio_ownership.persisted_loopback(frame)
        save_settings(preferences)
        frame._last_saved_settings = deepcopy(edited)
        self.preferences = preferences
        shared = {key: preferences[key] for key in SHARED_PREFERENCES if (key,) in changes}
        if shared:
            for other in self.frames:
                if other is frame:
                    continue
                # Theme mode and native help are application-wide. Sound,
                # measurement and audio settings remain with the session.
                other._change_settings({**other._persistent_settings, **shared}, other._session_overrides)
                other._last_saved_settings.update(shared)

    def quit(self, event=None):
        if self.quitting:
            return False
        self.quitting = True
        try:
            frames = tuple(self.frames)
            if (event is None or event.CanVeto()) and any(
                not frame.confirm_replace_session(closing=True) for frame in frames
            ):
                if event is not None:
                    event.Veto()
                return False
            for frame in frames:
                frame.quit(confirmed=True)
            return True
        finally:
            self.quitting = False


class FlutteryFrame(wx.Frame):
    """Own one session, its workspace and the audio used for its measurements."""

    def __init__(self, *, audio=None, settings=None, automatic_updates=True, audio_ownership=None):
        super().__init__(None, title='Fluttery', size=(820, 610))
        if wx.HelpProvider.Get() is None:
            wx.HelpProvider.Set(wx.SimpleHelpProvider())
        self.settings = load_settings() if settings is None else normalize_settings(settings)
        self._persistent_settings = deepcopy(self.settings)
        self._documents = _SessionDocuments.current_app()
        self.session_number = self._documents.next_session_number
        self._documents.next_session_number += 1
        self._audio_ownership = audio_ownership or self._documents.audio
        self._session_overrides = {}
        self.session = new_session(self.settings['technician'])
        self.audio = AudioEngine(self.settings) if audio is None else audio
        if audio is not None:
            audio.configure(self.settings)
        self.sounds = InterfaceSounds()
        self.debug_log = deque(maxlen=500)
        self.is_measuring = False
        self.measurement_elapsed = 0.0
        self._run_started = 0.0
        self._run_clock_origin = 0.0
        self._run_stream_offset = 0.0
        self._run_target_duration = 0.0
        self._run_worst = 0.0
        self._run_valid = False
        self._run_uncertain = False
        self._run_lost_tone = False
        self._run_options = {}
        self._run_spectrum_time = -float('inf')
        self._run_signal_loss = None
        self._run_input_capture = None
        self._last_sequence = None
        self._last_health = None
        self._last_debug_tick = 0.0
        self._last_results_tick = 0.0
        self._auto_input_handled = False
        self._auto_input_page = None
        self._auto_input_pending = False
        self._microphone_pending = False
        self._microphone_resume = None
        self._microphone_denied = None
        self._microphone_interactive = False
        self._closing = False
        self._calibrating = False
        self._setup_wizard = None
        self._sweep_dialog = None
        self._sweep_run_id = None
        self.instrument_dialogs = {}
        self._bench_capture = None
        self._lab_signal_owner = None
        self._difference_owner = None
        self._input_identity = '<default input>'
        self._output_identity = '<default output>'
        self._tape_end_detector = None
        self._tape_end_seen = False
        self._initial_setup_pending = False
        self._setup_open_path = None
        self._automatic_updates = automatic_updates
        self._update_service_started = False
        self._update_stage = None
        self._manual_update_check = False
        self._seen_update = None
        self._sparkle = None
        self._session_dirty = False
        self._settings_save_pending = False
        self._results_need_refresh = False
        self._notification = None
        self._configure_monitor()
        self._apply_appearance()
        self.workspace = DesktopWorkspace(self, create_measurement_pages, ResultsPage, SettingsPage)
        self.book = self.workspace.book
        self.pages = self.workspace.pages
        self.results = self.pages['results']
        self.settings_page = self.pages['settings']
        self.keyboard = KeyboardAccess(self)
        self.workspace.prepare_frames()
        self.workspace.switch_layout(self.settings['workspaceLayout'], show=False)
        self._scaled_fonts = WeakKeyDictionary()
        self._apply_text_scale()
        self._bind_sounds()
        self.keyboard.refresh()

        # Keep UI polling independent of capture. The processor can send multiple
        # measurement updates between ticks, and inactive views can skip painting.
        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._tick, self.timer)
        self.timer.Start(67)
        self.update_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._check_background_updates, self.update_timer)
        if automatic_updates:
            self.update_timer.Start(12 * 60 * 60 * 1000)
            wx.CallLater(4000, self._start_update_service)
        self._audio_ownership.register(self)
        self._last_saved_settings = deepcopy(self._persistent_settings)
        self._documents.add(self)

    def show_startup(self, *, offer_setup=False):
        from .staging import cleanup_session_staging

        cleanup_session_staging()
        self._initial_setup_pending = offer_setup and not self.settings['setupWizardSeen']
        key = self.settings['defaultStartTab']
        self.workspace.frame_for_key(key).Centre()
        self.workspace.navigate(key, sound=False)
        if self.settings['interfaceLoopbackPendingDisconnect']:
            wx.CallAfter(self.show_response, 'interface')
        elif self._initial_setup_pending:
            wx.CallAfter(self.show_setup_wizard, first_launch=True)

    def show_setup_wizard(self, *, first_launch=False):
        if self._closing or self._setup_wizard is not None:
            return
        if first_launch and not self._initial_setup_pending:
            return  # A manual invocation may already have handled the first run.
        first_launch = self._initial_setup_pending
        parent, previous_focus = self.workspace.parent, wx.Window.FindFocus()
        with SetupWizard(parent, self, first_launch=first_launch) as wizard:
            self._setup_wizard = wizard
            try:
                self._apply_text_scale()
                result = wizard.ShowModal()
            finally:
                if wizard:
                    for widget in widget_tree(wizard, include_composite_children=True):
                        self._scaled_fonts.pop(widget, None)
                self._setup_wizard = None
        if self._closing:
            return
        self.announce('Setup complete.' if result == wx.ID_OK else 'Setup wizard closed.')
        if parent and parent.IsShown():
            parent.Raise()
            if previous_focus and parent.IsDescendant(previous_focus) and previous_focus.IsShownOnScreen():
                previous_focus.SetFocus()
        if first_launch:
            # A deliberate dismissal also lets the user start normally next time.
            self._initial_setup_pending = False
            self.apply_settings(setupWizardSeen=True)
            self._auto_input_page = None
            self.maybe_start_live_input(self.current_page)
            if self._automatic_updates:
                wx.CallAfter(self._start_update_service)
        if self._setup_open_path is not None:
            path, self._setup_open_path = self._setup_open_path, None
            wx.CallAfter(self.open_path, path)

    @property
    def dialog_parent(self):
        if self._setup_wizard is not None:
            return self._setup_wizard
        if self._sweep_dialog is not None and self._sweep_dialog.IsActive():
            return self._sweep_dialog
        for dialog in self.instrument_dialogs.values():
            if dialog and dialog.IsActive():
                return dialog
        return self.workspace.parent

    def _settings_display(self):
        return self._setup_wizard.form if self._setup_wizard is not None else self.settings_page

    def _edit_action(self, method):
        focus = wx.Window.FindFocus()
        action = getattr(focus, method, None)
        if callable(action):
            action()

    def _bind_sounds(self):
        # Observe the event before dispatch so native controls can consume their events.
        self._sound_filter = _SoundFilter(self)
        wx.EvtHandler.AddFilter(self._sound_filter)

    def play_sound(self, name):
        # The system's default output may be the device under test.
        if self.settings['enableUISounds'] and self._audio_ownership.permits_sounds():
            self.sounds.play(self.settings['theme'], name)

    @property
    def current_page(self):
        return self.workspace.active_key

    def navigate(self, page, view=None):
        return self.workspace.navigate(page, view)

    @property
    def has_other_sessions(self):
        return len(self._documents.frames) > 1

    def activate_session(self):
        self._documents.activate(self)

    def new_session_window(self):
        if self._closing or self._documents.quitting:
            return False
        # A new session owns its own file, results and engine. It inherits the
        # saved defaults and leaves the original document and stream in place.
        frame = FlutteryFrame(settings=self._documents.preferences, automatic_updates=False,
                              audio_ownership=self._audio_ownership)
        frame.activate_session()
        frame.show_startup()
        frame.announce('New session opened.')
        return True

    def _apply_appearance(self):
        appearance = {'auto': wx.App.Appearance.System, 'light': wx.App.Appearance.Light, 'dark': wx.App.Appearance.Dark}
        app = wx.App.Get()
        if app is not None:
            app.SetAppearance(appearance[self.settings['colorMode']])

    def _move_text_scale(self, change):
        self.apply_settings(textScale=self.settings['textScale'] + change)

    def _apply_text_scale(self):
        factor = self.settings['textScale'] / 100

        def update(root):
            widgets = tuple(widget_tree(root, include_composite_children=True))
            choices = {window: window.GetSelection() for window in widgets if isinstance(window, wx.Choice)}
            # Read inherited fonts before changing any ancestor. GTK can
            # otherwise record an already scaled font for a child.
            for window in widgets:
                if window not in self._scaled_fonts:
                    self._scaled_fonts[window] = window.GetFont()
            for window in widgets:
                original = self._scaled_fonts[window]
                if original.IsOk():
                    window.SetFont(original.Scaled(factor))
            # Cocoa can clear the selection when rebuilding a popup for a new font.
            for window, selected in choices.items():
                if window.GetSelection() != selected:
                    window.SetSelection(selected)
            for window in reversed(widgets):
                window.Layout()
                if isinstance(window, wx.ScrolledWindow):
                    window.FitInside()

        # Windows have different natural footprints. Increase each minimum only
        # when larger interface text needs more room for native control labels.
        minimum_sizes = {'measure': (620, 450), 'scope': (610, 420), 'turntable': (650, 470),
                         'thd': (610, 435), 'generate': (530, 320), 'results': (670, 480),
                         'settings': (600, 465)}
        for key, frame in self.workspace.windows.items():
            update(frame)
            instrument = self.workspace._instrument_key(key) or key
            base = (680, 480) if key == 'measure' and self.workspace.layout == 'tabs' else minimum_sizes[instrument]
            minimum = frame.FromDIP(tuple(round(length * max(1.0, factor)) for length in base))
            frame.SetMinClientSize(minimum)
            if not frame.IsMaximized() and not frame.IsFullScreen():
                width, height = frame.GetClientSize()
                if width < minimum[0] or height < minimum[1]:
                    frame.SetClientSize((max(width, minimum[0]), max(height, minimum[1])))
        if self._setup_wizard is not None:
            update(self._setup_wizard)
            self._setup_wizard.apply_text_scale(factor)
            self._setup_wizard.relayout()
            # Applying text size inside Next must advance from the original page
            # before the larger or smaller controls alter its page boundaries.
            self._setup_wizard.queue_refit()
        if self._sweep_dialog is not None:
            update(self._sweep_dialog)
        for dialog in self.instrument_dialogs.values():
            if not dialog:
                continue
            update(dialog)
            if callable(refresh := getattr(dialog, 'refresh_display_settings', None)):
                refresh()

    def save_settings(self):
        try:
            self._documents.save_preferences(self)
        except (OSError, ValueError) as error:
            self._settings_save_pending = True
            self.show_error('Could not save settings', error)
            return False
        self._settings_save_pending = False
        return True

    def apply_settings(self, **changes):
        from .session_setup import setting_differences

        effective = normalize_settings({**self.settings, **changes})
        updates = {path: value for path, value in setting_differences(effective, self.settings, replace=True)
                   if path[0] in changes}
        return self._apply_setting_updates(updates)

    def _apply_setting_updates(self, updates):
        from .session_setup import with_values

        persistent = normalize_settings(with_values(self._persistent_settings, updates))
        overrides = {path: value for path, value in self._session_overrides.items()
                     if not any(path[:len(update)] == update or update[:len(path)] == path for update in updates)}
        # The permission prompt can outlive other settings edits. Store the
        # requested values and reapply them to the current preferences on grant.
        return self._change_settings(persistent, overrides, save=True,
                                     after_permission=lambda: self._apply_setting_updates(updates))

    def _change_settings(self, persistent, overrides, *, save=False, require_route=False, after_permission=None):
        from .session_setup import with_values

        before = self.settings
        candidate = normalize_settings(with_values(persistent, overrides))
        if before == candidate:
            old_persistent = self._persistent_settings
            self._persistent_settings, self._session_overrides = persistent, overrides
            return not save or (old_persistent == persistent and not self._settings_save_pending) or self.save_settings()
        was_running = self.audio.running
        was_input, was_output = self.audio.input_enabled, self.audio.output_enabled
        route_changed = any(before[key] != candidate[key] for key in ('inputDevice', 'outputDevice', 'sampleRate', 'channels'))
        if route_changed and (self._calibrating or before['interfaceLoopbackPendingDisconnect']):
            self.announce('Finish calibration and confirm the loopback cable is disconnected before changing audio devices.'
                          if before['interfaceLoopbackPendingDisconnect'] else
                          'Wait for calibration to finish before changing audio devices.')
            return False
        monitor_started = candidate['monitoring'] and not before['monitoring']
        if monitor_started and not was_input and not self._allow_microphone(
            after_permission or (lambda: self._change_settings(persistent, overrides, save=save, require_route=require_route))
        ):
            return False
        if self.is_measuring and (route_changed or any(before[key] != candidate[key] for key in (
            'targetFreq', 'weightingStandard', 'flutterBand', 'correctionFactor', 'detectorTiming',
            'weightingProfile', 'splitFrequency', 'useDCBlocker', 'useAutoTarget',
        ))):
            self._record(self.audio.snapshot())
        self.settings = candidate
        self.audio.configure(candidate)
        self._configure_monitor()
        monitor_stopped = before['monitoring'] and not candidate['monitoring']
        if (route_changed and was_running) or monitor_started or (monitor_stopped and was_output):
            try:
                generating = _generated_signal_active(self.audio.snapshot())
                output_needed = candidate['monitoring'] or (was_output and (not monitor_stopped or generating))
                analysis_only = was_running and not (was_input or was_output) and route_changed
                if was_input or monitor_started or output_needed or analysis_only:
                    self._start_audio(was_input or monitor_started, bool(output_needed))
                    if output_needed and not self.audio.output_enabled and (monitor_started or require_route):
                        # Input-only fallback is useful for capture, but cannot monitor live audio.
                        warning = self.audio.snapshot().get('health', {}).get('outputWarning')
                        raise RuntimeError(warning or 'Select an available audio output device to monitor the input.')
                else:
                    self.audio.stop()
            except (OSError, RuntimeError, ValueError) as error:
                self.settings = before
                self.audio.configure(before)
                self._configure_monitor()
                if was_running:
                    try:
                        self._start_audio(was_input, was_output)
                    except (OSError, RuntimeError, ValueError) as recovery:
                        self.log(f'Restoring previous audio stream: {recovery}')
                elif self.audio.running:
                    self.audio.stop()
                self.show_error('Could not change audio settings', error)
                return False
        self._persistent_settings, self._session_overrides = persistent, overrides
        if candidate['colorMode'] != before['colorMode']:
            self._apply_appearance()
            for frame in self.workspace.windows.values():
                frame.toolbar_signature = ()
            self.workspace.update_commands()
        layout_changed = candidate['workspaceLayout'] != before['workspaceLayout']
        if layout_changed:
            self.workspace.switch_layout(candidate['workspaceLayout'])
        if layout_changed or candidate['textScale'] != before['textScale']:
            self._apply_text_scale()
        if candidate['autoStartInput'] and not before['autoStartInput']:
            self._auto_input_handled = self.audio.input_enabled
            self._auto_input_page = None
            self.maybe_start_live_input(self.current_page)
        if before['showHelpTags'] != candidate['showHelpTags']:
            self.keyboard.refresh()
            self.workspace.refresh_help_tags()
        if before['enableUISounds'] and not candidate['enableUISounds']:
            self.sounds.stop()
        if self._sparkle is not None and candidate['checkForUpdates'] != before['checkForUpdates']:
            self._sparkle.set_automatic(candidate['checkForUpdates'])
        metadata = self.session['metadata']
        if candidate['technician'] and candidate['technician'] != before['technician'] and not metadata['technician']:
            if self._session_has_data():
                self.session_changed()
            metadata['technician'] = candidate['technician']
        if (not self.session.get('settings') and self._session_has_data()
                and any(candidate[key] != before[key] for key in SESSION_SETTING_KEYS)):
            self._session_dirty = True
        self._refresh_settings_views()
        return not save or self.save_settings()

    def _refresh_settings_views(self):
        for key, page in self.pages.items():
            if key != 'settings' and callable(refresh := getattr(page, 'refresh_settings', None)):
                refresh()
        self.settings_page.refresh_settings(preserve_edits=True)
        if self._setup_wizard is not None:
            self._setup_wizard.form.refresh_settings(preserve_edits=True)
        if self._sweep_dialog is not None:
            self._sweep_dialog.refresh_display_settings()
        for dialog in self.instrument_dialogs.values():
            if dialog and callable(refresh := getattr(dialog, 'refresh_display_settings', None)):
                refresh()
        self._results_need_refresh = True

    def _configure_monitor(self):
        difference = self._difference_owner is not None
        self.audio.set_monitor(not self._audio_ownership.loopback_pending and
                               not self.settings['interfaceLoopbackPendingDisconnect'] and
                               (difference or self.settings['monitoring']),
                               min(0.35, self.settings['monitoringVolume']) if difference else self.settings['monitoringVolume'],
                               'difference' if difference else self.settings['monitoringMode'])

    def _start_audio(self, input_enabled, output_enabled):
        settings = self.settings
        devices = self.audio.devices() if input_enabled or output_enabled else []
        indices = {}
        for direction, enabled in (('input', input_enabled), ('output', output_enabled)):
            selected = settings[f'{direction}Device']
            indices[direction] = None
            if selected and enabled:
                matches = {key: index for key, _, index in device_options(devices, direction)}
                if selected not in matches:
                    raise ValueError(f'The selected audio {direction} device is unavailable. Choose another device in Audio I/O.')
                indices[direction] = matches[selected]
        if self.is_measuring:
            self._record(self.audio.snapshot())
        with self._audio_ownership.route(self, physical=input_enabled or output_enabled):
            self._configure_monitor()
            changed = self.audio.start(indices['input'], indices['output'], settings['sampleRate'] or None,
                                       settings['channels'], input_enabled=input_enabled, output_enabled=output_enabled)
        if changed is not False:
            for direction in ('input', 'output'):
                device_index = getattr(self.audio, f'{direction}_device_index', None)
                identity = next((key for key, _, index in device_options(devices, direction) if index == device_index),
                                settings[f'{direction}Device'] or f'<default {direction}>')
                setattr(self, f'_{direction}_identity', identity)
            self._last_sequence = None
            # PortAudio reopens reset the DSP sample clock. Retain the point in the
            # session's timeline at which capture resumed, including the interruption.
            if self.is_measuring:
                self._run_stream_offset = time.monotonic() - self._run_started
                self._run_uncertain = True
                self._run_gap('The audio stream was reopened.', self._run_clock_origin + self._run_stream_offset)
            self.log(f'Audio started. Input {input_enabled}, output {output_enabled}, {settings["channels"]} channel(s).')

    def audio_input_identity(self):
        return self._input_identity

    def audio_output_identity(self):
        return self._output_identity

    def maybe_start_live_input(self, page):
        previous_page, self._auto_input_page = self._auto_input_page, page
        if (not self._initial_setup_pending and page != previous_page and page in AUTO_INPUT_PAGES and self.settings['autoStartInput']
                and not (self._auto_input_handled or self._auto_input_pending or self._microphone_pending)):
            self._auto_input_pending = True
            # Show the tool before asking the operating system to open its input.
            wx.CallAfter(self._start_automatic_input)

    def _start_automatic_input(self):
        self._auto_input_pending = False
        if (self._closing or self._auto_input_handled or not self.settings['autoStartInput']
                or self._auto_input_page not in AUTO_INPUT_PAGES):
            return
        if self.audio.input_enabled:
            self._auto_input_handled = True
        else:
            # Keep automatic start available on the next tool visit if the device is unplugged.
            self.ensure_audio(interactive=False)

    def _microphone_error(self, error, interactive):
        # Permission errors need a user decision in System Settings. A different
        # tool should not reopen the same privacy notice automatically.
        self._auto_input_handled = True
        if interactive:
            self.show_error('Microphone access', error)
        else:
            self.announce(str(error))

    def _allow_microphone(self, resume, *, interactive=True, on_denied=None):
        if sys.platform != 'darwin':
            return True
        try:
            status = macos.microphone_authorization()
            if status is None or status == macos.MICROPHONE_AUTHORIZED:
                return True
            if status != macos.MICROPHONE_UNDETERMINED:
                raise PermissionError('In System Settings, open Privacy & Security → Microphone and allow Fluttery to access the microphone.')
            if not self._microphone_pending:
                self._microphone_pending = True
                self._audio_ownership.request_microphone(self, lambda complete: macos.request_microphone_access(
                    lambda granted: wx.CallAfter(complete, granted),
                ))
            if self._microphone_resume is None or interactive:
                if self._microphone_denied is not None:
                    self._microphone_denied()
                self._microphone_resume = resume
                self._microphone_denied = on_denied
                self._microphone_interactive = interactive
        except (OSError, RuntimeError, ValueError) as error:
            self._microphone_pending = False
            self._microphone_resume = None
            self._microphone_denied = None
            self._microphone_error(error, interactive)
            if on_denied is not None:
                on_denied()
        return False

    def _microphone_answered(self, granted):
        self._microphone_pending = False
        resume, self._microphone_resume = self._microphone_resume, None
        denied, self._microphone_denied = self._microphone_denied, None
        if self._closing or resume is None:
            return
        if granted:
            resume()
        else:
            self._microphone_error('In System Settings, open Privacy & Security → Microphone and allow Fluttery to access the microphone.',
                                   self._microphone_interactive)
            if denied is not None:
                denied()

    def ensure_audio(self, input_enabled=True, output_enabled=None, *, interactive=True, after_permission=None,
                     require_physical=False):
        requested_input = input_enabled
        snapshot = self.audio.snapshot()
        # Internal loopback is usable even on a computer without audio hardware.
        generator_on = _generated_signal_active(snapshot)
        software_loopback = _loopback_signal_active(snapshot)
        if software_loopback and not require_physical and not self.audio.input_enabled:
            input_enabled = False
        if input_enabled and not self.audio.input_enabled:
            resume = after_permission or (lambda: self.ensure_audio(
                True, output_enabled, interactive=interactive, require_physical=require_physical,
            ))
            if not self._allow_microphone(resume, interactive=interactive):
                return False
        if output_enabled is None:
            output_enabled = bool(self.audio.output_enabled or (input_enabled and self.settings['monitoring']) or (
                generator_on and not software_loopback
            ))
        if self.audio.running and (self.audio.input_enabled, self.audio.output_enabled) == (input_enabled, output_enabled):
            if requested_input:
                self._auto_input_handled = True
            return True
        try:
            self._start_audio(input_enabled, output_enabled)
        except (OSError, RuntimeError, ValueError) as error:
            if interactive:
                self.show_error('Could not start audio', error)
            else:
                self.announce(f'Could not start the live audio input. {error}')
            return False
        if requested_input:
            self._auto_input_handled = True
        if output_enabled and not self.audio.output_enabled:
            self.announce('Audio input is active. The selected output is unavailable.')
            # The input tools can still measure when the default output fails to open.
            return self.audio.input_enabled
        return True

    def ensure_output(self):
        if self.audio.output_enabled:
            return True
        input_enabled = self.audio.input_enabled
        loopback = self.audio.snapshot().get('loopback')
        try:
            self._start_audio(input_enabled, True)
            if self.audio.output_enabled:
                return True
            raise RuntimeError('No audio output is available.')
        except (OSError, RuntimeError, ValueError) as error:
            if loopback:
                try:
                    self._start_audio(input_enabled, False)
                except (OSError, RuntimeError, ValueError) as recovery:
                    self.show_error('Could not start loopback', recovery)
                    return False
                self.announce('The output is unavailable. The generator is using internal loopback.')
                return True
            if input_enabled and not self.audio.input_enabled:
                try:
                    self._start_audio(True, False)
                except (OSError, RuntimeError, ValueError) as recovery:
                    self.log(f'Restoring audio input: {recovery}')
            self.show_error('Could not start audio output', error)
            return False

    def show_frequency_sweep(self):
        from .sweep_ui import SweepDialog

        if self.workspace.parent is None:
            self.workspace.navigate('generate', sound=False)
        if self._sweep_dialog is None:
            # Keep the editor modeless when switching instruments or layouts.
            self._sweep_dialog = SweepDialog(None, self)
            self._apply_text_scale()
            self._sweep_dialog.CentreOnScreen()
        self.workspace._bring_forward(self._sweep_dialog)
        self._sweep_dialog.refresh_live(self.audio.snapshot())
        return self._sweep_dialog

    def instrument_dialog(self, key, factory, *args):
        if self.workspace.parent is None:
            self.workspace.navigate('measure', sound=False)
        if not self.instrument_dialogs.get(key):
            dialog = factory(None, self, *args)
            self.instrument_dialogs[key] = dialog
            dialog.CentreOnScreen()
            self._apply_text_scale()
        dialog = self.instrument_dialogs[key]
        self.workspace._bring_forward(dialog)
        return dialog

    def forget_instrument(self, dialog):
        for key, instrument in self.instrument_dialogs.items():
            if instrument is dialog:
                del self.instrument_dialogs[key]
                return

    def show_response(self, mode='device'):
        from .response_ui import ResponseDialog

        owner = self._audio_ownership.loopback_owner
        if mode == 'interface' and owner is not None and owner is not self and not owner._closing:
            # Bring the original physical-loopback workflow forward even if
            # the menu command came from another session's window.
            return owner.show_response(mode)
        dialog = self.instrument_dialog('response', ResponseDialog, mode)
        dialog.select_mode(mode)
        return dialog

    def hold_interface_loopback(self):
        if (self.is_measuring or self._bench_capture is not None or self._lab_signal_owner is not None
                or _generated_signal_active(self.audio.snapshot())):
            self.announce('Stop the current test signal before connecting the loopback cable.')
            return False
        # Engage the process and device gates before displaying connection
        # instructions. A failed preferences write leaves both gates held.
        try:
            self._audio_ownership.prepare_loopback(self)
        except ValueError as error:
            self.announce(str(error))
            return False
        return self.apply_settings(interfaceLoopbackPendingDisconnect=True)

    def confirm_interface_disconnected(self):
        if not self._audio_ownership.begin_loopback_confirmation(self):
            return False
        old_persistent = self._persistent_settings
        saved = False
        try:
            saved = self.apply_settings(interfaceLoopbackPendingDisconnect=False)
            if not saved:
                self._persistent_settings = old_persistent
                self.settings = {**self.settings, 'interfaceLoopbackPendingDisconnect': True}
        finally:
            self._audio_ownership.finish_loopback_confirmation(self, saved=saved)
            self._configure_monitor()
        if not saved:
            return False
        if self._initial_setup_pending:
            wx.CallAfter(self.show_setup_wizard, first_launch=True)
        self.release_output()
        return True

    def show_alignment(self, workflow):
        from .alignment_ui import AlignmentDialog

        return self.instrument_dialog(f'alignment-{workflow}', AlignmentDialog, workflow)

    def show_level_meter(self, mode='voltage'):
        from .lab_ui import LevelsDialog

        return self.instrument_dialog(f'levels-{mode}', LevelsDialog, mode)

    def show_bench_analyzer(self):
        from .bench_ui import BenchAnalyzerDialog

        return self.instrument_dialog('bench-analyzer', BenchAnalyzerDialog)

    def show_offline_wav(self, path=None):
        from .lab_ui import OfflineWavDialog

        dialog = self.instrument_dialog('offline-wav', OfflineWavDialog)
        if path is not None:
            dialog.open_file(path)
        return dialog

    def show_raw_speed_capture(self):
        from .lab_ui import RawSpeedDialog

        return self.instrument_dialog('raw-speed', RawSpeedDialog)

    def use_offline_result(self, path, result, settings):
        if self.is_measuring:
            self.announce('Finish the live measurement before importing the readings from a WAV file.')
            return False
        from .sessions import archive_run, clear_run

        options = {key: settings[key] for key in ('keepCompleteHistory', 'keepSignalLossDetails', 'includeInputAudio')}
        keep_previous = self._keep_previous_run(options)
        if self.session['history'] and not keep_previous and not self.confirm(
                'Replace the current wow and flutter history?', 'Import WAV readings'):
            return False
        if keep_previous:
            archive_run(self.session, settings=self.session.get('settings') or self.session_settings())
        else:
            self.results.session_path = None
        clear_run(self.session)
        start = result['start']
        origin = time.time()
        rows = [{**sample, 'timestamp': origin + start + sample['timestamp']} for sample in result['history']]
        self.session['history'] = rows
        spectra = [{**_stored_spectrum(row), 'timestamp': origin + start + row['timestamp']}
                   for row in result.get('spectra', ())]
        if not spectra and rows and (spectrum := _stored_spectrum(result['spectrum']))['spectrum']:
            spectra = [{**spectrum, 'timestamp': rows[-1]['timestamp']}]
        if spectra:
            self.session['spectra'] = spectra
        if (recorded := result.get('inputAudio')) is not None:
            self.session['inputAudio'] = recorded
        if (events := result.get('signalLoss')):
            self.session['signalLoss'] = [{**event, 'start': origin + start + event['start'],
                                          'end': origin + start + event['end']} for event in events]
        from .session_setup import CALIBRATION_KEYS, ROUTE_KEYS

        # The source is a WAV, so the app's current PortAudio devices and
        # physical calibration do not describe where the sound was recorded.
        run_settings = {key: deepcopy(settings[key]) for key in SESSION_SETTING_KEYS
                        if key in settings and key not in ROUTE_KEYS | CALIBRATION_KEYS}
        self.session['runSettings'] = run_settings
        self.session['settings'] = run_settings
        self.session['runRetention'] = options
        self.session['recordedAudio'] = {'file': path.name, 'start': start, 'channel': result['channel'],
                                         'sampleRate': result['rate'], 'correctionFactor': settings['correctionFactor'],
                                         'metrics': result['metrics'],
                                         'settings': {key: settings[key] for key in PROFILE_KEYS if key in settings}}
        self.session_changed()
        self.navigate('results', 'graph')
        self.announce('Imported the test-tone measurements into Results.')
        return True

    def record_bench_result(self, kind, payload):
        self.log_measurement(kind, payload)

    @property
    def studio_playback_busy(self):
        return (self._bench_capture is not None or self._lab_signal_owner is not None
                or self.settings['interfaceLoopbackPendingDisconnect'])

    def start_lab_signal(self, waveform, *, frequency=1000, level=.1, pan=0, owner=None, after_permission=None):
        owner = owner if owner is not None else self.dialog_parent
        if self.settings['interfaceLoopbackPendingDisconnect']:
            self.announce('Confirm that the interface loopback cable is disconnected first.')
            return False
        if self._bench_capture is not None or self._sweep_run_id is not None:
            self.announce('Wait for the current test signal or capture to finish.')
            return False
        if self._lab_signal_owner is not None and self._lab_signal_owner is not owner:
            self.announce('A different instrument is playing a test signal.')
            return False
        if not self.ensure_output() or not self.audio.output_enabled:
            self.announce('Select a physical audio output to play the reference signal.')
            return False
        generator = self.pages['generate']
        generator.generating = False
        generator._update_generator()
        self._lab_signal_owner = owner
        self.audio.set_generator(True, waveform=waveform, frequency=frequency, level=level, pan=pan,
                                 modulation_frequency=0, modulation_depth=0)
        return True

    def stop_lab_signal(self, owner=None):
        if self._lab_signal_owner is None or owner is not None and owner is not self._lab_signal_owner:
            return
        self._lab_signal_owner = None
        self.audio.set_generator(False)
        self.pages['generate']._applied_generator_values = None
        self.pages['generate']._update_generator()
        self.release_output()

    def set_lab_difference(self, owner, enabled):
        if not enabled:
            if self._difference_owner is owner:
                self._difference_owner = None
                self._configure_monitor()
                if not _generated_signal_active(self.audio.snapshot()):
                    self.release_output()
            return True
        if self.settings['channels'] != 2:
            self.announce('Set Input channels to Stereo in Audio I/O before monitoring the difference between the channels.')
            return False
        if self._difference_owner is not None and self._difference_owner is not owner:
            return False
        if self._bench_capture is not None or self.settings['interfaceLoopbackPendingDisconnect']:
            self.announce('Finish the response capture before turning on monitoring.')
            return False
        def resume():
            if owner and owner.IsShownOnScreen():
                self.set_lab_difference(owner, True)

        if not self.ensure_audio(output_enabled=True, after_permission=resume, require_physical=True):
            return False
        if not (self.audio.input_enabled and self.audio.output_enabled):
            return False
        self._difference_owner = owner
        self._configure_monitor()
        return True

    def bench_start_capture(self, duration, *, stimulus=None, stereo=False, on_ready=None, interface_loopback=False,
                            still_wanted=None):
        if self._closing or (still_wanted is not None and not still_wanted()):
            return None
        if self._audio_ownership.loopback_pending and not (
            interface_loopback and self._audio_ownership.owns_pending_loopback(self)
        ):
            raise ValueError('Confirm that the interface loopback cable is disconnected first.')
        if self._bench_capture is not None or self._lab_signal_owner is not None or _generated_signal_active(self.audio.snapshot()):
            raise ValueError('Stop the test signal or finish the current capture before starting a measurement.')
        if stereo and self.settings['channels'] != 2:
            raise ValueError('Set Input channels to Stereo in Audio I/O before measuring both channels.')
        def notify_failure():
            if on_ready is not None and (still_wanted is None or still_wanted()):
                on_ready(None)

        if not self.audio.input_enabled and not self._allow_microphone(
            lambda: self._resume_bench_capture(duration, stimulus, stereo, on_ready, interface_loopback, still_wanted),
            on_denied=notify_failure,
        ):
            return None
        if not self.ensure_audio(output_enabled=stimulus is not None or self.audio.output_enabled):
            notify_failure()
            return None
        options = {'interface_loopback': True} if interface_loopback else {}
        self._audio_ownership.stop_sounds()
        try:
            capture = self.audio.start_bench_capture(duration, stimulus, **options)
        except (OSError, RuntimeError, ValueError):
            self.release_output()
            raise
        self._bench_capture = capture
        return capture

    def _resume_bench_capture(self, duration, stimulus, stereo, on_ready, interface_loopback, still_wanted):
        try:
            capture = self.bench_start_capture(duration, stimulus=stimulus, stereo=stereo, on_ready=on_ready,
                                               interface_loopback=interface_loopback, still_wanted=still_wanted)
        except (OSError, RuntimeError, ValueError) as error:
            if on_ready is not None:
                on_ready(None)
            self.show_error('Could not start response measurement', error)
        else:
            if capture is not None and on_ready is not None:
                on_ready(capture)

    def bench_play_reference(self, stimulus, on_ready=None, *, still_wanted=None):
        return self.bench_start_capture(stimulus.total_seconds + 1.1, stimulus=stimulus, on_ready=on_ready,
                                        still_wanted=still_wanted)

    def bench_finish_capture(self, capture):
        self._end_bench_capture(capture, cancel=False)

    def bench_cancel_capture(self, capture):
        self._end_bench_capture(capture, cancel=True)

    def _end_bench_capture(self, capture, *, cancel):
        self.audio.finish_bench_capture(capture, cancel=cancel)
        if self._bench_capture is capture:
            self._bench_capture = None
            self.release_output()

    def start_sweep(self, settings):
        if self.studio_playback_busy:
            self.announce('Finish the current test signal before starting a sweep.')
            return False
        previous = self.audio.snapshot()
        if previous.get('sweep', {}).get('active'):
            return False  # Wait for the active sweep or its stop tail to finish.
        try:
            settings.validate()
        except ValueError as error:
            self.show_error('Could not play sweep', error)
            return False
        if not self.ensure_output():
            return False
        try:
            self._sweep_run_id = self.audio.start_sweep(settings)
        except (OSError, RuntimeError, ValueError) as error:
            if not _generated_signal_active(previous):
                self.release_output()
            self.show_error('Could not play sweep', error)
            return False
        generator = self.pages['generate']
        generator.generating = False  # Sweep and fixed tone share one transport.
        generator._update_generator()
        generator.refresh_measurement(self.audio.snapshot())
        self.play_sound('start')
        self.announce('Frequency sweep started.')
        return True

    def stop_sweep(self):
        return self.audio.stop_sweep()

    def _refresh_sweep(self, snapshot):
        if self._sweep_dialog is not None and self._sweep_dialog.IsShownOnScreen():
            self._sweep_dialog.refresh_live(snapshot)
        sweep = snapshot.get('sweep', {})
        if self._sweep_run_id is None or sweep.get('active'):
            return
        status = sweep.get('status') if sweep.get('id') == self._sweep_run_id else 'interrupted'
        self._sweep_run_id = None
        if not snapshot.get('generator', {}).get('enabled'):
            self.release_output()
        if status == 'stopped':
            self.play_sound('stop')
        self.announce({'completed': 'Frequency sweep complete.', 'stopped': 'Frequency sweep stopped.'}
                      .get(status, 'Frequency sweep interrupted.'))

    def release_output(self):
        if (self._closing or self.settings['monitoring'] or self._difference_owner is not None or self.studio_playback_busy
                or _generated_signal_active(self.audio.snapshot())):
            return
        if self.audio.input_enabled and self.audio.output_enabled:
            self.ensure_audio(input_enabled=True, output_enabled=False)
        elif not self.audio.input_enabled and self.audio.running:
            self.audio.stop()

    def stop_input(self):
        self._auto_input_handled = True  # Changing tools must respect an explicit stop.
        self._microphone_resume = None
        denied, self._microphone_denied = self._microphone_denied, None
        if denied is not None:
            denied()
        snapshot = self.audio.snapshot()
        loopback = _loopback_signal_active(snapshot)
        if self.is_measuring and not loopback:
            self.stop_measurement()
        if _generated_signal_active(snapshot) and (self.audio.output_enabled or loopback):
            if self.audio.input_enabled:
                try:
                    self.audio.stop_capture()
                except (OSError, RuntimeError) as error:
                    self.show_error('Could not stop live input', error)
                    return
        else:
            self.audio.stop()
        self.announce('Live input is stopped. Internal loopback is active.' if loopback else 'Live input stopped.')

    def start_measurement(self):
        if self.is_measuring:
            return False
        if self._calibrating:
            self.announce('Wait for calibration to finish before starting a measurement.')
            return False
        if self.settings['interfaceLoopbackPendingDisconnect']:
            self.announce('Confirm that the interface loopback cable is disconnected first.')
            return False
        from .sessions import archive_run, clear_run

        options = {key: self.settings[key] for key in ('keepCompleteHistory', 'keepSignalLossDetails', 'includeInputAudio')}
        keep_previous = self._keep_previous_run(options)
        if self.session['history'] and not keep_previous and not self.confirm(
                'Replace the current wow and flutter history?', 'Start measurement'):
            return False
        if not self.ensure_audio(after_permission=self.start_measurement):
            return False
        self.audio.reset()
        self.audio.retain_measurements(bool(options['keepCompleteHistory']))
        # Give Results a new history generation even if its window stays hidden
        # until there are already samples from this run.
        if keep_previous:
            archive_run(self.session, settings=self.session.get('settings') or self.session_settings())
        else:
            self.results.session_path = None
        clear_run(self.session)
        self.session['settings'] = self.session_settings()
        self.session['runSettings'] = deepcopy(self.session['settings'])
        self.session['runRetention'] = options.copy()
        self._run_worst = 0.0
        self._run_valid = False
        self._run_uncertain = False
        self._run_lost_tone = False
        self._run_options = options
        self._run_spectrum_time = -float('inf')
        self._run_signal_loss = None
        self._run_input_capture = None
        if options['includeInputAudio'] and self.audio.input_enabled:
            from .audio.session_capture import SessionAudioCapture

            self._run_input_capture = SessionAudioCapture()
            self.audio.start_session_audio(self._run_input_capture)
        self._run_started = time.monotonic()
        self._run_clock_origin = time.time()
        self._run_stream_offset = 0.0
        self.measurement_elapsed = 0.0
        self._last_sequence = None
        duration = self.settings['duration']
        self._run_target_duration = 0 if duration == 'continuous' else float(duration)
        self._tape_end_detector = (TapeEndDetector(self.settings['tapeSilenceSeconds'], self.settings['tapeSilenceDb'])
                                   if self.settings['tapeAutoStop'] else None)
        self._tape_end_seen = False
        self.is_measuring = True
        self.session_changed()
        self.play_sound('start')
        self.announce('Measurement started.')
        return True

    def _keep_previous_run(self, options):
        return (any(options.values()) or any(self.session.get(key) for key in ('inputAudio', 'signalLoss', 'previousRuns'))
                or bool(self.session.get('runRetention', {}).get('keepCompleteHistory')))

    def prepare_session_save(self):
        if self._run_input_capture is not None and self.is_measuring:
            self.stop_measurement()

    def stop_measurement(self):
        if not self.is_measuring:
            return False
        self._record(self.audio.snapshot())
        self.measurement_elapsed = time.monotonic() - self._run_started
        self.is_measuring = False
        self.audio.retain_measurements(False)
        audio_note = ''
        if self._run_input_capture is not None:
            capture = self._run_input_capture
            self._run_input_capture = None
            if (recorded := self.audio.finish_session_audio(capture)) is not None:
                self.session['inputAudio'] = recorded
                if recorded.incomplete_reason:
                    audio_note = f' Input audio is incomplete. {recorded.incomplete_reason}'
        self.play_sound('stop')
        self.measurement_elapsed = time.monotonic() - self._run_started
        self.session_changed()
        if self.settings['enableQC']:
            limit = self.settings['qcLimit']
            uninterrupted = not (self._run_uncertain or (self._run_lost_tone and not self._tape_end_seen))
            if self._run_worst > limit or (self._run_valid and uninterrupted):
                outcome = 'failed' if self._run_worst > limit else 'passed'
                self.announce(f'Measurement stopped. The run {outcome} the quality control check. '
                              f'The peak was {self._run_worst:.3f} percent. The limit was {limit:.3f} percent.{audio_note}')
            else:
                self.announce(f'Measurement stopped. The quality control result is inconclusive. '
                              f'Quality control requires a sustained reference tone without clipping and a complete set of readings.{audio_note}')
        else:
            self.announce(f'Measurement stopped.{audio_note}')
        return True

    def toggle_measurement(self):
        if self.is_measuring:
            return self.stop_measurement()
        self.navigate('measure')
        return self.start_measurement()

    def _record(self, snapshot):
        drain = getattr(self.audio, 'drain_measurements', None)
        pending = list(drain()) if callable(drain) else []
        if not self.is_measuring:
            return
        latest = snapshot.get('metrics', {})
        if not pending and latest:
            pending = [latest]
        history = self.session['history']
        previous_sequence = self._last_sequence
        health = snapshot.get('health', {})
        spectrum_time = None
        for metrics in pending:
            sequence = metrics.get('sequence')
            if not sequence or (self._last_sequence is not None and sequence <= self._last_sequence):
                continue
            timestamp = self._run_clock_origin + self._run_stream_offset + max(0.0, metrics.get('timestamp', 0.0))
            if history and timestamp <= history[-1]['timestamp']:
                timestamp = history[-1]['timestamp'] + 0.000001
            if self._last_sequence is not None and sequence != self._last_sequence + 1 and history:
                # The queue can overrun during a blocked UI refresh. Preserve a
                # visible discontinuity even when detailed logs are disabled.
                self._run_uncertain = True
                self._run_gap('Some measurement readings were lost during analysis.', timestamp)
            self._last_sequence = sequence
            history.append({**metrics, 'timestamp': timestamp})
            if sequence == latest.get('sequence') and latest.get('locked') and not latest.get('invalidFrames'):
                # Capture can publish another observation between the snapshot
                # and queue drain. The spectrum belongs to the snapshot's block.
                spectrum_time = timestamp
            if self._run_options.get('keepSignalLossDetails'):
                if not metrics.get('locked') or metrics.get('invalidFrames'):
                    reason = (health.get('inputWarning') or metrics.get('gapReason') or
                              (CLOCK_WARNING if metrics.get('clockUnsupported') else
                               'Input clipped.' if metrics.get('clipped') else
                               'The reference tone was ambiguous.' if metrics.get('referenceAmbiguous') else
                               'The audio input was interrupted.' if metrics.get('invalidFrames') else 'The reference tone was unavailable.'))
                    if self._run_signal_loss is None or self._run_signal_loss['reason'] != reason:
                        # Detector timestamps mark the end of an observation.
                        # Include its block in the logged interval, even for
                        # a single missed observation between good blocks.
                        rate = metrics.get('analysisRate', 0)
                        start = max(self._run_clock_origin + self._run_stream_offset,
                                    timestamp - (1 / rate if rate > 0 else 0))
                        self._run_signal_loss = {'start': start, 'end': timestamp, 'reason': reason}
                        self.session.setdefault('signalLoss', []).append(self._run_signal_loss)
                    self._run_signal_loss['end'] = timestamp
                else:
                    self._run_signal_loss = None
            if self._tape_end_detector is not None:
                self._tape_end_seen |= self._tape_end_detector.observe(
                    timestamp, metrics.get('locked'), metrics.get('inputLevel', 0))
            if metrics.get('clipped') or metrics.get('referenceAmbiguous'):
                self._run_uncertain = True
            if not metrics.get('locked') or metrics.get('invalidFrames'):
                self._run_lost_tone |= self._run_valid
            else:
                if self._run_lost_tone:
                    self._run_uncertain = True
                    self._run_lost_tone = False
                field = ('scrapePeak' if metrics.get('flutterBand', self.settings['flutterBand']) == 'scrape' else
                         'aes6Peak' if metrics.get('weighting', self.settings['weightingStandard']) == 'aes6' else 'peak')
                if (peak := metrics.get(field)) is not None and metrics.get('rmsSeconds', 0) >= 1.0:
                    self._run_valid = True
                    self._run_worst = max(self._run_worst, peak)
        if (spectrum_time is not None and spectrum_time - self._run_spectrum_time >= 0.5
                and (spectrum := _stored_spectrum(snapshot.get('flutter_spectrum', {})))['spectrum']):
            self._run_spectrum_time = spectrum_time
            spectra = self.session.setdefault('spectra', [])
            spectra.append({'timestamp': self._run_spectrum_time, **spectrum})
            if not self._run_options.get('keepCompleteHistory'):
                del spectra[:-1200]
        if not self._run_options.get('keepCompleteHistory'):
            del history[:-self.settings['maxSessionSamples']]
        if self._last_sequence != previous_sequence:
            self.session_changed()

    def _run_gap(self, reason, before=None):
        history = self.session['history']
        if not history:
            return
        last = history[-1]['timestamp']
        end = before if before is not None and before > last else last + 0.000002
        at = (last + end) / 2
        history.append({'timestamp': at, 'locked': False, 'gapReason': reason})
        if self._run_options.get('keepSignalLossDetails'):
            self.session.setdefault('signalLoss', []).append({'start': last, 'end': end, 'reason': reason})
            self._run_signal_loss = None
        self.session_changed()

    def _tick(self, event):
        snapshot = self.audio.snapshot()
        self._record(snapshot)
        self._remember_auto_target(snapshot.get('metrics', {}))
        now = time.monotonic()
        if self.is_measuring:
            self.measurement_elapsed = now - self._run_started
            health = snapshot.get('health', {})
            loopback = _loopback_signal_active(snapshot)
            if not (health.get('inputActive') or (health.get('running') and loopback)):
                self.stop_measurement()
                self.announce('Measurement ended because the audio input was lost.')
            elif self._run_target_duration and self.measurement_elapsed >= self._run_target_duration:
                self.stop_measurement()
            elif self._tape_end_seen:
                self.stop_measurement()
                self.announce('Measurement ended after the tape’s test signal stopped.', speak=True)
        # The turntable and distortion tools may still be recording when the user
        # navigates away. Their own refresh methods suppress hidden plot painting.
        for key in LIVE_PAGES:
            self.pages[key].refresh_measurement(snapshot)
        self._show_audio_status(snapshot)
        self._refresh_sweep(snapshot)
        settings_display = self._settings_display()
        if settings_display.IsShownOnScreen() and now - self._last_debug_tick >= 0.25:
            self._last_debug_tick = now
            settings_display.refresh_live(snapshot)
        if self.results.IsShownOnScreen():
            refresh_live = getattr(self.results, 'refresh_live', None)
            if callable(refresh_live):
                refresh_live(snapshot)
            # Rebuild history views at a lower cadence than capture, and flush
            # the final readings as soon as the recording stops.
            if self._results_need_refresh and (not self.is_measuring or now - self._last_results_tick >= 0.25):
                self._results_need_refresh = False
                self._last_results_tick = now
                self.results.refresh_results()
        self.workspace.update_commands()

    def _remember_auto_target(self, metrics):
        configured = self.settings['targetFreq']
        detected = metrics.get('activeTarget')
        # Follow a deliberate analyzer switch between the two standard tones.
        # Ignore stale snapshots from before a manual edit or a device reopen.
        if (self.settings['useAutoTarget'] and self.settings['weightingStandard'] != 'aes6'
                and self.settings['flutterBand'] == 'rotational' and metrics.get('locked')
                and metrics.get('requestedTarget') == configured
                and configured in (3000, 3150) and detected in (3000, 3150) and detected != configured):
            if self._session_overrides:
                # Automatic detection inside a temporary setup belongs to
                # that session too, even if the target was not an original
                # override. Explicit edits still use the normal preferences.
                overrides = {**self._session_overrides, ('targetFreq',): detected}
                changed = self._change_settings(self._persistent_settings, overrides)
            else:
                changed = self.apply_settings(targetFreq=detected)
            if changed:
                self.announce(f'The reference frequency changed to {detected:g} Hz.')

    def _show_audio_status(self, snapshot):
        health = snapshot.get('health', {})
        metrics = snapshot.get('metrics', {})
        loopback = _loopback_signal_active(snapshot)
        status_key = (health.get('running'), health.get('inputActive'), health.get('error'), health.get('outputWarning'),
                      metrics.get('locked'), loopback, health.get('inputWarning'), metrics.get('clockUnsupported'))
        if status_key == self._last_health:
            return
        old = self._last_health
        self._last_health = status_key
        if health.get('error'):
            label = 'Audio device error.'
            self.log(health['error'])
            self.announce(health['error'])
        elif not health.get('running'):
            label = 'Live audio is stopped.'
        elif health.get('inputWarning'):
            label = 'The audio input was disconnected.'
            self.announce(health['inputWarning'])
        elif health.get('inputActive') and metrics.get('clockUnsupported'):
            label = 'Check clock calibration.'
            self.log(CLOCK_WARNING)
            self.announce(CLOCK_WARNING)
        elif health.get('inputActive') or loopback:
            label = 'Signal locked.' if status_key[4] else 'Waiting for a reference tone.'
        else:
            label = 'Audio output active.'
        if health.get('outputWarning') and (old is None or old[3] != status_key[3]):
            self.announce(health['outputWarning'])
        if old is not None and old[0] and old[4] and not status_key[4] and self.is_measuring:
            self.announce('The reference signal was lost.')
        self.workspace.sync_audio_status(label.strip('.'))

    def _session_has_data(self):
        return (any(self.session.get(key) for key in ('history', 'measurementLog', 'previousRuns', 'inputAudio', 'signalLoss'))
                or any(self.session.get('metadata', {}).values()))

    def session_changed(self):
        self._session_dirty = True
        self._results_need_refresh = True

    def session_saved(self):
        self._session_dirty = False

    def _finalize_session_measurements(self):
        if self.is_measuring:
            self.stop_measurement()
        distortion = self.pages['thd']
        if distortion.running:
            distortion.refresh_measurement(self.audio.snapshot())
            distortion._toggle()
        self.pages['turntable'].finish_session_readings()

    def confirm_replace_session(self, *, closing=False):
        turntable = self.pages['turntable']
        active_measurement = self.is_measuring or self.pages['thd'].running or turntable.is_capturing
        if active_measurement and not closing:
            self.announce('Stop the current measurement before changing this session.')
            return False
        if not closing and not self._can_restore_working_setup():
            return False
        unsaved_readings = turntable.has_pending_readings or (self._session_dirty and self._session_has_data())
        if not (active_measurement or unsaved_readings):
            return True
        prompt = 'Save the changes to this session before continuing?'
        if active_measurement:
            prompt = 'Closing this session will stop its measurements. Save the session before closing?'
        with wx.MessageDialog(self.dialog_parent, prompt, 'Unsaved session',
                              wx.YES_NO | wx.CANCEL | wx.ICON_QUESTION) as dialog:
            dialog.SetYesNoCancelLabels('Save', 'Discard Changes', 'Cancel')
            answer = dialog.ShowModal()
        if answer == wx.ID_YES:
            return self.results.save_session(before_write=self._finalize_session_measurements)
        return answer == wx.ID_NO

    def _can_restore_working_setup(self):
        from .session_setup import ROUTE_KEYS

        if ((self._calibrating or self.settings['interfaceLoopbackPendingDisconnect'])
                and any(self.settings[key] != self._persistent_settings[key] for key in ROUTE_KEYS)):
            self.announce('Finish calibration before changing sessions. This session is using a temporary audio route.')
            return False
        return True

    def session_loaded(self, session=None):
        if not self._can_restore_working_setup():
            return False
        turntable = self.pages['turntable']
        if turntable.is_capturing or self.pages['thd'].running:
            self.announce('Stop the current measurement before changing this session.')
            return False
        if self.is_measuring:
            self.stop_measurement()
        if self._session_overrides and not self._change_settings(self._persistent_settings, {}):
            # A device used before the session might have been unplugged since
            # then. Stop the old stream and restore the preferences for reopening.
            self.audio.stop()
            if not self._change_settings(self._persistent_settings, {}):
                return False
        turntable.reset_session_readings()
        self.session = session if session is not None else new_session(self._persistent_settings['technician'])
        if self.session.get('settings'):
            self.review_session_setup(self.session['settings'], announce_match=False)
        self._session_dirty = False
        self._results_need_refresh = True
        self.results.refresh_results()
        return True

    def review_session_setup(self, saved, *, announce_match=True):
        if self.is_measuring or self._calibrating or self.settings['interfaceLoopbackPendingDisconnect']:
            self.announce('Finish the measurement or calibration before changing the working setup.')
            return False
        from .session_setup import (
            CALIBRATION_KEYS,
            INPUT_ROUTE_KEYS,
            RESET,
            ROUTE_KEYS,
            SessionSetupDialog,
            SettingDifference,
            at_path,
            recorded_setup,
            selected_setup,
            setting_differences,
            with_values,
        )

        current = {**self._session_route_settings(), 'technician': self.settings['technician']}
        saved = recorded_setup(saved)
        recognised = {key: value for key, value in saved.items() if key in (*SESSION_SETTING_KEYS, 'technician')}
        desired = normalize_settings({**current, **recognised})
        def recorded_routes(required):
            # A system default or auto rate in an old record describes an
            # instruction, not the physical device that supplied calibration.
            return all(key in saved and saved[key] and saved[key] == desired[key] for key in required)

        recorded_route = recorded_routes(ROUTE_KEYS)
        differences = tuple(setting_differences(saved, current))
        if not differences:
            if announce_match:
                self.announce('The saved settings match the working setup.')
            return False
        audio_keys = {path[0] for path, _ in differences} & (ROUTE_KEYS | CALIBRATION_KEYS)

        def affected_routes(keys):
            if 'responseCalibrations' in keys:
                return ROUTE_KEYS
            if keys & CALIBRATION_KEYS:
                return INPUT_ROUTE_KEYS
            return frozenset()

        try:
            devices = self.audio.devices() if audio_keys else ()
        except (OSError, RuntimeError):
            devices = ()

        def route_directions(keys):
            directions = []
            if keys & INPUT_ROUTE_KEYS or 'responseCalibrations' in keys:
                directions.append('input')
            if keys & {'outputDevice', 'responseCalibrations'} or ('sampleRate' in keys and (desired['outputDevice'] or self.audio.output_enabled)):
                directions.append('output')
            return directions

        def validate(selection):
            candidate, issue = selected_setup(current, selection)
            if issue:
                return issue
            changed = {path[0] for path in selection}
            required = affected_routes(changed)
            if any(candidate[key] != desired[key] for key in required):
                return 'Select the matching audio route before applying a saved calibration.'
            if errors := self._setup_route_errors(candidate, devices, route_directions(changed | required)):
                return '\n'.join(dict.fromkeys(errors.values()))
            return ''

        rows = []
        for path, value in differences:
            key = path[0]
            warning = ''
            if key not in recognised or (value is not RESET and at_path(desired, path, RESET) != value):
                warning = 'This setting is unsupported or the recorded value is invalid.'
            elif key in CALIBRATION_KEYS:
                required = affected_routes({key})
                if not recorded_routes(required):
                    warning = 'The session did not record the interface route for this calibration.'
                elif self._setup_route_errors(desired, devices, route_directions(required | {key})):
                    warning = 'The original audio route is unavailable.'
            elif key in ROUTE_KEYS:
                individual = normalize_settings(with_values(current, {path: value}))
                directions = route_directions({key})
                # A device and its sample format may only be compatible when
                # selected together. Keep the row selectable in that case.
                combined = self._setup_route_errors(desired, devices, directions)
                separate = self._setup_route_errors(individual, devices, directions)
                if combined and separate:
                    warning = combined.get(key) or separate.get(key) or ''
            rows.append(SettingDifference(path, at_path(current, path), value, warning))

        with SessionSetupDialog(self.dialog_parent, rows, validate, recorded_route=recorded_route) as dialog:
            if dialog.ShowModal() != wx.ID_OK or not dialog.selection:
                return False
            before, previous = self._persistent_settings, self._session_overrides
            active, issue = selected_setup(self.settings, dialog.selection)
            if issue:
                self.announce(issue)
                return False
            if dialog.permanent:
                settings, issue = selected_setup(before, dialog.selection)
                if issue:
                    self.announce(issue)
                    return False
                overrides = dict(setting_differences(active, settings, replace=True))
                if not self._change_settings(settings, overrides, save=True, require_route=True):
                    self._change_settings(before, previous)
                    return False
                self.announce('Fluttery saved the selected session settings as preferences.')
            else:
                overrides = dict(setting_differences(active, before, replace=True))
                if not self._change_settings(before, overrides, require_route=True):
                    return False
                self.announce('Fluttery applied the selected settings for this session.')
        return True

    def _setup_route_errors(self, candidate, devices, directions):
        errors = {}
        routes = {}
        for direction in directions:
            key = f'{direction}Device'
            available = {value: index for value, _, index in device_options(devices, direction)}
            selected = candidate[key]
            if selected and selected not in available:
                errors[key] = f'The saved audio {direction} device is unavailable.'
            elif available:
                routes[direction] = available.get(selected)
            else:
                errors[key] = f'No audio {direction} is available.'
        for direction, index in routes.items():
            try:
                self.audio.check_route(direction, index, candidate['sampleRate'], candidate['channels'])
            except (RuntimeError, ValueError, OSError) as error:
                errors['sampleRate'] = str(error)
                errors[f'{direction}Device'] = str(error)
                if direction == 'input':
                    errors['channels'] = str(error)
        return errors

    def _session_route_settings(self):
        values = dict(self.settings)
        for direction in ('input', 'output'):
            if getattr(self.audio, f'{direction}_enabled'):
                identity = getattr(self, f'_{direction}_identity')
                if identity and not identity.startswith('<default '):
                    values[f'{direction}Device'] = identity
        if not values['sampleRate'] and self.audio.running:
            health = self.audio.snapshot().get('health', {})
            if rate := health.get('inputSampleRate') or health.get('outputSampleRate'):
                values['sampleRate'] = round(rate)
        return values

    def session_settings(self):
        active = self._session_route_settings()
        settings = {key: deepcopy(active[key]) for key in SESSION_SETTING_KEYS}
        settings['technician'] = self.session['metadata'].get('technician', '')
        return settings

    def clear_session(self):
        if not self.confirm('Clear the measurement history, other recorded results and session details?', 'Clear session'):
            return False
        if not self.session_loaded():
            return False
        self.results.session_path = None
        self.results.clear_comparison(announce=False)
        self.session_changed()
        self.results.refresh_results()
        self.workspace.update_commands()
        self.announce('Session cleared.')
        return True

    def clear_history(self):
        history = self.session['history']
        if not history or not self.confirm('Clear the current wow and flutter history and any audio captured during that run?',
                                           'Clear history'):
            return False
        if self.is_measuring:
            self.stop_measurement()
        from .sessions import clear_run

        clear_run(self.session)
        self.session_changed()
        self.results.refresh_results()
        self.announce('Wow and flutter history cleared.')
        return True

    def log_measurement(self, kind, data, stats=None):
        entries = self.session['measurementLog']
        entries.insert(0, {'timestamp': time.time(), 'type': kind, 'data': data, 'stats': stats})
        del entries[self.settings['maxSessionSamples']:]
        self.session_changed()

    def clear_measurements(self, kind):
        entries = self.session['measurementLog']
        entries[:] = [entry for entry in entries if entry.get('type') != kind]
        self.session_changed()

    def calibrate(self, kind, reference, duration, callback, *, after_permission=None):
        if self._calibrating:
            return False
        if self.is_measuring:
            self.announce('Stop the measurement before changing calibration.')
            return False
        if self.settings['interfaceLoopbackPendingDisconnect']:
            self.announce('Confirm that the interface loopback cable is disconnected first.')
            return False
        if self.audio.snapshot().get('loopback'):
            self.announce('Turn off internal loopback and use an independent reference tone to calibrate.')
            return False
        if kind == 'channel' and self.settings['channels'] != 2:
            self.announce('Select a stereo input before calibrating the channels.')
            return False
        if not self.ensure_audio(after_permission=after_permission):
            return False
        self._calibrating = True

        def run():
            method = self.audio.calibrate_clock if kind == 'clock' else self.audio.calibrate_channels
            try:
                result = method(reference, duration=duration)
            except (OSError, RuntimeError, ValueError) as error:
                wx.CallAfter(self._calibration_finished, kind, None, str(error), callback)
            else:
                wx.CallAfter(self._calibration_finished, kind, result, '', callback)

        Thread(target=run, name='Fluttery calibration', daemon=True).start()
        return True

    def _calibration_finished(self, kind, result, error, callback):
        if self._closing:
            return
        self._calibrating = False
        if error:
            message = error
        elif kind == 'clock' and self.apply_settings(correctionFactor=result):
            message = f'Clock calibrated. Correction factor {result:.7f}.'
        elif kind == 'channel' and self.apply_settings(channelCalibration=result):
            message = f"Channels calibrated. Left trim {result['leftTrimDb']:+.2f} dB. Right trim {result['rightTrimDb']:+.2f} dB."
        else:
            message = 'The calibration result could not be saved.'
        callback(message)
        self.announce(message)

    def read_current(self):
        page = self.pages[self.current_page]
        if callable(readout := getattr(page, 'read_current', None)):
            readout()
            return
        snapshot = self.audio.snapshot()
        metrics = snapshot.get('metrics', {})
        frequency = metrics.get('currentFreq', 0)
        if metrics.get('locked'):
            text = f"{frequency:.2f} Hz. Peak {metrics['peak']:.3f} percent. RMS {metrics['rms']:.3f} percent."
        elif frequency > 0:
            text = f'{frequency:.2f} Hz. The wow and flutter reference is unlocked.'
        else:
            text = 'No frequency detected.'
        self._show_text('Current measurement', text)

    def log(self, text):
        if self.settings['debugLoggingEnabled']:
            self.debug_log.append(f'{time.strftime("%H:%M:%S")}  {text}')

    def debug_text(self):
        return '\n'.join(self.debug_log)

    def export_debug(self):
        if not self.debug_log:
            self.announce('No diagnostic messages to save.', settings_feedback=True)
            return
        with wx.FileDialog(self.dialog_parent, 'Save diagnostic log', defaultFile='fluttery-diagnostics.txt',
                           wildcard='Text files (*.txt)|*.txt', style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            try:
                atomic_write(dialog.GetPath(), (self.debug_text() + '\n').encode('utf-8'))
            except OSError as error:
                self.show_error('Could not save diagnostics', error)
                return
        self.announce('Diagnostic log saved.', settings_feedback=True)

    def copy_text(self, text):
        if not text:
            self.announce('No diagnostic messages to copy.', settings_feedback=True)
            return
        clipboard = wx.TheClipboard
        if not clipboard.Open():
            self.announce('The clipboard is busy. Try copying the log again.', settings_feedback=True)
            return
        try:
            copied = clipboard.SetData(wx.TextDataObject(text))
        finally:
            clipboard.Close()
        self.announce('Copied to clipboard.' if copied else 'Could not copy the diagnostic log to the clipboard.',
                      settings_feedback=True)

    def confirm(self, text, title):
        with wx.MessageDialog(self.dialog_parent, text, title, wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION) as dialog:
            return dialog.ShowModal() == wx.ID_YES

    def show_error(self, title, error):
        self.log(f'{title}: {error}')
        with wx.MessageDialog(self.dialog_parent, str(error), title, wx.OK | wx.ICON_ERROR) as dialog:
            dialog.ShowModal()

    def announce(self, text, *, speak=False, settings_feedback=False):
        self.workspace.set_message(text.replace('\n', ' '))
        if settings_feedback and (self._setup_wizard is not None or self.current_page == 'settings'):
            self._settings_display().show_feedback(text)
        self.log(text)
        if not (speak or self.settings['announceStatus']):
            return
        parent = self.dialog_parent
        try:
            if sys.platform == 'darwin':
                macos.announce(text)
            elif sys.platform == 'win32' and isinstance(parent, wx.Frame) and (status := parent.GetStatusBar()) is not None:
                # Notify accessibility clients that the native status text has changed.
                notify = ctypes.windll.user32.NotifyWinEvent
                notify.argtypes = [ctypes.c_uint, ctypes.c_void_p, ctypes.c_long, ctypes.c_long]
                notify.restype = None
                notify(0x800C, status.GetHandle(), -4, 0)
            else:
                self._notification = wx.adv.NotificationMessage('Fluttery', text, parent=parent)
                self._notification.Show(timeout=3)
        except (OSError, RuntimeError, ValueError) as error:
            self.log(f'Accessibility notification: {error}')

    def _show_text(self, title, text, *, parent=None):
        with wx.Dialog(parent or self.dialog_parent, title=title, style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER) as dialog:
            layout = wx.BoxSizer(wx.VERTICAL)
            description = wx.TextCtrl(dialog, value=text, name=title, style=wx.TE_MULTILINE | wx.TE_READONLY)
            describe(description, 'Read or copy the text in this window.')
            layout.Add(description, 1, wx.EXPAND | wx.ALL, self.FromDIP(10))
            layout.Add(dialog.CreateSeparatedButtonSizer(wx.OK), 0, wx.EXPAND | wx.ALL, self.FromDIP(10))
            dialog.SetSizer(layout)
            dialog.SetSize(self.FromDIP((620, 410)))
            description.SetFocus()
            dialog.ShowModal()

    def _show_shortcuts(self):
        from .manual_content import SHORTCUTS
        self._show_guide(SHORTCUTS)

    def _show_settings_reference(self):
        from .manual_content import SETTINGS
        self._show_guide(SETTINGS)

    def _show_guide(self, topic='README.md'):
        from .manual_ui import ManualDialog
        parent, focused = self.dialog_parent, wx.Window.FindFocus()
        with ManualDialog(parent, topic) as dialog:
            dialog.ShowModal()
        if parent and parent.IsShown():
            parent.Raise()
            if focused and parent.IsDescendant(focused) and focused.IsShownOnScreen():
                focused.SetFocus()

    def open_project_repository(self):
        if not webbrowser.open(updates.REPOSITORY):
            self.show_error('Could not open the project repository', updates.REPOSITORY)

    def open_donation_page(self):
        if not webbrowser.open(DONATION_PAGE):
            self.show_error('Could not open the donation page', DONATION_PAGE)

    def open_feedback(self):
        if not webbrowser.open(FEEDBACK_ADDRESS):
            self.show_error('Could not open the email application', f'Write to {FEEDBACK_ADDRESS.removeprefix("mailto:")}.')

    def _show_about(self):
        info = wx.adv.AboutDialogInfo()
        info.SetName('Fluttery')
        info.SetVersion(__version__)
        info.SetDescription('Desktop audio measurement, signal generation and turntable calibration.')
        info.SetWebSite(updates.REPOSITORY)
        info.SetCopyright("Created by Ashley Cox at The Blind Man's Workshop")
        info.SetIcon(wx.Icon(str(ICON), wx.BITMAP_TYPE_PNG))
        info.AddDeveloper('Ashley Cox')
        wx.adv.AboutBox(info, self.dialog_parent)

    def _start_update_service(self):
        if self._closing or self._initial_setup_pending or self._update_service_started:
            return
        self._update_service_started = True
        try:
            self._sparkle = macos.Sparkle.for_current_bundle()
        except (OSError, RuntimeError, ValueError) as error:
            self.log(f'Sparkle initialization: {error}')
        if self._sparkle is None:
            self._check_background_updates()
        else:
            self._sparkle.set_automatic(self.settings['checkForUpdates'])

    def _check_background_updates(self, event=None):
        if (self.settings['checkForUpdates'] and self._sparkle is None and not self.is_measuring
                and self._setup_wizard is None and not self._initial_setup_pending):
            self.check_updates()

    def check_updates(self, manual=False):
        owner = self._documents.update_owner
        if owner is not None and owner is not self:
            return owner.check_updates(manual)
        if self._sparkle is not None:
            if manual:
                self._sparkle.check()
            return
        if self._update_stage == 'download':
            if manual:
                self.announce('An update is already downloading.', settings_feedback=True)
            return
        # A manual check can reuse a background request and receive its result.
        if manual:
            self._manual_update_check = True
            self.announce('Checking for updates…', settings_feedback=True)
        if self._update_stage == 'check':
            return
        self._update_stage = 'check'

        def check():
            try:
                release = updates.latest_release(__version__)
            except (OSError, ValueError, HTTPException) as error:
                wx.CallAfter(self._show_update, None, str(error))
            else:
                wx.CallAfter(self._show_update, release, '')

        Thread(target=check, name='Fluttery update check', daemon=True).start()

    def _show_update(self, release, error):
        self._update_stage = None
        manual, self._manual_update_check = self._manual_update_check, False
        if self._closing:
            return
        if error:
            self.log(f'Update check: {error}')
            if manual:
                self.announce('Could not check for updates.', settings_feedback=True)
                self.show_error('Could not check for updates', error)
            return
        if release is None:
            if manual:
                self.announce('No update is available.', settings_feedback=True)
                wx.MessageBox('No update is available.', 'Fluttery updates', wx.OK | wx.ICON_INFORMATION, self.dialog_parent)
            return
        if not manual and (not self.settings['checkForUpdates'] or self._seen_update == release.version
                           or self.is_measuring or self._setup_wizard is not None):
            return
        self._seen_update = release.version
        if manual:
            self.announce(f'Fluttery {release.version} is available.', settings_feedback=True)
        appimage = updates.current_appimage()
        if appimage and release.asset:
            if self.confirm(f'Fluttery {release.version} is available. Download and install it now?', 'Fluttery update'):
                self._install_linux_update(release.asset, appimage)
        elif self.confirm(f'Fluttery {release.version} is available. Open the release page?', 'Fluttery update'):
            webbrowser.open(release.page)

    def _install_linux_update(self, asset, target):
        self._update_stage = 'download'
        self.announce('Downloading update…', settings_feedback=True)

        def install():
            try:
                updates.install_appimage(asset, target)
            except (OSError, ValueError, HTTPException) as error:
                wx.CallAfter(self._finish_update, str(error))
            else:
                wx.CallAfter(self._finish_update, '')

        Thread(target=install, name='Fluttery update download', daemon=True).start()

    def _finish_update(self, error):
        self._update_stage = None
        if self._closing:
            return
        if error:
            self.announce('Could not install the update.', settings_feedback=True)
            self.show_error('Could not install the update', error)
        else:
            message = 'The update is installed. Reopen Fluttery to use the new version.'
            self.announce(message, settings_feedback=True)
            wx.MessageBox(message, 'Fluttery update', wx.OK | wx.ICON_INFORMATION, self.dialog_parent)

    def open_path(self, path):
        if self._initial_setup_pending or self._setup_wizard is not None:
            # Finder/Explorer can send an open event while setup owns the window.
            self._setup_open_path = path
            return
        if Path(path).suffix.lower() in ('.wav', '.wave'):
            self.show_offline_wav(path)
            return
        if self.confirm_replace_session():
            try:
                session = load_session(path)
            except (OSError, ValueError, RecursionError) as error:
                self.show_error('Could not open session', error)
            else:
                path = Path(path)
                if self.session_loaded(session):
                    self.results.session_path = path if path.suffix.lower() == '.fluttery' else None
                    self.navigate('results')
                    self.results.refresh_results()

    def quit_application(self, event=None):
        return self._documents.quit(event)

    def quit(self, event=None, *, confirmed=False):
        if self._closing:
            return False
        if not confirmed and (event is None or event.CanVeto()):
            if self.has_other_sessions and self._audio_ownership.owns_pending_loopback(self):
                self.announce('Confirm that the interface loopback cable is disconnected before closing this session.')
                self.show_response('interface')
                if event is not None:
                    event.Veto()
                return False
            if not self.confirm_replace_session(closing=True):
                if event is not None:
                    event.Veto()
                return False
        if self.is_measuring:
            self.stop_measurement()
        self._closing = True
        self.timer.Stop()
        self.update_timer.Stop()
        if self._sweep_dialog is not None:
            self._sweep_dialog.shutdown()
        for dialog in tuple(self.instrument_dialogs.values()):
            if not dialog:
                continue
            if callable(shutdown := getattr(dialog, 'shutdown', None)):
                shutdown()
            dialog.Destroy()
        self.audio.close()
        self.sounds.close()
        self._audio_ownership.unregister(self)
        if self._sweep_dialog is not None:
            self._sweep_dialog.Destroy()
            self._sweep_dialog = None
        wx.EvtHandler.RemoveFilter(self._sound_filter)
        self.keyboard.close()
        self._documents.remove(self)
        self.workspace.destroy_secondary()
        self.Destroy()
        return True


class _SoundFilter(wx.EventFilter):
    TYPES: ClassVar = {wx.EVT_BUTTON.typeId: 'button', wx.EVT_CHECKBOX.typeId: 'switch',
                       wx.EVT_CHOICE.typeId: 'switch', wx.EVT_LISTBOX.typeId: 'switch',
                       wx.EVT_SLIDER.typeId: 'dial', wx.EVT_SPIN.typeId: 'dial',
                       wx.EVT_SPINCTRL.typeId: 'dial', wx.EVT_SPINCTRLDOUBLE.typeId: 'dial'}

    def __init__(self, frame):
        super().__init__()
        self.frame = frame

    def FilterEvent(self, event):
        event_type = event.GetEventType()
        sound = self.TYPES.get(event_type)
        keyboard_focus = event_type == wx.EVT_SET_FOCUS.typeId and self.frame.settings['soundOnFocus']
        if sound or keyboard_focus:
            control = event.GetEventObject()
            if isinstance(control, wx.Control) and self.frame.keyboard._owner(control.GetTopLevelParent()) is not None:
                if sound:
                    self.frame.play_sound(sound)
                elif not isinstance(control, (wx.StaticText, wx.StaticLine)):
                    self.frame.play_sound('focus')
        return wx.EventFilter.Event_Skip


class FlutteryApp(wx.App):
    """Route macOS Finder document opens into the same session loader."""

    def OnInit(self):
        self.SetAppName('Fluttery')
        self.frame = FlutteryFrame()
        self.frame.show_startup(offer_setup=True)
        return True

    def MacReopenApp(self):
        if (frame := getattr(self, 'frame', None)) is not None and not frame._closing:
            frame.workspace.reopen()

    def MacOpenFiles(self, files):
        if files and (frame := getattr(self, 'frame', None)) is not None and not frame._closing:
            wx.CallAfter(frame.open_path, files[0])


def main():
    with ExitStack() as launch:
        if sys.platform == 'win32' and getattr(sys, 'frozen', False):
            from .windows import hold_installation

            try:
                launch.enter_context(hold_installation(Path(sys.executable).parent))
            except OSError as error:
                ctypes.windll.user32.MessageBoxW(None, str(error), 'Fluttery', 0x10)
                return
        _run_desktop()


def _run_desktop():
    if sys.argv[1:] == ['--help-manual']:
        # Start Menu and installer Help can open the offline book by itself.
        from .manual_ui import ManualDialog
        reader = wx.App(False)
        reader.SetAppName('Fluttery')
        with ManualDialog(None) as dialog:
            reader.SetTopWindow(dialog)
            dialog.ShowModal()
        return
    app = FlutteryApp(False)
    if len(sys.argv) > 1 and not sys.argv[1].startswith('-'):
        wx.CallAfter(app.frame.open_path, sys.argv[1])
    app.MainLoop()
