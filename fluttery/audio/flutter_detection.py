"""Frequency weighting and timed readouts for the wow and flutter meter."""

from collections import deque
from functools import lru_cache
from math import pi

import numpy as np
from scipy import optimize, signal

from .spectrum import power_spectrum

AES_CARRIER = 3150.0
AES_WINDOW = 5.0
SCRAPE_CARRIER = 12_500.0
SCRAPE_MIN_RATE = 44_100
SCRAPE_BAND = (200.0, 5000.0)
DRIFT_BAND = (0.05, 0.5)
DRIFT_WINDOW = 20.0


@lru_cache(maxsize=16)
def reference_weighting(sample_rate):
    """Design the 4 Hz-normalized weighting shared by IEC/DIN and AES6."""
    # Fit a third-order high-pass, first-order low-pass and one shelving
    # section to the nominal CCIR 409 curve. This models the additional
    # low-frequency rejection and the broad top, which a single band-pass
    # cannot do. The digital response is within 0.44 dB of the table points
    # from 0.2 to 200 Hz at supported analysis sample rates.
    # https://search.itu.int/history/HistoryDigitalCollectionDocLibrary/4.276.43.en.1005.pdf
    # https://aes.org/publications/standards-store/?id=15
    high_pole = 2 * pi * 0.441438
    damping = high_pole / 0.680693
    poles = [*np.roots((1, damping, high_pole**2)), -2 * pi * 1.560835,
             -2 * pi * 40.0, -2 * pi * 8.669942]
    zeros = [0, 0, 0, -2 * pi * 29.169300]
    digital = signal.bilinear_zpk(zeros, poles, 1.0, fs=sample_rate)
    coefficients = signal.zpk2sos(*digital)
    _, response = signal.sosfreqz(coefficients, worN=(4.0,), fs=sample_rate)
    coefficients[0, :3] /= abs(response[0])
    return coefficients


def statistical_peak(samples):
    """Magnitude exceeded for 5% of the valid elapsed time, in speed percent."""
    return float(np.quantile(np.abs(samples), 0.95))


class SlidingRms:
    """Rectangular RMS window with incremental power accounting, per channel."""

    def __init__(self, count, channel_count):
        self.power = np.zeros((count, channel_count))
        self.total = np.zeros(channel_count)
        self.index = 0
        self.count = 0

    def reset(self):
        self.power.fill(0)
        self.total.fill(0)
        self.index = self.count = 0

    def add(self, samples):
        capacity = len(self.power)
        incoming = np.square(samples[-capacity:])
        count = len(incoming)
        end = min(capacity, self.index + count)
        first = end - self.index
        first_part = self.power[self.index:end]
        tail = self.power[:count - first]
        self.total += incoming.sum(axis=0) - first_part.sum(axis=0) - tail.sum(axis=0)
        first_part[:] = incoming[:first]
        tail[:] = incoming[first:]
        self.index = (self.index + count) % capacity
        self.count = min(capacity, self.count + count)

    def values(self):
        if self.count == len(self.power):
            return np.sqrt(np.maximum(self.total, 0) / self.count)
        return (None,) * len(self.total)


class RollingMaxima:
    """Ten-second maxima of the live meter's timestamped readings."""

    def __init__(self):
        self.rms = deque()
        self.peak = deque()

    def reset(self):
        self.rms.clear()
        self.peak.clear()

    @staticmethod
    def _push(window, timestamp, value):
        while window and window[0][0] <= timestamp - 10:
            window.popleft()
        if value is not None:
            while window and window[-1][1] <= value:
                window.pop()
            window.append((timestamp, value))
        return window[0][1] if window else None

    def add(self, timestamp, rms, peak):
        return self._push(self.rms, timestamp, rms), self._push(self.peak, timestamp, peak)


def drift_band_rms(samples, sample_rate):
    """Measure low-band power, assigning resolved edge tones by fitted frequency."""
    # The drift band ends at 0.5 Hz. Reducing the rate makes nonlinear
    # frequency refinement inexpensive without treating FFT bins as tones.
    decimation = max(1, int(sample_rate / 12))
    values = signal.resample_poly(samples, 1, decimation, padtype='line').astype(np.float64)
    rate = sample_rate / decimation
    spacing = rate / len(values)
    time = (np.arange(len(values)) - (len(values) - 1) / 2) / rate
    baseline = np.column_stack((np.ones(len(time)), time * (2 * spacing)))
    values -= baseline @ np.linalg.lstsq(baseline, values, rcond=None)[0]
    power = 0.0
    for boundary in DRIFT_BAND:
        fitted = _drift_edge_tone(values, rate, boundary, time, baseline)
        if fitted is None:
            continue
        frequency, tone, tone_power = fitted
        values -= tone
        tolerance = .002 * spacing
        if DRIFT_BAND[0] - tolerance <= frequency <= DRIFT_BAND[1] + tolerance:
            power += tone_power
    values -= baseline @ np.linalg.lstsq(baseline, values, rcond=None)[0]
    spectrum = power_spectrum(values, rate, remove_dc=True)
    # A random residual has no precisely resolved lines. Fractional bin
    # coverage avoids gain steps as the observation grows by whole samples.
    edges = spectrum.frequencies - spacing / 2
    overlap = np.minimum(edges + spacing, DRIFT_BAND[1]) - np.maximum(edges, DRIFT_BAND[0])
    power += np.dot(spectrum.power, np.clip(overlap / spacing, 0, 1))
    return float(np.sqrt(power))


