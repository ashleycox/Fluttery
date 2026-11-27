"""Compare recorded instrument settings and review them without opening an audio stream."""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum, auto

import wx

from .keyboard_access import describe
from .preferences import (
    DETECTOR_FIELDS,
    DETECTOR_PRESETS,
    WEIGHTING_FIELDS,
    WEIGHTING_PRESETS,
    normalize_settings,
)

ROUTE_KEYS = frozenset(('inputDevice', 'outputDevice', 'sampleRate', 'channels'))
CALIBRATION_KEYS = frozenset(('correctionFactor', 'channelCalibration', 'headAzimuthOffsetUs',
                              'voltageCalibrations', 'responseCalibrations'))
INPUT_ROUTE_KEYS = frozenset(('inputDevice', 'sampleRate', 'channels'))
OVERRIDE_GROUPS = frozenset(('alignmentOptions', 'benchLevelOptions', 'responseTestDefaults'))
PRESET_GROUPS = {'detectorTiming': (DETECTOR_PRESETS, DETECTOR_FIELDS),
                 'weightingProfile': (WEIGHTING_PRESETS, WEIGHTING_FIELDS)}


class _SettingAction(Enum):
    RESET = auto()


RESET = _SettingAction.RESET

LABELS = {
    'targetFreq': 'Reference frequency', 'inputDevice': 'Audio input', 'outputDevice': 'Audio output',
    'sampleRate': 'Sample rate', 'channels': 'Input channels', 'testRecordSpeed': 'Test record speed',
    'testRecordCustomRpm': 'Custom test record speed', 'turntable78Reference': '78 RPM reference',
    'channelCalibration': 'Channel calibration', 'correctionFactor': 'Clock correction factor',
    'headAzimuthOffsetUs': 'Head azimuth offset (µs)', 'qcLimit': 'Quality control limit',
    'useAutoTarget': 'Automatically recognise reference tone', 'exportSettings': 'Report settings',
}


def setting_differences(recorded, active, prefix=(), *, replace=False):
    """Yield changed leaves, with explicit resets for authoritative groups."""
    for key, value in recorded.items():
        path = (*prefix, key)
        current = active.get(key, RESET)
        if isinstance(value, dict) and (isinstance(current, dict) or current is RESET):
            yield from setting_differences(value, {} if current is RESET else current, path,
                                           replace=replace or path[0] in OVERRIDE_GROUPS)
        elif current != value:
            yield path, value
    if replace:
        # Missing leaves in a legacy session are simply unrecorded. Embedded
        # instrument overrides are complete maps, so omissions there are resets.
        for key, current in active.items():
            if key in recorded:
                continue
            path = (*prefix, key)
            if isinstance(current, dict) and current:
                yield from setting_differences({}, current, path, replace=True)
            else:
                yield path, RESET


def with_values(settings, updates):
    result = deepcopy(settings)
    for path, value in updates.items():
        branch = result
        parents = []
        for key in path[:-1]:
            if not isinstance(branch.get(key), dict):
                branch[key] = {}
            parents.append((branch, key))
            branch = branch[key]
        if value is RESET:
            branch.pop(path[-1], None)
            for parent, key in reversed(parents):
                if parent[key]:
                    break
                del parent[key]
        else:
            branch[path[-1]] = deepcopy(value)
    return result


def at_path(settings, path, default=None):
    for key in path:
        if not isinstance(settings, dict) or key not in settings:
            return default
        settings = settings[key]
    return settings


