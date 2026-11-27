"""Audio bench measurements with independent time-domain fits and spectral estimates."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from functools import lru_cache
from math import ceil, floor, isfinite

import numpy as np
from scipy import fft, optimize, signal

from .response import GUARD_SECONDS, MARKER_SECONDS, _fade, _marker, _marker_positions, _pcm, _sample_at
from .spectrum import distortion, power_db, power_spectrum

STEP_END_GUARD_SECONDS = .015
CLIP_LEVEL = .9995

IMD_MODES = {
    'smpte': (60.0, 7000.0, 4.0),
    'din': (250.0, 8000.0, 4.0),
    'ccif': (19000.0, 20000.0, 1.0),
}


def _measure_tones(values, rate, frequencies):
    """Fit all components jointly so noncoherent bins cannot contaminate each other."""
    time = (np.arange(len(values), dtype=np.float64) - (len(values) - 1) / 2) / rate
    weight = np.sqrt(signal.windows.blackmanharris(len(values), sym=False))
    block_size = 32768

    def design(part):
        angles = 2 * np.pi * time[part, None] * frequencies
        return np.column_stack((np.cos(angles), np.sin(angles), np.ones(len(angles))))

    # A tall-skinny QR keeps the least-squares conditioning without one
    # sample-by-tone matrix for the entire capture. At high sample rates and
    # long observations, such a matrix and its solver workspaces can use GiBs.
    reduced = np.empty((0, 2 * len(frequencies) + 2))
    for offset in range(0, len(values), block_size):
        part = slice(offset, offset + block_size)
        matrix = np.column_stack((design(part), values[part])) * weight[part, None]
        reduced = np.linalg.qr(np.concatenate((reduced, matrix)), mode='r')
    coefficients, _, rank, _ = np.linalg.lstsq(reduced[:, :-1], reduced[:, -1], rcond=1e-10)
    if rank != len(coefficients):
        raise ValueError('The tones cannot be distinguished from each other in this capture.')
    cosine, sine = np.split(coefficients[:-1], 2)
    residual = np.empty_like(values)
    for offset in range(0, len(values), block_size):
        part = slice(offset, offset + block_size)
        residual[part] = values[part] - design(part) @ coefficients
    return (cosine - 1j * sine) / np.sqrt(2), residual


def _refine_tone(values, rate, requested, tolerance):
    spectrum = power_spectrum(values, rate, remove_dc=True)
    width = spectrum.bin_width
    search = max(2 * width, requested * tolerance)
    low, high = max(width, requested - search), min(rate / 2 - width, requested + search)
    if high <= low:
        raise ValueError('This sample rate cannot resolve the selected reference tones.')
    estimated, _ = spectrum.peak(low, high)
    if not estimated:
        estimated = requested
    window = signal.windows.blackmanharris(len(values), sym=False)
    time = np.arange(len(values), dtype=float) / rate
    weighted = (values - np.average(values, weights=window)) * window

    def energy(frequency):
        return -abs(np.dot(weighted, np.exp(-2j * np.pi * frequency * time))) ** 2

    limits = max(low, estimated - width), min(high, estimated + width)
    return float(optimize.minimize_scalar(energy, bounds=limits, method='bounded',
                                          options={'xatol': width * 1e-6}).x)


def two_tone_imd(samples, rate, first_hz, second_hz, *, mode='smpte', tolerance=0.003, ceiling=None,
                 check_clipping=True):
    """Compute modulation sidebands or individual difference-frequency products.

    Modulation depth uses the in-phase baseband envelope of the upper tone.
    Difference products are reported relative to the sum of both tone amplitudes.
    """
    values = np.asarray(samples, dtype=np.float64).reshape(-1)
    if len(values) < max(round(rate / 4), 256) or not np.isfinite(values).all():
        raise ValueError('Capture at least a quarter second of finite audio.')
    df = rate / len(values)
    nyquist = min(rate / 2, ceiling) if ceiling is not None else rate / 2
    if not (5 <= first_hz < second_hz < nyquist - 4 * df and second_hz - first_hz >= 6 * df):
        raise ValueError('Choose resolvable tones below the input Nyquist frequency.')
    if mode not in IMD_MODES:
        raise ValueError('Choose an intermodulation method.')
    first = _refine_tone(values, rate, first_hz, tolerance)
    second = _refine_tone(values, rate, second_hz, tolerance)
    if mode == 'ccif':
        specified = {'f2 − f1': second - first, '2f1 − f2': 2 * first - second,
                     '2f2 − f1': 2 * second - first}
    else:
        specified = {f'f2 {sign} {order}f1': second + factor * order * first
                     for order in (1, 2, 3) for sign, factor in (('−', -1), ('+', 1))}
    products = {name: f for name, f in specified.items()
                if 3 * df < f < nyquist - 3 * df and min(abs(f - first), abs(f - second)) > 4 * df}
    if not products:
        raise ValueError('There are no resolvable distortion products at this sample rate.')
    fitted_frequencies = np.array((first, second, *products.values()))
    # Remove ordinary harmonics as nuisance terms. When a product coincides
    # with a harmonic, it cannot be identified reliably and is left unreported.
    harmonics = [order * f for f in (first, second) for order in range(2, 9) if order * f < nyquist - 3 * df]
    ambiguous = {key for key, f in products.items() if any(abs(f - h) < 4 * df for h in harmonics)}
    if ambiguous:
        products = {key: f for key, f in products.items() if key not in ambiguous}
        fitted_frequencies = np.array((first, second, *products.values()))
    if not products:
        raise ValueError('The selected tones make their harmonics and distortion products overlap.')
    extra = []
    for frequency in harmonics:
        if min(abs(frequency - f) for f in (*fitted_frequencies, *extra)) >= 4 * df:
            extra.append(frequency)
    amplitudes, residual = _measure_tones(values, rate, np.array((*fitted_frequencies, *extra)))
    measured = np.abs(amplitudes)
    reference = float(sum(measured[:2])) if mode == 'ccif' else float(measured[1])
    if min(measured[:2]) < 1e-5 or reference <= 0:
        raise ValueError('Both reference tones need a measurable input level.')
    residual_spectrum = power_spectrum(residual, rate, remove_dc=True)
    def noise_amplitude(frequency):
        distance = np.abs(residual_spectrum.frequencies - frequency)
        near = (distance >= 5 * df) & (distance <= 22 * df) & (residual_spectrum.frequencies < nyquist)
        bin_power = residual_spectrum.power[near]
        # A median periodogram bin has ln(2) times the mean for Gaussian noise.
        return np.sqrt(float(np.median(bin_power)) / np.log(2)) if bin_power.size else 0.0

    if any(amplitude < 5 * noise_amplitude(frequency) for amplitude, frequency in zip(measured[:2], (first, second), strict=True)):
        raise ValueError('Both reference tones must be above the input noise floor.')
    rows = []
    floors = {label: noise_amplitude(frequency) for label, frequency in products.items()}
    for (label, frequency), component in zip(products.items(), measured[2:2 + len(products)], strict=True):
        floor_amplitude = floors[label]
        rows.append({'product': label, 'frequencyHz': float(frequency),
                     'levelDbc': power_db((component / reference) ** 2),
                     'floorDbc': power_db((floor_amplitude / reference) ** 2) if floor_amplitude > 0 else None,
                     'limited': bool(floor_amplitude > 0 and component < 3 * floor_amplitude)})
    sidebands = dict(zip(products, amplitudes[2:2 + len(products)], strict=True))
    if mode == 'ccif':
        imd = 100 * abs(sidebands['f2 − f1']) / reference if 'f2 − f1' in sidebands else None
        limit = 300 * floors.get('f2 − f1', 0) / reference
    else:
        # Let c be the carrier phasor. After rotating by arg(c), an AM pair
        # adds with its conjugate. An FM pair cancels. Summing RF sideband
        # powers directly counts FM as distortion and understates pure AM.
        carrier = amplitudes[1]
        pairs = [(plus, minus) for order in (1, 2, 3) if (plus := f'f2 + {order}f1') in sidebands
                 and (minus := f'f2 − {order}f1') in sidebands]
        depths = [abs(sidebands[plus] / carrier + np.conj(sidebands[minus] / carrier)) for plus, minus in pairs]
        imd = 100 * float(np.linalg.norm(depths)) if depths else None
        limit = 300 * float(np.linalg.norm([floors[label] for pair in pairs for label in pair])) / reference
    clipped = bool(check_clipping and np.max(np.abs(values)) >= CLIP_LEVEL)
    if clipped:
        imd = None
    return {'imdPercent': None if imd is None else float(imd), 'firstHz': first, 'secondHz': second,
            'imdLimitPercent': float(limit) if limit else None, 'imdLimited': bool(imd is not None and imd < limit), 'products': rows,
            'referenceDbfs': power_db(reference ** 2), 'clipping': clipped,
            'incomplete': bool(len(products) != len(specified))}


@lru_cache(maxsize=6)
def _spectrum_window(length, window):
    return signal.get_window(window, length)


class SpectrumAverage:
    """Average one-sided calibrated density in linear power and retain peak hold."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.rate = self.length = self.window = None
        self.frames = 0
        self.power = self.peak = None
        self.blocks = deque()
        self.count = 0
        self._epoch = self._next_end = None

    def observe(self, values, rate, *, window='hann', average=8):
        self._add(values, rate, window, average)
        return self.values()

    def _add(self, values, rate, window, average):
        samples = np.asarray(values, dtype=np.float64)
        if samples.ndim == 1:
            samples = samples[:, None]
        if len(samples) < 128 or not np.isfinite(samples).all():
            raise ValueError('Wait for at least 128 samples of usable audio.')
        if (self.rate, self.length, self.window) != (rate, len(samples), window):
            self.reset()
            self.rate, self.length, self.window = rate, len(samples), window
        average = max(1, min(32, int(average)))
        taper = _spectrum_window(len(samples), window)
        samples = samples - np.average(samples, axis=0, weights=taper)
        transform = fft.rfft(samples * taper[:, None], axis=0)
        density = abs(transform)**2 / (rate * np.dot(taper, taper))
        density[1:-1 if len(samples) % 2 == 0 else None] *= 2
        if self.power is None or self.power.shape != density.shape:
            self.frames = 0
            self.blocks.clear()
            self.power = np.zeros_like(density)
            self.peak = density.copy()
        while len(self.blocks) >= average:
            self.power -= self.blocks.popleft()
        self.blocks.append(density)
        self.power += density
        self.frames += 1
        if self.frames % 128 == 0:
            self.power = np.sum(self.blocks, axis=0)
        np.maximum(self.peak, density, out=self.peak)
        self.count = len(self.blocks)

    def consume(self, buffer, length, *, window='hann', average=8):
        """Consume all new half-overlapped blocks retained in the input buffer."""
        if len(buffer.samples) < length:
            return False
        if (self._epoch, self.rate, self.length, self.window) != (buffer.epoch, buffer.rate, length, window):
            self.reset()
            self.rate, self.length, self.window = buffer.rate, length, window
            self._epoch = buffer.epoch
            self._next_end = buffer.end
        hop = length // 2
        earliest = buffer.start + length
        if self._next_end < earliest:
            # If the queue has aged out, keep the sample grid and resume at
            # the first complete block instead of joining unrelated samples.
            self._next_end += ceil((earliest - self._next_end) / hop) * hop
        end = self._next_end
        while self._next_end <= buffer.end:
            offset = self._next_end - buffer.start
            self._add(buffer.samples[offset - length:offset], buffer.rate, window, average)
            self._next_end += hop
        return self._next_end > end

    def values(self):
        return (fft.rfftfreq(self.length, d=1 / self.rate),
                10 * np.log10(np.maximum(self.power / self.count, 1e-30)),
                10 * np.log10(np.maximum(self.peak, 1e-30)))


