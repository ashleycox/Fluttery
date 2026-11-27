"""Native preferences panes, with audio calibration and diagnostics."""

from __future__ import annotations

import base64
import copy
import io
import time
from pathlib import Path

import wx
import wx.dataview as dv
from PIL import Image

from .keyboard_access import describe
from .preferences import (
    BUILTIN_PROFILES,
    DEFAULTS,
    DETECTOR_FIELDS,
    DETECTOR_PRESETS,
    RESULT_LABELS,
    RESULT_VALUES,
    SAMPLE_RATES,
    SPECTROGRAM_RANGES,
    TAB_LABELS,
    TAB_VALUES,
    TURNTABLE_LABELS,
    TURNTABLE_VALUES,
    WEIGHTING_FIELDS,
    WEIGHTING_PRESETS,
    make_profile,
    preset_settings,
)
from .reporting import report_logo_png

SETTINGS_HELP = {
    'theme': 'Choose the instrument colours and the sounds for switches, buttons and the counter reset.',
    'colorMode': 'Use light colours, dark colours, or the current desktop appearance.',
    'textScale': 'Change text size across Fluttery windows without changing the system font size.',
    'enableUISounds': 'Play instrument and navigation sounds through the system’s default audio output.',
    'soundOnFocus': 'Play a quiet cue when keyboard focus moves to another control.',
    'showHelpTags': 'Show tooltips and help tags for Fluttery controls. This setting is on by default.',
    'useAccessibility': 'Add a detailed description of each graph alongside the values for the selected point.',
    'announceStatus': 'Send important measurement and device status changes to assistive technology.',
    'workspaceLayout': 'Use separate instrument windows or one tabbed workspace. Settings opens in its own window.',
    'maxSessionSamples': 'Limit the number of samples kept in the session. Older samples are dropped first.',
    'keepCompleteHistory': 'Keep every reading, including readings from earlier runs. This increases the session file size.',
    'keepSignalLossDetails': 'Retain the exact times and reported reasons for interruptions and loss of reference.',
    'includeInputAudio': 'Keep a lossless copy of the input audio in the session file. This can make the file much larger.',
    'defaultStartTab': 'Choose which tool or window opens when Fluttery starts.',
    'defaultTurntableView': 'Choose the test to show when you first open the turntable tool.',
    'defaultResultsView': 'Choose the first plot, table or metadata view in Results.',
    'checkForUpdates': 'Check for new Fluttery releases automatically.',
    'inputDevice': 'Choose an audio input device or use the system default.',
    'outputDevice': 'Choose the audio output for the signal generator and live monitoring.',
    'channels': 'Capture one channel or a stereo pair. Select Stereo for phase measurements and stereo calibration.',
    'sampleRate': 'Choose the number of audio samples per second or let the device choose.',
    'autoStartInput': 'Start live input when you first open Wow & Flutter, Oscilloscope or Turntable. You can stop it at any time.',
    'monitoring': 'Send the captured signal to the chosen output. Use headphones to prevent acoustic feedback.',
    'monitoringMode': 'Hear stereo, one channel, the average or sum of both channels, or left minus right.',
    'monitoringVolume': 'Set the gain for live monitoring. Zero mutes the signal. At one, the gain is unchanged.',
    'targetFreq': 'Enter the nominal frequency of the reference tone used to measure speed and stability.',
    'showUnsettledFrequency': 'Show an estimate when a test tone is present but its frequency has not settled. The lock indicator and wow and flutter measurements still use the regular checks.',
    'duration': 'Choose when to stop the measurement automatically. Select Continuous to measure until you stop it.',
    'weightingStandard': 'Choose legacy IEC/DIN peak, AES6 two-sigma peak, JIS RMS or unweighted measurement.',
    'flutterBand': 'Measure rotational wow and flutter, or use a 12.5 kHz reference to measure scrape flutter at higher frequencies.',
    'rmsDetector': 'Choose the adjustable exponential RMS detector or an exact one-second moving window.',
    'meterScale': 'Choose a fixed scale or let Auto adjust it to the signal level. Auto waits briefly before reducing the scale.',
    'tapeAutoStop': 'Stop recording when the test tone ends and the input stays quiet.',
    'tapeSilenceSeconds': 'Set how long the input must stay quiet after the test tone ends.',
    'tapeSilenceDb': 'Set the highest input level, in dBFS, that counts as silence at the end of a tape.',
    'testRecordSpeed': 'Enter the speed printed on the test record for accurate turntable RPM calculations.',
    'testRecordCustomRpm': 'Enter the exact cutting speed when you select Custom under Test record speed.',
    'turntable78Reference': 'Use 78.26 RPM or 78.00 RPM for the 78 RPM speed preset. This applies to both record and playback presets.',
    'meterDamping': 'Set how quickly the analogue reading moves after a change. Zero removes damping.',
    'splitFrequency': 'Set the modulation frequency that separates slower wow from faster flutter.',
    'graphSource': 'Choose the smoothed meter peak or the raw block peak for the wow and flutter history.',
    ('weightingProfile', 'preset'): 'Choose how the weighting filter emphasises modulation frequencies.',
    ('weightingProfile', 'centreFrequency'): 'Select the centre of a custom weighting filter in hertz.',
    ('weightingProfile', 'q'): 'Set the bandwidth of the custom weighting filter. A higher Q gives a narrower response.',
    ('weightingProfile', 'gain'): 'Scale the output of the custom weighting filter.',
    ('detectorTiming', 'preset'): 'Choose a response preset for RMS and peak detectors, or choose Custom.',
    ('detectorTiming', 'rmsTime'): 'Set the averaging time of the RMS detector.',
    ('detectorTiming', 'peakAttackTime'): 'Set how quickly the peak detector follows rising levels.',
    ('detectorTiming', 'peakReleaseTime'): 'Set how quickly the peak detector drops after a peak.',
    'useDCBlocker': 'Exclude steady speed offsets from the wow and flutter measurement.',
    'useAutoTarget': 'Follow a detected 3,000 or 3,150 hertz test tone automatically.',
    'useAdaptiveThreshold': 'Adjust the lock threshold as the input level changes.',
    'enableQC': 'Show whether a locked wow and flutter reading is within the quality control limit.',
    'qcLimit': 'Set the maximum acceptable wow and flutter percentage for quality control.',
    'correctionFactor': 'Correct frequency readings that are consistently too high or too low because of sample clock error.',
    'headAzimuthOffsetUs': 'Subtract the measured delay between the interface’s channels from tape head azimuth readings.',
    ('channelCalibration', 'referenceFrequency'): 'Enter the frequency of the reference tone you feed into both channels.',
    ('channelCalibration', 'leftTrimDb'): 'Apply a gain correction in decibels to the left input for stereo analysis.',
    ('channelCalibration', 'rightTrimDb'): 'Apply a gain correction in decibels to the right input for stereo analysis.',
    ('exportSettings', 'includeMetadata'): 'Include technician, equipment and notes in reports and suggested filenames.',
    ('exportSettings', 'includeDataTables'): 'Include the underlying measurement tables in supported report formats.',
    ('exportSettings', 'includeAccessibility'): 'Include text descriptions of graphs in the report.',
    ('exportSettings', 'paperSize'): 'Choose the report page size for PDF and printing.',
    ('exportSettings', 'scaleFactor'): 'Change the scale of the PDF page layout.',
    ('exportSettings', 'filenameTemplate'): 'Use {date}, {time}, {technician} and {equipment} to name exported files.',
    'technician': 'Use this name in new sessions and in the current session if its technician field is blank.',
    'plotGrid': 'Show grid lines on graphs and instrument plots.',
    'plotGridDetail': 'Use the main grid lines, or draw lighter subdivisions between them.',
    'plotCanvas': 'Match the app’s appearance, or give plots a light or dark background of their own.',
    'plotTraceStyle': 'Use fine, standard or bold lines for signal traces.',
    'plotPalette': 'Use the instrument colours, a blue and orange palette, or a high-contrast palette.',
    'plotLegend': 'Show the legend for multiple traces, show it whenever a trace is labelled, or hide it.',
    'plotPointMarkers': 'Draw dots on individual samples when there is enough room to see them.',
    'spectrogramPalette': 'Choose the colours used to show level in the flutter spectrogram.',
    'spectrogramRange': 'Set how far below 0 dB the spectrogram colour scale extends. A wider range reveals weaker signals.',
    'debugLoggingEnabled': 'Keep diagnostic messages for troubleshooting. Copy or save them from Diagnostics.',
    'Calibration status': 'Check whether calibration has succeeded, failed or is still running.',
    'Live calibration levels': 'Read the raw levels of the left and right channels and the difference after gain correction.',
    'Audio device and stream status': 'Check signal lock, input level, frequency, analysis timing, dropouts and device status.',
    'Recent diagnostic messages': 'Read or copy recent messages recorded while diagnostic logging is enabled.',
}
ACTION_HELP = {
    'Image Export Defaults…': 'Choose the format, size and appearance to start with when saving or copying any plot.',
    'Check for updates now': 'Check the release feed for a newer version of Fluttery.',
    'Refresh device list': 'Rescan for available input and output devices.',
    'Start live input': 'Apply audio changes and open the selected input.',
    'Stop live input': 'Stop the live input. You can still use the signal generator.',
    'Apply selected profile': 'Use the selected measurement profile and save the resulting settings.',
    'Save current settings as a profile…': 'Name and save the current measurement parameters for reuse.',
    'Delete selected custom profile': 'Remove the selected user-created profile after confirmation.',
    'Use 3,000 Hz': 'Set the clock calibration reference to 3,000 hertz.',
    'Use 3,150 Hz': 'Set the clock calibration reference to 3,150 hertz.',
    'Calibrate clock': 'Measure the connected reference tone to calculate a clock correction factor.',
    'Reset clock correction': 'Set the sample clock correction factor back to one.',
    'Auto-balance stereo channels': 'Capture the same tone on both channels and calculate gain trims.',
    'Reset channel trims': 'Return both channel gains to their uncorrected levels.',
    'Interface Response Calibration…': 'Measure and save the response of a loopback cable and the selected interface.',
    'Voltage Calibration…': 'Calibrate AC readings with a known reference voltage or enter the channel factors manually.',
    'Choose report logo…': 'Choose an image to include in reports.',
    'Remove report logo': 'Clear the image used in reports.',
    'Refresh audio status': 'Show up-to-date diagnostics for audio devices, streams and interface sounds.',
    'Refresh log': 'Show the latest diagnostic messages.',
    'Copy log': 'Copy recent diagnostic messages to the clipboard.',
    'Save log…': 'Write recent diagnostic messages to a text file.',
    'Clear log': 'Clear the diagnostic messages kept by Fluttery.',
}


