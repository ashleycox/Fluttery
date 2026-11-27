"""Measurements shared by the cartridge and tape alignment assistants.

A test groove or tape supplies the stimulus. Calculations describe the audio
returned by the equipment. The setup positions themselves are supplied by the
operator and can be compared using repeated captures.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import pi, sqrt

import numpy as np
from scipy import fft, signal

from .spectrum import level_db, power_spectrum

ALIGNMENT_OPTION_LIMITS = {
    'seconds': (0.2, 2.0), 'tone_hz': (20, 20_000), 'second_hz': (20, 20_000),
    'tolerance_us': (0.1, 100), 'level_dbfs': (-70, -3), 'distortion_limit': (0.01, 30),
    'minimum_force': (0.1, 20), 'maximum_force': (0.1, 20),
}
ALIGNMENT_TARGET_LIMITS = {
    ('phono-loading', 2): (-60, 30), ('phono-gain', 2): (-60, -3),
    ('phono-gain', 3): (0.5, 30), ('tape-bias', 3): (0.05, 20),
}
_ALIGNMENT_STEP_COUNTS = {
    'antiskate': 3, 'tracking-force': 3, 'vta': 3, 'phono-loading': 3,
    'phono-gain': 4, 'head-azimuth': 3, 'tape-bias': 6,
}
_ALIGNMENT_OPTION_CHOICES = {'method': ('sidebands', 'difference'), 'noise': ('white', 'pink')}


def normalize_alignment_options(value):
    """Retain valid, explicitly saved step overrides from user preferences."""
    if not isinstance(value, dict):
        return {}
    normalized = {}
    for workflow, count in _ALIGNMENT_STEP_COUNTS.items():
        steps = value.get(workflow)
        if not isinstance(steps, dict):
            continue
        valid_steps = {}
        for index in range(1, count):
            step = steps.get(str(index))
            if not isinstance(step, dict):
                continue
            limits = dict(ALIGNMENT_OPTION_LIMITS)
            if (target := ALIGNMENT_TARGET_LIMITS.get((workflow, index))) is not None:
                limits['target_db'] = target
            options = {}
            for key, (lower, upper) in limits.items():
                number = step.get(key)
                if isinstance(number, (int, float)) and not isinstance(number, bool) and lower <= number <= upper:
                    options[key] = float(number)
            for key, choices in _ALIGNMENT_OPTION_CHOICES.items():
                if (selection := step.get(key)) in choices:
                    options[key] = selection
            if options.get('minimum_force', 1) > options.get('maximum_force', 3):
                options.pop('minimum_force', None)
                options.pop('maximum_force', None)
            if workflow == 'vta' and options.get('second_hz', 3000) <= options.get('tone_hz', 300):
                options.pop('tone_hz', None)
                options.pop('second_hz', None)
            if options:
                valid_steps[str(index)] = options
        if valid_steps:
            normalized[workflow] = valid_steps
    return normalized


@dataclass(frozen=True, slots=True)
class ChannelReading:
    rms_dbfs: float
    peak_dbfs: float
    tone_dbfs: float
    thd_percent: float
    thdn_percent: float
    hd3_percent: float | None
    confidence_db: float


@dataclass(frozen=True, slots=True)
class ToneReading:
    frequency: float
    channels: tuple[ChannelReading, ...]


@dataclass(frozen=True, slots=True)
class AzimuthReading:
    frequency: float
    phase_degrees: float
    delay_us: float
    tone_levels: tuple[float, float]
    confidence_db: tuple[float, float]
    coherence: float
    jitter_us: float
    clipped: bool


@dataclass(frozen=True, slots=True)
class TwoToneReading:
    frequencies: tuple[float, float]
    distortion_percent: tuple[float, ...]
    carrier_dbfs: tuple[float, ...]
    products: tuple[float, ...]


def _frames(samples):
    values = np.asarray(samples, dtype=np.float64)
    if values.ndim == 1:
        values = values[:, None]
    if values.ndim != 2 or values.shape[1] not in (1, 2) or len(values) < 32 or not np.all(np.isfinite(values)):
        raise ValueError('Wait for a complete audio capture.')
    return values


def levels(samples):
    """Return digital AC RMS and true sampled peak for each channel."""
    values = _frames(samples)
    rms = np.std(values, axis=0)
    # Include DC offsets in the peak. A biased converter can clip one side only.
    peak = np.max(np.abs(values), axis=0)
    return tuple((level_db(r), level_db(p)) for r, p in zip(rms, peak, strict=True))


def _fft_peaks(values, rate):
    length = fft.next_fast_len(4 * len(values), real=True)
    ac = (values - values.mean(axis=0)) * np.hanning(len(values))[:, None]
    # Combine channel powers to tolerate opposite polarity and unequal signals.
    power = np.sum(np.abs(fft.rfft(ac, n=length, axis=0)) ** 2, axis=1)
    return power, rate / length


def _frequency_near(power, resolution, nominal, tolerance):
    if not np.isfinite(nominal) or nominal <= 0 or nominal >= resolution * (len(power) - 1):
        raise ValueError('The selected tone is outside the range of the input sample rate.')
    half_band = max(2 * resolution, nominal * tolerance)
    first = max(1, int(np.ceil((nominal - half_band) / resolution)))
    last = min(len(power) - 2, int(np.floor((nominal + half_band) / resolution)))
    if first > last:
        raise ValueError('Increase the capture duration for this tone.')
    index = first + int(np.argmax(power[first:last + 1]))
    left, peak, right = np.log(np.maximum(power[index - 1:index + 2], np.finfo(float).tiny))
    curvature = left - 2 * peak + right
    offset = np.clip(0.5 * (left - right) / curvature, -0.5, 0.5) if curvature < 0 else 0
    return (index + offset) * resolution


def _fit(values, rate, frequencies):
    """Fit quadratures at fractional FFT bins and return peak-amplitude phasors."""
    times = np.arange(len(values))[:, None] * (2 * pi / rate)
    phase = times * np.asarray(frequencies)
    design = np.column_stack((np.ones(len(values)), np.cos(phase), np.sin(phase)))
    window = np.hanning(len(values))
    # Weight the least squares solution itself. The same fit is applied to
    # both channels, enforcing a common reference phase and time origin.
    weighted = design * window[:, None]
    gram = design.T @ weighted
    if np.linalg.cond(gram) > 1e9:
        raise ValueError('The tones or products are too close for this capture length.')
    coefficients = np.linalg.solve(gram, weighted.T @ values)
    cosine, sine = np.split(coefficients[1:], 2)
    return cosine - 1j * sine, values - design @ coefficients, window


def _rms(values, weights):
    return np.sqrt(np.average(values ** 2, axis=0, weights=weights))


def _residual_noise(values, window, rate):
    # An audio-band residual avoids interpreting out-of-band recorder bias or
    # interface ultrasonic noise as tracking distortion. Use Hann energy
    # normalization to preserve RMS units even when the tone is off bin.
    power = np.abs(fft.rfft(values * window[:, None], axis=0)) ** 2
    bins = fft.rfftfreq(len(values), 1 / rate)
    valid = (bins >= 20) & (bins <= 20_000) & (bins < rate / 2)
    return np.sqrt(2 * power[valid].sum(axis=0) / (len(values) * np.sum(window ** 2)))


def tone_reading(samples, rate, target_hz, *, tolerance=0.035):
    """Measure a specified fundamental, its harmonics, and the audio residual."""
    values = _frames(samples)
    power, resolution = _fft_peaks(values, rate)
    frequency = _frequency_near(power, resolution, target_hz, tolerance)
    cap = min(rate / 2 - resolution, 20_000)
    if frequency > cap + resolution / 2:
        raise ValueError('Choose a tone within the 20 kHz analysis band.')

    # Follow changes in the reference tone on short overlapping windows so
    # ordinary record-speed variation contributes less to the residual.
    # Pool measured powers so a brief burst remains visible in the results.
    width = min(len(values), max(round(rate * 0.14), round(18 * rate / frequency)))
    starts = np.unique(np.r_[np.arange(0, len(values) - width + 1, max(1, width // 2)), len(values) - width])
    powers, residuals = [], []
    harmonic_count = min(10, max(1, int(cap / (frequency * (1 + tolerance)))))
    for offset in starts:
        part = values[offset:offset + width]
        spectrum, spacing = _fft_peaks(part, rate)
        local = _frequency_near(spectrum, spacing, frequency, tolerance)
        phasors, residual, window = _fit(part, rate, np.arange(1, harmonic_count + 1) * local)
        powers.append(np.abs(phasors) ** 2 / 2)
        residuals.append(_residual_noise(residual, window, rate) ** 2)
    tone_powers = np.mean(powers, axis=0)
    fundamental = np.sqrt(tone_powers[0])
    noise = np.sqrt(np.mean(residuals, axis=0))
    harmonic = np.sqrt(np.sum(tone_powers[1:], axis=0))
    divisor = np.maximum(fundamental, 1e-15)
    channels = tuple(ChannelReading(
        rms, peak, level_db(fundamental[i]), float(100 * harmonic[i] / divisor[i]),
        float(100 * np.hypot(harmonic[i], noise[i]) / divisor[i]),
        float(100 * sqrt(tone_powers[2, i]) / divisor[i]) if harmonic_count >= 3 else None,
        level_db(fundamental[i]) - level_db(noise[i]),
    ) for i, (rms, peak) in enumerate(levels(values)))
    return ToneReading(frequency, channels)


def azimuth_reading(samples, rate, target_hz, *, tolerance=0.035):
    """Return right-channel delay relative to left, modulo one test-tone cycle."""
    values = _frames(samples)
    if values.shape[1] != 2:
        raise ValueError('Select a stereo input for a tape-head azimuth measurement.')
    if not 0 < target_hz < rate / 2 or not 0 < tolerance <= .15:
        raise ValueError('Choose a reference tone inside the audio capture bandwidth.')
    clipped = bool(np.max(np.abs(values)) >= 0.995)
    power, resolution = _fft_peaks(values, rate)
    _frequency_near(power, resolution, target_hz, tolerance)
    # A fixed sinusoid fit mistakes common speed modulation for noise. Follow
    # the phase of the entire narrow-band carrier, including its FM sidebands.
    # Apply identical zero-phase filters so the measurement adds no delay.
    width = min(max(1.45 * tolerance * target_hz, 8 * resolution), .43 * target_hz)
    low = target_hz - width
    high = min(rate / 2 - resolution, target_hz + width)
    if high <= low or len(values) < rate / 12:
        raise ValueError('Wait for a complete test-tone observation.')
    values = values - np.mean(values, axis=0)
    sos = signal.butter(6, (low, high), btype='bandpass', output='sos', fs=rate)
    band = signal.sosfiltfilt(sos, values, axis=0)
    pilot = signal.hilbert(band, axis=0)
    # Depending on the carrier/filter bandwidth, leave out the start and end
    # where the forward/backward filter and analytic transform are unsettled.
    edge = min(len(values) // 5, max(round(rate * .016), round(4 * rate / (high - low))))
    pilot = pilot[edge:-edge]
    values = values[edge:-edge]
    window = np.hanning(len(pilot))
    product = pilot[:, 0] * np.conj(pilot[:, 1])
    weights = window * np.abs(product)
    if sum(weights) <= 1e-20:
        raise ValueError('Waiting for a common test tone in both channels.')
    mean_cross = np.dot(window, product) / np.sum(weights)
    phase = float(np.angle(mean_cross))
    # The derivatives from both channels estimate the same physical carrier.
    # Weight them like the relative-phase estimate so AM or asymmetric dropout
    # cannot favour one part of the observation in only one of the two values.
    increments = np.angle(pilot[1:] * np.conj(pilot[:-1]))
    frequency = float(np.average(increments.mean(axis=1), weights=np.sqrt(weights[1:] * weights[:-1]))
                      * rate / (2 * pi))
    if not frequency > 0 or abs(frequency / target_hz - 1) > tolerance:
        raise ValueError('Waiting for a common tone near the specified reference frequency.')
    delay_scale = 1e6 / (2 * pi * frequency)
    # Treat common frequency excursions as a real change in phase for a fixed
    # delay. Convert each short block to the same carrier frequency before
    # summarizing repeatability and preserve the original cycle ambiguity.
    sections = np.array_split(np.arange(len(pilot) - 1), min(24, max(1, round(len(pilot) / (rate * .035)))))
    deviations, confidence_weights = [], []
    for block in sections:
        weight = weights[block + 1]
        if not np.any(weight):
            continue
        local_frequency = np.average(increments[block].mean(axis=1), weights=weight) * rate / (2 * pi)
        expected_phase = phase * local_frequency / frequency
        local_cross = np.dot(window[block + 1], product[block + 1])
        deviations.append(np.angle(local_cross * np.exp(-1j * expected_phase)))
        confidence_weights.append(np.sum(weight))
    tone_rms = np.sqrt(np.average(np.abs(pilot) ** 2, axis=0, weights=window) / 2)
    noise_rms = _rms(values - pilot.real, window)
    tone_levels = tuple(level_db(level) for level in tone_rms)
    confidence = tuple(level - level_db(noise) for level, noise in zip(tone_levels, noise_rms, strict=True))
    jitter = sqrt(np.average(np.square(deviations), weights=confidence_weights)) * delay_scale
    return AzimuthReading(frequency, float(np.degrees(phase)), phase * delay_scale, tone_levels, confidence,
                          float(abs(mean_cross)), jitter, clipped)


def unwrap_delay(delay_us, offset_us, frequency):
    """Remove the interface offset without mistaking a phase wrap for head movement."""
    period = 1e6 / frequency
    return (delay_us - offset_us + period / 2) % period - period / 2


def resolve_azimuth_delay(fine_us, fine_hz, fine_margin_us, coarse_us, coarse_hz, coarse_margin_us):
    """Select a fine-tone cycle only when the two observations support the same delay.

    The margins are conservative repeatability bounds, not confidence intervals.
    A lower tone still has its own phase wrap, so the result assumes the physical
    delay is inside half of that tone's period.
    """
    if not 0 < coarse_hz < fine_hz:
        return None
    period = 1e6 / fine_hz
    coarse_half_period = 5e5 / coarse_hz
    margin = fine_margin_us + coarse_margin_us
    if abs(coarse_us) + coarse_margin_us >= coarse_half_period or margin >= period / 2:
        return None
    resolved = fine_us + round((coarse_us - fine_us) / period) * period
    if abs(resolved - coarse_us) > margin or abs(resolved) + fine_margin_us >= coarse_half_period:
        return None
    return resolved


def two_tone_reading(samples, rate, first_hz, second_hz, method='sidebands', *, tolerance=0.03):
    """Read recorded intermodulation products from a known two-tone test record.

    Sidebands are the first two pairs around the upper tone. Difference mode
    uses the fundamental difference and the two third-order nearby products.
    Both measurements express the RMS sum relative to the upper test tone.
    """
    values = _frames(samples)
    first, second = sorted((first_hz, second_hz))
    if first <= 0 or second - first < max(first, second) * tolerance:
        raise ValueError('Choose distinct test tones separated by more than the search tolerance.')
    power, resolution = _fft_peaks(values, rate)
    low = _frequency_near(power, resolution, first, tolerance)
    high = _frequency_near(power, resolution, second, tolerance)
    if method == 'sidebands':
        targets = (high - 2 * low, high - low, high + low, high + 2 * low)
    elif method == 'difference':
        # A real signal folds the lower third-order product through DC when
        # the test tones are more than an octave apart.
        targets = (high - low, abs(2 * low - high), 2 * high - low)
    else:
        raise ValueError('Choose a supported intermodulation measurement method.')
    # Harmonics do not count as intermodulation. Accept only resolvable
    # products distinct from the excitation, DC, and low-order harmonics.
    occupied = (low, high, 2 * low, 3 * low, 2 * high, 3 * high)
    products = tuple(target for target in targets
                     if 20 <= target <= rate / 2 - 8 * resolution
                     and all(abs(target - tone) > 8 * resolution for tone in occupied if tone > 0))
    if len(products) != len(targets) or min(np.diff(sorted(products))) <= 8 * resolution:
        raise ValueError('These tones produce overlapping or out-of-band products. Check the test record and method.')
    fit_frequencies = tuple(frequency for frequency in occupied if frequency < rate / 2 - 8 * resolution)
    phasors, _, _ = _fit(values, rate, (*fit_frequencies, *products))
    bottom, top = np.abs(phasors[:2]) / sqrt(2)
    strongest = np.maximum(bottom, top)
    if np.any(np.minimum(bottom, top) < np.maximum(10 ** (-76 / 20), strongest * 0.006)):
        raise ValueError('Waiting for both test tones on the input channels.')
    distortion = 100 * np.sqrt(np.sum(np.abs(phasors[-len(products):]) ** 2, axis=0) / 2) / top
    return TwoToneReading((low, high), tuple(map(float, distortion)), tuple(level_db(value) for value in top), products)


def band_curve(samples, rate, low_hz=40.0, high_hz=20_000.0):
    """Unweighted one-third octave AC levels for a broadband verification track."""
    values = _frames(samples)
    # Use fixed centre frequencies so captures can be compared directly across
    # devices, durations and sampling rates. Each channel stays independent.
    centers = 1000 * 2 ** (np.arange(-18, 15) / 3)
    centers = centers[(centers >= low_hz) & (centers <= min(high_hz, rate / 2 / (2 ** (1 / 6))))]
    result = []
    for column in values.T:
        spectrum = power_spectrum(column, rate, remove_dc=True)
        result.append(np.array([level_db(sqrt(spectrum.power[spectrum.band(
            frequency / (2 ** (1 / 6)), frequency * (2 ** (1 / 6)))].sum())) for frequency in centers]))
    return centers, np.asarray(result)


def compare_distortion(left, right, limit, *, balance=False):
    """Rank valid alternatives and avoid favoring two equally distorted channels."""
    worst = max(left, right)
    score = abs(left - right) + 0.1 * worst if balance else worst
    return score if worst <= limit else None