@dataclass(frozen=True)
class StepStimulus:
    steps: tuple[tuple[float, float], ...]
    settle: float = .25
    observe: float = .75
    second_hz: float | None = None
    low_to_high: float = 1

    @property
    def period(self):
        return self.settle + self.observe + STEP_END_GUARD_SECONDS

    @property
    def duration(self):
        return len(self.steps) * self.period

    @property
    def total_seconds(self):
        return self.duration + 2 * (MARKER_SECONDS + GUARD_SECONDS)

    def segments(self, rate):
        start = round(MARKER_SECONDS * rate) + round(GUARD_SECONDS * rate)
        end = start + round(self.duration * rate)
        return start, end, end + round(GUARD_SECONDS * rate)

    def observation(self, index, rate):
        start = round((index * self.period + self.settle) * rate)
        return slice(start, start + round(self.observe * rate))

    def render(self, rate):
        minimum = 5 if self.second_hz is not None else 20
        if (not self.steps or self.settle < .10 or self.observe < .25 or self.duration > 115
                or not all(isfinite(f) and isfinite(level) and minimum <= f <= .46 * rate and -90 <= level <= -3
                           for f, level in self.steps)
                or self.second_hz is not None and not (self.steps[-1][0] < self.second_hz < .47 * rate
                                                       and 0 < self.low_to_high <= 10)):
            raise ValueError('Check the stimulus frequencies, levels and observation times for this output sample rate.')
        start, end, ending = self.segments(rate)
        marker = round(MARKER_SECONDS * rate)
        output = np.zeros(ending + marker, dtype=np.float32)
        output[:marker] = _marker(rate) * .1
        output[ending:] = _marker(rate, True) * .1
        for index, (frequency, db) in enumerate(self.steps):
            left = start + round(index * self.period * rate)
            right = min(end, start + round((index + 1) * self.period * rate))
            time = np.arange(right - left) / rate
            tone = np.sin(2 * np.pi * frequency * time)
            if self.second_hz is not None:
                tone = (self.low_to_high * tone + np.sin(2 * np.pi * self.second_hz * time)) / (self.low_to_high + 1)
            tone *= 10 ** (db / 20)
            output[left:right] = _fade(tone, round(.010 * rate))
        return output


