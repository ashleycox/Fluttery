"""Native controls for preparing, playing and exporting frequency sweeps."""

from __future__ import annotations

from dataclasses import replace
from math import ulp
from pathlib import Path
from threading import Event, Thread
from time import monotonic
from wave import Error as WaveError

import wx
from wx import dataview as dv

from .audio.sweep import (
    MAX_RULES,
    WAV_BIT_DEPTHS,
    WAV_SAMPLE_RATES,
    WAVEFORMS,
    SweepRule,
    SweepSettings,
    export_sweep,
)
from .charts import LinePlot
from .keyboard_access import describe
from .preferences import DEFAULT_SWEEP_WAV, MAX_SWEEP_PRESETS

SWEEP_HELP = '06-signals.md#create-a-frequency-sweep'
FREQUENCY_SCALES = (('log', 'Logarithmic'), ('linear', 'Linear'))
AMPLITUDE_TRANSITIONS = (('step', 'Step'), ('linear', 'Linear Ramp'), ('log', 'Logarithmic Ramp'))
WAVE_CHOICES = tuple((name, name.title()) for name in WAVEFORMS)
FACTORY_PRESETS = (
    ('Audible Range', SweepSettings()),
    ('Audible Range (Descending)', SweepSettings(start_frequency=20000, end_frequency=20)),
    ('Bass', SweepSettings(start_frequency=20, end_frequency=200)),
    ('Midrange', SweepSettings(start_frequency=200, end_frequency=4000)),
    ('Treble', SweepSettings(start_frequency=4000, end_frequency=20000)),
    ('Turntable Resonance', SweepSettings(start_frequency=5, end_frequency=30, duration=15, curve='linear')),
)
FACTORY_NAMES = {'custom', *(name.casefold() for name, _ in FACTORY_PRESETS)}


def _name(control, label, help_text):
    control.SetName(label)
    if hasattr(control, 'SetAccessibleName'):
        control.SetAccessibleName(label)
    describe(control, help_text)
    return control


def _field(parent, layout, label, control, help_text):
    column = wx.BoxSizer(wx.VERTICAL)
    caption = wx.StaticText(parent, label=label)
    column.Add(caption, 0, wx.BOTTOM, parent.FromDIP(3))
    column.Add(_name(control, label, help_text), 0, wx.EXPAND)
    layout.Add(column, 0, wx.EXPAND)
    return control


def _choice(parent, layout, label, options, help_text):
    choice = _field(parent, layout, label, wx.Choice(parent, choices=[caption for _, caption in options]), help_text)
    choice.SetSelection(0)
    return choice


def _button(parent, label, help_text, callback, identifier=wx.ID_ANY):
    button = wx.Button(parent, identifier, label=label)
    describe(button, help_text)
    button.Bind(wx.EVT_BUTTON, lambda event: callback())
    return button


def _message(parent, title, text, *, error=False):
    flags = wx.OK | (wx.ICON_ERROR if error else wx.ICON_INFORMATION)
    with wx.MessageDialog(parent, str(text), title, flags) as dialog:
        dialog.ShowModal()


def _confirm(parent, title, text):
    with wx.MessageDialog(parent, text, title, wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION) as dialog:
        return dialog.ShowModal() == wx.ID_YES


def _number(field, label):
    try:
        return float(field.GetValue().strip())
    except (ValueError, OverflowError) as error:
        raise ValueError(f'Enter a number for {label}.') from error


def _fmt(value):
    return f'{value:.12g}'


def _validated_name(parent, title, initial):
    with wx.TextEntryDialog(parent, 'Enter a name for the sweep preset', title, initial) as dialog:
        if dialog.ShowModal() != wx.ID_OK:
            return None
        name = dialog.GetValue().strip()
    if not name or len(name) > 80:
        _message(parent, title, 'Enter a name between 1 and 80 characters.', error=True)
        return None
    if name.casefold() in FACTORY_NAMES:
        _message(parent, title, 'Choose a name that does not match a built-in preset or Custom.', error=True)
        return None
    return name


def _dialog_buttons(dialog, layout, apply_label, apply_action):
    buttons = wx.StdDialogButtonSizer()
    cancel = _button(dialog, 'Cancel', 'Close without applying changes.', lambda: dialog.EndModal(wx.ID_CANCEL), wx.ID_CANCEL)
    apply = _button(dialog, apply_label, f'{apply_label} and return to the previous window.', apply_action, wx.ID_OK)
    buttons.AddButton(cancel)
    buttons.AddButton(apply)
    buttons.Realize()
    layout.Add(buttons, 0, wx.ALIGN_RIGHT | wx.TOP, dialog.FromDIP(14))
    apply.SetDefault()


