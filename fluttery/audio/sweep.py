"""Phase-continuous frequency sweeps for live playback and streaming WAV export."""

from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import asdict, dataclass
from functools import lru_cache
from itertools import pairwise
from numbers import Real
from pathlib import Path
from struct import pack

import numpy as np

from ..staging import atomic_output
from .generator import SHORT_HARMONIC_SERIES, polyblamp, polyblep

MIN_FREQUENCY = 1
MAX_FREQUENCY = 80_000
MIN_DURATION = 0.1
MAX_DURATION = 600
MAX_RULES = 128
SWEEP_CURVES = ('linear', 'log')
LEVEL_CURVES = ('step', *SWEEP_CURVES)
WAVEFORMS = ('sine', 'square', 'triangle', 'sawtooth')
WAV_BIT_DEPTHS = (16, 24, 32)
WAV_SAMPLE_RATES = (8_000, 16_000, 22_050, 32_000, 44_100, 48_000, 88_200, 96_000, 176_400, 192_000)
MAX_PLAYBACK_RATE = 384_000
_PLAYBACK_MARGIN = 0.49
_EDGE_FADE_SECONDS = 0.002


def _bounded(value, minimum, maximum):
    return isinstance(value, Real) and not isinstance(value, bool) and minimum <= value <= maximum


@dataclass(frozen=True, slots=True)
class SweepRule:
    frequency: float
    waveform: str | None = None
    level: float | None = None
    level_curve: str = 'step'
    frequency_curve: str | None = None


@dataclass(frozen=True, slots=True)
class SweepSettings:
    start_frequency: float = 20.0
    end_frequency: float = 20_000.0
    duration: float = 10.0
    curve: str = 'log'
    waveform: str = 'sine'
    level: float = 0.25
    rules: tuple[SweepRule, ...] = ()

    def validate(self, sample_rate=None):
        if not all(_bounded(f, MIN_FREQUENCY, MAX_FREQUENCY) for f in (self.start_frequency, self.end_frequency)):
            raise ValueError(f'Start and end frequencies must be between {MIN_FREQUENCY:,} and {MAX_FREQUENCY:,} Hz.')
        if self.start_frequency == self.end_frequency:
            raise ValueError('Choose different start and end frequencies.')
        if not _bounded(self.duration, MIN_DURATION, MAX_DURATION):
            raise ValueError(f'Duration must be between {MIN_DURATION:g} and {MAX_DURATION:g} seconds.')
        if self.curve not in SWEEP_CURVES or self.waveform not in WAVEFORMS:
            raise ValueError('Choose a supported sweep curve and waveform.')
        if not _bounded(self.level, 0, 1):
            raise ValueError('Amplitude must be between 0 and 100 percent.')

        low, high = sorted((self.start_frequency, self.end_frequency))
        if sample_rate is not None:
            if not _bounded(sample_rate, WAV_SAMPLE_RATES[0], MAX_PLAYBACK_RATE):
                raise ValueError(f'The sample rate must be between {WAV_SAMPLE_RATES[0]:,} and {MAX_PLAYBACK_RATE:,} Hz.')
            limit = _PLAYBACK_MARGIN * sample_rate
            if high > limit:
                raise ValueError(f'The highest frequency for {sample_rate:,.0f} Hz audio is {limit:,.1f} Hz. '
                                 'Lower the sweep frequency or select a higher sample rate.')

        if not isinstance(self.rules, (tuple, list)) or len(self.rules) > MAX_RULES:
            raise ValueError(f'Use at most {MAX_RULES} sweep rules.')
        times = set()
        for rule in self.rules:
            if (not isinstance(rule, SweepRule) or not _bounded(rule.frequency, low, high)
                    or rule.frequency == self.start_frequency):
                raise ValueError('Each rule frequency must lie after the start and within the sweep range.')
            if rule.frequency == self.end_frequency and (
                rule.level is None or rule.level_curve == 'step' or rule.waveform is not None or rule.frequency_curve is not None
            ):
                raise ValueError('Use an end-frequency rule only as the destination of an amplitude ramp.')
            time = self._crossing_time(rule.frequency)
            if time in times or time <= 0 or (time >= self.duration and rule.frequency != self.end_frequency):
                raise ValueError('Give each rule a distinct frequency.')
            times.add(time)
            if rule.waveform is not None and rule.waveform not in WAVEFORMS:
                raise ValueError('Choose a supported waveform for every waveform rule.')
            if rule.frequency_curve is not None and rule.frequency_curve not in SWEEP_CURVES:
                raise ValueError('Choose linear or logarithmic frequency interpolation for each sweep section.')
            if rule.level is not None and not _bounded(rule.level, 0, 1):
                raise ValueError('Rule amplitudes must be between 0 and 100 percent.')
            if rule.level_curve not in LEVEL_CURVES:
                raise ValueError('Choose a step, linear ramp, or logarithmic ramp for every amplitude rule.')
            if rule.waveform is None and rule.level is None and rule.frequency_curve is None:
                raise ValueError('Each rule must change the waveform, amplitude, or frequency curve.')
        return self

    def _crossing_time(self, frequency):
        if self.curve == 'linear':
            part = (frequency - self.start_frequency) / (self.end_frequency - self.start_frequency)
        else:
            part = _log_ratio(self.start_frequency, frequency) / _log_ratio(self.start_frequency, self.end_frequency)
        return self.duration * part

    def to_dict(self):
        settings = asdict(self)
        settings['rules'] = list(settings['rules'])
        return settings

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise ValueError('A sweep preset must contain settings.')  # noqa: TRY004 - Malformed saved data.
        rules = data.get('rules', ())
        if not isinstance(rules, (tuple, list)) or len(rules) > MAX_RULES:
            raise ValueError(f'Use at most {MAX_RULES} sweep rules.')
        try:
            return cls(**{key: value for key, value in data.items() if key in cls.__dataclass_fields__ and key != 'rules'},
                       rules=tuple(SweepRule(**rule) for rule in rules)).validate()
        except TypeError as error:
            raise ValueError('A sweep preset contains an invalid rule.') from error

    def trajectory(self, samples=256):
        """Return times, Hz and programmed amplitudes for a plot or readout."""
        timeline = _Timeline(self.validate())
        knots = np.array(sorted(set(timeline.frequency_starts + timeline.level_starts)))
        times = np.unique(np.concatenate((np.linspace(0, self.duration, max(2, samples)), knots,
                                         np.nextafter(knots[knots > 0], -np.inf))))
        frequencies = np.empty_like(times)
        for section, indices in _available_sections(timeline.frequency_sections, timeline.frequency_starts, times):
            frequencies[indices] = section.frequency(times[indices] - section.time)
        return times, frequencies, timeline.levels_at(times)