def analyse_steps(reference, return_audio, output_rate, input_rate, stimulus):
    """Measure each steady hold after independently aligning both end markers."""
    windows, delay, drift = _aligned_observations(reference, return_audio, output_rate, input_rate, stimulus)
    band_high = min(20000, .46 * min(input_rate, output_rate))
    rows = []
    for (frequency, level), (frames, clipping) in zip(stimulus.steps, windows, strict=True):
        for channel, (block, clipped) in enumerate(zip(frames.T, clipping, strict=True), 1):
            reading = {}
            if not clipped and frequency <= band_high:
                spectrum = power_spectrum(block, output_rate, remove_dc=True)
                reading = distortion(block, spectrum, output_rate, min_frequency=20, max_frequency=band_high,
                                     harmonic_count=24, expected_frequency=frequency, check_clipping=False)
            valid = reading.get('available', False)
            rows.append({'frequencyHz': frequency, 'levelDbfs': level, 'channel': channel,
                         'detectedHz': reading['fundamentalFrequency'] if valid else None,
                         'signalDbfs': reading['signalDb'] if valid else None,
                         'thd': reading['thd'] if valid else None, 'thdn': reading['thdn'] if valid else None,
                         'hd3': reading['hd3'] if valid else None, 'clipping': clipped})
    return rows, delay, drift