def _drift_edge_tone(values, rate, boundary, time, baseline):
    """Fit significant tones whose window lobes straddle a band edge."""
    spacing = rate / len(values)
    low = max(.62 * spacing, boundary - 1.8 * spacing)
    high = boundary + 1.8 * spacing
    # Hann weighting reduces the frequency pull from stronger out-of-band
    # tones. Fit the same baseline with each candidate so removing DC and
    # slope does not remove part of a tone near the lower band edge.
    weight = np.sin(pi * (np.arange(len(values)) + .5) / len(values))
    weighted_values = values * weight

    def fit(frequency):
        radians = 2 * pi * frequency * time
        carrier = np.column_stack((np.cos(radians), np.sin(radians)))
        design = np.column_stack((carrier, baseline))
        fitted = np.linalg.lstsq(design * weight[:, np.newaxis], weighted_values, rcond=None)[0]
        return carrier @ fitted[:2], float(np.dot(fitted[:2], fitted[:2]) / 2), values - design @ fitted

    def criterion(frequency):
        residual = weight * fit(frequency)[2]
        return np.dot(residual, residual)

    grid = np.linspace(low, high, 13)
    best = int(np.argmin([criterion(frequency) for frequency in grid]))
    result = optimize.minimize_scalar(criterion, method='bounded',
                                      bounds=(grid[max(0, best - 1)], grid[min(len(grid) - 1, best + 1)]),
                                      options={'xatol': spacing * 1e-5})
    frequency = result.x
    tone, power, residual = fit(frequency)
    spectrum = power_spectrum(residual, rate, remove_dc=True)
    noise = spectrum.band_power(frequency - 2 * spacing, frequency + 2 * spacing)
    if power > max(1e-10, 8 * noise) and low + .001 * spacing < frequency < high - .001 * spacing:
        return frequency, tone, power
    return None


def repeating_frequency(samples, sample_rate, low, high):
    """Estimate a sustained period and distinguish its resolved harmonics."""
    spectrum = power_spectrum(samples, sample_rate, remove_dc=True)
    search_low = max(low, 3 * spectrum.bin_width)
    frequency, _ = spectrum.resonant_peak(search_low, high)
    if not frequency:
        return None, None
    resolution = spectrum.bin_width
    peak_power = spectrum.band_power(frequency - 3.6 * resolution, frequency + 3.6 * resolution)
    band_power = spectrum.band_power(search_low, high)
    # A broad peak or an isolated outlier can identify a spectrum maximum
    # without establishing a repeating component. Require both prominence
    # and appreciable energy in the resolved main lobe.
    in_band = spectrum.band(search_low, high)
    neighbours = spectrum.power[in_band & (np.abs(spectrum.frequencies - frequency) > 4 * resolution)]
    background = float(np.median(neighbours)) if neighbours.size else 0.0
    if peak_power < max(1e-8, 35 * background, .12 * band_power):
        return None, None

    # A chirp or isolated burst can pass a spectrum-only check. Fit a common
    # oscillator on three non-overlapping time intervals. An ongoing repeat
    # retains its phase and a detectable component throughout the record.
    time = (np.arange(len(samples)) - len(samples) / 2) / sample_rate

    def sustained(candidate):
        phase = 2 * pi * candidate * time
        design = np.column_stack((np.cos(phase), np.sin(phase), np.ones(len(time))))
        phasors = []
        for rows, values in zip(np.array_split(design, 3), np.array_split(samples, 3), strict=True):
            cosine, sine, _ = np.linalg.lstsq(rows, values, rcond=None)[0]
            phasors.append(complex(cosine, sine))
        magnitude = np.abs(phasors)
        return (np.min(magnitude) > .32 * np.max(magnitude)
                and abs(sum(phasors)) > .94 * sum(magnitude))

    if not sustained(frequency):
        return None, None
    # A mechanical waveform with a strong second/third/fourth harmonic still
    # repeats at its fundamental. Only substitute a subharmonic that is
    # itself resolved, significant above the floor, and phase-stationary.
    for order in (4, 3, 2):
        harmonic = frequency / order
        if harmonic < search_low or abs(harmonic - frequency) < 5 * resolution:
            continue
        lower, db = spectrum.resonant_peak(max(search_low, harmonic - .15 * resolution),
                                           harmonic + .15 * resolution)
        if lower and 10 ** (db / 10) >= max(.05 * peak_power, 35 * background) and sustained(lower):
            frequency = lower
            break
    return frequency, 1.0 / frequency
