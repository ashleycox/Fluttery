"""Phase-continuous signal generation and monitoring routes."""

from dataclasses import dataclass, replace
from fractions import Fraction
from math import pi, sin

import numpy as np
from scipy.signal import lfilter

from .settings import bounded

SHORT_HARMONIC_SERIES = 32
NOISE_WAVEFORMS = ('white', 'pink')


@dataclass(frozen=True)
class GeneratorSettings:
    enabled: bool = False
    frequency: float = 3000.0
    waveform: str = "sine"
    level: float = 0.25
    modulation_frequency: float = 0.0
    modulation_depth: float = 0.0
    pan: float = 0.0

    def effective_frequency(self, sample_rate):
        # Leave headroom for the top of the FM sweep as well as the carrier.
        modulation = self.modulation_depth / 100 if self.modulation_frequency else 0.0
        return min(self.frequency, 0.49 * sample_rate / (1 + modulation))

    def updated(self, enabled=None, **changes):
        waveform = changes.get("waveform", self.waveform)
        return replace(
            self,
            enabled=self.enabled if enabled is None else bool(enabled),
            waveform=waveform if waveform in ("sine", "square", "triangle", "sawtooth", *NOISE_WAVEFORMS) else "sine",
            frequency=bounded(changes.get("frequency"), 1, 24_000, self.frequency),
            level=bounded(changes.get("level"), 0, 1, self.level),
            modulation_frequency=bounded(
                changes.get("modulation_frequency"), 0, 100, self.modulation_frequency
            ),
            modulation_depth=bounded(changes.get("modulation_depth"), 0, 100, self.modulation_depth),
            pan=bounded(changes.get("pan"), -1, 1, self.pan),
        )


def polyblep(phase, step):
    """Soften waveform discontinuities over one sample on either side."""
    step = np.broadcast_to(step, phase.shape)
    correction = np.zeros(len(phase))
    beginning = phase < step
    position = phase[beginning] / step[beginning]
    correction[beginning] = 2 * position - position * position - 1
    ending = phase > 1 - step
    position = (phase[ending] - 1) / step[ending]
    correction[ending] = position * position + 2 * position + 1
    return correction


def polyblamp(phase, step):
    # Integrating the BLEP polynomial gives a cubic smoothing term for a
    # change in slope. A triangle has two of these per period, and no jumps.
    distance = np.minimum(phase, 1 - phase)
    fraction = np.divide(distance, step, out=np.ones_like(distance), where=step > 0)
    return (step / 3) * np.maximum(1 - fraction, 0) ** 3


def triangle_wave(phase, step, highest_harmonic):
    if highest_harmonic <= SHORT_HARMONIC_SERIES:
        # For short harmonic series, Fourier synthesis removes aliased
        # overtones entirely and preserves the amplitude of the in-band ones.
        angle = 2 * pi * phase
        values = np.cos(angle)
        for order in range(3, highest_harmonic + 1, 2):
            values += np.cos(order * angle) / order ** 2
        return (-8 / pi ** 2) * values
    # At low pitches there can be thousands of in-band overtones. Correct
    # the two corners in constant time using the local phase increment.
    values = 1 - 4 * np.abs(phase - 0.5)
    return values + 4 * (polyblamp(phase, step) - polyblamp((phase + 0.5) % 1, step))


def discontinuous_wave(phase, step, highest_harmonic, square):
    if highest_harmonic <= SHORT_HARMONIC_SERIES:
        # A short Fourier series preserves the ratios of the audible
        # harmonics. Scale it by its continuous-time peak so ringing at the
        # edges of the waveform respects the selected output level.
        orders = range(1, highest_harmonic + 1, 2 if square else 1)
        peak_angle = pi / (2 * len(orders)) if square else pi / (len(orders) + 1)
        peak = sum(sin(order * peak_angle) / order for order in orders)
        angle = 2 * pi * phase
        values = np.sin(angle)
        for order in orders[1:]:
            values += np.sin(order * angle) / order
        return values / (peak if square else -peak)
    # At lower pitches, smooth the jumps in constant time per sample instead
    # of summing hundreds of in-band terms.
    if square:
        values = np.where(phase < 0.5, 1.0, -1.0)
        return values + polyblep(phase, step) - polyblep((phase + 0.5) % 1, step)
    return 2 * phase - 1 - polyblep(phase, step)


class NoiseSource:
    """Continuous test noise with equal pink-noise power per octave."""

    def __init__(self, seed=None):
        self.random = np.random.default_rng(seed)
        self._rate = None
        self._filters = ()

    def render(self, count, sample_rate, waveform):
        if waveform == 'white':
            return self.random.uniform(-1, 1, count)
        if self._rate != sample_rate:
            self._rate = sample_rate
            # Independent first-order processes of equal variance, spaced one
            # octave apart, sum to an approximately 1/f power spectrum. Set
            # pole positions in Hz so the low-frequency response survives an
            # interface sample-rate change.
            edges = 4.0 * 2 ** np.arange(int(np.ceil(np.log2(sample_rate / 4))))
            poles = np.exp(-2 * pi * edges / sample_rate)
            self._filters = [(np.sqrt(1 - pole ** 2), pole, np.zeros(1)) for pole in poles]
        if not count:
            return np.empty(0)
        excitation = self.random.standard_normal((count, len(self._filters)))
        pink = np.zeros(count)
        for index, (gain, pole, state) in enumerate(self._filters):
            band, state[:] = lfilter([gain], [1, -pole], excitation[:, index], zi=state)
            pink += band
        # Keep the output bounded, with approximately 15 dB peak headroom
        # before the rare Gaussian excursion is clipped.
        return np.clip(pink / (5.5 * np.sqrt(len(self._filters))), -1, 1)