def recorded_setup(settings):
    """Expand the values implied by a saved preset or a legacy field name."""
    expanded = deepcopy(settings)
    for key, (presets, fields) in PRESET_GROUPS.items():
        group = expanded.get(key)
        if not isinstance(group, dict):
            continue
        if key == 'weightingProfile' and 'centreFrequency' not in group and 'centerFrequency' in group:
            group['centreFrequency'] = group.pop('centerFrequency')
        preset = presets.get(str(group.get('preset')))
        if preset:
            for field, value in zip(fields, preset[1:], strict=True):
                group.setdefault(field, value)
    if expanded.get('duration') in (10, 30, 60):
        expanded['duration'] = str(int(expanded['duration']))
    rate = expanded.get('sampleRate')
    if isinstance(rate, str) and rate.strip().isdecimal():
        try:
            expanded['sampleRate'] = int(rate)
        except ValueError:
            pass  # Keep an invalid recorded rate visible as unavailable.
    return expanded


def selected_setup(base, updates):
    """Apply selected controls without silently modifying an unchecked value."""
    draft = with_values(base, updates)
    for key, (presets, fields) in PRESET_GROUPS.items():
        if not any(path[0] == key for path in updates):
            continue
        group = draft.get(key, {})
        if not isinstance(group, dict):
            continue
        preset = presets.get(str(group.get('preset')))
        if preset is None or all(group.get(field) == value for field, value in zip(fields, preset[1:], strict=True)):
            continue
        if (key, 'preset') in updates:
            name = presets[group['preset']][0]
            return None, f'Select the settings for the {name} preset, or select its numerical values individually.'
        # Editing a value within a named preset creates a custom preset, just
        # as editing the corresponding fields through Measurement Settings.
        group['preset'] = 'custom'
    candidate = normalize_settings(draft)
    for path, value in updates.items():
        # A removed override may have a normal, implicit default while
        # neighboring overrides keep this settings group present.
        if value is not RESET and at_path(candidate, path, RESET) != value:
            return None, f'Choose a compatible value in the same settings group to use {_caption(path)}.'
    # A constrained setting may normalize its siblings. Protect existing
    # values if they were not selected. Optional groups can acquire implicit
    # defaults when their first custom field is set.
    for path, _ in setting_differences(candidate, draft, replace=True):
        if (at_path(base, path, RESET) is not RESET
                and not any(path[:len(selected)] == selected for selected in updates)):
            return None, f'The selection also affects {_caption(path)}. Select the matching settings.'
    return candidate, ''


def _caption(path):
    return ' › '.join(LABELS.get(part, re.sub(r'(?<=[a-z0-9])(?=[A-Z])', ' ', part).capitalize())
                      for part in path)


def _display(path, value):
    if value is RESET:
        return 'Use default'
    if path[-1] in ('inputDevice', 'outputDevice') and value is None:
        return 'System default'
    if value is None:
        return 'Unrecorded'
    if isinstance(value, bool):
        return 'On' if value else 'Off'
    if path[-1] == 'sampleRate' and isinstance(value, (int, float)):
        return 'Device default' if not value else f'{value:,} Hz'
    if path[-1] == 'testRecordSpeed' and value == 33:
        return '33⅓ RPM'
    if isinstance(value, list):
        names = [item['name'] for item in value if isinstance(item, dict) and isinstance(item.get('name'), str) and item['name']]
        return ', '.join(names) if names and len(names) == len(value) else f'{len(value)} saved records'
    if path[-1] == 'logoDataURL' and value:
        return 'Custom logo'
    return str(value)


@dataclass(frozen=True)
class SettingDifference:
    path: tuple[str, ...]
    current: object
    saved: object
    warning: str = ''

    @property
    def label(self):
        return _caption(self.path)

    @property
    def details(self):
        return (f'{self.label}\nCurrent: {_display(self.path, self.current)}\n'
                f'Session: {_display(self.path, self.saved)}\n{self.warning or "Available"}')