def _aligned_observations(reference, return_audio, output_rate, input_rate, stimulus):
    if reference is None or len(reference) < stimulus.segments(output_rate)[1]:
        raise ValueError('The generated test reference is incomplete.')
    recording = _pcm(return_audio)
    first, ratio, drift = _marker_positions(recording, output_rate, input_rate, stimulus, None, None)
    start, _, _ = stimulus.segments(output_rate)

    def windows():
        for index in range(len(stimulus.steps)):
            observation = stimulus.observation(index, output_rate)
            begin, end = start + observation.start, start + observation.stop
            # Inspect converter samples before alignment can move or soften a
            # clipped peak. Only compare the actual uninterrupted observation.
            raw = recording[max(0, floor(first + ratio * begin)):ceil(first + ratio * end)]
            clipping = tuple(bool(value) for value in np.any(np.abs(raw) >= CLIP_LEVEL, axis=0))
            yield _sample_at(recording, first, ratio, begin, end), clipping

    return windows(), first * 1000 / input_rate, drift


def analyse_imd_capture(recording, input_rate, first, second, *, mode, stimulus=None, reference=None, output_rate=None):
    """Keep each channel's result when another return is silent or clipped."""
    rate = input_rate
    if stimulus is not None:
        windows, _, _ = _aligned_observations(reference, recording, output_rate, input_rate, stimulus)
        data, clipping = next(windows)
        rate = output_rate
    else:
        data = _pcm(recording)
        clipping = tuple(np.any(np.abs(data) >= CLIP_LEVEL, axis=0))
    rows = []
    for column, clipped in zip(data.T, clipping, strict=True):
        try:
            if clipped:
                raise ValueError('Input clipped. Reduce the recording level.')
            reading = two_tone_imd(column, rate, first, second, mode=mode, ceiling=input_rate / 2, check_clipping=False)
        except (ValueError, np.linalg.LinAlgError) as error:
            reading = {'imdPercent': None, 'firstHz': None, 'secondHz': None, 'products': [],
                       'clipping': bool(clipped), 'incomplete': True, 'error': str(error)}
        rows.append(reading)
    return rows