class SignalGenerator:
    def __init__(self):
        self.phase = 0.0
        self.modulation_phase = 0.0
        self.amplitude = 0.0
        self.noise = NoiseSource()

    def render(self, count, sample_rate, settings):
        result = np.zeros((count, 2), dtype=np.float32)
        if not count or (not settings.enabled and not self.amplitude):
            return result
        indices = np.arange(count)
        if settings.waveform in NOISE_WAVEFORMS:
            values = self.noise.render(count, sample_rate, settings.waveform)
        else:
            values = self._tone(indices, count, sample_rate, settings)
        goal = settings.level if settings.enabled else 0.0
        if goal != self.amplitude:
            # A short gain ramp avoids clicks on start, stop and volume edits.
            decay = np.exp(-(indices + 1) / (0.003 * sample_rate))
            amplitude = goal + decay * (self.amplitude - goal)
            self.amplitude = float(amplitude[-1])
            if abs(goal - self.amplitude) < 1e-5:
                self.amplitude = goal
        else:
            amplitude = self.amplitude
        values *= amplitude
        result[:, 0] = values * (1 - max(0.0, settings.pan))
        result[:, 1] = values * (1 + min(0.0, settings.pan))
        return result

    def _tone(self, indices, count, sample_rate, settings):
        carrier_step = settings.effective_frequency(sample_rate) / sample_rate
        mod_step = settings.modulation_frequency / sample_rate
        depth = settings.modulation_depth / 100 if mod_step else 0.0
        if depth:
            # Integrate the modulation over each sample interval. The mean
            # of a sinusoid over that interval is its midpoint times sinc.
            modulation = np.sin(2 * pi * (self.modulation_phase + (indices + 0.5) * mod_step))
            steps = carrier_step * (1 + depth * np.sinc(mod_step) * modulation)
            cumulative = np.cumsum(steps)
            phase = (self.phase + np.concatenate(([0.0], cumulative[:-1]))) % 1
            self.phase = (self.phase + cumulative[-1]) % 1
        else:
            steps = carrier_step
            phase = (self.phase + indices * steps) % 1
            self.phase = (self.phase + count * steps) % 1
        self.modulation_phase = (self.modulation_phase + count * mod_step) % 1
        waveform = settings.waveform
        if waveform in ("square", "sawtooth", "triangle"):
            # Select the same harmonics throughout an FM cycle and reserve
            # space for the sidebands beyond the highest instantaneous pitch.
            highest_pitch = sample_rate * carrier_step * (1 + depth)
            limit = 0.49 * sample_rate - (settings.modulation_frequency if depth else 0.0)
            highest_harmonic = max(1, int(limit / highest_pitch))
            if waveform == "triangle":
                values = triangle_wave(phase, steps, highest_harmonic)
            else:
                values = discontinuous_wave(phase, steps, highest_harmonic, waveform == "square")
        else:
            values = np.sin(2 * pi * phase)
        return values


def monitor_route(frames, demodulated, mode):
    if mode == "demodulated":
        return np.repeat(np.clip(demodulated * 0.05, -1, 1)[:, np.newaxis], 2, axis=1)
    if frames.shape[1] == 1:
        if mode in ("right", "difference"):
            # A mono capture cannot provide an interchannel comparison.
            return np.zeros((len(frames), 2), dtype=frames.dtype)
        return np.repeat(frames, 2, axis=1)
    if mode == "left":
        return np.repeat(frames[:, :1], 2, axis=1)
    if mode == "right":
        return np.repeat(frames[:, 1:], 2, axis=1)
    if mode == "mono":
        return np.repeat(frames.mean(axis=1, keepdims=True), 2, axis=1)
    if mode == "mono_sum":
        # Leave headroom policy to the output mixer, which applies gain and clips the final stream.
        return np.repeat(frames.sum(axis=1, keepdims=True), 2, axis=1)
    if mode == "difference":
        # Half gain preserves headroom for opposite-polarity full-scale inputs.
        return np.repeat(0.5 * (frames[:, :1] - frames[:, 1:]), 2, axis=1)
    return frames


class MonitorResampler:
    """Linear conversion for listening when input and output rates differ."""

    def __init__(self, input_rate, output_rate):
        step = Fraction(input_rate / output_rate).limit_denominator(1_000_000_000)
        self.step = step.numerator
        self.denominator = step.denominator
        self.offset = 0
        self.previous = None

    def process(self, frames):
        if self.step == self.denominator or not len(frames):
            return frames
        if self.previous is not None:
            frames = np.concatenate((self.previous[np.newaxis], frames))
        self.previous = frames[-1].copy()
        # A rational phase never drifts across an exact sample boundary as
        # the PortAudio block sizes change (for example, 32 to 48 kHz).
        end = (len(frames) - 1) * self.denominator
        positions = np.arange(self.offset, end, self.step, dtype=np.int64)
        self.offset += len(positions) * self.step - end
        base, remainder = np.divmod(positions, self.denominator)
        fraction = (remainder / self.denominator)[:, np.newaxis]
        return frames[base] + (frames[base + 1] - frames[base]) * fraction