def field_help(key, label):
    if isinstance(key, tuple) and len(key) == 3:
        return f'Include {label.lower()} in reports when those measurements are available.'
    return SETTINGS_HELP[key]


def device_identifier(device, ordinal=1):
    # PortAudio indices can change across launches. The ordinal handles identical device names.
    return f"{device['host_api']} / {device['name']} [{ordinal}]"


def device_options(devices, direction):
    counts = {}
    options = []
    for device in devices:
        if not device[f'{direction}_channels']:
            continue
        name = (device['host_api'], device['name'])
        counts[name] = counts.get(name, 0) + 1
        key = device_identifier(device, counts[name])
        label = f'{name[1]} ({name[0]})'
        if counts[name] > 1:
            label += f' #{counts[name]}'
        options.append((key, label, device['id']))
    return options


class Form(wx.ScrolledWindow):
    """Use stock controls and a consistent one-column tab order at any window size."""

    def __init__(self, parent, *, compact=False):
        super().__init__(parent, style=wx.TAB_TRAVERSAL | wx.VSCROLL)
        self.SetScrollRate(0, 12)
        self.body = wx.BoxSizer(wx.VERTICAL)
        self.SetSizer(self.body)
        self.group = self.body
        self.groups = []
        self.compact = compact
        self.group_spacing = self.FromDIP(8 if compact else 12)

    def section(self, title):
        # Parent inputs to the static box so native accessibility trees can expose the grouping.
        box = wx.StaticBoxSizer(wx.VERTICAL, self, title)
        self.groups.append(box)
        self._attach_group(box)
        self.group = box
        return box.GetStaticBox()

    def _attach_group(self, group):
        self.body.Add(group, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, self.group_spacing)

    def setup_parts(self, height):
        """Fit whole labelled groups into the wizard's available height."""
        part, used = [], 0
        for group in self.groups:
            required = group.CalcMin().height + self.group_spacing
            if part and used + required > height:
                yield tuple(part)
                part, used = [], 0
            part.append(group)
            used += required
        if part:
            yield tuple(part)

    def show_setup_part(self, groups):
        # Detach off-page groups and hide their native parent. The controls
        # keep their values, tab order and individual visibility (e.g. the logo).
        for item in self.body.GetChildren():
            self.body.Detach(item.GetSizer())
        for group in self.groups:
            group.GetStaticBox().Show(group in groups)
        for group in groups:
            self._attach_group(group)
        self.Scroll(0, 0)
        self.Layout()
        self.FitInside()

    @property
    def parent_control(self):
        return self.group.GetStaticBox()

    def add(self, widget, *, stretch=False):
        self.group.Add(widget, 0, wx.ALL | (wx.EXPAND if stretch else 0), self.FromDIP(4 if self.compact else 7))
        return widget

    def labelled(self, label, widget, *, stretch=False, inline=False):
        # Keep the visible label directly before its control in reading order.
        if wx.Platform in ('__WXMSW__', '__WXMAC__') and hasattr(widget, 'SetAccessibleName'):
            widget.SetAccessibleName(label.GetLabel())
        if inline or (self.compact and not isinstance(widget, wx.TextCtrl)):
            row = wx.BoxSizer(wx.HORIZONTAL)
            caption = row.Add(label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, self.FromDIP(9))
            if not self.compact:
                caption.SetMinSize(self.FromDIP((142, -1)))
            row.Add(widget, 1, wx.ALIGN_CENTER_VERTICAL)
            self.group.Add(row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, self.FromDIP(4 if self.compact else 7))
            return widget
        self.add(label)
        return self.add(widget, stretch=stretch)

    def note(self, text):
        label = wx.StaticText(self.parent_control, label=text)
        label.Wrap(470)
        return self.add(label, stretch=True)

    def _action(self, label, callback):
        button = wx.Button(self.parent_control, label=label)
        describe(button, ACTION_HELP[label])
        button.Bind(wx.EVT_BUTTON, lambda event: callback())
        return button

    def action(self, label, callback):
        return self.add(self._action(label, callback))

    def actions(self, *commands):
        if not self.compact:
            for label, callback in commands:
                self.action(label, callback)
            return
        row = wx.WrapSizer(wx.HORIZONTAL)
        for label, callback in commands:
            row.Add(self._action(label, callback), 0, wx.RIGHT | wx.BOTTOM, self.FromDIP(5))
        self.group.Add(row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, self.FromDIP(4))


