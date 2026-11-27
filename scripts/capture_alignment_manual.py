"""Capture the cartridge and tape assistants on the current native desktop."""

from __future__ import annotations

import argparse
import sys
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import numpy as np
import wx

from fluttery import app, preferences
from fluttery.alignment_ui import _analyse
from scripts.capture_manual import PLATFORM_NAMES, RATE, ManualExamples

SYNTHETIC_DURATION = 0.6


def stereo_signal(frequency, left, right, *, harmonic=(0, 0), delay_us=0):
    period = np.arange(round(RATE * SYNTHETIC_DURATION)) * (2 * np.pi * frequency / RATE)
    lag = 2 * np.pi * frequency * delay_us / 1e6
    return np.column_stack((left * (np.sin(period) + harmonic[0] * np.cos(3 * period)),
                            right * (np.sin(period - lag) + harmonic[1] * np.cos(3 * period))))


def trial(assistant, label, samples):
    assistant.setting.ChangeValue(label)
    assistant._last_read = _analyse(samples, RATE, assistant.step.mode, assistant.options)
    assistant._present(assistant._last_read)
    assistant._capture_trial()


class AlignmentExamples(ManualExamples):
    def run(self, **_options):
        self.frame.show_startup()
        self.views = iter(self._alignment_views())
        self._schedule(self._show_next, delay=450)
        self.desktop.MainLoop()
        if self.error is not None:
            raise self.error

    def _show_manual(self):
        self._finish()

    def _show(self, workflow, step):
        assistant = self.frame.show_alignment(workflow)
        assistant.timer.Stop()
        assistant.step_index = step
        assistant._show_step()
        assistant.CentreOnScreen()
        assistant.Raise()
        return assistant

    @staticmethod
    def _show_comparison(assistant):
        assistant.view.SetStringSelection('Captured Readings')
        assistant._change_graph()
        assistant.view.SetFocus()

    def _alignment_views(self):
        assistant = self._show('antiskate', 1)
        assistant.track.ChangeValue('315 Hz, band 1')
        for label, distortion in (('0.8', (0.010, 0.003)), ('1.2', (0.006, 0.005)), ('1.6', (0.004, 0.009))):
            trial(assistant, label, stereo_signal(315, 0.20, 0.23, harmonic=distortion))
        trial(assistant, '1.2', stereo_signal(315, 0.20, 0.23, harmonic=(0.0058, 0.0052)))
        assistant.track.ChangeValue('315 Hz, band 2')
        trial(assistant, '1.2', stereo_signal(315, 0.20, 0.23, harmonic=(0.008, 0.007)))
        self._show_comparison(assistant)
        assistant.comparison_choice.SetSelection(0)
        assistant._choose_comparison(None)
        assistant.comparison_choice.SetFocus()
        yield 'alignment-antiskate', assistant
        assistant.Hide()

        assistant = self._show('tracking-force', 1)
        assistant.track.ChangeValue('315 Hz, band +12 dB')
        for label, distortion in (('1.75', (0.014, 0.018)), ('1.85', (0.009, 0.011)), ('1.95', (0.007, 0.008))):
            trial(assistant, label, stereo_signal(315, 0.31, 0.34, harmonic=distortion))
        self._show_comparison(assistant)
        yield 'alignment-tracking-force', assistant
        assistant.Hide()

        assistant = self._show('vta', 1)
        assistant.track.ChangeValue('300 Hz and 3 kHz')
        for label, distortion in (('0.0 mm', (0.010, 0.013)), ('0.5 mm', (0.006, 0.008)), ('1.0 mm', (0.008, 0.010))):
            stereo = stereo_signal(300, 0.21, 0.24) + stereo_signal(3000, 0.12, 0.13)
            trial(assistant, label, stereo + stereo_signal(3300, 0.12 * distortion[0], 0.13 * distortion[1]))
        self._show_comparison(assistant)
        yield 'alignment-vta', assistant
        assistant.Hide()

        assistant = self._show('phono-loading', 1)
        for label in ('47 kΩ, 100 pF', '47 kΩ, 200 pF', '47 kΩ, 300 pF'):
            trial(assistant, label, stereo_signal(1000, 0.18, 0.19))
        assistant._move_step(1)
        assistant.track.ChangeValue('10 kHz reference band')
        for label, level in (('47 kΩ, 100 pF', 0.82), ('47 kΩ, 200 pF', 0.97), ('47 kΩ, 300 pF', 1.15)):
            trial(assistant, label, stereo_signal(10000, 0.18 * level, 0.19 * level * 1.01))
        for frequency, level in ((2500, 1.01), (5000, 0.99)):
            assistant.settings_by_step[assistant.step_index] = replace(assistant.options, tone_hz=frequency)
            assistant.track.ChangeValue(f'{frequency / 1000:g} kHz reference band')
            trial(assistant, '47 kΩ, 200 pF', stereo_signal(frequency, 0.18 * level, 0.19 * level * 1.01))
        assistant.view.SetStringSelection('Loading Response')
        assistant._change_graph()
        assistant.view.SetFocus()
        yield 'alignment-phono-loading', assistant
        assistant.Hide()

        assistant = self._show('phono-gain', 1)
        random = np.random.default_rng(621)
        for label, gain in (('36', 1), ('42', 2), ('48', 4)):
            trial(assistant, label, random.normal(0, 0.0001 * gain, (int(SYNTHETIC_DURATION * RATE), 2)))
        assistant._move_step(1)
        assistant.track.ChangeValue('1 kHz reference groove')
        for label, gain in (('36', 1), ('42', 2), ('48', 4)):
            trial(assistant, label, stereo_signal(1000, 0.08 * gain, 0.081 * gain))
        self._show_comparison(assistant)
        yield 'alignment-phono-gain', assistant
        assistant.Hide()

        assistant = self._show('head-azimuth', 1)
        assistant.track.ChangeValue('10 kHz alignment cassette')
        trial(assistant, 'After adjustment', stereo_signal(10000, 0.24, 0.29, delay_us=4.05))
        assistant.view.SetFocus()
        yield 'alignment-head-azimuth', assistant
        assistant.Hide()

        assistant = self._show('tape-bias', 2)
        for label, left, right in (('Position 1', 0.23, 0.22), ('Position 2', 0.27, 0.26), ('Position 3', 0.26, 0.25)):
            trial(assistant, label, stereo_signal(10000, left, right))
        assistant._move_step(1)
        for label, left, right in (('Position 4', 0.255, 0.245), ('Position 5', 0.212, 0.206)):
            trial(assistant, label, stereo_signal(10000, left, right))
        self._show_comparison(assistant)
        yield 'alignment-tape-bias', assistant
        assistant.Hide()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    platform = PLATFORM_NAMES.get(sys.platform)
    if platform is None:
        parser.error('Capture is supported on macOS, Windows and Linux with X11.')
    destination = Path(__file__).resolve().parents[1] / 'fluttery' / 'manual' / 'images' / platform
    parser.add_argument('--output', type=Path, default=destination)
    options = parser.parse_args()
    options.output.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix='fluttery-alignment-manual-') as directory:
        scratch = Path(directory)
        with patch.object(app, 'save_settings', lambda data: preferences.save_settings(data, scratch / 'preferences.json')):
            desktop = wx.App(False)
            desktop.SetAppName('Fluttery')
            wx.Log.SetActiveTarget(wx.LogStderr())
            examples = AlignmentExamples(desktop, options.output, scratch)
            with suppress(KeyboardInterrupt):
                examples.run()


if __name__ == '__main__':
    main()