def _log_ratio(start, end):
    return math.log1p((end - start) / start)


@dataclass(frozen=True, slots=True)
class _FrequencySection:
    time: float
    start_frequency: float
    slope: float
    curve: str
    waveform: str
    cycles_at_start: float

    def frequency(self, elapsed):
        if self.curve == 'linear':
            return self.start_frequency + self.slope * elapsed
        return self.start_frequency * np.exp(self.slope * elapsed)

    def cycles(self, elapsed):
        # Analytically integrate instantaneous frequency. Starting each block
        # from absolute sample time keeps phase independent of block size.
        if self.curve == 'linear':
            distance = elapsed * (self.start_frequency + 0.5 * self.slope * elapsed)
        else:
            distance = self.start_frequency * np.expm1(self.slope * elapsed) / self.slope
        return self.cycles_at_start + distance


@dataclass(frozen=True, slots=True)
class _LevelSection:
    time: float
    duration: float
    start_level: float
    end_level: float
    curve: str

    def levels(self, elapsed):
        if self.curve == 'step' or self.start_level == self.end_level:
            return self.start_level
        fraction = elapsed / self.duration
        if self.curve == 'linear':
            return self.start_level + fraction * (self.end_level - self.start_level)
        # Zero has no finite dB value. Use a 120 dB relative range for the
        # ramp and retain exact zero at the boundary where it was requested.
        left = self.start_level or max(self.end_level * 1e-6, math.nextafter(0.0, 1.0))
        right = self.end_level or max(self.start_level * 1e-6, math.nextafter(0.0, 1.0))
        level = np.exp((1 - fraction) * math.log(left) + fraction * math.log(right))
        return np.where(elapsed == 0, self.start_level, level)


def _available_sections(sections, starts, times):
    first = bisect_right(starts, times[0]) - 1
    last = bisect_right(starts, times[-1]) - 1
    boundaries = times.searchsorted(starts[first + 1:last + 1])
    for section, start, end in zip(sections[first:last + 1], [0, *boundaries], [*boundaries, len(times)], strict=True):
        if end > start:
            yield section, slice(start, end)