class SettingsPage(wx.Panel):
    SECTIONS = ('general', 'audio', 'measurement', 'calibration', 'graphs', 'reports', 'diagnostics')

    def __init__(self, parent, controller, *, guided=False):
        super().__init__(parent)
        self.controller = controller
        self.guided = guided
        self.fields = {}
        self._field_groups = {}
        self._section_fields = {}
        self._preset_controls = []
        self.devices = None
        self._next_device_refresh = 0.0
        self._shown_logo = None
        self._calibration_time = None
        self.categories = None
        sections = wx.BoxSizer(wx.HORIZONTAL)
        if not guided:
            navigation = wx.Panel(self)
            self.categories = dv.DataViewListCtrl(navigation, style=dv.DV_SINGLE | dv.DV_NO_HEADER)
            self.categories.SetName('Settings categories')
            describe(self.categories, 'Use the Up and Down arrow keys to choose a category. Press Tab to enter the selected pane.')
            self.categories.AppendTextColumn('Settings categories')
            if hasattr(self.categories, 'SetAccessibleName'):
                self.categories.SetAccessibleName('Settings categories')
            self.categories.SetMinSize(self.FromDIP((155, -1)))
            self.categories.Bind(dv.EVT_DATAVIEW_SELECTION_CHANGED, self._category_changed)
            nav_sizer = wx.BoxSizer(wx.VERTICAL)
            nav_sizer.Add(self.categories, 1, wx.EXPAND)
            navigation.SetSizer(nav_sizer)
            sections.Add(navigation, 0, wx.EXPAND | wx.ALL, self.FromDIP(12))
            sections.Add(wx.StaticLine(self, style=wx.LI_VERTICAL), 0, wx.EXPAND | wx.TOP | wx.BOTTOM, self.FromDIP(12))
        self.book = wx.Simplebook(self)
        if not guided and wx.Platform == '__WXMAC__':
            from .macos import accessibility_group

            # Keep the scrollable options out of the window's flat reading order.
            accessibility_group(self.book.GetHandle(), 'Settings pane')
        builders = (self._make_general, self._make_audio, self._make_measurement, self._make_calibration,
                    self._make_graphs, self._make_export, self._make_debug)
        for section, build in zip(self.SECTIONS, builders, strict=True):
            before = len(self.fields)
            build()
            self._section_fields[section] = tuple(self.fields)[before:]

        body = wx.BoxSizer(wx.VERTICAL)
        sections.Add(self.book, 1, wx.EXPAND | (0 if guided else wx.TOP | wx.RIGHT), self.FromDIP(8))
        body.Add(sections, 1, wx.EXPAND)
        self.feedback = wx.StaticText(self, label='', name='Settings action result')
        describe(self.feedback, 'Read the result of the last action in this pane.')
        self.feedback.Hide()
        body.Add(self.feedback, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, self.FromDIP(10))
        if not guided:
            self._make_buttons(body)
            self.categories.SelectRow(0)
        self.SetSizer(body)
        self.refresh_settings()

    def _make_buttons(self, body):
        footer = wx.Panel(self, name='Settings actions')
        if wx.Platform == '__WXMAC__':
            from .macos import accessibility_group

            # VoiceOver gets one fixed action group alongside the scrolling pane.
            accessibility_group(footer.GetHandle(), 'Settings actions')
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        discard = wx.Button(footer, label='Revert edits')
        apply = wx.Button(footer, label='Apply Settings')
        close = wx.Button(footer, label='Close', id=wx.ID_CLOSE)
        describe(apply, 'Save and apply the edited preferences.')
        describe(discard, 'Restore the values from the last time you applied your settings.')
        describe(close, 'Close Settings and return to the previous tool.')
        apply.Bind(wx.EVT_BUTTON, lambda event: self.apply())
        discard.Bind(wx.EVT_BUTTON, lambda event: self.refresh_settings())
        close.Bind(wx.EVT_BUTTON, lambda event: self.GetTopLevelParent().Close())
        buttons.AddStretchSpacer()
        buttons.Add(discard, 0, wx.RIGHT, self.FromDIP(8))
        buttons.Add(apply, 0, wx.RIGHT, self.FromDIP(8))
        buttons.Add(close)
        footer.SetSizer(buttons)
        body.Add(footer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, self.FromDIP(10))

    def _category_changed(self, event):
        item = event.GetItem()
        if not item.IsOk():
            return
        index = self.categories.ItemToRow(item)
        if not 0 <= index < len(self.SECTIONS) or index == self.book.GetSelection():
            return
        # A sidebar selection changes the pane without making the first field its focus target.
        # Programmatic navigation changes the pane before selecting the row and exits above.
        self.book.ChangeSelection(index)
        self.show_feedback('')
        if not self.categories.HasFocus():
            self.categories.SetFocus()
        if self.SECTIONS[index] == 'audio':
            self.refresh_devices()
        self.controller.play_sound('switch')

    def select_section(self, section):
        if section in self.SECTIONS:
            index = self.SECTIONS.index(section)
            if self.book.GetSelection() != index:
                self.show_feedback('')
            self.book.ChangeSelection(index)
            if self.categories is not None:
                self.categories.SelectRow(index)
            if section == 'audio':
                self.refresh_devices(quiet=self.guided)

    def keyboard_actions(self):
        if self.guided:
            return
        for index, section in enumerate(self.SECTIONS):
            title = self.book.GetPageText(index)
            yield (f'Open {title} settings', lambda section=section: self.focus_section(section),
                   f'Show the {title} pane and keep keyboard focus in the category table.')

    def focus_section(self, section):
        self.select_section(section)
        self.categories.SetFocus()

    def show_feedback(self, message):
        # Action results need only appear after an operation, and clear when leaving its pane.
        if self.feedback.GetLabel() == message:
            return
        self.feedback.SetLabel(message)
        self.feedback.Show(bool(message))
        self.Layout()

    def _page(self, name):
        form = Form(self.book, compact=self.guided)
        self.book.AddPage(form, name)
        if self.categories is not None:
            self.categories.AppendItem([name])
        return form

    def _field(self, form, key, read, write):
        self.fields[key] = read, write
        self._field_groups[key] = form.group

    def _bool(self, form, key, label):
        control = form.add(wx.CheckBox(form.parent_control, label=label))
        describe(control, field_help(key, label))
        self._field(form, key, control.GetValue, control.SetValue)
        return control

    def _choice(self, form, key, label, options, *, inline=False):
        caption = wx.StaticText(form.parent_control, label=label)
        control = wx.Choice(form.parent_control, choices=[text for _, text in options], name=label)
        form.labelled(caption, control, stretch=True, inline=inline)
        describe(control, field_help(key, label))
        values = [value for value, _ in options]
        self._field(
            form, key,
            lambda: values[max(0, control.GetSelection())],
            lambda value: control.SetSelection(values.index(value) if value in values else 0),
        )
        return control

    def _float(self, form, key, label, minimum, maximum, initial, step, digits=2):
        caption = wx.StaticText(form.parent_control, label=label)
        control = wx.SpinCtrlDouble(form.parent_control, min=minimum, max=maximum, initial=initial,
                                    inc=step, name=label)
        control.SetDigits(digits)
        form.labelled(caption, control)
        describe(control, field_help(key, label))
        self._field(form, key, control.GetValue, lambda value: control.SetValue(float(value)))
        return control

    def _integer(self, form, key, label, minimum, maximum, initial):
        caption = wx.StaticText(form.parent_control, label=label)
        control = wx.SpinCtrl(form.parent_control, min=minimum, max=maximum, initial=initial, name=label)
        form.labelled(caption, control)
        describe(control, field_help(key, label))
        self._field(form, key, control.GetValue, lambda value: control.SetValue(int(value)))
        return control

    def _text(self, form, key, label):
        caption = wx.StaticText(form.parent_control, label=label)
        control = wx.TextCtrl(form.parent_control, name=label)
        form.labelled(caption, control, stretch=True)
        describe(control, field_help(key, label))
        self._field(form, key, control.GetValue, control.ChangeValue)
        return control

    def _readout(self, form, label, *, lines=3):
        caption = wx.StaticText(form.parent_control, label=label)
        control = wx.TextCtrl(form.parent_control, name=label, style=wx.TE_READONLY | wx.TE_MULTILINE,
                              size=(-1, self.FromDIP(20 * lines)))
        form.labelled(caption, control, stretch=True)
        describe(control, SETTINGS_HELP[label])
        return control

    def _make_general(self):
        form = self._page('General')
        form.section('Appearance and accessibility')
        self._choice(form, 'theme', 'Instrument appearance', (
            ('modern', 'Modern'), ('retro', 'Retro hi-fi'),
            ('neumorphism', 'Soft contours'), ('neubrutalism', 'High contrast'),
        ))
        self._choice(form, 'colorMode', 'Colour mode', (('auto', 'Follow system'), ('light', 'Light'), ('dark', 'Dark')))
        self._integer(form, 'textScale', 'Interface text size (%)', 80, 150, 100)
        if self.guided:
            form.section('Sounds and accessibility')
        self._bool(form, 'enableUISounds', 'Play interface sounds')
        self._bool(form, 'soundOnFocus', 'Play a quiet cue when a control receives focus')
        self._bool(form, 'showHelpTags', 'Show tooltips and help tags')
        self._bool(form, 'useAccessibility', 'Show expanded text descriptions for graphs')
        self._bool(form, 'announceStatus', 'Announce important status changes')
        form.section('Startup and identity')
        self._choice(form, 'workspaceLayout', 'Workspace layout', (
            ('windows', 'Separate tool windows'), ('tabs', 'Tabbed workspace'),
        ))
        form.section('Session capture')
        self._integer(form, 'maxSessionSamples', 'Maximum compact history samples', 100, 10000, 1000)
        self._bool(form, 'keepCompleteHistory', 'Keep complete run history')
        self._bool(form, 'keepSignalLossDetails', 'Keep detailed signal-loss information')
        self._bool(form, 'includeInputAudio', 'Include original input audio')
        if self.guided:
            form.section('Startup tools')
        self._choice(form, 'defaultStartTab', 'Open Fluttery on', tuple(zip(TAB_VALUES, TAB_LABELS, strict=True)))
        self._choice(form, 'defaultTurntableView', 'Default turntable view',
                     tuple(zip(TURNTABLE_VALUES, TURNTABLE_LABELS, strict=True)))
        self._choice(form, 'defaultResultsView', 'Default results view',
                     tuple(zip(RESULT_VALUES, RESULT_LABELS, strict=True)))
        if not self.guided:
            self._bool(form, 'checkForUpdates', 'Automatically check for updates')
            form.action('Check for updates now', lambda: self.controller.check_updates(manual=True))

    def _make_audio(self):
        form = self._page('Audio I/O')
        form.section('Audio devices')
        self.input_choice = self._choice(form, 'inputDevice', 'Audio input', ((None, 'System default'),))
        self.output_choice = self._choice(form, 'outputDevice', 'Audio output', ((None, 'System default'),))
        form.action('Refresh device list', self.refresh_devices)
        if self.guided:
            form.section('Input format and capture')
        self._choice(form, 'channels', 'Input channels', ((1, 'Mono'), (2, 'Stereo')))
        sample_rate = self._choice(form, 'sampleRate', 'Sample rate', ((0, 'Device default'),))
        rates = (0,)

        def set_rate(rate):
            nonlocal rates
            rates = tuple(sorted({*SAMPLE_RATES, rate}))
            sample_rate.Set([f'{item:,} Hz' if item else 'Device default' for item in rates])
            sample_rate.SetSelection(rates.index(rate))

        self.fields['sampleRate'] = (lambda: rates[max(0, sample_rate.GetSelection())], set_rate)
        self._bool(form, 'autoStartInput', 'Start live input when opening measurement tools')
        form.actions(('Start live input', self._start_input), ('Stop live input', self._stop_input))
        form.section('Input monitoring')
        self._bool(form, 'monitoring', 'Play the input through the selected output')
        form.note('Use headphones or a separate output to avoid acoustic feedback.')
        self._choice(form, 'monitoringMode', 'Monitor channels', (
            ('stereo', 'Stereo'), ('left', 'Left only'), ('right', 'Right only'),
            ('mono', 'Mono mix (average)'), ('mono_sum', 'Mono sum (left + right)'),
            ('difference', 'Left minus right'),
        ))
        self._float(form, 'monitoringVolume', 'Monitor volume (0 to 1)', 0, 1, 1, 0.01)

    def _make_measurement(self):
        form = self._page('Measurement')
        form.section('Measurement profiles')
        caption = wx.StaticText(form.parent_control, label='Profile')
        self.profiles = form.labelled(caption, wx.Choice(form.parent_control, name='Profile'), stretch=True)
        describe(self.profiles, 'Choose a built-in or saved measurement profile to apply.')
        self.profile_description = form.note('')
        self.profiles.Bind(wx.EVT_CHOICE, lambda event: self._describe_profile())
        form.action('Apply selected profile', self._apply_profile)
        form.action('Save current settings as a profile…', self._save_profile)
        form.action('Delete selected custom profile', self._delete_profile)
        form.section('Reference and meter')
        self.measure_reference = self._float(form, 'targetFreq', 'Reference frequency (Hz)', 1, 24000, 3000, 1, 1)
        self._bool(form, 'showUnsettledFrequency', 'Show frequency before the reading settles')
        self._choice(form, 'duration', 'Run duration', (
            ('continuous', 'Continuous'), ('10', '10 seconds'), ('30', '30 seconds'), ('60', '60 seconds'),
        ))
        self.measure_standard = self._choice(form, 'weightingStandard', 'Measurement standard', (
            ('iec', 'IEC/DIN peak'), ('aes6', 'AES6 weighted peak (two-sigma)'),
            ('jis', 'JIS RMS'), ('unweighted', 'Unweighted'),
        ))
        self.measure_band = self._choice(form, 'flutterBand', 'Flutter analysis band', (
            ('rotational', 'Rotational wow and flutter'), ('scrape', 'Scrape flutter (12,500 Hz reference)'),
        ))
        form.note('AES6 uses a 3,150 Hz reference and its own weighting. Scrape flutter uses a 12,500 Hz tone.')
        if self.guided:
            form.section('Meter and history')
        self._choice(form, 'meterScale', 'Analogue meter full scale', (
            (0, 'Auto'), (0.1, '0.1%'), (0.4, '0.4%'), (1, '1%'), (4, '4%'),
        ))
        self._float(form, 'meterDamping', 'Analogue needle damping (seconds)', 0, 5, 0.1, 0.01)
        self._float(form, 'splitFrequency', 'Wow / flutter split (Hz)', 0.5, 30, 6, 0.1, 1)
        self._choice(form, 'graphSource', 'History graph source', (
            ('detectorPeak', 'Detector peak'), ('rawBlockPeak', 'Raw block peak'),
        ))
        if self.guided:
            form.section('Turntable reference speed')
        self._choice(form, 'testRecordSpeed', 'Test record speed', (
            (50 / 3, '16⅔ RPM'), (33, '33⅓ RPM'), (45, '45 RPM'), (78, '78 RPM'), (0, 'Custom'),
        ))
        self._float(form, 'testRecordCustomRpm', 'Custom test record speed (RPM)', 1, 300, 100 / 3, 0.01, 4)
        self._choice(form, 'turntable78Reference', '78 RPM reference', ((78.26, '78.26 RPM'), (78.0, '78.00 RPM')))
        form.section('Weighting profile')
        self.measure_weighting = self._preset(form, 'weightingProfile', WEIGHTING_PRESETS, WEIGHTING_FIELDS, (
            ('Centre frequency (Hz)', 0.5, 30, 0.1), ('Q', 0.1, 10, 0.01), ('Gain', 0.1, 10, 0.01),
        ))
        form.section('Detector timing')
        self.measure_rms = self._choice(form, 'rmsDetector', 'RMS detector', (
            ('exponential', 'Exponential averaging'), ('oneSecond', 'One-second window'),
        ))
        self._preset(form, 'detectorTiming', DETECTOR_PRESETS, DETECTOR_FIELDS, (
            ('RMS averaging (seconds)', 0.05, 5, 0.005), ('Peak attack (seconds)', 0.001, 0.5, 0.001),
            ('Peak release (seconds)', 0.05, 5, 0.005),
        ))
        form.section('Advanced logic and quality control')
        self._bool(form, 'useDCBlocker', 'Remove steady speed error from wow and flutter')
        self.measure_auto_reference = self._bool(form, 'useAutoTarget', 'Automatically recognise 3,000 and 3,150 Hz references')
        self._bool(form, 'useAdaptiveThreshold', 'Adapt signal lock to the input level')
        self._bool(form, 'enableQC', 'Enable pass/fail quality control')
        self._float(form, 'qcLimit', 'Quality control limit (%)', 0.001, 100, 0.1, 0.01, 3)
        form.section('Tape-end detection')
        self._bool(form, 'tapeAutoStop', 'Stop at the end of a test tape')
        self._float(form, 'tapeSilenceSeconds', 'Tape-end silence (seconds)', 1, 120, 6, 0.5, 1)
        self._float(form, 'tapeSilenceDb', 'Tape-end threshold (dBFS)', -100, -6, -45, 1, 1)
        for control in (self.measure_standard, self.measure_band, self.measure_rms):
            control.Bind(wx.EVT_CHOICE, lambda event: self._update_presets())

    def _preset(self, form, key, presets, fields, parameters):
        options = [(name, details[0]) for name, details in presets.items()] + [('custom', 'Custom')]
        selection = self._choice(form, (key, 'preset'), 'Preset', tuple(options))
        controls = []
        for field, (label, minimum, maximum, step) in zip(fields, parameters, strict=True):
            controls.append(self._float(form, (key, field), label, minimum, maximum, 1, step, 3))
        self._preset_controls.append((key, presets, fields, controls))
        selection.Bind(wx.EVT_CHOICE, lambda event: self._update_presets())
        return selection

    def _update_presets(self):
        scrape = self.fields['flutterBand'][0]() == 'scrape'
        mode = self.fields['weightingStandard'][0]()
        weighting_allowed = not scrape and mode not in ('aes6', 'unweighted')
        reference_allowed = not scrape and mode != 'aes6'
        self.measure_standard.Enable(not scrape)
        self.measure_reference.Enable(reference_allowed)
        self.measure_auto_reference.Enable(reference_allowed)
        self.measure_weighting.Enable(weighting_allowed)
        for key, presets, fields, controls in self._preset_controls:
            name = self.fields[(key, 'preset')][0]()
            if name != 'custom':
                for field, value in preset_settings(presets, fields, name).items():
                    if field != 'preset':
                        self.fields[(key, field)][1](value)
            for field, control in zip(fields, controls, strict=True):
                enabled = (weighting_allowed if key == 'weightingProfile' else
                           field != 'rmsTime' or self.fields['rmsDetector'][0]() == 'exponential')
                control.Enable(name == 'custom' and enabled)

    def _make_calibration(self):
        form = self._page('Calibration')
        form.section('Clock calibration')
        form.note('Use an independently calibrated reference tone on the selected input.')
        caption = wx.StaticText(form.parent_control, label='Known reference frequency (Hz)')
        self.reference = form.labelled(caption, wx.SpinCtrlDouble(form.parent_control, min=1, max=24000,
                                                                  initial=3000, inc=1, name='Known reference frequency'))
        self.reference.SetDigits(2)
        describe(self.reference, 'Enter the known frequency of the independent clock calibration tone.')
        form.actions(('Use 3,000 Hz', lambda: self.reference.SetValue(3000)),
                     ('Use 3,150 Hz', lambda: self.reference.SetValue(3150)))
        caption = wx.StaticText(form.parent_control, label='Capture duration (seconds)')
        self.calibration_duration = form.labelled(caption, wx.SpinCtrl(form.parent_control, min=1, max=60, initial=3,
                                                                       name='Calibration capture duration'))
        describe(self.calibration_duration, 'Set how long to capture the reference signal before applying calibration.')
        self.clock_button = form.action('Calibrate clock', lambda: self._start_calibration('clock'))
        self.calibration_progress = form.add(wx.Gauge(form.parent_control, range=100, name='Calibration progress'),
                                             stretch=True)
        describe(self.calibration_progress, 'Shows the progress of the calibration capture.')
        self.calibration_progress._fluttery_read_value = lambda: f'{self.calibration_progress.GetValue()} percent complete.'
        if self.guided:
            form.section('Clock correction')
        self._float(form, 'correctionFactor', 'Clock correction factor', 0.000001, 100, 1, 0.00001, 7)
        form.action('Reset clock correction', lambda: self._set_field('correctionFactor', 1.0))
        form.section('Channel calibration')
        form.note('Feed the same tone at the same level into both channels before auto-balancing.')
        self._float(form, ('channelCalibration', 'referenceFrequency'), 'Stereo reference frequency (Hz)',
                    1, 24000, 3150, 1, 1)
        self._float(form, ('channelCalibration', 'leftTrimDb'), 'Left gain trim (dB)', -24, 24, 0, 0.1, 2)
        self._float(form, ('channelCalibration', 'rightTrimDb'), 'Right gain trim (dB)', -24, 24, 0, 0.1, 2)
        self.channels_button = form.action('Auto-balance stereo channels', lambda: self._start_calibration('channel'))
        form.action('Reset channel trims', lambda: self._set_field('channelCalibration', DEFAULTS['channelCalibration']))
        if self.guided:
            form.section('Calibration readings')
        self.calibration_text = self._readout(form, 'Calibration status', lines=2 if self.guided else 3)
        self.levels = self._readout(form, 'Live calibration levels', lines=3)
        form.section('Interface response and voltage')
        form.action('Interface Response Calibration…', lambda: self.controller.show_response('interface'))
        form.action('Voltage Calibration…', lambda: self.controller.show_level_meter('calibration'))
        self._float(form, 'headAzimuthOffsetUs', 'Head azimuth interface delay (µs)', -500000, 500000, 0, 0.1, 2)

    def _make_graphs(self):
        form = self._page('Graphs')
        form.section('Plot appearance')
        self._choice(form, 'plotCanvas', 'Plot background', (
            ('system', 'Follow app appearance'), ('light', 'Light'), ('dark', 'Dark'),
        ), inline=True)
        self._bool(form, 'plotGrid', 'Show grid lines')
        self._choice(form, 'plotGridDetail', 'Grid detail', (('major', 'Major lines'), ('minor', 'Major and minor lines')),
                     inline=True)
        self._choice(form, 'plotLegend', 'Plot legend', (
            ('auto', 'Several traces'), ('always', 'Always show'), ('hidden', 'Hide'),
        ), inline=True)
        form.section('Signal traces')
        self._choice(form, 'plotTraceStyle', 'Trace weight', (('fine', 'Fine'), ('standard', 'Standard'), ('bold', 'Bold')),
                     inline=True)
        self._choice(form, 'plotPalette', 'Trace colours', (
            ('theme', 'Instrument theme'), ('blueOrange', 'Blue and orange'), ('highContrast', 'High contrast'),
        ), inline=True)
        self._bool(form, 'plotPointMarkers', 'Mark individual samples')
        form.section('Flutter spectrogram')
        self._choice(form, 'spectrogramPalette', 'Spectrogram colours', (
            ('viridis', 'Blue to yellow (Viridis)'), ('magma', 'Purple to cream (Magma)'), ('grayscale', 'Greyscale'),
        ), inline=True)
        self._choice(form, 'spectrogramRange', 'Displayed level range', tuple(
            (value, f'−{value} to 0 dB') for value in SPECTROGRAM_RANGES
        ), inline=True)
        form.section('Plot images')
        form.action('Image Export Defaults…', self._configure_plot_images)

    def _configure_plot_images(self):
        from .plot_export_ui import configure_export_defaults

        configure_export_defaults(self, self.controller)

    def _make_export(self):
        form = self._page('Reports')
        form.section('Report options')
        for key, label in (('includeMetadata', 'Include report metadata'), ('includeDataTables', 'Include data tables'),
                           ('includeAccessibility', 'Include graph text descriptions')):
            self._bool(form, ('exportSettings', key), label)
        if self.guided:
            form.section('Page setup')
        self._choice(form, ('exportSettings', 'paperSize'), 'Paper size', tuple(
            (value, value.title() if len(value) > 2 else value) for value in ('A1', 'A2', 'A3', 'A4', 'A5', 'LETTER', 'LEGAL')
        ))
        self._choice(form, ('exportSettings', 'scaleFactor'), 'PDF layout scale', tuple(
            (factor, f'{factor:.0%}') for factor in (0.75, 1.0, 1.25, 1.5)
        ))
        self._text(form, ('exportSettings', 'filenameTemplate'), 'Suggested filename')
        form.note('Use {date}, {time}, {technician} or {equipment} in the filename template.')
        if self.guided:
            form.section('Report branding')
        self._text(form, 'technician', 'Default technician')
        self.logo_status = form.note('No custom logo selected.')
        describe(self.logo_status, 'Check whether the report logo has loaded and read its dimensions.')
        self.logo_preview = form.add(wx.StaticBitmap(form.parent_control, name='Report logo preview'))
        describe(self.logo_preview, 'Preview the image saved with your report settings.')
        self.logo_preview.Hide()
        form.action('Choose report logo…', self._choose_logo)
        form.action('Remove report logo', self._remove_logo)
        form.section('Session sections')
        for key, label in zip((*RESULT_VALUES[:4], 'stats'), (*RESULT_LABELS[:4], 'Session statistics'), strict=True):
            self._bool(form, ('exportSettings', 'sessionData', key), label)
        form.section('Turntable and distortion sections')
        for key, label in zip((*TURNTABLE_VALUES, 'thd'), (*TURNTABLE_LABELS, 'Distortion (THD/THD+N)'), strict=True):
            self._bool(form, ('exportSettings', 'turntableData', key), label)
        if self.guided:
            form.section('Other instrument sections')
        self._bool(form, ('exportSettings', 'turntableData', 'alignment'), 'Cartridge and tape alignment readings')
        self._bool(form, ('exportSettings', 'turntableData', 'frequency-response'), 'Frequency response measurements')
        self._bool(form, ('exportSettings', 'turntableData', 'bench-imd'), 'Two-tone intermodulation')
        self._bool(form, ('exportSettings', 'turntableData', 'bench-distortion'), 'Stepped distortion measurements')

    def _make_debug(self):
        form = self._page('Diagnostics')
        form.section('Audio status')
        self.health = self._readout(form, 'Audio device and stream status', lines=4 if self.guided else 8)
        form.action('Refresh audio status', self._refresh_debug)
        form.section('Debug log')
        self._bool(form, 'debugLoggingEnabled', 'Capture audio and application diagnostics')
        self.debug = self._readout(form, 'Recent diagnostic messages', lines=4 if self.guided else 8)
        form.actions(('Refresh log', self._refresh_debug),
                     ('Copy log', lambda: self.controller.copy_text(self.controller.debug_text())),
                     ('Save log…', self.controller.export_debug), ('Clear log', self._clear_debug))

    def _clear_debug(self):
        self.controller.debug_log.clear()
        self._refresh_debug()

    def _refresh_debug(self, snapshot=None):
        snapshot = self.controller.audio.snapshot() if snapshot is None else snapshot
        health = dict(snapshot.get('health', {}))
        metrics = snapshot.get('metrics', {})
        frequency = metrics.get('currentFreq', 0)
        analysis = (
            'Signal: Locked' if metrics.get('locked') else 'Signal: Unlocked',
            f"Input level: {metrics.get('inputLevel', -100):.1f} dBFS",
            f'Detected frequency: {frequency:.2f} Hz' if frequency > 0 else 'Detected frequency: Waiting for a tone',
            f"Average analysis interval: {health.pop('averageAnalysisIntervalMs', 0):.1f} ms",
            f"Longest analysis gap: {health.pop('maxAnalysisIntervalMs', 0):.1f} ms",
            f"Detector dropouts: {metrics.get('detectorDropouts', health.pop('detectorDropouts', 0))}",
            f"Capture gaps and lock losses: {metrics.get('dropouts', 0)}",
        )
        sound_error = getattr(self.controller.sounds, 'last_error', '')
        if sound_error:
            health['Interface sounds'] = sound_error
        lines = '\n'.join((*analysis, '', *(f'{key}: {value}' for key, value in health.items())))
        for field, text in ((self.health, lines), (self.debug, self.controller.debug_text())):
            if not field.HasFocus() and field.GetValue() != text:
                field.ChangeValue(text)

    def refresh_live(self, snapshot):
        index = self.book.GetSelection()
        pane = self.SECTIONS[index] if index != wx.NOT_FOUND else None
        if pane == 'audio' and time.monotonic() >= self._next_device_refresh:
            self.refresh_devices(quiet=True, only_if_changed=True)
        if pane == 'diagnostics':
            self._refresh_debug(snapshot)
        if pane != 'calibration':
            return
        if self._calibration_time is not None:
            started, duration = self._calibration_time
            self.calibration_progress.SetValue(min(99, round(100 * (time.monotonic() - started) / duration)))
        if not self.levels.HasFocus() and callable(read := getattr(self.controller.audio, 'channel_readings', None)):
            reference = self.fields[('channelCalibration', 'referenceFrequency')][0]()
            levels = read(reference)
            if levels.get('available'):
                text = (f"Raw left {levels['rawLeftDb']:.1f} dBFS. Raw right {levels['rawRightDb']:.1f} dBFS.\n"
                        f"After gain correction, the left channel minus the right is {levels['balanceDb']:+.2f} dB.")
            else:
                text = 'Waiting for a stereo reference tone.'
            if self.levels.GetValue() != text:
                self.levels.ChangeValue(text)

    def _section_to_apply(self, section):
        if section is not None or not self.guided:
            return section
        return self.SECTIONS[self.book.GetSelection()]

    def _keys_for(self, section, groups):
        keys = self._section_fields.get(section, self.fields)
        return tuple(key for key in keys if groups is None or self._field_groups[key] in groups)

    def _get_changes(self, section=None, *, groups=None):
        section = self._section_to_apply(section)
        settings = copy.deepcopy(self.controller.settings)
        for key in self._keys_for(section, groups):
            get_value, _ = self.fields[key]
            path = (key,) if isinstance(key, str) else key
            target = settings
            for part in path[:-1]:
                target = target[part]
            target[path[-1]] = get_value()
        if self._includes_logo(section, groups):
            settings['exportSettings']['logoDataURL'] = self.logo
        return {k: v for k, v in settings.items() if self.controller.settings.get(k) != v}

    def _includes_logo(self, section, groups):
        return section in (None, 'reports') and (groups is None or any(
            self.logo_status.GetParent() == group.GetStaticBox() for group in groups
        ))

    def apply(self, *, feedback=True, section=None, groups=None):
        section = self._section_to_apply(section)
        applied = self.controller.apply_settings(**self._get_changes(section, groups=groups))
        if applied:
            self.refresh_settings(preserve_edits=section is not None, committed_section=section, committed_groups=groups)
            if feedback:
                message = 'Settings applied.'
                self.show_feedback(message)
                self.controller.announce(message)
        return applied

    def refresh_settings(self, *, preserve_edits=False, committed_section=None, committed_groups=None):
        # Live settings changes and a wizard step's commit can occur while another
        # pane is half-edited. Rebase that draft while reading back committed fields.
        baseline = getattr(self, '_baseline', {}) if preserve_edits else {}
        committed = self._keys_for(committed_section, committed_groups) if committed_section is not None else ()
        pending = {key: value for key, (get, _) in self.fields.items()
                   if key in baseline and key not in committed and (value := get()) != baseline[key]}
        logo_applied = committed_section == 'reports' and self._includes_logo(committed_section, committed_groups)
        pending_logo = (preserve_edits and not logo_applied and hasattr(self, 'logo')
                        and self.logo != self._baseline_logo)
        logo_draft = self.logo if pending_logo else None
        if self.profiles.GetSelection() >= 0 and preserve_edits and committed_section != 'measurement':
            current_profile = self.profile_data[self.profiles.GetSelection()]['id']
            profile_draft = current_profile if current_profile != self._baseline_profile else None
        else:
            profile_draft = None
        has_device_draft = 'inputDevice' in pending or 'outputDevice' in pending
        self.refresh_devices(keep_selection=has_device_draft, quiet=True, use_cache=True)
        settings = self.controller.settings
        for key, (_, set_value) in self.fields.items():
            path = (key,) if isinstance(key, str) else key
            value = settings
            for part in path:
                value = value[part]
            set_value(value)
        self._update_presets()
        self._baseline = {key: get() for key, (get, _) in self.fields.items()}
        for key, value in pending.items():
            self.fields[key][1](value)
        self._update_presets()
        self.profile_data = [*BUILTIN_PROFILES, *settings['measurementProfiles']]
        self.profiles.Set([item['name'] for item in self.profile_data])
        self._baseline_profile = settings['selectedMeasurementProfileId']
        selected = next((index for index, item in enumerate(self.profile_data)
                         if item['id'] == (profile_draft or self._baseline_profile)), 0)
        self.profiles.SetSelection(selected)
        self._describe_profile()
        self._baseline_logo = settings['exportSettings']['logoDataURL']
        self.logo = logo_draft if pending_logo else self._baseline_logo
        self._logo_label()
        self._refresh_debug()

    def refresh_devices(self, keep_selection=True, quiet=False, use_cache=False, only_if_changed=False):
        if not use_cache or self.devices is None:
            self._next_device_refresh = time.monotonic() + 2
            try:
                devices = self.controller.audio.devices()
            except (OSError, RuntimeError) as error:
                self.controller.log(f'Listing audio devices: {error}')
                if not quiet:
                    self.controller.show_error('Could not list audio devices', error)
                if self.devices is not None:
                    return  # Preserve the current route and draft through a transient enumeration error.
                devices = []
            if only_if_changed and devices == self.devices:
                return
            self.devices = devices
        devices = self.devices
        for direction, control in (('input', self.input_choice), ('output', self.output_choice)):
            key = f'{direction}Device'
            saved_value = self.controller.settings[key]
            current_value = self.fields[key][0]() if keep_selection else saved_value
            options = device_options(devices, direction)
            values = [None, *(value for value, _, _ in options)]
            labels = ['System default', *(label for _, label, _ in options)]
            # Retain both the saved route and the unsaved draft across hotplug.
            for missing in (saved_value, current_value):
                if missing and missing not in values:
                    labels.append(f'Unavailable: {missing}')
                    values.append(missing)
            control.Set(labels)
            control.SetSelection(values.index(current_value) if current_value in values else 0)
            self.fields[key] = (
                lambda control=control, values=values: values[max(0, control.GetSelection())],
                lambda value, control=control, values=values: control.SetSelection(
                    values.index(value) if value in values else 0
                ),
            )

    def _set_field(self, key, value):
        if self.controller.apply_settings(**{key: value}):
            self.show_feedback('Clock correction reset.' if key == 'correctionFactor' else 'Channel trims reset.')

    def _start_input(self):
        if not self or not self.IsShownOnScreen():  # The initiating window may have closed during the prompt.
            return
        if self.apply(feedback=False, section='audio') and self.controller.ensure_audio(
            after_permission=self._start_input, require_physical=True,
        ):
            self.show_feedback('Live input started.')
        self._refresh_debug()

    def _stop_input(self):
        self.controller.stop_input()
        self.show_feedback('Live input stopped.')

    def _describe_profile(self):
        profile = self.profile_data[self.profiles.GetSelection()]
        self.profile_description.SetLabel(profile['description'] or 'Saved measurement settings.')
        self.profile_description.Wrap(self.FromDIP(470))

    def _apply_profile(self):
        profile = self.profile_data[self.profiles.GetSelection()]
        changes = self._get_changes('measurement')
        changes.update(profile['settings'], selectedMeasurementProfileId=profile['id'])
        if self.controller.apply_settings(**changes):
            self.refresh_settings(preserve_edits=True, committed_section='measurement')
            message = f"Applied {profile['name']}."
            self.show_feedback(message)
            self.controller.announce(message)

    def _save_profile(self):
        with wx.TextEntryDialog(self, 'Enter a name for this profile.', 'Save measurement profile') as dialog:
            if dialog.ShowModal() != wx.ID_OK or not dialog.GetValue().strip():
                return
            changes = self._get_changes('measurement')
            profile = make_profile(dialog.GetValue(), {**self.controller.settings, **changes})
        changes.update(measurementProfiles=[*self.controller.settings['measurementProfiles'], profile],
                       selectedMeasurementProfileId=profile['id'])
        if self.controller.apply_settings(**changes):
            self.refresh_settings(preserve_edits=True, committed_section='measurement')
            self.controller.announce(f"Saved {profile['name']}.")

    def _delete_profile(self):
        profile = self.profile_data[self.profiles.GetSelection()]
        if profile in BUILTIN_PROFILES:
            self.controller.announce('You can only delete profiles you have created.')
            return
        if not self.controller.confirm(f"Delete the profile {profile['name']}?", 'Delete profile'):
            return
        remaining = [item for item in self.controller.settings['measurementProfiles'] if item['id'] != profile['id']]
        self.controller.apply_settings(measurementProfiles=remaining, selectedMeasurementProfileId='iec-service')

    def _start_calibration(self, kind):
        # The controller keeps hardware sampling and worker completion off the UI thread.
        if not self or not self.IsShownOnScreen() or not self.apply(feedback=False, section='calibration'):
            return
        reference = self.reference.GetValue() if kind == 'clock' else self.controller.settings['channelCalibration']['referenceFrequency']
        duration = self.calibration_duration.GetValue()
        if self.controller.calibrate(kind, reference, duration, self._finish_calibration,
                                     after_permission=lambda: self._start_calibration(kind)):
            self._calibration_time = (time.monotonic(), duration)
            self.calibration_progress.SetValue(0)
            self.clock_button.Disable()
            self.channels_button.Disable()
            self.calibration_text.ChangeValue('Capturing the reference tone…')

    def _finish_calibration(self, message):
        if not self:
            return
        self._calibration_time = None
        self.calibration_progress.SetValue(100 if ' calibrated.' in message else 0)
        self.clock_button.Enable()
        self.channels_button.Enable()
        self.calibration_text.ChangeValue(message)

    def _choose_logo(self):
        wildcard = 'Image files (*.png;*.jpg;*.jpeg;*.gif;*.webp)|*.png;*.jpg;*.jpeg;*.gif;*.webp|All files|*.*'
        with wx.FileDialog(self, 'Choose report logo', wildcard=wildcard, style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            path = Path(dialog.GetPath())
        try:
            if path.stat().st_size > 20_000_000:
                raise ValueError('Choose an image smaller than 20 MB.')
            with Image.open(path) as source:
                source.thumbnail((1200, 1200))
                buffer = io.BytesIO()
                source.convert('RGBA').save(buffer, format='PNG')
            self.logo = 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode('ascii')
        except (OSError, ValueError, Image.DecompressionBombError) as error:
            self.controller.show_error('Could not use report logo', error)
            return
        self._logo_label()

    def _remove_logo(self):
        self.logo = None
        self._logo_label()

    def _logo_label(self):
        if self.logo == self._shown_logo:
            return
        self._shown_logo = self.logo
        preview = report_logo_png({'logoDataURL': self.logo})
        if preview:
            with Image.open(io.BytesIO(preview)) as logo:
                width, height = logo.size
                logo.thumbnail(tuple(self.FromDIP((225, 80))), Image.Resampling.LANCZOS)
                bitmap = wx.Bitmap.FromBufferRGBA(*logo.size, logo.tobytes())
            self.logo_preview.SetBitmap(bitmap)
            self.logo_status.SetLabel(f'Report logo: {width} by {height} pixels.')
        else:
            self.logo_status.SetLabel('Report logo could not be read.' if self.logo else 'No custom logo selected.')
        self.logo_preview.Show(bool(preview))
        pane = self.logo_preview.GetParent().GetParent()
        pane.Layout()
        pane.FitInside()
