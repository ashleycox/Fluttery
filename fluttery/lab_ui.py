"""Native instruments for calibrated levels and audio recording analysis."""

from collections import deque
from pathlib import Path
from threading import Event, Thread

import numpy as np
import wx

from .audio.levels import (
    LEVEL_BANDS,
    VOLTAGE_UNITS,
    ac_levels,
    amplitude_db,
    band_rms,
    calibration_factor,
    default_level_options,
    measure_noise,
    voltage_reference,
)
from .audio.recordings import analyse_wav, inspect_wav
from .charts import LinePlot
from .keyboard_access import describe
from .sweep_ui import _button, _name


class LabDialog(wx.Dialog):
    """Shared layout and lifecycle for instrument dialogs."""

    def __init__(self, parent, controller, title, help_topic):
        super().__init__(parent, title=title, style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.controller = controller
        self.help_topic = help_topic
        self.activity = ''
        self.content = wx.BoxSizer(wx.VERTICAL)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(self.content, 1, wx.EXPAND | wx.ALL, self.FromDIP(14))
        self.SetSizer(outer)
        self.Bind(wx.EVT_CLOSE, self._close)

    def _row(self, *, weight=0):
        row = wx.WrapSizer(wx.HORIZONTAL, flags=wx.REMOVE_LEADING_SPACES)
        self.content.Add(row, weight, wx.EXPAND | wx.BOTTOM, self.FromDIP(8))
        return row

    def _label(self, name, description):
        caption = wx.StaticText(self, label=name)
        describe(caption, description)
        self.content.Add(caption, 0, wx.EXPAND | wx.BOTTOM, self.FromDIP(8))
        return caption

    def _field(self, row, name, description, widget):
        field = wx.BoxSizer(wx.VERTICAL)
        field.Add(wx.StaticText(self, label=name), 0, wx.BOTTOM, self.FromDIP(3))
        field.Add(_name(widget, name, description), 0, wx.EXPAND)
        row.Add(field, 0, wx.RIGHT | wx.BOTTOM, self.FromDIP(9))
        return widget

    def _number(self, row, label, explanation, value):
        return self._field(row, label, explanation, wx.TextCtrl(self, value=str(value), size=self.FromDIP((115, -1))))

    def _choice(self, row, label, explanation, choices):
        control = self._field(row, label, explanation, wx.Choice(self, choices=choices))
        if choices:
            control.SetSelection(0)
        return control

    def _action(self, row, label, explanation, callback, identifier=wx.ID_ANY):
        button = _button(self, label, explanation, callback, identifier)
        row.Add(button, 0, wx.RIGHT | wx.BOTTOM | wx.ALIGN_CENTER_VERTICAL, self.FromDIP(6))
        return button

    def _readout(self, label):
        self.status = _name(wx.TextCtrl(self, style=wx.TE_READONLY | wx.TE_MULTILINE,
                                       size=self.FromDIP((-1, 92))), label,
                            'Read or copy the measurement as text. The display stops updating while this field has focus.')
        self.status.Bind(wx.EVT_KILL_FOCUS, self._resume_readout)
        if wx.Platform == '__WXMSW__':
            self.status.SetBackgroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOW))
            self.status.SetForegroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOWTEXT))
        self.content.Add(self.status, 0, wx.EXPAND | wx.BOTTOM, self.FromDIP(8))

    def _resume_readout(self, event):
        event.Skip()
        self._tell(self.activity, force=True)

    def _tell(self, text, *, force=False):
        self.activity = text
        if (force or not self.status.HasFocus()) and text != self.status.GetValue():
            self.status.ChangeValue(text)

    def _actions(self, *, audio_settings=True):
        row = self._row()
        self._action(row, 'Help', 'Read the user manual for this instrument.',
                     lambda: self.controller._show_guide(self.help_topic))
        if audio_settings:
            self._action(row, 'Audio Settings…', 'Open the Audio I/O settings section.',
                         lambda: self.controller.navigate('settings', 'audio'))
        self._footer = row
        return row

    def _plot(self, title, xlabel, ylabel):
        plot = LinePlot(self, title=title, x_label=xlabel, y_label=ylabel, controls=False)
        plot.set_appearance(self.controller.settings)
        plot.set_expanded_descriptions(self.controller.settings['useAccessibility'])
        self.content.Add(plot, 1, wx.EXPAND | wx.BOTTOM, self.FromDIP(8))
        return plot

    def _finish_layout(self, size):
        self._action(self._footer, 'Close', 'Close this instrument.', self.Close, wx.ID_CLOSE)
        self.SetEscapeId(wx.ID_CLOSE)
        width, height = self.FromDIP(size)
        area = wx.Display(0).GetClientArea()
        self.SetClientSize((min(width, area.width - self.FromDIP(24)), min(height, area.height - self.FromDIP(54))))
        # Measure wrapped rows at the actual width. GTK initially creates a
        # narrow dialog and otherwise retains that temporary minimum height.
        self.Layout()
        self.SetMinClientSize(self.GetSizer().CalcMin())

    def refresh_display_settings(self):
        if (plot := getattr(self, 'plot', None)) is not None:
            plot.set_appearance(self.controller.settings)
            plot.set_expanded_descriptions(self.controller.settings['useAccessibility'])

    def shutdown(self):
        pass

    def _close(self, event):
        self.shutdown()
        self.controller.forget_instrument(self)
        self.Destroy()


