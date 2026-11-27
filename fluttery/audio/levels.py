"""Reference-calibrated voltage, A-weighted noise and harmonic metering."""

from dataclasses import dataclass
from math import isfinite, log10, sqrt

import numpy as np
from scipy import signal

from .spectrum import (
    FLOOR_DB,
    _distortion_window,
    _fit_basis,
    _refine_peak,
    _sample_angles,
    power_spectrum,
)

LEVEL_BANDS = {
    'audio': ('Audio (20 Hz–20 kHz)', 20.0, 20000.0),
    'wide': ('Wideband AC', 0.0, 192000.0),
    'low': ('Hum and rumble (20–300 Hz)', 20.0, 300.0),
    'voice': ('Voice band (300 Hz–3.4 kHz)', 300.0, 3400.0),
    'custom': ('Custom', 20.0, 20000.0),
}
VOLTAGE_UNITS = {'auto': 'Automatic', 'volts': 'Volts RMS', 'millivolts': 'Millivolts RMS', 'dbfs': 'dBFS RMS'}
LEVEL_MODES = ('voltage', 'calibration', 'snr')


def default_level_options(mode):
    return {'band': 'audio', 'lowHz': 20.0, 'highHz': 20000.0, 'window': 2.0 if mode == 'snr' else 1.0, 'units': 'auto'}


def normalize_bench_level_options(values):
    """Validate saved settings independently for each bench instrument."""
    if not isinstance(values, dict):
        return {}
    result = {}
    for mode in LEVEL_MODES:
        raw = values.get(mode)
        if not isinstance(raw, dict):
            continue
        default = default_level_options(mode)
        band = raw.get('band')
        band = band if isinstance(band, str) and band in LEVEL_BANDS else default['band']
        _, low, high = LEVEL_BANDS[band]
        if band == 'custom':
            try:
                custom_low, custom_high = float(raw['lowHz']), float(raw['highHz'])
            except (KeyError, TypeError, ValueError, OverflowError):
                pass
            else:
                if 0 <= custom_low < custom_high <= 192000:
                    low, high = custom_low, custom_high
        try:
            window = float(raw.get('window', default['window']))
        except (TypeError, ValueError, OverflowError):
            window = default['window']
        if not .25 <= window <= 5:
            window = default['window']
        units = raw.get('units')
        units = units if mode == 'voltage' and isinstance(units, str) and units in VOLTAGE_UNITS else 'auto'
        result[mode] = {'band': band, 'lowHz': low, 'highHz': high, 'window': window, 'units': units}
    return result


def a_weighting_power(frequency):
    """Analogue A-weighting magnitude squared, normalised at 1 kHz."""
    frequency = np.asarray(frequency, dtype=np.float64)
    squares = frequency ** 2
    a, b, c, d = np.square([20.598997, 107.65265, 737.86223, 12194.217])

    def magnitude(value):
        return d * value ** 2 / ((value + a) * np.sqrt((value + b) * (value + c)) * (value + d))

    return np.square(magnitude(squares) / magnitude(1000.0 ** 2))


def amplitude_db(amplitude, reference=1.0):
    return 20 * log10(amplitude / reference) if amplitude > 0 else FLOOR_DB


def ac_levels(frames, rate, low=20, high=20000, *, observation_frames=None):
    """Measure RMS and true sample peak after removing DC and applying the chosen band."""
    values = np.asarray(frames, dtype=np.float64)
    if values.ndim == 1:
        values = values[:, np.newaxis]
    if values.ndim != 2 or not values.size or not np.isfinite(values).all():
        raise ValueError('Wait for a complete observation window of finite audio.')
    if not isfinite(rate) or not 0 <= low < min(high, rate / 2):
        raise ValueError('Choose an increasing frequency band below half the input sample rate.')
    length = len(values) if observation_frames is None else observation_frames
    if not 1 <= length <= len(values):
        raise ValueError('Wait for a complete observation window of finite audio.')
    observed = values[-length:]
    filtered = values - np.mean(observed, axis=0)
    if low > 0 or high < rate / 2:
        edges, mode = ((low, high), 'bandpass') if low and high < rate / 2 else (
            (low, 'highpass') if low else (high, 'lowpass'))
        sos = signal.butter(4, edges, btype=mode, fs=rate, output='sos')
        # Warm the causal filter with the real preceding audio when present.
        # Reflect at the buffer boundary only when the route has just opened.
        warmup = min(len(values) - 1, int(rate))
        if missing := max(0, warmup - (len(values) - length)):
            filtered = np.pad(filtered, ((missing, 0), (0, 0)), mode='reflect', reflect_type='odd')
        try:
            initial = signal.sosfilt_zi(sos)[:, :, None] * filtered[0]
            filtered, _ = signal.sosfilt(sos, filtered, axis=0, zi=initial)
        except np.linalg.LinAlgError as error:
            raise ValueError('The measurement band cannot be resolved at this sample rate.') from error
    filtered = filtered[-length:]
    if not np.isfinite(filtered).all():
        raise ValueError('The measurement band cannot be resolved at this sample rate.')
    return (np.sqrt(np.mean(filtered ** 2, axis=0)), np.max(np.abs(filtered), axis=0),
            np.max(np.abs(observed), axis=0))


@dataclass(frozen=True)
class NoiseReading:
    rms: float
    weighted_rms: float
    frequency: np.ndarray
    density_db: np.ndarray
    weighted_density_db: np.ndarray


