"""Capture native manual illustrations without using live audio or preferences.

Run from a desktop session with ``python -m scripts.capture_manual``.
On macOS the terminal may need Screen Recording permission. Linux requires
an X11 window manager, xprop and xwininfo.
"""

from __future__ import annotations

import argparse
import ctypes
import io
import os
import plistlib
import re
import subprocess
import sys
import tempfile
from ctypes import wintypes
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import wx
from PIL import Image, ImageCms, ImageGrab

from fluttery import app, preferences
from fluttery.audio import AudioEngine
from fluttery.sessions import new_session, save_session
from fluttery.settings_ui import device_identifier

RATE = 48_000
REFERENCE = 3150
RECORDING_TIME = 1_784_080_800
PLATFORM_NAMES = {'darwin': 'macos', 'linux': 'linux', 'win32': 'windows'}
HOST_APIS = {'darwin': 'Core Audio', 'linux': 'ALSA', 'win32': 'WASAPI'}
DEVICE = {
    'id': 0, 'name': 'Example USB Audio Interface', 'host_api': HOST_APIS.get(sys.platform, 'Audio'),
    'input_channels': 2, 'output_channels': 2, 'sample_rate': RATE,
}
RENDER_WAIT_MS = 220


def screenshot_for_manual(image):
    # Convert display-specific profiles before dropping the PNG metadata. A
    # native macOS capture can carry an iCCP chunk that wx's PNG reader warns about.
    if profile := image.info.get('icc_profile'):
        return ImageCms.profileToProfile(image, io.BytesIO(profile), ImageCms.createProfile('sRGB'),
                                         outputMode=image.mode)
    return image


class SyntheticInput(AudioEngine):
    """Use the real processor and app commands with an in-memory input route."""

    def devices(self):
        return [DEVICE]

    @property
    def running(self):
        return self._input_stream is not None or self._using_internal_source()

    def start(self, input_device=None, output_device=None, sample_rate=None, channels=None,
              *, input_enabled=True, output_enabled=True):
        if output_enabled:
            raise RuntimeError('Manual screenshots use silent internal loopback.')
        self._input_stream = SimpleNamespace(samplerate=sample_rate or RATE) if input_enabled else None
        return True

    def stop(self):
        self._input_stream = None

    def snapshot(self):
        snapshot = super().snapshot()
        if self.input_enabled:
            snapshot['health']['analysisMessages'] = self.processor.sequence
        return snapshot


class MacWindowCapture:
    """Capture this process's front Fluttery window with the native title bar."""

    def __init__(self):
        self.graphics = ctypes.CDLL('/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics')
        self.foundation = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        pointer = ctypes.c_void_p
        self.graphics.CGWindowListCopyWindowInfo.argtypes = (ctypes.c_uint32, ctypes.c_uint32)
        self.graphics.CGWindowListCopyWindowInfo.restype = pointer
        self.foundation.CFPropertyListCreateData.argtypes = (pointer, pointer, ctypes.c_long, ctypes.c_ulong, pointer)
        self.foundation.CFPropertyListCreateData.restype = pointer
        self.foundation.CFDataGetLength.argtypes = (pointer,)
        self.foundation.CFDataGetLength.restype = ctypes.c_long
        self.foundation.CFDataGetBytePtr.argtypes = (pointer,)
        self.foundation.CFDataGetBytePtr.restype = pointer
        self.foundation.CFRelease.argtypes = (pointer,)
        self.foundation.CFRelease.restype = None

    def window_id(self, window):
        windows = self.graphics.CGWindowListCopyWindowInfo(1, 0)
        data = None
        try:
            data = self.foundation.CFPropertyListCreateData(None, windows, 100, 0, None)
            if not data:
                raise RuntimeError('Could not read the native window list.')
            payload = ctypes.string_at(self.foundation.CFDataGetBytePtr(data), self.foundation.CFDataGetLength(data))
            size = window.GetSize()
            position = window.GetScreenPosition()
            # wx and Core Graphics can disagree by the title bar height for
            # native modal windows. Use the nearest geometry among our windows.
            candidates = []
            for record in plistlib.loads(payload):
                bounds = record.get('kCGWindowBounds', {})
                if record.get('kCGWindowOwnerPID') != os.getpid() or bounds.get('Width', 0) < 300:
                    continue
                distance = (abs(bounds['X'] - position.x) + abs(bounds['Y'] - position.y)
                            + abs(bounds['Width'] - size.width) + abs(bounds['Height'] - size.height))
                if record.get('kCGWindowName') == window.GetTitle():
                    return record['kCGWindowNumber']
                candidates.append((distance, record['kCGWindowNumber']))
            if candidates:
                return min(candidates)[1]
        finally:
            if data:
                self.foundation.CFRelease(data)
            if windows:
                self.foundation.CFRelease(windows)
        raise RuntimeError(f'Could not locate the window for {window.GetTitle()!r}.')

    def image(self, window, scratch):
        raw = scratch / 'window.png'
        raw.unlink(missing_ok=True)
        subprocess.run(['screencapture', '-x', '-o', '-l', str(self.window_id(window)), str(raw)], check=True)
        return Image.open(raw)


