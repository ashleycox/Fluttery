"""Reference speeds and two-pass stereo separation measurements."""

from dataclasses import dataclass
from itertools import pairwise
from math import ceil, log10

import numpy as np
from scipy import fft, signal

PLAYBACK_SPEEDS = (33, 45, 78, 50 / 3, 0)
SPEED_LABELS = ('33⅓ RPM', '45 RPM', '78 RPM', '16⅔ RPM', 'Custom')


def nominal_speed(selection, reference_78=78.26, custom=33 + 1 / 3):
    """Map saved selector values to the exact actual reference in RPM."""
    return {0: custom, 33: 100 / 3, 78: reference_78}.get(selection, selection)


def display_rpm(speed):
    text = {100 / 3: '33⅓', 50 / 3: '16⅔'}.get(speed, f'{speed:.6g}')
    return f'{text} RPM'


def automatic_record_reference(detected, record_rpm, playback_rpm):
    """Resolve the common carrier frequencies only when the choice is clear."""
    if min(detected, record_rpm, playback_rpm) <= 0 or not np.isfinite((detected, record_rpm, playback_rpm)).all():
        return None
    # Undo an intentional difference between the record's cutting speed and
    # the target playback speed before recognizing its nominal test tone.
    at_record_speed = detected * record_rpm / playback_rpm
    matches = [tone for tone in (3000.0, 3150.0) if abs(at_record_speed / tone - 1) <= 0.019]
    return matches[0] if len(matches) == 1 else None


@dataclass(frozen=True, slots=True)
class SeparationReading:
    direction: str
    frequency: float
    driven_dbfs: float
    leakage_dbfs: float
    noise_dbfs: float
    resolution_dbfs: float
    separation_db: float
    phase_deg: float | None
    limited: bool


def _amplitude_db(value):
    return 20 * log10(max(float(value), 1e-15))


def _pilot_frequency(driven, rate, expected_hz):
    # Padded FFT plus interpolated log power gives a narrow band centre; the
    # separation estimate fits the time-varying carrier measured in that band.
    length = fft.next_fast_len(4 * len(driven), real=True)
    powers = np.abs(fft.rfft(driven * np.hanning(len(driven)), n=length)) ** 2
    spacing = rate / length
    low, high = (max(15.0, expected_hz * 0.95), min(expected_hz * 1.05, rate * 0.46, 20_000)) if expected_hz else (
        15.0, min(rate * 0.46, 20_000),
    )
    first, last = max(1, ceil(low / spacing)), min(len(powers) - 2, int(high / spacing))
    if first >= last:
        raise ValueError('The selected reference tone is outside the capture bandwidth.')
    index = first + int(np.argmax(powers[first:last + 1]))
    a, b, c = np.log(np.maximum(powers[index - 1:index + 2], 1e-300))
    curvature = a - 2 * b + c
    fraction = np.clip(0.5 * (a - c) / curvature, -0.5, 0.5) if curvature < 0 < powers[index] else 0
    return float((index + fraction) * spacing)