class _Timeline:
    def __init__(self, settings):
        descending = settings.start_frequency > settings.end_frequency
        rules = sorted(settings.rules, key=lambda rule: rule.frequency, reverse=descending)
        landmarks = [(settings._crossing_time(rule.frequency), rule) for rule in rules]
        self.frequency_sections = self._frequency_sections(settings, landmarks)
        self.level_sections = self._level_sections(settings, landmarks)
        self.frequency_starts = tuple(section.time for section in self.frequency_sections)
        self.level_starts = tuple(section.time for section in self.level_sections)

    @staticmethod
    def _frequency_sections(settings, landmarks):
        start = SweepRule(settings.start_frequency, settings.waveform)
        # The endpoint may have an amplitude rule, which has no further
        # frequency section. Keep a single endpoint for phase integration.
        knots = [(0.0, start), *((time, rule) for time, rule in landmarks if rule.frequency != settings.end_frequency),
                 (settings.duration, SweepRule(settings.end_frequency))]
        sections, cycles = [], 0.0
        waveform = settings.waveform
        for (time, rule), (end, next_rule) in pairwise(knots):
            frequency, target = rule.frequency, next_rule.frequency
            duration = end - time
            waveform = rule.waveform or waveform
            curve = rule.frequency_curve or settings.curve
            slope = ((target - frequency) if curve == 'linear' else _log_ratio(frequency, target)) / duration
            section = _FrequencySection(time, frequency, slope, curve, waveform, cycles)
            cycles = section.cycles(duration) % 1.0
            sections.append(section)
        return tuple(sections)

    @staticmethod
    def _level_sections(settings, landmarks):
        sections, time, level = [], 0.0, settings.level
        for end, rule in landmarks:
            if rule.level is not None:
                sections.append(_LevelSection(time, end - time, level, rule.level, rule.level_curve))
                time, level = end, rule.level
        sections.append(_LevelSection(time, settings.duration - time, level, level, 'step'))
        return tuple(sections)

    def levels_at(self, times):
        values = np.empty_like(times)
        for section, indices in _available_sections(self.level_sections, self.level_starts, times):
            values[indices] = section.levels(times[indices] - section.time)
        return values


@lru_cache(maxsize=3)
def _harmonic_peaks(waveform):
    step = 1 if waveform == 'sawtooth' else 2
    exponent = 2 if waveform == 'triangle' else 1
    peaks = []
    for count, order in enumerate(range(1, SHORT_HARMONIC_SERIES + 1, step), start=1):
        angle = math.pi / (count + 1) if waveform == 'sawtooth' else math.pi / (2 * count)
        peaks.append(sum((1 if waveform == 'triangle' else math.sin(i * angle)) / i**exponent
                         for i in range(1, order + 1, step)))
    return tuple(peaks)


def _high_waveform(waveform, phase, frequencies, limit):
    # Fade harmonics out before Nyquist so they don't disappear abruptly
    # mid-chirp. Each faded series is a weighted combination of finite
    # partial sums, whose peaks give a conservative amplitude envelope.
    angle = math.tau * phase
    triangle = waveform == 'triangle'
    trig = np.cos if triangle else np.sin
    power = 2 if triangle else 1
    highest = min(SHORT_HARMONIC_SERIES, int(limit / frequencies.min()))
    orders = range(1, highest + 1, 1 if waveform == 'sawtooth' else 2)
    values = trig(angle)
    peak = np.ones_like(phase)
    peaks = _harmonic_peaks(waveform)[:len(orders)]
    for order, (before, current) in zip(orders[1:], pairwise(peaks), strict=True):
        weight = np.clip((limit - order * frequencies) / (0.07 * limit), 0, 1)
        weight = weight * weight * (3 - 2 * weight)
        values += weight * trig(order * angle) / order**power
        peak += weight * (current - before)
    return values / (peak if waveform == 'square' else -peak)


def _waveform(waveform, phase, frequencies, sample_rate):
    if waveform == 'sine':
        return np.sin(math.tau * phase)
    # A constant-time edge correction works well at low pitches where there
    # may be thousands of harmonics. At higher pitches, use a short additive
    # series and crossfade between methods by frequency, not by block size.
    limit = sample_rate * _PLAYBACK_MARGIN
    low = limit / SHORT_HARMONIC_SERIES
    high = low * 1.3
    output = np.empty_like(phase)
    only_high = frequencies >= high
    lower = ~only_high
    if lower.any():
        p, step = phase[lower], frequencies[lower] / sample_rate
        if waveform == 'sawtooth':
            output[lower] = 2 * p - 1 - polyblep(p, step)
        elif waveform == 'square':
            output[lower] = np.where(p < 0.5, 1.0, -1.0) + polyblep(p, step) - polyblep((p + 0.5) % 1, step)
        else:
            output[lower] = 1 - 4 * np.abs(p - 0.5) + 4 * (polyblamp(p, step) - polyblamp((p + 0.5) % 1, step))
    if only_high.any():
        output[only_high] = _high_waveform(waveform, phase[only_high], frequencies[only_high], limit)
    transition = (frequencies > low) & lower
    if transition.any():
        f = frequencies[transition]
        blend = (f - low) / (high - low)
        blend = blend * blend * (3 - 2 * blend)
        output[transition] = output[transition] * (1 - blend) + _high_waveform(waveform, phase[transition], f, limit) * blend
    # Independently protect the requested peak amplitude from floating-point
    # round-off in a normalized series.
    return np.clip(output, -1.0, 1.0)


