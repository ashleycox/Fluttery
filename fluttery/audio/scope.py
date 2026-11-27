"""Sample-clock oscilloscope acquisition, independent of the GUI refresh rate."""

from dataclasses import dataclass

import numpy as np

SCOPE_HISTORY_SECONDS = 4
TIME_DIVISIONS = 10
LEVEL_DIVISIONS = 8


@dataclass(frozen=True, slots=True)
class ScopeBuffer:
    """One contiguous snapshot ending at an exclusive sample index."""

    rate: float
    samples: np.ndarray
    end: int
    epoch: object

    @property
    def start(self):
        return self.end - len(self.samples)


@dataclass(frozen=True, slots=True)
class ScopeSweep:
    rate: float
    samples: np.ndarray
    time_ms: np.ndarray
    trigger_frame: float | None


def edge_positions(samples, level, *, rising=True, hysteresis=0.0):
    """Time each threshold crossing by interpolation, qualifying its excursions."""
    values = np.asarray(samples, dtype=np.float64) - level
    if not rising:
        values = -values
    finite = np.isfinite(values)
    crossing, = np.nonzero(finite[:-1] & finite[1:] & (values[:-1] < 0) & (values[1:] >= 0))
    if hysteresis > 0 and len(crossing):
        # A Schmitt transition starts below the low threshold and completes
        # above the high threshold. Timing is measured at the final central
        # crossing before confirmation, allowing chatter inside the band.
        boundary = np.flatnonzero(~finite | (np.abs(values) >= hysteresis))
        confirmed = (values[boundary[:-1]] <= -hysteresis) & (values[boundary[1:]] >= hysteresis)
        high = boundary[1:][confirmed]
        crossing = crossing[np.searchsorted(crossing, high) - 1]
    return crossing - values[crossing] / (values[crossing + 1] - values[crossing])


def acquire_sweep(buffer, span_ms, *, channel=0, level=0.0, rising=True, pretrigger=0.2,
                  hysteresis=0.0, after=None, first=False, free_run=False, recent_ms=None):
    """Extract a complete capture with elapsed times relative to the trigger.

    ``after`` is an exclusive absolute crossing position. Single acquisition
    asks for the earliest eligible crossing; a live sweep uses the latest.
    A crossing near the end of input remains eligible on a later poll, when
    enough post-trigger samples exist. In free-running mode the latest slice
    is shown if a recent completed edge is unavailable.
    """
    frames = max(2, round(buffer.rate * span_ms / 1000) + 1)
    before = round((frames - 1) * pretrigger)
    position = None
    if channel is not None and len(buffer.samples) >= frames:
        positions = edge_positions(buffer.samples[:, channel], level, rising=rising, hysteresis=hysteresis)
        starts = np.floor(positions).astype(np.int64) - before
        eligible = (starts >= 0) & (starts + frames <= len(buffer.samples))
        if after is not None:
            eligible &= positions + buffer.start > after
        if recent_ms is not None:
            eligible &= len(buffer.samples) - 1 - (positions + frames - before - 1) <= recent_ms * buffer.rate / 1000
        if len(found := np.flatnonzero(eligible)):
            choice = found[0 if first else -1]
            start = int(starts[choice])
            position = float(positions[choice])
    if position is None:
        if not free_run or len(buffer.samples) < 2:
            return None
        start = max(0, len(buffer.samples) - frames)
    samples = buffer.samples[start:start + frames].copy()
    origin = position if position is not None else start
    times = (np.arange(len(samples), dtype=float) + start - origin) * (1000 / buffer.rate)
    return ScopeSweep(buffer.rate, samples, times, position + buffer.start if position is not None else None)