class LinuxWindowCapture:
    """Capture the real GTK window and its window-manager frame on X11."""

    @staticmethod
    def _xprop(*args):
        return subprocess.check_output(['xprop', *args], text=True)

    def _window_id(self, window):
        focused = re.findall(r'0x[0-9a-f]+', self._xprop('-root', '_NET_ACTIVE_WINDOW'))
        listed = re.findall(r'0x[0-9a-f]+', self._xprop('-root', '_NET_CLIENT_LIST_STACKING'))
        owned = []
        for identifier in dict.fromkeys([*focused, *reversed(listed)]):
            if int(identifier, 16) == 0:
                continue
            values = self._xprop('-id', identifier, '-f', '_NET_WM_NAME', '8u', '_NET_WM_PID', '_NET_WM_NAME')
            if f'= {os.getpid()}\n' not in values:
                continue
            if window.GetTitle() in values:
                return identifier
            owned.append(identifier)
        if owned and owned[0] in focused:
            return owned[0]
        raise RuntimeError(f'Cannot locate a Fluttery X11 window titled {window.GetTitle()!r}.')

    @staticmethod
    def _bounds(identifier):
        hierarchy = subprocess.check_output(['xwininfo', '-id', identifier, '-tree'], text=True)
        root = re.search(r'Root window id: (0x[0-9a-f]+)', hierarchy)
        parent = re.search(r'Parent window id: (0x[0-9a-f]+)', hierarchy)
        if root and parent and parent[1] != root[1]:
            identifier = parent[1]  # Openbox reparents each GTK client into a decorated frame.
        details = subprocess.check_output(['xwininfo', '-id', identifier], text=True)
        fields = ('Absolute upper-left X', 'Absolute upper-left Y', 'Width', 'Height')
        left, top, width, height = (int(re.search(rf'^\s*{key}:\s+(-?\d+)', details, re.MULTILINE)[1]) for key in fields)
        return left, top, left + width, top + height

    def image(self, window, _scratch):
        return ImageGrab.grab(bbox=self._bounds(self._window_id(window)), xdisplay=os.environ['DISPLAY'])


class WindowsWindowCapture:
    @staticmethod
    def image(window, _scratch):
        handle = window.GetHandle()
        user = ctypes.WinDLL('user32')
        user.GetForegroundWindow.argtypes = ()
        user.GetForegroundWindow.restype = wintypes.HWND
        user.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
        user.GetWindowThreadProcessId.restype = wintypes.DWORD
        user.GetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int)
        user.GetWindowLongW.restype = ctypes.c_long
        user.SetWindowPos.argtypes = (wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                      ctypes.c_int, ctypes.c_int, wintypes.UINT)
        user.SetWindowPos.restype = wintypes.BOOL
        owner = wintypes.DWORD()
        user.GetWindowThreadProcessId(handle, ctypes.byref(owner))
        if owner.value != os.getpid():
            raise RuntimeError('Manual captures are limited to this process\'s own windows.')
        if user.GetForegroundWindow() != handle:
            window.Raise()
        # Pillow's HWND capture can omit the menu and title on Windows 11.
        # Desktop Window Manager gives the visible frame in physical pixels.
        manager = ctypes.WinDLL('dwmapi')
        visible_bounds = 9  # DWMWA_EXTENDED_FRAME_BOUNDS
        manager.DwmGetWindowAttribute.argtypes = (wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD)
        manager.DwmGetWindowAttribute.restype = ctypes.c_long
        manager.DwmFlush.argtypes = ()
        manager.DwmFlush.restype = ctypes.c_long
        bounds = wintypes.RECT()
        if manager.DwmGetWindowAttribute(handle, visible_bounds, ctypes.byref(bounds), ctypes.sizeof(bounds)) != 0:
            raise RuntimeError(f'Cannot read the visible bounds of {window.GetTitle()!r}.')
        restore_order = False
        if user.GetForegroundWindow() != handle:
            # A scheduled capture can be allowed to show its app but denied
            # foreground activation. Put only our verified window on top.
            was_topmost = user.GetWindowLongW(handle, -20) & 0x8  # GWL_EXSTYLE and WS_EX_TOPMOST
            move_flags = 0x1 | 0x2 | 0x10 | 0x40  # No resize/move/activation, show the window.
            if not user.SetWindowPos(handle, -1, 0, 0, 0, 0, move_flags):
                raise RuntimeError(f'Cannot raise {window.GetTitle()!r} for capture.')
            restore_order = not was_topmost
        try:
            manager.DwmFlush()
            return ImageGrab.grab(bbox=(bounds.left, bounds.top, bounds.right, bounds.bottom), all_screens=True)
        finally:
            if restore_order:
                user.SetWindowPos(handle, -2, 0, 0, 0, 0, move_flags)  # HWND_NOTOPMOST