class SweepRenderer:
    """Render a one-shot sweep without materializing the full sample array."""

    def __init__(self, settings, sample_rate):
        settings.validate(sample_rate)
        self._timeline = _Timeline(settings)
        self._duration = settings.duration
        self.sample_rate = sample_rate
        self.total_frames = round(settings.duration * sample_rate)
        self.position = 0
        self._fade_frames = min(round(_EDGE_FADE_SECONDS * sample_rate), (self.total_frames - 1) // 2)

    @property
    def finished(self):
        return self.position >= self.total_frames

    def values_at(self, seconds):
        """Read the programmed frequency and level, before the edge taper."""
        time = min(self._duration, max(0.0, seconds))
        frequency_section = self._timeline.frequency_sections[bisect_right(self._timeline.frequency_starts, time) - 1]
        level_section = self._timeline.level_sections[bisect_right(self._timeline.level_starts, time) - 1]
        return float(frequency_section.frequency(time - frequency_section.time)), float(level_section.levels(time - level_section.time))

    def render(self, frames):
        output = np.zeros(frames, dtype=np.float32)
        count = min(frames, self.total_frames - self.position)
        if not count:
            return output

        indices = np.arange(self.position, self.position + count)
        times = indices / self.sample_rate
        level = self._timeline.levels_at(times)
        for section, selection in _available_sections(self._timeline.frequency_sections, self._timeline.frequency_starts, times):
            elapsed = times[selection] - section.time
            phase = section.cycles(elapsed) % 1.0
            frequency = section.frequency(elapsed)
            output[selection] = _waveform(section.waveform, phase, frequency, self.sample_rate) * level[selection]

        # A short raised-cosine taper prevents transport-edge clicks. In-sweep
        # waveform switches and explicit amplitude steps still happen exactly
        # at the configured frequency, within one output sample.
        distance = np.minimum(indices, self.total_frames - indices - 1)
        fade = distance < self._fade_frames
        rendered = output[:count]
        rendered[fade] *= 0.5 * (1 - np.cos(math.pi * distance[fade] / self._fade_frames))
        self.position += count
        return output


def _pcm_bytes(samples, channels, bit_depth):
    if channels == 2:
        samples = np.repeat(samples, 2)
    maximum = 1 << (bit_depth - 1)
    pcm = np.clip(np.rint(samples.astype(np.float64) * maximum), -maximum, maximum - 1)
    if bit_depth == 16:
        return pcm.astype('<i2').tobytes()
    pcm = pcm.astype('<i4')
    if bit_depth == 24:
        return pcm.view(np.uint8).reshape(-1, 4)[:, :3].tobytes()
    return pcm.tobytes()


def export_sweep(path, settings, *, sample_rate, channels, bit_depth, cancelled=None, progress=None):
    """Write a PCM WAV incrementally and replace the destination on success."""
    if (not _bounded(sample_rate, WAV_SAMPLE_RATES[0], WAV_SAMPLE_RATES[-1])
            or int(sample_rate) != sample_rate or channels not in (1, 2) or bit_depth not in WAV_BIT_DEPTHS):
        raise ValueError('Choose a whole-number WAV sample rate, mono or stereo, and 16-, 24-, or 32-bit PCM.')
    sample_rate, channels, bit_depth = int(sample_rate), int(channels), int(bit_depth)
    renderer = SweepRenderer(settings, sample_rate)
    destination = Path(path)
    frame_size = channels * (bit_depth // 8)
    data_size = frame_size * renderer.total_frames
    padding = data_size % 2
    with atomic_output(destination) as stream:
        # The frame count is known. The RIFF length includes the word-alignment
        # byte for an odd 24-bit mono export, while the data chunk length does not.
        stream.write(pack('<4sI4s4sIHHIIHH4sI', b'RIFF', data_size + padding + 36, b'WAVE', b'fmt ', 16,
                          1, channels, sample_rate, sample_rate * frame_size, frame_size, bit_depth, b'data', data_size))
        while not renderer.finished:
            if cancelled is not None and cancelled():
                raise InterruptedError('Sweep export cancelled.')
            block = renderer.render(min(16_384, renderer.total_frames - renderer.position))
            stream.write(_pcm_bytes(block, channels, bit_depth))
            if progress is not None:
                progress(renderer.position, renderer.total_frames)
        if cancelled is not None and cancelled():
            raise InterruptedError('Sweep export cancelled.')
        if padding:
            stream.write(b'\x00')
    return destination