class RuleDialog(wx.Dialog):
    def __init__(self, parent, sweep, rule=None):
        super().__init__(parent, title='Edit Sweep Rule' if rule else 'Add Sweep Rule')
        self.sweep = sweep
        self.rule = None
        parent_layout = wx.BoxSizer(wx.VERTICAL)
        fields = wx.BoxSizer(wx.VERTICAL)
        self.waveforms = ((None, 'Keep Current Waveform'), *WAVE_CHOICES)
        self.scales = ((None, 'Keep Current Scale'), *FREQUENCY_SCALES)
        self.frequency = _field(self, fields, 'Rule frequency (Hz)', wx.TextCtrl(self),
                                'Set the frequency for this rule.')
        fields.AddSpacer(self.FromDIP(10))
        self.waveform = _choice(self, fields, 'Waveform at this frequency', self.waveforms,
                                'Keep the current waveform or switch to a different one at this frequency.')
        fields.AddSpacer(self.FromDIP(10))
        self.frequency_curve = _choice(self, fields, 'Frequency scale after this rule', self.scales,
                                       'Choose how the frequency changes from this point to the next rule or the end of the sweep.')
        self.change_level = wx.CheckBox(self, label='Change amplitude')
        describe(self.change_level, 'Set a new amplitude using this rule.')
        self.change_level.Bind(wx.EVT_CHECKBOX, lambda event: self._update_controls())
        fields.Add(self.change_level, 0, wx.TOP | wx.BOTTOM, self.FromDIP(10))
        self.level = _field(self, fields, 'Target amplitude (%)', wx.TextCtrl(self),
                            'Set the new peak level as a percentage of full scale.')
        fields.AddSpacer(self.FromDIP(10))
        self.transition = _choice(self, fields, 'Amplitude transition', AMPLITUDE_TRANSITIONS,
                                  'A step changes the level immediately. A ramp changes it gradually from the previous amplitude setting.')
        parent_layout.Add(fields, 0, wx.EXPAND)
        _dialog_buttons(self, parent_layout, 'Save Rule', self._save)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(parent_layout, 1, wx.EXPAND | wx.ALL, self.FromDIP(16))
        self.SetSizerAndFit(outer)
        width, height = self.GetSize()
        self.SetMinSize((self.FromDIP(455), height))
        self.SetSize(max(width, self.FromDIP(455)), height)
        if rule is None:
            self.level.ChangeValue(_fmt(sweep.level * 100))
        else:
            self.frequency.ChangeValue(_fmt(rule.frequency))
            self.waveform.SetSelection([value for value, _ in self.waveforms].index(rule.waveform))
            self.frequency_curve.SetSelection([value for value, _ in self.scales].index(rule.frequency_curve))
            self.change_level.SetValue(rule.level is not None)
            self.level.ChangeValue(_fmt((sweep.level if rule.level is None else rule.level) * 100))
            self.transition.SetSelection([value for value, _ in AMPLITUDE_TRANSITIONS].index(rule.level_curve))
        self._update_controls()
        self.CentreOnParent()
        self.frequency.SetFocus()

    def _update_controls(self):
        enabled = self.change_level.GetValue()
        self.level.Enable(enabled)
        self.transition.Enable(enabled)

    def _save(self):
        try:
            level = _number(self.level, 'target amplitude') / 100 if self.change_level.GetValue() else None
            rule = SweepRule(
                frequency=_number(self.frequency, 'rule frequency'),
                waveform=self.waveforms[self.waveform.GetSelection()][0],
                level=level,
                level_curve=AMPLITUDE_TRANSITIONS[self.transition.GetSelection()][0] if level is not None else 'step',
                frequency_curve=self.scales[self.frequency_curve.GetSelection()][0],
            )
            replace(self.sweep, rules=(rule,)).validate()
        except ValueError as error:
            _message(self, 'Check Sweep Rule', error, error=True)
            return
        self.rule = rule
        self.EndModal(wx.ID_OK)