LEVEL_HELP = '11-bench-levels.md'


class LevelOptionsDialog(wx.Dialog):
    """Choose a named bench bandwidth or set it explicitly for this instrument."""

    def __init__(self, parent, mode, options):
        super().__init__(parent, title='Measurement Options')
        self.mode = mode
        self.result = None
        self.band = _name(wx.Choice(self, choices=[item[0] for item in LEVEL_BANDS.values()]), 'Measurement bandwidth',
                          'Select a standard bandwidth or choose Custom to set both frequency limits.')
        self.low = _name(wx.TextCtrl(self), 'Low limit (Hz)', 'Lowest frequency in the custom measurement band.')
        self.high = _name(wx.TextCtrl(self), 'High limit (Hz)', 'Highest frequency in the custom measurement band.')
        self.window = _name(wx.TextCtrl(self), 'Observation window (seconds)',
                            'Time used to calculate each reading. A longer window gives steadier readings.')
        self.units = (_name(wx.Choice(self, choices=list(VOLTAGE_UNITS.values())), 'Plot units',
                            'Automatic uses volts when all active channels are calibrated and dBFS otherwise.')
                      if mode == 'voltage' else None)
        form = wx.FlexGridSizer(cols=2, vgap=self.FromDIP(9), hgap=self.FromDIP(12))
        form.AddGrowableCol(1)
        for control in (self.band, self.low, self.high, self.window, self.units):
            if control is not None:
                form.Add(wx.StaticText(self, label=control.GetName()), 0, wx.ALIGN_CENTER_VERTICAL)
                form.Add(control, 1, wx.EXPAND)
        self.band.Bind(wx.EVT_CHOICE, self._update_band)
        self._load(options)
        actions = wx.BoxSizer(wx.HORIZONTAL)
        actions.Add(_button(self, 'Restore Defaults', 'Restore the original settings for this instrument.',
                            lambda: self._load(default_level_options(mode))), 0, wx.RIGHT, self.FromDIP(8))
        actions.AddStretchSpacer()
        cancel = _button(self, 'Cancel', 'Close without applying changes.', lambda: self.EndModal(wx.ID_CANCEL), wx.ID_CANCEL)
        apply = _button(self, 'Apply', 'Save these defaults for this instrument.', self._apply, wx.ID_OK)
        for button in (cancel, apply):
            actions.Add(button, 0, wx.LEFT, self.FromDIP(5))
        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(form, 0, wx.EXPAND | wx.ALL, self.FromDIP(14))
        layout.Add(actions, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, self.FromDIP(14))
        self.SetSizerAndFit(layout)
        width, height = self.GetSize()
        self.SetMinSize((max(width, self.FromDIP(540)), height))
        self.SetSize(self.GetMinSize())
        apply.SetDefault()
        self.CentreOnParent()

    def _load(self, options):
        self.band.SetSelection(tuple(LEVEL_BANDS).index(options['band']))
        self.low.ChangeValue(f'{options["lowHz"]:g}')
        self.high.ChangeValue(f'{options["highHz"]:g}')
        self.window.ChangeValue(f'{options["window"]:g}')
        if self.units is not None:
            self.units.SetSelection(tuple(VOLTAGE_UNITS).index(options['units']))
        self._update_band(None)

    def _update_band(self, event):
        band = tuple(LEVEL_BANDS)[self.band.GetSelection()]
        if band != 'custom':
            _, low, high = LEVEL_BANDS[band]
            self.low.ChangeValue(f'{low:g}')
            self.high.ChangeValue(f'{high:g}')
        for control in (self.low, self.high):
            control.Enable(band == 'custom')

    def _apply(self):
        try:
            low, high, seconds = (float(control.GetValue()) for control in (self.low, self.high, self.window))
            if not (0 <= low < high <= 192000 and .25 <= seconds <= 5):
                raise ValueError
        except ValueError:
            wx.MessageBox('Enter a low limit below the high limit, both between 0 and 192,000 Hz. Set the window to 0.25 to 5 seconds.',
                          'Check measurement options', wx.OK | wx.ICON_WARNING, self)
            return
        self.result = {'band': tuple(LEVEL_BANDS)[self.band.GetSelection()], 'lowHz': low, 'highHz': high,
                       'window': seconds, 'units': tuple(VOLTAGE_UNITS)[self.units.GetSelection()] if self.units else 'auto'}
        self.EndModal(wx.ID_OK)


