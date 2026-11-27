"""Standalone spectrum, two-tone and stepped distortion instruments."""

from __future__ import annotations

from dataclasses import dataclass
from threading import Thread

import numpy as np
import wx

from .audio.bench import (
    IMD_MODES,
    STEP_END_GUARD_SECONDS,
    SpectrumAverage,
    StepStimulus,
    analyse_imd_capture,
    analyse_steps,
)
from .audio.response import LEAD_SECONDS
from .audio.scope import SCOPE_HISTORY_SECONDS
from .lab_ui import LabDialog

MODES = ('Spectrum', 'Two-tone intermodulation', 'THD versus frequency', 'THD versus level')
WINDOWS = ('hann', 'blackmanharris', 'flattop')
NAMES = ('Left', 'Right')


@dataclass(frozen=True)
class BenchTest:
    view: int
    seconds: float
    imd_mode: str
    first: float
    second: float
    stimulus: StepStimulus | None


class BenchAnalyzerDialog(LabDialog):
    def __init__(self, parent, controller):
        super().__init__(parent, controller, 'Spectrum and Distortion Tests', '06-signals.md#measure-spectrum-and-intermodulation')
        self.average = SpectrumAverage()
        self.live = False
        self.capture = None
        self.request = None
        self.processing = False
        self.result = None
        self._view = 0
        self._step_ranges = {2: ('40', '10000'), 3: ('-60', '-6')}
        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._tick, self.timer)

        row = self._row()
        self.mode = self._choice(row, 'Instrument', 'View the input spectrum or choose a two-tone IMD or stepped distortion test.', MODES)
        self.mode.Bind(wx.EVT_CHOICE, self._mode_changed)
        self.fft_fields = self._row()
        self.window = self._choice(self.fft_fields, 'FFT window', 'Control spectral leakage and noise bandwidth.',
                                   ('Hann', 'Blackman–Harris', 'Flat top'))
        self.average_count = self._choice(self.fft_fields, 'Average blocks', 'Choose how many blocks to average in linear power.',
                                         ('1', '4', '8', '16', '32'))
        self.average_count.SetSelection(2)
        self.fft_size = self._choice(self.fft_fields, 'FFT samples', 'More samples give finer frequency resolution.',
                                     ('4096', '8192', '16384', '32768', '65536', '131072'))
        self.fft_size.SetSelection(2)
        self.hold = self._action(self.fft_fields, 'Peak Hold', 'Show or hide the peak trace since the most recent reset.',
                                 self._toggle_peak)
        # wxMSW retains the initial button width after SetLabel. Reserve enough
        # space for the longer label so the action stays readable in both states.
        self.hold.SetLabel('Hide Peak Hold')
        self.hold.InvalidateBestSize()
        self.hold.SetMinSize(self.hold.GetBestSize())
        self.hold.SetLabel('Peak Hold')
        self.hold_visible = False
        self._action(self.fft_fields, 'Reset Average and Peak', 'Clear accumulated spectrum data.', self._reset_spectrum)

        self.imd_fields = self._row()
        self.method = self._choice(self.imd_fields, 'IMD method', 'Choose the calculation for the two input tones.',
                                   ('SMPTE modulation', 'DIN modulation', 'CCIF difference tone'))
        self.method.Bind(wx.EVT_CHOICE, self._imd_preset)
        self.first = self._number(self.imd_fields, 'First tone (Hz)', 'Enter the lower frequency on the test source.', 60)
        self.second = self._number(self.imd_fields, 'Second tone (Hz)', 'Enter the upper frequency on the test source.', 7000)
        self.imd_level = self._number(self.imd_fields, 'Peak output (dBFS)', 'Level for the generated tone pair.', -24)

        self.step_fields = self._row()
        self.start = self._number(self.step_fields, 'Start', 'Start frequency in Hz or level in peak dBFS.', 40)
        self.end = self._number(self.step_fields, 'End', 'End frequency in Hz or level in peak dBFS.', 10000)
        self.count = self._number(self.step_fields, 'Steps', 'Choose between 2 and 60 test frequencies or levels.', 16)
        self.fixed = self._number(self.step_fields, 'Fixed level (dBFS)', 'Peak output held throughout the frequency test.', -20)
        self.fixed_frequency = self._number(self.step_fields, 'Fixed tone (Hz)', 'Frequency held throughout the level test.', 997)
        self.capture_fields = self._row()
        self.observe = self._number(self.capture_fields, 'Observation per step (s)',
                                    'Allow more time to measure low frequencies or very low levels of distortion.', 1)
        self.settle = self._number(self.capture_fields, 'Settling time (s)', 'Discard this interval after each step.', .25)
        self.plot = self._plot('Input spectrum', 'Frequency (Hz)', 'Density (dBFS/Hz)')
        self._readout('Bench measurements')
        footer = self._actions()
        self.measure = self._action(footer, 'Start Spectrum', 'Start live input and display the selected measurement.',
                                    self._start_input)
        self.generate = self._action(footer, 'Generate and Measure', 'Play the configured test signal and capture its return.',
                                     self._start_generated)
        self.stop = self._action(footer, 'Stop', 'Stop the current measurement or freeze the spectrum.', self._stop)
        self.save = self._action(footer, 'Save to Session', 'Keep the displayed result in the current session.', self._store)
        self._mode_changed(None)
        self._finish_layout((890, 665))
        self.timer.Start(150)

    def _mode_changed(self, _event):
        self._stop()
        view = self.mode.GetSelection()
        if self._view in self._step_ranges:
            self._step_ranges[self._view] = self.start.GetValue(), self.end.GetValue()
        self._view = view
        self.result = None
        self._reset_spectrum()
        self.content.Show(self.fft_fields, view == 0, recursive=True)
        self.content.Show(self.imd_fields, view == 1, recursive=True)
        self.content.Show(self.step_fields, view in (2, 3), recursive=True)
        self.content.Show(self.capture_fields, view != 0, recursive=True)
        self.measure.Show(view in (0, 1))
        self.measure.SetLabel('Start Spectrum' if view == 0 else 'Measure Input')
        self.generate.Show(view != 0)
        self.step_fields.Show(self.fixed.GetContainingSizer(), view == 2, recursive=True)
        self.step_fields.Show(self.fixed_frequency.GetContainingSizer(), view == 3, recursive=True)
        if view in (2, 3):
            start, end = self._step_ranges[view]
            self.start.ChangeValue(start)
            self.end.ChangeValue(end)
        self.plot.log_x = view != 3
        self.plot.set_labels(MODES[view], 'Peak output (dBFS)' if view == 3 else 'Frequency (Hz)',
                             ('Density (dBFS/Hz)', 'Product level (dBc)', 'Distortion (%)', 'Distortion (%)')[view])
        self.plot.clear()
        connection = 'Connect the interface’s output to the device’s input, then connect the device’s output to the interface’s input. '
        self._tell(('Choose Start Spectrum to see the input level by frequency. Turn on Peak Hold to keep the highest readings.',
                    'Choose Measure Input for your own two-tone source, or Generate and Measure to use Fluttery’s test tones.',
                    connection + 'Set the range of frequencies and the output level.',
                    connection + 'Set the range of output levels and the test frequency.')[view])
        self._enable()
        self.Layout()

    def _imd_preset(self, _event):
        first, second, _ = tuple(IMD_MODES.values())[self.method.GetSelection()]
        self.first.ChangeValue(f'{first:g}')
        self.second.ChangeValue(f'{second:g}')

    def _enable(self):
        active = self.capture is not None or self.request is not None or self.processing
        self.mode.Enable(not active)
        self.measure.Enable(not active and not self.live)
        self.generate.Enable(not active)
        self.stop.Enable(self.live or self.capture is not None or self.request is not None)
        self.save.Enable(self.result is not None and not active)

    def _reset_spectrum(self):
        self.average.reset()
        self.plot.clear()

    def _toggle_peak(self):
        self.hold_visible = not self.hold_visible
        self.hold.SetLabel('Hide Peak Hold' if self.hold_visible else 'Peak Hold')
        self._spectrum(force=True)

    def _spectrum(self, *, force=False):
        if self.mode.GetSelection() != 0 or not (self.live or force):
            return
        if self.live or not self.average.count:
            length = int(self.fft_size.GetStringSelection())
            buffer = self.controller.audio.scope_frames(SCOPE_HISTORY_SECONDS)
            if length > int(buffer.rate * SCOPE_HISTORY_SECONDS):
                self._tell(f'{length:,} FFT samples exceed the capture window at {buffer.rate:,.0f} Hz. Choose fewer samples.')
                return
            if len(buffer.samples) < length:
                self._tell(f'Waiting for {length:,} contiguous input samples.')
                return
            try:
                ready = self.average.consume(buffer, length, window=WINDOWS[self.window.GetSelection()],
                                             average=int(self.average_count.GetStringSelection()))
            except ValueError as error:
                self._tell(str(error))
                return
            if not (ready or force and self.average.count):
                return
        x, average, hold = self.average.values()
        channels = min(self.controller.settings['channels'], average.shape[1])
        traces = [{'label': f'{NAMES[index]} density', 'x': x[1:], 'y': average[1:, index]}
                  for index in range(channels)]
        if self.hold_visible:
            traces.extend({'label': f'{NAMES[index]} peak', 'x': x[1:], 'y': hold[1:, index], 'style': 'dashed'}
                          for index in range(channels))
        self.plot.set_series(traces)
        strongest = int(np.argmax(average[1:, 0])) + 1
        blocks = '1 block' if self.average.count == 1 else f'{self.average.count:,} blocks'
        resolution = self.average.rate / self.average.length
        self._tell(f'{blocks} averaged. Bin spacing {resolution:.3g} Hz. '
                   f'Strongest left band {x[strongest]:.3f} Hz, {average[strongest, 0]:.2f} dBFS per Hz.')

    def _start_input(self):
        if self.mode.GetSelection() == 0:
            if self.request is not None or self.live:
                return
            ticket = self.request = object()

            def start_spectrum():
                if not self._wants_capture(ticket):
                    return
                self.live = self.controller.ensure_audio(after_permission=start_spectrum)
                if self.live or not self.controller._microphone_pending:
                    self.request = None
                self._enable()

            self._tell('Opening the selected audio devices…')
            self._enable()
            start_spectrum()
        else:
            self._start_test(False)

    def _start_generated(self):
        self._start_test(True)

    def _parameters(self, generated):
        view = self.mode.GetSelection()
        observe = float(self.observe.GetValue())
        settle = float(self.settle.GetValue())
        if not .25 <= observe <= 10 or not .10 <= settle <= 5:
            raise ValueError('Set the observation time to 0.25–10 seconds and the settling time to 0.10–5 seconds.')
        if view == 1:
            mode = tuple(IMD_MODES)[self.method.GetSelection()]
            first, second = float(self.first.GetValue()), float(self.second.GetValue())
            if not 5 <= first < second:
                raise ValueError('Enter the lower tone frequency first and the higher frequency second.')
            stimulus = (StepStimulus(((first, float(self.imd_level.GetValue())),), settle, observe,
                                     second, IMD_MODES[mode][2]) if generated else None)
            return BenchTest(view, observe, mode, first, second, stimulus)
        first, last = float(self.start.GetValue()), float(self.end.GetValue())
        count = int(self.count.GetValue())
        if not 2 <= count <= 60 or not first < last or count * (settle + observe + STEP_END_GUARD_SECONDS) > 115:
            raise ValueError('Set the end value above the start value. Use 2–60 steps and keep the total time at or below 115 seconds.')
        if view == 2:
            if not 20 <= first < last <= 20000:
                raise ValueError('Enter a frequency range from 20 to 20,000 Hz.')
            steps = tuple((float(f), float(self.fixed.GetValue())) for f in np.geomspace(first, last, count))
        else:
            fixed = float(self.fixed_frequency.GetValue())
            if not 20 <= fixed <= 20000:
                raise ValueError('Enter a fixed frequency from 20 to 20,000 Hz.')
            steps = tuple((fixed, float(level)) for level in np.linspace(first, last, count))
        return BenchTest(view, observe, '', 0, 0, StepStimulus(steps, settle, observe))

    def _start_test(self, generated):
        if self.request is not None or self.capture is not None or self.processing:
            return
        try:
            test = self._parameters(generated)
            duration = test.seconds if test.stimulus is None else test.stimulus.total_seconds + LEAD_SECONDS + .9
            ticket = object()
            self.request = ticket
            self.result = None
            self.plot.clear()
            self._tell('Opening the selected audio devices…')
            self._enable()
            def started(capture):
                self._started(ticket, test, capture)

            capture = self.controller.bench_start_capture(
                duration, stimulus=test.stimulus, on_ready=started, still_wanted=lambda: self._wants_capture(ticket))
            if capture is not None:
                started(capture)
            elif not self.controller._microphone_pending:
                self.request = None
                self._tell('Capture did not start. Check the devices selected in Audio Settings.')
        except (OSError, RuntimeError, ValueError) as error:
            self.request = None
            self._tell(str(error))
        self._enable()

    def _wants_capture(self, ticket):
        return bool(self) and self.request is ticket and self.IsShown()

    def _started(self, ticket, test, capture):
        if not self._wants_capture(ticket):
            if capture is not None:
                self.controller.bench_cancel_capture(capture)
            return
        self.request = None
        if capture is None:
            self._tell('Could not start the capture. Check the app’s microphone permission and the selected audio devices.')
            self._enable()
            return
        self.capture = capture
        self.test = test
        self._enable()

    def _tick(self, _event):
        if self.request is not None and not self.controller._microphone_pending:
            self.request = None
            self._tell('Could not start the capture. Check the app’s microphone permission and the selected audio devices.')
            self._enable()
        self._spectrum()
        if self.capture is None:
            return
        self.capture.abort_stalled_input()
        if self.capture.active:
            self._tell(f'Capturing {self.capture.seconds:.1f} of {self.capture.duration:.1f} seconds.')
            return
        capture, self.capture = self.capture, None
        self.controller.bench_finish_capture(capture)
        if capture.error:
            self._tell(capture.error)
            self._enable()
            return
        test = self.test
        self.processing = True
        self._enable()
        self._tell('Analysing the capture…')

        def compute():
            try:
                values = capture.read()
                if test.view == 1:
                    rows = analyse_imd_capture(values, capture.input_rate, test.first, test.second, mode=test.imd_mode,
                                               stimulus=test.stimulus, reference=capture.reference, output_rate=capture.output_rate)
                else:
                    rows, _, _ = analyse_steps(capture.reference, values, capture.output_rate, capture.input_rate, test.stimulus)
                wx.CallAfter(self._complete, test, rows, '')
            except (ValueError, ArithmeticError, np.linalg.LinAlgError) as error:
                wx.CallAfter(self._complete, test, [], str(error))

        Thread(target=compute, name='Fluttery distortion analysis', daemon=True).start()

    def _complete(self, test, rows, error):
        if not self or self.IsBeingDeleted():
            return
        self.processing = False
        if error:
            self._tell(error)
        elif test.view == 1:
            traces, summary = [], []
            for index, row in enumerate(rows):
                if row.get('error'):
                    summary.append(f'{NAMES[index]} unavailable. {row["error"]}')
                    continue
                products = sorted(row['products'], key=lambda item: item['frequencyHz'])
                traces.append({'label': NAMES[index], 'x': [p['frequencyHz'] for p in products],
                               'y': [p['levelDbc'] for p in products], 'style': 'points'})
                value = row['imdPercent']
                limited = row.get('imdLimited')
                status = ('input clipping' if row['clipping'] else 'some products unavailable' if row['incomplete']
                          else 'near the noise floor' if limited else 'measured')
                formatted = ('unavailable' if value is None else f'below {row["imdLimitPercent"]:.5g}%'
                             if limited else f'{value:.5g}%')
                summary.append(f'{NAMES[index]} {formatted} ({status}). Reference tones {row["firstHz"]:.3f} and {row["secondHz"]:.3f} Hz.')
                summary.extend(f'{p["product"]}: {p["levelDbc"]:.2f} dBc' + (' (near floor)' if p['limited'] else '')
                               for p in products)
            self.plot.set_series(traces, fit=True)
            self._tell('\n'.join(summary))
            self.result = ('bench-imd', {'method': test.imd_mode, 'data': rows})
        else:
            axis_key = 'frequencyHz' if test.view == 2 else 'levelDbfs'
            traces = []
            for channel in sorted({row['channel'] for row in rows}):
                readings = [row for row in rows if row['channel'] == channel]
                for key, label in (('thd', 'THD'), ('thdn', 'THD+N')):
                    traces.append({'label': f'{NAMES[channel - 1]} {label}', 'x': [r[axis_key] for r in readings],
                                   'y': [r[key] if r[key] is not None else np.nan for r in readings],
                                   'style': 'dashed' if key == 'thdn' else 'line'})
            self.plot.set_series(traces, fit=True)
            measured = sum(row['thdn'] is not None for row in rows)
            self._tell(f'{measured} of {len(rows)} channel readings are usable. '
                       'The plot leaves gaps where the return signal was missing or clipped. Select a point to read its value.')
            self.result = ('bench-distortion', {'axis': axis_key, 'data': rows})
        self._enable()

    def _store(self):
        if self.result is not None:
            kind, payload = self.result
            self.controller.record_bench_result(kind, payload)
            self._tell('Measurement saved in Session Results.')

    def _stop(self):
        self.live = False
        self.request = None
        if self.capture is not None:
            self.controller.bench_cancel_capture(self.capture)
            self.capture = None
        if hasattr(self, 'save'):
            self._enable()

    def shutdown(self):
        self.timer.Stop()
        self._stop()
