"""Native controls for live measurement, analysis, and generation."""

import math
from collections import deque
from dataclasses import dataclass, replace
from statistics import mean
from threading import Thread
from time import monotonic, time

import numpy as np
import wx

from .audio.flutter_detection import AES_WINDOW, DRIFT_WINDOW
from .audio.generator import GeneratorSettings, NoiseSource, SignalGenerator
from .audio.scope import LEVEL_DIVISIONS, TIME_DIVISIONS, acquire_sweep
from .audio.spectrum import FLOOR_DB
from .audio.turntable import (
    PLAYBACK_SPEEDS,
    SPEED_LABELS,
    automatic_record_reference,
    display_rpm,
    nominal_speed,
    stereo_separation,
)
from .charts import LinePlot, set_control_help
from .plot_style import contrast_rgb

# These trace colours retain the theme accents while keeping the operating
# system's contrast and focus settings for all native controls.
THEME_ACCENTS = {
    "modern": (("#0866a5", "#a74426"), ("#65c7fc", "#f9a588")),
    "retro": (("#925710", "#b73a20"), ("#ffb640", "#ff9677")),
    "neumorphism": (("#187568", "#6c49a0"), ("#6ae2cc", "#c4a6ff")),
    "neubrutalism": (("#254dc1", "#b52365"), ("#b8ef56", "#ff94c1")),
}

COMMAND_HELP = {
    "analysis": "Start, pause or resume the distortion analyzer. Pausing retains its recorded measurements.",
    "audio_settings": "Open the settings for audio devices, channels and monitoring.",
    "capture_speed": "Save the unweighted, demodulated speed signal to a PCM WAV file.",
    "clear_session": "Clear the current session history and recorded measurements after confirmation.",
    "fit_plot": "Restore the plot to its standard axes. Press Home while the plot has focus.",
    "freeze": "Freeze the scope trace and statistics for inspection, or resume live updates.",
    "generate": "Start or stop the selected test tone on the configured output or internal loopback.",
    "generator_options": "Show or hide stereo pan, internal loopback and frequency modulation controls.",
    "generator_preview": "Show or hide a silent preview of the selected signal.",
    "graph_settings": "Set graph contrast, grid lines and the spectrogram colour scale in Settings.",
    "input": "Activate audio input for live readings without starting a recording.",
    "measure": "Start or stop a wow and flutter recording in the current session.",
    "measurement_settings": "Open the reference, measurement standard, band, timing and meter settings.",
    "measurement_view": "Switch between combined wow and flutter readings and the separate frequency bands.",
    "meter_details": "Show or hide the one-second RMS, ten-second maxima, slow drift and repeating speed estimates.",
    "meter_plot": "Switch the graph between the run history and the live speed-variation spectrum.",
    "offline_wav": "Open a WAV recording of a reference tone and analyse it.",
    "phase": "Open Phase (XY) in the oscilloscope to compare the left and right channels.",
    "plot_controls": "Show or hide buttons for choosing plot points, switching traces and changing the zoom.",
    "plot_data": "Focus the plot's native text field to read the summary or selected point.",
    "read": "Open a fixed snapshot of the current values. You can select and copy the text. Escape closes it.",
    "record": "Start or stop recording the selected turntable test. Completed readings appear in Results.",
    "reset_counter": "Reset the elapsed-time tape counter to zero without clearing the measurement session.",
    "reset_thd": "Stop the analyzer and clear the standalone distortion measurements from this session.",
    "scope_source": "Switch between the incoming audio waveform and the demodulated speed variation.",
    "scope_trigger_settings": "Show or hide the oscilloscope trigger controls.",
    "scope_arm": "Wait for a matching edge, then hold the captured waveform on screen.",
    "separation_left": "Capture a fresh section of the left-only test tone and measure leakage into the right channel.",
    "separation_right": "Capture a fresh section of the right-only test tone and measure leakage into the left channel.",
    "separation_save": "Add the captured channel separation readings to the session.",
    "separation_clear": "Clear the current pair of separation readings.",
    "separation_cancel": "Cancel the active test capture.",
    "stereo": "Configure two input channels for turntable readings and stereo phase measurements.",
    "view_data": "Open the relevant results and recorded readings for this tool.",
}

METRIC_HELP = {
    "AES6 weighted peak": "The 95th percentile of the magnitudes of weighted speed variation over five seconds of continuous reference lock.",
    "Capture span": "The duration of the waveform segment selected by the timebase, before plot zoom is applied.",
    "Channel balance": "Difference in test-tone level between the left and right channels, in dB.",
    "Channels selected": "Number of audio input channels configured for this test.",
    "Detected frequency": "The test-tone frequency in Hz. The display marks the reading as an estimate until the tone settles.",
    "Flutter peak": "Maximum variation above the wow and flutter split frequency, expressed as a percentage.",
    "Fundamental frequency": "Frequency of the fundamental tone, which is the reference for harmonic distortion.",
    "Generator": "Shows whether the generator is stopped, playing, or using internal loopback.",
    "Input configuration": "This stereo measurement needs separate left and right input signals.",
    "Input level": "Reference RMS in dBFS. The thin marker holds the strongest input peak briefly. Higher levels approach clipping.",
    "Input peak": "The highest absolute sample level relative to full scale. For stereo, this is the higher of the two channel peaks.",
    "Input RMS": "The root-mean-square level relative to full scale. For stereo, this is the higher of the two channel readings.",
    "Third harmonic": "Amplitude of the third harmonic as a percentage of the fundamental.",
    "Left channel": "Level of the reference tone in the left channel, with channel trim applied.",
    "Left → right": "Separation in decibels from a left-only test track. A lower bound means the leakage was below the measurement resolution.",
    "Left THD plus noise": "Harmonic distortion and residual noise relative to the left-channel test tone.",
    "Louder channel": "Identifies which channel has the greater level, or reports when they match.",
    "Low-frequency rumble": "Combined level of low-frequency components associated with mechanical rumble.",
    "Measured speed": "Turntable rotation rate, calculated from the measured and reference frequencies.",
    "Output frequency": "Frequency of the generated tone. The sample rate can limit the requested frequency.",
    "Output level": "Selected maximum output amplitude, expressed as a percentage of digital full scale.",
    "Peak": "Maximum detected wow and flutter, in percent speed variation.",
    "Peak resonance": "Strongest low-frequency resonance. A typical cartridge target is 8 to 12 Hz.",
    "Playback speed": "Nominal playback speed used to calculate the percentage deviation.",
    "Resonance level": "Level of the strongest low-frequency resonance, in dB.",
    "Right channel": "Level of the reference tone in the right channel, with channel trim applied.",
    "Right → left": "Separation in decibels from a right-only test track. A lower bound means the leakage was below the measurement resolution.",
    "Right THD plus noise": "Harmonic distortion and residual noise relative to the right-channel test tone.",
    "RMS": "Root-mean-square wow and flutter, in percent speed variation.",
    "RMS noise level": "Overall root-mean-square level of the input signal, in dB.",
    "Scrape peak": "Smoothed peak speed variation from 200 to 5,000 Hz, relative to the 12.5 kHz carrier.",
    "Scrape RMS": "Root-mean-square speed variation in the 200 to 5,000 Hz scrape band.",
    "Speed deviation": "Percentage difference from the reference. Positive values indicate faster playback and negative values indicate slower playback.",
    "Stereo correlation": "Stereo waveform correlation on a scale from minus one (opposed) to plus one (in phase).",
    "THD": "Total harmonic distortion, expressed as a percentage of the fundamental tone.",
    "THD plus noise": "Harmonic distortion and residual noise, expressed as a percentage of the fundamental tone.",
    "Wow peak": "Maximum variation below the wow and flutter split frequency, expressed as a percentage.",
}

COUNTER_HELP = (
    "Recording time since the last reset. In the Retro theme, each unit is one tenth of a second. "
    "Choose Reset Counter to clear the counter or Read Values to check the elapsed time."
)


def _name_control(control, label):
    name = label.replace("&", "")
    control.SetName(name)
    set_accessible_name = getattr(control, "SetAccessibleName", None)
    if set_accessible_name is not None:
        set_accessible_name(name)


def _format_number(value, digits=2, unit=""):
    if value is None or not math.isfinite(value):
        return "Unavailable"
    return f"{value:.{digits}f}{unit}"


def _db(value):
    return _format_number(value, 1, " dB")


def _frequency_and_drift(metrics, reference_hz):
    """The frequency counter can read an off-speed tone without a W&F lock."""
    detected = metrics.get("currentFreq", 0)
    if detected <= 0 or not math.isfinite(detected):
        return None, None
    drift = metrics.get("speedDrift")
    if drift is None:
        drift = (detected / reference_hz - 1) * 100
    return detected, drift


def _measurement_frequency_text(detected, estimate):
    if detected is not None:
        return f"{detected:.2f} Hz"
    if estimate > 0 and math.isfinite(estimate):
        return f"{estimate:.2f} Hz (estimate)"
    return "No signal"


def _turntable_speed(metrics, reference_hz, record_rpm, playback_rpm):
    detected = metrics.get("currentFreq", 0)
    if detected <= 10 or not math.isfinite(detected) or metrics.get("clipped"):
        return None
    rpm = record_rpm * detected / reference_hz
    return rpm, (rpm / playback_rpm - 1) * 100


def _analysis_description(metrics, health):
    interval = health.get("averageAnalysisIntervalMs") or 0
    if interval <= 0 and (rate := metrics.get("analysisRate", 0)) > 0:
        interval = 1000 / rate
    if health.get("running") and interval > 0:
        return f"{interval:.1f} ms per reading"
    return "Waiting for audio"


def _level_state(level):
    if level > -3:
        return "Hot"
    if level > -20:
        return "Good"
    return "Low" if level > -70 else "No signal"


def _measurement_confidence(metrics, health):
    if metrics.get("clockUnsupported"):
        return "The corrected sample rate is outside the measurement range. Check Audio I/O clock correction."
    level = metrics.get("inputLevel", -100)
    lock = "Locked" if metrics.get("locked") else "Unstable" if level > -70 else "Waiting"
    if metrics.get("referenceAmbiguous"):
        return f"Ambiguous reference at {_format_number(metrics.get('currentFreq'), 1, ' Hz')}. Enter the test tone frequency."
    if metrics.get("referencePending"):
        return "Confirming the new reference tone."
    if metrics.get("clipped"):
        return "Input clipped. Reduce the recording level."
    dropped = metrics.get("detectorDropouts", 0)
    cadence = _analysis_description(metrics, health)
    return f"{lock} · {_level_state(level)}: {_db(level)} · {cadence} · {dropped} missed readings"


CONFIDENCE_HELP = (
    "Reference lock, input level, analysis cadence and the number of analysis windows "
    "with a signal present but no measurable tone. Read Values also reports reference losses and capture gaps."
)


METER_SCALES = (0.1, 0.4, 1.0, 4.0)


def _quality_control_status(metrics, locked, peak, limit):
    if metrics.get("invalidFrames") or metrics.get("clipped") or metrics.get("referenceAmbiguous"):
        return "Uncertain reading"
    if not locked or peak is None or not math.isfinite(peak) or metrics.get("rmsSeconds", 0) < 1:
        return "Awaiting valid reading"
    return "Fail" if peak > limit else "Pass"


class AutoMeterScale:
    """Adjust the shared needle range without chasing momentary dips."""

    def __init__(self):
        self.scale = METER_SCALES[0]
        self._pending = None

    def update(self, readings, now):
        peak = max((value for value in readings if value is not None and math.isfinite(value)), default=None)
        if peak is None:
            self._pending = None
            return self.scale
        chosen = next((scale for scale in METER_SCALES if peak <= 0.90 * scale), METER_SCALES[-1])
        if chosen > self.scale:
            self.scale = chosen
            self._pending = None
        elif chosen < self.scale and peak <= 0.65 * chosen:
            if self._pending is None or self._pending[0] != chosen:
                self._pending = chosen, now
            elif now - self._pending[1] >= 3:
                self.scale = chosen
                self._pending = None
        else:
            self._pending = None
        return self.scale


def _window_reading(value, locked, elapsed, duration):
    if not locked:
        return "Waiting for lock"
    if value is not None:
        return _format_number(value, 3, "%")
    # Never round an incomplete window up to its full duration.
    seconds = min(duration, math.floor(elapsed * 10) / 10)
    if seconds >= duration:
        return "Calculating"
    return f"{seconds:g} of {duration:g} s locked"


def _valid_history_points(history, field):
    """Keep elapsed time across gaps and distinguish missing samples from measured zero."""
    start = history[0].get("timestamp", 0)
    elapsed, readings = [], []
    interrupted = False
    for row in history:
        value = row.get(field)
        if value is None and field == "bufferPeak":
            value = row.get("peak")
        # Legacy sessions have measurements without an explicit lock field.
        if row.get("locked") is False or value is None or not math.isfinite(value):
            interrupted = bool(readings)
            continue
        timestamp = row.get("timestamp", start) - start
        if interrupted:
            # Disconnect only the missing interval. The valid runs on each
            # side can still be drawn as lines or exported with the same gap.
            elapsed.append(timestamp)
            readings.append(math.nan)
            interrupted = False
        readings.append(value)
        elapsed.append(timestamp)
    return elapsed, readings


def _spectrum_series(spectrum, label="Spectrum", ceiling=None, color=None):
    frequency = np.asarray(spectrum.get("frequencies", ()))
    level = np.asarray(spectrum.get("db", ()))
    if not len(frequency) or len(frequency) != len(level):
        return []
    if ceiling is not None:
        mask = frequency <= ceiling
        frequency, level = frequency[mask], level[mask]
    trace = {"label": label, "x": frequency, "y": level}
    if color is not None:
        trace["color"] = color
    return [trace]


