"""Power spectra and secondary measurements for the streaming audio engine."""

from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from scipy import signal

FLOOR_DB = -160.0


def level_db(amplitude):
    return float(20 * np.log10(max(float(amplitude), 1e-8)))


def power_db(power):
    return float(10 * np.log10(max(float(power), 1e-16)))


def amplitude_rms(samples):
    return float(np.sqrt(np.mean(np.square(samples, dtype=np.float64)))) if len(samples) else 0.0


def readonly(values):
    values.setflags(write=False)
    return values


@lru_cache(maxsize=8)
def _analysis_window(size):
    window = signal.windows.blackmanharris(size, sym=False)
    return window, size * float(np.square(window).sum())


@lru_cache(maxsize=8)
def _distortion_window(size):
    # A narrower taper than the visual spectrum keeps the fitted fundamental
    # and the noise beside it more distinct while still rejecting far-off tones.
    window = signal.windows.blackman(size, sym=False)
    return window, size * float(np.square(window).sum())


@lru_cache(maxsize=8)
def _sample_angles(size):
    # Centred positions in radians per FFT bin. Working in bin units keeps the
    # least-squares frequency gradient well scaled at every sample rate.
    return (np.arange(size) - (size - 1) / 2) * (2 * np.pi / size)


@dataclass(frozen=True)
class PowerSpectrum:
    frequencies: np.ndarray
    power: np.ndarray

    @property
    def bin_width(self):
        return self.frequencies[1] if len(self.frequencies) > 1 else 0.0

    def band(self, low, high):
        return (self.frequencies >= low) & (self.frequencies <= high)

    def band_power(self, low, high):
        return float(self.power[self.band(low, high)].sum())

    def peak(self, low, high):
        selected = np.flatnonzero(self.band(low, high))
        if not len(selected) or not np.any(self.power[selected] > 1e-16):
            return 0.0, FLOOR_DB
        index = selected[int(np.argmax(self.power[selected]))]
        return self._interpolated_frequency(index), power_db(self.power[index])

    def _interpolated_frequency(self, index):
        frequency = float(self.frequencies[index])
        if 0 < index < len(self.power) - 1:
            before, center, after = np.log(np.maximum(self.power[index - 1 : index + 2], 1e-16))
            divisor = before - 2 * center + after
            if divisor != 0:
                frequency += float(np.clip(0.5 * (before - after) / divisor, -0.5, 0.5) * self.bin_width)
        return frequency

    def resonant_peak(self, low, high):
        # A loud peak outside the band may have a shoulder inside it. Consider
        # only local maxima and test their interpolated centres against the band.
        half_bin = self.bin_width / 2
        neighbours = self.power[self.band(low - 8 * half_bin, high + 8 * half_bin)]
        # A nearby strong tone leaves Blackman–Harris sidelobes below −90 dB.
        # With an 80 dB floor relative to it, these cannot masquerade as peaks.
        floor = max(1e-16, 1e-8 * neighbours.max(initial=0))
        selected = np.flatnonzero(self.band(low - half_bin, high + half_bin))
        selected = selected[(selected > 0) & (selected < len(self.power) - 1)]
        powers = self.power[selected]
        selected = selected[(powers > floor) & (powers >= self.power[selected - 1])
                            & (powers > self.power[selected + 1])]
        tolerance = 0.01 * self.bin_width
        for index in selected[np.argsort(self.power[selected])[::-1]]:
            frequency = self._interpolated_frequency(index)
            if low - tolerance <= frequency <= high + tolerance:
                centre = min(high, max(low, frequency))
                # Measure the complete lobe. Clipping it at the search limits
                # would underread a valid resonance close to either cutoff.
                return centre, self.tone_db(frequency)
        return 0.0, FLOOR_DB

    def tone_db(self, tone_hz):
        if self.bin_width == 0 or tone_hz <= 0 or tone_hz > self.frequencies[-1]:
            return FLOOR_DB
        return power_db(self.band_power(tone_hz - 3.6 * self.bin_width, tone_hz + 3.6 * self.bin_width))

    def snapshot(self, low=0.0, high=float("inf")):
        selected = self.band(low, high)
        frequency, level = self.peak(max(low, self.bin_width), high)
        return {
            "frequencies": readonly(self.frequencies[selected]),
            "db": readonly(10 * np.log10(np.maximum(self.power[selected], 1e-16))),
            "peakFrequency": frequency,
            "peakDb": level,
        }