def tone(frequency, *, seconds=2, gains=(0.43, 0.39), phase_difference=0.0, harmonics=(0, 0), noise=0):
    time = np.arange(round(RATE * seconds)) / RATE
    phase = 2 * np.pi * frequency * time[:, None] + np.array([0, phase_difference])
    waveform = (np.sin(phase) + np.array(harmonics) * np.sin(2 * phase)) * np.array(gains)
    if noise:
        waveform += np.random.default_rng(37).normal(0, noise, waveform.shape)
    return waveform.astype(np.float32)


def groove(*, resonance=False):
    time = np.arange(4 * RATE) / RATE
    rumble = 0.0012 * np.sin(2 * np.pi * 9.3 * time) + 0.0008 * np.sin(2 * np.pi * 22 * time)
    if resonance:
        rumble += 0.047 * np.sin(2 * np.pi * 10.25 * time) + 0.008 * np.sin(2 * np.pi * 14.75 * time)
    stereo_noise = np.random.default_rng(68).normal(0, 0.0008, (len(time), 2))
    return (rumble[:, None] + stereo_noise).astype(np.float32)


def flutter_sample(*, before=False):
    time = np.arange(16 * RATE) / RATE
    flutter = (0.0007 * np.sin(2 * np.pi * 4 * time)
               + 0.0011 * np.sin(2 * np.pi * (9 * time + 0.25 * time ** 2))
               + 0.00045 * np.sin(2 * np.pi * 0.7 * time))
    drift = 0.0014 + 0.0005 * np.sin(2 * np.pi * 0.14 * time)
    frequency = REFERENCE * (1 + drift + flutter * (1.8 if before else 1))
    phase = np.cumsum(frequency * (2 * np.pi / RATE))
    return np.column_stack((0.49 * np.sin(phase), 0.46 * np.sin(phase - 0.06))).astype(np.float32)


def flutter_diagnostic_sample(carrier, modulation, overtone):
    time = np.arange(23 * RATE) / RATE
    # Show the principal repeating component, a smaller independent one, a
    # slow variation and a positive speed offset in the same synthetic run.
    deviations = ((0.19, 0.00035), (modulation, 0.001), (overtone, 0.00018))
    phase = 2 * np.pi * carrier * 1.0017 * time
    for frequency, depth in deviations:
        phase += carrier * depth / frequency * (1 - np.cos(2 * np.pi * frequency * time))
    return np.column_stack((0.4 * np.sin(phase), 0.38 * np.sin(phase - 0.06))).astype(np.float32)