class LevelsDialog(LabDialog):
    def __init__(self, parent, controller, mode):
        title, anchor = {
            'voltage': ('AC Voltmeter', 'measure-ac-voltage'),
            'calibration': ('Voltage Calibration', 'calibrate-voltage'),
            'snr': ('A-weighted Signal-to-Noise', 'measure-a-weighted-signal-to-noise'),
        }[mode]
        super().__init__(parent, controller, title, LEVEL_HELP + '#' + anchor)
        self.mode = mode
        self.options = controller.settings.get('benchLevelOptions', {}).get(mode, default_level_options(mode))
        self._data = deque(maxlen=120)
        self._sequence = None
        self._first_frame = None
        self._reference = None
        self._reference_route = None
        self._reference_band = None
        self._reference_sequence = None
        self._noise_readings = []
        self._rendered_noise = None
        self._pending = None
        self._previous_route = None
        explanations = {
            'voltage': ('Read AC RMS and sample peak for each input. To show the readings in volts, '
                        'calibrate each channel with a known voltage.'),
            'calibration': ('Feed a steady sine wave into the interface and measure its voltage at the input with a '
                            'reference meter. Enter the RMS voltage, capture the tone and check the channel factors before saving.'),
            'snr': ('Play a steady reference signal and choose Capture Reference. Stop the signal and leave the input '
                    'and gain unchanged, then choose Capture Noise. Fluttery compares A-weighted noise with the unweighted RMS reference.'),
        }
        instruction = self._label(explanations[mode], explanations[mode])
        instruction.Wrap(self.FromDIP(715))
        fields = self._row()
        self._action(fields, 'Measurement Options…', 'Choose the bandwidth, observation window and available plot units.', self._options)
        self.options_summary = _name(wx.StaticText(self), 'Active measurement options',
                                     'Shows the current bandwidth and capture window.')
        fields.Add(self.options_summary, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, self.FromDIP(8))
        self._show_options()
        self.plot_selection = None
        if mode == 'snr':
            self.plot_selection = self._choice(fields, 'Plot', 'Automatic shows level history until a noise spectrum is available.',
                                               ['Automatic', 'Level history', 'Noise spectrum'])
            self.plot_selection.Bind(wx.EVT_CHOICE, lambda event: self._draw_history())
        self.plot = self._plot('Input level history', 'Elapsed time (s)', 'AC level (dBFS)')
        if mode == 'calibration':
            fields = self._row()
            self.reference_v = self._number(fields, 'Reference voltage (V RMS)', 'Voltage read on the reference AC meter.', 1.0)
            self.cal_channel = self._choice(fields, 'Calibrate channel', 'Choose which channel receives the reference voltage.',
                                            ['Left or mono', 'Right', 'Both (same known voltage)'])
            fields = self._row()
            self.left_factor = self._number(fields, 'Left V per full scale', 'Calculated or manually entered left-channel factor.', '')
            self.right_factor = self._number(fields, 'Right V per full scale', 'Calculated or manually entered right-channel factor.', '')
        self._readout('Level measurements')
        row = self._actions()
        self._action(row, 'Start Input', 'Begin receiving audio from the selected input.', self._start)
        if mode == 'voltage':
            self._action(row, 'Calibrate…', 'Open the voltage calibration assistant.',
                         lambda: self.controller.show_level_meter('calibration'))
        elif mode == 'calibration':
            self._action(row, 'Capture Reference', 'Calculate voltage factors from the current input.', self._capture_voltage)
            self._action(row, 'Save Calibration', 'Save the entered factors for this route.', self._save_voltage)
        else:
            self._action(row, 'Capture Reference', 'Store the live signal level for each channel.', self._capture_reference)
            self.noise_button = self._action(row, 'Capture Noise', 'Compare the current noise with the stored reference.', self._capture_noise)
            self.noise_button.Disable()
        self._tell('Choose Start Input if the input is not already running in another instrument.')
        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._tick, self.timer)
        self.timer.Start(250)
        self._finish_layout((780, 680 if mode == 'calibration' else 535))

    def shutdown(self):
        self.timer.Stop()

    def _start(self):
        if self and self.IsShown():
            self.controller.ensure_audio(after_permission=self._start, require_physical=True)

    def _settings(self):
        return self.options['lowHz'], self.options['highHz'], self.options['window']

    def _show_options(self):
        low, high, window = self._settings()
        band = 'Wideband AC' if self.options['band'] == 'wide' else f'{low:g}–{high:g} Hz'
        self.options_summary.SetLabel(f'{band}  ·  {window:g} s window')

    def _options(self):
        with LevelOptionsDialog(self, self.mode, self.options) as dialog:
            if dialog.ShowModal() != wx.ID_OK or dialog.result == self.options:
                return
            updated = dialog.result
        settings = self.controller.settings.get('benchLevelOptions', {})
        self.controller.apply_settings(benchLevelOptions={**settings, self.mode: updated})

    def refresh_display_settings(self):
        super().refresh_display_settings()
        latest = self.controller.settings.get('benchLevelOptions', {}).get(self.mode, default_level_options(self.mode))
        if latest == self.options:
            return
        previous = self._settings()
        self.options = latest
        self._show_options()
        if previous != self._settings():
            self._reference_route = None
            self._reference_sequence = None
            self._noise_readings = []
            self._reference = 'Measurement settings changed. Capture a new reference.' if self.mode == 'snr' else None
            if self.mode == 'snr':
                self.noise_button.Disable()
            self._data.clear()
            self._first_frame = None
            self._sequence = None
            self.plot.clear()
            if self._reference:
                self._tell(self._reference, force=True)
        self._draw_history()
        self.Layout()

    def _pcm(self, *, preroll=0):
        low, high, window = self._settings()
        correction = self.controller.settings.get('correctionFactor', 1.0)
        device_rate, frames, sequence, epoch = self.controller.audio.input_observation((window + preroll) * correction)
        rate = device_rate * correction
        if len(frames) < np.ceil((window * correction) * device_rate):
            raise ValueError('Wait until the input has run for a full observation window.')
        if low >= rate / 2:
            raise ValueError('Set the lower limit of the measurement band below half the input sample rate.')
        # The window and its frequency bands use the corrected clock. Voltage
        # factors still belong to the device's negotiated hardware mode.
        device = self.controller.audio_input_identity(), round(device_rate)
        route = device, frames.shape[1], epoch, correction
        return rate, frames, sequence, route, low, high

    def _calibration(self, route):
        return next((record for record in self.controller.settings['voltageCalibrations']
                     if (record['device'], record['rate']) == route), {})

    def _prepare_calibration_route(self, route, calibration):
        if self._pending == route:
            return
        if self._pending is not None:
            self._reference = 'Input changed or was interrupted. Capture another reference before saving.'
        self._pending = route
        for field, key in ((self.left_factor, 'left'), (self.right_factor, 'right')):
            value = calibration.get(key)
            field.ChangeValue(f'{value:.10g}' if value is not None else '')

    def _tick(self, event):
        if not self.IsShownOnScreen():
            return
        try:
            rate, frames, sequence, route, low, high = self._pcm(preroll=0 if self.mode == 'snr' else 1)
            if sequence == self._sequence and route == self._previous_route:
                return
            if self.mode == 'snr':
                level = band_rms(frames, rate, low, high)
                peak = clip = np.max(np.abs(frames), axis=0)
            else:
                observation = min(len(frames), int(np.ceil(self.options['window'] * rate)))
                level, peak, clip = ac_levels(frames, rate, low, high, observation_frames=observation)
        except ValueError as error:
            if self.mode == 'snr':
                self.noise_button.Disable()
            self._tell(str(error))
            return
        self._sequence = sequence
        if route != self._previous_route or self._first_frame is None:
            self._previous_route = route
            self._first_frame = sequence
            self._data.clear()
        self._data.append(((sequence - self._first_frame) / rate, *level))
        calibration = self._calibration(route[0])
        if self.mode == 'calibration':
            self._prepare_calibration_route(route, calibration)
        lines = []
        for channel, name in enumerate(('Left' if len(level) == 2 else 'Mono', 'Right')[:len(level)]):
            factor = calibration.get('left' if channel == 0 else 'right')
            status = f'{name}. {amplitude_db(level[channel]):.2f} dBFS RMS, {amplitude_db(peak[channel]):.2f} dBFS peak.'
            if factor is not None:
                volts = factor * level[channel]
                status += (f' {volts:.6g} V RMS, {factor * peak[channel]:.6g} V peak, '
                           f'{amplitude_db(volts):.2f} dBV, {amplitude_db(volts, .775):.2f} dBu.')
            else:
                status += ' Voltage is uncalibrated.'
            if clip[channel] >= .985:
                status += ' Near clipping.'
            lines.append(status)
        if self.mode == 'snr' and self._reference_sequence is not None:
            self._show_noise_readiness(lines, rate, len(frames), sequence, route)
        if self._reference is not None:
            lines.append(self._reference)
        self._tell('\n'.join(lines))
        if not (self.plot.canvas.HasFocus() or self.plot.readout.HasFocus()):
            self._draw_history()

    def _show_noise_readiness(self, lines, rate, window_frames, sequence, route):
        if self._reference_route != route or self._reference_band != self._settings():
            self._reference = 'Input changed or was interrupted. Capture another reference for a new comparison.'
            self._reference_sequence = None
            self._noise_readings = []
            self.noise_button.Disable()
            return
        remaining = (window_frames - (sequence - self._reference_sequence)) / rate
        self.noise_button.Enable(remaining <= 0)
        if not self._noise_readings:
            if remaining > 0:
                lines.append(f'Wait {remaining:.1f} s for a fresh noise reading.')
            else:
                lines.append('Stop the reference signal before you choose Capture Noise. Keep the gain the same.')

    def _draw_history(self):
        if self.mode == 'snr':
            view = self.plot_selection.GetSelection()
            if view == 2 or view == 0 and self._noise_readings:
                self._draw_noise()
                return
        self._rendered_noise = None
        entries = tuple(self._data)
        if not entries:
            self.plot.log_x = False
            self.plot.set_labels('Input level history', 'Elapsed time (s)', 'AC level (dBFS RMS)')
            self.plot.clear()
            return
        route, channels, _, _ = self._previous_route
        calibration = self._calibration(route)
        factors = tuple(calibration.get(key) for key in ('left', 'right')[:channels])
        units = self.options['units'] if self.mode == 'voltage' else 'dbfs'
        if units == 'auto':
            units = 'volts' if all(factors) else 'dbfs'
        title = 'Input level history'
        if units != 'dbfs' and not any(factors):
            title = 'Input voltage — calibration required'
        self.plot.log_x = False
        y_label = {'dbfs': 'AC level (dBFS RMS)', 'volts': 'AC voltage (V RMS)', 'millivolts': 'AC voltage (mV RMS)'}[units]
        self.plot.set_labels(title, 'Elapsed time (s)', y_label)
        x = [row[0] for row in entries]
        names = ('Left', 'Right') if channels == 2 else ('Mono',)
        series = []
        for index, (name, factor) in enumerate(zip(names, factors, strict=True), 1):
            if units == 'dbfs':
                y = [amplitude_db(row[index]) for row in entries]
            elif factor is not None:
                scale = factor * (1000 if units == 'millivolts' else 1)
                y = [scale * row[index] for row in entries]
            else:
                continue
            series.append({'label': name, 'x': x, 'y': y, 'color': ('#2477d3', '#ad4c2d')[index - 1]})
        self.plot.set_series(series, fit=True)

    def _draw_noise(self):
        if self._rendered_noise is self._noise_readings:
            return
        self._rendered_noise = self._noise_readings
        self.plot.log_x = True
        self.plot.set_labels('Captured noise spectrum', 'Frequency (Hz)', 'dBFS/Hz')
        names = ('L', 'R') if len(self._noise_readings) == 2 else ('Mono',)
        series = []
        for reading, name, color in zip(self._noise_readings, names, ('#2477d3', '#ad4c2d')):
            series.extend(({'label': f'{name} unweighted', 'x': reading.frequency, 'y': reading.density_db, 'color': color},
                           {'label': f'{name} A-weighted', 'x': reading.frequency, 'y': reading.weighted_density_db,
                            'color': color, 'style': 'dashed'}))
        self.plot.set_series(series, fit=True)

    def _capture_voltage(self):
        try:
            rate, frames, _, route, low, high = self._pcm()
            voltage = float(self.reference_v.GetValue())
            choice = self.cal_channel.GetSelection()
            indices = (0, 1) if choice == 2 else (choice,)
            if max(indices) >= frames.shape[1]:
                raise ValueError('Set Input channels to Stereo in Audio I/O to calibrate the right channel.')
            factors = {}
            for index in indices:
                samples = frames[:, index]
                peak = float(np.max(np.abs(samples)))
                if peak >= .985:
                    raise ValueError('The reference is clipping or close to clipping. Reduce the input level.')
                rms, frequency = voltage_reference(samples, rate, low, high)
                factors[index] = calibration_factor(voltage, rms, peak), frequency
        except ValueError as error:
            self.controller.show_error('Could not calibrate voltage', error)
            return
        # A capture can be the first action on a new route before the live
        # timer runs. Seed both fields before replacing the selected factor.
        self._prepare_calibration_route(route, self._calibration(route[0]))
        for index, (factor, _) in factors.items():
            (self.left_factor, self.right_factor)[index].ChangeValue(f'{factor:.10g}')
        tones = ', '.join(f"{('Left' if frames.shape[1] == 2 else 'Mono', 'Right')[index]} {hz:,.2f} Hz"
                          for index, (_, hz) in factors.items())
        self._reference = f'Reference captured ({tones}). Review or edit the factors, then choose Save Calibration.'
        self._tell(self._reference, force=True)

    def _save_voltage(self):
        try:
            _, _, _, route, _, _ = self._pcm()
            device, rate = route[0]
            if self._pending != route:
                raise ValueError('Wait for the current input to settle, then capture or re-enter the channel factors.')
            factors = [float(field.GetValue()) if field.GetValue().strip() else None
                       for field in (self.left_factor, self.right_factor)]
            if any(value is not None and not 1e-6 <= value <= 1e7 for value in factors):
                raise ValueError('Enter a positive voltage factor for each channel that you want to calibrate. Leave an uncalibrated channel blank.')
        except ValueError as error:
            self.controller.show_error('Could not save voltage calibration', error)
            return
        calibrations = [entry for entry in self.controller.settings['voltageCalibrations']
                        if (entry['device'], entry['rate']) != (device, rate)]
        if any(factors):
            calibrations.insert(0, {'device': device, 'rate': rate, 'left': factors[0], 'right': factors[1]})
            feedback = 'Voltage calibration saved for this input and sample rate.'
        elif self._calibration((device, rate)) and self.controller.confirm(
            'Remove the voltage calibration for this input and sample rate?', 'Remove voltage calibration'
        ):
            feedback = 'Voltage calibration removed for this input and sample rate.'
        else:
            return
        if self.controller.apply_settings(voltageCalibrations=calibrations):
            self._reference = feedback
            self._tell(self._reference, force=True)

    def _capture_reference(self):
        try:
            rate, frames, sequence, route, low, high = self._pcm()
            rms = band_rms(frames, rate, low, high)
            peak = np.max(np.abs(frames), axis=0)
            if np.any(rms < 1e-4) or np.any(peak >= .985):
                raise ValueError('Adjust the reference level so it can be measured on every active channel without clipping.')
        except ValueError as error:
            self.controller.show_error('Could not capture SNR reference', error)
            return
        self._reference_route = route
        self._reference_band = self._settings()
        self._reference_sequence = sequence
        self._noise_readings = []
        self.noise_button.Disable()
        self._reference_rms = rms
        levels = ', '.join(f'{name} {amplitude_db(value):.2f} dBFS' for name, value in
                           zip(('Left', 'Right') if len(rms) == 2 else ('Mono',), rms, strict=True))
        self._reference = f'Reference captured ({levels}). Remove the test signal and keep the same gain.'
        self._tell(self._reference, force=True)
        self._draw_history()

    def _capture_noise(self):
        try:
            rate, frames, sequence, route, low, high = self._pcm()
            if (self._reference_sequence is None or self._reference_route != route
                    or self._reference_band != self._settings()):
                raise ValueError('Capture a new reference using the current audio route and bandwidth.')
            if sequence - self._reference_sequence < len(frames):
                raise ValueError('Wait for a new observation window after capturing the reference.')
            if np.max(np.abs(frames)) >= .985:
                raise ValueError('The input clipped or nearly clipped while Fluttery measured the noise.')
            self._noise_readings = measure_noise(frames, rate, low, high)
        except ValueError as error:
            self.controller.show_error('Could not capture noise', error)
            return
        lines = []
        for channel, reading in enumerate(self._noise_readings):
            name = ('Left', 'Right')[channel] if len(self._noise_readings) == 2 else 'Mono'
            reference = self._reference_rms[channel]
            if not reading.rms or not reading.weighted_rms:
                lines.append(f'{name}: no measurable noise in this window. SNR unavailable. Check for digital muting.')
                continue
            lines.append(f'{name} SNR: {amplitude_db(reference) - amplitude_db(reading.weighted_rms):.2f} dB(A). '
                         f'Unweighted SNR: {amplitude_db(reference) - amplitude_db(reading.rms):.2f} dB. '
                         f'A-weighted noise: {amplitude_db(reading.weighted_rms):.2f} dBFS.')
        self._reference = '\n'.join(lines)
        self._tell(self._reference, force=True)
        self._draw_history()