def power_spectrum(samples, sample_rate, *, remove_dc=False):
    """One-sided, energy-normalized periodogram, in full-scale RMS power."""
    if len(samples) < 16:
        return PowerSpectrum(np.empty(0), np.empty(0))
    window, normalization = _analysis_window(len(samples))
    if remove_dc:
        # Subtract the taper-weighted mean so the windowed signal has no DC
        # bin and a static input offset cannot leak into the rumble band.
        samples = samples - np.einsum("n,n->", samples, window) / window.sum()
    power = _periodogram(samples, window, normalization)
    frequencies = np.fft.rfftfreq(len(samples), 1.0 / sample_rate)
    return PowerSpectrum(frequencies, power)


def _periodogram(samples, window, normalization):
    transformed = np.fft.rfft(samples * window)
    power = np.square(np.abs(transformed)) / normalization
    power[1:] *= 2
    if len(samples) % 2 == 0:
        power[-1] /= 2
    return power


def _fit_basis(samples, basis, window):
    weighted = basis * window
    # Direct inner products avoid launching BLAS workers for these long,
    # narrow products on each refresh.
    gram = np.einsum("in,jn->ij", weighted, basis)
    projections = np.einsum("in,n...->i...", weighted, samples)
    amplitudes = np.linalg.solve(gram, projections)
    residual = samples - np.einsum("in,i...->n...", basis, amplitudes)
    return amplitudes, residual


def _harmonic_basis(phase, count):
    basis = np.ones((2 * count + 1, len(phase)))
    cosine, sine = basis[:count], basis[count:-1]
    np.cos(phase, out=cosine[0])
    np.sin(phase, out=sine[0])
    # Use angle addition for the other harmonics. The fit takes two
    # trigonometric evaluations per sample regardless of the harmonic count.
    for order in range(1, count):
        cosine[order] = cosine[order - 1] * cosine[0] - sine[order - 1] * sine[0]
        sine[order] = cosine[order - 1] * sine[0] + sine[order - 1] * cosine[0]
    return basis


def _fit_harmonics(samples, phase, count, window):
    """Fit all orders jointly while bounding the size of the design matrix."""
    block_frames = 32_768
    if len(samples) <= block_frames:
        return _fit_basis(samples, _harmonic_basis(phase, count), window)

    columns = 2 * count + 1
    gram = np.zeros((columns, columns))
    projections = np.zeros(columns)
    # Capture length, rate and harmonic count are independently configurable.
    # Accumulate the same weighted products as the short fit so a long capture
    # never holds a full-resolution design matrix or its weighted copy.
    for start in range(0, len(samples), block_frames):
        block = slice(start, start + block_frames)
        basis = _harmonic_basis(phase[block], count)
        weighted = basis * window[block]
        gram += np.einsum("in,jn->ij", weighted, basis)
        projections += np.einsum("in,n->i", weighted, samples[block])
    amplitudes = np.linalg.solve(gram, projections)
    residual = np.array(samples, dtype=np.float64, copy=True)
    for start in range(0, len(samples), block_frames):
        block = slice(start, start + block_frames)
        basis = _harmonic_basis(phase[block], count)
        residual[block] -= np.einsum("in,i->n", basis, amplitudes)
    return amplitudes, residual


def _refine_peak(samples, frequency, bin_width, positions, window):
    """Refine an FFT peak by locally fitting the phase slope of a sinusoid."""
    offset = frequency / bin_width
    basis = np.ones((3, len(samples)))
    cosine, sine = basis[:2]
    for _ in range(3):
        phase = offset * positions
        np.cos(phase, out=cosine)
        np.sin(phase, out=sine)
        # Fit the DC offset too so it cannot pull a low tone off frequency.
        amplitudes, residual = _fit_basis(samples, basis, window)
        slope = positions * (amplitudes[1] * cosine - amplitudes[0] * sine)
        sensitivity = float(np.sum(window * slope ** 2))
        if not sensitivity:
            break
        change = float(np.sum(window * residual * slope)) / sensitivity
        offset += np.clip(change, -0.5, 0.5)
        if abs(change) < 1e-8:
            break
    return float(offset * bin_width)


def fitted_stereo_levels(samples, frequency, bin_width):
    if not bin_width or not 2 <= frequency / bin_width <= (len(samples) - 1) / 2:
        return None
    window, _ = _distortion_window(len(samples))
    phase = frequency / bin_width * _sample_angles(len(samples))
    # Both channels are fitted at exactly the same frequency. Counting only
    # that coherent component keeps neighbouring interference out of ratios.
    coefficients, _ = _fit_basis(samples, _harmonic_basis(phase, 1), window)
    left, right = np.sum(coefficients[:2] ** 2, axis=0) / 2
    return power_db(left), power_db(right)