def stereo_separation(samples, rate, channel, *, expected_hz=0, trims_db=(0.0, 0.0)):
    """Measure coherent leakage for one known driven channel.

    A complex regression follows the test tone present in the driven channel.
    Thus ordinary speed modulation in a tape or test record is shared by the
    pilot and its leakage instead of being counted as independent residual.
    The reported lower limit combines the finite-window noise floor and the
    repeatability of the complex fit across independent sections.
    """
    values = np.asarray(samples, dtype=np.float64)
    if values.ndim != 2 or values.shape[1] != 2 or channel not in (0, 1):
        raise ValueError('Select two input channels before measuring stereo separation.')
    if not np.isfinite(rate) or rate <= 0 or len(values) < max(64, .5 * rate):
        raise ValueError('Capture at least half a second of the channel-only test tone.')
    if not np.isfinite(values).all() or np.max(np.abs(values)) >= 0.995:
        raise ValueError('The capture contains invalid samples or clipping. Lower the input gain and repeat.')
    if not np.isfinite(expected_hz) or expected_hz < 0:
        raise ValueError('Enter a positive reference frequency or choose automatic tone detection.')
    trims = np.asarray(trims_db, dtype=np.float64)
    if trims.shape != (2,) or not np.isfinite(trims).all():
        raise ValueError('Both channel calibrations must have finite gain values.')

    values = (values - np.mean(values, axis=0)) * np.power(10.0, trims / 20)
    driver = values[:, channel]
    frequency = _pilot_frequency(driver, rate, expected_hz)
    if not frequency or np.std(driver) < 10 ** (-70 / 20):
        raise ValueError('No usable tone on the selected driven channel.')
    half_band = max(22.0, frequency * 0.045)
    low, high = max(5.0, frequency - half_band), min(rate * 0.485, frequency + half_band)
    sos = signal.butter(6, (low, high), btype='bandpass', output='sos', fs=rate)
    band = signal.sosfiltfilt(sos, values, axis=0)
    analytic = signal.hilbert(band, axis=0)
    # Limit the fit to the settled part of the zero-phase band filter.
    edge = min(round(.14 * rate), max(round(.025 * rate), round(4 * rate / (high - low))))
    if 2 * edge >= len(values) - 32:
        raise ValueError('Capture more audio at this test frequency.')
    analytic = analytic[edge:-edge]
    values = values[edge:-edge]
    pilot, return_tone = analytic[:, channel], analytic[:, 1 - channel]
    weights = np.hanning(len(pilot))
    power = float(np.sum(weights * np.abs(pilot) ** 2))
    if power <= 1e-25:
        raise ValueError('No usable tone on the selected driven channel.')
    ratio = np.sum(weights * np.conj(pilot) * return_tone) / power
    driven_rms = np.sqrt(power / (2 * np.sum(weights)))
    residual_driver = np.sqrt(np.average((values[:, channel] - pilot.real) ** 2, weights=weights))
    residual_leak = np.sqrt(np.average((values[:, 1 - channel] - (ratio * pilot).real) ** 2, weights=weights))
    if driven_rms < max(10 ** (-65 / 20), residual_driver * 5):
        raise ValueError('The driven channel must carry a stable test tone above the noise and distortion.')
    if abs(ratio) > 10 ** (-6 / 20):
        raise ValueError('Both channels contain a strong tone. Play the selected channel-only track and repeat.')
    # The initial spectrum only locates the pilot's band. Average the actual
    # analytic phase advance within the fitted segment to read the carrier,
    # including records whose strongest FFT peak is a modulation sideband.
    phase_product = pilot[1:] * np.conj(pilot[:-1])
    frequency = float(np.average(np.angle(phase_product), weights=weights[1:] * np.abs(phase_product))
                      * rate / (2 * np.pi))

    windowed = []
    sections = np.linspace(0, len(pilot), 9, dtype=int)
    for start, stop in pairwise(sections):
        reference = pilot[start:stop]
        energy = float(np.vdot(reference, reference).real)
        if energy > 1e-25:
            windowed.append(np.vdot(reference, return_tone[start:stop]) / energy)
    repeatability = float(np.std(windowed, ddof=1) / np.sqrt(len(windowed))) if len(windowed) > 1 else 0.0
    residual_pilot_band = np.sqrt(np.average(np.abs(return_tone - ratio * pilot) ** 2, weights=weights))
    duration = len(pilot) / rate
    # Hann energy reduces the effective observation by the equivalent noise
    # bandwidth factor of 1.5. Use a conservative three-standard-error limit.
    floor = max(3 * repeatability,
                3 * residual_pilot_band / np.sqrt(2 * power / np.sum(weights) * max(1.0, (high - low) * duration / 1.5)),
                1e-8)
    magnitude = abs(ratio)
    limited = magnitude <= floor
    return SeparationReading(
        'Left to right' if channel == 0 else 'Right to left', frequency, _amplitude_db(driven_rms),
        _amplitude_db(driven_rms * magnitude), _amplitude_db(residual_leak), _amplitude_db(driven_rms * floor),
        -_amplitude_db(max(magnitude, floor)), None if limited else float(np.angle(ratio, deg=True)), limited,
    )