class SessionSetupDialog(wx.Dialog):
    """One native list with explicit, individually selectable settings."""

    def __init__(self, parent, rows, validate, *, recorded_route):
        super().__init__(parent, title='Review Session Settings', style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.rows = tuple(rows)
        self.validate = validate
        self.selection = {}
        self.permanent = False
        outer = wx.BoxSizer(wx.VERTICAL)
        intro = ('This session was recorded with different settings. Choose which values to use.'
                 if recorded_route else
                 'This session was recorded with different settings. Fluttery has no record of the original audio route.')
        heading = wx.StaticText(self, label=intro)
        heading.Wrap(self.FromDIP(540))
        outer.Add(heading, 0, wx.EXPAND | wx.ALL, self.FromDIP(12))
        self.values = wx.CheckListBox(self, name='Differences in session settings', choices=[
            f'{row.label}  •  {_display(row.path, row.saved)}' + ('  (unavailable)' if row.warning else '')
            for row in self.rows
        ])
        describe(self.values, 'Use the arrow keys to review settings. Press Space to select a compatible setting.')
        outer.Add(self.values, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, self.FromDIP(12))
        details_label = wx.StaticText(self, label='Selected difference')
        outer.Add(details_label, 0, wx.LEFT | wx.TOP, self.FromDIP(12))
        self.details = wx.TextCtrl(self, style=wx.TE_READONLY | wx.TE_MULTILINE, name='Selected setting difference',
                                   size=self.FromDIP((-1, 85)))
        describe(self.details, 'Read the current and recorded values and check for compatibility issues.')
        outer.Add(self.details, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, self.FromDIP(12))
        self.values.Bind(wx.EVT_LISTBOX, self._highlight)
        self.values.Bind(wx.EVT_CHECKLISTBOX, self._check)
        shortcuts = wx.BoxSizer(wx.HORIZONTAL)
        self._button(shortcuts, 'Select All Compatible', lambda: self._select(True),
                     'Select every setting that can be used on this computer.')
        self._button(shortcuts, 'Clear Selection', lambda: self._select(False),
                     'Keep all settings at their current values.')
        outer.Add(shortcuts, 0, wx.ALL, self.FromDIP(8))
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        buttons.AddStretchSpacer()
        keep = self._button(buttons, 'Keep Current Settings', lambda: self.EndModal(wx.ID_CANCEL),
                            'Open the session using the current setup.')
        self._button(buttons, 'Use for This Session', lambda: self._accept(False),
                     'Temporarily apply only the settings you selected.')
        self._button(buttons, 'Save as My Settings', lambda: self._accept(True),
                     'Save the selected settings as preferences.')
        keep.SetDefault()
        self.SetEscapeId(keep.GetId())
        outer.Add(buttons, 0, wx.EXPAND | wx.ALL, self.FromDIP(8))
        self.SetSizer(outer)
        self.SetMinSize(self.FromDIP((560, 390)))
        work = wx.GetClientDisplayRect()
        size = self.FromDIP((660, 475))
        self.SetSize((min(size.width, work.width), min(size.height, work.height)))
        self.CentreOnParent()
        self.values.SetSelection(0)
        self._highlight(None)
        self.values.SetFocus()

    def _button(self, row, title, action, help_text):
        button = wx.Button(self, label=title)
        describe(button, help_text)
        button.Bind(wx.EVT_BUTTON, lambda _event: action())
        row.Add(button, 0, wx.RIGHT, self.FromDIP(4))
        return button

    def _highlight(self, _event):
        index = self.values.GetSelection()
        if index != wx.NOT_FOUND:
            self.details.ChangeValue(self.rows[index].details)
            self.details.SetInsertionPoint(0)

    def _check(self, event):
        index = event.GetInt()
        if self.rows[index].warning:
            self.values.Check(index, False)
        self._highlight(event)

    def _select(self, checked):
        for index, row in enumerate(self.rows):
            self.values.Check(index, checked and not row.warning)

    def _accept(self, permanent):
        chosen = {self.rows[index].path: self.rows[index].saved for index in self.values.GetCheckedItems()
                  if not self.rows[index].warning}
        if warning := self.validate(chosen):
            wx.MessageBox(warning, 'Selected Settings Cannot Be Applied', wx.OK | wx.ICON_INFORMATION, self)
            return
        self.selection, self.permanent = chosen, permanent
        self.EndModal(wx.ID_OK)