def distortion(samples, spectrum, sample_rate, min_frequency=20.0, max_frequency=20_000.0, harmonic_count=5,
               *, expected_frequency=None, check_clipping=True):
    default = {
        "available": False,
        "fundamentalFrequency": 0.0,
        "thd": 0.0,
        "thdn": 0.0,
        "hd3": None,
        "harmonicDbc": (),
        "noisePercent": 0.0,
        "signalDb": FLOOR_DB,
        "confidence": "No signal",
    }
    if not spectrum.bin_width or not len(samples):
        return default
    if check_clipping and np.max(np.abs(samples)) >= 0.9995:
        return {**default, "confidence": "Input clipped. Reduce the recording level.", "clipped": True}
    # Leave at least half a bin around Nyquist, where the sine and cosine
    # components become indistinguishable. Examine the nearest FFT bin at
    # either band edge and apply the exact range after sub-bin refinement.
    bin_width = spectrum.bin_width
    upper = min(max_frequency, sample_rate / 2 - bin_width / 2)
    if spectrum.power[0] > 1e-12:
        # A large DC offset broadens into the lowest few display bins. Remove
        # the weighted offset for the coarse search without changing the plot.
        spectrum = power_spectrum(samples, sample_rate, remove_dc=True)
    low_search, high_search = max(min_frequency - bin_width / 2, 2 * bin_width), upper + bin_width / 2
    if expected_frequency is not None:
        # With a generated stimulus, make the search local. An overloaded
        # device can return a harmonic louder than the expected fundamental.
        margin = max(4 * bin_width, 0.005 * expected_frequency)
        low_search = max(low_search, expected_frequency - margin)
        high_search = min(high_search, expected_frequency + margin)
    if high_search <= low_search:
        return default
    frequency, _ = spectrum.peak(low_search, high_search)
    if not 0 < frequency < sample_rate / 2 - bin_width / 4:
        return default

    level = spectrum.tone_db(frequency)
    if level < -90.0:
        return {
            **default,
            "fundamentalFrequency": frequency,
            "signalDb": level,
            "confidence": "Weak signal",
        }

    positions = _sample_angles(len(samples))
    window, normalization = _distortion_window(len(samples))
    try:
        frequency = _refine_peak(samples, frequency, bin_width, positions, window)
        tolerance = 1e-5 * bin_width
        if not min_frequency - tolerance <= frequency <= upper + tolerance:
            return default
        frequency = min(upper, max(min_frequency, frequency))
        count = max(1, min(harmonic_count, int(upper / frequency)))
        phase = frequency / bin_width * positions
        # Fit the actual harmonic frequencies and subtract those components
        # before integrating noise. This counts close-in interference that
        # broad FFT-bin masks would either include as THD or entirely hide.
        coefficients, residual = _fit_harmonics(samples, phase, count, window)
    except np.linalg.LinAlgError:
        return default  # Unresolvable phase near Nyquist or during startup.
    cosine, sine = np.split(coefficients[:-1], 2)
    tone_power = (cosine ** 2 + sine ** 2) / 2
    fundamental_power = float(tone_power[0])
    signal_db = power_db(fundamental_power)
    if signal_db < -90.0:
        return {**default, "fundamentalFrequency": frequency, "signalDb": signal_db, "confidence": "Weak signal"}
    noise_power = float(_periodogram(residual, window, normalization)[spectrum.band(min_frequency, max_frequency)].sum())
    harmonic_power = float(tone_power[1:].sum())
    thd = 100.0 * np.sqrt(harmonic_power / fundamental_power)
    thdn = 100.0 * np.sqrt((noise_power + harmonic_power) / fundamental_power)
    confidence = "Good signal"
    if signal_db < -55.0:
        confidence = "Weak signal"
    elif thdn >= 50.0:
        confidence = "Poor signal"
    return {
        "available": True,
        "fundamentalFrequency": frequency,
        "thd": float(thd) if count >= 2 else None,
        "thdn": float(thdn),
        "hd3": float(100 * np.sqrt(tone_power[2] / fundamental_power)) if count >= 3 else None,
        "harmonicDbc": tuple(power_db(component / fundamental_power) for component in tone_power),
        "noisePercent": float(100.0 * np.sqrt(noise_power / fundamental_power)),
        "signalDb": signal_db,
        "harmonicsMeasured": count - 1,
        "confidence": confidence,
    }
