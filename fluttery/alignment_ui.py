"""Guided native assistants for aligning cartridges, phono stages and tape heads."""

from __future__ import annotations

import csv
import io
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, replace

import numpy as np
import wx
import wx.dataview as dv

from .audio.alignment import (
    ALIGNMENT_OPTION_LIMITS,
    ALIGNMENT_TARGET_LIMITS,
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
from .charts import LinePlot
from .keyboard_access import describe
from .reporting import _csv_cell
from .sessions import atomic_write

MANUAL = '10-alignment.md'


@dataclass(frozen=True, slots=True)
class Step:
    name: str
    mode: str
    instructions: str
    tone_hz: float = 0.0
    waveform: str = ''


@dataclass(frozen=True, slots=True)
class Workflow:
    title: str
    help_anchor: str
    position: str
    instructions: tuple[Step, ...]


CAPTURED_READINGS = 'Captured Readings'

WORKFLOWS = {
    'antiskate': Workflow('Anti-skate Assistant', 'compare-anti-skate-settings', 'Anti-skate setting', (
        Step('Connect and choose a track', 'guide',
             'Connect the phono stage’s stereo output to your interface’s line inputs and set the tracking force. '
             'Choose a moderately demanding band on the test record. It should carry the same steady tone on both '
             'channels, and the record should list the tone’s frequency. Use that band for every setting you compare.'),
        Step('Compare balance', 'tone',
             'Play the test groove, enter the arm’s anti-skate setting and choose Capture Reading. '
             'Replay the groove at nearby settings. Look for low distortion and similar readings on both channels.', 315),
        Step('Verify', 'tone',
             'Try the most promising settings on another suitable test band. Compare readings from the same band '
             'at the same level. Play an ordinary record and recheck tracking force and azimuth.', 315),
    )),
    'tracking-force': Workflow('Tracking Force Assistant', 'compare-tracking-force-settings', 'Measured tracking force (g)', (
        Step('Set the starting force', 'guide',
             'Use a stylus gauge at record height to set the tracking force recommended by the cartridge manufacturer. '
             'Stay within the manufacturer’s permitted range. Choose a stereo tracking test that lists the tone '
             'frequency and recording levels.'),
        Step('Compare on one groove', 'tone',
             'Play the same groove at each setting. Enter the force from your gauge in grams and capture a reading. '
             'Look for low distortion on both channels. Enter the manufacturer’s tracking-force limits in Advanced Settings.', 315),
        Step('Check a more demanding groove', 'tone',
             'Replay a suitable louder band at the settings that worked best. Enter the band name or recorded level '
             'as the test track label. Stop if the stylus repeatedly mistracks. Check anti-skate again afterwards.', 315),
    )),
    'vta': Workflow('Vertical Tracking Angle and Stylus Rake', 'compare-vertical-tracking-angle-and-stylus-rake', 'Arm height or angle label', (
        Step('Choose a two-tone track', 'guide',
             'Set up the arm and cartridge according to the manufacturers’ instructions. Choose a stereo two-tone '
             'intermodulation track, and look up its two frequencies and measurement method.'),
        Step('Compare test positions', 'imd',
             'Enter the two frequencies and choose the intermodulation method in Advanced Settings. '
             'Play the same groove at each setting. Choose positions you can return to for a repeat measurement. '
             'Capture a reading at each position and look for lower distortion on both channels.', 300),
        Step('Verify the result', 'imd',
             'Repeat the track at the positions that gave the lowest readings. If your record has another suitable band, '
             'use it to check those positions again. Enter the arm height or angle as a label so you can match each '
             'distortion reading to the physical setting.', 300),
    )),
    'phono-loading': Workflow('Phono Loading Assistant', 'compare-phono-stage-loading', 'Load setting (Ω and pF)', (
        Step('Connect and choose known-level tones', 'guide',
             'Connect the phono stage’s line outputs to the interface. Keep the turntable, connections and interface '
             'gain the same for each load. Choose reference and comparison tones whose recorded levels the test '
             'record lists.'),
        Step('Capture a reference for each load', 'tone',
             'Choose a load within the recommended range. Play the reference tone and capture a reading under that '
             'load’s label. Repeat with the same tone at each load you want to compare.', 1000),
        Step('Compare response at other frequencies', 'tone',
             'Play a comparison tone. In Advanced Settings, enter its frequency and listed level relative to the reference. '
             'Keep the same load label you used for the reference. Repeat for the other test frequencies, then compare the readings.', 10000),
    )),
    'phono-gain': Workflow('Phono Gain Assistant', 'compare-phono-stage-gain', 'Gain setting (dB)', (
        Step('Connect without changing the interface gain', 'guide',
             'Connect the phono stage’s line outputs to the interface. Pick a reference groove and a loud groove typical '
             'of the records you play. Set the interface gain low enough to leave room for loud peaks and keep it there '
             'while you change the phono gain.'),
        Step('Capture the quiet baseline', 'level',
             'Play an unmodulated groove, or keep the stylus stationary to measure the electronics alone. '
             'Use the same source at every gain setting. Enter the phono stage gain and capture a reading.'),
        Step('Measure the reference groove', 'tone',
             'Play the reference groove at each gain. Use the same labels as in the quiet test. '
             'Compare the digital reference level, relative signal-to-noise ratio and channel balance.', 1000),
        Step('Check peak headroom', 'level',
             'Play the same loud groove at each setting and check for clipping. Make sure your preferred gain leaves '
             'at least the minimum peak headroom you set in Advanced Settings.'),
    )),
    'head-azimuth': Workflow('Tape Head Azimuth', 'measure-tape-head-azimuth', 'Head position label', (
        Step('Connect the deck and choose a test tape', 'guide',
             'Select a stereo input and use a reference tape with an in-phase test tone on both channels. '
             'During the measurement step, enter the test tape’s stated frequency in Advanced Settings. '
             'To measure the interface’s own delay, feed one reference signal to both inputs with a suitable splitter.'),
        Step('Measure the delay', 'azimuth',
             'Play the alignment tone and follow the deck’s service instructions as you watch the compensated delay. '
             'Set the frequency, tolerance or interface offset in Advanced Settings. You can also capture the offset '
             'by feeding the same tone to both inputs.', 10000),
        Step('Verify at another frequency', 'azimuth',
             'Capture the higher-frequency tone first. Leave the head where it is, play a lower-frequency tone from '
             'the same tape and keep the same head position label. The lower tone helps determine which cycle of the '
             'higher tone gives the actual delay.', 1000),
    )),
    'tape-bias': Workflow('Tape Bias and Record Level Assistant', 'adjust-tape-bias-and-record-level', 'Adjustment label', (
        Step('Set up source and off-tape return', 'guide',
             'Follow the manufacturers’ instructions for setting the bias and recording level. Use a blank tape and switch off '
             'noise reduction. On a three-head deck, use off-tape monitoring. On a two-head deck, record a labelled '
             'segment, rewind and play it back to measure it.'),
        Step('Capture the record-level reference', 'tone',
             'Play the reference tone through the deck’s source monitor or connect it directly to the interface. '
             'Capture the level on both channels. Set the test frequency and output level in Advanced Settings, '
             'or use an external generator.', 400, 'sine'),
        Step('Find the bias peak', 'tone',
             'Play the high-frequency test tone at the level given in the service instructions. Start with low bias '
             'and increase it a little at a time. Capture readings as you go so you can see where the level peaks.', 10000, 'sine'),
        Step('Set the overbias drop', 'tone',
             'Set the specified overbias drop in Advanced Settings. Keep increasing bias past the peak. '
             'Fluttery shows how far each channel has fallen from its saved peak, so you can adjust to the target.', 10000, 'sine'),
        Step('Match record level', 'tone',
             'Record and play back the midband reference tone. Adjust the recording level until the playback level matches '
             'the source reference, then check third-harmonic distortion and capture the final levels. '
             'Recheck bias if you made a large adjustment.', 400, 'sine'),
        Step('Verify broadband response', 'noise',
             'Use the same noise signal for the reference and tape tests. To see the change across frequency, '
             'capture the source monitor or direct interface signal first, then switch to tape monitoring. '
             'Compare the plotted bands and capture the final reading.', 0, 'white'),
    )),
}


@dataclass(frozen=True, slots=True)
class Options:
    seconds: float = 0.6
    tone_hz: float = 1000
    second_hz: float = 3000
    tolerance_us: float = 5
    offset_us: float = 0
    target_db: float = 0
    level_dbfs: float = -20
    distortion_limit: float = 3
    minimum_force: float = 1
    maximum_force: float = 3
    method: str = 'sidebands'
    noise: str = 'white'


@dataclass(frozen=True, slots=True)
class Trial:
    step: str
    setting: str
    track: str
    metric: str
    left: float
    right: float
    score: float | None
    # A list lets session JSON hold the original units and optional readings.
    details: str
    peak: tuple[float, ...] = ()
    tone_frequency: float = 0
    comparison: str = ''
    curve: tuple[list[float], ...] = ()
    curve_unit: str = ''
    nominal_hz: float = 0.0
    target_db: float | None = None
    reference_hz: float = 0.0
    tolerance_us: float | None = None
    generator_level_dbfs: float | None = None
    delay_margin_us: float | None = None
    delay_offset_us: float | None = None
    delay_input: str = ''
    verified_delay_us: float | None = None
    verification_fine_hz: float | None = None
    input_route: tuple[str, float, float] | None = None


@dataclass(frozen=True, slots=True)
class Reading:
    metrics: object
    curve: tuple[np.ndarray, ...] = ()


def _captured_medians(trials):
    """Keep acquisition order while pooling repeated readings under the same label."""
    groups = {}
    for trial in trials:
        groups.setdefault(trial.setting.casefold(), []).append(trial)
    settings, labels, left, right = [], [], [], []
    for repeats in groups.values():
        setting = repeats[0].setting
        settings.append(setting)
        labels.append(setting if len(repeats) == 1 else f'{setting} ({len(repeats)} readings)')
        left.append(float(np.median([trial.left for trial in repeats])))
        right.append(float(np.median([trial.right for trial in repeats])))
    return settings, labels, left, right


def _analyse(samples, rate, mode, options):
    if mode == 'tone':
        return Reading(tone_reading(samples, rate, options.tone_hz))
    if mode == 'imd':
        return Reading((two_tone_reading(samples, rate, options.tone_hz, options.second_hz, options.method), levels(samples)))
    if mode == 'level':
        return Reading(levels(samples))
    if mode == 'noise':
        frequency, curves = band_curve(samples, rate)
        return Reading(levels(samples), (frequency, *curves))
    if mode == 'azimuth':
        measurement = azimuth_reading(samples, rate, options.tone_hz)
        # Show several cycles from one contiguous segment at the same scale
        # on both axes. Gain differences remain visible when the delay is zero.
        xy = samples[-max(14, round(9 * rate / measurement.frequency)):]
        return Reading(measurement, tuple(xy.T))
    raise ValueError('Choose a measurement step.')


_FIELDS = {
    'seconds': ('Analysis interval (s)', 0.1, 2, 'Duration of audio used for each live reading.'),
    'tone_hz': ('Test tone (Hz)', 1, 2, 'Enter the nominal frequency listed for your test track.'),
    'second_hz': ('Upper test tone (Hz)', 1, 2, 'Second frequency on the two-tone test track.'),
    'tolerance_us': ('Allowed delay (µs)', 0.5, 2, 'Set how far the corrected delay can be from zero.'),
    'offset_us': ('Interface delay offset (µs)', 0.1, 3, 'Right-minus-left delay measured with the same signal on both inputs.'),
    'level_dbfs': ('Generator peak level (dBFS)', 1, 1, 'Digital peak of the generated test signal.'),
    'distortion_limit': ('Maximum channel distortion (%)', 0.05, 2, 'Exclude settings that exceed this limit on either channel.'),
    'minimum_force': ('Minimum cartridge force (g)', 0.1, 2, 'Enter the lower limit specified for your cartridge.'),
    'maximum_force': ('Maximum cartridge force (g)', 0.1, 2, 'Enter the upper limit specified for your cartridge.'),
}
_TARGET = {
    ('phono-loading', 2): ('Expected level relative to reference (dB)', 0.25, 2,
                           'Use the relative recorded level specified by your test record.'),
    ('phono-gain', 2): ('Nominal reference level (dBFS RMS)', 1, 1,
                        'Set the reference level at the interface. Leave enough headroom for peaks to pass without clipping.'),
    ('phono-gain', 3): ('Minimum peak headroom (dB)', 1, 1,
                        'Headroom required above the highest sample peak on the loud groove.'),
    ('tape-bias', 3): ('Target drop past peak (dB)', 0.1, 2,
                       'Use the overbias drop specified for your tape formulation, deck and speed.'),
}


def _control(parent, widget, name, tip):
    widget.SetName(name)
    widget.SetAccessibleName(name)
    describe(widget, tip)
    return widget


def _caption(widget, label):
    widget.SetLabel(label)
    widget.SetName(label)
    widget.SetAccessibleName(label)


def _button(parent, label, tip, function, identifier=wx.ID_ANY):
    button = _control(parent, wx.Button(parent, identifier, label=label), label, tip)
    button.Bind(wx.EVT_BUTTON, lambda event: function())
    return button


def _label(parent, row, label, widget, tip, *, gap=0):
    caption = wx.StaticText(parent, label=label)
    describe(caption, tip)
    row.Add(caption, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, parent.FromDIP(gap))
    row.Add(_control(parent, widget, label, tip), 1, wx.EXPAND)
    return widget


class AlignmentOptionsDialog(wx.Dialog):
    def __init__(self, parent, options, defaults, workflow, index, step):
        super().__init__(parent, title='Advanced Alignment Settings')
        self.options, self.defaults, self.fields = options, defaults, {}
        names = ['seconds']
        if step.mode in ('tone', 'imd', 'azimuth'):
            names.append('tone_hz')
        if step.mode == 'imd':
            names += ['second_hz', 'distortion_limit', 'method']
        if step.mode == 'azimuth':
            names += ['tolerance_us', 'offset_us']
        if step.waveform:
            names.append('level_dbfs')
        if step.mode == 'noise':
            names.append('noise')
        if workflow in ('antiskate', 'tracking-force'):
            names.append('distortion_limit')
        if workflow == 'tracking-force':
            names += ['minimum_force', 'maximum_force']
        if (workflow, index) in _TARGET:
            names.append('target_db')
        grid = wx.FlexGridSizer(cols=2, vgap=self.FromDIP(9), hgap=self.FromDIP(10))
        grid.AddGrowableCol(1)
        for name in names:
            if name in ('method', 'noise'):
                label, choices, tip = (
                    ('Intermodulation products', ('Sidebands around upper tone', 'Difference and third-order products'),
                     'Select the set of products documented for the frequencies on your test record.') if name == 'method' else
                    ('Broadband stimulus', ('White noise', 'Pink noise'),
                     'Use the same spectrum and output level for the reference and tape traces.')
                )
                value = getattr(options, name)
                field = wx.Choice(self, choices=choices)
                field.SetSelection(int(value in ('difference', 'pink')))
            else:
                if name == 'target_db':
                    label, increment, digits, tip = _TARGET[(workflow, index)]
                    low, high = ALIGNMENT_TARGET_LIMITS[workflow, index]
                else:
                    label, increment, digits, tip = _FIELDS[name]
                    low, high = ALIGNMENT_OPTION_LIMITS.get(name, (-500000, 500000))
                field = wx.SpinCtrlDouble(self, min=low, max=high, initial=getattr(options, name), inc=increment)
                field.SetDigits(digits)
            self.fields[name] = _label(self, grid, label, field, tip)
        form = wx.BoxSizer(wx.VERTICAL)
        form.Add(grid, 0, wx.EXPAND | wx.ALL, self.FromDIP(16))
        restore = _button(self, 'Restore Defaults', 'Reset this step’s draft settings. Choose OK to save them.', self._restore)
        form.Add(restore, 0, wx.LEFT | wx.BOTTOM, self.FromDIP(12))
        form.Add(self.CreateSeparatedButtonSizer(wx.OK | wx.CANCEL), 0, wx.EXPAND | wx.ALL, self.FromDIP(12))
        self.SetSizerAndFit(form)
        self.SetMinSize(self.GetSize())
        self.CentreOnParent()
        self.Bind(wx.EVT_BUTTON, self._accept, id=wx.ID_OK)

    def _restore(self):
        for key, field in self.fields.items():
            value = getattr(self.defaults, key)
            if key in ('method', 'noise'):
                field.SetSelection(int(value in ('difference', 'pink')))
            else:
                field.SetValue(value)

    def _accept(self, _event):
        changes = {}
        for key, field in self.fields.items():
            if key == 'method':
                changes[key] = ('sidebands', 'difference')[field.GetSelection()]
            elif key == 'noise':
                changes[key] = ('white', 'pink')[field.GetSelection()]
            else:
                changes[key] = field.GetValue()
        candidate = replace(self.options, **changes)
        if candidate.minimum_force > candidate.maximum_force:
            wx.MessageBox('Maximum cartridge force must be at least the minimum.', 'Check Force Limits', wx.OK | wx.ICON_ERROR, self)
            return
        if self.fields.get('second_hz') and candidate.second_hz <= candidate.tone_hz:
            wx.MessageBox('The upper test tone must exceed the lower test tone.', 'Check Test Tones', wx.OK | wx.ICON_ERROR, self)
            return
        self.options = candidate
        self.EndModal(wx.ID_OK)


class AlignmentDialog(wx.Dialog):
    """Guide the measurement while leaving the number of readings up to the operator."""

    def __init__(self, parent, controller, workflow):
        self.workflow_id = workflow
        self.workflow = WORKFLOWS[workflow]
        super().__init__(parent, title=self.workflow.title, style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.controller = controller
        self.step_index = 0
        self.settings_by_step = {}
        self.trials = []
        self.history = deque(maxlen=120)
        self.bias_peak = None
        self.reference_curve = None
        self._reference_context = None
        self.listen_enabled = False
        self.test_started = False
        self._examiner = ThreadPoolExecutor(max_workers=1, thread_name_prefix='Alignment measurement')
        self._pending = None
        self._generation = 0
        self._input_context = None
        self._sequence = None
        self._first_frame_at = 0.0
        self._last_input_at = 0.0
        self._last_read = None
        self._acceptable = False
        self._last_status = ''
        self._selected_comparison = None
        self._comparison_keys = ()
        self._build()
        self._show_step()
        self.refresh_display_settings()
        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._poll, self.timer)
        self.Bind(wx.EVT_SHOW, self._visibility)
        self.Bind(wx.EVT_CLOSE, self._close_event)
        self.Bind(wx.EVT_CHAR_HOOK, self._key)

    @property
    def step(self):
        return self.workflow.instructions[self.step_index]

    @property
    def options(self):
        return self._options_for_step(self.step_index)

    def _options_for_step(self, index):
        saved = self.settings_by_step.get(index)
        if saved is not None:
            return replace(saved, offset_us=self.controller.settings.get('headAzimuthOffsetUs', 0))
        workflow = self.controller.settings.get('alignmentOptions', {}).get(self.workflow_id, {})
        return replace(self._default_options(index), **workflow.get(str(index), {}))

    def _default_options(self, index):
        step = self.workflow.instructions[index]
        interval = {'azimuth': 0.4, 'imd': 0.8, 'level': 1.2, 'noise': 1.5}
        targets = {('tape-bias', 3): 2, ('phono-gain', 2): -20, ('phono-gain', 3): 6}
        defaults = Options(seconds=interval.get(step.mode, 0.6), tone_hz=step.tone_hz or 1000,
                           offset_us=self.controller.settings.get('headAzimuthOffsetUs', 0),
                           level_dbfs=-30 if self.workflow_id == 'tape-bias' and index in (2, 3, 5) else -20,
                           target_db=targets.get((self.workflow_id, index), 0))
        # Paired tape steps must use the same stimulus before subtracting their
        # measured levels. Follow the source step unless explicitly overridden.
        if self.workflow_id == 'tape-bias' and index in (3, 4):
            reference = self._options_for_step(2 if index == 3 else 1)
            defaults = replace(defaults, tone_hz=reference.tone_hz, level_dbfs=reference.level_dbfs)
        return defaults

    def _build(self):
        size = self.FromDIP
        outer = wx.BoxSizer(wx.VERTICAL)
        self.heading = wx.StaticText(self, label='')
        font = self.heading.GetFont()
        font.MakeBold()
        font.SetPointSize(font.GetPointSize() + 3)
        self.heading.SetFont(font)
        describe(self.heading, 'The current step and its position in the assistant.')
        outer.Add(self.heading, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, size(14))
        self.instructions = _control(self, wx.TextCtrl(self, style=wx.TE_READONLY | wx.TE_MULTILINE,
                                     size=size((-1, 68))), 'Step instructions', 'Read or copy the instructions for this step.')
        outer.Add(self.instructions, 0, wx.EXPAND | wx.ALL, size(12))

        actions = wx.WrapSizer(wx.HORIZONTAL, flags=wx.REMOVE_LEADING_SPACES)
        self.live_button = _button(self, 'Start Live Input', 'Start capturing from the selected audio input.', self._start_audio)
        self.audio_button = _button(self, 'Audio I/O Settings…', 'Open device, channel and sample-rate settings.',
                                    lambda: self.controller.navigate('settings', 'audio'))
        self.advanced_button = _button(self, 'Advanced Settings…', 'Choose the test signals, limits and measurement interval.',
                                       self._advanced)
        self.signal_button = _button(self, 'Start Test Signal', 'Turn on the configured generator signal for this step.',
                                     self._toggle_signal)
        self.listen_button = _button(self, 'Listen to Left Minus Right', 'Hear the stereo difference while adjusting azimuth.',
                                     self._toggle_difference)
        self.reference_button = _button(self, 'Capture Reference', 'Set the reference used in this measurement step.',
                                        self._capture_reference)
        for button in (self.live_button, self.audio_button, self.advanced_button, self.signal_button, self.listen_button,
                       self.reference_button):
            actions.Add(button, 0, wx.RIGHT | wx.BOTTOM, size(6))
        outer.Add(actions, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, size(12))

        self.measurement = wx.Panel(self)
        tools = wx.BoxSizer(wx.VERTICAL)
        graph_row = wx.BoxSizer(wx.HORIZONTAL)
        self.view = _label(self.measurement, graph_row, 'Plot view', wx.Choice(self.measurement),
                           'Choose live history, XY or a comparison of captured readings.', gap=8)
        self.view.Bind(wx.EVT_CHOICE, lambda _event: self._change_graph())
        tools.Add(graph_row, 0, wx.EXPAND | wx.BOTTOM, size(3))
        self.comparison_row = wx.BoxSizer(wx.HORIZONTAL)
        self.comparison_choice = _label(self.measurement, self.comparison_row, 'Compared test', wx.Choice(self.measurement),
                                        'Choose which matching track, frequency and target to plot together.', gap=8)
        self.comparison_choice.Bind(wx.EVT_CHOICE, self._choose_comparison)
        tools.Add(self.comparison_row, 0, wx.EXPAND | wx.BOTTOM, size(3))
        self.plot = LinePlot(self.measurement, controls=False)
        tools.Add(self.plot, 1, wx.EXPAND | wx.BOTTOM, size(6))
        self.readout = _control(self.measurement, wx.TextCtrl(self.measurement, size=size((-1, 64)),
                                style=wx.TE_READONLY | wx.TE_MULTILINE), 'Alignment readings',
                                'Read the measurements, limits and test signal confidence for both channels. '
                                'The readout pauses while it has focus.')
        self.readout.Bind(wx.EVT_KILL_FOCUS, self._readout_blur)
        tools.Add(self.readout, 0, wx.EXPAND | wx.BOTTOM, size(8))

        self.entry_grid = wx.FlexGridSizer(cols=4, vgap=self.FromDIP(5), hgap=size(7))
        self.entry_grid.AddGrowableCol(1)
        self.entry_grid.AddGrowableCol(3)
        self.setting = _label(self.measurement, self.entry_grid, self.workflow.position, wx.TextCtrl(self.measurement),
                              'Name the physical setup for this capture. Use the same name for related steps.')
        self.setting.Bind(wx.EVT_TEXT, self._relabel_reading)
        self.track = _label(self.measurement, self.entry_grid, 'Test track label', wx.TextCtrl(self.measurement),
                            'Optionally name the test band, recorded level or source used for this measurement.')
        tools.Add(self.entry_grid, 0, wx.EXPAND | wx.BOTTOM, size(7))

        result_row = wx.WrapSizer(wx.HORIZONTAL, flags=wx.REMOVE_LEADING_SPACES)
        self.capture_button = _button(self.measurement, 'Capture Reading', 'Keep the current reading with the setup and track labels.',
                                      self._capture_trial)
        self.trials_button = _button(self.measurement, f'{CAPTURED_READINGS}…', 'Review, compare, delete or export readings.',
                                     self._show_trials)
        self.session_button = _button(self.measurement, 'Save to Session', 'Save a copy of the captured readings in Session Results.',
                                      self._save_session)
        for button in (self.capture_button, self.trials_button, self.session_button):
            result_row.Add(button, 0, wx.RIGHT | wx.BOTTOM, size(7))
        tools.Add(result_row, 0, wx.EXPAND)
        self.measurement.SetSizer(tools)
        outer.Add(self.measurement, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, size(12))
        self.welcome = wx.StaticText(self, label='When you have the equipment connected and the test track ready, choose Next.')
        describe(self.welcome, 'You can compare the readings you capture as you work through the steps.')
        outer.Add(self.welcome, 1, wx.EXPAND | wx.ALL, size(14))

        navigation = wx.BoxSizer(wx.HORIZONTAL)
        navigation.Add(_button(self, 'Help', 'Read the guide for this assistant.', self._show_help), 0, wx.RIGHT, size(6))
        navigation.Add(_button(self, 'Close', 'Close the assistant and stop its test output.', self._hide, wx.ID_CLOSE), 0)
        navigation.AddStretchSpacer()
        self.back = _button(self, 'Back', 'Return to the previous step.', lambda: self._move_step(-1))
        self.forward = _button(self, 'Next', 'Continue to the next step.', lambda: self._move_step(1))
        self.SetAffirmativeId(self.forward.GetId())
        for button in (self.back, self.forward):
            navigation.Add(button, 0, wx.LEFT, size(7))
        outer.Add(navigation, 0, wx.EXPAND | wx.ALL, size(12))
        self.SetSizer(outer)
        display = wx.GetClientDisplayRect()
        width, height = size((825, 770 if wx.Platform == '__WXGTK__' else 705))
        min_width, min_height = size((675, 630 if wx.Platform == '__WXGTK__' else 580))
        self.SetMinSize((min(min_width, display.width - 12), min(min_height, display.height - 35)))
        self.SetSize((min(width, display.width - 12), min(height, display.height - 35)))
        self.CentreOnParent() if self.GetParent() else self.CentreOnScreen()

    def _readout_blur(self, event):
        event.Skip()
        self.readout.ChangeValue(self._last_status)

    def _relabel_reading(self, event):
        event.Skip()
        if self._last_read is not None:
            self.history.clear()
            self._present(self._last_read, append=False)
        if self.view.GetStringSelection() == 'Loading Response':
            self._plot_loading_response()

    def _key(self, event):
        if event.GetKeyCode() == wx.WXK_ESCAPE:
            self._hide()
        elif event.GetKeyCode() == wx.WXK_F1:
            self._show_help()
        else:
            event.Skip()

    def keyboard_actions(self):
        return (('Read Alignment Values', lambda: self.controller._show_text('Alignment readings', self._last_status, parent=self),
                 'Open a stable copy of the live readout.'),)

    def _show_help(self):
        self.controller._show_guide(f'{MANUAL}#{self.workflow.help_anchor}')

    def refresh_display_settings(self):
        self.plot.set_appearance(self.controller.settings)
        self.plot.set_expanded_descriptions(self.controller.settings.get('useAccessibility', True))
        if wx.Platform == '__WXMSW__':
            for field in (self.instructions, self.readout):
                field.SetBackgroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOW))
                field.SetForegroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOWTEXT))
                field.Refresh(False)

    def _move_step(self, delta):
        target = self.step_index + delta
        if 0 <= target < len(self.workflow.instructions):
            self._stop_test()
            if self.listen_enabled and self.workflow.instructions[target].mode != 'azimuth':
                self._set_difference(False)
            self.step_index = target
            self._show_step()
            self.instructions.SetFocus()
        elif delta > 0:
            self._show_trials()

    def _reference_label(self):
        if self.step.mode == 'azimuth':
            return 'Capture Interface Offset…'
        if self.step.mode == 'noise':
            return 'Use Current Curve as Reference'
        if self.workflow_id == 'tape-bias' and self.step_index == 2:
            return 'Use Current Level as Bias Peak'
        return ''

    def _show_step(self):
        self.heading.SetLabel(f'Step {self.step_index + 1} of {len(self.workflow.instructions)}  •  {self.step.name}')
        self.instructions.ChangeValue(self.step.instructions)
        guide = self.step.mode == 'guide'
        self.measurement.Show(not guide)
        self.welcome.Show(guide)
        self.advanced_button.Show(not guide)
        self.signal_button.Show(bool(self.step.waveform))
        self.listen_button.Show(self.step.mode == 'azimuth')
        reference = self._reference_label()
        self.reference_button.Show(bool(reference))
        if reference:
            _caption(self.reference_button, reference)
        self.back.Enable(self.step_index > 0)
        _caption(self.forward, 'Review Readings…' if self.step_index == len(self.workflow.instructions) - 1 else 'Next')
        self.forward.SetDefault()
        labels = ['Live History', CAPTURED_READINGS]
        if self.step.mode == 'azimuth':
            labels.insert(0, 'Stereo XY')
        elif self.step.mode == 'noise':
            labels.insert(0, 'Band Levels')
        elif self.workflow_id == 'phono-loading' and self.step_index == 2:
            labels.append('Loading Response')
        self.view.Set(labels)
        self.view.SetSelection(0)
        self._selected_comparison = None
        self._invalidate()
        self.Layout()

    def _invalidate(self):
        self._generation += 1
        self._analysis_options = self.options
        self._first_frame_at = time.monotonic() + self._analysis_options.seconds
        self._last_read = None
        self._acceptable = False
        self.history.clear()
        self._configure_plot()
        if self.view.GetStringSelection() in (CAPTURED_READINGS, 'Loading Response'):
            self._update_plot()
        self._button_state()
        self._status('Waiting for fresh audio for this step.' if self.step.mode != 'guide' else self.step.instructions)

    def _change_graph(self):
        self._configure_plot()
        self._update_plot()

    def _visibility(self, event):
        if event.IsShown():
            self._invalidate()
            self.timer.Start(160)
        else:
            self.timer.Stop()
        event.Skip()

    def _close_event(self, event):
        if event.CanVeto():
            event.Veto()
            self._hide()
        else:
            self.shutdown()
            event.Skip()

    def _hide(self):
        self._stop_test()
        if self.listen_enabled:
            self._set_difference(False)
        self.Hide()

    def shutdown(self):
        self._stop_test()
        if self.listen_enabled:
            self._set_difference(False)
        self.timer.Stop()
        self._examiner.shutdown(wait=False, cancel_futures=True)

    def _advanced(self):
        defaults = self._default_options(self.step_index)
        with AlignmentOptionsDialog(self, self.options, defaults, self.workflow_id, self.step_index, self.step) as dialog:
            if dialog.ShowModal() == wx.ID_OK:
                self._stop_test()
                stored = dict(self.controller.settings.get('alignmentOptions', {}))
                workflow = dict(stored.get(self.workflow_id, {}))
                workflow[str(self.step_index)] = {
                    key: value for key, value in asdict(dialog.options).items()
                    if key != 'offset_us' and value != getattr(defaults, key)
                }
                stored[self.workflow_id] = workflow
                self.settings_by_step.pop(self.step_index, None)
                changes = {'alignmentOptions': normalize_alignment_options(stored)}
                if self.step.mode == 'azimuth':
                    changes['headAzimuthOffsetUs'] = dialog.options.offset_us
                self.controller.apply_settings(**changes)
                self._invalidate()

    def _start_audio(self):
        if not self or not self.IsShown():
            return
        if self.controller.settings.get('channels') != 2 and not self.controller.apply_settings(channels=2):
            return
        if self.controller.ensure_audio(after_permission=self._start_audio, require_physical=True):
            self._invalidate()

    def _toggle_signal(self):
        if self.test_started:
            self._stop_test()
            return
        options = self.options
        if self.controller.start_lab_signal(options.noise if self.step.mode == 'noise' else self.step.waveform,
                                            frequency=options.tone_hz, level=10 ** (options.level_dbfs / 20), owner=self):
            self.test_started = True
            _caption(self.signal_button, 'Stop Test Signal')
            self._invalidate()

    def _stop_test(self):
        if self.test_started:
            self.controller.stop_lab_signal(self)
            self.test_started = False
            _caption(self.signal_button, 'Start Test Signal')

    def _set_difference(self, enabled):
        if self.controller.set_lab_difference(self, enabled):
            self.listen_enabled = enabled
            _caption(self.listen_button, 'Stop Difference Monitoring' if enabled else 'Listen to Left Minus Right')

    def _toggle_difference(self):
        if not self or not self.IsShown():
            return
        if self.controller.settings.get('channels') != 2 and not self.controller.apply_settings(channels=2):
            return
        generation = self._generation

        def resume_monitoring():
            if self and self.IsShown() and self._generation == generation and not self.listen_enabled:
                self._toggle_difference()

        if (not self.listen_enabled and not self.controller.audio.input_enabled
                and not self.controller.ensure_audio(after_permission=resume_monitoring, require_physical=True)):
            return
        self._set_difference(not self.listen_enabled)

    def _input_state(self):
        audio = self.controller.audio
        clock = self.controller.settings.get('correctionFactor', 1.0) if audio.input_enabled else 1.0
        physical_rate, _, sequence, epoch = audio.input_observation(0)
        return (self.controller.audio_input_identity(), physical_rate, clock, epoch), sequence

    def _poll(self, _event=None):
        if self.step.mode == 'guide':
            return
        options = self.options
        context, sequence = self._input_state()
        if context != self._input_context or options != self._analysis_options:
            self._input_context = context
            self._invalidate()
        if self._pending is not None:
            generation, work = self._pending
            if not work.done():
                return
            self._pending = None
            if generation == self._generation:
                try:
                    self._last_read = work.result()
                except (ValueError, FloatingPointError, np.linalg.LinAlgError) as error:
                    self._last_read = None
                    self._acceptable = False
                    self._status(str(error))
                else:
                    self._present(self._last_read)
                self._button_state()
        now = time.monotonic()
        if sequence == self._sequence:
            if self._last_read is not None and now - self._last_input_at > max(0.8, options.seconds * 1.5):
                self._last_read = None
                self._acceptable = False
                self._button_state()
                self._status('Live audio has stopped. Start the input to resume measuring.')
            return
        _, physical_rate, clock, epoch = context
        if now < self._first_frame_at or physical_rate <= 0:
            return
        seconds = options.seconds * clock
        source_rate, samples, sequence, source_epoch = self.controller.audio.input_observation(seconds)
        if source_rate != physical_rate or source_epoch != epoch:
            return
        rate = physical_rate * clock
        self._sequence = sequence
        if len(samples) < np.ceil(physical_rate * seconds):
            return
        if samples.ndim != 2 or samples.shape[1] != 2:
            self._status('Select a stereo input in Audio I/O, then start the input.')
            self._last_read = None
            self._acceptable = False
            self._button_state()
            return
        self._last_input_at = now
        self._pending = self._generation, self._examiner.submit(_analyse, samples, rate, self.step.mode, options)

    def _status(self, message):
        if message != self._last_status:
            self._last_status = message
            if not self.readout.HasFocus():
                self.readout.ChangeValue(message)

    def _button_state(self):
        ready = self._last_read is not None
        self.capture_button.Enable(ready and self._acceptable)
        self.reference_button.Enable(ready)
        self.trials_button.Enable(bool(self.trials))
        self.session_button.Enable(bool(self.trials))

    def _related(self, step, setting, numeric_field, *, reference_hz=None):
        route = self._route_key()
        matches = [trial for trial in self.trials if trial.step == step and trial.setting.casefold() == setting.casefold()
                   and trial.input_route == route
                   and (reference_hz is None or trial.nominal_hz == reference_hz)]
        if not matches:
            return None
        return np.median([getattr(trial, numeric_field) for trial in matches], axis=0)

    def _capture_reference(self):
        if self._last_read is None or not self._input_is_current():
            return
        reading = self._last_read.metrics
        if self.step.mode == 'azimuth':
            if not self._valid_azimuth(reading):
                self.controller.announce('A clean, steady stereo tone is needed to capture an offset.')
                return
            question = ('Are both interface inputs receiving the same reference signal directly? '
                        'Use this only to remove an interface offset. Align the tape head using the compensated result.')
            if wx.MessageBox(question, 'Capture Interface Offset', wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, self) != wx.YES:
                return
            if not self._input_is_current():
                return
            if self.controller.apply_settings(headAzimuthOffsetUs=reading.delay_us):
                self.controller.announce(f'Interface offset saved at {reading.delay_us:+.2f} microseconds.')
        elif self.step.mode == 'noise':
            if min(channel[0] for channel in reading) < -70 or max(channel[1] for channel in reading) > -0.05:
                self.controller.announce('A clear broadband reference without clipping is needed.')
                return
            self.reference_curve = tuple(np.copy(column) for column in self._last_read.curve)
            self._reference_context = self._broadband_context()
            self.controller.announce('Broadband reference captured. Switch to tape monitoring for the comparison.')
        else:
            if not self._valid_tone(reading):
                self.controller.announce('A clean stereo bias test tone is needed to capture its peak.')
                return
            self.bias_peak = (self.options.tone_hz, self.options.level_dbfs, self._route_key(),
                              tuple(channel.tone_dbfs for channel in reading.channels))
            self.controller.announce('Saved the current levels as the bias peak.')
        self._present(self._last_read, append=False)

    @staticmethod
    def _valid_tone(tone):
        return all(ch.tone_dbfs > -76 and ch.confidence_db > 12 and ch.peak_dbfs < -0.04 for ch in tone.channels)

    @staticmethod
    def _valid_azimuth(reading):
        return bool(min(reading.tone_levels) > -76 and min(reading.confidence_db) > 10
                    and reading.coherence >= 0.90 and not reading.clipped)

    def _compare_tone(self, reading, setting, track, options):
        left, right = reading.channels
        tone_levels = left.tone_dbfs, right.tone_dbfs
        peaks = left.peak_dbfs, right.peak_dbfs
        workflow, step = self.workflow_id, self.step_index
        if workflow in ('antiskate', 'tracking-force'):
            l, r = left.thdn_percent, right.thdn_percent
            balance = workflow == 'antiskate'
            score = compare_distortion(l, r, options.distortion_limit, balance=balance)
            assessment = ('Distortion is within the limit.' if score is not None else 'Distortion exceeds the limit.')
            summary = (f'Left {l:.3f}%, right {r:.3f}% THD+N. Left minus right {l - r:+.3f} percentage points.\n'
                       f'Worst channel {max(l, r):.3f}%. {assessment}')
            return 'THD+N (%)', l, r, score, summary
        if workflow == 'phono-loading' and step == 2:
            # Reference levels are kept per exact named load. Combining baselines
            # from different gain paths would create a false response difference.
            reference_hz = self._options_for_step(1).tone_hz
            reference = self._related(self.workflow.instructions[1].name, setting, 'left', reference_hz=reference_hz)
            if reference is None:
                return 'Tone (dBFS RMS)', *tone_levels, None, 'First capture the midband reference using this load label.'
            right_reference = self._related(self.workflow.instructions[1].name, setting, 'right', reference_hz=reference_hz)
            l, r = left.tone_dbfs - reference, right.tone_dbfs - right_reference
            score = max(abs(l - options.target_db), abs(r - options.target_db))
            return ('Relative response (dB)', l, r, score,
                    (f'Left {l:+.2f} dB, right {r:+.2f} dB relative to the reference. '
                    f'Target {options.target_db:+.2f} dB. Largest error {score:.2f} dB.'))
        if workflow == 'phono-gain' and step == 2:
            reference = self._related(self.workflow.instructions[1].name, setting, 'left')
            right_reference = self._related(self.workflow.instructions[1].name, setting, 'right')
            summary = (f'Reference tone left {left.tone_dbfs:.2f}, right {right.tone_dbfs:.2f} dBFS RMS. '
                       f'Target {options.target_db:g} dBFS. Channel difference {left.tone_dbfs - right.tone_dbfs:+.2f} dB.')
            if reference is not None:
                summary += (f'\nRelative signal-to-noise left {left.tone_dbfs - reference:.1f}, '
                            f'right {right.tone_dbfs - right_reference:.1f} dB.')
            score = max(abs(level - options.target_db) for level in tone_levels)
            return 'Tone (dBFS RMS)', *tone_levels, score, summary
        if workflow == 'tape-bias' and step == 3:
            peak_levels = self._bias_levels(options)
            if peak_levels is None:
                return 'Tone (dBFS RMS)', *tone_levels, None, 'Capture the bias peak using the same test frequency first.'
            l, r = (peak - current for peak, current in zip(peak_levels, tone_levels, strict=True))
            score = max(abs(value - options.target_db) for value in (l, r))
            return ('Drop from bias peak (dB)', l, r, score,
                    (f'Drop from peak left {l:+.2f} dB, right {r:+.2f} dB. Target {options.target_db:g} dB. '\
                    'Approach this reading on the higher-bias side of the peak.'))
        if workflow == 'tape-bias' and step == 4:
            baseline = [trial for trial in self.trials if trial.step == self.workflow.instructions[1].name and
                        trial.nominal_hz == options.tone_hz and trial.generator_level_dbfs == options.level_dbfs
                        and trial.input_route == self._route_key()]
            if baseline:
                # Most recent source reference wins so undoing a source-level
                # adjustment does not average the preceding calibration.
                l, r = left.tone_dbfs - baseline[-1].left, right.tone_dbfs - baseline[-1].right
                return ('Record-level offset (dB)', l, r, max(abs(l), abs(r)),
                        f'Tape return relative to source: left {l:+.2f} dB, right {r:+.2f} dB. Target 0 dB.')
            return 'Tone (dBFS RMS)', *tone_levels, None, 'Capture the same test frequency in the record-level reference step first.'
        return ('Tone (dBFS RMS)', *tone_levels, None,
                (f'Left {left.tone_dbfs:.2f}, right {right.tone_dbfs:.2f} dBFS RMS. '
                f'Channel difference {left.tone_dbfs - right.tone_dbfs:+.2f} dB. '\
                f'Peak left {peaks[0]:.1f}, right {peaks[1]:.1f} dBFS.'))

    def _bias_levels(self, options):
        reference = options.tone_hz, options.level_dbfs, self._route_key()
        if self.bias_peak is not None and self.bias_peak[:3] == reference:
            return self.bias_peak[3]
        matches = [trial for trial in self.trials if trial.step == self.workflow.instructions[2].name and
                   (trial.nominal_hz, trial.generator_level_dbfs, trial.input_route) == reference]
        if matches:
            return max(trial.left for trial in matches), max(trial.right for trial in matches)
        return None

    def _route_key(self):
        # A stream restart invalidates pending work, but a previous reference
        # can still be compared after restarting the same hardware mode.
        return self._input_context[:3] if self._input_context is not None else None

    def _input_is_current(self):
        if self._input_context is None or (
                self._input_state()[0] == self._input_context and self.options == self._analysis_options):
            return True
        self._invalidate()
        self.controller.announce('The input changed. Wait for a fresh reading.')
        return False

    def _broadband_context(self):
        options = self.options
        return self._route_key(), options.noise, options.level_dbfs

    def _has_broadband_reference(self, frequency):
        return (self.reference_curve is not None and self._reference_context == self._broadband_context()
                and np.array_equal(self.reference_curve[0], frequency))

    def _noise_curves(self, reading):
        frequency, *curves = reading.curve
        if self._has_broadband_reference(frequency):
            curves = [curve - reference for curve, reference in zip(curves, self.reference_curve[1:], strict=True)]
        return (frequency, *curves)

    def _trial(self, reading):
        options = self.options
        step = self.step
        setting, track = self.setting.GetValue().strip(), self.track.GetValue().strip()
        frequency, peaks, curves, curve_unit = 0.0, (), (), ''
        metric, score, left, right = '', None, 0.0, 0.0
        delay_margin = delay_offset = verified_delay = verification_hz = None
        delay_input = ''
        values = reading.metrics
        if step.mode == 'tone':
            frequency = values.frequency
            peaks = tuple(ch.peak_dbfs for ch in values.channels)
            metric, left, right, score, summary = self._compare_tone(values, setting, track, options)
            harmonics = tuple(ch.hd3_percent for ch in values.channels)
            if all(harmonic is not None for harmonic in harmonics):
                summary += f'\nThird harmonic left {harmonics[0]:.3f}%, right {harmonics[1]:.3f}%.'
            summary += f' Test tone {frequency:,.2f} Hz.'
            valid = self._valid_tone(values)
            if not valid:
                summary = 'Waiting for a clear, unclipped test tone in both channels.\n' + summary
        elif step.mode == 'imd':
            product, peak_levels = values
            peaks = tuple(level[1] for level in peak_levels)
            left, right = product.distortion_percent
            metric, score, frequency = 'Two-tone IMD (%)', compare_distortion(left, right, options.distortion_limit), sum(product.frequencies)
            valid = max(peaks) < -0.04
            summary = (f'Left {left:.3f}%, right {right:.3f}% IMD. '
                       f'Tones {product.frequencies[0]:,.1f} and {product.frequencies[1]:,.1f} Hz.\n')
            summary += 'Products ' + ', '.join(f'{product_hz:,.1f} Hz' for product_hz in product.products) + '.'
        elif step.mode == 'azimuth':
            frequency = values.frequency
            left = unwrap_delay(values.delay_us, options.offset_us, values.frequency)
            right = values.delay_us
            metric, score = 'Delay (µs, corrected and raw)', abs(left)
            valid = self._valid_azimuth(values)
            phase = left * values.frequency * 360 / 1e6
            delay_margin = max(3 * values.jitter_us, 0.1)
            delay_offset = options.offset_us
            delay_input = str(self.controller.audio_input_identity() or '')
            half_period = 5e5 / frequency
            if abs(left) - delay_margin > options.tolerance_us:
                tolerance_status = 'Outside tolerance in this cycle'
            elif abs(left) + delay_margin <= options.tolerance_us:
                tolerance_status = 'Within tolerance in this cycle'
            else:
                tolerance_status = 'Reading overlaps the tolerance boundary'
            summary = (f'Right-channel delay {left:+.2f} µs ({phase:+.1f}°). Raw {right:+.2f} µs. '
                       f'Tolerance ±{options.tolerance_us:g} µs. {tolerance_status}. '
                       f'Single-tone range ±{half_period:.2f} µs.\n'
                       f'Tone {frequency:,.2f} Hz. Phase stability {values.coherence * 100:.1f}%, '
                       f'variation {values.jitter_us:.2f} µs. Left {values.tone_levels[0]:.1f}, '
                       f'right {values.tone_levels[1]:.1f} dBFS RMS.')
            if self.step_index == 2 and valid:
                fine = next((item for item in reversed(self.trials)
                             if item.step == self.workflow.instructions[1].name and item.delay_margin_us is not None
                             and item.setting.casefold() == setting.casefold() and item.delay_offset_us == delay_offset
                             and item.delay_input == delay_input and item.input_route == self._route_key()
                             and item.tone_frequency > frequency), None)
                if fine is None:
                    summary += '\nUse the same position label and offset as the higher-frequency reading.'
                else:
                    verified_delay = resolve_azimuth_delay(fine.left, fine.tone_frequency, fine.delay_margin_us,
                                                           left, frequency, delay_margin)
                    if verified_delay is None:
                        summary += '\nThe two tones do not identify a consistent delay cycle.'
                    else:
                        verification_hz = fine.tone_frequency
                        limit = options.tolerance_us
                        outcome = ('within tolerance' if abs(verified_delay) + fine.delay_margin_us <= limit else
                                   'outside tolerance' if abs(verified_delay) - fine.delay_margin_us > limit else
                                   'overlaps the tolerance boundary')
                        summary += (f'\nVerified high-frequency delay {verified_delay:+.2f} µs, {outcome}. '
                                    f'Assuming a delay within ±{half_period:.2f} µs.')
            if not valid:
                summary = 'Waiting for a stable, unclipped test tone in both channels.\n' + summary
        else:
            peaks = tuple(value[1] for value in values)
            left, right = (value[0] for value in values)
            metric, valid = 'AC level (dBFS RMS)', max(peaks) < -0.04
            summary = f'Left {left:.2f}, right {right:.2f} dBFS RMS. Peak left {peaks[0]:.2f}, right {peaks[1]:.2f} dBFS.'
            if self.workflow_id == 'phono-gain' and self.step_index == 3:
                left, right = -peaks[0], -peaks[1]
                metric, score = 'Peak headroom (dB)', None
                valid = min(left, right) > 0.04
                summary += f'\nPeak headroom left {left:.2f}, right {right:.2f} dB. Target at least {options.target_db:g} dB.'
                quiet = self._related(self.workflow.instructions[1].name, setting, 'left')
                nominal = self._related(self.workflow.instructions[2].name, setting, 'left')
                if quiet is not None and nominal is not None:
                    quiet_right = self._related(self.workflow.instructions[1].name, setting, 'right')
                    nominal_right = self._related(self.workflow.instructions[2].name, setting, 'right')
                    snr = nominal - quiet, nominal_right - quiet_right
                    summary += f'\nReference SNR left {snr[0]:.1f}, right {snr[1]:.1f} dB.'
                    if min(left, right) >= options.target_db:
                        # Rank by the lower channel SNR, after meeting the
                        # user's peak-headroom requirement on both channels.
                        score = -min(snr)
                        summary += ' Headroom target met.'
                if min(left, right) < options.target_db:
                    summary += ' Below the headroom target.'
            if step.mode == 'noise':
                curves = tuple(column.tolist() for column in self._noise_curves(reading))
                valid = valid and min(left, right) > -76
                referenced = self._has_broadband_reference(reading.curve[0])
                curve_unit = 'Relative level (dB)' if referenced else 'Digital band level (dBFS RMS)'
                summary += '\n' + ('The plot shows band levels relative to the captured reference.' if referenced else
                                    'The plot shows digital band levels. Capture a reference with this signal and audio route.')
        if peaks and max(peaks) >= -0.04:
            summary = 'Possible clipping. Reduce the input or generator level.\n' + summary
        # Group only like-for-like readings. A change in frequency, stimulus or
        # reference starts a new comparison even if the user keeps the label.
        test = f'{track} · {options.tone_hz:g} Hz' if frequency else track or step.name
        if step.mode == 'imd':
            test += f' + {options.second_hz:g} Hz ({options.method})'
        elif step.mode == 'noise':
            test += f' · {options.noise} · {options.level_dbfs:g} dBFS'
        if (self.workflow_id, self.step_index) in _TARGET:
            test += f' · target {options.target_db:g}'
        reference_hz = 0.0
        if metric == 'Relative response (dB)':
            reference_hz = self._options_for_step(1).tone_hz
            test += f' · reference {reference_hz:g} Hz'
        tolerance_us = options.tolerance_us if step.mode == 'azimuth' else None
        if tolerance_us is not None:
            test += f' · tolerance ±{tolerance_us:g} µs · offset {options.offset_us:+g} µs'
        generator_level = options.level_dbfs if step.waveform else None
        if generator_level is not None and step.mode != 'noise':
            test += f' · {generator_level:g} dBFS'
        target = options.target_db if (self.workflow_id, self.step_index) in _TARGET else None
        if metric == 'Record-level offset (dB)':
            target = 0.0
        return Trial(step.name, setting, track, metric, left, right, score, summary, peaks, frequency, test,
                     curves, curve_unit, options.tone_hz if frequency else 0, target,
                     reference_hz, tolerance_us, generator_level, delay_margin, delay_offset,
                     delay_input, verified_delay, verification_hz, self._route_key()), valid

    def _present(self, reading, *, append=True):
        trial, valid = self._trial(reading)
        self._acceptable = valid
        self._status(trial.details)
        if append and valid:
            self.history.append((time.monotonic(), trial.left, trial.right))
        if self.view.GetStringSelection() not in (CAPTURED_READINGS, 'Loading Response'):
            self._update_plot()

    def _configure_plot(self):
        view = self.view.GetStringSelection()
        self.plot.x_formatter = None
        self.plot.x_ticks = None
        self.plot.log_x = view in ('Band Levels', 'Loading Response')
        self.plot.square_axes = view == 'Stereo XY'
        self.plot.clear()
        self.plot.set_default_view()
        self.measurement.GetSizer().Show(self.comparison_row, False, recursive=True)
        self.measurement.Layout()
        if view == 'Stereo XY':
            self.plot.set_labels('Stereo alignment', 'Left input', 'Right input')
        elif view == 'Band Levels':
            self.plot.set_labels('Broadband comparison', 'Frequency (Hz)', 'Level (dBFS RMS)')
        elif view == 'Loading Response':
            self.plot.set_labels('Loading response for the entered setting', 'Frequency (Hz)', 'Level (dB)')
        elif view == CAPTURED_READINGS:
            self.plot.set_labels('Captured comparison', self.workflow.position, 'Reading in current units')
        else:
            self.plot.set_labels('Live measurement', 'Time (s)', 'Delay (µs)' if self.step.mode == 'azimuth' else 'Reading in current units')
        self.plot.fit()

    @staticmethod
    def _traces(x, left, right, labels=('Left', 'Right')):
        return [{'label': label, 'x': x, 'y': values} for label, values in zip(labels, (left, right), strict=True)]

    def _update_plot(self):
        view, reading = self.view.GetStringSelection(), self._last_read
        if view == 'Loading Response':
            self._plot_loading_response()
            return
        if view == CAPTURED_READINGS:
            self._plot_captured_comparison()
            return
        if reading is None:
            return
        if view == 'Stereo XY':
            left, right = reading.curve
            limit = float(max(0.01, np.max(np.abs((left, right))) * 1.15))
            self.plot.set_default_view((-limit, limit), (-limit, limit))
            self.plot.set_series([{'label': 'Input', 'x': left, 'y': right},
                                  {'label': 'In phase', 'x': (-limit, limit), 'y': (-limit, limit), 'style': 'dashed'}])
        elif view == 'Band Levels':
            bins, left, right = self._noise_curves(reading)
            corrected = self._has_broadband_reference(bins)
            label = 'Relative level (dB)' if corrected else 'Level (dBFS RMS)'
            if self.plot.y_label != label:
                self.plot.set_labels(y_label=label)
            self.plot.set_series(self._traces(bins, left, right))
        elif self.history:
            unit = self._axis_label(self._trial(reading)[0].metric)
            if self.plot.y_label != unit:
                self.plot.set_labels(y_label=unit)
            timestamp, left, right = np.asarray(self.history).T
            elapsed = timestamp - timestamp[0]
            traces = self._traces(elapsed, left, right, self._trace_labels())
            if self.step.mode == 'azimuth':
                tolerance = self.options.tolerance_us
                traces += [{'label': label, 'x': elapsed, 'y': np.full(len(elapsed), value), 'style': 'dashed'}
                           for label, value in (('Lower allowed limit', -tolerance), ('Upper allowed limit', tolerance))]
            self.plot.set_series(traces)

    def _choose_comparison(self, _event):
        index = self.comparison_choice.GetSelection()
        if index != wx.NOT_FOUND:
            self._selected_comparison = self._comparison_keys[index]
            self._plot_captured_comparison()

    def _plot_captured_comparison(self):
        groups = {}
        latest = None
        for trial in self.trials:
            if trial.step == self.step.name:
                latest = (trial.metric, trial.comparison, trial.input_route)
                groups.setdefault(latest, []).append(trial)
        keys = tuple(groups)
        if self.comparison_choice.GetCount() != len(keys) or keys != self._comparison_keys:
            name_counts = Counter(key[:2] for key in keys)
            captions = []
            for metric, label, route in keys:
                caption = f'{label} ({metric})'
                if route is not None and name_counts[metric, label] > 1:
                    caption += f' · {route[0]} · {route[1]:g} Hz × {route[2]:g}'
                captions.append(caption)
            self.comparison_choice.Set(captions)
        self._comparison_keys = keys
        self.measurement.GetSizer().Show(self.comparison_row, len(keys) > 1, recursive=True)
        self.measurement.Layout()
        if not keys:
            self.plot.x_ticks = None
            self.plot.clear()
            return
        key = self._selected_comparison if self._selected_comparison in groups else latest
        self.comparison_choice.SetSelection(keys.index(key))
        trials = groups[key]
        settings, labels, left, right = _captured_medians(trials)

        def setting_label(value):
            index = int(value)
            return labels[index - 1] if value == index and 1 <= index <= len(labels) else f'{value:g}'

        self.plot.x_formatter = setting_label
        positions = np.arange(1, len(labels) + 1)
        self.plot.x_ticks = tuple(enumerate(settings, start=1))
        traces = self._traces(positions, left, right, self._trace_labels())
        if len(positions) == 1:
            for trace in traces:
                trace['style'] = 'points'
        trial = trials[0]
        if trial.tolerance_us is not None:
            targets = (('Lower tolerance', -trial.tolerance_us), ('Upper tolerance', trial.tolerance_us))
        elif trial.target_db is not None:
            label = 'Minimum headroom' if trial.metric == 'Peak headroom (dB)' else 'Target'
            targets = ((label, trial.target_db),)
        else:
            targets = ()
        target_positions = (0.75, 1.25) if len(positions) == 1 else positions
        for label, value in targets:
            traces.append({'label': label, 'x': target_positions,
                           'y': np.full(len(target_positions), value), 'style': 'dashed'})
        caption = 'Captured comparison' if len(labels) == len(trials) else f'Captured comparison · {len(trials)} readings'
        self.plot.set_labels(title=caption, y_label=self._axis_label(trial.metric))
        self.plot.set_series(traces, fit=True)

    def _trace_labels(self):
        return ('Compensated delay', 'Raw delay') if self.step.mode == 'azimuth' else ('Left', 'Right')

    @staticmethod
    def _axis_label(metric):
        return {'Drop from bias peak (dB)': 'Drop (dB)', 'Delay (µs, corrected and raw)': 'Delay (µs)',
                'Relative response (dB)': 'Level (dB)'}.get(metric, metric)

    def _plot_loading_response(self):
        setting = self.setting.GetValue().strip().casefold()
        groups, latest = {}, {}
        reference_hz = self._options_for_step(1).tone_hz
        for trial in self.trials:
            if trial.setting.casefold() != setting or not trial.nominal_hz or trial.input_route != self._route_key():
                continue
            if trial.step == self.workflow.instructions[1].name and trial.nominal_hz == reference_hz:
                row = (0, 0, 0)  # The reference groove defines the relative zero.
            elif trial.metric == 'Relative response (dB)' and trial.target_db is not None and trial.reference_hz == reference_hz:
                row = (trial.left, trial.right, trial.target_db)
            else:
                continue
            # Prefer the latest test band at each frequency and combine only
            # its repeats, including readings taken before a different band.
            latest[trial.nominal_hz] = trial.comparison
            groups.setdefault((trial.nominal_hz, trial.comparison), []).append(row)
        if not latest:
            self.plot.clear()
            return
        frequencies = sorted(latest)
        left, right, target = np.asarray([np.median(groups[(frequency, latest[frequency])], axis=0)
                                          for frequency in frequencies]).T
        traces = self._traces(frequencies, left, right)
        traces.append({'label': 'Target', 'x': frequencies, 'y': target, 'style': 'dashed'})
        self.plot.set_series(traces)

    def _capture_trial(self):
        if self._last_read is None or not self._input_is_current():
            return
        trial, valid = self._trial(self._last_read)
        if not valid:
            self.controller.announce('Wait for a stable reading before capturing it.')
            return
        if not trial.setting:
            self.setting.SetFocus()
            self.controller.announce('Enter a setting label for this reading.')
            return
        if self.workflow_id == 'tracking-force':
            try:
                force = float(trial.setting)
            except ValueError:
                force = float('nan')
            if not self.options.minimum_force <= force <= self.options.maximum_force:
                self.setting.SetFocus()
                self.controller.announce(f'Enter a measured force from {self.options.minimum_force:g} to '
                                         f'{self.options.maximum_force:g} grams.')
                return
        if self.workflow_id == 'phono-loading' and self.step_index == 2 and trial.metric != 'Relative response (dB)':
            self.controller.announce('Capture the reference tone for this load before recording a comparison.')
            return
        self.trials.append(trial)
        self._button_state()
        self._update_plot()
        self.controller.announce(f'Captured reading {len(self.trials)} for {trial.setting}.', speak=True)

    def _show_trials(self):
        with TrialDialog(self, self.controller, self.trials, self.workflow.title) as dialog:
            dialog.ShowModal()
        self._button_state()
        self._update_plot()

    def _save_session(self):
        if self.trials:
            self.controller.log_measurement('alignment', {'title': self.workflow.title, 'workflow': self.workflow_id,
                                                           'trials': [asdict(trial) for trial in self.trials]})
            self.controller.announce('Alignment readings saved to Session Results.', speak=True)