class OfflineWavDialog(LabDialog):
    def __init__(self, parent, controller):
        super().__init__(parent, controller, 'Analyse Test-Tone WAV', LEVEL_HELP + '#analyse-a-test-tone-wav')
        self.path = None
        self.result = None
        self._worker = None
        self._cancel = Event()
        self._closed = False
        self._report_progress = 0.0
        self._manual_reference = f'{controller.settings["targetFreq"]:g}'
        self._reference_forced = False
        row = self._row()
        intro = wx.StaticText(self, label='Open an uncompressed WAV recording of a test tone.')
        describe(intro, 'Saved Fluttery sessions can be opened from the File menu.')
        row.Add(intro, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, self.FromDIP(10))
        self.clock_correction = wx.CheckBox(self, label='Use saved clock correction')
        describe(self.clock_correction, 'Apply Fluttery’s saved clock correction only if it applies to the interface that made the recording.')
        row.Add(self.clock_correction, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, self.FromDIP(6))
        row = self._row()
        self._action(row, 'Choose WAV…', 'Select a WAV with a test tone.', self._choose_file)
        self.file_label = self._field(row, 'Recording', 'File name and audio format.',
                                      wx.TextCtrl(self, style=wx.TE_READONLY, size=self.FromDIP((370, -1))))
        self.plot_view = self._choice(row, 'Plot view', 'Choose peak variation, RMS variation, speed error or the spectrum from the last period of reference lock.',
                                      ['Peak variation', 'RMS variation', 'Speed error', 'Modulation spectrum'])
        self.plot_view.Bind(wx.EVT_CHOICE, lambda event: self._render_result_plot())
        self.plot_view.Disable()
        row = self._row()
        self.channel = self._choice(row, 'Recorded channel', 'Choose the channel containing the reference tone.', [])
        self.start_time = self._number(row, 'Start (s)', 'Optional offset from the beginning of the WAV.', 0)
        self.duration = self._number(row, 'Duration (s)', 'Enter zero to analyse until the end.', 0)
        self.reference = self._number(row, 'Reference tone (Hz)', 'Expected tone on the recording.', controller.settings['targetFreq'])
        self.method = self._choice(row, 'Analysis mode', 'Select the detector or scrape-flutter band.',
                                   ['Current measurement settings', 'AES6 weighted peak', 'Unweighted', 'Scrape flutter'])
        self.method.Bind(wx.EVT_CHOICE, self._update_reference)
        self._update_reference(None)
        self.plot = self._plot('Speed variation in the recording', 'Time in recording (s)', 'Variation (%)')
        self._readout('WAV analysis results')
        row = self._actions(audio_settings=False)
        self._action(row, 'Measurement Settings…', 'Open the Measurement settings section.',
                     lambda: controller.navigate('settings', 'measurement'))
        self.run_button = self._action(row, 'Analyse', 'Analyse the selected part of the recording on the chosen channel.', self._analyse)
        self.cancel_button = self._action(row, 'Cancel Analysis', 'Stop processing the current file.', self._cancel.set)
        self.session_button = self._action(row, 'Use in Session…', 'Add these readings to the Fluttery session.', self._use)
        self.cancel_button.Disable()
        self.session_button.Disable()
        self.run_button.Disable()
        self._tell('Choose an uncompressed WAV recording.')
        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._tick, self.timer)
        self.timer.Start(300)
        self._finish_layout((825, 650))

    def shutdown(self):
        self.timer.Stop()
        self._closed = True
        self._cancel.set()

    def _choose_file(self):
        with wx.FileDialog(self, 'Open test tone', wildcard='Wave audio (*.wav;*.wave)|*.wav;*.wave',
                           style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as dialog:
            if dialog.ShowModal() == wx.ID_OK:
                self.open_file(dialog.GetPath())

    def _update_reference(self, event):
        mode = self.method.GetSelection()
        settings = self.controller.settings
        forced = (12500 if mode == 3 or (mode == 0 and settings['flutterBand'] == 'scrape') else
                  3150 if mode == 1 or (mode == 0 and settings['weightingStandard'] == 'aes6') else None)
        if not self._reference_forced:
            self._manual_reference = self.reference.GetValue()
        self._reference_forced = forced is not None
        self.reference.ChangeValue(str(forced) if forced is not None else self._manual_reference)
        self.reference.Enable(not self._reference_forced)

    def refresh_display_settings(self):
        super().refresh_display_settings()
        self._update_reference(None)

    def open_file(self, path):
        if self._worker is not None:
            self._tell('Cancel the current analysis before choosing another file.', force=True)
            return
        try:
            info = inspect_wav(path)
        except (OSError, ValueError) as error:
            self.controller.show_error('Could not open WAV', error)
            return
        self.path = Path(path)
        self.result = None
        self.session_button.Disable()
        self.plot_view.Disable()
        self.channel.Set([f'Channel {entry}' for entry in range(1, info.channels + 1)])
        self.channel.SetSelection(0)
        self.file_label.ChangeValue(f'{self.path.name}  •  {info.rate:,} Hz  •  {info.duration:.2f} s')
        self.start_time.ChangeValue('0')
        self.duration.ChangeValue('0')
        self.plot.clear()
        self.run_button.Enable()
        self._tell('Choose a channel. To analyse only part of the recording, set a start time and duration. Then choose Analyse.',
                   force=True)

    def _analyse(self):
        if self.path is None or self._worker is not None:
            return
        try:
            start, duration, frequency = map(float, (self.start_time.GetValue(), self.duration.GetValue(), self.reference.GetValue()))
            if not (np.isfinite(start) and np.isfinite(duration) and start >= 0 and duration >= 0 and 1 <= frequency <= 24000):
                raise ValueError('Enter a start time and duration of zero or more, and a reference frequency between 1 and 24,000 Hz.')
        except ValueError as error:
            self.controller.show_error('Check the analysis range', error)
            return
        settings = {**self.controller.settings, 'useAutoTarget': False, 'targetFreq': frequency}
        if not self.clock_correction.GetValue():
            settings['correctionFactor'] = 1.0
        selection = self.method.GetSelection()
        if selection in (1, 2, 3):
            settings['weightingStandard'] = ('aes6', 'unweighted', 'unweighted')[selection - 1]
            settings['flutterBand'] = 'scrape' if selection == 3 else 'rotational'
        self.result = None
        self.session_button.Disable()
        self.plot_view.Disable()
        self.plot.clear()
        self._cancel.clear()
        self._report_progress = 0.0
        channel = max(0, self.channel.GetSelection())
        path = self.path
        self.run_button.Disable()
        self.cancel_button.Enable()

        def analyse():
            try:
                result = analyse_wav(path, settings, channel=channel, start=start, duration=duration or None,
                                     cancelled=self._cancel.is_set, progress=self._update_progress,
                                     retain_input=settings['includeInputAudio'])
            except (OSError, ValueError, ArithmeticError) as error:
                wx.CallAfter(self._complete, None, str(error), settings)
            else:
                wx.CallAfter(self._complete, result, '', settings)

        self._worker = Thread(target=analyse, name='Fluttery offline WAV', daemon=True)
        self._worker.start()

    def _update_progress(self, value):
        self._report_progress = value

    def _tick(self, event):
        if self._worker is not None:
            self._tell(f'Analysing. {self._report_progress:.0%} complete.')

    def _complete(self, result, error, settings):
        if self._closed:
            return
        self._worker = None
        self.run_button.Enable()
        self.cancel_button.Disable()
        if result is None:
            self._tell(error or 'Analysis cancelled.', force=True)
            return
        self.result = result
        self.result_settings = settings
        self.session_button.Enable()
        self.plot_view.Enable()
        metrics = result['metrics']
        peak_label = ('scrape peak' if metrics.get('flutterBand') == 'scrape' else
                      'AES6 peak' if metrics.get('weighting') == 'aes6' else 'detector peak')
        peak = result['maximumPeak']
        peak_reading = f'{peak:.5f}%' if peak is not None else 'needs a longer locked signal'
        lines = [f'Channel {result["channel"]}, {result["rate"]:,} Hz. {result["duration"]:.2f} seconds analysed.',
                 f'{result["validSeconds"]:.2f} seconds of locked tone. Maximum {peak_label}: {peak_reading}.']
        for key, label, unit in (
            ('currentFreq', 'Detected tone', ' Hz'), ('rms', 'RMS', '%'), ('aes6Peak', 'AES6 two-sigma peak', '%'),
            ('rmsOneSecond', 'One-second RMS', '%'), ('driftRms', 'Drift RMS', '%'),
            ('scrapeRms', 'Scrape RMS', '%'), ('scrapePeak', 'Scrape peak', '%'),
            ('flutterPeriodHz', 'Dominant repetition', ' Hz'),
        ):
            if (value := metrics.get(key)) is not None:
                lines.append(f'{label}: {value:.5g}{unit}.')
        self._render_result_plot()
        self._tell('\n'.join(lines), force=True)

    def _render_result_plot(self):
        if self.result is None:
            return
        result, settings = self.result, self.result_settings
        view = self.plot_view.GetSelection()
        spectrum = result['spectrum']
        if view == 3:
            self.plot.set_labels('Modulation spectrum of the last locked signal', 'Modulation frequency (Hz)', 'Level (dB)')
            self.plot.set_default_view()
            self.plot.set_series([{'label': 'Modulation spectrum', 'x': spectrum.get('frequencies', ()),
                                   'y': spectrum.get('db', ())}], fit=True)
            return
        detectors = {
            0: (('scrapePeak', 'Scrape peak') if settings['flutterBand'] == 'scrape'
                else ('aes6Peak', 'AES6 weighted peak') if settings['weightingStandard'] == 'aes6'
                else ('peak', 'Detector peak')),
            1: ('rms', 'RMS variation'),
            2: ('speedDrift', 'Speed error'),
        }
        key, label = detectors[view]
        self.plot.set_labels(label + ' over the recording', 'Time in recording (s)', 'Variation (%)')
        start = result['start']
        self.plot.set_default_view(xlim=(start, start + result['duration']))
        history = result['history']
        y = [row[key] if row.get('locked') and row.get(key) is not None else np.nan for row in history]
        if not np.isfinite(y).any():
            self.plot.clear()
            return
        # Keep lines within each locked interval while leaving the original
        # time of every gap visible in the plotted and exported series.
        x = [row['timestamp'] + start for row in history]
        self.plot.set_series([{'label': label, 'x': x, 'y': y}], fit=True)

    def _use(self):
        if self.result is not None:
            self.controller.use_offline_result(self.path, self.result, self.result_settings)


class RawSpeedDialog(LabDialog):
    def __init__(self, parent, controller):
        super().__init__(parent, controller, 'Save Unweighted Speed WAV', LEVEL_HELP + '#save-raw-speed-as-wav')
        self.capture = None
        self.stopping = False
        self._label('Record changes in playback speed as a WAV file.', 'The file contains speed deviation before weighting.')
        self._readout('Speed WAV recording activity')
        row = self._actions()
        self.start_button = self._action(row, 'Start Saving…', 'Choose a destination and begin capturing raw speed.', self._start)
        self.stop_button = self._action(row, 'Finish and Save', 'Finish the WAV recording and save it.', self._stop)
        self.cancel_button = self._action(row, 'Discard', 'Discard the unfinished recording.', self._cancel)
        self.stop_button.Disable()
        self.cancel_button.Disable()
        self._tell('Start live input and play your reference tone. Choose Start Saving to pick a file and begin recording.')
        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._tick, self.timer)
        self.timer.Start(200)
        self._finish_layout((710, 205))

    def _start(self):
        if (not self or not self.IsShown() or self.capture is not None
                or not self.controller.ensure_audio(after_permission=self._start)):
            return
        with wx.FileDialog(self, 'Save unweighted speed', defaultFile='Fluttery Speed.wav',
                           wildcard='Wave audio (*.wav)|*.wav', style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            try:
                settings = self.controller.settings
                tape_end = (settings['tapeSilenceSeconds'], settings['tapeSilenceDb']) if settings['tapeAutoStop'] else None
                self.capture = self.controller.audio.begin_raw_speed_capture(dialog.GetPath(), tape_end=tape_end)
            except (OSError, ValueError) as error:
                self.controller.show_error('Could not start speed capture', error)
                return
        self.stopping = False
        self.start_button.Disable()
        self.stop_button.Enable()
        self.cancel_button.Enable()
        self._tell('Recording speed variation.', force=True)

    def _stop(self):
        if self.capture is not None:
            self.stopping = True
            self.controller.audio.finish_raw_speed_capture(self.capture)
            self.stop_button.Disable()
            self.cancel_button.Disable()

    def _cancel(self):
        if self.capture is not None:
            self.stopping = True
            self.controller.audio.finish_raw_speed_capture(self.capture, cancel=True)
            self._tell('Discarding capture.', force=True)
            self.stop_button.Disable()
            self.cancel_button.Disable()

    def _tick(self, event):
        capture = self.capture
        if capture is None:
            return
        if capture.done.is_set():
            self.controller.audio.finish_raw_speed_capture(capture)
            self.capture = None
            self.start_button.Enable()
            self.stop_button.Disable()
            self.cancel_button.Disable()
            if capture.error:
                text = f'Capture ended. {capture.error}'
            elif capture.saved:
                text = f'Saved {capture.seconds:.2f} seconds to {capture.target.name}.'
                if capture.ended_at_tape:
                    text = f'The tape’s test signal ended. {text}'
            else:
                text = 'Capture discarded.'
            self._tell(text, force=True)
        elif not self.stopping:
            metrics = self.controller.pages['measure'].snapshot.get('metrics', {})
            frequency = metrics.get('activeTarget')
            lock_status = 'Locked' if metrics.get('locked') else 'Waiting for a locked tone'
            reference = f'{lock_status} on {frequency:g} Hz.' if frequency else f'{lock_status}.'
            self._tell(f'Recording speed variation. {capture.seconds:.1f} seconds.\n{reference}')

    def shutdown(self):
        self.timer.Stop()
        if self.capture is not None and not self.stopping:
            self.controller.audio.finish_raw_speed_capture(self.capture)

    def _close(self, event):
        if self.capture is not None and not self.stopping:
            with wx.MessageDialog(self, 'Save the unweighted speed variation recorded so far?', self.GetTitle(),
                                  wx.YES_NO | wx.CANCEL | wx.ICON_QUESTION) as dialog:
                dialog.SetYesNoCancelLabels('Finish and Save', 'Discard', 'Keep Recording')
                result = dialog.ShowModal()
            if result == wx.ID_CANCEL:
                return
            if result == wx.ID_YES:
                self._stop()
            else:
                self._cancel()
        super()._close(event)