class TapeCounter(wx.Panel):
    """Paint the counter wheels. The Read Values command exposes the same value as text."""

    def __init__(self, parent):
        super().__init__(parent, name="Tape counter")
        set_control_help(self, COUNTER_HELP)
        self.value = "00:00.00"
        self.retro = False
        self.SetMinSize(self.FromDIP((168, 44)))
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.Bind(wx.EVT_PAINT, self._paint)
        self.Bind(wx.EVT_SYS_COLOUR_CHANGED, self._system_colors)

    def _system_colors(self, event):
        event.Skip()
        self.Refresh(False)

    def set_value(self, value, retro):
        if (value, retro) != (self.value, self.retro):
            self.value, self.retro = value, retro
            self.Refresh(False)

    def _paint(self, _event):
        dc = wx.AutoBufferedPaintDC(self)
        dc.SetBackground(wx.Brush(wx.SystemSettings.GetColour(wx.SYS_COLOUR_3DFACE)))
        dc.Clear()
        width, height = self.GetClientSize()
        if width < 20 or height < 20:
            return
        inset = self.FromDIP(1)
        edge = wx.SystemSettings.GetColour(wx.SYS_COLOUR_3DSHADOW)
        face = wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOW)
        dc.SetPen(wx.Pen(wx.Colour("#777b80") if self.retro else edge))
        dc.SetBrush(wx.Brush(wx.Colour("#292c30") if self.retro else face))
        dc.DrawRoundedRectangle(inset, inset, width - 2 * inset, height - 2 * inset, self.FromDIP(6))

        dc.SetFont(wx.Font(wx.FontInfo(16).Family(wx.FONTFAMILY_TELETYPE).Bold()))
        dc.SetTextForeground(wx.Colour("#ffe0a4") if self.retro else wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOWTEXT))
        if not self.retro:
            text_width, text_height = dc.GetTextExtent(self.value)
            dc.DrawText(self.value, (width - text_width) // 2, (height - text_height) // 2)
            return

        wheel = self.FromDIP(18)
        separator = self.FromDIP(8)
        gap = self.FromDIP(1)
        padding = self.FromDIP(5)
        positions = [wheel if character.isdigit() else separator for character in self.value]
        span = sum(positions) + gap * (len(positions) - 1)
        x = (width - span) // 2
        for character, size in zip(self.value, positions, strict=True):
            text_x, text_y = dc.GetTextExtent(character)
            if character.isdigit():
                bounds = wx.Rect(x, padding, size, height - 2 * padding)
                dc.GradientFillLinear(bounds, wx.Colour("#43474c"), wx.Colour("#17191c"), wx.SOUTH)
                dc.SetPen(wx.Pen(wx.Colour("#17191c")))
                dc.DrawLine(x, height // 2, x + size, height // 2)
            dc.DrawText(character, x + (size - text_x) // 2, (height - text_y) // 2)
            x += size + gap


class InputLevelMeter(wx.Panel):
    """Meter the reference RMS with a separate peak marker on a dBFS scale."""

    _FLOOR = -60.0
    _HOLD_SECONDS = 1.5
    _FALL_DB_PER_SECOND = 24.0

    def __init__(self, parent):
        super().__init__(parent, name="Input level")
        self._reading = (None, None, "#0866a5", "#a74426")
        self._hold_until = None
        self._previous_time = None
        self.SetMinSize(self.FromDIP((135, 77)))
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.Bind(wx.EVT_PAINT, self._paint)

    def set_reading(self, rms, peak, now, colors):
        # A short hold lets the operator adjust gain without having to catch
        # individual transients. Clear it when input stops or is reconfigured.
        held = self._reading[1]
        if peak is None:
            held = self._hold_until = None
        elif held is None or peak >= held:
            held = peak
            self._hold_until = now + self._HOLD_SECONDS
        elif now > self._hold_until:
            elapsed = now - max(self._previous_time, self._hold_until)
            held = max(peak, held - elapsed * self._FALL_DB_PER_SECOND)
        self._previous_time = now
        reading = (rms, held, *colors)
        if reading != self._reading:
            self._reading = reading
            self.Refresh(False)

    def readout(self):
        rms, held, _, _ = self._reading
        if rms is None:
            return "Waiting for input."
        peak = f"{held:.1f} dBFS" if held is not None else "Unavailable"
        return f"RMS: {rms:.1f} dBFS. Held peak: {peak}."

    def _paint(self, _event):
        dc = wx.AutoBufferedPaintDC(self)
        panel_color = self.GetParent().GetBackgroundColour()
        dc.SetBackground(wx.Brush(panel_color))
        dc.Clear()
        width, height = self.GetClientSize()
        if width < self.FromDIP(105) or height < self.FromDIP(44):
            return
        rms, held, accent, warning = self._reading
        text = wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOWTEXT)
        border = wx.SystemSettings.GetColour(wx.SYS_COLOUR_3DSHADOW)
        face = wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOW)
        good = wx.Colour(*contrast_rgb(accent, face.Get(False), 3))
        hot = wx.Colour(*contrast_rgb(warning, face.Get(False), 4.5))
        dc.SetFont(self.GetFont().Scaled(0.85))
        dc.SetTextForeground(text)
        dc.SetPen(wx.Pen(border))
        dc.SetBrush(wx.Brush(face))
        dc.DrawRoundedRectangle(0, 0, width, height, self.FromDIP(5))

        start = self.FromDIP(17)
        span = width - start - self.FromDIP(13)
        top = max(self.FromDIP(6), (height - self.FromDIP(40)) // 2)
        rail = self.FromDIP(13)

        def px(level):
            return start + round(span * min(1, max(0, (level - self._FLOOR) / -self._FLOOR)))

        dc.SetPen(wx.Pen(border))
        dc.SetBrush(wx.Brush(face))
        dc.DrawRectangle(start, top, span + 1, rail)
        if rms is not None and px(rms) > start:
            dc.SetPen(wx.TRANSPARENT_PEN)
            dc.SetBrush(wx.Brush(hot if rms >= -3 else good))
            dc.DrawRectangle(start + 1, top + 1, px(rms) - start, rail - 2)
        if held is not None and held > self._FLOOR:
            dc.SetPen(wx.Pen(hot if held >= -1 else text, self.FromDIP(2)))
            dc.DrawLine(px(held), top - self.FromDIP(3), px(held), top + rail + self.FromDIP(2))

        dc.SetPen(wx.Pen(border))
        for level in (-60, -40, -20, -10, 0):
            x = px(level)
            bottom = top + rail
            dc.DrawLine(x, bottom, x, bottom + self.FromDIP(3))
            label = str(level).replace("-", "−")
            label_width = dc.GetTextExtent(label).width
            dc.DrawText(label, x - label_width // 2, bottom + self.FromDIP(5))


class NeedleMeter(wx.Panel):
    """Draw the analogue scale while the adjacent native text gives the exact reading."""

    def __init__(self, parent, name):
        super().__init__(parent, name=name)
        self.reading = (None, METER_SCALES[0], "#0866a5", False)
        self.SetMinSize(self.FromDIP((112, 77)))
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.Bind(wx.EVT_PAINT, self._paint)

    def set_reading(self, position, full_scale, accent, primary):
        reading = (position, full_scale, accent, primary)
        if reading != self.reading:
            self.reading = reading
            self.Refresh(False)

    def _paint(self, _event):
        dc = wx.AutoBufferedPaintDC(self)
        dc.SetBackground(wx.Brush(wx.SystemSettings.GetColour(wx.SYS_COLOUR_3DFACE)))
        dc.Clear()
        width, height = self.GetClientSize()
        if width < self.FromDIP(80) or height < self.FromDIP(58):
            return

        position, full_scale, accent, primary = self.reading
        graphics = wx.GraphicsContext.Create(dc)
        text_color = wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOWTEXT)
        edge_color = wx.SystemSettings.GetColour(wx.SYS_COLOUR_3DSHADOW)
        face = wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOW)
        accent_color = wx.Colour(*contrast_rgb(accent, face.Get(False), 4.5))
        graphics.SetPen(wx.Pen(accent_color if primary else edge_color, self.FromDIP(1)))
        graphics.SetBrush(wx.Brush(face))
        graphics.DrawRoundedRectangle(0.5, 0.5, width - 1, height - 1, self.FromDIP(5))

        center_x, center_y = width / 2, height - self.FromDIP(10)
        radius = min(center_x - self.FromDIP(13), center_y - self.FromDIP(23))

        def location(fraction, length):
            angle = math.pi * (fraction / 2 - 0.75)
            return center_x + length * math.cos(angle), center_y + length * math.sin(angle)

        graphics.SetPen(wx.Pen(text_color, self.FromDIP(1)))
        scale_path = graphics.CreatePath()
        scale_path.MoveToPoint(*location(0, radius))
        for step in range(1, 25):
            scale_path.AddLineToPoint(*location(step / 24, radius))
        graphics.StrokePath(scale_path)

        font = wx.SystemSettings.GetFont(wx.SYS_DEFAULT_GUI_FONT).Scaled(0.8)
        graphics.SetFont(font, text_color)
        for step in range(11):
            fraction = step / 10
            x, y = location(fraction, radius)
            labelled = step % 5 == 0
            inner_x, inner_y = location(fraction, radius - self.FromDIP(6 if labelled else 3))
            graphics.StrokeLine(x, y, inner_x, inner_y)
            if labelled:
                text = f"{fraction * full_scale:.2f}"
                text_width, text_height = graphics.GetTextExtent(text)
                x, y = location(fraction, radius + self.FromDIP(10))
                graphics.DrawText(text, x - text_width / 2, y - text_height / 2)

        if position is None:
            return
        x, y = location(position / 1000, radius - self.FromDIP(7))
        graphics.SetPen(wx.Pen(accent_color, self.FromDIP(2)))
        graphics.StrokeLine(center_x, center_y, x, y)
        graphics.SetBrush(wx.Brush(accent_color))
        pivot = self.FromDIP(2)
        graphics.DrawEllipse(center_x - pivot, center_y - pivot, 2 * pivot, 2 * pivot)


class BalanceScale(wx.Panel):
    """Display the signed stereo difference on a fixed centre-zero scale."""

    def __init__(self, parent):
        super().__init__(parent, name="Channel balance scale")
        set_control_help(self, "The marker moves toward the louder channel. The centre is 0 dB. The ticks are 1 dB apart, up to 5 dB on either side.")
        self.reading = (None, "#0866a5")
        self.SetMinSize(self.FromDIP((162, 44)))
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.Bind(wx.EVT_PAINT, self._paint)

    def set_reading(self, difference, accent):
        reading = (difference, accent)
        if reading != self.reading:
            self.reading = reading
            self.Refresh(False)

    def _paint(self, _event):
        dc = wx.AutoBufferedPaintDC(self)
        dc.SetBackground(wx.Brush(wx.SystemSettings.GetColour(wx.SYS_COLOUR_3DFACE)))
        dc.Clear()
        width, height = self.GetClientSize()
        inset = self.FromDIP(12)
        if width < 3 * inset or height < 3 * inset:
            return
        edge = wx.SystemSettings.GetColour(wx.SYS_COLOUR_3DSHADOW)
        dc.SetPen(wx.Pen(edge))
        face = wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOW)
        dc.SetBrush(wx.Brush(face))
        dc.DrawRoundedRectangle(0, 0, width, height, self.FromDIP(5))
        foreground = wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOWTEXT)
        dc.SetPen(wx.Pen(foreground, self.FromDIP(1)))
        dc.SetTextForeground(foreground)
        y = self.FromDIP(10)
        dc.DrawLine(inset, y, width - inset, y)
        for step in range(11):
            x = round(inset + (width - 2 * inset) * step / 10)
            extent = self.FromDIP(4 if step % 5 == 0 else 2)
            dc.DrawLine(x, y - extent, x, y + extent)
        for x, label, align in ((inset, "Left", 0), (width // 2, "0 dB", 0.5), (width - inset, "Right", 1)):
            text_width, _ = dc.GetTextExtent(label)
            dc.DrawText(label, round(x - text_width * align), y + self.FromDIP(10))
        difference, accent = self.reading
        if difference is not None:
            # Positive values mean left is louder. Retain the exact dB value in the native readout.
            fraction = min(1, max(-1, difference / 5))
            x = round(width / 2 - fraction * (width / 2 - inset))
            color = wx.Colour(*contrast_rgb(accent, face.Get(False), 4.5))
            dc.SetPen(wx.Pen(color, self.FromDIP(2)))
            dc.SetBrush(wx.Brush(color))
            dc.DrawLine(width // 2, y, x, y)
            dc.DrawCircle(x, y, self.FromDIP(5))


class MeasurementPanel(wx.Panel):
    def __init__(self, parent, controller):
        super().__init__(parent)
        self.controller = controller
        self.snapshot = {}
        self.summary = ""
        self.readout_label = "Current readings"
        self.content = wx.BoxSizer(wx.VERTICAL)
        self.SetSizer(self.content)

    def row(self, *, wrap=False):
        sizer = wx.WrapSizer(wx.HORIZONTAL) if wrap else wx.BoxSizer(wx.HORIZONTAL)
        self.content.Add(sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, self.FromDIP(10))
        return sizer

    def label(self, sizer, title, help_text):
        label = wx.StaticText(self, label=title)
        set_control_help(label, help_text)
        sizer.Add(label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, self.FromDIP(8))
        return label

    def choice(self, row, title, choices, selected=0, action=None, *, help_text):
        field = self._label_field(row, title, help_text)
        control = wx.Choice(self, choices=list(choices))
        _name_control(control, title)
        set_control_help(control, help_text)
        control.SetSelection(selected)
        if action is not None:
            control.Bind(wx.EVT_CHOICE, lambda event: action())
        field.Add(control, 0, wx.ALIGN_CENTER_VERTICAL)
        return control

    def spin(self, row, title, minimum, maximum, value, increment=1, digits=0, action=None, *, help_text):
        field = self._label_field(row, title, help_text)
        control = wx.SpinCtrlDouble(
            self, min=minimum, max=maximum, initial=value, inc=increment,
            style=wx.SP_ARROW_KEYS | wx.TE_PROCESS_ENTER,
        )
        _name_control(control, title)
        set_control_help(control, help_text)
        control.SetDigits(digits)
        # Some native ports expose the numeric editor and spinner as separate controls.
        for child in control.GetChildren():
            if isinstance(child, wx.SpinButton):
                guidance = "Use the arrows to change the value one step at a time."
            elif isinstance(child, wx.TextCtrl):
                guidance = "Type a number or use Up and Down Arrow to adjust it."
            else:
                guidance = ""
            set_control_help(child, " ".join(part for part in (help_text, guidance) if part))
        if action is not None:
            control.Bind(wx.EVT_SPINCTRLDOUBLE, lambda event: action())
            control.Bind(wx.EVT_TEXT_ENTER, lambda event: action())

            def on_blur(event):
                event.Skip()
                action()

            control.Bind(wx.EVT_KILL_FOCUS, on_blur)
        field.Add(control, 0, wx.ALIGN_CENTER_VERTICAL)
        return control

    def _label_field(self, row, title, help_text):
        # A narrow window can wrap an entire field to the next line, keeping
        # the native label beside its control and in the original tab order.
        field = wx.BoxSizer(wx.HORIZONTAL)
        self.label(field, title, help_text)
        row.Add(field, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, self.FromDIP(12))
        return field

    def checkbox(self, row, title, action, *, help_text):
        control = wx.CheckBox(self, label=title)
        set_control_help(control, help_text)
        control.Bind(wx.EVT_CHECKBOX, lambda event: action())
        row.Add(control, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, self.FromDIP(12))
        return control

    def set_summary(self, text):
        self.summary = text

    def desktop_actions(self):
        return [("read", "Read Values", self.read_current)]

    def desktop_help(self, key=None):
        return COMMAND_HELP if key is None else COMMAND_HELP.get(key, "")

    @staticmethod
    def _focus_actions(fields):
        return tuple(
            (label, control.SetFocus, control._description) for label, control in fields
            if control.IsShown() and control.IsEnabled()
        )

    def desktop_options(self):
        plot = getattr(self, "plot", None)
        if plot is None or not plot.IsShown():
            return []
        title = "Hide Plot Controls" if plot.controls_visible else "Show Plot Controls"
        return [
            ("plot_data", "Read Plot Data", plot.focus_data),
            ("plot_controls", title, lambda: plot.set_controls_visible(not plot.controls_visible)),
            ("fit_plot", "Fit Plot", plot.fit),
            ("graph_settings", "Graph Settings…", lambda: self.controller.navigate("settings", "graphs")),
        ]

    def read_current(self):
        # A separate snapshot keeps live updates away from the reading caret.
        with wx.Dialog(wx.GetTopLevelParent(self), title=self.readout_label,
                       style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER) as dialog:
            dialog._fluttery_help_enabled = self.controller.settings.get("showHelpTags", True)
            set_control_help(dialog, COMMAND_HELP["read"])
            layout = wx.BoxSizer(wx.VERTICAL)
            label = wx.StaticText(dialog, label="Values")
            field = wx.TextCtrl(dialog, value=self.summary or "Waiting for audio.",
                                style=wx.TE_MULTILINE | wx.TE_READONLY)
            _name_control(field, self.readout_label)
            help_text = "A fixed snapshot. Use the arrow keys to move through the values or select text to copy."
            set_control_help(label, help_text)
            set_control_help(field, help_text)
            layout.Add(label, 0, wx.LEFT | wx.RIGHT | wx.TOP, self.FromDIP(12))
            layout.Add(field, 1, wx.EXPAND | wx.ALL, self.FromDIP(12))
            layout.Add(dialog.CreateSeparatedButtonSizer(wx.OK), 0, wx.EXPAND | wx.ALL, self.FromDIP(8))
            dialog.SetEscapeId(wx.ID_OK)
            if close := dialog.FindWindowById(wx.ID_OK):
                set_control_help(close, "Close this snapshot and return to the instrument. You can also press Escape.")
            dialog.SetSizer(layout)
            dialog.SetSize(self.FromDIP((550, 285)))
            dialog.CentreOnParent()
            field.SetInsertionPoint(0)
            field.SetFocus()
            dialog.ShowModal()

    def add_plot(self, title, x_label, y_label, **kwargs):
        self.plot = LinePlot(self, title=title, x_label=x_label, y_label=y_label, controls=False, **kwargs)
        self.plot.set_appearance(self.controller.settings)
        self.plot.set_expanded_descriptions(self.controller.settings.get("useAccessibility", True))
        self.content.Add(self.plot, 1, wx.EXPAND | wx.ALL, self.FromDIP(10))
        self._spectrum_bounds = None
        return self.plot

    def show_spectrum(self, series):
        if self.plot.log_x and series:
            frequencies = series[0]["x"]
            nonzero = np.searchsorted(frequencies, 0.0, side="right")
            if len(frequencies) > nonzero + 1:
                bounds = (float(frequencies[nonzero]), float(frequencies[-1]))
                if bounds != self._spectrum_bounds:
                    self._spectrum_bounds = bounds
                    self.plot.set_default_view(xlim=bounds)
        self.plot.set_series(series)

    def theme_accents(self):
        names = THEME_ACCENTS.get(self.controller.settings.get("theme"), THEME_ACCENTS["modern"])
        dark = wx.SystemSettings.GetAppearance().IsDark()
        return names[int(dark)]

    def metric_readouts(self, captions, *, row=None):
        row = self.row() if row is None else row
        readouts = []
        for index, caption in enumerate(captions):
            group = wx.BoxSizer(wx.VERTICAL)
            label = wx.StaticText(self, label=caption)
            value = wx.StaticText(self, label="—", style=wx.ST_NO_AUTORESIZE | wx.ST_ELLIPSIZE_END)
            self._describe_metric(label, value, caption)
            label._fluttery_read_value = value.GetLabel
            value.SetFont(value.GetFont().Scaled(1.65 if index == 0 else 1.43).Bold())
            group.Add(label, 0, wx.EXPAND | wx.BOTTOM, self.FromDIP(2))
            group.Add(value, 0, wx.EXPAND)
            row.Add(group, 1, wx.EXPAND | wx.RIGHT, self.FromDIP(8))
            readouts.append((label, value))
        return readouts

    @staticmethod
    def _describe_metric(label, value, caption):
        description = METRIC_HELP.get(caption, f"Current {caption.lower()}.")
        set_control_help(label, description)
        set_control_help(value, description)

    def set_metrics(self, controls, readings):
        accent, warning = self.theme_accents()
        background = self.GetBackgroundColour().Get(False)
        layout_changed = False
        neutral = wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOWTEXT)
        for index, ((label, value), (caption, text, alert)) in enumerate(zip(controls, readings, strict=True)):
            if label.GetLabel() != caption:
                label.SetLabel(caption)
                self._describe_metric(label, value, caption)
                layout_changed = True
            if value.GetLabel() != text:
                value.SetLabel(text)
            color = wx.Colour(*contrast_rgb(warning if alert else accent, background, 4.5)) if alert or index == 0 else neutral
            if value.GetForegroundColour() != color:
                value.SetForegroundColour(color)
        if layout_changed:
            self.Layout()

    def start_live_input(self):
        if self.controller.ensure_audio(require_physical=True):
            self.controller.announce("Live input is active.")

    def refresh_measurement(self, snapshot):
        self.snapshot = snapshot

    def refresh_settings(self):
        if (plot := getattr(self, "plot", None)) is not None:
            plot.set_appearance(self.controller.settings)
            plot.set_expanded_descriptions(self.controller.settings.get("useAccessibility", True))


class MeasurePanel(MeasurementPanel):
    def __init__(self, parent, controller):
        super().__init__(parent, controller)
        set_control_help(self, "Measure wow, flutter and speed deviation from a reference tone.")
        self.readout_label = "Wow and flutter readings"
        self.counter_base = 0.0
        self.counter_start = None
        self.split_readings = False
        self.spectrum_visible = False
        self.details_visible = False
        self._spectrum_source = None
        self.last_meter_update = monotonic()
        self.auto_meter_scale = AutoMeterScale()
        self._meter_mode = None
        self._display_scale = None
        self.meter_levels = [0.0, 0.0, 0.0]
        self.meter_readings = ("Waiting for audio.",) * 3
        readings = self.row()
        self.frequency_cards = self.metric_readouts(("Detected frequency", "Speed deviation"), row=readings)
        counter_group = wx.BoxSizer(wx.VERTICAL)
        counter_label = wx.StaticText(self, label="Tape counter")
        set_control_help(counter_label, COUNTER_HELP)
        counter_group.Add(counter_label, 0, wx.BOTTOM, self.FromDIP(3))
        self.tape_counter = TapeCounter(self)
        counter_label._fluttery_read_value = lambda: self.tape_counter.value
        counter_group.Add(self.tape_counter, 0)
        readings.Add(counter_group, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, self.FromDIP(8))

        meters = self.row()
        self.meter_labels = []
        self.meters = []
        self.split_rms = []
        for index, label in enumerate(("Input level", "Peak", "RMS")):
            group = wx.BoxSizer(wx.VERTICAL)
            caption = wx.StaticText(self, label=label)
            group.Add(caption, 0)
            meter = InputLevelMeter(self) if index == 0 else NeedleMeter(self, label)
            _name_control(meter, label)
            self._describe_metric(caption, meter, label)

            def read_gauge(index=index):
                if index == 0:
                    return self.meters[0].readout()
                return self.meter_readings[index]

            caption._fluttery_read_value = read_gauge
            meter._fluttery_read_value = read_gauge
            group.Add(meter, 0, wx.EXPAND | wx.TOP, self.FromDIP(3))
            if index:
                band = "wow" if index == 1 else "flutter"
                value = wx.StaticText(self, label=f"{band.title()} RMS: —")
                set_control_help(value, f"Root-mean-square {band} in its frequency band, as a percentage of speed.")
                self.split_rms.append(value)
                group.Add(value, 0, wx.TOP, self.FromDIP(3))
                value.Hide()
            meters.Add(group, 1, wx.EXPAND | wx.RIGHT, self.FromDIP(10 if index < 2 else 0))
            self.meter_labels.append(caption)
            self.meters.append(meter)

        self.confidence = self.label(self.row(wrap=True), "Waiting for audio.", CONFIDENCE_HELP)
        self.range_status = self.label(self.row(wrap=True), "Needle range: Auto (0.1%).",
                                       "Both needles use this range. You can still read values above the scale in the text display.")
        self.details_row = self.row(wrap=True)
        self.detail_readings = (
            self.label(self.details_row, "One-second RMS: Unavailable",
                       "Root-mean-square variation over the last complete second of locked input."),
            self.label(self.details_row, "10-second maxima: Unavailable",
                       "Highest one-second RMS and smoothed detector peak readings from the last ten seconds."),
            self.label(self.details_row, "Very slow drift: Unavailable",
                       "Unweighted RMS from 0.05 to 0.5 Hz after twenty seconds of steady reference lock."),
            self.label(self.details_row, "Repeating speed variation: Unavailable",
                       "Frequency and period of the strongest recurring speed variation, when it can be measured."),
        )
        self.content.Show(self.details_row, False, recursive=True)
        self.qc_row = self.row()
        self.qc_status = self.label(
            self.qc_row, "", "Shows whether peak wow and flutter exceeds the configured limit while the reference is locked.",
        )
        self.content.Show(self.qc_row, False, recursive=True)
        self.spectrum_status = self.label(
            self.row(wrap=True), "", "Shows whether there is enough continuous audio for the live flutter spectrum.",
        )
        self.spectrum_status.Hide()
        self.add_plot(
            "Live history", "Elapsed time (seconds)", "W&F (%)",
            x_formatter=lambda value: f"{value:.3f}", y_formatter=lambda value: f"{value:.5f}",
        )
        self.plot.set_default_view(ylim=(0, METER_SCALES[0]))
        self._history_change = None

    def desktop_actions(self):
        label = "Stop Measurement" if self.controller.is_measuring else "Start Measurement"
        return [
            ("measure", label, self.controller.toggle_measurement),
            ("reset_counter", "Reset Counter", self._reset_counter),
            ("measurement_settings", "Measure Settings…", lambda: self.controller.navigate("settings", "measurement")),
            *super().desktop_actions(),
        ]

    def desktop_options(self):
        mode = "Show Combined Readings" if self.split_readings else "Show Split Wow and Flutter"
        plot_mode = "Show Run History" if self.spectrum_visible else "Show Live Flutter Spectrum"
        detail_mode = "Hide Detector Statistics" if self.details_visible else "Show Detector Statistics"
        actions = [
            ("input", "Start Live Input", self.start_live_input),
            ("meter_plot", plot_mode, self._toggle_plot),
            ("meter_details", detail_mode, self._toggle_details),
            ("offline_wav", "Analyse Recorded Tone WAV…", self.controller.show_offline_wav),
            ("capture_speed", "Save Unweighted Speed WAV…", self.controller.show_raw_speed_capture),
            ("view_data", "View Results", lambda: self.controller.navigate("results", "graph")),
        ]
        if self.controller.settings.get("flutterBand") != "scrape":
            actions.insert(1, ("measurement_view", mode, self._toggle_split))
        return [*actions, *super().desktop_options()]

    def _toggle_split(self):
        self.split_readings = not self.split_readings
        self.refresh_measurement(self.snapshot)

    def _toggle_plot(self):
        self.spectrum_visible = not self.spectrum_visible
        self._history_change = None
        self._spectrum_source = object()
        self.plot.clear()
        self.plot.fit()
        self.refresh_measurement(self.snapshot)

    def _toggle_details(self):
        self.details_visible = not self.details_visible
        self.content.Show(self.details_row, self.details_visible, recursive=True)
        self.refresh_measurement(self.snapshot)
        self.Layout()

    def _reset_counter(self):
        self.counter_base = 0.0
        self.counter_start = self.controller.measurement_elapsed if self.controller.is_measuring else None
        self.controller.play_sound("counter_reset")
        self.refresh_measurement(self.snapshot)
        self.controller.announce("Measurement counter reset.")

    def _counter_time(self, recording):
        elapsed = self.controller.measurement_elapsed
        if recording:
            if self.counter_start is None:
                self.counter_start = 0.0
            return self.counter_base + max(0.0, elapsed - self.counter_start)
        if self.counter_start is not None:
            self.counter_base += max(0.0, elapsed - self.counter_start)
            self.counter_start = None
        return self.counter_base

    def refresh_measurement(self, snapshot):
        super().refresh_measurement(snapshot)
        settings = self.controller.settings
        metrics = snapshot.get("metrics", {})
        health = snapshot.get("health", {})
        recording = self.controller.is_measuring
        now = monotonic()
        counter = self._counter_time(recording)
        retro = settings.get("theme") == "retro"
        history = self.controller.session.get("history", [])
        if not self.IsShownOnScreen():
            return

        scrape = settings.get("flutterBand") == "scrape"
        aes = not scrape and settings.get("weightingStandard") == "aes6"
        target = 12500 if scrape else 3150 if aes else metrics.get("activeTarget") or settings.get("targetFreq", 3000)
        detected, drift = _frequency_and_drift(metrics, target)
        locked = bool(metrics.get("locked", detected is not None))
        inactive = health.get("running") is False or (
            health.get("inputActive") is False and not health.get("loopbackActive")
        )
        if inactive:
            detected = drift = None
            locked = False
        estimate = snapshot.get("unsettled_frequency", 0) if not inactive and settings.get("showUnsettledFrequency") else 0
        frequency = _measurement_frequency_text(detected, estimate)
        speed = f"{drift:+.3f}%" if drift is not None else "Unavailable"
        level = -100 if inactive else metrics.get("inputLevel", -100)
        confidence = "Waiting for audio input" if inactive else _measurement_confidence(metrics, health)
        scope = snapshot.get("scope", {})
        scope_peak = max(scope.get("peakDb", -100), scope.get("rightPeakDb", -100))
        if not inactive and scope_peak >= -0.1:
            confidence += " · Input clipping"
        elif not inactive and scope_peak >= -1:
            confidence += " · Input near clipping"
        if not inactive and metrics.get("scrapeUnsupported"):
            confidence = "Scrape flutter needs an input sample rate of 44.1 kHz or higher"
        elif scrape:
            confidence = f"Scrape band 200–5,000 Hz · {confidence}"
        elif aes:
            confidence = f"AES6 · {confidence}"
        if self.confidence.GetLabel() != confidence:
            self.confidence.SetLabel(confidence)
        lines = [
            f"Frequency: {frequency}. Reference: {target:g} Hz. Speed deviation: {speed}.",
            f"{confidence}. Reference losses and capture gaps: {metrics.get('dropouts', 0)}.",
        ]

        peak_key = "aes6Peak" if aes else "scrapePeak" if scrape else "peak"
        peak = metrics.get(peak_key) if locked else None
        one_second = settings.get("rmsDetector") == "oneSecond"
        rms_key = "rmsOneSecond" if one_second else "scrapeRms" if scrape else "rms"
        rms = metrics.get(rms_key) if locked else None

        def percent(value):
            return _format_number(value, 3, "%")

        aes_text = _window_reading(peak, locked, metrics.get("aes6Seconds", 0), AES_WINDOW) if aes else None
        rms_text = _window_reading(rms, locked, metrics.get("rmsSeconds", 0), 1) if one_second else None
        primary = None
        split_visible = self.split_readings and not scrape
        if self.split_rms[0].IsShown() != split_visible:
            for control in self.split_rms:
                control.Show(split_visible)
            self.Layout()
        if split_visible:
            split = settings.get("splitFrequency", 6)
            wow, flutter = metrics.get("wow", {}), metrics.get("flutter", {})
            wow_peak, flutter_peak = (band.get("peak") if locked else None for band in (wow, flutter))
            wow_rms, flutter_rms = (
                band.get("rms") if locked and (not one_second or rms is not None) else None for band in (wow, flutter)
            )
            wow_rms_text, flutter_rms_text = (rms_text if one_second and value is None else percent(value)
                                              for value in (wow_rms, flutter_rms))
            lines.append(
                f"Split at {split:g} Hz. Wow peak: {percent(wow_peak)}. Wow RMS: {wow_rms_text}. "
                f"Flutter peak: {percent(flutter_peak)}. Flutter RMS: {flutter_rms_text}."
            )
            for control, name, value in zip(self.split_rms, ("Wow", "Flutter"), (wow_rms_text, flutter_rms_text), strict=True):
                label = f"{name} RMS: {value}"
                if control.GetLabel() != label:
                    control.SetLabel(label)
            names = ("Input level", "Wow peak", "Flutter peak")
            readings = wow_peak, flutter_peak
            reading_text = tuple(map(percent, readings))
        else:
            peak_name, rms_name = ("Scrape peak", "Scrape RMS") if scrape else ("AES6 weighted peak" if aes else "Peak", "RMS")
            primary = rms_name if scrape or settings.get("weightingStandard") == "jis" else peak_name
            names = ("Input level", peak_name, rms_name)
            readings = peak, rms
            reading_text = (aes_text or percent(peak), rms_text or percent(rms))
            lines.append(f"{peak_name}: {reading_text[0]}. {rms_name}: {reading_text[1]}. Primary reading: {primary}.")
        fixed_scale = settings.get("meterScale")
        mode = (scrape, aes, split_visible, fixed_scale)
        if mode != self._meter_mode:
            self.auto_meter_scale = AutoMeterScale()
            self._meter_mode = mode
        full_scale = fixed_scale or self.auto_meter_scale.update(readings, now)
        range_changed = self._display_scale != full_scale
        self._display_scale = full_scale
        range_text = f"Needle range: {'Fixed' if fixed_scale else 'Auto'} ({full_scale:g}% full scale)"
        over_range = [name for name, value in zip(names[1:], readings, strict=True) if value is not None and value > full_scale]
        if over_range:
            range_text += f" · Above range: {', '.join(over_range)}"
        if self.range_status.GetLabel() != range_text:
            self.range_status.SetLabel(range_text)
        accents = self.theme_accents()
        background = self.GetBackgroundColour().Get(False)
        range_color = (wx.Colour(*contrast_rgb(accents[1], background, 4.5)) if over_range
                       else wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOWTEXT))
        if self.range_status.GetForegroundColour() != range_color:
            self.range_status.SetForegroundColour(range_color)
        lines.append(f"{range_text}.")
        period = metrics.get("flutterPeriodHz")
        if locked and period:
            repetition = f"{period:.3f} Hz · {1 / period:.4g} s per cycle"
        elif not locked:
            repetition = "Waiting for lock"
        else:
            elapsed = metrics.get("driftSeconds", 0)
            minimum = 1 if scrape else 2
            repetition = _window_reading(None, True, elapsed, minimum) if elapsed < minimum else "No clear repeating peak"
        one_second_text = _window_reading(metrics.get("rmsOneSecond"), locked, metrics.get("rmsSeconds", 0), 1)
        drift_text = _window_reading(metrics.get("driftRms"), locked, metrics.get("driftSeconds", 0), DRIFT_WINDOW)
        rolling_rms = None if inactive else metrics.get("rollingRmsMax")
        rolling_peak = None if inactive else metrics.get("rollingPeakMax")
        detail_lines = (
            f"One-second RMS: {one_second_text}",
            f"10-second maxima: {percent(rolling_rms)} RMS, {percent(rolling_peak)} detector peak",
            f"Very slow drift, 0.05–0.5 Hz RMS: {drift_text}",
            f"Repeating speed variation: {repetition}",
        )
        lines.extend(f"{line}." for line in detail_lines)
        if self.details_visible:
            for label, reading in zip(self.detail_readings, detail_lines, strict=True):
                if label.GetLabel() != reading:
                    label.SetLabel(reading)
        if split_visible and aes:
            lines.append(f"AES6 weighted peak for the combined band: {aes_text}.")
        rms_label = f"{level:.1f} dBFS" if not inactive else "Unavailable"
        self.meter_readings = (rms_label, *reading_text)
        if not inactive:
            lines.append(f"Input RMS: {rms_label}. Input peak: {scope_peak:.1f} dBFS.")
        values = (None, *(reading / full_scale if reading is not None else None for reading in readings))
        qc_summary = ""
        show_qc = bool(settings.get("enableQC"))
        if show_qc:
            limit = settings.get("qcLimit", 0.1)
            qc_status = _quality_control_status(metrics if not inactive else {}, locked, peak, limit)
            qc_summary = f"Quality control: {qc_status}. Peak limit: {limit:.3f}%."
            if self.qc_status.GetLabel() != qc_summary:
                self.qc_status.SetLabel(qc_summary)
        if self.qc_status.IsShown() != show_qc:
            self.content.Show(self.qc_row, show_qc, recursive=True)
            self.Layout()
        state = "Measuring" if recording else "Ready"
        seconds, fraction = divmod(int(counter * 100), 100)
        minutes, seconds = divmod(seconds, 60)
        counter_text = f"{int(counter * 10):03d}" if retro else f"{minutes:02d}:{seconds:02d}.{fraction:02d}"
        lines.append(f"{state}. Tape counter: {counter_text}.")
        if qc_summary:
            lines.append(qc_summary)
        self.set_metrics(self.frequency_cards, (
            ("Detected frequency", frequency, False),
            ("Speed deviation", speed, drift is not None and abs(drift) > 0.5),
        ))
        self.tape_counter.set_value(counter_text, retro)
        damping = settings.get("meterDamping", 0.1)
        alpha = 1 - math.exp(-(now - self.last_meter_update) / damping) if damping > 0 else 1.0
        self.last_meter_update = now
        for index, (meter, caption, meter_label, value) in enumerate(zip(self.meters, self.meter_labels, names, values, strict=True)):
            target_value = None if value is None else 1000 * min(1, max(0, value))
            changed_meter = meter.GetName() != meter_label
            is_primary = meter_label == primary
            label = f"{meter_label}{' (primary)' if is_primary else ''} · {self.meter_readings[index]}"
            if caption.GetLabel() != label:
                caption.SetLabel(label)
            if changed_meter:
                _name_control(meter, meter_label)
                self._describe_metric(caption, meter, meter_label)
            if index == 0:
                meter.set_reading(None if inactive else level, None if inactive else scope_peak, now, accents)
                continue
            previous = self.meter_levels[index]
            if previous is not None and target_value is not None and not (changed_meter or range_changed):
                target_value = previous + (target_value - previous) * alpha
            self.meter_levels[index] = target_value
            display_level = None if target_value is None else round(target_value)
            meter.set_reading(display_level, full_scale, accents[int(index == 2 and self.split_readings)], is_primary)

        if self.spectrum_visible:
            if status := self._refresh_flutter_spectrum(snapshot, locked, scrape, accents[0]):
                lines.append(status)
        else:
            self._set_spectrum_status("")
            self._refresh_history(history, settings.get("graphSource"), full_scale)
        self.set_summary("\n".join(lines))

    def _set_spectrum_status(self, text):
        if self.spectrum_status.GetLabel() != text:
            self.spectrum_status.SetLabel(text)
        if self.spectrum_status.IsShown() != bool(text):
            self.spectrum_status.Show(bool(text))
            self.Layout()

    def _refresh_flutter_spectrum(self, snapshot, locked, scrape, color):
        title = "Scrape flutter spectrum" if scrape else "Flutter spectrum"
        mode_changed = self.plot.title != title
        spectrum = snapshot.get("flutter_spectrum") or {}
        if not locked or snapshot.get("metrics", {}).get("invalidFrames") or not len(spectrum.get("db", ())):
            spectrum = None
        if mode_changed:
            self.plot.set_labels(title, "Modulation frequency (Hz)", "Level (dB re 1 Hz)")
            self.plot.set_default_view(xlim=(200, 5000) if scrape else (0, 200))
            self.plot.fit()
        if mode_changed or spectrum is not self._spectrum_source:
            self._spectrum_source = spectrum
            self.plot.set_series(_spectrum_series(spectrum or {}, color=color))
        status = "Waiting for a continuous reference to show the spectrum." if spectrum is None else ""
        self._set_spectrum_status(status)
        return status

    def _refresh_history(self, history, source, full_scale):
        color, _ = self.theme_accents()
        last = history[-1] if history else {}
        scrape = last.get("flutterBand") == "scrape"
        aes = not scrape and last.get("weighting") == "aes6"
        key = (len(history), history[-1].get("timestamp") if history else None, source, id(history), color, full_scale, aes, scrape)
        if key == self._history_change:
            return
        self._history_change = key
        if self.plot.title != "Live history":
            self.plot.set_labels("Live history", "Elapsed time (seconds)", "W&F (%)")
        if not history:
            self.plot.set_default_view(ylim=(0, full_scale))
            self.plot.clear()
            return
        raw = source == "rawBlockPeak"
        field = "bufferPeak" if raw else "aes6Peak" if aes else "scrapePeak" if scrape else "peak"
        label = "Raw block peak" if raw else "AES6 weighted peak" if aes else "Scrape peak" if scrape else "Detector peak"
        elapsed, readings = _valid_history_points(history, field)
        # Keep the usual service scale, expanding for peaks that would otherwise
        # fall above the graph. A user-selected zoom retains its current limits.
        highest = max((reading for reading in readings if math.isfinite(reading)), default=0)
        ceiling = max(full_scale, highest * 1.05)
        duration = history[-1].get("timestamp", 0) - history[0].get("timestamp", 0)
        self.plot.set_default_view(xlim=(0, max(0.1, duration)), ylim=(0, ceiling))
        if not readings:
            self.plot.clear()
            return
        self.plot.set_series([{
            "label": label,
            "x": elapsed,
            "y": readings,
            "color": color,
        }])


class ScopePanel(MeasurementPanel):
    def __init__(self, parent, controller):
        super().__init__(parent, controller)
        set_control_help(self, "Inspect audio and speed waveforms, or compare the left and right channels in Phase (XY) view.")
        self.readout_label = "Oscilloscope readings"
        self.frozen_snapshot = None
        self._frozen_voltage = None
        self._single_complete = False
        self._current_sweep = None
        self._display_xy = None
        self._view_bounds = None
        self._scope_signature = None
        self._source_epoch = None
        self._scanned_frame = None
        self._trigger_frame = None
        self._armed_after = None
        self._arming = False
        self._trigger_signature = None
        self._acquisition_notice = ""
        self._advanced = False
        controls = self.row(wrap=True)
        self.mode = self.choice(
            controls, "Scope mode", ("Waveform", "Phase (XY)"), action=self._mode_changed,
            help_text="Show a waveform over time or plot the left and right channels against each other.",
        )
        self.gain = self.spin(
            controls, "Vertical scale (/div)", .0001, 1000, .25, increment=.05, digits=4,
            action=self._range_changed,
            help_text="Signal units per major division. The vertical display covers eight divisions.",
        )
        self.units = self.choice(
            controls, "Vertical units", ("Full scale", "Volts"), action=self._trigger_changed,
            help_text="Use fractions of digital full scale or measured volts with a voltage calibration for this input.",
        )
        self.waveform_controls = self.row(wrap=True)
        self.source = self.choice(
            self.waveform_controls, "Source", ("Raw input", "Speed variation"),
            action=self._mode_changed, help_text="Select incoming audio or the speed variation from the test tone.",
        )
        self.timebase = self.spin(
            self.waveform_controls, "Timebase (ms/div)", 0.01, 200, 0.5, increment=.1, digits=2,
            action=self._trigger_changed, help_text="Time per major division. The horizontal display covers ten divisions.",
        )
        self.inputs = self.choice(
            self.waveform_controls, "Channels", ("All available", "Left", "Right"), action=self._trigger_changed,
            help_text="Choose which raw audio traces to display independently of the trigger source.",
        )
        self.xy_controls = self.row(wrap=True)
        self.xy_span = self.spin(
            self.xy_controls, "Trace window (ms)", .2, 500, 20, increment=1, digits=1, action=self._range_changed,
            help_text="Set how long a segment of the signal the phase plot displays. Use a longer span for low frequencies.",
        )
        self.trigger_header = self.row(wrap=True)
        self.trigger_options = wx.Button(self, label="Show Trigger Settings")
        set_control_help(self.trigger_options, "Show or hide the scope's edge trigger controls.")
        self.trigger_options.Bind(wx.EVT_BUTTON, lambda _event: self._toggle_trigger_settings())
        self.trigger_header.Add(self.trigger_options, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, self.FromDIP(8))
        self.arm = wx.Button(self, label="Arm Single Capture")
        set_control_help(self.arm, "Wait for the next eligible edge, capture it once, then keep the waveform on screen.")
        self.arm.Bind(wx.EVT_BUTTON, lambda _event: self._arm_single())
        self.trigger_header.Add(self.arm, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, self.FromDIP(8))
        self.signal_display = self.label(
            self.trigger_header, "Waiting for audio.",
            "Shows the acquisition state, the trigger status and the signal quality.",
        )
        self.trigger_row = self.row(wrap=True)
        self.trigger_mode = self.choice(
            self.trigger_row, "Trigger mode", ("Auto", "Normal", "Single"), action=self._trigger_changed,
            help_text="Auto shows the input even when there is no trigger. Normal holds the last triggered capture. Single waits for one trigger after you arm it.",
        )
        self.trigger_source = self.choice(
            self.trigger_row, "Trigger input", ("Left / mono", "Right"), action=self._trigger_changed,
            help_text="Choose the raw input that determines the capture time. The speed-variation trace triggers on itself.",
        )
        self.trigger_slope = self.choice(
            self.trigger_row, "Trigger edge", ("Rising", "Falling"), action=self._trigger_changed,
            help_text="Capture when the chosen signal rises or falls through the trigger level.",
        )
        self.trigger_detail = self.row(wrap=True)
        self.trigger_level = self.spin(
            self.trigger_detail, "Trigger level", -1000, 1000, 0, increment=.01, digits=4,
            action=self._trigger_changed, help_text="Edge threshold in the same units as the plotted signal.",
        )
        self.pretrigger = self.spin(
            self.trigger_detail, "Before trigger (%)", 0, 90, 20, increment=5, action=self._trigger_changed,
            help_text="Percentage of the waveform kept before the trigger.",
        )
        self.trigger_hysteresis = self.spin(
            self.trigger_detail, "Hysteresis", 0, 1000, .002, increment=.002, digits=4,
            action=self._trigger_changed,
            help_text="Require the signal to cross both trigger thresholds. This prevents repeated triggers from noise.",
        )
        _name_control(self.trigger_hysteresis, "Trigger hysteresis")
        self.scope_readouts = self.metric_readouts(("Input RMS", "Input peak", "Capture span"))
        self.add_plot(
            "Input waveform", "Time (ms)", "Level (FS)",
            x_formatter=lambda value: f"{value:.5g}", y_formatter=lambda value: f"{value:.5g}",
        )
        self._mode_changed()

    def desktop_actions(self):
        freeze = "Resume" if self.frozen_snapshot is not None else "Freeze"
        return [
            ("input", "Start Live Input", self.start_live_input),
            ("freeze", freeze, self._toggle_freeze),
            *super().desktop_actions(),
        ]

    def desktop_options(self):
        options = super().desktop_options()
        if self.mode.GetSelection() == 0:
            source = self.source.GetSelection()
            title = "Show Raw Input Waveform" if source else "Show Wow and Flutter Waveform"
            options.insert(0, ("scope_source", title, lambda: self._select_source(1 - source)))
            label = "Hide Trigger Settings" if self._advanced else "Show Trigger Settings"
            options.insert(1, ("scope_trigger_settings", label, self._toggle_trigger_settings))
            if self.trigger_mode.GetSelection() == 2:
                options.insert(2, ("scope_arm", "Arm Single Capture", self._arm_single))
        return options

    def keyboard_actions(self):
        fields = (
            ("Select Scope Mode", self.mode), ("Edit Scope Timebase", self.timebase),
            ("Edit Scope Vertical Scale", self.gain), ("Select Waveform Source", self.source),
            ("Select Scope Channels", self.inputs), ("Edit Phase Trace Window", self.xy_span),
            ("Select Scope Vertical Units", self.units),
            ("Select Trigger Mode", self.trigger_mode), ("Select Trigger Input", self.trigger_source),
            ("Select Trigger Edge", self.trigger_slope), ("Edit Trigger Level", self.trigger_level),
            ("Edit Pre-trigger Percentage", self.pretrigger), ("Edit Trigger Hysteresis", self.trigger_hysteresis),
        )
        return self._focus_actions(fields)

    def set_view(self, view):
        self.mode.SetSelection(1 if view == "xy" else 0)
        self._mode_changed()

    def _select_source(self, selection):
        self.source.SetSelection(selection)
        self._mode_changed()

    def _toggle_trigger_settings(self):
        self._advanced = not self._advanced
        self._sync_controls()

    def _sync_controls(self):
        xy = self.mode.GetSelection() == 1
        modulation = not xy and self.source.GetSelection() == 1
        self.content.Show(self.waveform_controls, not xy, recursive=True)
        self.content.Show(self.xy_controls, xy, recursive=True)
        self.inputs.Enable(not modulation)
        self.units.Enable(not modulation)
        self.trigger_source.Enable(not modulation)
        self.trigger_options.Show(not xy)
        self.trigger_options.SetLabel("Hide Trigger Settings" if self._advanced else "Show Trigger Settings")
        self.arm.Show(not xy and self.trigger_mode.GetSelection() == 2)
        for row in (self.trigger_row, self.trigger_detail):
            self.content.Show(row, self._advanced and not xy, recursive=True)
        self.Layout()

    def _mode_changed(self):
        xy = self.mode.GetSelection() == 1
        signature = (xy, None if xy else self.source.GetSelection())
        if signature != self._scope_signature:
            self._scope_signature = signature
            self.frozen_snapshot = self._display_xy = None
            self._frozen_voltage = None
            self._single_complete = False
            self._reset_capture()
            self.plot.clear()
        self.plot.square_axes = xy
        self._sync_controls()
        self._render_current_view()

    def _reset_capture(self):
        self._current_sweep = None
        self._trigger_frame = self._armed_after = self._scanned_frame = None
        self._arming = False
        self._acquisition_notice = ""

    def _current_trigger_options(self):
        return (
            self.timebase.GetValue(), self.units.GetSelection(), self.trigger_mode.GetSelection(),
            self.trigger_source.GetSelection(), self.trigger_slope.GetSelection(), self.trigger_level.GetValue(),
            self.pretrigger.GetValue(), self.trigger_hysteresis.GetValue(), self.inputs.GetSelection(),
        )

    def _trigger_changed(self):
        signature = self._current_trigger_options()
        if self.frozen_snapshot is None and signature != self._trigger_signature:
            self._reset_capture()
        self._trigger_signature = signature
        self._sync_controls()
        self._range_changed()

    def _range_changed(self):
        self._render_current_view()
        self.plot.fit()

    def _render_current_view(self):
        self._render(self.snapshot)

    def refresh_settings(self):
        super().refresh_settings()
        self._render_current_view()

    def _toggle_freeze(self):
        if self.frozen_snapshot is None:
            xy = self.mode.GetSelection() == 1
            visible = self._display_xy if xy else self._current_sweep
            if visible is None:
                self.controller.announce("Waiting for a waveform to freeze.")
                return
            modulation = not xy and self.source.GetSelection() == 1
            self._freeze_capture(visible, self.snapshot, modulation=modulation)
            self._arming = False
            self.controller.announce("Oscilloscope frozen.")
        else:
            self.frozen_snapshot = None
            self._frozen_voltage = None
            self._single_complete = False
            self._reset_capture()
            self.controller.announce("Oscilloscope resumed.")
        self._render_current_view()

    def _arm_single(self):
        if not self.IsShownOnScreen() or self.mode.GetSelection() != 0 or self.trigger_mode.GetSelection() != 2:
            return
        signature = self._scope_signature, self._current_trigger_options()

        def resume():
            if self and signature == (self._scope_signature, self._current_trigger_options()):
                self._arm_single()

        if not self.controller.ensure_audio(after_permission=resume):
            return
        modulation = self.source.GetSelection() == 1
        buffer = self.controller.audio.scope_frames(0, modulation=modulation)
        self.frozen_snapshot = None
        self._frozen_voltage = None
        self._single_complete = False
        self._reset_capture()
        self._source_epoch = buffer.epoch
        self._armed_after = buffer.end - 1
        self._scanned_frame = buffer.end
        self._arming = True
        self._trigger_signature = self._current_trigger_options()
        self.controller.announce("Single capture armed. Waiting for a matching edge.")
        self._render_current_view()

    def refresh_measurement(self, snapshot):
        super().refresh_measurement(snapshot)
        if self.IsShownOnScreen() and self.frozen_snapshot is None:
            self._render(snapshot)

    def _freeze_capture(self, capture, snapshot, *, modulation=False, single=False):
        self._frozen_voltage = None if modulation else self._voltage_factors(capture.samples.shape[1], snapshot)
        self._single_complete = single
        self.frozen_snapshot = capture

    def _voltage_factors(self, channels, snapshot):
        rate = snapshot.get("sample_rate")
        identity = self.controller.audio_input_identity()
        record = next((item for item in self.controller.settings.get("voltageCalibrations", ())
                       if item["device"] == identity and item["rate"] == round(rate or 0)), {})
        factors = tuple(record.get(key) for key in ("left", "right")[:channels])
        if any(factor is not None for factor in factors) and not snapshot.get("health", {}).get("loopbackActive"):
            return np.asarray(factors, dtype=float)
        return None

    def _display_channels(self, channels):
        if channels == 1 or self.inputs.GetSelection() == 1:
            return (0,)
        return (1,) if self.inputs.GetSelection() == 2 else (0, 1)

    def _display_voltage(self, factors, channels, *, xy=False):
        required = tuple(range(channels)) if xy else self._display_channels(channels)
        if not xy and self.trigger_source.GetSelection() < channels:
            required = (*required, self.trigger_source.GetSelection())
        if factors is not None and np.isfinite(factors[list(required)]).all():
            return np.where(np.isfinite(factors), factors, 1.0)
        return None

    def _lose_capture(self, message):
        was_armed = self._arming
        self._reset_capture()
        if was_armed:
            self._acquisition_notice = message
            self.controller.announce(message)

    def _acquire(self, span, modulation, snapshot):
        if not self.controller.audio.running:
            if self._arming:
                self._lose_capture("Input stopped. Arm again.")
            if self.trigger_mode.GetSelection() == 1 and self._current_sweep is not None:
                return self._current_sweep, "Input stopped. Holding last trigger."
            return None, self._acquisition_notice or "Input stopped"
        # The engine returns measured seconds and the effective sample rate
        # together, so acquisition and its time axis use the same clock.
        preferred_window = max(.35, 2 * span / 1000)
        buffer = self.controller.audio.scope_frames(preferred_window, modulation=modulation, after=self._scanned_frame)
        if buffer.epoch is not self._source_epoch:
            self._lose_capture("Input changed. Arm again.")
            self._source_epoch = buffer.epoch
        if not len(buffer.samples):
            return None, self._acquisition_notice or "Waiting for input"
        if self._scanned_frame is not None and buffer.start > self._scanned_frame:
            self._lose_capture("Capture buffer overrun. Arm again.")
            if self._acquisition_notice:
                return None, self._acquisition_notice
        mode = self.trigger_mode.GetSelection()
        if mode == 2 and not self._arming:
            return self._current_sweep, self._acquisition_notice or "Ready to arm"
        channel = 0 if modulation else self.trigger_source.GetSelection()
        if channel >= buffer.samples.shape[1]:
            if mode == 0:
                self._current_sweep = acquire_sweep(buffer, span, channel=None, free_run=True)
            return self._current_sweep, "Right trigger unavailable"
        if self._scanned_frame != buffer.end:
            factors = None
            if not modulation and self.units.GetSelection() == 1:
                channels = buffer.samples.shape[1]
                factors = self._display_voltage(self._voltage_factors(channels, snapshot), channels)
            factor = factors[channel] if factors is not None else 1.0
            self._scanned_frame = buffer.end
            sweep = acquire_sweep(
                buffer, span, channel=channel, level=self.trigger_level.GetValue() / factor,
                rising=self.trigger_slope.GetSelection() == 0, pretrigger=self.pretrigger.GetValue() / 100,
                hysteresis=self.trigger_hysteresis.GetValue() / factor,
                after=self._armed_after if mode == 2 else self._trigger_frame if mode == 1 else None,
                first=mode == 2, free_run=mode == 0, recent_ms=max(160, span / 4) if mode == 0 else None,
            )
            if sweep is not None:
                self._current_sweep = sweep
                self._trigger_frame = sweep.trigger_frame
                if mode == 2:
                    self._freeze_capture(sweep, snapshot, modulation=modulation, single=True)
                    self._arming = False
                    self.controller.announce("Single sweep captured.")
                    return sweep, "Single sweep complete"
        sweep = self._current_sweep
        ready = "Waiting for trigger" if mode == 1 else "Armed" if mode == 2 else "Auto"
        if sweep is None:
            return None, ready
        if mode == 1 and self._trigger_frame is not None:
            elapsed = (buffer.end - self._trigger_frame) * 1000 / buffer.rate
            if elapsed > span + max(180, span / 4):
                return sweep, "Holding last trigger"
        return sweep, "Triggered" if sweep.trigger_frame is not None else "Free run"

    def _render(self, snapshot):
        xy = self.mode.GetSelection() == 1
        modulation = not xy and self.source.GetSelection() == 1
        scope = snapshot.get("scope", {})
        raw_levels = tuple(np.asarray(scope.get(key, ())) for key in ("left", "right"))
        # The calibrated vertical display does not change the measured sample
        # or remove a DC offset. It only changes the units of the trace.
        scale = self.gain.GetValue()
        amplitude = LEVEL_DIVISIONS / 2 * scale
        span = self.timebase.GetValue() * TIME_DIVISIONS
        before = self.pretrigger.GetValue() / 100
        accent, secondary = self.theme_accents()
        details = "Stereo correlation" if xy else "Capture span"
        detail_value = "Unavailable"
        samples = None
        times = None
        triggered = False
        status = "Single sweep complete" if self._single_complete else "Frozen"
        if xy:
            buffer = self.frozen_snapshot
            if buffer is None:
                if self.controller.audio.running:
                    buffer = self.controller.audio.scope_frames(self.xy_span.GetValue() / 1000)
                    status = "Live"
                else:
                    status = "Input stopped"
                self._display_xy = buffer if buffer is not None and len(buffer.samples) >= 2 else None
            samples = buffer.samples if buffer is not None else None
        else:
            sweep = self.frozen_snapshot
            if sweep is None:
                sweep, status = self._acquire(span, modulation, snapshot)
            if sweep is not None:
                samples, times = sweep.samples, sweep.time_ms
                triggered = sweep.trigger_frame is not None
                detail_value = f"{(times[-1] - times[0]):.3g} ms" if len(times) else "Unavailable"
                if not modulation:
                    raw_levels = (samples[:, 0], samples[:, 1] if samples.shape[1] == 2 else ())
        if samples is None and not self.controller.audio.running:
            raw_levels = (), ()
        self.plot.mark_time_origin = triggered
        channels = samples.shape[1] if samples is not None else 1
        voltage = None
        if not modulation and samples is not None and self.units.GetSelection() == 1:
            factors = self._frozen_voltage if self.frozen_snapshot is not None else self._voltage_factors(channels, snapshot)
            voltage = self._display_voltage(factors, channels, xy=xy)
        unit = "%" if modulation else "V" if voltage is not None else "FS"
        lines = []
        if not modulation and self.units.GetSelection() == 1 and voltage is None:
            lines.append("Voltage calibration is unavailable for this input. The trace uses full-scale units.")
        if voltage is not None:
            samples = samples * voltage
        if xy:
            stereo = channels == 2
            self.plot.set_labels("Stereo phase" if stereo else "Mono XY waveform",
                                 f"{'Left' if stereo else 'Input X'} ({unit})",
                                 f"{'Right' if stereo else 'Input Y'} ({unit})")
            if samples is not None and len(samples):
                left, right = samples[:, 0], samples[:, -1]
                raw_levels = (buffer.samples[:, 0], buffer.samples[:, 1] if stereo else ())
                self.plot.set_series([{"label": "Stereo phase" if stereo else "Mono input",
                                       "x": left, "y": right, "color": accent}])
                if stereo:
                    ac_left, ac_right = left - left.mean(), right - right.mean()
                    power = float(np.linalg.norm(ac_left) * np.linalg.norm(ac_right))
                    if power > 0:
                        detail_value = f"{np.clip((ac_left @ ac_right) / power, -1, 1):.3f}"
                else:
                    lines.append("The phase plot shows the mono signal on both axes.")
            else:
                self.plot.clear()
            self._set_view_bounds((-amplitude, amplitude), (-amplitude, amplitude), scale, scale)
        else:
            self.plot.set_labels("Wow and flutter waveform" if modulation else "Input waveform", "Time (ms)",
                                 f"{'Speed variation' if modulation else 'Level'} ({unit})")
            if samples is not None:
                visible = self._display_channels(channels)
                if not modulation and channels == 1 and self.inputs.GetSelection() == 2:
                    lines.append("The right channel is unavailable. Showing the mono signal.")
                labels = ("Wow and flutter",) if modulation else ("Input",) if channels == 1 else ("Left", "Right")
                self.plot.set_series([
                    {"label": labels[channel], "x": times, "y": samples[:, channel],
                     "color": (accent, secondary)[channel], "style": "dashed" if channel == 1 else "line"}
                    for channel in visible
                ])
                if modulation:
                    valid = samples[np.isfinite(samples)]
                    if len(valid):
                        variation = np.sqrt(np.mean(np.square(valid), dtype=np.float64))
                        lines.append(f"Demodulated speed variation: {variation:.4g}% RMS.")
            else:
                self.plot.clear()
            if triggered and self.frozen_snapshot is not None and len(times) > 1:
                before = max(0, -times[0] / (times[-1] - times[0]))
            offset = before * span if triggered else 0
            self._set_view_bounds((-offset, span - offset), (-amplitude, amplitude), self.timebase.GetValue(), scale)
            lines.append(f"Timebase: {self.timebase.GetValue():g} ms per division.")
        levels = [_scope_signal(channel) for channel in raw_levels]
        rms, peak = max(row[0] for row in levels), max(row[1] for row in levels)
        has_input = any(len(channel) for channel in raw_levels)
        quality = _scope_status(rms, peak) if has_input else "No audio"
        display = f"{status} · {quality}"
        if self.signal_display.GetLabel() != display:
            self.signal_display.SetLabel(display)
        rms_text = f"{rms:.1f} dBFS" if has_input else "Unavailable"
        peak_text = f"{peak:.1f} dBFS" if has_input else "Unavailable"
        self.set_metrics(self.scope_readouts, (
            ("Input RMS", rms_text, False), ("Input peak", peak_text, quality == "Clipping"),
            (details, detail_value, False),
        ))
        lines[:0] = [
            f"{status}. {quality}.", f"Input RMS level: {rms_text}. Input peak level: {peak_text}.",
            f"{details}: {detail_value}.", f"Vertical scale: {scale:g} {unit} per division.",
        ]
        if all(len(channel) for channel in raw_levels):
            lines.append(f"Left input: {levels[0][0]:.1f} dBFS RMS, {levels[0][1]:.1f} dBFS peak. "
                         f"Right input: {levels[1][0]:.1f} dBFS RMS, {levels[1][1]:.1f} dBFS peak.")
        if xy:
            lines.append(f"Trace window: {self.xy_span.GetValue():g} ms.")
        else:
            trigger = "speed variation" if modulation else self.trigger_source.GetStringSelection()
            lines.append(f"{self.trigger_mode.GetStringSelection()} trigger. {self.trigger_slope.GetStringSelection()} edge. "
                         f"Source {trigger}. Level {self.trigger_level.GetValue():g} {unit}. "
                         f"Hysteresis {self.trigger_hysteresis.GetValue():g} {unit}.")
        self.set_summary("\n".join(lines))

    def _set_view_bounds(self, xlim, ylim, x_step, y_step):
        bounds = (xlim, ylim, x_step, y_step)
        if bounds != self._view_bounds:
            self._view_bounds = bounds
            self.plot.set_grid_steps(x_step, y_step)
            self.plot.set_default_view(xlim=xlim, ylim=ylim)


def _scope_signal(samples):
    rms = peak = -100.0
    if len(samples):
        rms = 20 * math.log10(max(float(np.sqrt(np.mean(np.square(samples)))), 1e-5))
        peak = 20 * math.log10(max(float(np.max(np.abs(samples))), 1e-5))
    return rms, peak, _scope_status(rms, peak)


def _scope_status(rms, peak):
    if peak > 20 * math.log10(0.98):
        return "Clipping"
    if rms < -50:
        return "Low or no signal"
    return "High signal" if rms > -3 else "Good signal"


class THDPanel(MeasurementPanel):
    log_interval = 0.1

    def __init__(self, parent, controller):
        super().__init__(parent, controller)
        set_control_help(self, "Measure harmonic distortion and noise relative to the strongest input tone.")
        self.readout_label = "Distortion readings"
        self.running = False
        self.started = False
        self._start_request = None
        self.last_sequence = None
        self.last_reading_time = None
        self.next_log_time = None
        self._plot_reading = {}, {}
        self.distortion_cards = self.metric_readouts(("THD plus noise", "THD", "Third harmonic", "Fundamental frequency"))
        views = self.row(wrap=True)
        self.plot_view = self.choice(
            views, "Plot view", ("Spectrum", "Harmonics"), action=self._plot_view_changed,
            help_text="Show the input spectrum or the fitted harmonic levels in dB relative to the fundamental.",
        )
        self.signal_display = self.label(
            views, "Analysis ready.",
            "Shows whether the analyser is paused and how reliably it can detect the test tone.",
        )
        self.add_plot(
            "Input spectrum", "Frequency (Hz)", "Level (dB)", log_x=True,
            x_formatter=lambda value: f"{value:.2f}", y_formatter=lambda value: f"{value:.2f}",
        )
        self.set_summary("Analysis ready.")

    def desktop_actions(self):
        label = "Pause Analysis" if self.running else "Resume Analysis" if self.started else "Start Analysis"
        return [("analysis", label, self._toggle), *super().desktop_actions()]

    def desktop_options(self):
        return [
            ("reset_thd", "Reset Distortion Results", self._reset),
            ("view_data", "View Results", lambda: self.controller.navigate("results", "thd")),
            *super().desktop_options(),
        ]

    def _toggle(self):
        if self.running:
            self.running = False
            self.signal_display.SetLabel("Analysis paused.")
            self.controller.play_sound("stop")
            self.controller.announce("Distortion analysis paused.")
            return
        request = self._start_request = object()

        def start():
            if not self or self._start_request is not request or not self.IsShownOnScreen():
                return
            if self.controller.ensure_audio(after_permission=start):
                self._start_request = None
                self.running = True
                self.started = True
                self.next_log_time = None
                self.signal_display.SetLabel("Waiting for a signal.")
                self.controller.play_sound("start")
                self.controller.announce("Distortion analysis started.")

        start()

    def _reset(self):
        self._start_request = None
        self.running = False
        self.started = False
        self.last_sequence = None
        self.last_reading_time = self.next_log_time = None
        self.signal_display.SetLabel("Analysis ready.")
        self._plot_reading = {}, {}
        self._render_plot()
        self._set_distortion({})
        self.set_summary("Analysis ready.")
        self.controller.clear_measurements("thd")

    def refresh_measurement(self, snapshot):
        super().refresh_measurement(snapshot)
        if not self.running:
            return

        health = snapshot.get("health", {})
        thd = snapshot.get("thd", {})
        ready = thd.get("available") and thd.get("signalDb", -100) > -90 and health.get("running")
        if self.IsShownOnScreen():
            self._set_distortion(thd if ready else {})
            status = thd.get("confidence") or health.get("status") or "Waiting for a signal."
            if self.signal_display.GetLabel() != status:
                self.signal_display.SetLabel(status)
            if ready:
                lines = [
                    (f"THD plus noise: {_format_number(thd.get('thdn'), 3, '%')}. "
                     f"THD: {_format_number(thd.get('thd'), 3, '%')}. "
                     f"Third harmonic: {_format_number(thd.get('hd3'), 3, '%')}."),
                    f"Fundamental: {_format_number(thd.get('fundamentalFrequency'), 1, ' Hz')}.",
                    status,
                ]
                if harmonics := thd.get("harmonicDbc"):
                    levels = ", ".join(f"H{order}: {level:+.1f} dBc" for order, level in enumerate(harmonics, 1))
                    lines.append(f"Fitted harmonic levels. {levels}.")
                self.set_summary("\n".join(lines))
            else:
                self.set_summary(status)
            self._plot_reading = snapshot.get("spectrum", {}), thd if ready else {}
            self._render_plot()

        metrics = snapshot.get("metrics", {})
        sequence = metrics.get("sequence")
        if ready and (sequence is None or sequence != self.last_sequence):
            self.last_sequence = sequence
            self._log_distortion(thd, metrics.get("timestamp", monotonic()))

    def _plot_view_changed(self):
        bars = self.plot_view.GetSelection() == 1
        self.plot.clear()
        self.plot.log_x = not bars
        self.plot.x_ticks = None
        self.plot.set_default_view()
        self._spectrum_bounds = None
        if bars:
            self.plot.set_labels("Fitted harmonic levels", "Harmonic order", "Level (dBc)")
            self.plot.x_formatter = lambda number: f"{number:.0f}"
        else:
            self.plot.set_labels("Input spectrum", "Frequency (Hz)", "Level (dB)")
            self.plot.x_formatter = lambda number: f"{number:.2f}"
        self._render_plot()

    def _render_plot(self):
        spectrum, reading = self._plot_reading
        accent, _ = self.theme_accents()
        if self.plot_view.GetSelection() == 0:
            self.show_spectrum(_spectrum_series(spectrum, color=accent))
            return
        levels = reading.get("harmonicDbc", ())
        orders = range(1, len(levels) + 1)
        self.plot.x_ticks = tuple((order, f"H{order}") for order in orders)
        self.plot.set_default_view(xlim=(0.5, max(1, len(levels)) + 0.5), ylim=(FLOOR_DB, max(5, max(levels, default=0) + 5)))
        self.plot.set_series([{"label": "Harmonics", "x": orders, "y": levels, "color": accent, "style": "bars"}])

    def _set_distortion(self, thd):
        self.set_metrics(self.distortion_cards, (
            ("THD plus noise", _format_number(thd.get("thdn"), 3, "%"), thd.get("thdn", 0) > 1),
            ("THD", _format_number(thd.get("thd"), 3, "%"), (thd.get("thd") or 0) > 1),
            ("Third harmonic", _format_number(thd.get("hd3"), 3, "%"), False),
            ("Fundamental frequency", _format_number(thd.get("fundamentalFrequency"), 1, " Hz"), False),
        ))

    def _log_distortion(self, thd, reading_time):
        # Audio time keeps logging independent of UI redraws. Analysis may be
        # reset by a route change or another tool while this tab stays active.
        if self.next_log_time is None or (
                self.last_reading_time is not None and reading_time < self.last_reading_time):
            self.next_log_time = reading_time
        self.last_reading_time = reading_time
        if reading_time + 1e-6 < self.next_log_time:
            return
        self.next_log_time = max(self.next_log_time + self.log_interval, reading_time)
        self.controller.log_measurement("thd", {
            "thd": float(thd["thdn"]),
            "thdOnly": thd["thd"],
            "hd3": thd.get("hd3"),
            "fundamental": float(thd["fundamentalFrequency"]),
            "noisePercent": float(thd.get("noisePercent", 0)),
            "signalDb": float(thd["signalDb"]),
            "timestamp": time(),
        })


TURNTABLE_TOOLS = (
    ("rpm", "Speed stability (RPM)"),
    ("crosstalk", "Crosstalk and azimuth"),
    ("balance", "Channel balance"),
    ("tracking", "Tracking distortion"),
    ("noise", "Noise floor"),
    ("resonance", "Low-frequency resonance"),
)


@dataclass(frozen=True, slots=True)
class _SeparationCapture:
    channel: int
    rate: float
    frame: int
    epoch: object
    started: float
    route: tuple
    frequency: float
    clock_scale: float
    trims_db: tuple


class TurntablePanel(MeasurementPanel):
    _SEPARATION_WINDOW = .8

    def __init__(self, parent, controller):
        super().__init__(parent, controller)
        set_control_help(self, "Check turntable speed, channel alignment, tracking, noise or resonance.")
        self.readout_label = "Turntable readings"
        self.recording = None
        self.last_sequence = None
        self._speed_context = None
        self._speed_origin = None
        self._speed_history = deque(maxlen=3000)
        self._speed_chart_key = None
        self._auto_rpm_reference = None
        self._rpm_pref = self._rpm_preferences()
        self._capture_request = None
        self._separation = {}
        self._separation_route = None
        self._separation_dirty = False
        self._separation_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._poll_separation, self._separation_timer)
        self.Bind(wx.EVT_WINDOW_DESTROY, self._stop_separation_timer)
        self.latest_spectrum = {}
        self.latest_spectrum_stats = {}
        self.default_view = controller.settings.get("defaultTurntableView", "rpm")

        self.tools = tuple(key for key, _ in TURNTABLE_TOOLS)
        views = [title for _, title in TURNTABLE_TOOLS]
        view_index = self.tools.index(self.default_view) if self.default_view in self.tools else 0
        self.selection_row = self.row(wrap=True)
        self.tool = self.choice(
            self.selection_row, "Test", views, view_index, self._tool_changed,
            help_text="Select the measurement to show or record. Each test uses its own setup instructions.",
        )
        self.speed = self.choice(
            self.selection_row, "Playback speed", SPEED_LABELS, action=self._rpm_changed,
            help_text="Choose the intended turntable speed. The analyzer compares the measured RPM with this value.",
        )
        self.playback_field = self.selection_row.GetChildren()[-1].GetSizer()
        self.record_speed = self.choice(
            self.selection_row, "Test record speed", SPEED_LABELS, PLAYBACK_SPEEDS.index(self._rpm_pref[0]),
            self._rpm_changed,
            help_text="Choose the playback speed specified for the reference tone on your test record.",
        )
        self.record_field = self.selection_row.GetChildren()[-1].GetSizer()
        self.rpm_options = self.row(wrap=True)
        self.reference_choice = self.choice(
            self.rpm_options, "Reference tone", ("Detect 3,000 / 3,150 Hz", "Enter frequency"),
            int(not self._rpm_pref[3]), self._rpm_changed,
            help_text="Detect a standard reference tone automatically, or enter the frequency printed on the record.",
        )
        self.reference_hz = self.spin(
            self.rpm_options, "Reference (Hz)", 1, 24000, self._rpm_pref[2], increment=1, digits=2,
            action=self._rpm_changed, help_text="Enter the nominal frequency of the test track at the stated record speed.",
        )
        self.custom_record_speed = self.spin(
            self.rpm_options, "Custom record RPM", 1, 300, self._rpm_pref[1], increment=.1, digits=3,
            action=self._rpm_changed, help_text="Enter the playback speed specified for a record with a custom test speed.",
        )
        self.custom_playback_speed = self.spin(
            self.rpm_options, "Custom playback RPM", 1, 300, 100 / 3, increment=.1, digits=3,
            action=self._rpm_changed, help_text="Enter the target turntable speed to compare against the measured speed.",
        )
        self.capture_row = wx.WrapSizer(wx.HORIZONTAL, wx.REMOVE_LEADING_SPACES)
        self.content.Add(self.capture_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, self.FromDIP(10))
        self.capture_left = self._separation_button(self.capture_row, "Capture Left", "separation_left",
                                                    lambda: self._take_separation(0))
        self.capture_right = self._separation_button(self.capture_row, "Capture Right", "separation_right",
                                                     lambda: self._take_separation(1))
        self.save_separation = self._separation_button(self.capture_row, "Save Readings", "separation_save",
                                                       self._save_separation)
        self.clear_separation = self._separation_button(self.capture_row, "Clear Readings", "separation_clear",
                                                        self._clear_separation)
        self.cancel_separation = self._separation_button(self.capture_row, "Cancel Capture", "separation_cancel",
                                                         self._cancel_separation)
        self.separation_options = self.row(wrap=True)
        self.separation_hz = self.spin(
            self.separation_options, "Expected tone (Hz)", 0, 24000, 0, increment=1, digits=2,
            help_text="Enter the test track frequency, or enter zero to detect the strongest test tone automatically.",
        )
        self.separation_status = self.label(
            self.separation_options, "", "Reports the capture progress, signal quality and measured resolution.",
        )
        self.turntable_cards = self.metric_readouts(("Reading", "Reference"))
        self.separation_live_row = self.row(wrap=True)
        self.separation_live = self.label(
            self.separation_live_row, "",
            "Live RMS level of the test tone on each channel. Includes any input gain trims.",
        )
        self.separation_details = self.row()
        details = wx.BoxSizer(wx.VERTICAL)
        self.separation_details.Add(details, 1, wx.EXPAND)
        self.separation_quality = [wx.StaticText(self), wx.StaticText(self)]
        for label in self.separation_quality:
            set_control_help(label, "Frequency, signal levels, resolution and phase for this direction.")
            details.Add(label, 0, wx.EXPAND | wx.BOTTOM, self.FromDIP(8))
        self.description = self.label(
            self.row(), "", "Instructions for setting up the current turntable test. Use Test to choose another measurement.",
        )
        self.confidence = self.label(self.row(wrap=True), "Waiting for audio.", CONFIDENCE_HELP)
        self.balance_scale = BalanceScale(self)
        self.content.Add(self.balance_scale, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, self.FromDIP(12))
        self.add_plot(
            "Turntable measurements", "Frequency (Hz)", "Level (dB)",
            x_formatter=lambda value: f"{value:.2f}", y_formatter=lambda value: f"{value:.2f}",
        )
        self._tool_changed()
        self.refresh_settings()

    def desktop_actions(self):
        if self.recording is not None:
            action = ("record", "Stop Recording", self._toggle_recording)
        elif self.active_tool in ("crosstalk", "balance", "tracking") and self.controller.settings.get("channels", 1) != 2:
            action = ("stereo", "Enable Stereo Input", self._set_stereo)
        else:
            action = ("record", "Start Recording", self._toggle_recording)
        return [action, *super().desktop_actions()]

    def desktop_options(self):
        actions = [
            ("audio_settings", "Audio Settings", lambda: self.controller.navigate("settings", "audio")),
            ("view_data", "View Results", self._view_results),
        ]
        if self.active_tool not in ("crosstalk", "balance", "tracking") or self.controller.settings.get("channels", 1) == 2:
            actions.insert(0, ("input", "Start Live Input", self.start_live_input))
            if self.active_tool in ("crosstalk", "balance"):
                actions.insert(1, ("phase", "Measure Phase in Scope", self._show_phase))
        if self.active_tool == "crosstalk":
            controls = (
                ("separation_left", self.capture_left, lambda: self._take_separation(0)),
                ("separation_right", self.capture_right, lambda: self._take_separation(1)),
                ("separation_save", self.save_separation, self._save_separation),
                ("separation_clear", self.clear_separation, self._clear_separation),
                ("separation_cancel", self.cancel_separation, self._cancel_separation),
            )
            actions = [(key, button.GetLabel(), callback) for key, button, callback in controls
                       if button.IsShown() and button.IsEnabled()] + actions
        return [*actions, *super().desktop_options()]

    def keyboard_actions(self):
        fields = (
            ("Select Turntable Test", self.tool), ("Select Playback Speed", self.speed),
            ("Select Test Record Speed", self.record_speed), ("Select Speed Reference", self.reference_choice),
            ("Edit Reference Frequency", self.reference_hz), ("Edit Custom Record RPM", self.custom_record_speed),
            ("Edit Custom Playback RPM", self.custom_playback_speed), ("Edit Separation Tone", self.separation_hz),
        )
        return self._focus_actions(fields)

    @property
    def active_tool(self):
        return self.tools[self.tool.GetSelection()]

    @property
    def is_capturing(self):
        return self.recording is not None or self._capture_request is not None

    @property
    def has_pending_readings(self):
        return self.is_capturing or self._separation_dirty

    def finish_session_readings(self):
        if self.recording is not None:
            self._toggle_recording()
        self._cancel_separation()
        self._save_separation()

    def reset_session_readings(self):
        if self._separation:
            self._clear_separation()

    def set_view(self, view):
        if view in self.tools and self.recording is None:
            self.tool.SetSelection(self.tools.index(view))
            self._tool_changed()

    def refresh_settings(self):
        super().refresh_settings()
        default = self.controller.settings.get("defaultTurntableView", "rpm")
        if default != self.default_view:
            self.default_view = default
            self.set_view(default)
        current = self._rpm_preferences()
        if current != self._rpm_pref and self.recording is None:
            previous, self._rpm_pref = self._rpm_pref, current
            if current[0] != previous[0]:
                self.record_speed.SetSelection(PLAYBACK_SPEEDS.index(current[0]))
            if current[1] != previous[1]:
                self.custom_record_speed.SetValue(current[1])
            if current[2] != previous[2]:
                self.reference_hz.SetValue(current[2])
            if current[3] != previous[3]:
                self.reference_choice.SetSelection(int(not current[3]))
            self._auto_rpm_reference = None
        self._sync_turntable_controls()
        self._update_view()

    def _rpm_preferences(self):
        settings = self.controller.settings
        nominal = settings.get("testRecordSpeed", 33)
        return (nominal if nominal in PLAYBACK_SPEEDS else 33, settings.get("testRecordCustomRpm", 100 / 3),
                settings.get("targetFreq", 3000), settings.get("useAutoTarget", True),
                settings.get("turntable78Reference", 78.26))

    def _rpm_changed(self):
        self._auto_rpm_reference = None
        self._sync_turntable_controls()
        self._update_view()

    def _sync_turntable_controls(self):
        rpm = self.active_tool == "rpm"
        crosstalk = self.active_tool == "crosstalk"
        editing = self.recording is None
        stereo = self.controller.settings.get("channels", 1) == 2
        for field in (self.playback_field, self.record_field):
            self.selection_row.Show(field, rpm, recursive=True)
        self.content.Show(self.rpm_options, rpm, recursive=True)
        for control in (self.speed, self.record_speed, self.reference_choice):
            control.Enable(editing)
        self.reference_hz.Enable(editing and self.reference_choice.GetSelection() == 1)
        self.custom_record_speed.Enable(editing and PLAYBACK_SPEEDS[self.record_speed.GetSelection()] == 0)
        self.custom_playback_speed.Enable(editing and PLAYBACK_SPEEDS[self.speed.GetSelection()] == 0)
        for field, selected in ((self.reference_hz, self.reference_choice.GetSelection() == 1),
                                (self.custom_record_speed, PLAYBACK_SPEEDS[self.record_speed.GetSelection()] == 0),
                                (self.custom_playback_speed, PLAYBACK_SPEEDS[self.speed.GetSelection()] == 0)):
            for item in self.rpm_options.GetChildren():
                if (group := item.GetSizer()) is not None and group.GetItem(field) is not None:
                    self.rpm_options.Show(group, rpm and selected, recursive=True)
                    break
        self.content.Show(self.capture_row, crosstalk, recursive=True)
        self.content.Show(self.separation_options, crosstalk, recursive=True)
        self.content.Show(self.separation_live_row, crosstalk and stereo, recursive=True)
        self.content.Show(self.separation_details, crosstalk and bool(self._separation), recursive=True)
        for channel, label in enumerate(self.separation_quality):
            label.Show(crosstalk and channel in self._separation)
        idle = self._capture_request is None
        for button in (self.capture_left, self.capture_right, self.separation_hz):
            button.Enable(stereo and editing and idle)
        self.save_separation.Enable(bool(self._separation) and self._separation_dirty and idle)
        self.clear_separation.Enable(bool(self._separation) and idle)
        self.cancel_separation.Show(crosstalk and not idle)
        self.Layout()

    def _set_stereo(self):
        self.controller.apply_settings(channels=2)
        self.refresh_settings()
        if self.controller.settings.get("channels") == 2:
            self.tool.SetFocus()

    def _separation_button(self, sizer, label, help_key, action):
        button = wx.Button(self, label=label)
        set_control_help(button, COMMAND_HELP[help_key])
        button.Bind(wx.EVT_BUTTON, lambda _event: action())
        sizer.Add(button, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT | wx.BOTTOM, self.FromDIP(5))
        return button

    def _stop_separation_timer(self, event):
        event.Skip()
        if event.GetEventObject() is self:
            self._separation_timer.Stop()
            self._capture_request = None

    def _separation_route_key(self):
        return self.controller.audio_input_identity(), self.controller.settings.get("channels", 1)

    def _take_separation(self, channel):
        if (not self.IsShownOnScreen() or self._capture_request is not None
                or self.active_tool != "crosstalk" or self.recording is not None):
            return
        if self.controller.settings.get("channels", 1) != 2:
            self.controller.announce("Select a stereo input to measure separation in both directions.")
            return
        frequency = self.separation_hz.GetValue()

        def resume():
            if self and frequency == self.separation_hz.GetValue():
                self._take_separation(channel)

        if not self.controller.ensure_audio(after_permission=resume, require_physical=True):
            return
        audio = self.controller.audio
        rate, _, frame, epoch = audio.input_observation(0)
        trims = self.controller.settings.get("channelCalibration", {})
        clock_scale = self.controller.settings.get("correctionFactor", 1.0)
        self._capture_request = _SeparationCapture(
            channel, rate, frame, epoch, monotonic(), self._separation_route_key(), frequency,
            clock_scale, (trims.get("leftTrimDb", 0.0), trims.get("rightTrimDb", 0.0)),
        )
        self.separation_status.SetLabel(f"Capturing {'left' if channel == 0 else 'right'} tone…")
        self._sync_turntable_controls()
        self._separation_timer.Start(70)

    def _poll_separation(self, _event):
        request = self._capture_request
        if request is None:
            self._separation_timer.Stop()
            return
        audio = self.controller.audio
        seconds = self._SEPARATION_WINDOW * request.clock_scale
        rate, frames, frame, epoch = audio.input_observation(seconds)
        if rate != request.rate or self._separation_route_key() != request.route:
            self._finish_separation(request, None, "The input changed during the capture. Repeat the test.")
            return
        # The input device can keep its name and rate while its stream is
        # reopened or drops frames. Do not combine observations across breaks.
        if request.epoch is not None and epoch != request.epoch:
            self._finish_separation(request, None, "The input was interrupted during the capture. Repeat the test.")
            return
        if request.epoch is None and epoch is not None:
            request = self._capture_request = replace(request, epoch=epoch)
        if monotonic() - request.started > 4:
            self._finish_separation(request, None, "The test did not receive a continuous signal. Check the selected input.")
            return
        minimum = math.ceil(seconds * rate)
        if len(frames) < minimum or frame - request.frame < minimum:
            return
        self._separation_timer.Stop()
        self.separation_status.SetLabel("Measuring separation…")

        def measure():
            try:
                reading = stereo_separation(frames, rate * request.clock_scale, request.channel,
                                            expected_hz=request.frequency, trims_db=request.trims_db)
                wx.CallAfter(self._finish_separation, request, reading, "")
            except (ValueError, ArithmeticError, np.linalg.LinAlgError) as error:
                wx.CallAfter(self._finish_separation, request, None, str(error))

        Thread(target=measure, name="Fluttery stereo separation", daemon=True).start()

    def _finish_separation(self, request, reading, error):
        if not self or self.IsBeingDeleted() or self._capture_request is not request:
            return
        self._separation_timer.Stop()
        self._capture_request = None
        if reading is not None:
            route = request.route, request.rate, request.clock_scale, request.trims_db, request.frequency
            if route != self._separation_route:
                self._separation.clear()
                self._separation_route = route
            self._separation[request.channel] = reading, time()
            self._separation_dirty = True
            self.separation_status.SetLabel(f"{reading.direction} captured at {reading.frequency:.2f} Hz.")
            limit = "At least " if reading.limited else ""
            self.controller.announce(f"{reading.direction} separation: {limit}{reading.separation_db:.2f} dB.")
        else:
            self.separation_status.SetLabel(error)
            self.controller.announce(error)
        self._sync_turntable_controls()
        self._update_view()

    def _cancel_separation(self):
        if self._capture_request is not None:
            self._finish_separation(self._capture_request, None, "Separation capture cancelled.")

    def _clear_separation(self):
        self._separation.clear()
        self._separation_dirty = False
        self._separation_route = None
        self.separation_status.SetLabel("")
        self._sync_turntable_controls()
        self._update_view()

    def _save_separation(self):
        if not self._separation or not self._separation_dirty:
            return
        rows = []
        for channel in sorted(self._separation):
            reading, timestamp = self._separation[channel]
            rows.append({
                "timestamp": timestamp, "direction": reading.direction, "frequencyHz": reading.frequency,
                "drivenDbfs": reading.driven_dbfs, "leakageDbfs": reading.leakage_dbfs,
                "noiseDbfs": reading.noise_dbfs, "resolutionDbfs": reading.resolution_dbfs,
                "separationDb": reading.separation_db, "phaseDegrees": reading.phase_deg, "limited": reading.limited,
            })
        self.controller.log_measurement("crosstalk", rows, {"bidirectional": len(rows) == 2,
                                                            "capturedDirections": len(rows)})
        self._separation_dirty = False
        self._sync_turntable_controls()
        self.controller.announce("The stereo separation readings were saved to this session.")

    def _show_phase(self):
        if self and self.IsShownOnScreen() and self.controller.ensure_audio(after_permission=self._show_phase):
            self.controller.navigate("scope", "xy")

    def _view_results(self):
        tool = self.active_tool
        if tool == "tracking":
            entries = self.controller.session["measurementLog"]
            tracking = (entry for entry in entries if entry["type"] == "tracking")
            latest = max(tracking, key=lambda entry: entry["timestamp"], default={}).get("data")
            if not (isinstance(latest, list) and latest and isinstance(latest[0], (float, int))):
                self.controller.navigate("results", "thd")
                return
        self.controller.results.review_measurements(tool)

    def _tool_changed(self):
        tool = self.active_tool
        if tool != "crosstalk":
            self._cancel_separation()
        self._auto_rpm_reference = None
        self._sync_turntable_controls()
        hints = {
            "rpm": "Play the reference tone. Select the speed you want to check.",
            "crosstalk": "Play a test tone on one channel only and capture it. Repeat the test on the other channel.",
            "balance": "Play a test tone with equal levels on both channels.",
            "tracking": "Play a tracking test tone and compare distortion in the two channels.",
            "noise": "Play a silent groove to measure the noise floor and rumble.",
            "resonance": "Play a resonance test track or observe a low-frequency sweep.",
        }
        self.description.SetLabel(hints[tool])
        self.balance_scale.Show(tool == "balance")
        self.plot.Show(tool in ("rpm", "tracking", "noise", "resonance"))
        self.plot.clear()
        self._speed_history.clear()
        self._speed_origin = self._speed_chart_key = None
        self.plot.log_x = tool in ("tracking", "noise")
        self._spectrum_bounds = None
        self.plot.set_default_view(xlim=(0, 30) if tool == "resonance" else None)
        self.plot.fit()
        self.plot.set_labels(
            title={"rpm": "Turntable speed over time", "tracking": "Tracking spectrum", "noise": "Noise floor spectrum",
                   "resonance": "Low-frequency resonance spectrum"}.get(tool, "Turntable measurements"),
            x_label="Elapsed time (s)" if tool == "rpm" else "Frequency (Hz)",
            y_label="Speed (RPM)" if tool == "rpm" else "Level (dB)",
        )
        self.Layout()
        self._update_view()

    def _update_view(self):
        self._present(self.snapshot, draw=True, record=False)

    def _toggle_recording(self):
        if self.recording is None:
            if self._capture_request is not None:
                self.controller.announce("Finish or cancel the stereo separation capture first.")
                return
            tool = self.active_tool

            def resume():
                if self and self.IsShownOnScreen() and self.recording is None and tool == self.active_tool:
                    self._toggle_recording()

            if not self.controller.ensure_audio(after_permission=resume):
                return
            limit = self.controller.settings.get("maxSessionSamples", 1000)
            self.recording = deque(maxlen=max(1, limit))
            self.latest_spectrum = {}
            self.latest_spectrum_stats = {}
            self.last_sequence = None
            self.tool.Disable()
            self.speed.Disable()
            self._sync_turntable_controls()
            self.controller.play_sound("start")
            self.controller.announce("Turntable recording started.")
            return

        kind = self.active_tool
        spectrum_only = kind in ("noise", "resonance")
        tracking_spectrum = kind == "tracking" and not self.recording
        if spectrum_only:
            # Quick captures can start and stop between display updates.
            # Use the live spectrum and its matching diagnostic readings.
            self._present(self.controller.audio.snapshot(), draw=False, record=True)
        elif tracking_spectrum:
            snapshot = self.controller.audio.snapshot()
            if snapshot.get("health", {}).get("running"):
                self._cache_spectrum(snapshot.get("stereo_spectrum", {}).get("left", {}))
        data = list(self.recording)
        self.recording = None
        self.tool.Enable()
        self.speed.Enable()
        self.controller.play_sound("stop")
        self.refresh_settings()
        if spectrum_only or tracking_spectrum:
            data, stats = self._recorded_spectrum()
        else:
            stats = self._recorded_stats(kind, data) if data else {}
        if data:
            self.controller.log_measurement(kind, data, stats)
            self.controller.announce("The tracking spectrum was saved to the session." if tracking_spectrum else
                                     "Turntable recording saved to session.")
        else:
            self.controller.announce("No usable signal was captured.")

    def _cache_spectrum(self, spectrum, stats=None):
        if spectrum is self.latest_spectrum:
            return bool(spectrum)
        frequency = spectrum.get("frequencies", ())
        db = spectrum.get("db", ())
        if not len(db) or len(frequency) != len(db):
            return False
        self.latest_spectrum = spectrum
        self.latest_spectrum_stats = stats or {}
        return True

    def _recorded_spectrum(self):
        frequencies = np.asarray(self.latest_spectrum.get("frequencies", ()))
        db = np.asarray(self.latest_spectrum.get("db", ()))
        if not len(frequencies) or len(frequencies) != len(db):
            return [], {}
        stats = {
            "spectrumEncoding": "db",
            "startHz": float(frequencies[0]),
            "binHz": float(frequencies[1] - frequencies[0]) if len(frequencies) > 1 else 0.0,
            "peakFrequency": float(self.latest_spectrum.get("peakFrequency") or 0),
            "peakDb": float(self.latest_spectrum.get("peakDb") or 0),
            **self.latest_spectrum_stats,
        }
        return db.tolist(), stats

    @staticmethod
    def _recorded_stats(kind, data):
        if kind == "crosstalk":
            return {"avgL": mean(point["left"] for point in data), "avgR": mean(point["right"] for point in data)}
        if kind == "tracking":
            left = [point["thd"] for point in data if "thd" in point]
            right = [point["rightThdn"] for point in data if "rightThdn" in point]
            stats = {"avg": mean(left), "min": min(left), "max": max(left)} if left else {}
            if right:
                stats.update(rightAvg=mean(right), rightMin=min(right), rightMax=max(right))
            return stats
        values = [point[kind] for point in data]
        return {"avg": mean(values), "min": min(values), "max": max(values)}

    def refresh_measurement(self, snapshot):
        super().refresh_measurement(snapshot)
        draw = self.IsShownOnScreen()
        if draw or self.recording is not None:
            self._present(snapshot, draw=draw, record=self.recording is not None)

    def _rpm_context(self, metrics):
        reference_78 = self._rpm_pref[4]
        record_rpm = nominal_speed(PLAYBACK_SPEEDS[self.record_speed.GetSelection()], reference_78,
                                   self.custom_record_speed.GetValue())
        playback_rpm = nominal_speed(PLAYBACK_SPEEDS[self.speed.GetSelection()], reference_78,
                                     self.custom_playback_speed.GetValue())
        detected = metrics.get("currentFreq", 0)
        if self.reference_choice.GetSelection() == 1:
            return record_rpm, playback_rpm, self.reference_hz.GetValue()
        if abs(detected) <= 10 or not np.isfinite(detected):
            if self.recording is None:
                self._auto_rpm_reference = None
        elif self._auto_rpm_reference is None and not metrics.get("clipped"):
            self._auto_rpm_reference = automatic_record_reference(detected, record_rpm, playback_rpm)
        return record_rpm, playback_rpm, self._auto_rpm_reference

    def _separation_readout(self, stereo, *, draw=True):
        lines = []
        readings = []
        for channel, caption in enumerate(("Left → right", "Right → left")):
            result = self._separation.get(channel)
            if result is None:
                readings.append((caption, "Awaiting capture", False))
                continue
            reading = result[0]
            separation = f"{'≥ ' if reading.limited else ''}{reading.separation_db:.2f} dB"
            readings.append((caption, separation, False))
            certainty = " Limited by the measurement floor." if reading.limited else ""
            phase = f" Phase {reading.phase_deg:+.2f} degrees." if reading.phase_deg is not None else ""
            header = f"{reading.direction} · {reading.frequency:,.2f} Hz · {'Floor limited' if reading.limited else phase.strip()}"
            levels = (f"Drive {reading.driven_dbfs:.1f} dBFS · Leakage {reading.leakage_dbfs:.1f} dBFS · "
                      f"Noise {reading.noise_dbfs:.1f} dBFS · Resolution {reading.resolution_dbfs:.1f} dBFS")
            details = f"{header}\n{levels}"
            if self.separation_quality[channel].GetLabel() != details:
                self.separation_quality[channel].SetLabel(details)
                self.Layout()
            lines.append(f"{reading.direction}: {separation} at {reading.frequency:.2f} Hz. "
                         f"Driven tone {reading.driven_dbfs:.1f} dBFS. Leakage {reading.leakage_dbfs:.1f} dBFS. "
                         f"Residual noise {reading.noise_dbfs:.1f} dBFS. "
                         f"Tone resolution {reading.resolution_dbfs:.1f} dBFS.{phase}{certainty}")
        if len(self._separation) == 2:
            a, b = (reading.frequency for reading, _timestamp in self._separation.values())
            if abs(a - b) > .01 * min(a, b):
                lines.append("The two separation measurements used different frequencies.")
        if stereo.get("available"):
            levels = []
            for side in ("left", "right"):
                raw = stereo.get(f"raw{side.title()}Db")
                reading = ("below the measurement floor" if raw is not None and raw <= FLOOR_DB else
                           _format_number(stereo.get(f"{side}Db"), 1, " dBFS RMS"))
                levels.append(f"{side} {reading}")
            live = f"Live test tone: {' · '.join(levels)}"
            lines.append(f"Channel balance: {stereo.get('balanceDb', 0):+.2f} dB, left minus right.")
        else:
            live = "Live test tone: input clipped" if stereo.get("clipped") else "Live test tone: awaiting input"
        if draw and self.separation_live.GetLabel() != live:
            self.separation_live.SetLabel(live)
        lines.append(live + ".")
        if status := self.separation_status.GetLabel():
            lines.append(status)
        return readings, lines

    def _present(self, snapshot, *, draw, record):
        tool = self.active_tool
        metrics = snapshot.get("metrics", {})
        health = snapshot.get("health", {})
        stereo = snapshot.get("stereo", {})
        sequence = metrics.get("sequence")
        running = health.get("running")
        capture = record and running and (sequence is None or sequence != self.last_sequence)
        point = None
        accent, secondary = self.theme_accents()
        if tool == "rpm":
            record_speed, speed, target = self._rpm_context(metrics)
            reference_text = f"{target:g} Hz" if target is not None else "waiting for a clear test tone"
            nominal, record_label = display_rpm(speed), display_rpm(record_speed)
            if draw:
                hint = f"Test record: {record_label}. Playback target: {nominal}. Reference: {reference_text}."
                if self.description.GetLabel() != hint:
                    self.description.SetLabel(hint)
            lines = [f"Playback speed: {nominal}. Test record: {record_label}. Reference: {reference_text}."]
            speed_reading = _turntable_speed(metrics, target, record_speed, speed) if running and target else None
            if speed_reading is not None:
                rpm, deviation = speed_reading
                lines.append(f"Measured speed: {rpm:.3f} RPM. Deviation: {deviation:+.3f}%.")
                threshold = "more than 1%" if abs(deviation) > 1 else "more than 0.5%"
                if abs(deviation) > 0.5:
                    lines.append(f"Speed differs from {nominal} by {threshold}.")
                readings = (
                    ("Measured speed", f"{rpm:.3f} RPM", False),
                    ("Speed deviation", f"{deviation:+.3f}%", abs(deviation) > 0.5),
                )
                point = {"rpm": rpm, "deviation": deviation, "testRecordRpm": record_speed, "playbackRpm": speed,
                         "referenceHz": target, "frequencyHz": metrics["currentFreq"]}
            else:
                if metrics.get("clipped"):
                    lines.append("Input clipped. Reduce the recording level.")
                elif self.reference_choice.GetSelection() == 0 and metrics.get("currentFreq", 0) > 10:
                    lines.append("Fluttery could not identify the reference tone with confidence. Select Enter frequency for this track.")
                else:
                    lines.append("Waiting for the reference tone.")
                readings = (("Measured speed", "Unavailable", False), ("Playback speed", nominal, False))
            if draw and running:
                timestamp = metrics.get("timestamp")
                self._render_speed_history(timestamp, point["rpm"] if point else None,
                                           (speed, record_speed, target), accent)
        elif tool == "crosstalk":
            if stereo.get("available"):
                left, right = stereo["leftDb"], stereo["rightDb"]
                point = {"left": float(left), "right": float(right), "balance": float(stereo["balanceDb"])}
                if left != right:
                    key = "leftToRightDb" if left > right else "rightToLeftDb"
                    leakage = stereo.get(key)
                    if leakage is not None:
                        point[key] = float(leakage)
            readings, lines = self._separation_readout(stereo if running else {}, draw=draw)
        elif tool == "balance":
            balance = stereo.get("balanceDb") if stereo.get("available") else None
            if draw:
                self.balance_scale.set_reading(balance, accent if balance is None or abs(balance) <= 1 else secondary)
            if balance is None:
                lines = ["Waiting for the reference tone."]
                readings = (("Channel balance", "Unavailable", False), ("Louder channel", "Unavailable", False))
            else:
                direction = "Centred" if abs(balance) < 0.005 else ("Left" if balance > 0 else "Right")
                lines = [
                    f"Balance: {direction}, {abs(balance):.2f} dB.",
                    f"Left channel: {_db(stereo.get('leftDb'))}. Right channel: {_db(stereo.get('rightDb'))}.",
                ]
                if abs(balance) > 1:
                    lines.append("The channel difference exceeds 1 dB.")
                readings = (
                    ("Channel balance", f"{abs(balance):.2f} dB", abs(balance) > 1),
                    ("Louder channel", direction, False),
                )
                point = {"left": float(stereo["leftDb"]), "right": float(stereo["rightDb"]),
                         "balance": float(balance)}
        elif tool == "tracking":
            tracking = snapshot.get("tracking", {})
            left = tracking.get("left", {})
            right = tracking.get("right", {})
            spectra = snapshot.get("stereo_spectrum", {})
            # The FFT can update within an analysis block and may be usable
            # even when there is no fundamental for a THD reading.
            if record and running:
                self._cache_spectrum(spectra.get("left", {}))
            left_thdn = left.get("thdn") if left.get("available") else None
            right_thdn = right.get("thdn") if right.get("available") else None
            readings = (
                ("Left THD plus noise", _format_number(left_thdn, 3, "%"), left_thdn is not None and left_thdn > 1),
                ("Right THD plus noise", _format_number(right_thdn, 3, "%"), right_thdn is not None and right_thdn > 1),
            )
            lines = [
                f"Left THD plus noise: {readings[0][1]}. Right THD plus noise: {readings[1][1]}.",
                (f"Left fundamental: {_format_number(left.get('fundamentalFrequency') if left_thdn is not None else None, 1, ' Hz')}. "
                 f"Right fundamental: {_format_number(right.get('fundamentalFrequency') if right_thdn is not None else None, 1, ' Hz')}."),
                (f"Signal confidence, left: {left.get('confidence', 'No signal')}. "
                 f"Right: {right.get('confidence', 'No signal')}."),
            ]
            if left_thdn is not None or right_thdn is not None:
                point = {}
                if left_thdn is not None:
                    point.update(thd=float(left_thdn), thdOnly=left["thd"], fundamental=float(left["fundamentalFrequency"]))
                if right_thdn is not None:
                    point.update(rightThdn=float(right_thdn), rightThdOnly=right["thd"],
                                 rightFundamental=float(right["fundamentalFrequency"]))
            if draw:
                self.show_spectrum(
                    _spectrum_series(spectra.get("left", {}), "Left", color=accent)
                    + [dict(trace, style="dashed") for trace in
                       _spectrum_series(spectra.get("right", {}), "Right", color=secondary)]
                )
        else:
            spectrum = snapshot.get("resonance_spectrum" if tool == "resonance" else "spectrum", {})
            if tool == "resonance":
                reading = snapshot.get("resonance", {})
                frequency = reading.get("frequency")
                measured_peak = _format_number(frequency if frequency else None, 2, " Hz")
                readings = (
                    ("Peak resonance", measured_peak, bool(frequency and not 8 <= frequency <= 12)),
                    ("Resonance level", _db(reading.get("levelDb")) if frequency else "Unavailable", False),
                )
                lines = [
                    f"Peak resonance: {measured_peak}. Resonance level: {readings[1][1]}.",
                    "Typical target range: 8 to 12 Hz.",
                ]
                spectrum_stats = {key: float(value) for key, value in reading.items()
                                  if key in ("frequency", "levelDb") and value is not None}
            else:
                reading = snapshot.get("noise", {})
                readings = (
                    ("RMS noise level", _db(reading.get("rmsDb")), False),
                    ("Low-frequency rumble", _db(reading.get("rumbleDb")), False),
                )
                lines = [
                    f"RMS noise level: {_db(reading.get('rmsDb'))}.",
                    f"Low-frequency rumble: {_db(reading.get('rumbleDb'))}.",
                    f"Strongest frequency: {_format_number(spectrum.get('peakFrequency'), 1, ' Hz')}.",
                ]
                spectrum_stats = {key: float(value) for key, value in reading.items()
                                  if key in ("rmsDb", "rumbleDb") and value is not None}
            if record and running:
                self._cache_spectrum(spectrum, spectrum_stats)
            if draw:
                self.show_spectrum(
                    _spectrum_series(spectrum, ceiling=30 if tool == "resonance" else None, color=accent)
                )

        if capture and point is not None:
            self.last_sequence = sequence
            self.recording.append({**point, "timestamp": time()})
        if draw:
            needs_stereo = tool in ("crosstalk", "balance", "tracking") and self.controller.settings.get("channels", 1) != 2
            self.confidence.Show(not needs_stereo and tool != "crosstalk")
            if needs_stereo:
                lines = ["This test requires a stereo input. Choose Enable Stereo Input to continue."]
                readings = (
                    ("Input configuration", "Stereo required", True),
                    ("Channels selected", "1", False),
                )
                if tool == "balance":
                    self.balance_scale.set_reading(None, accent)
                self.plot.clear()
            else:
                if not running and not record:
                    lines.insert(0, "Start live input to measure your turntable.")
                if tool == "balance" and stereo.get("available") and "toneFrequency" in stereo:
                    lines.append(f"Stereo test tone: {stereo['toneFrequency']:.1f} Hz. Reference: {stereo['targetFrequency']:g} Hz.")
                if tool != "crosstalk":
                    frequency = metrics.get("currentFreq") or None
                    confidence = _measurement_confidence(metrics, health)
                    if self.confidence.GetLabel() != confidence:
                        self.confidence.SetLabel(confidence)
                    lines.append(f"Detected frequency: {_format_number(frequency, 1, ' Hz')}. {confidence}.")
                    lines.append(f"Reference losses and capture gaps: {metrics.get('dropouts', 0)}.")
            if self.recording is not None:
                lines.append("Recording.")
            self.set_metrics(self.turntable_cards, readings)
            self.set_summary("\n".join(lines))

    def _render_speed_history(self, timestamp, rpm, context, accent):
        if timestamp is None:
            return
        history = self._speed_history
        if context != self._speed_context or (history and timestamp < history[-1][0]):
            # A different reference or a restarted audio clock begins its own
            # trend. The recorded session is independent of this live view.
            history.clear()
            self.plot.clear()
            self._speed_context = context
            self._speed_origin = self._speed_chart_key = None
        if self._speed_origin is None:
            if rpm is None:
                return
            self._speed_origin = timestamp
        if not history or history[-1][0] < timestamp:
            history.append((timestamp, rpm if rpm is not None else math.nan))
        offset = timestamp - self._speed_origin
        while history and history[0][0] < timestamp - 30:
            history.popleft()
        key = (timestamp, accent)
        if self._speed_chart_key == key:
            return
        self._speed_chart_key = key
        times, speeds = zip(*history, strict=True)
        times = np.asarray(times) - self._speed_origin
        nominal = context[0]
        left, right = max(0, offset - 30), max(1, offset)
        visible = [value for value in speeds if math.isfinite(value)]
        low = min(nominal * 0.9975, min(visible, default=nominal))
        high = max(nominal * 1.0025, max(visible, default=nominal))
        margin = 0.07 * (high - low)
        self.plot.set_default_view(xlim=(left, right), ylim=(low - margin, high + margin))
        self.plot.set_series([
            {"label": "Measured speed", "x": times, "y": speeds, "color": accent},
            {"label": "Nominal speed", "x": (left, right), "y": (nominal, nominal), "color": "#6a7078", "style": "dashed"},
        ])


