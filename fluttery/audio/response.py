"""Acquisition and DSP for frequency response and interface compensation.

The analysis has two paths. A known digital excitation measures transfer gain
and phase, using two short markers to remove transport latency and sample-clock
mismatch. An external alignment tape can be traced from its stable tones; the
result is relative level and has no phase reference.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise
from math import ceil, floor, isfinite
from time import monotonic

import numpy as np
from scipy import fft, signal
from scipy.io import wavfile

from .recordings import TapeEndDetector, inspect_wav

LEAD_SECONDS = 0.35
MARKER_SECONDS = 0.16
GUARD_SECONDS = 0.12
MAX_CAPTURE_SECONDS = 180
MAX_RESPONSE_SECONDS = 120
KINDS = ('sweep', 'white', 'pink')
OCTAVE_RESOLUTIONS = (3, 6, 12, 24, 48)
RESPONSE_MODES = ('device', 'interface', 'tape_playback', 'tape_recording')
FLOOR_DB = -180.0
LOOPBACK_TONE_HZ = 997.0
LOOPBACK_SETTLE_SECONDS = 0.20
LOOPBACK_NOISE_SECONDS = 1.0
LOOPBACK_TONE_SECONDS = 1.0
CAPTURE_STALL_SECONDS = 3.0


class BenchCapture:
    """Bounded worker-owned buffers; published indices follow each completed write."""

    def __init__(self, input_rate, output_rate, duration, reference=None, *, channels=2):
        self.input_rate = float(input_rate)
        self.output_rate = float(output_rate) if output_rate else None
        self.duration = float(duration)
        if (not isfinite(self.input_rate) or not 1000 <= self.input_rate <= 384_000
                or not isfinite(self.duration) or not 0 < self.duration <= MAX_CAPTURE_SECONDS
                or channels not in (1, 2)):
            raise ValueError('Choose a supported capture rate, duration and channel count.')
        self.reference = None if reference is None else np.asarray(reference, dtype=np.float32).reshape(-1)
        if self.reference is not None and (self.output_rate is None or not 1000 <= self.output_rate <= 384_000):
            raise ValueError('This test requires a supported playback sample rate.')
        self._pcm = np.empty((ceil(self.input_rate * self.duration), channels), dtype=np.float32)
        self._count = 0
        self._output_position = 0
        self._lead = ceil(self.output_rate * LEAD_SECONDS) if self.reference is not None else 0
        self._closed = False
        self._last_input_at = monotonic()
        self.error = ''

    @property
    def seconds(self):
        return self._count / self.input_rate

    @property
    def active(self):
        return not self._closed and self._count < len(self._pcm)

    @property
    def output_pending(self):
        return self.reference is not None and self._output_position < self._lead + len(self.reference)

    def append(self, samples):
        if not self.active:
            return
        frames = np.asarray(samples, dtype=np.float32)
        if frames.ndim == 1:
            frames = frames[:, None]
        if frames.ndim != 2 or frames.shape[1] != self._pcm.shape[1]:
            self.cancel('The number of audio input channels changed during the test.')
            return
        start = self._count
        size = min(len(frames), len(self._pcm) - start)
        self._pcm[start:start + size] = frames[:size]
        self._count += size
        if size:
            self._last_input_at = monotonic()

    def abort_stalled_input(self):
        """Stop a bench test if its physical stream stops delivering frames."""
        if self.active and monotonic() - self._last_input_at > CAPTURE_STALL_SECONDS:
            self.cancel('The audio input stopped delivering samples. Check the input device and try again.')

    def output(self, count):
        """Return mono playback, including the lead and harmless trailing zeros."""
        samples = np.zeros(count, dtype=np.float32)
        if self.reference is not None and not self._closed:
            start = self._output_position - self._lead
            end = start + count
            begin = max(start, 0)
            stop = min(end, len(self.reference))
            if stop > begin:
                samples[begin - start:stop - start] = self.reference[begin:stop]
            self._output_position += count
        return samples

    def finish(self):
        self._closed = True

    def cancel(self, error=None):
        self.error = str(error or 'Capture cancelled.')
        self.finish()

    def preview(self, seconds=0.35):
        end = self._count
        return self._pcm[max(0, end - ceil(self.input_rate * seconds)):end].copy()

    def section(self, start, end):
        """Copy only the requested, already-published capture frames."""
        return self._pcm[max(0, start):min(end, self._count)].copy()

    def read(self):
        return self._pcm[:self._count].copy()


class PlaybackTailDetector:
    """Recognize the quiet tail after an external stepped or swept tape tone."""

    def __init__(self, rate, low_hz, high_hz, seconds, level_db, *, stereo=True):
        self.rate = rate
        self.stereo = stereo
        # Enough cycles at the low end to distinguish a tone from tape hiss,
        # with short windows for audio-band sweeps and silence boundaries.
        self._size = ceil(rate * min(.4, max(.125, 3 / low_hz)))
        self._window = signal.windows.hann(self._size, sym=False)
        self._frequency = fft.rfftfreq(self._size, 1 / rate)
        resolution = self._frequency[1]
        self._range = (self._frequency >= max(resolution, low_hz - resolution)) & (
            self._frequency <= min(high_hz + resolution, .49 * rate))
        self._detector = TapeEndDetector(seconds, level_db, armed_seconds=.5)
        self._frames = 0

    def observe(self, capture):
        """Return the first quiet frame once new PCM confirms the tape has ended."""
        available = round(capture.seconds * self.rate)
        while self._frames + self._size <= available:
            start = self._frames
            frames = capture.section(start, start + self._size)
            self._frames += len(frames)
            if not self.stereo:
                frames = frames[:, :1]
            centered = frames - np.mean(frames, axis=0, dtype=np.float64)
            power = np.mean(centered ** 2, axis=0)
            levels = 10 * np.log10(np.maximum(power, 1e-18))
            # A noisier channel must neither hide a test tone on the other
            # channel nor count as a quiet tape tail while its noise persists.
            locked = any(self._tone_present(centered[:, channel]) for channel in
                         np.flatnonzero(levels > self._detector.threshold_db))
            if self._detector.observe(start / self.rate, locked, float(np.max(levels))):
                return round(self._detector.silent_since * self.rate)
        return None

    def _tone_present(self, samples):
        if not self._range.any():
            return False
        power = np.abs(fft.rfft(samples * self._window)) ** 2
        peak = self._frequency[np.argmax(np.where(self._range, power, -1))]
        # A normal tape sweep spreads into nearby FFT bins. Integrate the whole
        # peak neighbourhood before deciding whether the energy is tonal.
        half_width = max(2.5 * self._frequency[1], .10 * peak)
        near_peak = np.abs(self._frequency - peak) <= half_width
        return power[near_peak].sum() >= .6 * power.sum()


def _fade(samples, frames):
    count = min(frames, len(samples) // 2)
    if count:
        ramp = np.sin(np.linspace(0, np.pi / 2, count)) ** 2
        samples[:count] *= ramp
        samples[-count:] *= ramp[::-1]
    return samples


def _marker(rate, ending=False, source_rate=None):
    size = round(MARKER_SECONDS * rate)
    time = np.arange(size, dtype=float) / rate
    low, high = 440, min(3900, 0.38 * (source_rate or rate))
    lower, upper = (high, low) if ending else (low, high)
    tone = signal.chirp(time, lower, MARKER_SECONDS, upper, method='logarithmic')
    return _fade(tone, round(rate * 0.016))


@dataclass(frozen=True, slots=True)
class ResponseStimulus:
    kind: str = 'sweep'
    low_hz: float = 20
    high_hz: float = 20_000
    duration: float = 10
    level_db: float = -20
    seed: int = 0

    def band(self, rate):
        if self.kind not in KINDS:
            raise ValueError('Choose a sweep, white noise or pink noise.')
        if not all(isfinite(value) for value in (self.low_hz, self.high_hz, self.duration, self.level_db)):
            raise ValueError('Enter finite values for the frequency, level and duration.')
        if not 2 <= self.duration <= MAX_RESPONSE_SECONDS:
            raise ValueError(f'Enter a stimulus duration of 2 to {MAX_RESPONSE_SECONDS} seconds.')
        if not -80 <= self.level_db <= -1:
            raise ValueError('Enter a peak output level of −80 to −1 dBFS.')
        if not 5 <= self.low_hz < self.high_hz or self.high_hz / self.low_hz < 1.10:
            raise ValueError('Enter frequencies at least 5 Hz and at least 10 percent apart.')
        if self.high_hz > 0.47 * rate:
            raise ValueError(f'The high frequency must be at most {0.47 * rate:g} Hz at this sample rate.')
        return self.low_hz, self.high_hz

    @property
    def total_seconds(self):
        return self.duration + 2 * (MARKER_SECONDS + GUARD_SECONDS)

    def segments(self, rate):
        marker = round(MARKER_SECONDS * rate)
        guard = round(GUARD_SECONDS * rate)
        duration = round(self.duration * rate)
        start = marker + guard
        return start, start + duration, start + duration + guard

    def render(self, rate):
        """Finite deterministic stimulus. Levels are peak dBFS for every source."""
        low, high = self.band(rate)
        start, end, ending = self.segments(rate)
        duration = end - start
        if self.kind == 'sweep':
            time = np.arange(duration, dtype=float) / rate
            stimulus = signal.chirp(time, low, self.duration, high, method='logarithmic')
        else:
            random = np.random.default_rng(self.seed)
            # Shape the finite noise in frequency space. The reference used
            # in analysis is the actual shaped signal, with its crest factor
            # and fade intact. Independent draws never have to match by chance.
            size = fft.next_fast_len(duration, real=True)
            white = random.standard_normal(size).astype(np.float32)
            spectrum = fft.rfft(white)
            frequencies = fft.rfftfreq(size, 1 / rate)
            # Gentle edges leave well-excited bins to each user-selected limit.
            taper = (np.clip((frequencies - 0.6 * low) / (0.4 * low), 0, 1)
                     * np.clip((min(0.5 * rate, 1.12 * high) - frequencies) / (0.12 * high), 0, 1))
            if self.kind == 'pink':
                taper /= np.sqrt(np.maximum(frequencies, low) / low)
            spectrum *= taper
            stimulus = fft.irfft(spectrum, size)[:duration]
        peak = 10 ** (self.level_db / 20)
        scale = peak / max(float(np.max(np.abs(stimulus))), np.finfo(np.float32).eps)
        channel = np.zeros(ending + round(MARKER_SECONDS * rate), dtype=np.float32)
        channel[:round(MARKER_SECONDS * rate)] = _marker(rate) * peak
        channel[start:end] = _fade(stimulus, round(rate * 0.022)) * scale
        channel[ending:] = _marker(rate, True) * peak
        return channel


@dataclass(frozen=True, slots=True)
class InterfaceStimulus(ResponseStimulus):
    """A response test followed by a quiet observation and a known clean tone."""

    @property
    def total_seconds(self):
        return (self.duration + 2 * (MARKER_SECONDS + GUARD_SECONDS)
                + LOOPBACK_SETTLE_SECONDS + LOOPBACK_NOISE_SECONDS + LOOPBACK_TONE_SECONDS)

    def baseline_segments(self, rate):
        _, _, ending = self.segments(rate)
        marker_end = ending + round(MARKER_SECONDS * rate)
        start = marker_end + round(LOOPBACK_SETTLE_SECONDS * rate)
        quiet_end = start + round(LOOPBACK_NOISE_SECONDS * rate)
        tone_end = quiet_end + round(LOOPBACK_TONE_SECONDS * rate)
        return start, quiet_end, tone_end

    def render(self, rate):
        excitation = ResponseStimulus.render(self, rate)
        _, tone_start, end = self.baseline_segments(rate)
        if 3 * LOOPBACK_TONE_HZ >= 0.47 * rate:
            raise ValueError('Select a sample rate that can measure harmonics of the 997 Hz calibration tone.')
        tone = np.sin((2 * np.pi * LOOPBACK_TONE_HZ / rate) * np.arange(end - tone_start))
        _fade(tone, round(.035 * rate))
        return np.concatenate((excitation, np.zeros(tone_start - len(excitation), dtype=np.float32),
                               (tone * 10 ** (self.level_db / 20)).astype(np.float32)))


@dataclass(frozen=True, slots=True)
class InterfaceBaseline:
    noise_dbfs: tuple[float, ...]
    tone_dbfs: tuple[float, ...]
    thd_percent: tuple[float, ...]
    thdn_percent: tuple[float, ...]
    drive_dbfs_peak: float
    high_hz: float

    def as_record(self):
        """Use the same channel order and units in profiles and session reports."""
        return {'toneHz': LOOPBACK_TONE_HZ, 'highHz': self.high_hz, 'driveDbfsPeak': self.drive_dbfs_peak,
                'noiseDbfs': list(self.noise_dbfs), 'toneDbfs': list(self.tone_dbfs),
                'thdPercent': list(self.thd_percent), 'thdnPercent': list(self.thdn_percent)}


@dataclass(frozen=True, slots=True)
class ResponseCurve:
    frequencies: np.ndarray
    gain_db: np.ndarray
    phase_deg: np.ndarray | None = None
    coherence: np.ndarray | None = None
    delay_ms: float | None = None
    drift_ppm: float | None = None
    clipping: bool = False
    relative: bool = False
    anchor_hz: float | None = None
    tracked: bool = False
    signal_dbfs: tuple[float, ...] = ()
    baseline: InterfaceBaseline | None = None
    anchor_channels: tuple[int, ...] = ()


def read_wav_samples(path):
    """Read bounded PCM and IEEE-float WAV input, preserving per-channel levels."""
    info = inspect_wav(path)
    if info.duration > MAX_CAPTURE_SECONDS:
        raise ValueError(f'Select a WAV of {MAX_CAPTURE_SECONDS:g} seconds or less for a response test.')
    if info.duration < 0.125:
        raise ValueError('This WAV has too few usable samples for a response test.')
    if info.bits == 24:
        # scipy cannot memory-map 24-bit samples. Widen only the selected
        # channels in short blocks so a multichannel file stays economical.
        raw = np.memmap(path, dtype=np.uint8, mode='r', offset=info.offset,
                        shape=(info.frames, info.channels, 3))
        values = np.empty((info.frames, min(info.channels, 2)), dtype=np.float32)
        for first in range(0, info.frames, 65536):
            block = raw[first:first + 65536, :2].astype(np.int32)
            levels = block[..., 0] | (block[..., 1] << 8) | (block[..., 2] << 16)
            values[first:first + len(block)] = ((levels ^ 0x800000) - 0x800000) / 0x800000
        return info.rate, values
    rate, samples = wavfile.read(path, mmap=True)
    values = np.asarray(samples)
    if values.ndim == 1:
        values = values[:, None]
    values = values[:, :2]
    if values.dtype.kind == 'u':
        midpoint = (np.iinfo(values.dtype).max + 1) / 2
        values = (values.astype(np.float32) - midpoint) / midpoint
    elif values.dtype.kind == 'i':
        values = values.astype(np.float32) / float(-np.iinfo(values.dtype).min)
    else:
        values = values.astype(np.float32)
    if len(values) < rate // 8 or not np.isfinite(values).all():
        raise ValueError('This WAV has too few usable samples for a response test.')
    return rate, values


def _pcm(samples):
    values = np.asarray(samples, dtype=np.float32)
    if values.ndim == 1:
        values = values[:, None]
    if values.ndim != 2 or not 1 <= values.shape[1] <= 2 or not len(values) or not np.isfinite(values).all():
        raise ValueError('Use a finite mono or stereo recording.')
    return values


def _find_marker(recording, key, low, high):
    low = max(low, 0)
    high = min(high, len(recording) - len(key))
    if high < low:
        return None, 0.0
    best_index, best_score = None, 0.0
    key = np.asarray(key - key.mean(), dtype=np.float32)
    key_power = np.dot(key, key)
    for channel in recording.T:
        search = channel[low:high + len(key)]
        correlations = signal.oaconvolve(search, key[::-1], mode='valid')
        cumulative = np.concatenate(([0.0], np.cumsum(search * search, dtype=np.float64)))
        powers = cumulative[len(key):] - cumulative[:-len(key)]
        totals = np.concatenate(([0.0], np.cumsum(search, dtype=np.float64)))
        powers -= np.square(totals[len(key):] - totals[:-len(key)]) / len(key)
        score = np.divide(np.abs(correlations), np.sqrt(np.maximum(powers, 0) * key_power),
                          out=np.zeros_like(correlations), where=powers > len(key) * 1e-10)
        index = int(np.argmax(score))
        if score[index] > best_score:
            best_score = float(score[index])
            position = float(index)
            if 0 < index < len(score) - 1:
                a, b, c = (float(value) for value in score[index - 1:index + 2])
                curve = a - 2 * b + c
                if curve:
                    position += float(np.clip(0.5 * (a - c) / curve, -0.5, 0.5))
            best_index = low + position
    return best_index, best_score


def _marker_positions(recording, reference_rate, capture_rate, stimulus, offset_ms, drift_ppm):
    """Estimate one timing map, using the same physical channel for both markers."""
    ratio = capture_rate / reference_rate
    _, ref_end, ref_second = stimulus.segments(reference_rate)
    capture_second = ref_second * ratio
    start_key = _marker(capture_rate, source_rate=reference_rate)
    end_key = _marker(capture_rate, True, reference_rate)
    first_limit = min(round(capture_rate * 40), len(recording) - round(ref_end * ratio))
    spread = round(capture_rate * max(0.2, 0.085 * stimulus.total_seconds))
    pairs = []
    start_found = False
    for index in range(recording.shape[1]):
        channel = recording[:, index:index + 1]
        first, start_score = ((capture_rate * offset_ms / 1000, 1.0) if offset_ms is not None
                              else _find_marker(channel, start_key, 0, first_limit))
        if first is None or start_score < .24:
            continue
        start_found = True
        if drift_ppm is None:
            center = round(first + capture_second)
            last, end_score = _find_marker(channel, end_key, center - spread, center + spread)
            if last is None or end_score < .24:
                continue
        else:
            last, end_score = None, 1.0
        pairs.append((start_score * end_score, first, last, index))
    if not start_found:
        raise ValueError('The start marker was not found. Check the return route and level, or set the offset manually.')
    if not pairs:
        raise ValueError('No channel contained both timing markers. Capture the full signal or enter the clock drift manually.')
    # Prefer the first channel when both of its markers are reliable. Keep
    # time zero on the same route across a loopback and its later DUT capture.
    _, first, last, index = next((pair for pair in pairs if pair[0] >= .36), max(pairs))
    if drift_ppm is None:
        drift_ppm = ((last - first) / capture_second - 1) * 1e6
        # Initial templates use the nominal clock. Once both markers give
        # a drift estimate, stretch both templates to remove bias from the
        # shifted peaks of their opposite chirps.
        if abs(drift_ppm) > 1:
            channel = recording[:, index:index + 1]
            refined_rate = capture_rate * (1 + drift_ppm / 1e6)
            radius = max(8, round(capture_rate * 0.012))
            if offset_ms is None:
                at = round(first)
                key = _marker(refined_rate, source_rate=reference_rate)
                candidate, confidence = _find_marker(channel, key, at - radius, at + radius)
                if candidate is not None and confidence >= 0.24:
                    first = candidate
            at = round(last)
            candidate, confidence = _find_marker(channel, _marker(refined_rate, True, reference_rate),
                                                 at - radius, at + radius)
            if candidate is not None and confidence >= 0.24:
                last = candidate
            drift_ppm = ((last - first) / capture_second - 1) * 1e6
    if not isfinite(first) or not isfinite(drift_ppm) or abs(drift_ppm) > 100_000:
        raise ValueError('Enter a valid timing offset and a clock drift within ±100,000 ppm.')
    return first, ratio * (1 + drift_ppm / 1e6), drift_ppm


def _sample_at(recording, first, ratio, start, end):
    frames = end - start
    first_position = first + ratio * start
    last_position = first + ratio * (end - 1)
    if frames <= 0 or first_position < 0 or last_position >= len(recording):
        raise ValueError('The capture does not contain the entire test signal at this timing offset.')
    if ratio == 1 and first == round(first):
        index = round(first_position)
        return recording[index:index + frames]
    # Cubic interpolation has a phase-dependent high-frequency droop. Use a
    # fractional-delay, Kaiser-windowed sinc with an anti-alias cutoff for
    # downsampling. Linear interpolation across the phase bank is much finer
    # than the precision obtainable from the measured return markers.
    cutoff = min(1, 1 / ratio)
    half = ceil(64 / cutoff)
    taps = np.arange(1 - half, half + 1)
    phases = max(64, min(2048, 1_000_000 // len(taps)))
    distance = np.linspace(0, 1, phases + 1)[:, None] - taps
    window = np.i0(11.5 * np.sqrt(np.maximum(0, 1 - (distance / half) ** 2))) / np.i0(11.5)
    bank = np.sinc(distance * cutoff) * window
    bank /= bank.sum(axis=1, keepdims=True)
    size = min(4096, max(1, 225_000 // len(taps)))
    output = np.empty((frames, recording.shape[1]), dtype=np.float64)
    for offset in range(0, frames, size):
        position = first + ratio * np.arange(start + offset, start + min(offset + size, frames), dtype=float)
        base = position.astype(np.intp)
        phase = (position - base) * phases
        lower = np.minimum(phase.astype(np.intp), phases - 1)
        fraction = (phase - lower)[:, None]
        kernel = bank[lower] * (1 - fraction) + bank[lower + 1] * fraction
        indices = np.clip(base[:, None] + taps, 0, len(recording) - 1)
        output[offset:offset + len(position)] = np.einsum('ij,ijc->ic', kernel, recording[indices], optimize=False)
    return output


def _log_bins(low, high, resolution):
    if resolution not in OCTAVE_RESOLUTIONS:
        raise ValueError('Choose a supported octave resolution.')
    count = max(2, ceil(np.log2(high / low) * resolution) + 1)
    centers = np.geomspace(low, high, count)
    edges = np.concatenate(([low / 2 ** (0.5 / resolution)], np.sqrt(centers[:-1] * centers[1:]),
                            [high * 2 ** (0.5 / resolution)]))
    return centers, edges


def _aggregate(freq, values, centers, edges, *, circular=False, interpolate=True):
    """Use a log-band median for gain and a circular mean for phase."""
    output = np.full((len(centers), values.shape[1]), np.nan)
    indices = np.searchsorted(freq, edges)
    for index, (start, stop) in enumerate(pairwise(indices)):
        if stop > start:
            entries = values[start:stop]
            for channel, column in enumerate(entries.T):
                valid = column[np.isfinite(column)]
                if len(valid):
                    output[index, channel] = (np.rad2deg(np.angle(np.mean(np.exp(1j * np.deg2rad(valid)))))
                                              if circular else np.median(valid))
    if interpolate:
        # Some high-resolution octave bins are narrower than the linear FFT
        # bins, especially at the bottom of the range. Fill only empty
        # between-bin holes. A populated band with an invalid observation
        # is a gap and must never be bridged by neighbouring measurements.
        populated = np.flatnonzero(np.diff(indices))
        empty = np.flatnonzero(np.diff(indices) == 0)
        after = np.searchsorted(populated, empty)
        bounded = (after > 0) & (after < len(populated))
        empty, after = empty[bounded], after[bounded]
        left, right = populated[after - 1], populated[after]
        for channel, column in enumerate(output.T):
            valid = np.isfinite(column)
            if valid.sum() < 2:
                continue
            missing = empty[valid[left] & valid[right]]
            if not len(missing):
                continue
            phase_or_gain = np.rad2deg(np.unwrap(np.deg2rad(column[valid]))) if circular else column[valid]
            interp = np.interp(np.log(centers[missing]), np.log(centers[valid]), phase_or_gain)
            output[missing, channel] = (interp + 180) % 360 - 180 if circular else interp
    return output


def _sweep_transfer(reference, recorded, rate, *, stimulus=None):
    size = fft.next_fast_len(len(reference), real=True)
    x = fft.rfft(np.asarray(reference, dtype=np.float64), size)
    y = fft.rfft(recorded, size, axis=0)
    energy = np.abs(x) ** 2
    floor = float(np.max(energy)) * 1e-10
    if floor <= 0:
        raise ValueError('The digital sweep reference is silent.')
    transfer = np.divide(y * np.conjugate(x[:, None]), (energy + floor)[:, None])
    if stimulus is not None and stimulus.high_hz >= 2 * stimulus.low_hz:
        # Inverting an exponential sweep places the second-harmonic impulse
        # earlier than the fundamental by T·ln(2)/ln(fmax/fmin). Higher orders
        # arrive earlier still (Farina, AES preprint 5093). Gate the impulse
        # around the fundamental and retain its ring-down in the trailing guard.
        gap = stimulus.duration * np.log(2) / np.log(stimulus.high_hz / stimulus.low_hz)
        leading = round(rate * min(.25, gap / 2))
        trailing = round(rate * .92 * GUARD_SECONDS)
        window = _fade(np.ones(leading + trailing + 1), round(rate * .02))
        ir = fft.irfft(transfer, size, axis=0)
        scale = fft.irfft(energy / (energy + floor), size)
        # Apply the identical gate to a unity return. Its spectrum removes
        # amplitude/phase coloration from the finite-band regularized inverse.
        for impulse in (ir, scale[:, None]):
            impulse[:trailing + 1] *= window[leading:, None]
            impulse[-leading:] *= window[:leading, None]
            impulse[trailing + 1:-leading] = 0
        scale = fft.rfft(scale)
        transfer = np.divide(fft.rfft(ir, axis=0), scale[:, None], out=np.full_like(transfer, np.nan),
                             where=np.abs(scale[:, None]) > .1)
    transfer[energy < floor] = np.nan
    return fft.rfftfreq(size, 1 / rate), transfer, None


def _noise_transfer(reference, recorded, rate):
    length = min(65536, 2 ** int(np.floor(np.log2(len(reference) / 5))))
    kwargs = {'fs': rate, 'window': 'hann', 'nperseg': length, 'noverlap': length // 2}
    frequencies, power = signal.welch(reference, **kwargs)
    floor = float(np.max(power)) * 1e-10
    transfer, coherence = [], []
    for channel in recorded.T:
        _, cross = signal.csd(reference, channel, **kwargs)
        _, response = signal.welch(channel, **kwargs)
        transfer.append(np.where(power > floor, cross / np.maximum(power, floor), np.nan))
        combined = power * response
        coherence.append(np.divide(np.abs(cross) ** 2, combined, out=np.zeros_like(response), where=combined > 0))
    return frequencies, np.column_stack(transfer), np.clip(np.column_stack(coherence), 0, 1)


def preview_transfer(capture, stimulus, *, resolution=12, offset_ms=None, drift_ppm=None):
    """Estimate response before the end marker arrives, using recorded frames only."""
    reference_rate = capture.output_rate
    capture_rate = capture.input_rate
    if reference_rate is None or capture.reference is None:
        return None
    ratio = capture_rate / reference_rate
    available = round(capture.seconds * capture_rate)
    if offset_ms is None:
        prefix = capture.section(0, round(2.5 * capture_rate))
        key = _marker(capture_rate, source_rate=reference_rate)
        first, quality = _find_marker(prefix, key, 0, len(prefix) - len(key))
        if first is None or quality < .55:
            return None
    else:
        first = offset_ms * capture_rate / 1000
    ratio *= 1 + (drift_ppm or 0) / 1e6
    start, end, _ = stimulus.segments(reference_rate)
    usable_end = min(end, int((available - first) / ratio) - round(.02 * reference_rate))
    if usable_end - start < .75 * reference_rate:
        return None
    low, high = stimulus.band(reference_rate)
    high = min(high, .47 * capture_rate)
    sweep = stimulus.kind == 'sweep'
    if sweep:
        # Retain the measured sweep so far, but report only the frequencies
        # excited before the trailing taper. No future samples are inferred.
        sweep_rate = np.log(stimulus.high_hz / stimulus.low_hz) / (end - start)
        high = min(high, stimulus.low_hz * np.exp(sweep_rate * (usable_end - start - round(.04 * reference_rate))))
    else:
        start = max(start, usable_end - round(4 * reference_rate))
    if high <= low * 1.1:
        return None
    begin_frame = max(0, int(first + ratio * start) - ceil(66 * max(1, ratio)))
    end_frame = ceil(first + ratio * usable_end) + ceil(66 * max(1, ratio))
    recorded = capture.section(begin_frame, end_frame)
    aligned = _sample_at(recorded, first - begin_frame, ratio, start, usable_end)
    reference = capture.reference[start:usable_end]
    if sweep:
        window = _fade(np.ones(len(reference)), round(.04 * reference_rate))
        frequencies, transfer, coherence = _sweep_transfer(reference * window, aligned * window[:, None],
                                                             reference_rate)
    else:
        frequencies, transfer, coherence = _noise_transfer(reference, aligned, reference_rate)
    centers, edges = _log_bins(low, high, resolution)
    if coherence is not None:
        transfer[coherence < .80] = np.nan
    gain = 20 * np.log10(np.maximum(np.abs(transfer), 10 ** (FLOOR_DB / 20)))
    phase = _aggregate(frequencies, np.angle(transfer, deg=True), centers, edges, circular=True, interpolate=False)
    return ResponseCurve(centers, _aggregate(frequencies, gain, centers, edges, interpolate=False), phase)


def _interface_baseline(recording, reference_rate, capture_rate, stimulus, first, ratio):
    from .spectrum import distortion, power_db, power_spectrum

    quiet_start, tone_start, end = stimulus.baseline_segments(reference_rate)
    guard = round(reference_rate * .08)
    quiet = _sample_at(recording, first, ratio, quiet_start, tone_start)
    tone = _sample_at(recording, first, ratio, tone_start + guard, end - guard)
    high = min(20_000, 0.47 * min(reference_rate, capture_rate))
    if high <= 3 * LOOPBACK_TONE_HZ:
        raise ValueError('The input sample rate cannot measure harmonics of the calibration tone.')
    noises, readings = [], []
    for idle, pilot in zip(quiet.T, tone.T, strict=True):
        noise = power_spectrum(idle, reference_rate, remove_dc=True)
        noises.append(power_db(noise.band_power(20, high)))
        spectrum = power_spectrum(pilot, reference_rate, remove_dc=True)
        reading = distortion(pilot, spectrum, reference_rate, max_frequency=high, harmonic_count=20, check_clipping=False)
        if not reading['available'] or abs(reading['fundamentalFrequency'] - LOOPBACK_TONE_HZ) > 2:
            raise ValueError('The 997 Hz calibration tone is missing or too weak on an input channel.')
        readings.append(reading)
    return InterfaceBaseline(tuple(noises), tuple(reading['signalDb'] for reading in readings),
                             tuple(reading['thd'] for reading in readings),
                             tuple(reading['thdn'] for reading in readings), stimulus.level_db, high)


def measure_response(reference, recording, reference_rate, capture_rate, stimulus,
                     *, resolution=12, offset_ms=None, drift_ppm=None, track_recorded_sweep=False):
    """Measure stereo gain and phase, with noise coherence when estimable."""
    recording = _pcm(recording)
    reference = np.asarray(reference, dtype=np.float32).reshape(-1)
    low, high = stimulus.band(reference_rate)
    high = min(high, 0.47 * capture_rate)
    if high <= low:
        raise ValueError('The input and output sample rates have no common measurement band.')
    start, end, _ = stimulus.segments(reference_rate)
    if len(reference) < end or not np.isfinite(reference).all():
        raise ValueError('The digital reference is incomplete.')
    first, ratio, drift = _marker_positions(recording, reference_rate, capture_rate, stimulus, offset_ms, drift_ppm)
    reference_test = reference[start:end]
    sweep_response = stimulus.kind == 'sweep' and not track_recorded_sweep
    if sweep_response:
        guard = round(GUARD_SECONDS * reference_rate)
        # Capture as much trailing response as exists before the end marker.
        # Manual timing also supports files ending at the last sweep sample.
        capture_end = min(end + guard, floor((len(recording) - 1 - first) / ratio) + 1)
        lead = min(guard, max(0, floor(start + first / ratio)))
        returned = np.asarray(_sample_at(recording, first, ratio, start - lead, max(end, capture_end)), dtype=np.float64)
        aligned = returned[lead:lead + end - start]
        excitation = np.pad(reference_test, (lead, len(returned) - lead - len(reference_test)))
    else:
        aligned = np.asarray(_sample_at(recording, first, ratio, start, end), dtype=np.float64)
    latency = first * 1000 / capture_rate
    if np.max(np.abs(aligned)) < 1e-5:
        raise ValueError('The return signal is too quiet to measure.')
    level = tuple(10 * np.log10(np.maximum(np.mean(aligned ** 2, axis=0), 10 ** (FLOOR_DB / 10))))
    clipping = bool(np.any(np.abs(recording) >= 0.999))
    if stimulus.kind == 'sweep' and track_recorded_sweep:
        # A tape transport can change speed many times within one sweep. Follow
        # the frequency actually returned by the tape so small instantaneous
        # speed changes do not move energy out of matched FFT bins. Phase is
        # deliberately unavailable in this mode without a stable time base.
        guard = round(reference_rate * 0.028)
        centers, edges = _log_bins(low, high, resolution)
        frequencies, power = _tone_windows(aligned[guard:-guard], reference_rate, low, high)
        level = _bands_of_tones(frequencies, power, edges, 'median')
        reference_rms = np.max(np.abs(reference[start + guard:end - guard])) / np.sqrt(2)
        gain = 20 * np.log10(np.maximum(level / reference_rms, 1e-9))
        return ResponseCurve(centers, gain, delay_ms=latency, drift_ppm=drift,
                             clipping=clipping, tracked=True)
    if sweep_response:
        frequencies, transfer, quality = _sweep_transfer(excitation, returned, reference_rate, stimulus=stimulus)
    else:
        frequencies, transfer, quality = _noise_transfer(reference_test, aligned, reference_rate)
        # Poor coherence signals a return dominated by uncorrelated noise or
        # nonlinearity. Keep the coherence data itself available for diagnosis.
        transfer[quality < .55] = np.nan
    centers, edges = _log_bins(low, high, resolution)
    gain = 20 * np.log10(np.maximum(np.abs(transfer), 10 ** (FLOOR_DB / 20)))
    phase = _aggregate(frequencies, np.angle(transfer, deg=True), centers, edges, circular=True)
    baseline = (_interface_baseline(recording, reference_rate, capture_rate, stimulus, first, ratio)
                if isinstance(stimulus, InterfaceStimulus) and not clipping else None)
    return ResponseCurve(centers, _aggregate(frequencies, gain, centers, edges), phase,
                         _aggregate(frequencies, quality, centers, edges) if quality is not None else None,
                         latency, drift, clipping, signal_dbfs=level, baseline=baseline)


def _tone_windows(data, rate, low_hz, high_hz):
    """Find steady tape tones and retain levels for channels carrying that tone."""
    if len(data) / rate < 0.25:
        raise ValueError('Capture at least a quarter second of the tape’s test tones.')
    # Excluding ultrasonic and subsonic components makes the Hilbert phase
    # slope track the recorded test tone rather than bias, DC or mechanical hum.
    sos = signal.butter(4, (max(3, 0.45 * low_hz), min(rate * 0.49, high_hz * 1.12)),
                        btype='bandpass', fs=rate, output='sos')
    hop = max(32, round(rate * 0.012))
    count = (len(data) - 1) // hop
    pad = ceil(max(0.25, 4 / low_hz) * rate)
    energy = np.empty((count, data.shape[1]))
    freq = np.empty_like(energy)
    jitter = np.empty_like(energy)
    # Keep Hilbert transforms and zero-phase filter workspaces bounded even
    # for an entire alignment track. Overlap both sides so the boundaries do
    # not appear as fictitious tones at the seams between blocks.
    for begin in range(0, count, 400):
        end = min(count, begin + 400)
        first = max(0, begin * hop - pad)
        last = min(len(data), end * hop + 1 + pad)
        filtered = signal.sosfiltfilt(sos, data[first:last], axis=0).astype(np.float32)
        selected = slice(begin * hop - first, end * hop - first + 1)
        analytic = signal.hilbert(filtered, axis=0)[selected]
        envelope = np.abs(analytic[:-1]).reshape(end - begin, hop, data.shape[1])
        energy[begin:end] = np.sqrt(np.mean(envelope ** 2, axis=1) / 2)
        instantaneous = np.angle(analytic[1:] * np.conjugate(analytic[:-1])) * (rate / (2 * np.pi))
        slope = instantaneous.reshape(end - begin, hop, data.shape[1])
        centers = freq[begin:end] = np.median(slope, axis=1)
        jitter[begin:end] = np.median(np.abs(slope - centers[:, None, :]), axis=1)
    tonal = ((freq >= low_hz * 0.98) & (freq <= high_hz * 1.02)
             & (jitter <= np.maximum(5, freq * 0.045)))
    tonal &= energy > np.maximum(1e-5, np.max(np.where(tonal, energy, 0), axis=0) * 0.008)
    # Choose a stable phase slope before comparing levels. Broadband noise on
    # one channel must neither suppress the clean channel nor count as its tone.
    strongest = np.argmax(np.where(tonal, energy, 0), axis=1)
    frequencies = freq[np.arange(count), strongest]
    matching = tonal & (np.abs(freq - frequencies[:, None]) <= np.maximum(5, frequencies[:, None] * .02))
    valid = matching.any(axis=1)
    if data.shape[1] == 2:
        # Two clear but different pitches do not identify a shared test tone.
        valid &= ~tonal.all(axis=1) | matching.all(axis=1)
    if np.count_nonzero(valid) < 4:
        raise ValueError('Too few clean test tones were detected. Check playback and capture the complete test track.')
    # The zero-phase tracking filter is there to reject hum and tape bias.
    # Remove its known forward/backward magnitude response from each measured
    # tone. Otherwise choosing a different analysis range changes the apparent
    # tape response, particularly when a tone is close to the upper limit.
    frequencies = frequencies[valid]
    _, tracker = signal.sosfreqz(sos, worN=frequencies, fs=rate)
    return frequencies, np.where(matching[valid], energy[valid], np.nan) / np.square(np.abs(tracker))[:, None]


def _playback_level(energy, aggregate):
    if aggregate == 'maximum':
        return np.max(energy, axis=0)
    if aggregate == 'peak':
        # Existing profiles use the original key for the robust percentile.
        return np.quantile(energy, 0.95, axis=0)
    if aggregate == 'mean':
        return np.sqrt(np.mean(energy ** 2, axis=0))
    return np.median(energy, axis=0)


def _channel_playback_levels(energy, aggregate):
    levels = np.full(energy.shape[1], np.nan)
    for channel, samples in enumerate(energy.T):
        usable = samples[np.isfinite(samples)]
        if len(usable):
            levels[channel] = _playback_level(usable, aggregate)
    return levels


def _bands_of_tones(freq, energy, edges, aggregate):
    values = np.full((len(edges) - 1, energy.shape[1]), np.nan)
    for index, (a, b) in enumerate(pairwise(edges)):
        in_band = energy[(freq >= a) & (freq < b)]
        if len(in_band):
            values[index] = _channel_playback_levels(in_band, aggregate)
    return values


def trace_playback(recording, rate, *, low_hz=20, high_hz=20_000, resolution=12, anchor_hz=1000,
                   aggregate='median'):
    """Trace externally supplied stepped or swept sine without a digital timing reference."""
    data = _pcm(recording)
    high_hz = min(high_hz, rate * 0.47)
    if not 5 <= low_hz < high_hz or not low_hz <= anchor_hz <= high_hz:
        raise ValueError('Set a frequency range that includes the reference frequency.')
    freq, energy = _tone_windows(data, rate, low_hz, high_hz)
    centers, edges = _log_bins(low_hz, high_hz, resolution)
    magnitude = 20 * np.log10(np.maximum(_bands_of_tones(freq, energy, edges, aggregate), 1e-9))
    anchor = 2 ** (0.55 / max(3, resolution))
    existing = (freq >= anchor_hz / anchor) & (freq <= anchor_hz * anchor)
    if not existing.any():
        raise ValueError(f'The {anchor_hz:g} Hz level reference was not detected. Choose a tone present on the test tape.')
    reference_db = 20 * np.log10(np.maximum(_channel_playback_levels(energy[existing], aggregate), 1e-9))
    normalization = float(np.nanmean(reference_db))
    return ResponseCurve(centers, magnitude - normalization, clipping=bool(np.any(np.abs(data) >= 0.999)),
                         relative=True, anchor_hz=anchor_hz, tracked=True,
                         anchor_channels=tuple(np.flatnonzero(np.isfinite(reference_db))))


def response_test_defaults(mode):
    return {
        'duration': 30 if mode == 'tape_playback' else 10,
        'lowHz': 20, 'highHz': 20_000, 'levelDb': -20, 'source': 'sweep',
        'tapeHeads': 'two_head', 'resolution': 12,
        'tailSeconds': 5 if mode == 'tape_recording' else 2,
        'anchorHz': 1000, 'trackTape': True, 'playbackAggregate': 'median', 'channels': 'audio', 'calibrationTrimDb': 0,
    }


def normalize_response_test_defaults(value):
    """Keep one reusable setup per test, excluding the timing of individual passes."""
    if not isinstance(value, dict):
        return {}
    defaults = {}
    for mode in RESPONSE_MODES:
        saved = value.get(mode)
        if not isinstance(saved, dict):
            continue
        factory = response_test_defaults(mode)
        setup = dict(factory)
        playback = mode == 'tape_playback'
        for key, bounds in {
            'duration': (1, MAX_CAPTURE_SECONDS) if playback else (2, MAX_RESPONSE_SECONDS),
            'lowHz': (5, 180_480), 'highHz': (5.5, 180_480), 'levelDb': (-80, -1),
            'tailSeconds': (.5, 40), 'anchorHz': (5, 90_000), 'calibrationTrimDb': (-40, 40),
        }.items():
            try:
                number = float(saved.get(key, factory[key]))
            except (TypeError, ValueError, OverflowError):
                continue
            if bounds[0] <= number <= bounds[1]:
                setup[key] = number
        # An external tape band must contain a reference the editor can represent.
        if setup['highHz'] < 1.1 * setup['lowHz'] or (playback and setup['lowHz'] > 90_000):
            setup.update(lowHz=factory['lowHz'], highHz=factory['highHz'])
        if playback and not setup['lowHz'] <= setup['anchorHz'] <= setup['highHz']:
            setup['anchorHz'] = min(setup['highHz'], max(setup['lowHz'], setup['anchorHz']))
        for key, allowed in {
            'source': KINDS, 'tapeHeads': ('two_head', 'three_head'), 'resolution': OCTAVE_RESOLUTIONS,
            'playbackAggregate': ('median', 'mean', 'peak', 'maximum'), 'channels': ('audio', 'mono', 'stereo'),
        }.items():
            if saved.get(key) in allowed:
                setup[key] = saved[key]
        if isinstance(saved.get('trackTape'), bool):
            setup['trackTape'] = saved['trackTape']
        defaults[mode] = setup
    return defaults


def response_balance(gain_db):
    """Positive values indicate higher right-channel gain at the same frequency."""
    return gain_db[:, 1:2] - gain_db[:, :1] if gain_db.shape[1] == 2 else None


def normalize_response_calibrations(value):
    """Validate settings, including curves saved on another platform or sample rate."""
    profiles = []
    if not isinstance(value, list):
        return profiles
    known = set()
    for profile in value[:64]:
        if not isinstance(profile, dict):
            continue
        name = str(profile.get('name', '')).strip()[:80]
        if not name or name.casefold() in known:
            continue
        try:
            frequencies = np.asarray(profile['frequencyHz'], dtype=float)
            gain = np.asarray(profile['gainDb'], dtype=float)
            rate = float(profile.get('sampleRate', 0))
            output_rate = float(profile.get('outputRate', 0))
            offset = float(profile.get('offsetDb', 0))
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if (frequencies.ndim != 1 or not 2 <= len(frequencies) <= 2048 or not np.isfinite(frequencies).all()
                or gain.ndim != 2 or gain.shape[0] != len(frequencies) or gain.shape[1] not in (1, 2)
                or np.isinf(gain).any() or not np.isfinite(gain).any(axis=1).all()
                or np.nanmax(np.abs(gain)) > 180 or not 0 <= rate <= 384_000
                or not 0 <= output_rate <= 384_000
                or not -40 <= offset <= 40 or not 0 < frequencies[0] < frequencies[-1] <= 192_000
                or np.any(np.diff(frequencies) <= 0)):
            continue
        bands = None
        if 'bandsHz' in profile:
            try:
                bands = np.asarray(profile['bandsHz'], dtype=np.float64)
            except (TypeError, ValueError, OverflowError):
                continue
            if (bands.ndim != 2 or bands.shape[1] != 2 or not 1 <= len(bands) <= len(frequencies)
                    or not np.isin(bands, frequencies).all() or np.any(bands[:, 0] > bands[:, 1])
                    or np.any(bands[1:, 0] <= bands[:-1, 1])):
                continue
        known.add(name.casefold())
        gain_values = [[float(value) if np.isfinite(value) else None for value in row] for row in gain]
        item = {'name': name, 'frequencyHz': frequencies.tolist(), 'gainDb': gain_values,
                'sampleRate': rate, 'outputRate': output_rate, 'offsetDb': offset,
                'input': str(profile.get('input', ''))[:240], 'output': str(profile.get('output', ''))[:240]}
        if bands is not None:
            item['bandsHz'] = bands.tolist()
        try:
            phase = np.asarray(profile.get('phaseDeg'), dtype=float)
        except (TypeError, ValueError, OverflowError):
            phase = np.empty(0)
        if phase.shape == gain.shape and not np.isinf(phase).any() and np.isfinite(phase).any():
            item['phaseDeg'] = [[float(value) if np.isfinite(value) else None for value in row] for row in phase]
        for key, maximum in (('markerOffsetMs', 180_000), ('clockDriftPpm', 100_000)):
            try:
                number = float(profile.get(key))
            except (TypeError, ValueError, OverflowError):
                continue
            if abs(number) <= maximum:
                item[key] = number
        baseline = _checked_baseline(profile.get('baseline'), gain.shape[1])
        if baseline is not None:
            item['baseline'] = baseline
        profiles.append(item)
    return profiles


def _checked_baseline(data, channels):
    if not isinstance(data, dict):
        return None
    limits = {'noiseDbfs': (-180, 10), 'toneDbfs': (-180, 10),
              'thdPercent': (0, 1e4), 'thdnPercent': (0, 1e4)}
    try:
        checked = {key: list(map(float, data[key])) for key in limits}
        high = float(data['highHz'])
        drive = float(data['driveDbfsPeak'])
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if not (3 * LOOPBACK_TONE_HZ < high <= 20_000 and -80 <= drive <= -1):
        return None
    if not all(len(values) == channels and all(limits[key][0] <= value <= limits[key][1] for value in values)
               for key, values in checked.items()):
        return None
    return dict(checked, highHz=high, driveDbfsPeak=drive, toneHz=LOOPBACK_TONE_HZ)


def calibration_profile(name, curve, rate, *, input_device='', output_device='', output_rate=0, offset_db=0):
    usable = np.isfinite(curve.gain_db)
    if curve.coherence is not None:
        usable &= curve.coherence >= .80
    valid = usable.any(axis=1)
    # A different amount of noise on each channel must not erase otherwise
    # usable bands. Explicit nulls keep that channel's missing bins distinct
    # from unity gain, even after serialization to preferences.
    gain = np.where(usable, curve.gain_db, np.nan)
    phase = None if curve.phase_deg is None else np.where(usable, curve.phase_deg, np.nan)[valid]
    record = {
        'name': name, 'frequencyHz': curve.frequencies[valid], 'gainDb': gain[valid],
        'sampleRate': rate, 'outputRate': output_rate, 'input': input_device, 'output': output_device, 'offsetDb': offset_db,
        'phaseDeg': phase,
        'markerOffsetMs': curve.delay_ms, 'clockDriftPpm': curve.drift_ppm,
    }
    measured_bands = _connected_runs(valid)
    if len(measured_bands) > 1:
        # Profiles use finite samples for compatibility. Remember each uninterrupted
        # frequency band separately so excluded FFT bins stay excluded after saving.
        record['bandsHz'] = [(curve.frequencies[run[0]], curve.frequencies[run[-1]]) for run in measured_bands]
    if curve.baseline is not None:
        record['baseline'] = curve.baseline.as_record()
    profiles = normalize_response_calibrations([record])
    if not profiles:
        raise ValueError('A calibration needs a name and at least two usable frequency bands.')
    return profiles[0]


def compensate_response(curve, profile):
    """Subtract interpolated gain within each uninterrupted calibration band."""
    offset = calibration_gain_at(curve.frequencies, profile, curve.gain_db.shape[1])
    return curve.gain_db - offset


def calibration_gain_at(target_hz, profile, channels):
    """Evaluate a saved gain correction without extrapolation across its gaps."""
    source_hz = np.asarray(profile['frequencyHz'])
    correction = np.asarray(profile['gainDb'], dtype=np.float64)
    gain = np.full((len(target_hz), channels), np.nan, dtype=np.float64)
    trim = profile.get('offsetDb', 0)
    for low, high in _calibration_bands(profile, source_hz):
        reference = (low <= source_hz) & (source_hz <= high)
        for channel in range(channels):
            baseline = correction[:, min(channel, correction.shape[1] - 1)]
            for run in _connected_runs(reference & np.isfinite(baseline)):
                target = (source_hz[run[0]] <= target_hz) & (target_hz <= source_hz[run[-1]])
                gain[target, channel] = np.interp(np.log(target_hz[target]), np.log(source_hz[run]), baseline[run]) + trim
    return gain


def _calibration_bands(profile, frequencies):
    return profile.get('bandsHz', ((frequencies[0], frequencies[-1]),))


def _connected_runs(valid):
    indices = np.flatnonzero(valid)
    return np.split(indices, np.flatnonzero(np.diff(indices) > 1) + 1) if len(indices) else ()


def compensate_phase(curve, profile):
    """Remove the loopback's phase on a matched timed test, preserving gaps."""
    if curve.phase_deg is None or profile is None or curve.relative or 'phaseDeg' not in profile:
        return curve.phase_deg
    frequencies = np.asarray(profile['frequencyHz'])
    phase = np.asarray(profile['phaseDeg'], dtype=np.float64)
    corrected = np.full_like(curve.phase_deg, np.nan, dtype=np.float64)
    for channel in range(min(corrected.shape[1], phase.shape[1])):
        baseline = phase[:, channel]
        for low, high in _calibration_bands(profile, frequencies):
            valid = np.isfinite(baseline) & (low <= frequencies) & (frequencies <= high)
            for run in _connected_runs(valid):
                usable = (frequencies[run[0]] <= curve.frequencies) & (curve.frequencies <= frequencies[run[-1]])
                # Delay has a linear phase slope in Hz, not along the log axis.
                unwrapped = np.unwrap(baseline[run], period=360)
                offset = np.interp(curve.frequencies[usable], frequencies[run], unwrapped)
                corrected[usable, channel] = (curve.phase_deg[usable, channel] - offset + 180) % 360 - 180
    return corrected


def phase_group_delay(frequency, phase_deg):
    """Differentiate locally-fitted phase on each valid segment of the grid."""
    if phase_deg is None:
        return None
    delay = np.full_like(phase_deg, np.nan, dtype=np.float64)
    for channel, phases in enumerate(phase_deg.T):
        for run in _connected_runs(np.isfinite(phases)):
            if len(run) < 3:
                continue
            x, y = frequency[run], np.unwrap(np.deg2rad(phases[run]))
            for index, center in enumerate(x):
                subset = slice(max(0, index - 3), min(len(x), index + 4))
                local = x[subset] - center
                radius = max(abs(local[0]), abs(local[-1]))
                weights = np.exp(-2 * (local / radius) ** 2)
                coefficients = np.polynomial.polynomial.polyfit(local / radius, y[subset], 2, w=weights)
                delay[run[index], channel] = -1000 * coefficients[1] / (2 * np.pi * radius)
    return delay
