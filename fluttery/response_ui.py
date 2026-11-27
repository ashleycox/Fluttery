"""Frequency-response instrument and guided interface and tape workflows."""

from __future__ import annotations

import csv
from dataclasses import dataclass, replace
from threading import Event, Thread

import numpy as np
import wx
from scipy.io import wavfile
from wx.lib.wordwrap import wordwrap

from .audio.response import (
    LEAD_SECONDS,
    MAX_CAPTURE_SECONDS,
    OCTAVE_RESOLUTIONS,
    InterfaceStimulus,
    PlaybackTailDetector,
    ResponseStimulus,
    calibration_profile,
    compensate_phase,
    compensate_response,
    measure_response,
    phase_group_delay,
    preview_transfer,
    read_wav_samples,
    response_balance,
    response_test_defaults,
    trace_playback,
)
from .charts import LinePlot
from .keyboard_access import describe
from .reporting import _csv_cell
from .staging import atomic_output

HELP_CHAPTER = '10-frequency-response.md'
MODES = (
    ('device', 'Frequency Response', 'measure-a-device'),
    ('interface', 'Interface Response Calibration', 'calibrate-an-interface'),
    ('tape_playback', 'Tape Playback Response', 'measure-tape-playback-response'),
    ('tape_recording', 'Tape Recording Response', 'measure-tape-recording-response'),
)
SOURCES = (('sweep', 'Logarithmic Sweep'), ('white', 'White Noise'), ('pink', 'Pink Noise'))
PLAYBACK_AGGREGATES = (('median', 'Median'), ('mean', 'RMS Average'), ('peak', '95th Percentile'), ('maximum', 'Peak Hold'))
PLOT_CHOICES = (('gain', 'Gain'), ('measured', 'Measured Gain'), ('balance', 'Channel Balance'),
                ('phase', 'Phase'), ('coherence', 'Coherence'), ('measured_phase', 'Measured Phase'),
                ('delay', 'Group Delay'))
CHANNEL_CHOICES = ((None, 'Automatic'), (False, 'Mono'), (True, 'Stereo'))
NO_PROFILE = 'No interface correction'
AUTO_PROFILE = 'Automatic (match audio route)'
KEEP_PROFILE = object()


def _describe(control, label, help_text):
    control.SetName(label)
    if hasattr(control, 'SetAccessibleName'):
        control.SetAccessibleName(label)
    describe(control, help_text)
    return control


def _field(parent, grid, label, control, help_text):
    box = wx.BoxSizer(wx.VERTICAL)
    caption = wx.StaticText(parent, label=label)
    box.Add(caption, 0, wx.BOTTOM, parent.FromDIP(3))
    box.Add(_describe(control, label, help_text), 0, wx.EXPAND)
    grid.Add(box, 1, wx.EXPAND)
    control._response_field = box
    control._response_caption = caption
    return control


def _button(parent, label, callback, help_text, identifier=wx.ID_ANY):
    button = _describe(wx.Button(parent, identifier, label=label), label, help_text)
    button.Bind(wx.EVT_BUTTON, lambda event: callback())
    return button


def _number(field, label):
    try:
        value = float(field.GetValue())
    except (OverflowError, ValueError) as error:
        raise ValueError(f'Enter a number for {label}.') from error
    if not np.isfinite(value):
        raise ValueError(f'Enter a finite number for {label}.')
    return value


def _show_error(parent, text):
    wx.MessageBox(str(text), 'Frequency Response', wx.OK | wx.ICON_ERROR, parent)


def _atomic_save(path, callback, *, binary=False):
    with atomic_output(path, mode='w+b' if binary else 'w+') as stream:
        callback(stream)


def _cell(value):
    return f'{value:.10g}' if np.isfinite(value) else ''


def _baseline_summary(baseline):
    lines = [(f"Loopback baseline, 20–{baseline['highHz']:g} Hz. Tone at {baseline['toneHz']:g} Hz, "
              f"{baseline['driveDbfsPeak']:g} dBFS peak output.")]
    labels = ('Left', 'Right') if len(baseline['noiseDbfs']) == 2 else ('Mono',)
    for index, label in enumerate(labels):
        lines.append(f"{label} noise {baseline['noiseDbfs'][index]:.2f} dBFS RMS. "
                     f"Tone {baseline['toneDbfs'][index]:.2f} dBFS RMS. "
                     f"THD {baseline['thdPercent'][index]:.5g}%. THD+N {baseline['thdnPercent'][index]:.5g}%.")
    return lines


@dataclass(frozen=True, slots=True)
class ResponseOptions:
    resolution: int = 12
    offset_ms: float | None = None
    drift_ppm: float | None = None
    anchor_hz: float = 1000
    playback_aggregate: str = 'median'
    tail_seconds: float = 2
    calibration_offset_db: float = 0
    stereo: bool | None = None
    track_tape: bool = True


@dataclass(frozen=True, slots=True)
class ResponsePass:
    kind: str
    stimulus: ResponseStimulus
    options: ResponseOptions
    action: str = 'measure'


@dataclass(frozen=True, slots=True)
class ResponseRoute:
    input_device: str
    output_device: str
    output_rate: float | None