class TrialDialog(wx.Dialog):
    """Review captured readings before saving or resuming the live workflow."""

    def __init__(self, parent, controller, trials, title):
        super().__init__(parent, title=f'{title} Readings', style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.controller, self.trials = controller, trials
        layout = wx.BoxSizer(wx.VERTICAL)
        self.table = _control(self, dv.DataViewListCtrl(self, style=dv.DV_SINGLE | dv.DV_ROW_LINES), 'Captured readings',
                              'Select a reading to see its values and compare it with readings from the same test.')
        for label, width in (('Step', 160), ('Setting', 115), ('Signal', 120), ('Measurement', 135),
                             ('Left or corrected', 108), ('Right or raw', 95)):
            self.table.AppendTextColumn(label, width=self.FromDIP(width), flags=dv.DATAVIEW_COL_RESIZABLE)
        self.table.Bind(dv.EVT_DATAVIEW_SELECTION_CHANGED, self._selection)
        layout.Add(self.table, 1, wx.EXPAND | wx.ALL, self.FromDIP(12))
        self.details = _control(self, wx.TextCtrl(self, size=self.FromDIP((-1, 115)),
                                style=wx.TE_MULTILINE | wx.TE_READONLY), 'Reading details',
                                'Read the selected values and the ranking of comparable settings by median score.')
        layout.Add(self.details, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, self.FromDIP(12))
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.delete = _button(self, 'Delete Reading…', 'Remove the selected reading from this assistant.', self._delete)
        self.save = _button(self, 'Export CSV…', 'Save all the captured readings to a CSV file.', self._export)
        buttons.Add(self.delete, 0)
        buttons.Add(self.save, 0, wx.LEFT, self.FromDIP(7))
        buttons.AddStretchSpacer()
        close = _button(self, 'Close', 'Return to the live assistant.', lambda: self.EndModal(wx.ID_CLOSE), wx.ID_CLOSE)
        buttons.Add(close, 0)
        layout.Add(buttons, 0, wx.EXPAND | wx.ALL, self.FromDIP(12))
        self.SetSizer(layout)
        display = wx.GetClientDisplayRect()
        width, height = self.FromDIP((810, 490))
        self.SetSize((min(width, display.width - 12), min(height, display.height - 32)))
        self.CentreOnParent()
        self.SetEscapeId(wx.ID_CLOSE)
        self._populate()
        self.table.SetFocus()

    def _populate(self, row=0):
        self.table.DeleteAllItems()
        for trial in self.trials:
            self.table.AppendItem((trial.step, trial.setting, trial.comparison, trial.metric,
                                   f'{trial.left:.4g}', f'{trial.right:.4g}'))
        self.delete.Enable(bool(self.trials))
        self.save.Enable(bool(self.trials))
        if self.trials:
            self.table.SelectRow(min(row, len(self.trials) - 1))
        self._selection()

    def _selection(self, _event=None):
        row = self.table.GetSelectedRow()
        if row < 0 or row >= len(self.trials):
            self.details.ChangeValue('Capture a reading to see it here.')
            return
        trial = self.trials[row]
        alternatives = {}
        for other in self.trials:
            comparable = ((other.step, other.comparison, other.metric, other.input_route)
                          == (trial.step, trial.comparison, trial.metric, trial.input_route))
            if comparable and other.score is not None:
                alternatives.setdefault(other.setting, []).append(other.score)
        result = f'{trial.step}. Setting {trial.setting}. Test {trial.comparison}.\n{trial.details}'
        if trial.input_route is not None:
            device, rate, clock = trial.input_route
            result += f'\nInput {device}, {rate:g} Hz, clock factor {clock:g}.'
        if alternatives:
            ranks = sorted((float(np.median(scores)), setting, len(scores)) for setting, scores in alternatives.items())
            lines = [f'{setting}: {score:.4g} ({count} {"reading" if count == 1 else "readings"})'
                     for score, setting, count in ranks]
            result += '\n\nSame test, median comparison score (lower is better).\n' + '\n'.join(lines)
        elif trial.score is None:
            result += '\n\nThis reading is a reference or exceeds the comparison limit.'
        self.details.ChangeValue(result)

    def _delete(self):
        row = self.table.GetSelectedRow()
        if row >= 0 and wx.MessageBox('Delete this captured reading?', 'Delete Reading',
                                     wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, self) == wx.YES:
            self.trials.pop(row)
            self._populate(row)

    def _export(self):
        with wx.FileDialog(self, 'Export Alignment Readings', defaultFile='Alignment Readings.csv',
                           wildcard='CSV files (*.csv)|*.csv', style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            filename = dialog.GetPath()
        text = io.StringIO(newline='')
        writer = csv.writer(text)
        writer.writerow(('Step', 'Setting', 'Test track', 'Measurement', 'Left or corrected', 'Right or raw', 'Score',
                         'Measured frequency (Hz)', 'Comparison group', 'Details', 'Input', 'Input sample rate (Hz)',
                         'Clock correction factor', 'Band frequency (Hz)', 'Left band', 'Right band'))
        for trial in self.trials:
            data = tuple(_csv_cell(value) for value in (trial.step, trial.setting, trial.track, trial.metric,
                         trial.left, trial.right, trial.score, trial.tone_frequency, trial.comparison, trial.details,
                         *(trial.input_route or ('', '', ''))))
            if trial.curve:
                writer.writerows((*data, *band) for band in zip(*trial.curve, strict=True))
            else:
                writer.writerow(data)
        try:
            atomic_write(filename, text.getvalue().encode('utf-8-sig'))
        except OSError as error:
            self.controller.show_error('Could Not Export Alignment Readings', error)
        else:
            self.controller.announce('Alignment readings exported.')