def _generator_preview(settings, sample_rate):
    """Render configured output samples with no device or shared audio state."""
    source = SignalGenerator()
    # Preview the steady output after the start ramp. Use a reproducible
    # noise example so editing the gain does not change the signal's shape.
    source.amplitude = settings.level
    source.noise = NoiseSource(seed=1255)
    frequency = settings.effective_frequency(sample_rate)
    span = 0.02 if settings.waveform in ("pink", "white") else min(1, 4 / frequency)
    count = max(12, math.ceil(sample_rate * span) + 1)
    samples = source.render(count, sample_rate, settings)
    return np.arange(count) * (1000 / sample_rate), samples


class GeneratorPanel(MeasurementPanel):
    waveforms = ("sine", "square", "sawtooth", "triangle", "white", "pink")

    def __init__(self, parent, controller):
        super().__init__(parent, controller)
        set_control_help(self, "Generate adjustable test tones for checking audio equipment or Fluttery's measurements.")
        self.readout_label = "Generator readings"
        self.generating = False
        self.show_advanced = False
        target = float(controller.settings.get("targetFreq", 3000))
        self.custom_frequency = target if target not in (3000, 3150) else 1000.0
        self._target = target

        self._preview_key = self._preview_xlim = None
        self.generator_cards = self.metric_readouts(("Output frequency", "Output level", "Generator"))
        self.tone = self.row(wrap=True)
        self.preset = self.choice(
            self.tone, "Frequency standard", ("3000 Hz", "3150 Hz", "Custom"),
            action=self._preset_changed,
            help_text="Choose a standard wow and flutter reference tone or choose Custom to enter a frequency.",
        )
        self.waveform = self.choice(
            self.tone, "Waveform", ("Sine", "Square", "Sawtooth", "Triangle", "White noise", "Pink noise"),
            action=self._modulation_changed,
            help_text="Choose the waveform. A sine wave is the usual reference for both wow and flutter tests and distortion tests.",
        )
        levels = self.row(wrap=True)
        self.level = self.spin(
            levels, "Output volume (%)", 0, 100, 50, increment=1, action=self._update_generator,
            help_text="Set the generated tone level as a percentage of full-scale output.",
        )
        self.custom_frequency_fields = wx.BoxSizer(wx.HORIZONTAL)
        self.frequency_row = levels
        self.frequency_row.Add(self.custom_frequency_fields, 0, wx.ALIGN_CENTER_VERTICAL)
        self.frequency = self.spin(
            self.custom_frequency_fields, "Custom frequency (Hz)", 1, 24000, target, digits=2,
            action=self._frequency_changed,
            help_text="Set the requested tone frequency in Hz. The selected audio sample rate can limit the output.",
        )
        route = self.row(wrap=True)
        self.loopback = self.checkbox(
            route, "Enable &Internal Loopback", self._toggle_loopback,
            help_text="Feed the generated tone directly into the analyzer so a test can run without external cables.",
        )
        self.modulation = self.checkbox(
            route, "Enable &Frequency Modulation", self._modulation_changed,
            help_text="Add a regular pitch variation to simulate wow or flutter.",
        )
        self.pan = self.spin(
            route, "Pan (%)", -100, 100, 0, increment=1, action=self._update_generator,
            help_text="−100 plays the left channel alone. 0 is centred. +100 plays the right channel alone.",
        )
        self.modulation_fields = self.row(wrap=True)
        modulation_rate = wx.BoxSizer(wx.HORIZONTAL)
        modulation_amount = wx.BoxSizer(wx.HORIZONTAL)
        self.modulation_fields.Add(modulation_rate, 0, wx.ALIGN_CENTER_VERTICAL)
        self.modulation_fields.Add(modulation_amount, 0, wx.ALIGN_CENTER_VERTICAL)
        self.modulation_frequency = self.spin(
            modulation_rate, "Modulation frequency (Hz)", 0.1, 100, 4, increment=0.1,
            digits=2, action=self._update_generator,
            help_text="Set how many times per second the test tone varies in pitch.",
        )
        self.modulation_depth = self.spin(
            modulation_amount, "Modulation depth (%)", 0, 5, 0, increment=0.01,
            digits=2, action=self._update_generator,
            help_text="Set the maximum pitch deviation as a percentage of the generated tone's frequency.",
        )
        self.advanced_row = route
        self.add_plot("Configured signal preview", "Time (ms)", "Output (FS)",
                      x_formatter=lambda value: f"{value:.4f}", y_formatter=lambda value: f"{value:+.4f}")
        self._show_advanced_fields(False)
        self.refresh_settings()
        self._modulation_changed()

    def desktop_actions(self):
        label = "Stop Generator" if self.generating else "Start Generator"
        options = "Hide Options" if self.show_advanced else "Options"
        return [
            ("generate", label, self._toggle_generator),
            ("sweep", "Frequency Sweep…", self.controller.show_frequency_sweep),
            ("generator_options", options, self._toggle_advanced),
            *super().desktop_actions(),
        ]

    def desktop_options(self):
        return [("frequency_response", "Measure Frequency Response…", self.controller.show_response),
                ("generator_preview", "Hide Signal Preview" if self.plot.IsShown() else "Show Signal Preview",
                 self._toggle_preview),
                *super().desktop_options()]

    def keyboard_actions(self):
        fields = (
            ("Select Frequency Standard", self.preset),
            ("Select Waveform", self.waveform),
            ("Edit Output Volume", self.level),
        )
        advanced_fields = (
            ("Focus Internal Loopback", self.loopback),
            ("Edit Stereo Pan", self.pan),
            ("Focus Frequency Modulation", self.modulation),
            ("Edit Modulation Frequency", self.modulation_frequency),
            ("Edit Modulation Depth", self.modulation_depth),
        )
        return (
            *self._focus_actions(fields),
            ("Edit Custom Frequency", self._focus_custom_frequency,
             "Focus the custom frequency field, or the standard selector if Custom has not been selected."),
            *((label, lambda field=field: self._focus_advanced_field(field), field._description)
              for label, field in advanced_fields),
        )

    def _focus_custom_frequency(self):
        control = self.frequency if self.frequency.IsShown() and self.frequency.IsEnabled() else self.preset
        (control if control.IsEnabled() else self.waveform).SetFocus()

    def _focus_advanced_field(self, field):
        if not self.show_advanced:
            self._show_advanced_fields(True)
        if not field.IsEnabled() or not field.IsShown():
            field = self.modulation if self.modulation.IsEnabled() else self.waveform
        field.SetFocus()

    def _toggle_advanced(self):
        self._show_advanced_fields(not self.show_advanced)
        (self.loopback if self.show_advanced else self.preset).SetFocus()

    def _toggle_preview(self):
        shown = not self.plot.IsShown()
        focus = wx.Window.FindFocus()
        self.content.Show(self.plot, shown)
        if shown:
            self._refresh_preview()
        elif focus is not None and self.plot.IsDescendant(focus):
            self.waveform.SetFocus()
        self.Layout()

    def _show_advanced_fields(self, shown):
        self.show_advanced = shown
        self.content.Show(self.advanced_row, shown, recursive=True)
        self._show_modulation_fields()

    def _show_modulation_fields(self):
        shown = self.show_advanced and self.modulation.GetValue() and self.modulation.IsEnabled()
        self.content.Show(self.modulation_fields, shown, recursive=True)
        self.Layout()

    def refresh_settings(self):
        super().refresh_settings()
        target = float(self.controller.settings.get("targetFreq", 3000))
        self._target = target
        if target not in (3000, 3150):
            self.custom_frequency = target
        choice = 0 if target == 3000 else 1 if target == 3150 else 2
        self.preset.SetSelection(choice)
        self._show_custom_frequency(choice == 2)
        self.frequency.SetValue(self.custom_frequency)
        self._update_generator()

    def _preset_changed(self):
        selected = self.preset.GetSelection()
        target = (3000.0, 3150.0, self.custom_frequency)[selected]
        self._show_custom_frequency(selected == 2)
        self._set_target(target)

    def _show_custom_frequency(self, shown):
        if self.frequency.IsShown() != shown:
            self.frequency_row.Show(self.custom_frequency_fields, shown, recursive=True)
            self.Layout()

    def _frequency_changed(self):
        if self.preset.GetSelection() == 2:
            self.custom_frequency = self.frequency.GetValue()
            self._set_target(self.custom_frequency)

    def _set_target(self, target):
        if self._target == target:
            return
        if not self.controller.apply_settings(targetFreq=target):
            self.refresh_settings()

    def _modulation_changed(self):
        noise = self.waveform.GetSelection() >= 4
        self.preset.Enable(not noise)
        self.frequency.Enable(not noise)
        self.modulation.Enable(not noise)
        enabled = self.modulation.GetValue() and not noise
        self.modulation_frequency.Enable(enabled)
        self.modulation_depth.Enable(enabled)
        self._show_modulation_fields()
        self._update_generator()

    def _toggle_loopback(self):
        self.controller.audio.set_loopback(self.loopback.GetValue())
        self._update_summary()
        self.controller.announce("Internal loopback enabled." if self.loopback.GetValue() else "Internal loopback disabled.")

    def _toggle_generator(self):
        if self.controller.studio_playback_busy:
            self.controller.announce("Finish the audio test or interface calibration before using the generator.")
            return
        if self.generating:
            self.generating = False
            self._update_generator()
            self.controller.release_output()
            self.controller.play_sound("stop")
            self.controller.announce("Generator stopped.")
            return
        self.generating = True
        self._update_generator()
        if self.controller.ensure_output():
            self.refresh_measurement(self.controller.audio.snapshot())
            self.controller.play_sound("start")
            self.controller.announce("Generator started.")
        else:
            self.generating = False
            self._update_generator()

    def _update_generator(self):
        if self.controller.studio_playback_busy:
            return
        values = (
            self.generating,
            self._target,
            self.waveforms[max(0, self.waveform.GetSelection())],
            self.level.GetValue() / 100,
            self.modulation_frequency.GetValue(),
            self.modulation_depth.GetValue() if self.modulation.GetValue() else 0.0,
            self.pan.GetValue() / 100,
        )
        if values != getattr(self, "_applied_generator_values", None):
            self._applied_generator_values = values
            self.controller.audio.set_generator(
                enabled=values[0], frequency=values[1], waveform=values[2],
                level=values[3], modulation_frequency=values[4], modulation_depth=values[5],
                pan=values[6],
            )
        self._update_summary()

    def _update_summary(self):
        health = self.snapshot.get("health", {})
        sweep = self.snapshot.get("sweep", {})
        self._refresh_preview()
        if sweep.get("active"):
            current = sweep.get("frequency")
            frequency = f"{current:.2f} Hz" if current else "Sweep in progress"
            state = "Stopping sweep" if sweep.get("status") == "stopping" else "Playing sweep"
            if not sweep.get("output") and sweep.get("loopback"):
                state = "Sweep in loopback"
            level = sweep.get("level")
            self.set_metrics(self.generator_cards, (
                ("Output frequency", frequency, False),
                ("Output level", f"{level * 100:g}%" if level is not None else "Unavailable", False),
                ("Generator", state, False),
            ))
            self.set_summary(f"{state}. {frequency}. {sweep['elapsed']:.1f} of {sweep['duration']:.1f} seconds.")
            return
        generator = self.snapshot.get("generator", {})
        current_settings = generator.get("frequency") == self._target
        loopback_active = self.loopback.GetValue() and health.get("running")
        frequency = self._target
        if self.controller.studio_playback_busy:
            state = "Instrument test signal"
        elif self.generating:
            if health.get("outputActive"):
                state = "Playing tone"
                if current_settings:
                    frequency = generator.get("effective_frequency", frequency)
            elif loopback_active:
                state = "Internal loopback"
                if current_settings:
                    frequency = generator.get("loopback_frequency", frequency)
            else:
                state = "Output unavailable"
        else:
            state = "Stopped"
        waveform = self.waveform.GetStringSelection()
        noise = self.waveform.GetSelection() >= 4
        volume = self.level.GetValue()
        self.set_metrics(self.generator_cards, (
            ("Output frequency", "Broadband" if noise else f"{frequency:g} Hz", False),
            ("Output level", f"{volume:g}%", False),
            ("Generator", state, state == "Output unavailable"),
        ))
        signal_label = waveform if noise else f"{waveform} at {frequency:g} Hz"
        lines = [f"{state}. {signal_label}. Output volume: {volume:g}%."]
        if not noise and abs(frequency - self._target) > 0.005:
            lines.append(f"The requested frequency is {self._target:g} Hz. The sample rate limits the output frequency.")
        modulation = "Frequency modulation off."
        if self.modulation.GetValue() and not noise:
            modulation = (
                f"Frequency modulation: {self.modulation_frequency.GetValue():g} Hz. "
                f"Depth: {self.modulation_depth.GetValue():g}%."
            )
        loopback = "Internal loopback on." if self.loopback.GetValue() else "Internal loopback off."
        pan = self.pan.GetValue()
        balance = "Stereo pan: centred." if pan == 0 else f"Stereo pan: {abs(pan):g}% {'left' if pan < 0 else 'right'}."
        lines.extend((modulation, f"{loopback} {balance}"))
        self.set_summary("\n".join(lines))

    def _refresh_preview(self):
        if not self.plot.IsShown():
            return
        health = self.snapshot.get("health", {})
        rate = int(health.get("outputSampleRate") or self.controller.settings.get("sampleRate")
                   or self.snapshot.get("sample_rate") or 48_000)
        settings = GeneratorSettings(
            enabled=True, frequency=self._target, waveform=self.waveforms[max(0, self.waveform.GetSelection())],
            level=self.level.GetValue() / 100, pan=self.pan.GetValue() / 100,
            modulation_frequency=self.modulation_frequency.GetValue(),
            modulation_depth=self.modulation_depth.GetValue() if self.modulation.GetValue() else 0,
        )
        accent, secondary = self.theme_accents()
        key = (settings, rate, accent, secondary)
        if key == self._preview_key:
            return
        self._preview_key = key
        times, samples = _generator_preview(settings, rate)
        stereo = settings.pan != 0
        series = [{"label": "Left" if stereo else "Output", "x": times, "y": samples[:, 0], "color": accent}]
        if stereo:
            series.append({"label": "Right", "x": times, "y": samples[:, 1], "color": secondary, "style": "dashed"})
        xlim = (0, times[-1])
        if self._preview_xlim != xlim:
            self._preview_xlim = xlim
            self.plot.set_default_view(xlim=xlim, ylim=(-1, 1))
            self.plot.fit()
        self.plot.set_series(series)

    def refresh_measurement(self, snapshot):
        super().refresh_measurement(snapshot)
        if self.IsShownOnScreen():
            self._update_summary()


def create_measurement_pages(notebook, controller):
    """Build the five live tools in notebook order."""
    return [
        ("Measure", MeasurePanel(notebook, controller)),
        ("Scope", ScopePanel(notebook, controller)),
        ("Turntable", TurntablePanel(notebook, controller)),
        ("THD", THDPanel(notebook, controller)),
        ("Generator", GeneratorPanel(notebook, controller)),
    ]