class ResponseOptionsDialog(wx.Dialog):
    def __init__(self, parent, options, mode, save_defaults, restore_defaults):
        super().__init__(parent, title='Advanced Response Settings', style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.options = options
        self._save_defaults = save_defaults
        self._restore_defaults = restore_defaults
        self.restored = False
        outer = wx.BoxSizer(wx.VERTICAL)
        layout = wx.BoxSizer(wx.VERTICAL)
        grid = wx.FlexGridSizer(cols=2, hgap=self.FromDIP(12), vgap=self.FromDIP(9))
        grid.AddGrowableCol(0, 1)
        grid.AddGrowableCol(1, 1)
        self.resolution = _field(self, grid, 'Points per octave',
                                 wx.Choice(self, choices=[str(value) for value in OCTAVE_RESOLUTIONS]),
                                 'Higher values give finer graph detail at the cost of less smoothing.')
        self.resolution.SetSelection(OCTAVE_RESOLUTIONS.index(options.resolution))
        self.anchor = _field(self, grid, 'Playback reference (Hz)', wx.TextCtrl(self, value=f'{options.anchor_hz:g}'),
                             'Measure the tape’s response relative to a reference tone on the same tape.')
        self.offset = _field(self, grid, 'Start offset (ms)',
                             wx.TextCtrl(self, value='' if options.offset_ms is None else f'{options.offset_ms:g}'),
                             'Leave this blank to let Fluttery find the first marker, or enter the marker’s position in the capture.')
        self.drift = _field(self, grid, 'Capture clock drift (ppm)',
                            wx.TextCtrl(self, value='' if options.drift_ppm is None else f'{options.drift_ppm:g}'),
                            'Leave this blank to measure drift from the final marker. Use a higher value for a stretched recording.')
        self.aggregate = _field(self, grid, 'Tape playback band statistic',
                                wx.Choice(self, choices=[label for _, label in PLAYBACK_AGGREGATES]),
                                'Summarize repeated readings within each frequency band on an external test tape.')
        self.aggregate.SetSelection(next(index for index, value in enumerate(PLAYBACK_AGGREGATES)
                                         if value[0] == options.playback_aggregate))
        self.tail = _field(self, grid, 'Extra capture time (seconds)', wx.TextCtrl(self, value=f'{options.tail_seconds:g}'),
                           'Allow extra time for hardware delay, off-tape monitoring or pressing Play after you start capturing.')
        self.offset_db = _field(self, grid, 'Calibration gain trim (dB)',
                                wx.TextCtrl(self, value=f'{options.calibration_offset_db:g}'),
                                'Adjust the loopback curve by this amount before using it to correct a result.')
        self.channels = _field(self, grid, 'Capture channels',
                               wx.Choice(self, choices=[label for _, label in CHANNEL_CHOICES]),
                               'Use the channels selected in Audio I/O for live tests and the channels in the file for WAVs. Select Mono or Stereo to override these defaults.')
        self.channels.SetSelection(next(index for index, value in enumerate(CHANNEL_CHOICES) if value[0] == options.stereo))
        playback = mode == 'tape_playback'
        for field, show in ((self.anchor, playback), (self.aggregate, playback), (self.offset, not playback),
                            (self.drift, not playback), (self.tail, not playback), (self.offset_db, mode == 'interface')):
            grid.Show(field._response_field, show, recursive=True)
        layout.Add(grid, 0, wx.EXPAND | wx.BOTTOM, self.FromDIP(14))
        self.track = _describe(wx.CheckBox(self, label='Follow speed changes in tape recording sweeps'),
                               'Follow speed changes in tape recording sweeps',
                               'Estimate gain from the frequencies in the recorded signal. Clear this option to use the matched reference and plot phase.')
        self.track.SetValue(options.track_tape)
        layout.Add(self.track, 0, wx.BOTTOM, self.FromDIP(10))
        self.track.Show(mode == 'tape_recording')
        default_actions = wx.BoxSizer(wx.HORIZONTAL)
        save = _button(self, 'Save as Defaults', self._save,
                       'Use the current settings whenever you open this test in a new session.')
        restore = _button(self, 'Restore Factory Defaults', self._restore,
                          'Restore the built-in starting settings for this test.')
        default_actions.Add(save, 0, wx.RIGHT, self.FromDIP(7))
        default_actions.Add(restore, 0)
        layout.Add(default_actions, 0, wx.BOTTOM, self.FromDIP(12))
        actions = wx.StdDialogButtonSizer()
        cancel = _button(self, 'Cancel', lambda: self.EndModal(wx.ID_CANCEL), 'Keep the previous options.', wx.ID_CANCEL)
        apply = _button(self, 'Apply', self._apply, 'Use these response settings for the next run.', wx.ID_OK)
        actions.AddButton(cancel)
        actions.AddButton(apply)
        actions.Realize()
        layout.Add(actions, 0, wx.ALIGN_RIGHT)
        apply.SetDefault()
        outer.Add(layout, 1, wx.EXPAND | wx.ALL, self.FromDIP(14))
        self.SetSizerAndFit(outer)
        self.SetMinSize(self.GetSize())
        self.CentreOnParent()

    def _read_options(self):
        anchor = _number(self.anchor, 'playback reference frequency')
        offset = _number(self.offset, 'start offset') if self.offset.GetValue().strip() else None
        drift = _number(self.drift, 'clock drift') if self.drift.GetValue().strip() else None
        tail = _number(self.tail, 'extra capture time')
        trim = _number(self.offset_db, 'calibration gain trim')
        if not 5 <= anchor <= 90_000 or (offset is not None and not 0 <= offset <= 170_000):
            raise ValueError('Enter a playback reference from 5 to 90,000 Hz and a start offset from 0 to 170,000 ms.')
        if drift is not None and abs(drift) > 100_000:
            raise ValueError('Clock drift must be within ±100,000 ppm.')
        if not 0.5 <= tail <= 40 or not -40 <= trim <= 40:
            raise ValueError('Enter 0.5 to 40 seconds of extra capture time and a gain trim from −40 to +40 dB.')
        return ResponseOptions(OCTAVE_RESOLUTIONS[self.resolution.GetSelection()], offset, drift, anchor,
                               PLAYBACK_AGGREGATES[self.aggregate.GetSelection()][0], tail, trim,
                               CHANNEL_CHOICES[self.channels.GetSelection()][0], self.track.GetValue())

    def _apply(self):
        self._accept(save_defaults=False)

    def _save(self):
        self._accept(save_defaults=True)

    def _accept(self, *, save_defaults):
        try:
            options = self._read_options()
            if save_defaults and not self._save_defaults(options):
                return
        except ValueError as error:
            _show_error(self, error)
        else:
            self.options = options
            self.EndModal(wx.ID_OK)

    def _restore(self):
        if self._restore_defaults():
            self.restored = True
            self.EndModal(wx.ID_OK)


class CalibrationProfilesDialog(wx.Dialog):
    def __init__(self, parent, profiles, selected):
        super().__init__(parent, title='Interface Response Profiles')
        self.profiles = [dict(profile) for profile in profiles]
        self.active_profile = selected
        outer = wx.BoxSizer(wx.VERTICAL)
        layout = wx.BoxSizer(wx.VERTICAL)
        self.list = _field(self, layout, 'Saved interface responses', wx.ListBox(self, size=self.FromDIP((385, 100))),
                           'Select a loopback profile to rename it, change its overall gain or delete it.')
        self.list.Bind(wx.EVT_LISTBOX, lambda event: self._summary())
        self.detail = _describe(wx.TextCtrl(self, style=wx.TE_READONLY | wx.TE_MULTILINE,
                                            size=self.FromDIP((-1, 136))), 'Interface profile details',
                                'Read the saved route, bandwidth, gain trim, timing, noise and distortion readings.')
        layout.Add(self.detail, 0, wx.EXPAND | wx.TOP, self.FromDIP(8))
        edits = wx.BoxSizer(wx.HORIZONTAL)
        self.edit_button = _button(self, 'Edit…', self._edit, 'Change the name and gain trim of the selected profile.')
        self.delete_button = _button(self, 'Delete…', self._delete, 'Delete the selected profile after confirmation.')
        for button in (self.edit_button, self.delete_button):
            edits.Add(button, 0, wx.RIGHT, self.FromDIP(7))
        layout.Add(edits, 0, wx.TOP, self.FromDIP(7))
        actions = wx.StdDialogButtonSizer()
        cancel = _button(self, 'Cancel', lambda: self.EndModal(wx.ID_CANCEL), 'Discard profile edits.', wx.ID_CANCEL)
        apply = _button(self, 'Apply Changes', lambda: self.EndModal(wx.ID_OK), 'Save profile edits.', wx.ID_OK)
        actions.AddButton(cancel)
        actions.AddButton(apply)
        actions.Realize()
        layout.Add(actions, 0, wx.ALIGN_RIGHT | wx.TOP, self.FromDIP(11))
        outer.Add(layout, 1, wx.EXPAND | wx.ALL, self.FromDIP(14))
        self.SetSizerAndFit(outer)
        selected = next((index for index, profile in enumerate(self.profiles) if profile['name'] == selected), 0)
        self._reload(selected)
        self.CentreOnParent()

    def _reload(self, selected):
        self.list.Set([profile['name'] for profile in self.profiles])
        if self.profiles:
            self.list.SetSelection(min(selected, len(self.profiles) - 1))
        self._summary()

    def _summary(self):
        index = self.list.GetSelection()
        self.edit_button.Enable(index != wx.NOT_FOUND)
        self.delete_button.Enable(index != wx.NOT_FOUND)
        summary = 'No saved interface responses.'
        if index != wx.NOT_FOUND:
            profile = self.profiles[index]
            summary = (f"Input {profile['input'] or 'System default'}. Output {profile['output'] or 'System default'}.\n"
                       f"{profile['sampleRate']:g} Hz sampling, {profile['frequencyHz'][0]:g} to "
                       f"{profile['frequencyHz'][-1]:g} Hz. {profile['offsetDb']:+g} dB trim.")
            if profile.get('outputRate') and profile['outputRate'] != profile['sampleRate']:
                summary += f" Output {profile['outputRate']:g} Hz."
            if 'bandsHz' in profile:
                summary += f" {len(profile['bandsHz'])} separate frequency bands."
            if 'markerOffsetMs' in profile:
                summary += f"\nCapture marker at {profile['markerOffsetMs']:.2f} ms."
            if 'clockDriftPpm' in profile:
                summary += f" Relative clock drift {profile['clockDriftPpm']:+.1f} ppm."
            if 'baseline' in profile:
                summary += '\n' + '\n'.join(_baseline_summary(profile['baseline']))
        self.detail.ChangeValue(summary)

    def _edit(self):
        index = self.list.GetSelection()
        profile = self.profiles[index]
        with wx.TextEntryDialog(self, 'Profile name', 'Rename Interface Profile', profile['name']) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            name = dialog.GetValue().strip()
        if not 1 <= len(name) <= 80 or any(i != index and existing['name'].casefold() == name.casefold()
                                            for i, existing in enumerate(self.profiles)):
            _show_error(self, 'Enter a unique profile name of 1 to 80 characters.')
            return
        with wx.TextEntryDialog(self, 'Add to the compensation curve in dB', 'Interface Calibration Gain',
                                f"{profile['offsetDb']:g}") as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            try:
                offset = float(dialog.GetValue())
                if not -40 <= offset <= 40:
                    raise ValueError
            except (ValueError, OverflowError):
                _show_error(self, 'Enter a gain trim from −40 to +40 dB.')
                return
        self.profiles[index] = {**profile, 'name': name, 'offsetDb': offset}
        if self.active_profile == profile['name']:
            self.active_profile = name
        self._reload(index)

    def _delete(self):
        index = self.list.GetSelection()
        name = self.profiles[index]['name']
        if wx.MessageBox(f'Delete “{name}”?', 'Delete Interface Profile', wx.YES_NO | wx.NO_DEFAULT, self) == wx.YES:
            self.profiles.pop(index)
            if self.active_profile == name:
                self.active_profile = ''
            self._reload(index)


class ResponseDialog(wx.Dialog):
    """A modeless workbench with one capture transport across all four modes."""

    def __init__(self, parent, controller, mode='device'):
        super().__init__(parent, title='Frequency Response', style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.controller = controller
        self.options = ResponseOptions()
        self.mode = None
        self._drafts = {}
        self._request = None
        self._capture = None
        self._capture_route = None
        self._pass = None
        self._tape_tail = None
        self._tape_end_trimmed = False
        self._reference = None
        self._reference_route = None
        self._alive = Event()
        self._alive.set()
        self._busy = False
        self._live_selection = None
        self._live_task = None
        self._live_preview = None
        self._live_correction_status = ''
        self._live_next = 0
        self._interface_ready = False
        self._disconnect_required = controller.settings.get('interfaceLoopbackPendingDisconnect', False)
        self._close_after_disconnect = False
        self._timer = wx.Timer(self)
        self._data = {}
        self._routes = {}
        self._pinned = {}
        self._pin_routes = {}
        self._profile_options = []
        self._activity = ''
        self._build()
        self.select_mode('interface' if self._disconnect_required else mode)
        self.Bind(wx.EVT_TIMER, self._tick, self._timer)
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.Bind(wx.EVT_CHAR_HOOK, self._key)
        self.Bind(wx.EVT_SIZE, self._resize)
        (self.run if self._disconnect_required else self.mode_choice).SetFocus()

    def _build(self):
        outer = wx.BoxSizer(wx.VERTICAL)
        layout = wx.BoxSizer(wx.VERTICAL)
        mode_row = wx.BoxSizer(wx.HORIZONTAL)
        self.mode_choice = _field(self, mode_row, 'Test', wx.Choice(self, choices=[label for _, label, _ in MODES]),
                                  'Choose a device response test, an interface calibration, or a tape playback or recording test.')
        self.mode_choice.Bind(wx.EVT_CHOICE, lambda event: self.select_mode(MODES[self.mode_choice.GetSelection()][0]))
        self.audio_settings = _button(self, 'Audio I/O…', self._audio_settings,
                                      'Open audio settings to choose the interface and sample rate.')
        mode_row.Add(self.audio_settings, 0, wx.ALIGN_BOTTOM | wx.LEFT, self.FromDIP(8))
        layout.Add(mode_row, 0, wx.EXPAND | wx.BOTTOM, self.FromDIP(9))

        guide = wx.StaticBoxSizer(wx.VERTICAL, self, 'Connection and test steps')
        self.guide = _describe(wx.StaticText(guide.GetStaticBox(), style=wx.ST_NO_AUTORESIZE), 'Connection and test steps',
                               'How to connect the equipment and start the selected measurement.')
        guide.Add(self.guide, 1, wx.EXPAND | wx.ALL, self.FromDIP(8))
        layout.Add(guide, 0, wx.EXPAND | wx.BOTTOM, self.FromDIP(8))
        settings = wx.StaticBoxSizer(wx.VERTICAL, self, 'Test settings')
        group = settings.GetStaticBox()
        grid = self._settings_grid = wx.FlexGridSizer(cols=3, hgap=self.FromDIP(9), vgap=self.FromDIP(6))
        for index in range(3):
            grid.AddGrowableCol(index, 1)
        self.duration = _field(group, grid, 'Signal or capture time (s)', wx.TextCtrl(group, value='10'),
                               'Set the duration of the test signal or the length of an external tape capture.')
        self.low = _field(group, grid, 'Lowest frequency (Hz)', wx.TextCtrl(group, value='20'),
                          'Set the lowest frequency to measure.')
        self.high = _field(group, grid, 'Highest frequency (Hz)', wx.TextCtrl(group, value='20000'),
                           'Set the highest frequency to measure. The sample rate must support this frequency.')
        self.stimulus = _field(group, grid, 'Test signal', wx.Choice(group, choices=[label for _, label in SOURCES]),
                               'Choose a logarithmic sine sweep, white noise or pink noise.')
        self.stimulus.SetSelection(0)
        self.level = _field(group, grid, 'Peak output (dBFS)', wx.TextCtrl(group, value='-20'),
                            'Set the peak digital output level. Start at a low level.')
        self.heads = _field(group, grid, 'Tape monitoring',
                            wx.Choice(group, choices=['Three-head deck, off-tape', 'Two-head deck, rewind and replay']),
                            'Choose whether the recorded tone can be played back from tape while it is being recorded.')
        self.heads.SetSelection(0)
        self.heads.Bind(wx.EVT_CHOICE, lambda event: self._update_mode())
        settings.Add(grid, 0, wx.EXPAND | wx.ALL, self.FromDIP(8))
        layout.Add(settings, 0, wx.EXPAND | wx.BOTTOM, self.FromDIP(8))
        self._controls = (self.mode_choice, self.stimulus, self.duration, self.level, self.low, self.high, self.heads)
        options = wx.WrapSizer(wx.HORIZONTAL, wx.REMOVE_LEADING_SPACES)
        self.advanced = _button(self, 'Advanced…', self._advanced, 'Set timing, capture channels and octave resolution manually.')
        self.open_wav = _button(self, 'Open Return WAV…', self._open_wav, 'Analyse a test signal recorded in a WAV file.')
        self.open_reference = _button(self, 'Open Test WAV…', self._open_reference,
                                      'Load a previously saved Fluttery test signal with timing markers.')
        self.stimulus_wav = _button(self, 'Save Test WAV…', self._save_stimulus,
                                    'Save the exact generated stimulus, including its timing markers.')
        for control in (self.advanced, self.open_wav, self.open_reference, self.stimulus_wav):
            options.Add(control, 0, wx.RIGHT | wx.BOTTOM, self.FromDIP(6))
        layout.Add(options, 0, wx.EXPAND)

        correction = wx.WrapSizer(wx.HORIZONTAL, wx.REMOVE_LEADING_SPACES)
        self.calibration = _field(self, correction, 'Interface correction profile', wx.Choice(self),
                                  'Match a loopback profile to the recorded input and output route, or choose one by name.')
        self.calibration.Bind(wx.EVT_CHOICE, lambda event: self._correction_changed())
        self.bypass = _describe(wx.CheckBox(self, label='Bypass Correction'), 'Bypass Correction',
                                'Show and save the raw response while keeping the selected profile available.')
        self.compare_raw = _describe(wx.CheckBox(self, label='Compare with Raw'), 'Compare with Raw',
                                     'Overlay the raw and corrected traces on the same axes.')
        for control in (self.bypass, self.compare_raw):
            control.Bind(wx.EVT_CHECKBOX, lambda event: self._correction_changed())
        self.manage = _button(self, 'Manage Profiles…', self._manage, 'Rename, trim or delete saved interface response profiles.')
        for control in (self.bypass, self.compare_raw, self.manage):
            correction.Add(control, 0, wx.ALIGN_BOTTOM | wx.LEFT | wx.BOTTOM, self.FromDIP(6))
        layout.Add(correction, 0, wx.EXPAND | wx.BOTTOM, self.FromDIP(7))

        result_row = wx.BoxSizer(wx.HORIZONTAL)
        result_row.Add(wx.StaticText(self, label='Plot'), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, self.FromDIP(7))
        self.metric = _describe(wx.Choice(self, choices=[label for _, label in PLOT_CHOICES]), 'Plot quantity',
                                'Choose measured or corrected gain and phase, stereo balance, group delay or noise coherence.')
        self.metric.SetSelection(0)
        self.metric.Bind(wx.EVT_CHOICE, lambda event: self._plot_selection_changed())
        result_row.Add(self.metric, 0, wx.ALIGN_CENTER_VERTICAL)
        result_row.AddStretchSpacer()
        self.pin = _button(self, 'Pin Trace', self._pin, 'Keep the current result on the plot while you measure another setting.')
        self.clear = _button(self, 'Clear Pins', self._clear_pins, 'Remove overlaid comparison traces for this test.')
        for button in (self.pin, self.clear):
            result_row.Add(button, 0, wx.LEFT, self.FromDIP(6))
        layout.Add(result_row, 0, wx.EXPAND | wx.BOTTOM, self.FromDIP(4))
        self.plot = LinePlot(self, title='Frequency response', x_label='Frequency (Hz)', y_label='Gain (dB)',
                             log_x=True, controls=False)
        layout.Add(self.plot, 1, wx.EXPAND)

        self.progress = _describe(wx.Gauge(self, range=1000), 'Response capture progress', 'Progress through the current run.')
        self.progress._fluttery_read_value = lambda: f'{self.progress.GetValue() / 10:g} percent.'
        layout.Add(self.progress, 0, wx.EXPAND | wx.TOP, self.FromDIP(6))
        self.status = _describe(wx.TextCtrl(self, style=wx.TE_READONLY | wx.TE_MULTILINE), 'Response activity',
                                'Read capture progress and results. Text remains stable while this field has focus.')
        self.status.SetMinSize((-1, 3 * self.status.GetCharHeight() + self.FromDIP(10)))
        self.status.Bind(wx.EVT_KILL_FOCUS, self._status_blur)
        layout.Add(self.status, 0, wx.EXPAND | wx.TOP, self.FromDIP(4))
        actions = wx.WrapSizer(wx.HORIZONTAL, wx.REMOVE_LEADING_SPACES)
        self.help = _button(self, 'Help', self._help, 'Read the user guide for this response test.')
        self.close = _button(self, 'Close', self._hide, 'Cancel the current capture and close this tool.', wx.ID_CLOSE)
        self.save_baseline = _button(self, 'Save Calibration…', self._save_calibration, 'Name and save the measured loopback curve.')
        self.export = _button(self, 'Export CSV…', self._export, 'Export data for the current result and any pinned responses.')
        self.capture_return = _button(self, 'Capture Recorded Tape', self._capture_recorded,
                                       'After rewinding, capture the recorded test while the deck plays it.')
        self.stop = _button(self, 'Stop', self._stop_capture, 'End an external capture early, or cancel a test that is playing its own signal.')
        self.run = _button(self, 'Measure Response', self._run, 'Start the current test after checking the connection and output level.')
        for button in (self.help, self.close, self.save_baseline, self.export, self.capture_return, self.stop, self.run):
            actions.Add(button, 0, wx.RIGHT | wx.BOTTOM, self.FromDIP(6))
        layout.Add(actions, 0, wx.EXPAND | wx.TOP, self.FromDIP(7))
        outer.Add(layout, 1, wx.EXPAND | wx.ALL, self.FromDIP(12))
        self.SetSizer(outer)
        gtk = wx.Platform == '__WXGTK__'
        rect = wx.GetClientDisplayRect()
        max_height = rect.height - self.FromDIP(28)
        width, height = self.FromDIP((860, 845 if gtk else 800))
        min_width, min_height = self.FromDIP((690, 665 if gtk else 630))
        self.SetMinSize((min(min_width, rect.width), min(min_height, max_height)))
        self.SetSize((min(width, rect.width), min(height, max_height)))
        self.CentreOnParent()
        self.refresh_display_settings()

    def select_mode(self, mode):
        if (self._capture is not None or self._request is not None or self._busy
                or (self._interface_ready or self._disconnect_required) and mode != 'interface'):
            return False
        mode = mode if any(key == mode for key, _, _ in MODES) else 'device'
        if self.mode == mode:
            return True
        if self.mode is not None:
            self._drafts[self.mode] = self._draft()
        self._request = None
        self.mode = mode
        text, source, heads, metric, calibration, self.options, bypass, compare = (
            self._drafts[mode] if mode in self._drafts else self._starting_draft(mode))
        for field, value in zip(self._draft_fields(), text, strict=True):
            field.ChangeValue(value)
        self.stimulus.SetSelection(source)
        self.heads.SetSelection(heads)
        self.metric.SetSelection(metric)
        self.mode_choice.SetSelection(next(i for i, item in enumerate(MODES) if item[0] == self.mode))
        self.SetTitle(next(label for key, label, _ in MODES if key == self.mode))
        self._reload_profiles(calibration)
        self.bypass.SetValue(bypass)
        self.compare_raw.SetValue(compare)
        self._update_mode()
        self._show_result()
        return True

    def _starting_draft(self, mode, *, factory=False):
        saved = {} if factory else self.controller.settings.get('responseTestDefaults', {})
        setup = saved.get(mode, response_test_defaults(mode))
        text = tuple(f'{setup[key]:g}' for key in ('duration', 'lowHz', 'highHz', 'levelDb'))
        options = ResponseOptions(resolution=setup['resolution'], anchor_hz=setup['anchorHz'],
                                  playback_aggregate=setup['playbackAggregate'], tail_seconds=setup['tailSeconds'],
                                  calibration_offset_db=setup['calibrationTrimDb'],
                                  stereo={'audio': None, 'mono': False, 'stereo': True}[setup['channels']],
                                  track_tape=setup['trackTape'])
        source = next(index for index, (kind, _) in enumerate(SOURCES) if kind == setup['source'])
        return text, source, int(setup['tapeHeads'] == 'two_head'), 0, None, options, False, False

    def _draft_fields(self):
        return self.duration, self.low, self.high, self.level

    def _draft(self):
        return (tuple(field.GetValue() for field in self._draft_fields()),
                self.stimulus.GetSelection(), self.heads.GetSelection(), self.metric.GetSelection(),
                self._profile_name(), self.options, self.bypass.GetValue(), self.compare_raw.GetValue())

    def _update_mode(self):
        duration_label = 'Capture time (s)' if self.mode == 'tape_playback' else 'Signal duration (s)'
        self.duration._response_caption.SetLabel(duration_label)
        self.duration.SetName(duration_label)
        if hasattr(self.duration, 'SetAccessibleName'):
            self.duration.SetAccessibleName(duration_label)
        if self._disconnect_required:
            interface = ('Unplug the loopback cable from the interface input. Then choose Confirm Cable Disconnected '
                         'so Fluttery can restore your monitoring settings.')
            interface_action = 'Confirm Cable Disconnected'
            action_help = 'Confirm that you have unplugged the loopback cable so Fluttery can restore normal monitoring.'
        elif self._interface_ready:
            interface = ('Fluttery has muted input monitoring. Turn off direct monitoring on the interface itself, '
                         'then connect its line output to its line input and choose Measure Loopback.')
            interface_action = 'Measure Loopback'
            action_help = 'Start the test when the physical loopback cable is connected.'
        else:
            interface = ('You can now review the response or save your calibration.'
                         if self._data.get('interface') else
                         'Choose Prepare Loopback before you plug in the cable. Fluttery will mute input monitoring '
                         'and keep it off until you confirm that you have unplugged the cable.')
            interface_action = 'Prepare Loopback'
            action_help = 'Mute software monitoring before connecting the loopback cable.'
        descriptions = {
            'device': ('Connect the interface output to the equipment you’re testing, then connect the equipment’s output '
                       'to the interface input. Set a low output level before you start the test.'),
            'interface': interface,
            'tape_playback': ('Play a swept or stepped alignment tape. Capture the test track, or open a WAV of the track. '
                              'The tape supplies the reference. Interface loopback correction is off for this test.'),
            'tape_recording': ('Select Tape monitoring on the deck. Start recording on blank tape, then measure the returned signal.'
                               if self.heads.GetSelection() == 0 else
                               'Record the test signal on blank tape. Rewind, choose Capture Recorded Tape, then press Play.'),
        }
        self._guide_text = descriptions[self.mode]
        self._wrap_guide()
        for field, show in ((self.stimulus, self.mode != 'tape_playback'), (self.level, self.mode != 'tape_playback'),
                            (self.heads, self.mode == 'tape_recording')):
            self._settings_grid.Show(field._response_field, show, recursive=True)
        self.run.SetLabel({'interface': interface_action,
                           'tape_playback': 'Capture Playback',
                           'tape_recording': 'Record Test Signal' if self.heads.GetSelection() else 'Measure off Tape',
                           'device': 'Measure Response'}[self.mode])
        _describe(self.run, self.run.GetLabel(),
                  action_help if self.mode == 'interface' else 'Start the selected response test.')
        self.save_baseline.Show(self.mode == 'interface')
        self.capture_return.Show(self.mode == 'tape_recording' and self.heads.GetSelection() == 1)
        self.open_wav.Show(self.mode in ('tape_playback', 'tape_recording'))
        self.open_reference.Show(self.mode == 'tape_recording')
        self.stimulus_wav.Show(self.mode != 'tape_playback')
        correctable = self.mode in ('device', 'tape_recording')
        for control in (self.calibration._response_caption, self.calibration, self.bypass, self.compare_raw):
            control.Show(correctable)
        self.manage.Show(self.mode != 'tape_playback')
        self._enable()
        self.Layout()

    def _resize(self, event):
        if event.GetEventObject() is self:
            self._wrap_guide()
            self.Layout()
        event.Skip()

    def _wrap_guide(self):
        width = max(self.FromDIP(240), self.GetClientSize().width - self.FromDIP(54))
        context = wx.ClientDC(self.guide)
        context.SetFont(self.guide.GetFont())
        label = wordwrap(self._guide_text, width, context, breakLongWords=False)
        self.guide.SetLabel(label)
        # GTK can retain the label's single-line best size after wrapping it.
        self.guide.SetMinSize((-1, context.GetMultiLineTextExtent(label).height + self.FromDIP(2)))

    def _enable(self):
        available = self._capture is None and self._request is None and not self._busy
        guarded = self._interface_ready or self._disconnect_required
        for control in (*self._controls, self.advanced, self.open_wav, self.open_reference,
                        self.stimulus_wav, self.calibration, self.manage, self.run, self.metric):
            control.Enable(available)
        self.mode_choice.Enable(available and not guarded)
        self.audio_settings.Enable(available and not guarded)
        if self._live_selection is not None and self._capture is not None:
            self.metric.Enable()
        live_response = self._capture is not None and self._live_selection is not None and self.metric.GetSelection() > 0
        correction_selected = self._profile_name() != '' and bool(self._profile_options)
        self.bypass.Enable(correction_selected and (available or live_response))
        if self._disconnect_required:
            # The cable can be removed as soon as playback stops; the result
            # analysis may finish on the worker while the operator does so.
            self.run.Enable(self._capture is None)
            for control in (self.advanced, self.manage, *self._controls[1:]):
                control.Disable()
        if available and not self._disconnect_required:
            self.stimulus.Enable(self.mode != 'tape_playback')
            self.level.Enable(self.mode != 'tape_playback')
            self.heads.Enable(self.mode == 'tape_recording')
        result = self._data.get(self.mode)
        comparable = False
        if result and available:
            _, curve, rate = result
            metric = PLOT_CHOICES[max(0, self.metric.GetSelection())][0]
            profile, _ = self._comparison_profile(curve, rate, self._routes.get(self.mode))
            comparable = self._metric_profile(curve, profile, metric) is not None
        self.compare_raw.Enable(correction_selected and (comparable or live_response))
        self.pin.Enable(available and result is not None and len(self._pinned.get(self.mode, [])) < 5)
        self.clear.Enable(available and bool(self._pinned.get(self.mode)))
        self.export.Enable(available and result is not None)
        self.save_baseline.Enable(available and not guarded and self.mode == 'interface' and result is not None)
        self.capture_return.Enable(available and self._reference is not None)
        self.stop.Enable(self._capture is not None or self._request is not None)

    def _advanced(self):
        with ResponseOptionsDialog(self, self.options, self.mode, self._save_test_defaults, self._clear_test_defaults) as dialog:
            if dialog.ShowModal() == wx.ID_OK:
                if dialog.restored:
                    text, source, heads, _, _, self.options, _, _ = self._starting_draft(self.mode, factory=True)
                    for field, value in zip(self._draft_fields(), text, strict=True):
                        field.ChangeValue(value)
                    self.stimulus.SetSelection(source)
                    self.heads.SetSelection(heads)
                    self._update_mode()
                    self._set_status('The factory settings for this test have been restored.')
                else:
                    self.options = dialog.options

    def _save_test_defaults(self, options):
        state = self._parameters(options=options)
        stimulus = state.stimulus
        if self.mode == 'tape_playback':
            if not stimulus.low_hz <= options.anchor_hz <= stimulus.high_hz:
                raise ValueError('Set a frequency range that includes the playback reference before saving defaults.')
        else:
            stimulus.band(self.controller.settings.get('sampleRate') or 384_000)
        defaults = dict(self.controller.settings.get('responseTestDefaults', {}))
        defaults[self.mode] = {
            'duration': stimulus.duration, 'lowHz': stimulus.low_hz, 'highHz': stimulus.high_hz,
            'levelDb': stimulus.level_db, 'source': stimulus.kind,
            'tapeHeads': 'two_head' if self.heads.GetSelection() else 'three_head', 'resolution': options.resolution,
            'tailSeconds': options.tail_seconds, 'anchorHz': options.anchor_hz, 'trackTape': options.track_tape,
            'playbackAggregate': options.playback_aggregate, 'calibrationTrimDb': options.calibration_offset_db,
            'channels': {None: 'audio', False: 'mono', True: 'stereo'}[options.stereo],
        }
        saved = self.controller.apply_settings(responseTestDefaults=defaults)
        if saved:
            self._set_status('Saved the starting settings for this test.')
        return saved

    def _clear_test_defaults(self):
        defaults = dict(self.controller.settings.get('responseTestDefaults', {}))
        defaults.pop(self.mode, None)
        return self.controller.apply_settings(responseTestDefaults=defaults)

    def _audio_settings(self):
        self.controller.navigate('settings', 'audio')

    def _help(self):
        topic = next(anchor for key, _, anchor in MODES if key == self.mode)
        self.controller._show_guide(f'{HELP_CHAPTER}#{topic}')

    def _key(self, event):
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            self._hide()
        elif event.GetKeyCode() == wx.WXK_F1:
            self._help()
        else:
            event.Skip()

    def _parameters(self, action='measure', *, options=None):
        options = self.options if options is None else options
        if self.mode == 'tape_recording' and self._reference is not None and action in ('rewound', 'open_wav'):
            stimulus = self._reference[0]
            if options.stereo is None and action != 'open_wav':
                options = replace(options, stereo=self.controller.settings['channels'] == 2)
            return ResponsePass(self.mode, stimulus, options, action)
        low, high = _number(self.low, 'lowest frequency'), _number(self.high, 'highest frequency')
        duration = _number(self.duration, 'test duration')
        if self.mode == 'tape_playback' and action == 'measure' and not 1 <= duration <= MAX_CAPTURE_SECONDS:
            raise ValueError(f'Enter a capture time of 1 to {MAX_CAPTURE_SECONDS} seconds.')
        if not 5 <= low < high or high / low < 1.10:
            raise ValueError('Choose frequencies at least 5 Hz and at least 10 percent apart.')
        level = -20 if self.mode == 'tape_playback' else _number(self.level, 'peak output level')
        stimulus_type = InterfaceStimulus if self.mode == 'interface' else ResponseStimulus
        stimulus = stimulus_type(SOURCES[self.stimulus.GetSelection()][0], low, high, duration, level)
        if options.stereo is None and action != 'open_wav':
            options = replace(options, stereo=self.controller.settings['channels'] == 2)
        return ResponsePass(self.mode, stimulus, options, action)

    def _run(self):
        if self.mode == 'interface':
            if self._disconnect_required:
                if self.controller.confirm_interface_disconnected():
                    self._disconnect_required = False
                    self._update_mode()
                    if self._close_after_disconnect:
                        self._close_after_disconnect = False
                        self._hide()
                    else:
                        self._show_result()
                        if self.save_baseline.IsEnabled():
                            self.save_baseline.SetFocus()
                return
            if not self._interface_ready:
                if self.controller.hold_interface_loopback():
                    self._interface_ready = True
                    self._data.pop('interface', None)
                    self._routes.pop('interface', None)
                    self._update_mode()
                    self._show_result()
                    self._set_status('Software monitoring is muted. Connect the loopback cable before measuring.')
                elif self.controller.settings.get('interfaceLoopbackPendingDisconnect', False):
                    self._interface_ready = True
                    self._require_disconnect()
                return
        two_head = self.mode == 'tape_recording' and self.heads.GetSelection() == 1
        self._begin('record' if two_head else 'measure')

    def _require_disconnect(self):
        if self.mode != 'interface' or not self._interface_ready:
            return
        self._interface_ready = False
        self._disconnect_required = True
        self._request = None
        self._update_mode()
        self.run.SetFocus()

    def _capture_recorded(self):
        self._begin('rewound')

    def _begin(self, action):
        if self._capture is not None or self._request is not None or self._busy:
            return
        try:
            state = self._parameters(action)
            request = self._request = object()

            def started(handle):
                self._started(handle, state, request)

            def still_wanted():
                return self._request is request and self._alive.is_set() and bool(self) and self.IsShown()

            # The return may be at a different sample rate. Validation at the
            # negotiated output rate is done before the engine queues any PCM.
            self._set_status('Opening the audio route…')
            self._enable()
            if action == 'record':
                capture = self.controller.bench_play_reference(state.stimulus, on_ready=started, still_wanted=still_wanted)
            else:
                stimulus = (None if state.kind == 'tape_playback' or action == 'rewound' else state.stimulus)
                duration = (state.stimulus.duration if state.kind == 'tape_playback' else
                            state.stimulus.total_seconds + LEAD_SECONDS + state.options.tail_seconds)
                capture = self.controller.bench_start_capture(duration, stimulus=stimulus, stereo=state.options.stereo,
                                                               on_ready=started, interface_loopback=state.kind == 'interface',
                                                               still_wanted=still_wanted)
            if capture is not None and self._capture is not capture:
                started(capture)
        except (OSError, RuntimeError, ValueError) as error:
            self._request = None
            self._require_disconnect()
            self._enable()
            self._set_status(str(error))
            _show_error(self, error)

    def _started(self, capture, state, request):
        if request is not self._request or not self._alive.is_set() or not self or not self.IsShown():
            if capture is not None:
                self.controller.bench_cancel_capture(capture)
            return
        self._request = None
        if capture is None:
            self._require_disconnect()
            self._enable()
            self._set_status('Audio capture did not start. Check the selected devices and the app’s permission to use audio input.')
            return
        self._capture, self._pass = capture, state
        self._capture_route = self._route(capture.output_rate)
        settings = self.controller.settings
        if state.kind == 'tape_playback' and settings.get('tapeAutoStop'):
            self._tape_tail = PlaybackTailDetector(capture.input_rate, state.stimulus.low_hz, state.stimulus.high_hz,
                                                    settings['tapeSilenceSeconds'], settings['tapeSilenceDb'],
                                                    stereo=state.options.stereo is not False)
        self.progress.SetValue(0)
        if (capture.reference is not None and state.action != 'record'
                and not (state.kind == 'tape_recording' and state.options.track_tape)):
            self._live_selection = self.metric.GetSelection()
            self.metric.Set(['Return Waveform', 'Live Gain', 'Live Phase'])
            self.metric.SetSelection(0)
        self._set_live_axes()
        self._enable()
        self._timer.Start(160)
        self._set_status('Recording the test signal.' if state.action == 'record' else 'Capturing audio. Play the test signal now.')

    def _plot_selection_changed(self):
        if self._capture is not None:
            self._set_live_axes()
            self._enable()
        else:
            self._show_result()

    def _correction_changed(self):
        if self._capture is None:
            self._show_result(fit=False)
        elif self._live_preview is not None:
            self._show_live_preview(*self._live_preview)

    def _set_live_axes(self):
        metric = max(0, self.metric.GetSelection()) if self._live_selection is not None else 0
        labels = (
            ('Live return waveform', 'Time before now (ms)', 'Amplitude (full scale)', (-1, 1)),
            ('Live gain preview', 'Frequency (Hz)', 'Gain (dB)', None),
            ('Live aligned phase preview', 'Frequency (Hz)', 'Phase (degrees)', (-180, 180)),
        )
        name, x_label, y_label, limits = labels[metric]
        self.plot.log_x = bool(metric)
        self.plot.set_labels(name, x_label, y_label)
        self.plot.set_default_view(ylim=limits)
        self.plot.clear()
        self._live_next = 0
        self._live_preview = None
        self._live_correction_status = ''

    def _restore_plot_selection(self):
        if self._live_selection is not None:
            self.metric.Set([label for _, label in PLOT_CHOICES])
            self.metric.SetSelection(self._live_selection)
            self._live_selection = None
        self._live_task = None
        self._live_preview = None
        self._live_correction_status = ''

    def _start_live_preview(self, capture, state, metric):
        if self._live_task is not None or capture.seconds < self._live_next:
            return
        self._live_next = max(.9, capture.seconds + .65)
        ticket = self._live_task = object()

        def work():
            options = state.options
            try:
                curve = preview_transfer(capture, state.stimulus, resolution=options.resolution,
                                         offset_ms=options.offset_ms, drift_ppm=options.drift_ppm)
            except (ArithmeticError, RuntimeError, ValueError):
                curve = None
            if self._alive.is_set():
                wx.CallAfter(self._live_preview_ready, ticket, capture, metric, curve, options.stereo)

        Thread(target=work, name='Fluttery response preview', daemon=True).start()

    def _live_preview_ready(self, ticket, capture, metric, curve, stereo):
        if self._live_task is not ticket:
            return
        self._live_task = None
        if not self._alive.is_set() or self._capture is not capture or self.metric.GetSelection() != metric or curve is None:
            return
        if stereo is False:
            curve = replace(curve, gain_db=curve.gain_db[:, :1],
                            phase_deg=curve.phase_deg[:, :1] if curve.phase_deg is not None else None)
        self._live_preview = (capture, 'gain' if metric == 1 else 'phase', curve)
        self._show_live_preview(*self._live_preview)

    def _show_live_preview(self, capture, metric, curve):
        route = self._capture_route
        profile, problem = self._comparison_profile(curve, capture.input_rate, route)
        traces, _ = self._response_series('', curve, metric, profile, route, compare=self.compare_raw.GetValue())
        self.plot.set_series(traces, fit=True)
        self._live_correction_status = self._correction_status(curve, metric, profile, problem, traces)

    def _route(self, output_rate):
        settings = self.controller.settings

        def identity(which):
            read = getattr(self.controller, f'audio_{which}_identity', None)
            return (read() if callable(read) else None) or settings.get(f'{which}Device') or ''

        return ResponseRoute(identity('input'), identity('output') if output_rate else '', output_rate)

    def _tick(self, _event):
        capture = self._capture
        if capture is None:
            return
        capture.abort_stalled_input()
        if (self._tape_tail is not None and not capture.error
                and (end_frame := self._tape_tail.observe(capture)) is not None):
            capture.finish()
            self._done_capture(end_frame=end_frame)
            return
        if not capture.active:
            self._done_capture()
            return
        self.progress.SetValue(min(1000, round(1000 * capture.seconds / capture.duration)))
        samples = capture.preview(0.024)
        if not self._pass.options.stereo:
            samples = samples[:, :1]
        labels = ('Left', 'Right') if samples.shape[1] == 2 else ('Mono',)
        live_metric = self.metric.GetSelection() if self._live_selection is not None else 0
        if live_metric > 0:
            self._start_live_preview(capture, self._pass, live_metric)
        elif len(samples) > 2:
            decimation = max(1, len(samples) // 1400)
            preview = samples[::decimation]
            time_ms = (np.arange(len(preview)) * decimation - len(samples)) * 1000 / capture.input_rate
            self.plot.set_series([{'label': label, 'x': time_ms, 'y': column}
                                  for label, column in zip(labels, preview.T, strict=True)], fit=True)
        step = 'Recording test signal' if self._pass.action == 'record' else 'Capturing return'
        if not len(samples):
            self._set_status(f'{step}. Waiting for audio input.')
        else:
            peaks = np.max(np.abs(samples), axis=0)
            levels = 20 * np.log10(np.maximum(peaks, 1e-9))
            reading = ', '.join(f'{label} {level:.1f} dBFS' for label, level in zip(labels, levels, strict=True))
            clipped = ' Input clipping.' if np.any(peaks >= 0.999) else ''
            preview = ' Preview timing is provisional until the end marker is captured.' if live_metric > 0 else ''
            correction = f'\n{self._live_correction_status}' if live_metric > 0 and self._live_correction_status else ''
            self._set_status(f'{step}. {capture.seconds:.1f} of {capture.duration:g} s. Peak {reading}.{clipped}{preview}'
                             f'{correction}')

    def _done_capture(self, *, end_frame=None):
        capture, state = self._capture, self._pass
        self._capture, self._pass = None, None
        self._tape_tail = None
        route, self._capture_route = self._capture_route, None
        self._timer.Stop()
        self.controller.bench_finish_capture(capture)
        self._restore_plot_selection()
        self._require_disconnect()
        if capture.error:
            self._set_status(capture.error)
            self._show_result(update_status=False)
            return
        if state.action == 'record':
            self._reference = (state.stimulus, capture.reference, capture.output_rate)
            self._reference_route = route
            self.progress.SetValue(1000)
            self._set_status('The test signal has finished. Stop recording and rewind before choosing Capture Recorded Tape.')
            self._show_result(update_status=False)
            return
        known = self._reference if state.action == 'rewound' else (state.stimulus, capture.reference, capture.output_rate)
        if state.action == 'rewound' and route is not None and self._reference_route is not None:
            route = ResponseRoute(route.input_device, self._reference_route.output_device, self._reference_route.output_rate)
        def analyse():
            # Tail silence confirms the transport stopped, but it contributes
            # no response data and would affect the end of the tone fit.
            samples = capture.read() if end_frame is None else capture.section(0, end_frame)
            return self._analyse(samples, capture.input_rate, state, known)

        message = ('Tape-end silence detected. Analysing the captured tones…' if end_frame is not None
                   else 'Analysing the captured return…')
        self._background(message, analyse, lambda curve: self._save_result(
            curve, state, capture.input_rate, route=route, tape_end=end_frame is not None))

    @staticmethod
    def _analyse(samples, rate, state, known):
        options = state.options
        if options.stereo is False and samples.ndim == 2:
            samples = samples[:, :1]
        if state.action == 'open_wav' and options.stereo and (samples.ndim == 1 or samples.shape[1] < 2):
            raise ValueError('The WAV has one channel. Choose Automatic or Mono in Advanced to analyse it.')
        if state.kind == 'tape_playback':
            return trace_playback(samples, rate, low_hz=state.stimulus.low_hz, high_hz=state.stimulus.high_hz,
                                  resolution=options.resolution, anchor_hz=options.anchor_hz,
                                  aggregate=options.playback_aggregate)
        stimulus, reference, reference_rate = known
        return measure_response(reference, samples, reference_rate, rate, stimulus, resolution=options.resolution,
                                offset_ms=options.offset_ms, drift_ppm=options.drift_ppm,
                                track_recorded_sweep=state.kind == 'tape_recording' and options.track_tape)

    def _background(self, message, operation, done):
        self._busy = True
        self._enable()
        self._set_status(message)

        def work():
            try:
                output, error = operation(), None
            except (ArithmeticError, OSError, RuntimeError, ValueError) as failure:
                output, error = None, failure
            if self._alive.is_set():
                wx.CallAfter(self._background_finished, done, output, error)

        Thread(target=work, name='Fluttery frequency response', daemon=True).start()

    def _background_finished(self, done, output, error):
        if not self._alive.is_set():
            return
        self._busy = False
        if error:
            self._set_status(str(error))
            self._show_result(update_status=False)
            if self.IsShown():
                _show_error(self, error)
        else:
            done(output)
        self._enable()

    def _save_result(self, curve, state, rate, *, route=None, tape_end=False):
        label = 'Tape playback' if state.kind == 'tape_playback' else f'{state.stimulus.level_db:g} dBFS'
        if state.kind == 'tape_playback':
            self._tape_end_trimmed = tape_end
        self._data[state.kind] = (label, curve, rate)
        self._routes[state.kind] = route
        self.progress.SetValue(1000)
        self._show_result()
        profile, _ = self._comparison_profile(curve, rate, route)
        applied = profile is not None and not self.bypass.GetValue()
        corrected_gain = self._gain(curve, profile) if profile is not None else None
        corrected_phase = (compensate_phase(curve, self._timed_profile(profile, route))
                           if self._metric_profile(curve, profile, 'phase') is not None else None)
        raw_delay = phase_group_delay(curve.frequencies, curve.phase_deg)
        corrected_delay = phase_group_delay(curve.frequencies, corrected_phase)
        gain = corrected_gain if applied else curve.gain_db
        phase = corrected_phase if applied and corrected_phase is not None else curve.phase_deg
        delay = corrected_delay if applied and corrected_phase is not None else raw_delay

        def column(values, channel):
            if values is None or channel >= values.shape[1]:
                return []
            return [float(value) if np.isfinite(value) else None for value in values[:, channel]]

        record = getattr(self.controller, 'record_bench_result', None)
        if callable(record):
            baseline = curve.baseline.as_record() if curve.baseline is not None else None
            record('frequency-response', {
                'name': label, 'mode': state.kind, 'frequencyHz': curve.frequencies.tolist(),
                'leftDb': column(gain, 0), 'rightDb': column(gain, 1),
                'rawLeftDb': column(curve.gain_db, 0), 'rawRightDb': column(curve.gain_db, 1),
                'correctedLeftDb': column(corrected_gain, 0), 'correctedRightDb': column(corrected_gain, 1),
                'leftPhase': column(phase, 0), 'rightPhase': column(phase, 1),
                'rawLeftPhase': column(curve.phase_deg, 0), 'rawRightPhase': column(curve.phase_deg, 1),
                'correctedLeftPhase': column(corrected_phase, 0), 'correctedRightPhase': column(corrected_phase, 1),
                'leftGroupDelayMs': column(delay, 0), 'rightGroupDelayMs': column(delay, 1),
                'rawLeftGroupDelayMs': column(raw_delay, 0), 'rawRightGroupDelayMs': column(raw_delay, 1),
                'correctedLeftGroupDelayMs': column(corrected_delay, 0), 'correctedRightGroupDelayMs': column(corrected_delay, 1),
                'leftCoherence': column(curve.coherence, 0), 'rightCoherence': column(curve.coherence, 1),
                'sampleRate': rate, 'delayMs': curve.delay_ms, 'driftPpm': curve.drift_ppm,
                'clipping': curve.clipping, 'relative': curve.relative, 'profile': profile['name'] if profile else '',
                'correctionApplied': applied,
                'inputDevice': route.input_device if route else '', 'outputDevice': route.output_device if route else '',
                'outputRate': route.output_rate if route else None, 'interfaceBaseline': baseline,
            })

    def _stop_capture(self):
        if self._request is not None:
            self._request = None
            self._require_disconnect()
            self._enable()
            self._set_status('Capture cancelled.')
            return
        if self._capture is None:
            return
        if self._pass.kind == 'tape_playback' and self._capture.seconds >= 0.25:
            self._capture.finish()
            self._done_capture()
        else:
            self._cancel_capture()
            self._require_disconnect()
            self._set_status('Capture cancelled.')

    def _cancel_capture(self):
        if self._capture is not None:
            capture = self._capture
            self._capture, self._pass = None, None
            self._capture_route = None
            self._tape_tail = None
            self._timer.Stop()
            self.controller.bench_cancel_capture(capture)
            self._restore_plot_selection()
            self.progress.SetValue(0)
            self._show_result(update_status=False)

    def _open_wav(self):
        try:
            state = self._parameters('open_wav')
        except ValueError as error:
            _show_error(self, error)
            return
        with wx.FileDialog(self, 'Open Captured Return', wildcard='WAV audio (*.wav)|*.wav',
                           style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            path = dialog.GetPath()
        known = self._reference

        def read():
            rate, samples = read_wav_samples(path)
            reference = known
            if state.kind != 'tape_playback' and reference is None:
                reference = (state.stimulus, state.stimulus.render(rate), rate)
            return self._analyse(samples, rate, state, reference), rate

        self._background('Reading the recorded return…', read, lambda data: self._save_result(data[0], state, data[1]))

    def _open_reference(self):
        try:
            state = self._parameters('open_reference')
        except ValueError as error:
            _show_error(self, error)
            return
        with wx.FileDialog(self, 'Open Response Test WAV', wildcard='WAV audio (*.wav)|*.wav',
                           style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            path = dialog.GetPath()

        def read():
            rate, samples = read_wav_samples(path)
            expected = state.stimulus.render(rate)
            if len(samples) != len(expected) or np.max(np.abs(samples[:, 0] - expected)) > 2e-4:
                raise ValueError('Set the signal type, duration, frequencies and level to match the original test before opening its WAV file.')
            return rate, samples[:, 0]

        def loaded(data):
            rate, reference = data
            self._reference = (state.stimulus, reference, rate)
            self._reference_route = None
            self._set_status('The reference signal is ready. Capture the returned signal or open the recording.')

        self._background('Reading the test reference…', read, loaded)

    def _save_stimulus(self):
        try:
            state = self._parameters()
            active_rate = self.controller.audio.snapshot().get('health', {}).get('outputSampleRate')
            rate = round(active_rate or self.controller.settings['sampleRate'] or 48_000)
            state.stimulus.band(rate)
        except ValueError as error:
            _show_error(self, error)
            return
        with wx.FileDialog(self, 'Save Response Test WAV', wildcard='WAV audio (*.wav)|*.wav',
                           defaultFile='Fluttery Response Test.wav', style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            path = dialog.GetPath()

        def write():
            stimulus = state.stimulus.render(rate)
            _atomic_save(path, lambda stream: wavfile.write(stream, rate, stimulus), binary=True)
            return stimulus

        def saved(samples):
            if state.kind == 'tape_recording':
                self._reference = (state.stimulus, samples, rate)
                self._reference_route = None
            self._set_status(f'Test WAV saved at {rate:g} Hz. The file includes timing markers.')

        self._background('Writing the test signal…', write, saved)

    def _reload_profiles(self, selected=KEEP_PROFILE):
        if selected is KEEP_PROFILE:
            selected = self._profile_name()
        profiles = self.controller.settings.get('responseCalibrations', [])
        names = [profile['name'] for profile in profiles]
        self._profile_options = profiles
        self.calibration.Set([AUTO_PROFILE, NO_PROFILE, *(
            f'{name} (saved profile)' if name in (AUTO_PROFILE, NO_PROFILE) else name for name in names)])
        index = 0 if selected is None else 1
        if selected in names:
            index = 2 + names.index(selected)
        self.calibration.SetSelection(index)

    def _profile_name(self):
        index = self.calibration.GetSelection()
        if index <= 0:
            return None
        return self._profile_options[index - 2]['name'] if 2 <= index < len(self._profile_options) + 2 else ''

    def _selected_profile(self):
        name = self._profile_name()
        return next((profile for profile in self._profile_options if profile['name'] == name), None)

    def _comparison_profile(self, curve, rate, route):
        if self.mode not in ('device', 'tape_recording') or self._profile_name() == '':
            return None, ''
        if (profile := self._selected_profile()) is not None:
            problem = self._correction_problem(curve, rate, profile, route)
            return (None, problem) if problem else (profile, '')
        if route is None or not route.output_rate:
            return None, 'The original output route is unknown.'
        if not self._profile_options:
            return None, 'No interface response profiles have been saved.'
        matches = [profile for profile in self._profile_options if self._profile_for(curve, rate, profile, route)]
        if len(matches) == 1:
            return matches[0], ''
        if matches:
            return None, 'More than one profile matches this route. Choose the profile you want by name.'
        return None, 'No interface response profile matches this capture’s audio route.'

    def _manage(self):
        with CalibrationProfilesDialog(self, self.controller.settings.get('responseCalibrations', []),
                                       self._profile_name()) as dialog:
            if dialog.ShowModal() == wx.ID_OK and self.controller.apply_settings(responseCalibrations=dialog.profiles):
                self._reload_profiles(dialog.active_profile)
                self._show_result()

    def _save_calibration(self):
        if self._interface_ready or self._disconnect_required:
            return
        result = self._data.get('interface')
        if result is None:
            return
        _, curve, rate = result
        if curve.clipping:
            _show_error(self, 'The input clipped during the capture. Lower the level and measure again before saving.')
            return
        route = self._routes.get('interface')
        if (route is None or not route.output_rate or not route.input_device or not route.output_device
                or route.input_device == '<default input>' or route.output_device == '<default output>'):
            _show_error(self, 'Select an input and an output, then measure the loopback before saving a profile.')
            return
        with wx.TextEntryDialog(self, 'Profile name', 'Save Interface Response', 'Interface loopback') as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            name = dialog.GetValue().strip()
        try:
            if not 1 <= len(name) <= 80:
                raise ValueError('Enter a profile name of 1 to 80 characters.')
            settings = self.controller.settings
            profile = calibration_profile(name, curve, rate, input_device=route.input_device,
                                          output_device=route.output_device, output_rate=route.output_rate,
                                          offset_db=self.options.calibration_offset_db)
            profiles = list(settings.get('responseCalibrations', []))
            found = next((i for i, current in enumerate(profiles) if current['name'].casefold() == name.casefold()), None)
            if found is None and len(profiles) >= 64:
                raise ValueError('Delete an interface profile before saving another one.')
            if found is None:
                profiles.append(profile)
            elif wx.MessageBox(f'Replace “{name}”?', 'Replace Interface Response', wx.YES_NO | wx.NO_DEFAULT,
                               self) == wx.YES:
                profiles[found] = profile
            else:
                return
            if self.controller.apply_settings(responseCalibrations=profiles):
                self._reload_profiles(name)
                self._set_status(f'Interface response saved as {name}.')
        except ValueError as error:
            _show_error(self, error)

    def _pin(self):
        result = self._data.get(self.mode)
        if result is None:
            return
        label, curve, rate = result
        pinned = self._pinned.setdefault(self.mode, [])
        with wx.TextEntryDialog(self, 'Name the comparison trace', 'Pin Response Trace', label) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            label = dialog.GetValue().strip()[:80] or f'Comparison {len(pinned) + 1}'
        pinned.append((label, curve, rate))
        self._pin_routes.setdefault(self.mode, {})[id(curve)] = self._routes.get(self.mode)
        self._show_result()

    def _clear_pins(self):
        self._pinned.pop(self.mode, None)
        self._pin_routes.pop(self.mode, None)
        self._show_result()

    def _route_for(self, curve):
        current = self._data.get(self.mode)
        if current is not None and curve is current[1]:
            return self._routes.get(self.mode)
        return self._pin_routes.get(self.mode, {}).get(id(curve))

    @staticmethod
    def _route_correction_problem(profile, route):
        # A loopback measures both converters. The input alone cannot identify
        # the correct curve, even if the same interface was used to record it.
        if route is None:
            return 'The recording’s audio route is unknown.'
        if not route.output_rate or not route.output_device:
            return 'The original test signal’s output route is unknown.'
        unconfirmed = ('', '<default input>', '<default output>')
        if route.input_device in unconfirmed or route.output_device in unconfirmed:
            return 'The capture did not identify both audio devices.'
        if (profile.get('input', '') in unconfirmed or profile.get('output', '') in unconfirmed
                or not profile.get('sampleRate') or not profile.get('outputRate')):
            return 'This profile is missing audio route details. Measure the loopback again before applying correction.'
        if route.input_device != profile['input'] or route.output_device != profile['output']:
            return 'The capture and the profile use different audio devices.'
        if abs(route.output_rate - profile['outputRate']) > 1:
            return 'The capture and the profile use different output sample rates.'
        return None

    @staticmethod
    def _correction_problem(curve, rate, profile, route):
        if curve.relative:
            return 'Tape playback uses a reference on the tape. Loopback correction requires a known interface output.'
        if profile is None:
            return 'Choose a loopback profile to apply interface correction.'
        if problem := ResponseDialog._route_correction_problem(profile, route):
            return problem
        if abs(profile['sampleRate'] - rate) > 1:
            return 'The capture and the profile use different input sample rates.'
        if len(profile['gainDb'][0]) < curve.gain_db.shape[1]:
            return 'The profile has fewer measured channels than the capture does.'
        return None

    @staticmethod
    def _profile_for(curve, rate, profile, route):
        return profile if ResponseDialog._correction_problem(curve, rate, profile, route) is None else None

    @staticmethod
    def _timed_profile(profile, route):
        return profile if profile is not None and ResponseDialog._route_correction_problem(profile, route) is None else None

    @staticmethod
    def _gain(curve, profile):
        return curve.gain_db if profile is None else compensate_response(curve, profile)

    def _plot_data(self, curve, profile, metric, route=None):
        if metric == 'measured_phase':
            return curve.phase_deg
        if metric == 'phase':
            return compensate_phase(curve, self._timed_profile(profile, route))
        if metric == 'delay':
            phase = compensate_phase(curve, self._timed_profile(profile, route))
            return phase_group_delay(curve.frequencies, phase)
        if metric == 'coherence':
            return curve.coherence
        if metric == 'measured':
            return curve.gain_db
        gain = self._gain(curve, profile)
        return response_balance(gain) if metric == 'balance' else gain

    @staticmethod
    def _metric_profile(curve, profile, metric):
        if metric in ('gain', 'balance'):
            return profile if metric == 'gain' or curve.gain_db.shape[1] == 2 else None
        if metric in ('phase', 'delay') and profile and curve.phase_deg is not None and 'phaseDeg' in profile:
            return profile
        return None

    def _response_series(self, label, curve, metric, profile, route, *, compare=False, scale=1):
        alternate = self._metric_profile(curve, profile, metric)
        bypass = self.bypass.GetValue()
        primary = None if bypass else alternate
        primary_data = self._plot_data(curve, primary, metric, route)
        values = [(primary, primary_data)]
        if compare and alternate is not None:
            # Keep legend order and colours stable when Bypass changes the
            # primary value without changing the two traces on the plot.
            other = self._plot_data(curve, alternate if bypass else None, metric, route)
            values = [(alternate, other), (None, primary_data)] if bypass else [*values, (None, other)]
        traces = []
        for correction, samples in values:
            if samples is None:
                continue
            tag = 'Corrected' if correction else 'Raw'
            channels = ('Left', 'Right') if samples.shape[1] == 2 else ('Mono',)
            for channel, data in zip(channels, samples.T, strict=True):
                if not np.isfinite(data).any():
                    continue
                # Wrapped phase and stepped test tones are point samples.
                dashed = (correction is None and alternate is not None) or channel == 'Right'
                style = ('points' if curve.relative or metric in ('phase', 'measured_phase')
                         else 'dashed' if dashed else 'line')
                channel_label = '' if metric == 'balance' else f' {channel}'
                trace_label = f'{tag}{channel_label}' + (f' · {label}' if label else '')
                traces.append({'label': trace_label, 'x': curve.frequencies / scale,
                               'y': data, 'style': style})
        return traces, primary_data is not None and bool(np.isfinite(primary_data).any())

    def _correction_status(self, curve, metric, profile, problem, traces):
        if self.mode not in ('device', 'tape_recording'):
            return ''
        if metric in ('measured', 'measured_phase', 'coherence'):
            return ''
        if profile is None:
            return f'{problem} Showing raw data.' if problem else 'Interface correction is off. Showing raw data.'
        if self._metric_profile(curve, profile, metric) is None:
            if metric in ('phase', 'delay') and curve.phase_deg is not None:
                return f'{profile["name"]} does not contain a phase correction. Showing raw data.'
            return ''
        has_corrected = any(trace['label'].split()[0] == 'Corrected' for trace in traces)
        if self.compare_raw.GetValue() and has_corrected and any(trace['label'].split()[0] == 'Raw' for trace in traces):
            primary = 'Raw' if self.bypass.GetValue() else 'Corrected'
            return f'Comparing raw and corrected data using {profile["name"]}. {primary} is the active response.'
        if self.bypass.GetValue():
            return f'Correction bypassed. Showing raw data. Profile available: {profile["name"]}.'
        return f'Interface correction: {profile["name"]}.' if has_corrected else (
            f'No corrected points for {profile["name"]} in this plot.')

    def _show_result(self, update_status=True, *, fit=True):
        if self._capture is not None:
            return
        self.plot.log_x = True
        result = self._data.get(self.mode)
        metric = PLOT_CHOICES[max(0, self.metric.GetSelection())][0]
        gain_unit = 'Relative level (dB)' if self.mode == 'tape_playback' else 'Gain (dB)'
        name, unit = {
            'gain': ('Frequency response', gain_unit),
            'measured': ('Measured frequency response', gain_unit),
            'balance': ('Channel balance', 'Right minus left (dB)'),
            'phase': ('Aligned phase', 'Phase (degrees)'),
            'measured_phase': ('Measured aligned phase', 'Phase (degrees)'),
            'delay': ('Group delay', 'Delay (ms)'),
            'coherence': ('Noise coherence', 'Coherence'),
        }[metric]
        curves_to_show = (*self._pinned.get(self.mode, []), *([result] if result else []))
        scale = 1000 if any(curve.frequencies[-1] >= 10_000 for _, curve, _ in curves_to_show) else 1
        self.plot.set_labels(name, 'Frequency (kHz)' if scale == 1000 else 'Frequency (Hz)', unit)
        bounds = {'phase': (-180, 180), 'measured_phase': (-180, 180), 'coherence': (0, 1)}.get(metric)
        curves = []
        latest_has_data = False
        profile = None
        status = ''
        for index, (label, curve, rate) in enumerate(curves_to_show):
            route = self._route_for(curve)
            candidate, problem = self._comparison_profile(curve, rate, route)
            latest = result is not None and index == len(curves_to_show) - 1
            traces, available = self._response_series(label if len(curves_to_show) > 1 else '',
                                                       curve, metric, candidate, route,
                                                       compare=latest and self.compare_raw.GetValue(), scale=scale)
            curves.extend(traces)
            if latest:
                latest_has_data, profile = available, candidate
                status = self._correction_status(curve, metric, candidate, problem, traces)
        if metric == 'balance' and curves:
            limit = max(1.0, 1.08 * max(float(np.nanmax(np.abs(trace['y']))) for trace in curves))
            bounds = -limit, limit
        self.plot.set_default_view(ylim=bounds)
        self.plot.set_series(curves, fit=fit)
        self._enable()
        if update_status:
            effective = None if self.bypass.GetValue() else profile
            summary = self._result_summary(result, profile, effective, metric, latest_has_data)
            self._set_status(f'{status}\n{summary}' if status else summary)

    def _result_summary(self, result, profile, effective, metric, has_data):
        if result is None:
            return 'Ready. Follow the connection and test steps for this instrument.'
        _, curve, rate = result
        bands = np.isfinite(curve.gain_db).any(axis=1)
        left, right = curve.frequencies[bands][[0, -1]] if bands.any() else (0, 0)
        summary = [f'{bands.sum()} bands from {left:g} to {right:g} Hz, captured at {rate:g} Hz.']
        if not has_data:
            summary.insert(0, {
                'phase': 'Aligned phase needs a timed test signal with stable playback speed.',
                'measured_phase': 'Aligned phase needs a timed test signal with stable playback speed.',
                'delay': 'Group delay needs several measured phase points from a stable timed test.',
                'coherence': 'Noise coherence needs a white or pink noise test with a known reference.',
                'balance': 'Channel balance needs matching left and right frequency data.',
                'gain': 'No gain points in the overlapping correction band.' if effective else 'No usable gain points.',
                'measured': 'No usable gain points.',
            }[metric])
        elif metric in ('gain', 'measured', 'balance'):
            used_profile = effective if metric != 'measured' else None
            gain = self._gain(curve, used_profile)
            target = curve.anchor_hz or 1000
            balance = response_balance(gain)
            usable = np.isfinite(balance[:, 0]) if metric == 'balance' else np.isfinite(gain).any(axis=1)
            points = np.flatnonzero(usable)
            nearest = points[np.argmin(np.abs(np.log(curve.frequencies[points] / target)))]
            at_reference = gain[nearest]
            channels = ('Left', 'Right') if len(at_reference) == 2 else ('Mono',)
            readings = ', '.join(f'{channel} {value:+.2f} dB' for channel, value in zip(channels, at_reference, strict=True)
                                 if np.isfinite(value))
            values = f'{curve.frequencies[nearest]:g} Hz. {readings}.'
            if balance is not None and np.isfinite(balance[nearest, 0]):
                values += f' Right minus left {balance[nearest, 0]:+.2f} dB.'
            summary.insert(0, values)
        if curve.delay_ms is not None:
            drift = float(curve.drift_ppm or 0)
            drift = drift if abs(drift) >= .05 else 0.0
            summary.append(f'Capture marker at {curve.delay_ms:.2f} ms. Relative clock drift {drift:+.1f} ppm.')
        if curve.tracked and not curve.relative:
            summary.append('Gain follows the returning tape sweep.')
        if self.mode == 'tape_playback' and self._tape_end_trimmed:
            summary.append('The capture stopped after the test signal fell silent. The analysis excludes the silent tail.')
        if curve.clipping:
            summary.insert(0, 'Input clipped. Lower the level and repeat the measurement.')
        if curve.baseline is not None:
            summary.extend(_baseline_summary(curve.baseline.as_record()))
        route = self._routes.get(self.mode)
        if curve.relative and metric in ('gain', 'measured', 'balance'):
            summary.append(self._correction_problem(curve, rate, profile, route))
        elif profile is not None and metric in ('gain', 'balance', 'phase', 'delay'):
            if effective is None:
                problem = self._correction_problem(curve, rate, profile, route)
                if problem:
                    summary.append(f'{problem} Showing measured data.')
                return '\n'.join(summary)
            if curve.phase_deg is not None and self._timed_profile(effective, route) and 'phaseDeg' in effective:
                summary.append('The profile corrects gain and aligned phase within the shared measured band.')
            elif metric in ('phase', 'delay') and has_data:
                summary.append('Showing measured phase without loopback phase correction.')
            else:
                summary.append('This profile applies gain correction within the shared measured band.')
            if curve.signal_dbfs and (baseline := effective.get('baseline')) is not None:
                noises = baseline['noiseDbfs'][:len(curve.signal_dbfs)]
                margin = min(signal - noise for signal, noise in zip(curve.signal_dbfs, noises, strict=True))
                if margin < 10:
                    summary.append(f'The return is only {margin:.1f} dB above the measured interface noise floor. '
                                   'Low-level results may be inconclusive.')
        return '\n'.join(summary)

    def _export(self):
        if self.mode not in self._data:
            return
        result = self._data[self.mode]
        traces = []
        for label, curve, rate in (*self._pinned.get(self.mode, []), result):
            route = self._route_for(curve)
            profile, _ = self._comparison_profile(curve, rate, route)
            traces.append((label, curve, rate, route, profile))
        bypass = self.bypass.GetValue()
        with wx.FileDialog(self, 'Export Frequency Response', defaultFile='Fluttery Response.csv',
                           wildcard='CSV table (*.csv)|*.csv', style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            path = dialog.GetPath()

        def write():
            def fill(stream):
                writer = csv.writer(stream)
                writer.writerow(['Trace', 'Channel', 'Frequency (Hz)', 'Measured gain (dB)', 'Corrected gain (dB)',
                                 'Measured aligned phase (degrees)', 'Corrected aligned phase (degrees)',
                                 'Group delay (ms)', 'Coherence', 'Sample rate (Hz)', 'Capture marker (ms)',
                                 'Relative clock drift (ppm)', 'Measured group delay (ms)', 'Corrected group delay (ms)',
                                 'Correction profile', 'Active response'])
                for label, curve, rate, route, profile in traces:
                    timed = self._timed_profile(self._metric_profile(curve, profile, 'phase'), route)
                    corrected = self._gain(curve, profile) if profile is not None else None
                    phase = compensate_phase(curve, timed) if timed is not None else None
                    raw_delay = phase_group_delay(curve.frequencies, curve.phase_deg)
                    corrected_delay = phase_group_delay(curve.frequencies, phase)
                    delay = corrected_delay if not bypass and corrected_delay is not None else raw_delay
                    for channel, values in enumerate(curve.gain_db.T):
                        name = 'Mono' if curve.gain_db.shape[1] == 1 else ('Left', 'Right')[channel]
                        for index, frequency in enumerate(curve.frequencies):
                            if not np.isfinite(values[index]):
                                continue
                            writer.writerow([_csv_cell(label), name, _cell(frequency), _cell(values[index]),
                                             _cell(corrected[index, channel]) if corrected is not None else '',
                                             _cell(curve.phase_deg[index, channel]) if curve.phase_deg is not None else '',
                                             _cell(phase[index, channel]) if phase is not None else '',
                                             _cell(delay[index, channel]) if delay is not None else '',
                                             _cell(curve.coherence[index, channel]) if curve.coherence is not None else '',
                                             _cell(rate), curve.delay_ms, curve.drift_ppm,
                                             _cell(raw_delay[index, channel]) if raw_delay is not None else '',
                                             _cell(corrected_delay[index, channel]) if corrected_delay is not None else '',
                                             _csv_cell(profile['name']) if profile else '',
                                             'Corrected' if profile and not bypass else 'Raw'])
            _atomic_save(path, fill)

        self._background('Saving response data…', write, lambda _: self._set_status('Response CSV saved.'))

    def refresh_display_settings(self):
        self.plot.set_appearance(self.controller.settings)
        self.plot.set_expanded_descriptions(self.controller.settings.get('useAccessibility', True))
        profiles = self.controller.settings.get('responseCalibrations', [])
        if self.mode is not None and self._profile_options != profiles:
            self._reload_profiles()
            if self._capture is None and self._request is None and not self._busy:
                self._show_result()

    def _set_status(self, text):
        self._activity = text
        if not self.status.HasFocus() and self.status.GetValue() != text:
            self.status.ChangeValue(text)

    def _status_blur(self, event):
        self.status.ChangeValue(self._activity)
        event.Skip()

    def keyboard_actions(self):
        return (('Read Response Activity…', self._read_status, 'Read the current measurements and capture status.'),)

    def _read_status(self):
        self.controller._show_text('Response Activity', self._activity, parent=self)

    def _on_close(self, event):
        if event.CanVeto():
            event.Veto()
            self._hide()
        else:
            self.shutdown()
            event.Skip()

    def _hide(self):
        self._request = None
        self._cancel_capture()
        self._enable()
        if self._interface_ready or self._disconnect_required:
            self._close_after_disconnect = True
            self._require_disconnect()
            self.run.SetFocus()
            return
        self.Hide()

    def shutdown(self):
        self._alive.clear()
        self._request = None
        self._cancel_capture()
        self._timer.Stop()
