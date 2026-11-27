"""Shared plot presentation and axis helpers for native displays and exports."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from math import ceil, floor, isfinite, log10

import numpy as np

TRACE_COLORS = ("#2175cf", "#ad4c2d", "#168366", "#8a52ba", "#a57d21")
HIGH_CONTRAST_TRACES = ("#005eae", "#a04500", "#00785a", "#8536a1", "#574b00")
BLUE_ORANGE_TRACES = ("#0072b2", "#bd5300", "#008462", "#9a4f89", "#746113")
THEME_ACCENT = {"modern": "#0866a5", "retro": "#925710", "neumorphism": "#187568", "neubrutalism": "#7350a7"}


@dataclass(frozen=True, slots=True)
class PlotColors:
    background: tuple[int, int, int]
    panel: tuple[int, int, int]
    text: tuple[int, int, int]
    muted: tuple[int, int, int]
    grid: tuple[int, int, int]
    minor_grid: tuple[int, int, int]
    axis: tuple[int, int, int]
    cursor: tuple[int, int, int]
    accent: tuple[int, int, int]


@lru_cache(maxsize=16)
def _plot_colors(dark, theme):
    # Keep the plot surface neutral. Theme accents belong to cues and traces,
    # where they can be adjusted against a consistent measurement background.
    if dark:
        background, panel = (28, 32, 37), (24, 27, 31)
        text, muted = (236, 239, 242), (174, 183, 191)
        grid, minor, axis = (58, 65, 72), (43, 48, 55), (106, 115, 124)
    else:
        background, panel = (253, 253, 253), (255, 255, 255)
        text, muted = (29, 36, 43), (78, 90, 102)
        grid, minor, axis = (216, 223, 229), (239, 242, 245), (132, 144, 155)
    accent = contrast_rgb(THEME_ACCENT.get(theme, THEME_ACCENT["modern"]), panel, 4.5)
    return PlotColors(background, panel, text, muted, grid, minor, axis, muted, accent)


@dataclass(frozen=True, slots=True)
class PlotStyle:
    grid: bool = True
    bold: bool = False
    high_contrast: bool = False
    heat_palette: str = "viridis"
    heat_range: int = 100
    canvas: str = "system"
    palette: str = "theme"
    grid_detail: str = "major"
    legend: str = "auto"
    weight: str = "standard"
    point_markers: bool = False
    theme: str = "modern"
    app_mode: str = "auto"

    @classmethod
    def from_settings(cls, settings):
        palette = settings.get("plotPalette", "theme")
        weight = settings.get("plotTraceStyle", "standard")
        return cls(grid=settings.get("plotGrid", True), bold=weight == "bold", high_contrast=palette == "highContrast",
                   heat_palette=settings.get("spectrogramPalette", "viridis"),
                   heat_range=settings.get("spectrogramRange", 100), canvas=settings.get("plotCanvas", "system"),
                   palette=palette, grid_detail=settings.get("plotGridDetail", "major"),
                   legend=settings.get("plotLegend", "auto"), weight=weight,
                   point_markers=settings.get("plotPointMarkers", False), theme=settings.get("theme", "modern"),
                   app_mode=settings.get("colorMode", "auto"))

    def colors(self, system_dark=False):
        mode = self.app_mode if self.canvas == "system" else self.canvas
        return _plot_colors(mode == "dark" or (mode == "auto" and system_dark), self.theme)

    @property
    def line_width(self):
        return 2.8 if self.bold else 1 if self.weight == "fine" else 1.8

    @property
    def colormap(self):
        return "gray" if self.heat_palette == "grayscale" else self.heat_palette

    def trace_rgb(self, color, index, background):
        if self.high_contrast or self.palette == "highContrast":
            color = HIGH_CONTRAST_TRACES[index % len(HIGH_CONTRAST_TRACES)]
        elif self.palette == "blueOrange":
            color = BLUE_ORANGE_TRACES[index % len(BLUE_ORANGE_TRACES)]
        return contrast_rgb(color, background, 4.5 if self.high_contrast or self.palette == "highContrast" else 3)


DEFAULT_PLOT_STYLE = PlotStyle()


def _luminance(rgb):
    red, green, blue = ((channel / 255 / 12.92 if channel <= 10 else ((channel / 255 + 0.055) / 1.055) ** 2.4)
                        for channel in rgb)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast_ratio(first, second):
    low, high = sorted((_luminance(first), _luminance(second)))
    return (high + 0.05) / (low + 0.05)


@lru_cache(maxsize=128)
def contrast_rgb(color, background, target):
    # Blend only as much as needed towards black or white to keep the series'
    # color recognizable against both light and dark system plot backgrounds.
    rgb = tuple(int(color[index:index + 2], 16) for index in (1, 3, 5))
    if contrast_ratio(rgb, background) >= target:
        return rgb
    end = max((0, 255), key=lambda tone: contrast_ratio((tone,) * 3, background))
    lower, upper = 0.0, 1.0
    chosen = (end,) * 3
    for _ in range(9):
        amount = (lower + upper) / 2
        blended = tuple(round(channel + (end - channel) * amount) for channel in rgb)
        if contrast_ratio(blended, background) >= target:
            chosen = blended
            upper = amount
        else:
            lower = amount
    return chosen


@lru_cache(maxsize=3)
def heat_colors(name):
    # Use the same perceptually ordered lookup tables as the report renderer.
    # Import lazily so opening a meter does not load Matplotlib.
    from matplotlib import colormaps

    return colormaps[name](np.arange(256), bytes=True)[:, :3]


def heat_image(levels, style):
    normalized = np.clip(np.nan_to_num(levels / style.heat_range + 1, nan=0), 0, 1)
    indices = np.rint(normalized * 255).astype(np.uint8)
    return heat_colors(style.colormap)[indices]


def cell_edges(centres):
    """Place image boundaries halfway between adjacent sample centres."""
    if len(centres) == 1:
        centre = float(centres[0])
        half_width = max(0.01, abs(centre) * 0.02)
        return np.array((centre - half_width, centre + half_width))
    middle = centres[:-1] * 0.5 + centres[1:] * 0.5
    first, last = centres[0], centres[-1]
    return np.concatenate(([first - (middle[0] - first)], middle, [last + (last - middle[-1])]))


def equal_axis_spans(horizontal, vertical):
    """Keep the same distance per unit in both directions on a square plot."""
    axes = horizontal, vertical
    spans = tuple(high - low for low, high in axes)
    size = max(spans)
    return tuple((low - (size - span) / 2, high + (size - span) / 2)
                 for (low, high), span in zip(axes, spans, strict=True))


def axis_ticks(low, high, count, *, logarithmic=False, step=None):
    """Return grid positions paired with labels in physical units."""
    span = high - low
    if not isfinite(span) or span <= 0 or (logarithmic and (low < -324 or high > 309)):
        return ()
    count = max(2, count)
    if logarithmic:
        values = _log_ticks(low, high, count)
    elif step is not None and isfinite(step) and step > 0 and 1 <= span / step <= 30:
        # Oscilloscope divisions keep a fixed physical size. If the user
        # zooms unusually far, fall back to readable automatic labels.
        begin, end = ceil(low / step - 1e-9), floor(high / step + 1e-9)
        values = np.arange(begin, end + 1, dtype=float) * step
    else:
        values = _linear_ticks(low, high, count)
    values = np.unique(values)
    with np.errstate(over="ignore"):
        labels = 10 ** values if logarithmic else values
    valid = np.isfinite(labels)
    if logarithmic:
        valid &= labels > 0
    values, labels = values[valid], labels[valid]
    _, distinct = np.unique(labels, return_index=True)
    values, labels = values[distinct], labels[distinct]
    digits = 5
    # Labels need enough digits to stay distinct when inspecting a tightly
    # zoomed drift trace at an absolute frequency around 3000 Hz.
    if labels.size >= 2:
        step = np.min(np.diff(labels))
        largest = np.max(np.abs(labels))
        digits = min(17, max(4, ceil(log10(largest) - log10(step)) + 1))
    return tuple(zip(values, (_tick_label(value, digits) for value in labels), strict=True))


def _tick_label(value, digits):
    text = f"{value:.{digits}g}"
    if 0.0001 <= abs(value) < 1_000_000_000:
        return format(Decimal(text), ",f")
    return text


def _linear_ticks(low, high, count):
    target = (high - low) / max(1, count - 1)
    if not target or not isfinite(target):
        return np.array((low, high))
    power = 10.0 ** floor(log10(target))
    if not power:
        return np.array((low, high))
    step = next(multiple * power for multiple in (1, 2, 2.5, 5, 10) if multiple * power >= target)
    if not isfinite(step):
        return np.array((low, high))
    first, last = ceil(low / step - 1e-9), floor(high / step + 1e-9)
    if last <= first:
        return np.array((low, high))
    return (first + np.arange(last - first + 1, dtype=float)) * step


def _log_ticks(low, high, count):
    # Label powers of ten across a wide range. Add 2 and 5 when they fit,
    # then switch to local linear steps once zoomed into part of a decade.
    decades = np.arange(floor(low), ceil(high) + 1, dtype=float)
    powers = decades[(decades >= low) & (decades <= high)]
    if len(powers) >= count:
        return powers[::ceil(len(powers) / count)]
    values = (decades[:, None] + np.log10((1, 2, 5))).reshape(-1)
    values = values[(values >= low) & (values <= high)]
    if 2 <= len(values) <= count:
        return values
    if len(powers) >= 2:
        return powers
    with np.errstate(over="ignore"):
        start, stop = np.power(10.0, (low, high))
    if isfinite(stop) and 0 < start < stop:
        return np.log10(_linear_ticks(start, stop, count))
    return powers


def peak_sample_indices(columns, values):
    """Keep each pixel column's edges and extrema in their original order."""
    if not len(values):
        return np.empty(0, dtype=np.intp)
    changes = np.r_[True, np.diff(columns) != 0]
    starts = np.flatnonzero(changes)
    if len(starts) == len(values):
        return starts
    groups = np.cumsum(changes) - 1
    indices = np.arange(len(values))
    min_values = np.minimum.reduceat(values, starts)[groups]
    max_values = np.maximum.reduceat(values, starts)[groups]
    minima = np.minimum.reduceat(np.where(values == min_values, indices, len(values)), starts)
    maxima = np.minimum.reduceat(np.where(values == max_values, indices, len(values)), starts)
    ends = np.r_[starts[1:] - 1, len(values) - 1]
    return np.unique(np.concatenate((starts, minima, maxima, ends)))