class SweepRulesDialog(wx.Dialog):
    def __init__(self, parent, sweep, rules):
        super().__init__(parent, title='Advanced Sweep Rules', style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.sweep = sweep
        self.rules = list(rules)
        body = wx.BoxSizer(wx.VERTICAL)
        heading = wx.StaticText(self, label='Waveform, amplitude and frequency rules')
        heading.SetFont(heading.GetFont().Bold().Larger())
        body.Add(heading, 0, wx.BOTTOM, self.FromDIP(6))
        note = wx.StaticText(self, label='Rules are listed in playback order. An amplitude ramp ends at the frequency set by its rule.')
        note.Wrap(self.FromDIP(565))
        body.Add(note, 0, wx.EXPAND | wx.BOTTOM, self.FromDIP(12))
        self.table = _name(dv.DataViewListCtrl(self, style=dv.DV_SINGLE | dv.DV_ROW_LINES), 'Sweep rules',
                           'Select a rule to edit or delete. Press Return to edit the selected rule.')
        for label, width in (('Frequency (Hz)', 145), ('Waveform', 112), ('Amplitude', 180), ('Following scale', 135)):
            self.table.AppendTextColumn(label, width=self.FromDIP(width), flags=dv.DATAVIEW_COL_RESIZABLE)
        self.table.Bind(dv.EVT_DATAVIEW_SELECTION_CHANGED, lambda event: self._update_actions())
        self.table.Bind(dv.EVT_DATAVIEW_ITEM_ACTIVATED, lambda event: self._edit())
        body.Add(self.table, 1, wx.EXPAND)
        actions = wx.BoxSizer(wx.HORIZONTAL)
        self.add = _button(self, 'Add Rule…', 'Create a rule at a frequency in the sweep.', self._add)
        self.edit = _button(self, 'Edit Rule…', 'Change the selected rule.', self._edit)
        self.delete = _button(self, 'Delete Rule', 'Remove the selected rule from the draft.', self._delete)
        for button in (self.add, self.edit, self.delete):
            actions.Add(button, 0, wx.RIGHT, self.FromDIP(6))
        body.Add(actions, 0, wx.TOP, self.FromDIP(8))
        _dialog_buttons(self, body, 'Apply Rules', self._apply)
        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(body, 1, wx.EXPAND | wx.ALL, self.FromDIP(16))
        self.SetSizer(layout)
        self.SetMinSize(self.FromDIP((575, 350)))
        self.SetSize(self.FromDIP((640, 415)))
        self.CentreOnParent()
        self._update_table(self.rules[0] if self.rules else None)
        (self.table if self.rules else self.add).SetFocus()

    def _selection(self):
        item = self.table.GetSelection()
        return self.table.ItemToRow(item) if item.IsOk() else wx.NOT_FOUND

    def _update_table(self, selected=None):
        self.rules.sort(key=lambda rule: rule.frequency, reverse=self.sweep.start_frequency > self.sweep.end_frequency)
        self.table.DeleteAllItems()
        for index, rule in enumerate(self.rules):
            transition = dict(AMPLITUDE_TRANSITIONS)[rule.level_curve]
            level = f'{rule.level * 100:g}% — {transition}' if rule.level is not None else 'Keep Current'
            scale = dict(FREQUENCY_SCALES).get(rule.frequency_curve, 'Keep Current')
            self.table.AppendItem([_fmt(rule.frequency), (rule.waveform or 'Keep Current').title(), level, scale])
            if rule == selected:
                self.table.SelectRow(index)
        self._update_actions()

    def _update_actions(self):
        has_selection = self._selection() != wx.NOT_FOUND
        self.add.Enable(len(self.rules) < MAX_RULES)
        self.edit.Enable(has_selection)
        self.delete.Enable(has_selection)

    def _add(self):
        self._change_rule(wx.NOT_FOUND)

    def _edit(self):
        if (index := self._selection()) != wx.NOT_FOUND:
            self._change_rule(index)

    def _change_rule(self, index):
        original = self.rules[index] if index != wx.NOT_FOUND else None
        with RuleDialog(self, self.sweep, original) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            rule = dialog.rule
        if any(other.frequency == rule.frequency for position, other in enumerate(self.rules) if position != index):
            _message(self, 'Check Sweep Rule', 'Add your changes to the existing rule at this frequency.', error=True)
            return
        if original is None:
            self.rules.append(rule)
        else:
            self.rules[index] = rule
        self._update_table(rule)
        self.table.SetFocus()

    def _delete(self):
        if (index := self._selection()) == wx.NOT_FOUND:
            return
        self.rules.pop(index)
        selected = self.rules[min(index, len(self.rules) - 1)] if self.rules else None
        self._update_table(selected)
        (self.table if self.rules else self.add).SetFocus()

    def _apply(self):
        try:
            replace(self.sweep, rules=tuple(self.rules)).validate()
        except ValueError as error:
            _message(self, 'Check Sweep Rules', error, error=True)
            return
        self.EndModal(wx.ID_OK)


class WavSettingsDialog(wx.Dialog):
    def __init__(self, parent, current):
        super().__init__(parent, title='Sweep WAV Settings')
        self.result = None
        body = wx.BoxSizer(wx.VERTICAL)
        depth_options = tuple((depth, f'{depth}-bit PCM') for depth in WAV_BIT_DEPTHS)
        channel_options = ((1, 'Mono'), (2, 'Stereo'))
        self.sample_rate = _field(self, body, 'Sample rate (Hz)', wx.ComboBox(
            self, choices=[str(rate) for rate in WAV_SAMPLE_RATES], value=str(current['sample_rate']),
            style=wx.CB_DROPDOWN,
        ), 'Select a common sample rate or enter an integer rate from 8000 to 192000 Hz.')
        body.AddSpacer(self.FromDIP(10))
        self.bit_depth = _choice(self, body, 'Bit depth', depth_options,
                                 'Choose 16-bit, 24-bit or 32-bit integer PCM samples.')
        body.AddSpacer(self.FromDIP(10))
        self.channels = _choice(self, body, 'Channels', channel_options,
                                'Choose Mono for a single channel or Stereo to save the same sweep on both channels.')
        self.bit_depth.SetSelection(WAV_BIT_DEPTHS.index(current['bit_depth']))
        self.channels.SetSelection(current['channels'] - 1)
        _dialog_buttons(self, body, 'Apply WAV Settings', self._apply)
        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(body, 1, wx.EXPAND | wx.ALL, self.FromDIP(16))
        self.SetSizerAndFit(layout)
        self.SetSize(max(self.GetSize().width, self.FromDIP(355)), self.GetSize().height)
        self.CentreOnParent()
        self.sample_rate.SetFocus()

    def _apply(self):
        try:
            sample_rate = int(self.sample_rate.GetValue().strip())
        except (ValueError, OverflowError):
            self.sample_rate.SetFocus()
            _message(self, 'Check WAV Settings', 'Enter a whole-number sample rate in Hz.', error=True)
            return
        if not 8000 <= sample_rate <= 192000:
            self.sample_rate.SetFocus()
            _message(self, 'Check WAV Settings', 'Use a sample rate between 8000 and 192000 Hz.', error=True)
            return
        self.result = {'sample_rate': sample_rate, 'channels': 1 + self.channels.GetSelection(),
                       'bit_depth': WAV_BIT_DEPTHS[self.bit_depth.GetSelection()]}
        self.EndModal(wx.ID_OK)


class ManagePresetsDialog(wx.Dialog):
    def __init__(self, parent, presets):
        super().__init__(parent, title='Manage Sweep Presets', style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.presets = [dict(preset) for preset in presets]
        self._original_names = [preset['name'] for preset in self.presets]
        body = wx.BoxSizer(wx.VERTICAL)
        self.names = _name(wx.ListBox(self, choices=[preset['name'] for preset in self.presets]), 'Saved sweep presets',
                           'Select a saved sweep preset to rename or delete.')
        self.names.Bind(wx.EVT_LISTBOX, lambda event: self._update_buttons())
        self.names.Bind(wx.EVT_LISTBOX_DCLICK, lambda event: self._rename())
        body.Add(self.names, 1, wx.EXPAND)
        actions = wx.BoxSizer(wx.HORIZONTAL)
        self.rename = _button(self, 'Rename…', 'Change the selected preset name.', self._rename)
        self.delete = _button(self, 'Delete…', 'Remove the selected saved preset.', self._delete)
        actions.Add(self.rename, 0, wx.RIGHT, self.FromDIP(6))
        actions.Add(self.delete)
        body.Add(actions, 0, wx.TOP, self.FromDIP(8))
        _dialog_buttons(self, body, 'Apply Changes', lambda: self.EndModal(wx.ID_OK))
        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(body, 1, wx.EXPAND | wx.ALL, self.FromDIP(16))
        self.SetSizer(layout)
        self.SetMinSize(self.FromDIP((360, 235)))
        self.SetSize(self.FromDIP((415, 290)))
        self.CentreOnParent()
        self._refresh(0)
        self.names.SetFocus()

    def _refresh(self, selection):
        self.names.Set([preset['name'] for preset in self.presets])
        if self.presets:
            self.names.SetSelection(min(selection, len(self.presets) - 1))
        self._update_buttons()

    def _update_buttons(self):
        selected = self.names.GetSelection() != wx.NOT_FOUND
        self.rename.Enable(selected)
        self.delete.Enable(selected)

    @property
    def names_after_edit(self):
        return dict(zip(self._original_names, (preset['name'] for preset in self.presets), strict=True))

    def _rename(self):
        index = self.names.GetSelection()
        if index == wx.NOT_FOUND:
            return
        original = self.presets[index]['name']
        name = _validated_name(self, 'Rename Sweep Preset', original)
        if not name or name == original:
            return
        if any(item['name'].casefold() == name.casefold() for i, item in enumerate(self.presets) if i != index):
            _message(self, 'Rename Sweep Preset', 'A saved preset already uses that name.', error=True)
            return
        self.presets[index]['name'] = name
        self._refresh(index)

    def _delete(self):
        if (index := self.names.GetSelection()) == wx.NOT_FOUND:
            return
        name = self.presets[index]['name']
        if _confirm(self, 'Delete Sweep Preset', f'Delete “{name}” from your saved sweep presets?'):
            self.presets.pop(index)
            self._original_names.pop(index)
            self._refresh(index)


class SweepDialog(wx.Dialog):
    """A modeless generator instrument that can sit beside the analyser."""

    def __init__(self, parent, controller):
        super().__init__(parent, title='Frequency Sweep', style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.controller = controller
        self.waves = WAVE_CHOICES
        self.rules = ()
        self.wav = dict(DEFAULT_SWEEP_WAV)
        self._preview_timer = None
        self._export_cancel = None
        self._export_line = ''
        self._activity_line = 'Ready to play or save a sweep.'
        self._progress_text = ''
        self._last_sweep_state = None
        self._shutdown = Event()

        layout = wx.BoxSizer(wx.VERTICAL)
        self._build_presets(layout)
        self._build_inputs(layout)
        self._build_preview(layout)
        self._build_transport(layout)
        gtk = wx.Platform == '__WXGTK__'
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(layout, 1, wx.EXPAND | wx.ALL, self.FromDIP(12 if gtk else 16))
        self.SetSizer(outer)
        display = wx.GetClientDisplayRect()
        # GTK's taller native controls leave less space for the plot. Allow
        # extra height and reserve space for window decorations on small screens.
        height_limit = display.height - self.FromDIP(24) if gtk else display.height
        min_width, min_height = self.FromDIP((615, 720 if gtk else 570))
        self.SetMinSize((min(min_width, display.width), min(min_height, height_limit)))
        width, height = self.FromDIP((790, 795) if gtk else (745, 655))
        self.SetSize((min(width, display.width), min(height, height_limit)))
        if parent is None:
            self.CentreOnScreen()
        else:
            self.CentreOnParent()

        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.Bind(wx.EVT_CHAR_HOOK, self._key)
        self._refresh_presets('Audible Range')
        self._load_sweep(SweepSettings(), DEFAULT_SWEEP_WAV)
        self.refresh_live(controller.audio.snapshot())
        self.preset.SetFocus()

    def _build_presets(self, layout):
        caption = wx.StaticText(self, label='Sweep preset')
        layout.Add(caption, 0, wx.BOTTOM, self.FromDIP(4))
        row = wx.BoxSizer(wx.HORIZONTAL)
        self.preset = _name(wx.Choice(self), 'Sweep preset',
                            'Load a built-in sweep or one of your saved sweep presets.')
        self.preset.Bind(wx.EVT_CHOICE, lambda event: self._choose_preset())
        row.Add(self.preset, 1, wx.ALIGN_CENTER_VERTICAL)
        save = _button(self, 'Save Preset…', 'Name and save the current sweep, including its rules and WAV settings.', self._save_preset)
        self.manage = _button(self, 'Manage Presets…', 'Rename or delete your saved sweep presets.', self._manage_presets)
        for button in (save, self.manage):
            row.Add(button, 0, wx.LEFT | wx.ALIGN_CENTER_VERTICAL, self.FromDIP(8))
        layout.Add(row, 0, wx.EXPAND | wx.BOTTOM, self.FromDIP(14))

    def _build_inputs(self, layout):
        sweep = wx.StaticBoxSizer(wx.VERTICAL, self, 'Sweep Settings')
        parent = sweep.GetStaticBox()
        grid = wx.FlexGridSizer(2, 3, self.FromDIP(9), self.FromDIP(12))
        for column in range(3):
            grid.AddGrowableCol(column, 1)
        self.start_frequency = _field(parent, grid, 'Start frequency (Hz)', wx.TextCtrl(parent),
                                      'Set the frequency at the start of the sweep.')
        self.end_frequency = _field(parent, grid, 'End frequency (Hz)', wx.TextCtrl(parent),
                                    'Set the final frequency. To sweep downward, set it below the starting frequency.')
        self.duration = _field(parent, grid, 'Duration (seconds)', wx.TextCtrl(parent),
                               'Set the total duration in seconds from 0.1 through 600.')
        self.scale = _choice(parent, grid, 'Frequency scale', FREQUENCY_SCALES,
                             'In a linear sweep, the frequency changes by the same number of hertz each second. In a logarithmic sweep, it changes by the same ratio each second.')
        self.waveform = _choice(parent, grid, 'Waveform', self.waves,
                                'Choose the initial waveform. A rule can switch waveforms during the sweep.')
        self.level = _field(parent, grid, 'Amplitude (%)', wx.TextCtrl(parent),
                            'Set the initial peak amplitude as a percentage of full scale.')
        sweep.Add(grid, 0, wx.EXPAND | wx.ALL, self.FromDIP(11))
        options = wx.BoxSizer(wx.HORIZONTAL)
        self.rules_button = _button(parent, 'Advanced Rules…',
                                    'Edit optional waveform, amplitude and frequency rules.', self._edit_rules)
        options.Add(self.rules_button, 0, wx.ALIGN_CENTER_VERTICAL)
        self.rules_count = wx.StaticText(parent, label='No rules')
        describe(self.rules_count, 'Number of rules in the current sweep.')
        options.Add(self.rules_count, 0, wx.LEFT | wx.ALIGN_CENTER_VERTICAL, self.FromDIP(8))
        sweep.Add(options, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, self.FromDIP(11))
        layout.Add(sweep, 0, wx.EXPAND | wx.BOTTOM, self.FromDIP(12))
        for field in (self.start_frequency, self.end_frequency, self.duration, self.level):
            field.Bind(wx.EVT_TEXT, lambda event: self._draft_changed())
        for choice in (self.scale, self.waveform):
            choice.Bind(wx.EVT_CHOICE, lambda event: self._draft_changed())

    def _build_preview(self, layout):
        row = wx.BoxSizer(wx.HORIZONTAL)
        caption = wx.StaticText(self, label='Plot preview')
        row.Add(caption, 0, wx.RIGHT | wx.ALIGN_CENTER_VERTICAL, self.FromDIP(8))
        self.preview_mode = _name(wx.Choice(self, choices=['Frequency', 'Amplitude']), 'Plot preview',
                                  'Plot frequency or amplitude against time. Frequency is shown in Hz or kHz and amplitude as a percentage.')
        self.preview_mode.SetSelection(0)
        self.preview_mode.Bind(wx.EVT_CHOICE, lambda event: self._refresh_preview())
        row.Add(self.preview_mode, 0)
        layout.Add(row, 0, wx.BOTTOM, self.FromDIP(4))
        self.preview = LinePlot(self, x_label='Time (s)', controls=False)
        self.preview.set_appearance(self.controller.settings)
        self.preview.set_expanded_descriptions(self.controller.settings['useAccessibility'])
        layout.Add(self.preview, 1, wx.EXPAND | wx.BOTTOM, self.FromDIP(10))

    def _build_transport(self, layout):
        self.progress = _name(wx.Gauge(self, range=1000), 'Sweep playback progress',
                              'The proportion of the sweep that has played.')
        self.progress._fluttery_read_value = lambda: f'{self.progress.GetValue() / 10:g} percent played.'
        layout.Add(self.progress, 0, wx.EXPAND | wx.BOTTOM, self.FromDIP(4))
        self.status = _name(wx.TextCtrl(self, style=wx.TE_READONLY | wx.TE_MULTILINE, size=self.FromDIP((-1, 45))),
                            'Sweep activity', 'Read the progress of playback or WAV export. The text stops updating while this field has focus.')
        self.status.Bind(wx.EVT_KILL_FOCUS, self._resume_status)
        if wx.Platform == '__WXMSW__':
            self.status.Bind(wx.EVT_SYS_COLOUR_CHANGED, self._status_colours_changed)
            self._set_status_colours()
        layout.Add(self.status, 0, wx.EXPAND | wx.BOTTOM, self.FromDIP(10))
        row = wx.BoxSizer(wx.HORIZONTAL)
        help_button = _button(self, 'Help', 'Read the frequency sweep user guide.', self._show_help)
        close = _button(self, 'Close', 'Stop sweep playback and close this window.', self._hide, wx.ID_CLOSE)
        self.wav_button = _button(self, 'WAV Settings…', 'Choose the exported sample rate, bit depth and channels.', self._edit_wav)
        self.export_button = _button(self, 'Save WAV…', 'Save this sweep as a WAV file or cancel an export in progress.', self._export)
        self.play_button = _button(self, 'Play Sweep', 'Play the sweep through the signal generator’s configured output.', self._play)
        self.stop_button = _button(self, 'Stop Sweep', 'Stop the sweep that is playing.', self.controller.stop_sweep)
        for button in (help_button, close):
            row.Add(button, 0, wx.RIGHT, self.FromDIP(6))
        row.AddStretchSpacer()
        for button in (self.wav_button, self.export_button, self.play_button, self.stop_button):
            row.Add(button, 0, wx.LEFT, self.FromDIP(6))
        layout.Add(row, 0, wx.EXPAND)

    def _status_colours_changed(self, event):
        self._set_status_colours()
        event.Skip()

    def _set_status_colours(self):
        self.status.SetBackgroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOW))
        self.status.SetForegroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOWTEXT))
        self.status.Refresh(False)

    def refresh_display_settings(self):
        self.preview.set_appearance(self.controller.settings)
        self.preview.set_expanded_descriptions(self.controller.settings['useAccessibility'])
        if wx.Platform == '__WXMSW__':
            self._set_status_colours()

    def _show_help(self):
        self.controller._show_guide(SWEEP_HELP)

    def keyboard_actions(self):
        return (('Read Sweep Activity…', self._read_activity, 'Open a text snapshot of the playback and export status.'),)

    def _read_activity(self):
        self.controller._show_text('Sweep Activity', self._progress_text, parent=self)

    def _read_sweep(self, *, include_rules=True):
        sweep = SweepSettings(
            start_frequency=_number(self.start_frequency, 'start frequency'),
            end_frequency=_number(self.end_frequency, 'end frequency'),
            duration=_number(self.duration, 'duration'),
            curve=FREQUENCY_SCALES[self.scale.GetSelection()][0],
            waveform=self.waves[self.waveform.GetSelection()][0],
            level=_number(self.level, 'amplitude') / 100,
            rules=tuple(self.rules) if include_rules else (),
        )
        sweep.validate()
        return sweep

    def _load_sweep(self, sweep, wav):
        self.start_frequency.ChangeValue(_fmt(sweep.start_frequency))
        self.end_frequency.ChangeValue(_fmt(sweep.end_frequency))
        self.duration.ChangeValue(_fmt(sweep.duration))
        self.scale.SetSelection([value for value, _ in FREQUENCY_SCALES].index(sweep.curve))
        self.waveform.SetSelection([value for value, _ in self.waves].index(sweep.waveform))
        self.level.ChangeValue(_fmt(sweep.level * 100))
        self.rules = tuple(sweep.rules)
        self.wav = {**DEFAULT_SWEEP_WAV, **wav}
        self._refresh_preview()

    def _draft_changed(self):
        if 0 < self.preset.GetSelection() <= len(FACTORY_PRESETS):
            self.preset.SetSelection(0)
        if self._preview_timer is not None:
            self._preview_timer.Stop()
        self._preview_timer = wx.CallLater(120, self._refresh_preview)

    def _refresh_preview(self):
        if not self or self._shutdown.is_set():
            return
        count = len(self.rules)
        caption = f'{count} rule{"s" if count != 1 else ""}' if count else 'No rules'
        if caption != self.rules_count.GetLabel():
            self.rules_count.SetLabel(caption)
            self.Layout()
        try:
            sweep = self._read_sweep()
            times, frequencies, levels = sweep.trajectory()
        except ValueError:
            self.preview.set_series([], fit=True)
            return
        frequency = self.preview_mode.GetSelection() == 0
        name = 'Frequency' if frequency else 'Amplitude'
        if frequency:
            # Keep both the plotted values and the keyboard readout in the
            # indicated unit. kHz makes tick labels easier to scan at high ranges.
            lower, upper = sorted((sweep.start_frequency, sweep.end_frequency))
            in_kilohertz = upper >= 10000
            label = 'Frequency (kHz)' if in_kilohertz else 'Frequency (Hz)'
            unit = 1000 if in_kilohertz else 1
            values = frequencies / unit
            lower, upper = lower / unit, upper / unit
            # A minimum of one representable step keeps a near-identical
            # start and end plottable after converting to kilohertz.
            padding = max((upper - lower) * .04, ulp(upper))
            limits = (max(0, lower - padding), upper + padding)
        else:
            label = 'Amplitude (%)'
            values = levels * 100
            limits = (0, 100)
        self.preview.y_formatter = _fmt
        self.preview.set_labels(title=f'{name} over time', y_label=label)
        self.preview.set_default_view(xlim=(0, sweep.duration), ylim=limits)
        self.preview.set_series([{'label': label, 'x': times, 'y': values}], fit=True)

    def _refresh_presets(self, selected='Custom'):
        self.saved_presets = tuple(self.controller.settings.get('sweepPresets', ()))
        labels = ['Custom', *(name for name, _ in FACTORY_PRESETS), *(item['name'] for item in self.saved_presets)]
        self.preset.Set(labels)
        self.preset.SetSelection(labels.index(selected) if selected in labels else 0)
        self.manage.Enable(bool(self.saved_presets))

    def _choose_preset(self):
        choice = self.preset.GetSelection()
        if 0 < choice <= len(FACTORY_PRESETS):
            self._load_sweep(FACTORY_PRESETS[choice - 1][1], DEFAULT_SWEEP_WAV)
        elif choice > len(FACTORY_PRESETS):
            data = self.saved_presets[choice - len(FACTORY_PRESETS) - 1]
            self._load_sweep(SweepSettings.from_dict(data['sweep']), data['wav'])

    def _save_preset(self):
        try:
            sweep = self._read_sweep()
        except ValueError as error:
            _message(self, 'Check Sweep Settings', error, error=True)
            return
        selected = self.preset.GetStringSelection()
        name = _validated_name(self, 'Save Sweep Preset', '' if selected.casefold() in FACTORY_NAMES else selected)
        if not name:
            return
        presets = list(self.controller.settings.get('sweepPresets', ()))
        entry = {'name': name, 'sweep': sweep.to_dict(), 'wav': dict(self.wav)}
        matching = next((index for index, item in enumerate(presets) if item['name'].casefold() == name.casefold()), None)
        if matching is None:
            if len(presets) >= MAX_SWEEP_PRESETS:
                _message(self, 'Save Sweep Preset', 'Delete an unused preset before creating a new one.', error=True)
                return
            presets.append(entry)
        elif _confirm(self, 'Replace Sweep Preset', f'Replace “{presets[matching]["name"]}” with these settings?'):
            presets[matching] = entry
        else:
            return
        if self.controller.apply_settings(sweepPresets=presets):
            self._refresh_presets(name)
            self._remember_activity(f'Preset “{name}” saved.')

    def _manage_presets(self):
        with ManagePresetsDialog(self, self.controller.settings.get('sweepPresets', ())) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            selected = self.preset.GetStringSelection()
            if self.controller.apply_settings(sweepPresets=dialog.presets):
                selection = selected if selected.casefold() in FACTORY_NAMES else dialog.names_after_edit.get(selected, 'Custom')
                self._refresh_presets(selection)
                self._remember_activity('Saved sweep presets updated.')

    def _edit_rules(self):
        try:
            sweep = self._read_sweep(include_rules=False)
        except ValueError as error:
            _message(self, 'Check Sweep Settings', error, error=True)
            return
        with SweepRulesDialog(self, sweep, self.rules) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            self.rules = tuple(dialog.rules)
        self._draft_changed()

    def _edit_wav(self):
        with WavSettingsDialog(self, self.wav) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            self.wav = dialog.result
        self._draft_changed()
        channels = 'mono' if self.wav['channels'] == 1 else 'stereo'
        self._remember_activity(f"WAV export will use {self.wav['sample_rate']:,} Hz, {self.wav['bit_depth']}-bit PCM, {channels}.")

    def _play(self):
        try:
            sweep = self._read_sweep()
        except ValueError as error:
            _message(self, 'Check Sweep Settings', error, error=True)
            return
        if self.controller.start_sweep(sweep):
            self.refresh_live(self.controller.audio.snapshot())

    def _export(self):
        if self._export_cancel is not None:
            self._export_cancel.set()
            self._export_line = 'Cancelling WAV export…'
            self._render_status()
            return
        try:
            sweep = self._read_sweep()
            wav = dict(self.wav)
            sweep.validate(wav['sample_rate'])
        except ValueError as error:
            _message(self, 'Check Sweep WAV', error, error=True)
            return
        with wx.FileDialog(self, 'Save Sweep WAV', defaultFile='Fluttery Sweep.wav',
                           wildcard='WAV audio (*.wav)|*.wav', style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            path = Path(dialog.GetPath())
        if not path.suffix:
            path = path.with_suffix('.wav')
            # The system picker confirmed the path entered there. Adding a
            # suffix can give it a different, already existing destination.
            if path.exists() and not _confirm(self, 'Replace Sweep WAV', f'Replace “{path.name}”?'):
                return

        cancel = Event()
        self._export_cancel = cancel
        self.export_button.SetLabel('Cancel Export')
        self._export_line = 'Saving WAV…'
        self._render_status()

        def report(callback, *args):
            if not self._shutdown.is_set():
                wx.CallAfter(callback, cancel, *args)

        def save():
            last = 0.0

            def progress(current, total):
                nonlocal last
                now = monotonic()
                if now - last >= 0.2:
                    last = now
                    report(self._export_progress, round(100 * current / total))

            try:
                export_sweep(path, sweep, **wav, cancelled=cancel.is_set, progress=progress)
            except InterruptedError:
                report(self._export_finished, 'WAV export cancelled.')
            except (RuntimeError, OSError, ValueError, WaveError) as error:
                report(self._export_finished, 'WAV export failed.', str(error))
            else:
                report(self._export_finished, f'WAV saved as {path.name}.')

        # A shutdown asks the renderer to cancel. Let the worker finish deleting
        # its temporary file before Python exits.
        Thread(target=save, name='Fluttery Sweep Export').start()

    def _export_progress(self, cancel, percent):
        if self and not self._shutdown.is_set() and self._export_cancel is cancel and not cancel.is_set():
            self._export_line = f'Saving WAV… {percent}%.'
            self._render_status()

    def _export_finished(self, cancel, text, error=''):
        if not self or self._shutdown.is_set() or self._export_cancel is not cancel:
            return
        self._export_cancel = None
        self._export_line = text
        self.export_button.SetLabel('Save WAV…')
        self._render_status()
        self.controller.announce(text)
        if error:
            self.controller.log(f'Sweep WAV export failed: {error}')
            if self.IsShownOnScreen():
                _message(self, text, error, error=True)

    def _remember_activity(self, text):
        self._activity_line = text
        self._render_status()
        self.controller.announce(text)

    def _render_status(self):
        text = '\n'.join(filter(None, (self._activity_line, self._export_line)))
        self._progress_text = text
        if not self.status.HasFocus() and self.status.GetValue() != text:
            self.status.ChangeValue(text)

    def _resume_status(self, event):
        event.Skip()
        self.status.ChangeValue(self._progress_text)

    def refresh_live(self, snapshot):
        sweep = snapshot.get('sweep', {})
        elapsed, duration = sweep.get('elapsed', 0), sweep.get('duration', 0)
        position = max(0, min(1000, round(1000 * elapsed / duration))) if duration else 0
        if position != self.progress.GetValue():
            self.progress.SetValue(position)
        active = sweep.get('active', False)
        self.play_button.Enable(not active)
        self.stop_button.Enable(active)
        state = sweep.get('status', 'idle')
        identity = (sweep.get('id'), state)
        if state == 'playing':
            frequency = sweep.get('frequency')
            level = sweep.get('level')
            reading = f' {frequency:,.1f} Hz' if frequency is not None else ''
            if level is not None:
                reading += f' at {level:.1%} amplitude' if reading else f' Amplitude {level:.1%}'
            if reading:
                reading += '.'
            self._activity_line = f'Playing the sweep. {elapsed:.1f} of {duration:g} seconds.{reading}'
        elif identity != self._last_sweep_state:
            messages = {'completed': f'Sweep complete. {duration:g} seconds.', 'stopped': 'Sweep stopped.',
                        'stopping': 'Stopping sweep…', 'interrupted': 'Sweep playback interrupted.'}
            if state in messages:
                self._activity_line = messages[state]
        self._last_sweep_state = identity
        self._render_status()

    def _key(self, event):
        if event.GetKeyCode() == wx.WXK_ESCAPE and not event.HasAnyModifiers():
            self._hide()
            return
        event.Skip()

    def _on_close(self, event):
        if not self._shutdown.is_set() and event.CanVeto():
            event.Veto()
            self._hide()
        else:
            self.shutdown()
            event.Skip()

    def _hide(self):
        if self.controller.audio.snapshot().get('sweep', {}).get('active'):
            self.controller.stop_sweep()
        self.Hide()

    def shutdown(self):
        self._shutdown.set()
        if self._preview_timer is not None:
            self._preview_timer.Stop()
        if self._export_cancel is not None:
            self._export_cancel.set()