def _band_spectrum(channel, rate, low, high):
    """Integrate the fraction of each one-sided bin that lies in the band."""
    spectrum = power_spectrum(channel, rate, remove_dc=True)
    frequencies = spectrum.frequencies[1:]
    half_bin = spectrum.bin_width / 2
    start = frequencies - half_bin
    end = np.minimum(frequencies + half_bin, rate / 2)
    overlap = np.minimum(end, high) - np.maximum(start, low)
    selected = overlap > 0
    widths = overlap[selected]
    if not len(widths):
        raise ValueError('Use a longer observation window to resolve the selected bandwidth.')
    power = spectrum.power[1:][selected] * widths / (end[selected] - start[selected])
    return frequencies[selected], power, widths


def _spectral_frames(frames, rate, low, high):
    values = np.asarray(frames, dtype=np.float64)
    if values.ndim == 1:
        values = values[:, np.newaxis]
    if not isfinite(rate) or not 0 <= low < min(high, rate / 2):
        raise ValueError('Choose an increasing frequency band below half the input sample rate.')
    if values.ndim != 2 or len(values) < max(64, rate / 8) or not np.isfinite(values).all():
        raise ValueError('Capture at least one eighth of a second of finite audio.')
    return values


def band_rms(frames, rate, low, high):
    """Measure the SNR reference with the same band integration as the noise."""
    values = _spectral_frames(frames, rate, low, high)
    return np.array([sqrt(float(_band_spectrum(column, rate, low, high)[1].sum())) for column in values.T])


def measure_noise(frames, rate, low=20, high=20000):
    """Integrate noise RMS and retain a readable logarithmic density plot."""
    arrays = _spectral_frames(frames, rate, low, high)
    results = []
    for channel in arrays.T:
        bins, powers, bandwidths = _band_spectrum(channel, rate, low, high)
        weighted = a_weighting_power(bins) * powers
        # Group FFT bins into twelfth-octave bands for the display only. Use
        # the full-resolution bins above to compute the actual noise readings.
        octave_bins = np.floor(12 * np.log2(bins / bins[0])).astype(int)
        counts = np.bincount(octave_bins)
        valid = counts > 0
        bandwidth = np.bincount(octave_bins, weights=bandwidths)[valid]
        frequencies = np.exp(np.bincount(octave_bins, weights=np.log(bins))[valid] / counts[valid])

        density = [10 * np.log10(np.maximum(np.bincount(octave_bins, weights=power)[valid] / bandwidth, 1e-20))
                   for power in (powers, weighted)]
        results.append(NoiseReading(sqrt(float(powers.sum())), sqrt(float(weighted.sum())), frequencies, *density))
    return results


def noise_levels(frames, rate, low=20, high=20000):
    """RMS noise in digital units after applying the band and A weighting."""
    return [(reading.rms, reading.weighted_rms) for reading in measure_noise(frames, rate, low, high)]


def calibration_factor(reference_vrms, measured, sample_peak):
    if not isfinite(reference_vrms) or not 0 < reference_vrms <= 1000:
        raise ValueError('Enter the positive RMS voltage measured by your reference meter.')
    if not isfinite(sample_peak) or sample_peak >= 0.985:
        raise ValueError('The interface input is too close to clipping. Reduce its gain and capture again.')
    if not isfinite(measured) or measured < 0.0001:
        raise ValueError('Input level is too low for useful voltage calibration.')
    return reference_vrms / measured


def voltage_reference(samples, rate, low, high):
    """Fit the physical reference before a measurement filter can change its level."""
    values, = _spectral_frames(samples, rate, low, high).T
    spectrum = power_spectrum(values, rate, remove_dc=True)
    spacing = spectrum.bin_width
    upper = min(high, rate / 2 - 4 * spacing)
    # Search beyond the chosen edges and check the refined frequency. This
    # permits a valid tone at a cutoff but won't mistake a lobe for a tone.
    initial, _ = spectrum.resonant_peak(max(12 * spacing, low - 2 * spacing), upper + 2 * spacing)
    error = 'Use a steady sine within the selected band. Use a longer window for low frequencies.'
    if not initial:
        raise ValueError(error)
    positions = _sample_angles(len(values))
    window, _ = _distortion_window(len(values))
    frequency = _refine_peak(values, initial, spacing, positions, window)
    if not max(12 * spacing, low - spacing * .001) <= frequency <= upper + spacing * .001:
        raise ValueError(error)

    # Fit offset and both quadratures on four independent parts. A changing
    # level or a second source must not become a permanent gain correction.
    phases = (frequency / spacing) * positions
    basis = np.stack((np.cos(phases), np.sin(phases), np.ones(len(phases))))
    levels = []
    for model, part in zip(np.array_split(basis, 4, axis=1), np.array_split(values, 4), strict=True):
        coefficients, residual = _fit_basis(part, model, np.ones(len(part)))
        rms = float(np.hypot(*coefficients[:2]) / sqrt(2))
        if rms < .0001 or np.linalg.norm(residual) / sqrt(len(part)) > .02 * rms:
            raise ValueError(error)
        levels.append(rms)
    measured = float(np.mean(levels))
    if np.ptp(levels) > .01 * measured:
        raise ValueError('The reference level changed during the observation. Wait for a steady signal.')
    return measured, frequency


def normalize_voltage_calibrations(values):
    if not isinstance(values, list):
        return []
    normalized = []
    devices = set()
    for value in values[:160]:
        if not isinstance(value, dict) or not isinstance(value.get('device'), str):
            continue
        device = value['device'][:600]
        try:
            rate = int(value['rate'])
            left = float(value['left']) if value.get('left') is not None else None
            right = float(value['right']) if value.get('right') is not None else None
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if not 8000 <= rate <= 384000 or (device, rate) in devices:
            continue
        if (left is None and right is None or
                any(factor is not None and not 1e-6 <= factor <= 1e7 for factor in (left, right))):
            continue
        normalized.append({'device': device, 'rate': rate, 'left': left, 'right': right})
        devices.add((device, rate))
    return normalized[:80]