class ManualExamples:
    def __init__(self, desktop, output, scratch):
        self.desktop = desktop
        self.output = output
        self.scratch = scratch
        self.capture = {'darwin': MacWindowCapture, 'linux': LinuxWindowCapture, 'win32': WindowsWindowCapture}[sys.platform]()
        self.manual_dialog = None
        self.error = None
        self.index = 0
        self.audio = SyntheticInput()
        device = device_identifier(DEVICE, 1)
        self.frame = app.FlutteryFrame(audio=self.audio, settings={
            'colorMode': 'light', 'enableUISounds': False, 'announceStatus': False,
            'autoStartInput': False, 'showHelpTags': False, 'channels': 2, 'targetFreq': REFERENCE, 'sampleRate': RATE,
            'enableQC': True, 'qcLimit': 0.15, 'inputDevice': device, 'outputDevice': device,
        }, automatic_updates=False)
        self.frame.timer.Stop()
        desktop.SetTopWindow(self.frame)

    def run(self, *, manual_only=False, sweeps_only=False, meters_only=False, bench_only=False,
            response_only=False, plots_only=False, instruments_only=False, results_only=False, setup_only=False):
        limited = any((manual_only, sweeps_only, meters_only, bench_only, response_only, plots_only,
                       instruments_only, results_only))
        self.frame.show_startup(offer_setup=not limited)
        self.setup_only = setup_only
        if sys.platform == 'linux':
            # Xvfb starts with the pointer over the centre of the instrument.
            # Move it off the windows so GTK doesn't paint transient hover UI.
            position = self.frame.ScreenToClient((1, 1))
            self.frame.WarpPointer(position.x, position.y)
        # GTK on an emulated machine can map the first native dialog before
        # its contents have painted, particularly on the first font load.
        action = self._show_manual if manual_only else self._wizard_step
        if sweeps_only:
            self.views = iter(self._sweep_views())
            action = self._show_next
        elif meters_only:
            self.views = iter(self._meter_views())
            action = self._show_next
        elif bench_only:
            self.views = self._bench_sequence(include_distortion=True)
            action = self._show_next
        elif response_only:
            self.views = iter(self._response_views())
            action = self._show_next
        elif plots_only:
            self.views = self._plot_views()
            action = self._show_next
        elif instruments_only:
            self.views = self._instrument_views()
            action = self._show_next
        elif results_only:
            self.views = self._results_views(include_gap=True)
            action = self._show_next
        self._schedule(action, delay=750)
        self.desktop.MainLoop()
        if self.error is not None:
            raise self.error

    def _schedule(self, action, *args, delay=RENDER_WAIT_MS):
        def run_event():
            try:
                action(*args)
            except Exception as error:  # noqa: BLE001 - surface capture failures after the native loop closes
                self._finish(error)

        wx.CallLater(delay, run_event)

    def _finish(self, error=None):
        self.error = error
        if self.manual_dialog and self.manual_dialog.IsModal():
            self.manual_dialog.EndModal(wx.ID_CANCEL)
        if wizard := self.frame._setup_wizard:
            wizard.EndModal(wx.ID_CANCEL)
        self.frame.is_measuring = False
        self.frame.session_saved()
        wx.CallAfter(self.frame.quit)

    def _wizard_step(self):
        wizard = self.frame._setup_wizard
        choices = {0: 'wizard-welcome', 1: 'wizard-updates', wizard.first_step('graphs'): 'wizard-graphs',
                   wizard.total_steps - 1: 'wizard-finish'}
        step = wizard.step
        if name := choices.get(step):
            self._save_image(wizard, name)
        is_final = step + 1 == wizard.total_steps
        wizard.next.Command(wx.CommandEvent(wx.EVT_BUTTON.typeId, wizard.next.GetId()))
        if not is_final and wizard.step == step:
            raise RuntimeError('The setup wizard did not advance.')
        after_setup = self._finish if self.setup_only else self._prepare_views
        self._schedule(after_setup if is_final else self._wizard_step)

    def _prepare_views(self):
        self.audio.start(output_enabled=False)
        self.before = self._record_reference('Before service', before=True)
        self.current = self._record_reference('Example measurement')
        self.frame.results.session_path = self.current
        self.views = iter(self._views())
        self._show_next()

    def _record_reference(self, name, *, before=False, include_gap=False):
        frame = self.frame
        frame.session = new_session('Example technician')
        frame.session['metadata'].update(equipment='Example turntable', notes='Measurements from a synthetic reference tone.')
        frame.pages['measure']._reset_counter()
        if not frame.start_measurement():
            raise RuntimeError('Could not start the synthetic reference recording.')
        samples = flutter_sample(before=before)
        if include_gap:
            samples[9 * RATE:11 * RATE] = 0
        frame._run_clock_origin = RECORDING_TIME
        for start in range(0, len(samples), RATE // 8):
            snapshot = self.audio.feed(samples[start:start + RATE // 8], RATE)
            frame._record(snapshot)
        frame._run_started = monotonic() - len(samples) / RATE
        frame._tick(None)
        frame.stop_measurement()
        destination = self.scratch / f'{name}.fluttery'
        save_session(destination, frame.session, settings=frame.session_settings())
        frame.session_saved()
        return destination

    def _tool(self, key, view=None, *, samples=None, size=(820, 555), status='Ready.'):
        frame = self.frame
        if samples is not None:
            self.audio.reset()
            self.audio.feed(samples, RATE)
        panel = frame.navigate(key, view)
        window = frame.workspace.frame_for_key(key)
        work_area = wx.Display(0).GetClientArea()
        window.SetSize((min(size[0], work_area.width), min(size[1], work_area.height)))
        window.CentreOnScreen()
        window.Raise()
        if status is not None:
            frame.workspace.set_message(status)
        frame._last_debug_tick = 0
        frame._tick(None)
        window.Layout()
        return window, panel

    def _views(self):
        frame = self.frame
        # Leave the processed reference live for the measurement and RPM examples.
        yield 'measure', self._tool('measure', size=(840, 605), status=None)[0]
        window, _ = self._tool('turntable', 'rpm', size=(760, 510))
        for second in range(15):
            drift = 0.0007 + 0.0015 * np.exp(-second / 5) * np.cos(second / 2)
            self.audio.feed(tone(REFERENCE * (1 + drift), seconds=1), RATE)
            frame._tick(None)
        yield 'turntable-rpm', window

        yield from self._instrument_views()
        yield 'turntable-balance', self._tool('turntable', 'balance', samples=tone(REFERENCE, gains=(0.46, 0.40)))[0]
        tracking = tone(REFERENCE, gains=(0.44, 0.42), harmonics=(0.006, 0.018), noise=0.00018)
        yield 'turntable-tracking', self._tool('turntable', 'tracking', samples=tracking)[0]
        yield 'turntable-noise', self._tool('turntable', 'noise', samples=groove())[0]
        yield 'turntable-resonance', self._tool('turntable', 'resonance', samples=groove(resonance=True))[0]

        yield from self._distortion_view()

        yield from self._results_views()

        self.audio.start(output_enabled=False)
        self.audio.reset()
        self.audio.feed(tone(REFERENCE), RATE)
        for tab in ('audio', 'graphs', 'calibration', 'reports'):
            size = (855, 745 if tab == 'calibration' else 650)
            yield f'settings-{tab}', self._tool('settings', tab, size=size)[0]
        frame.workspace.frame_for_key('settings').Hide()

        yield from self._sweep_views()
        yield from self._response_views()
        yield from self._meter_views()
        yield from self._bench_sequence()
        yield from self._plot_views()

        self.audio.stop()
        frame.apply_settings(workspaceLayout='tabs', colorMode='dark')
        yield 'tabbed-workspace', self._tool('results', 'graph', size=(900, 610))[0]

    def _results_views(self, *, include_gap=False):
        if include_gap:
            self.audio.start(output_enabled=False)
            self.before = self._record_reference('Before service', before=True)
            self.frame.results.session_path = self._record_reference('Example measurement', include_gap=True)
        # Use the stored run rather than the audio most recently on input.
        self.audio.stop()
        if include_gap:
            window, results = self._tool('results', 'spectrogram', size=(900, 610))
            results.plots['spectrogram'].set_view(ylim=(0, 30))  # Both illustrated bands sit below 20 Hz.
            yield 'results-spectrogram', window
        else:
            window, results = self._tool('results', 'fft', size=(900, 610))
            yield 'results-spectrum', window
        with patch.object(results, '_file_path', return_value=self.before):
            if not results.open_comparison():
                raise RuntimeError('Could not load the synthetic comparison session.')
        if not include_gap:
            yield 'results-comparison', window
            yield 'results-spectrogram', self._tool('results', 'spectrogram', size=(900, 610))[0]
        yield 'results-statistics', self._tool('results', 'stats', size=(900, 610))[0]

    def _instrument_views(self):
        from fluttery.audio.bench import StepStimulus, analyse_steps, two_tone_imd
        from fluttery.audio.turntable import stereo_separation
        from fluttery.bench_ui import BenchTest
        from fluttery.measurement_ui import _SeparationCapture

        self.audio.start(output_enabled=False)
        phase = tone(997, gains=(.52, .48), phase_difference=.48)
        window, scope = self._tool('scope', 'waveform', samples=phase, size=(920, 660))
        scope.timebase.SetValue(.2)
        scope._trigger_changed()
        if not scope._advanced:
            scope._toggle_trigger_settings()
        scope._trigger_changed()
        self.frame._tick(None)
        yield 'scope-waveform', window
        yield 'scope-xy', self._tool('scope', 'xy')[0]

        left = tone(REFERENCE, gains=(.46, .018), noise=.00032)
        right = tone(REFERENCE, gains=(.012, .44), phase_difference=.1, noise=.00028)
        window, turntable = self._tool('turntable', 'crosstalk', samples=right)
        _, _, frame_number, epoch = self.audio.input_observation(0)
        for channel, data in enumerate((left, right)):
            request = _SeparationCapture(channel, RATE, frame_number, epoch, monotonic(),
                                         turntable._separation_route_key(), REFERENCE, 1.0, (0., 0.))
            turntable._capture_request = request
            turntable._finish_separation(request, stereo_separation(data, RATE, channel, expected_hz=REFERENCE), '')
        yield 'turntable-crosstalk', window

        bench = self.frame.show_bench_analyzer()
        self.audio.reset()
        self.audio.feed(tone(997, harmonics=(.042, .015), noise=.0008), RATE)
        bench._spectrum(force=True)
        bench._toggle_peak()
        yield 'bench-spectrum', bench

        bench.mode.SetSelection(1)
        bench._mode_changed(None)
        t = np.arange(RATE) / RATE

        def blend(frequency):
            return np.sin(2 * np.pi * frequency * t)

        base = .32 * blend(60) + .08 * blend(7000)
        readings = [two_tone_imd(base + amplitude * blend(6940) + amplitude * .7 * blend(7060), RATE, 60, 7000)
                    for amplitude in (.00057, .00105)]
        bench._complete(BenchTest(1, 1, 'smpte', 60, 7000, None), readings, '')
        yield 'bench-imd', bench

        bench.mode.SetSelection(2)
        bench._mode_changed(None)
        bench.start.ChangeValue('45')
        bench.end.ChangeValue('8000')
        bench.count.ChangeValue('12')
        bench.settle.ChangeValue('0.15')
        bench.observe.ChangeValue('0.35')
        frequencies = np.geomspace(45, 8000, 12)
        sweep = StepStimulus(tuple((float(frequency), -20.) for frequency in frequencies), settle=.15, observe=.35)
        outgoing = sweep.render(RATE)
        returned = np.column_stack((outgoing + .035 * outgoing**2 + .06 * outgoing**3,
                                   outgoing + .055 * outgoing**2 + .11 * outgoing**3))
        noise = np.random.default_rng(22).normal(0, .00003, returned.shape)
        recordings = np.pad(returned + noise, ((3100, 5000), (0, 0)))
        readings, _, _ = analyse_steps(outgoing, recordings, RATE, RATE, sweep)
        bench._complete(BenchTest(2, .35, 'smpte', 60, 7000, sweep), readings, '')
        yield 'bench-stepped-distortion', bench
        bench.Hide()

    def _response_views(self):
        from dataclasses import replace

        from scipy import signal

        from fluttery.audio.response import calibration_profile
        from fluttery.response_ui import ResponseRoute

        frame = self.frame
        device = device_identifier(DEVICE, 1)
        route = ResponseRoute(device, device, RATE)
        interface = frame.show_response('interface')
        interface._run()
        state = interface._parameters()
        stimulus = state.stimulus.render(RATE)
        converters = signal.butter(1, 17_000, fs=RATE, output='sos')
        loop = signal.sosfilt(converters, stimulus)

        def measure(dialog, parameters, left, right):
            returned = np.pad(np.column_stack((left, right)), ((17_724, RATE // 4), (0, 0)))
            result = dialog._analyse(returned, RATE, parameters, (state.stimulus, stimulus, RATE))
            dialog._save_result(result, parameters, RATE, route=route)
            return result

        calibration = measure(interface, state, loop * .91, loop * .84)
        interface._require_disconnect()
        yield 'interface-disconnect', interface
        interface._run()
        yield 'interface-response', interface

        response = frame.show_response('device')
        frame.apply_settings(responseCalibrations=[calibration_profile(
            'Example loopback', calibration, RATE, input_device=device, output_device=device, output_rate=RATE,
        )])
        response._reload_profiles('Example loopback')
        equipment = signal.butter(1, (45, 7500), btype='bandpass', fs=RATE, output='sos')
        filtered = signal.sosfilt(equipment, loop)
        measure(response, response._parameters(), filtered * .91, filtered * .80)
        response.compare_raw.SetValue(True)
        response._show_result()
        yield 'frequency-response', response

        playback = frame.show_response('tape_playback')
        state = playback._parameters()
        sections = []
        for frequency in (31.5, 63, 125, 250, 500, 1000, 2000, 4000, 8000, 12500, 16000):
            factor = 1 / np.sqrt(1 + (40 / frequency) ** 2) / np.sqrt(1 + (frequency / 10000) ** 2)
            sections.append(tone(frequency, seconds=.32, gains=(.16 * factor, .14 * factor)))
        curve = playback._analyse(np.concatenate(sections), RATE, state, None)
        playback._save_result(curve, state, RATE)
        yield 'tape-playback-response', playback

        recording = frame.show_response('tape_recording')
        recording._reload_profiles('Example loopback')
        recording.heads.SetSelection(1)
        recording._update_mode()
        state = recording._parameters()
        stimulus = state.stimulus.render(RATE)
        recording._reference = (state.stimulus, stimulus, RATE)
        tape = signal.butter(1, (38, 9600), btype='bandpass', fs=RATE, output='sos')
        filtered = signal.sosfilt(tape, signal.sosfilt(converters, stimulus))
        base = measure(recording, state, filtered * .75, filtered * .71)
        recording._pinned['tape_recording'] = [('-20 dBFS', base, RATE)]
        recording._pin_routes['tape_recording'] = {id(base): route}
        state = replace(state, stimulus=replace(state.stimulus, level_db=-10))
        recording.level.SetValue('-10')
        stimulus = state.stimulus.render(RATE)
        tape = signal.butter(1, (38, 6700), btype='bandpass', fs=RATE, output='sos')
        filtered = signal.sosfilt(tape, signal.sosfilt(converters, stimulus))
        measure(recording, state, filtered * .72, filtered * .69)
        yield 'tape-recording-response', recording
        recording.Hide()

    def _distortion_view(self):
        distortion = tone(997, harmonics=(0.012, 0.009), noise=0.0013)
        distortion += tone(3 * 997, gains=(0.003, 0.002))
        window, analyzer = self._tool('thd', samples=distortion)
        analyzer._toggle()
        self.frame._tick(None)
        yield 'distortion', window
        analyzer.plot_view.SetSelection(1)
        analyzer._plot_view_changed()
        yield 'distortion-harmonics', window
        analyzer._toggle()

    def _plot_views(self):
        from fluttery.plot_export_ui import ImageOptionsDialog
        from fluttery.preferences import DEFAULT_PLOT_IMAGE

        frame = self.frame
        frame.apply_settings(plotCanvas='dark', plotGridDetail='minor', plotPalette='blueOrange')
        self.audio.start(output_enabled=False)
        phase = tone(997, gains=(0.52, 0.43), phase_difference=0.48)
        window, scope = self._tool('scope', 'waveform', samples=phase, size=(905, 600))
        scope._toggle_freeze()
        frame.workspace.update_commands()
        scope.plot.set_view(xlim=(0, 3))
        trace = scope.plot.snapshot()['traces'][0]
        for name, time_ms in (('A', 0.25), ('B', 1.76)):
            index = int(np.argmin(np.abs(trace['x'] - time_ms)))
            scope.plot._set_marker(name, (0, index))
        scope.plot.focus_data()
        yield 'plot-markers', window
        options = {**DEFAULT_PLOT_IMAGE, 'format': 'svg', 'background': 'transparent'}
        with ImageOptionsDialog(window, options, title='Stereo waveform', plot_size=(1600, 700)) as dialog:
            dialog.Show()
            yield 'plot-image-export', dialog
        scope._toggle_freeze()
        frame.apply_settings(plotCanvas='system', plotGridDetail='major', plotPalette='theme')

    def _bench_sequence(self, *, include_distortion=False):
        from scipy.io.wavfile import write

        from fluttery.audio.recordings import analyse_wav
        from fluttery.lab_ui import LevelOptionsDialog

        frame = self.frame
        self.audio.start(output_enabled=False)
        time = np.arange(RATE) / RATE
        sine = np.sin(2 * np.pi * 1000 * time)
        pcm = np.column_stack((.35 * sine, .33 * sine))
        self.audio.feed(pcm, RATE)
        calibration = frame.show_level_meter('calibration')
        calibration.cal_channel.SetSelection(2)
        for gain in (.94, .98, 1.001, 1, 1.0002):
            self.audio.feed(pcm * gain, RATE)
            calibration._tick(None)
        calibration._capture_voltage()
        yield 'voltage-calibration', calibration
        calibration._save_voltage()
        calibration.Hide()

        meter = frame.show_level_meter('voltage')
        for gain in (.92, .97, 1, .995, 1.004, 1, .999):
            self.audio.feed(pcm * (gain, .94 * gain), RATE)
            meter._tick(None)
        yield 'ac-voltmeter', meter
        with LevelOptionsDialog(meter, 'voltage', {**meter.options, 'band': 'custom', 'units': 'millivolts'}) as options:
            options.Show()
            yield 'bench-measurement-options', options
        meter.Hide()

        snr = frame.show_level_meter('snr')
        for gain in (.996, 1, 1.002, 1):
            self.audio.feed(pcm * gain, RATE)
            snr._tick(None)
        snr._capture_reference()
        random = np.random.default_rng(1709)
        hum = np.column_stack((.00067 * np.sin(2 * np.pi * 50 * time), .0005 * np.sin(2 * np.pi * 100 * time)))
        for gain in (.00018, .00017, .00018):
            self.audio.feed(random.normal(0, gain, pcm.shape) + hum, RATE)
            snr._tick(None)
        snr._capture_noise()
        yield 'a-weighted-snr', snr
        snr.Hide()

        path = self.scratch / 'Example cassette speed.wav'
        write(path, RATE, flutter_diagnostic_sample(3150, 4, 19)[:8 * RATE, 0])
        offline = frame.show_offline_wav(path)
        settings = {**frame.settings, 'weightingStandard': 'aes6'}
        offline.method.SetSelection(1)
        offline._update_reference(None)
        result = analyse_wav(path, settings)
        offline._complete(result, '', settings)
        yield 'offline-test-tone', offline
        offline.Hide()

        raw_waveform = frame.show_raw_speed_capture()
        yield 'save-unweighted-speed', raw_waveform
        raw_waveform.Hide()
        if include_distortion:
            yield from self._distortion_view()

    def _sweep_views(self):
        from fluttery.audio.sweep import SweepRule, SweepSettings
        from fluttery.sweep_ui import SweepRulesDialog

        window, generator = self._tool('generate', size=(750, 610))
        generator._toggle_advanced()
        generator.loopback.SetValue(True)
        generator._toggle_loopback()
        generator.modulation.SetValue(True)
        generator.modulation_frequency.SetValue(4)
        generator.modulation_depth.SetValue(0.25)
        generator._modulation_changed()
        generator._toggle_generator()
        self.frame._tick(None)
        yield 'generator', window
        generator._toggle_generator()

        sweep = self.frame.show_frequency_sweep()
        yield 'sweep', sweep
        example = SweepSettings(rules=(
            SweepRule(200, waveform='triangle'),
            SweepRule(1000, level=0.50, level_curve='log'),
            SweepRule(4000, level=0.25, frequency_curve='linear'),
            SweepRule(20000, level=0, level_curve='linear'),
        ))
        sweep._load_sweep(example, sweep.wav)
        sweep.preview_mode.SetSelection(1)
        sweep._refresh_preview()
        yield 'sweep-amplitude', sweep
        with SweepRulesDialog(sweep, sweep._read_sweep(include_rules=False), sweep.rules) as rules:
            rules.CentreOnParent()
            rules.Show()
            yield 'sweep-rules', rules
        sweep.Hide()

    def _meter_views(self):
        keys = ('weightingStandard', 'flutterBand', 'meterScale', 'rmsDetector', 'enableQC')
        original = {key: self.frame.settings[key] for key in keys}
        self.audio.start(output_enabled=False)
        for name, carrier, modulation, overtone, standard, band in (
            ('measure-aes6', 3150, 4, 19, 'aes6', 'rotational'),
            ('measure-scrape', 12500, 1200, 2460, 'unweighted', 'scrape'),
        ):
            self.frame.apply_settings(weightingStandard=standard, flutterBand=band, meterScale=0.4,
                                      rmsDetector='oneSecond', enableQC=False)
            window, measure = self._tool('measure', size=(810, 660),
                                         samples=flutter_diagnostic_sample(carrier, modulation, overtone))
            if not measure.details_visible:
                measure._toggle_details()
            if not measure.spectrum_visible:
                measure._toggle_plot()
            if band == 'rotational':
                measure.plot.set_view(xlim=(0, 40))
            else:
                measure.plot.fit()
            yield name, window
        measure._toggle_details()
        measure._toggle_plot()
        self.frame.apply_settings(**original)

    def _show_next(self):
        try:
            name, window = next(self.views)
        except StopIteration:
            self._show_manual()
        else:
            self._schedule(self._capture_view, name, window)

    def _capture_view(self, name, window):
        self._save_image(window, name)
        self._schedule(self._show_next, delay=1)

    def _save_image(self, window, name):
        with self.capture.image(window, self.scratch) as screenshot:
            screenshot = screenshot_for_manual(screenshot)
            # Keep text sharp at typical manual widths without shipping the
            # whole Retina bitmap. No unrelated windows are captured.
            screenshot.thumbnail((1440, 1440), Image.Resampling.LANCZOS)
            screenshot.save(self.output / f'{name}.png', optimize=True, icc_profile=None)
        print(f'{name}.png', flush=True)

    def _show_manual(self):
        self.frame.apply_settings(colorMode='light')
        self._schedule(self._save_manual)
        self.frame._show_guide()
        if self.error is None:
            self._finish()

    def _save_manual(self):
        from fluttery.manual_ui import ManualDialog

        dialog = next((window for window in wx.GetTopLevelWindows() if isinstance(window, ManualDialog)), None)
        if dialog is None:
            raise RuntimeError('The user manual did not open.')
        self.manual_dialog = dialog
        self._save_image(dialog, 'user-manual')
        illustrated = next((topic for topic in dialog.manual.topics
                            if any(figure.path.name == 'measure.png' for figure in topic.illustrations)), None)
        if illustrated is None:
            dialog.EndModal(wx.ID_CANCEL)
            return
        dialog._populate(illustrated.address)
        dialog.mode.SetStringSelection('Illustrations')
        dialog._choose_mode(None)
        self._schedule(self._save_manual_illustration)

    def _save_manual_illustration(self):
        self._save_image(self.manual_dialog, 'user-manual-illustration')
        self.manual_dialog.EndModal(wx.ID_CANCEL)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    platform = PLATFORM_NAMES.get(sys.platform)
    if platform is None:
        parser.error('Capture is supported on macOS, Windows and Linux with X11.')
    destination = Path(__file__).resolve().parents[1] / 'fluttery' / 'manual' / 'images' / platform
    parser.add_argument('--output', type=Path, default=destination)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--manual-only', action='store_true', help='Capture the reader after updating its illustrated pages.')
    modes.add_argument('--sweeps-only', action='store_true', help='Refresh the generator, sweep examples, and manual reader.')
    modes.add_argument('--meters-only', action='store_true', help='Refresh the AES6 and scrape flutter examples and reader.')
    modes.add_argument('--bench-only', action='store_true', help='Refresh voltage, SNR and recording examples and reader.')
    modes.add_argument('--response-only', action='store_true', help='Refresh the interface, device and tape response examples.')
    modes.add_argument('--plots-only', action='store_true', help='Refresh the marker and plot image export examples.')
    modes.add_argument('--instruments-only', action='store_true', help='Refresh the scope, stereo separation and distortion tests.')
    modes.add_argument('--results-only', action='store_true', help='Refresh the spectrogram and session statistics.')
    modes.add_argument('--setup-only', action='store_true', help='Refresh the Setup Wizard illustrations.')
    options = parser.parse_args()
    options.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='fluttery-manual-') as directory:
        scratch = Path(directory)

        def save_preferences(settings):
            preferences.save_settings(settings, scratch / 'preferences.json')

        with patch.object(app, 'save_settings', save_preferences):
            desktop = wx.App(False)
            desktop.SetAppName('Fluttery')
            ManualExamples(desktop, options.output, scratch).run(
                manual_only=options.manual_only, sweeps_only=options.sweeps_only, meters_only=options.meters_only,
                bench_only=options.bench_only, response_only=options.response_only,
                plots_only=options.plots_only, instruments_only=options.instruments_only, results_only=options.results_only,
                setup_only=options.setup_only,
            )


if __name__ == '__main__':
    main()
