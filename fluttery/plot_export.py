"""Render an immutable view of a native plot without reading live controls."""

from __future__ import annotations

import io
from dataclasses import dataclass, replace
from math import inf, log10, nextafter
from pathlib import Path
from textwrap import fill
from threading import Lock

import numpy as np

from .plot_style import DEFAULT_PLOT_STYLE, PlotStyle, axis_ticks, cell_edges, equal_axis_spans
from .sessions import atomic_write

IMAGE_FORMATS = ('png', 'svg', 'pdf')
MIN_IMAGE_WIDTH, MIN_IMAGE_HEIGHT = 320, 220
MAX_IMAGE_SIZE, MAX_IMAGE_PIXELS = 8000, 32_000_000
MIN_IMAGE_DPI, MAX_IMAGE_DPI = 72, 600

# The figure is local to the job. The lock serializes format conversions that
# share Matplotlib's font caches, including copies requested from two windows.
_RENDER_LOCK = Lock()


def _frozen_array(values, *, vector=False):
    array = np.asarray(values, dtype=float)
    if vector:
        array = array.reshape(-1)
    # An immutable bytes buffer prevents both a live data update and a caller
    # toggling ndarray write flags from changing a job already in progress.
    return np.frombuffer(array.tobytes(), dtype=array.dtype).reshape(array.shape)


@dataclass(frozen=True, slots=True)
class PlotTrace:
    label: str
    x: np.ndarray
    y: np.ndarray
    color: str = '#2175cf'
    style: str = 'line'
    palette_index: int | None = None

    def __post_init__(self):
        x = _frozen_array(self.x, vector=True)
        y = _frozen_array(self.y, vector=True)
        if x.shape != y.shape:
            raise ValueError('Plot coordinates must have the same length.')
        object.__setattr__(self, 'x', x)
        object.__setattr__(self, 'y', y)


@dataclass(frozen=True, slots=True)
class PlotCursor:
    label: str
    x: float
    y: float
    level: float | None = None


@dataclass(frozen=True, slots=True)
class PlotSnapshot:
    """Plot data, axes, and selected markers expressed in physical units."""

    title: str
    x_label: str
    y_label: str
    traces: tuple[PlotTrace, ...] = ()
    heatmap: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None
    log_x: bool = False
    square_axes: bool = False
    x_bounds: tuple[float, float] | None = None
    y_bounds: tuple[float, float] | None = None
    full_x_bounds: tuple[float, float] | None = None
    full_y_bounds: tuple[float, float] | None = None
    x_ticks: tuple[tuple[float, str], ...] | None = None
    cursors: tuple[PlotCursor, ...] = ()
    appearance: PlotStyle = DEFAULT_PLOT_STYLE
    system_dark: bool = False
    grid_steps: tuple[float | None, float | None] = (None, None)
    mark_time_origin: bool = False

    def __post_init__(self):
        object.__setattr__(self, 'traces', tuple(self.traces))
        object.__setattr__(self, 'cursors', tuple(self.cursors))
        if len(self.grid_steps) != 2 or any(step is not None and (not np.isfinite(step) or step <= 0)
                                            for step in self.grid_steps):
            raise ValueError('Graticule divisions must be positive and finite.')
        if self.x_ticks is not None:
            object.__setattr__(self, 'x_ticks', tuple((float(value), str(label)) for value, label in self.x_ticks))
        for key in ('x_bounds', 'y_bounds', 'full_x_bounds', 'full_y_bounds'):
            if (supplied := getattr(self, key)) is not None:
                low, high = map(float, supplied)
                if not (np.isfinite(low) and np.isfinite(high) and low < high):
                    raise ValueError('Plot limits must be finite and in ascending order.')
                if self.log_x and key.endswith('x_bounds') and low <= 0:
                    raise ValueError('A logarithmic plot starts above zero.')
                object.__setattr__(self, key, (low, high))
        if self.heatmap is not None:
            x, y, levels = self.heatmap
            x, y = (_frozen_array(axis, vector=True) for axis in (x, y))
            levels = _frozen_array(levels)
            if not len(x) or not len(y) or levels.shape != (len(y), len(x)):
                raise ValueError('A heatmap must have one value for each pair of x and y coordinates.')
            if not all(np.isfinite(axis).all() and (np.diff(axis) > 0).all() for axis in (x, y)):
                raise ValueError('Heatmap coordinates must be finite and in ascending order.')
            if self.log_x and x[0] <= 0:
                raise ValueError('A logarithmic heatmap starts above zero.')
            object.__setattr__(self, 'heatmap', (x, y, levels))


@dataclass(frozen=True, slots=True)
class PlotImageOptions:
    """Pixel dimensions and print density, shared by save and clipboard actions."""

    width: int = 1600
    height: int = 900
    dpi: int = 144
    background: str = 'light'
    full_view: bool = False
    show_title: bool = True
    show_legend: bool | None = None
    show_grid: bool | None = None
    show_cursors: bool = True
    title: str | None = None

    def __post_init__(self):
        fields = ((self.width, MIN_IMAGE_WIDTH, MAX_IMAGE_SIZE), (self.height, MIN_IMAGE_HEIGHT, MAX_IMAGE_SIZE),
                  (self.dpi, MIN_IMAGE_DPI, MAX_IMAGE_DPI))
        if not all(type(value) is int and lower <= value <= upper for value, lower, upper in fields):
            raise ValueError('Use a width of 320–8000 pixels, a height of 220–8000 pixels and a resolution of 72–600 DPI.')
        if self.width * self.height > MAX_IMAGE_PIXELS:
            raise ValueError('Choose a width and height that give at most 32 million pixels.')
        if self.background not in ('display', 'light', 'dark', 'transparent'):
            raise ValueError('Choose a display, light, dark, or transparent background.')


def _color(rgb):
    return tuple(channel / 255 for channel in rgb)


def _draw_lines(axis, snapshot, style, panel, scale):
    bars = []
    for index, trace in enumerate(snapshot.traces):
        valid = np.isfinite(trace.x) & np.isfinite(trace.y)
        if snapshot.log_x:
            valid &= trace.x > 0
        if not valid.any():
            continue
        palette_index = index if trace.palette_index is None else trace.palette_index
        color = _color(style.trace_rgb(trace.color, palette_index, panel))
        label = trace.label or '_nolegend_'
        stroke = max(0.3 * scale, style.line_width * 0.85 * scale)
        if trace.style == 'bars':
            x, y = trace.x[valid], trace.y[valid]
            axis.update_datalim(np.column_stack((x, y)))
            bars.append((x, y, color, label, stroke))
            continue
        if trace.style == 'points':
            axis.plot(trace.x[valid], trace.y[valid], linestyle='None', marker='o', color=color,
                      label=label, markersize=3.5 * scale)
            continue
        # NaNs break connected paths. Give isolated valid samples a point so
        # a brief signal lock between gaps is still represented in the image.
        isolated = valid & ~np.r_[False, valid[:-1]] & ~np.r_[valid[1:], False]
        marks = np.flatnonzero(valid if style.point_markers else isolated)
        x, y = ((trace.x, trace.y) if valid.all()
                else (np.where(valid, trace.x, np.nan), np.where(valid, trace.y, np.nan)))
        axis.plot(x, y, label=label, color=color, linewidth=stroke,
                  linestyle='--' if trace.style == 'dashed' else '-', marker='o' if len(marks) else None,
                  markevery=marks.tolist(), markersize=3 * scale)
    return bars


def _draw_heatmap(axis, snapshot, style):
    x, y, values = snapshot.heatmap
    x_edges = 10 ** cell_edges(np.log10(x)) if snapshot.log_x else cell_edges(x)
    # The cell boundaries stay accurate on a zoomed, non-uniform frequency or
    # time axis. Vector formats embed only the heat layer as a raster image.
    return axis.pcolormesh(x_edges, cell_edges(y), np.ma.masked_invalid(values), cmap=style.colormap,
                           vmin=-style.heat_range, vmax=0, rasterized=True)


def _ticks(axis, snapshot, scale, show_grid, palette, detail, *, heatmap):
    from matplotlib.ticker import AutoMinorLocator, LogLocator, NullFormatter

    left, right = axis.get_xlim()
    bottom, top = axis.get_ylim()
    if snapshot.x_ticks is None:
        if snapshot.log_x:
            x_ticks = [(10 ** tick, text) for tick, text in axis_ticks(log10(left), log10(right), 7, logarithmic=True)]
        else:
            x_ticks = axis_ticks(left, right, 7, step=snapshot.grid_steps[0])
    else:
        x_ticks = [(tick, text) for tick, text in snapshot.x_ticks if left <= tick <= right]
        # Keep long equipment or adjustment labels readable in a small image.
        max_ticks = max(1, min(8, 72 // max((len(text) for _, text in x_ticks), default=1)))
        if len(x_ticks) > max_ticks:
            sample = np.linspace(0, len(x_ticks) - 1, max_ticks, dtype=int)
            x_ticks = [x_ticks[index] for index in sample]
    y_ticks = axis_ticks(bottom, top, 6, step=snapshot.grid_steps[1])
    axis.set_xticks([tick for tick, _ in x_ticks], [text for _, text in x_ticks], parse_math=False)
    axis.set_yticks([tick for tick, _ in y_ticks], [text for _, text in y_ticks], parse_math=False)
    axis.set_xlim(left, right)
    axis.set_ylim(bottom, top)
    if show_grid:
        axis.set_axisbelow(not heatmap)
        grid_color = (1.0, 1.0, 1.0, 0.3) if heatmap else _color(palette.grid)
        minor_grid_color = (1.0, 1.0, 1.0, 0.17) if heatmap else _color(palette.minor_grid)
        axis.grid(color=grid_color, linewidth=0.65 * scale)
        if detail == 'minor':
            if snapshot.x_ticks is None:
                axis.xaxis.set_minor_locator(LogLocator(subs='auto') if snapshot.log_x else AutoMinorLocator(2))
                axis.xaxis.set_minor_formatter(NullFormatter())
            axis.yaxis.set_minor_locator(AutoMinorLocator(2))
            axis.grid(which='minor', color=minor_grid_color, linewidth=0.45 * scale)
            axis.tick_params(which='minor', length=0)
        if not heatmap and bottom < 0 < top:
            axis.axhline(0, color=_color(palette.axis), linewidth=0.85 * scale)
        if snapshot.square_axes and left < 0 < right:
            axis.axvline(0, color=_color(palette.axis), linewidth=0.85 * scale)
    if snapshot.mark_time_origin and left < 0 < right:
        axis.axvline(0, color=_color(palette.accent), linewidth=0.85 * scale, linestyle='--')


def _draw_cursors(axis, cursors, palette, scale, *, heatmap):
    from matplotlib.patheffects import Normal, Stroke

    left, right = axis.get_xlim()
    bottom, top = axis.get_ylim()
    ink, paper = _color(palette.accent), _color(palette.panel)
    guide = {'color': ink, 'linewidth': 0.85 * scale, 'zorder': 4}
    if heatmap:
        # Pair dark edges with the white guide so values remain legible on
        # either end of any spectrogram colour ramp.
        ink, paper = 'white', '#191d23'
        guide.update(color=ink, path_effects=[Stroke(linewidth=2 * scale, foreground=paper), Normal()])
    for cursor in cursors:
        if not (left <= cursor.x <= right and bottom <= cursor.y <= top):
            continue
        axis.axvline(cursor.x, **guide, linestyle='--')
        if not heatmap:
            axis.axhline(cursor.y, **guide, linestyle=':')
        axis.plot(cursor.x, cursor.y, marker='o', markerfacecolor=paper, markeredgecolor=ink,
                  markeredgewidth=1.2 * scale, markersize=5 * scale, zorder=5)
        # Put the callout on the inside of the axes when the point is near the
        # top or right edge. The document metadata retains exact coordinates.
        threshold = 10 ** (0.2 * log10(left) + 0.8 * log10(right)) if axis.get_xscale() == 'log' else (
            left + 0.8 * (right - left))
        left_side = cursor.x > threshold
        lower_side = cursor.y > bottom + 0.8 * (top - bottom)
        axis.annotate(cursor.label, (cursor.x, cursor.y),
                      xytext=((-6 if left_side else 6) * scale, (-7 if lower_side else 7) * scale),
                      textcoords='offset points', horizontalalignment='right' if left_side else 'left',
                      verticalalignment='top' if lower_side else 'bottom', color=ink, fontsize=9 * scale,
                      fontweight='bold', parse_math=False, clip_on=True, zorder=6,
                      bbox={'boxstyle': 'round,pad=0.15', 'facecolor': paper, 'edgecolor': 'none'})


def _legend(axis, style, options, palette, scale):
    handles, labels = axis.get_legend_handles_labels()
    enabled = options.show_legend
    if enabled is None:
        enabled = style.legend == 'always' or (style.legend == 'auto' and len(labels) > 1)
    if not enabled or not labels:
        return
    columns = max(1, min(4, 96 // (max(map(len, labels)) + 10)))
    legend = axis.legend(handles, [fill(label, width=76, break_long_words=True) for label in labels],
                         loc='lower left', bbox_to_anchor=(0, 1.025), ncols=columns, fontsize=9 * scale,
                         frameon=False, labelcolor=_color(palette.text), borderaxespad=0, handlelength=2.2)
    for label in legend.get_texts():
        label.set_parse_math(False)


def _create_figure(snapshot, options):
    from matplotlib.figure import Figure

    style = snapshot.appearance
    if options.background != 'display':
        style = replace(style, canvas='dark' if options.background == 'dark' else 'light')
    palette = style.colors(snapshot.system_dark)
    # Agg truncates pixel dimensions. Guard against floating-point division
    # rounding an exact requested size infinitesimally below the integer.
    width, height = (nextafter(size / options.dpi, inf) for size in (options.width, options.height))
    # Keep text proportionate to the drawing even when users choose a high
    # print density. The PNG still has the exact requested pixel dimensions.
    scale = max(0.7 / (options.dpi / 72), min(width / 9.5, height / 3.35))
    transparent = options.background == 'transparent'
    figure = Figure(figsize=(width, height), dpi=options.dpi, layout='constrained',
                    facecolor='none' if transparent else _color(palette.background))
    figure.get_layout_engine().set(w_pad=0.12 * scale, h_pad=0.1 * scale)
    axis = figure.subplots()
    axis.set_facecolor('none' if transparent else _color(palette.panel))
    ink = _color(palette.text)
    axis.set_xlabel(snapshot.x_label, color=ink, fontsize=10 * scale, parse_math=False)
    axis.set_ylabel(snapshot.y_label, color=ink, fontsize=10 * scale, parse_math=False)
    axis.tick_params(length=3 * scale, color=_color(palette.axis), width=0.8 * scale,
                     labelcolor=_color(palette.muted), labelsize=9 * scale)
    for spine in axis.spines.values():
        spine.set(linewidth=0.75 * scale, color=_color(palette.axis))
    if snapshot.log_x:
        axis.set_xscale('log')
    if snapshot.square_axes:
        axis.set_box_aspect(1)
    axis.margins(0.04)
    image = _draw_heatmap(axis, snapshot, style) if snapshot.heatmap is not None else None
    bars = _draw_lines(axis, snapshot, style, palette.panel, scale)
    axis.autoscale_view()
    x_bounds = snapshot.full_x_bounds if options.full_view else snapshot.x_bounds
    y_bounds = snapshot.full_y_bounds if options.full_view else snapshot.y_bounds
    if x_bounds is not None:
        axis.set_xlim(*x_bounds)
    if y_bounds is not None:
        axis.set_ylim(*y_bounds)
    if snapshot.square_axes:
        # Full-range export may start from data bounds with unequal spans.
        # Work in the same coordinates as the canvas before setting limits.
        horizontal = tuple(map(log10, axis.get_xlim())) if snapshot.log_x else axis.get_xlim()
        horizontal, vertical = equal_axis_spans(horizontal, axis.get_ylim())
        if snapshot.log_x:
            horizontal = tuple(10 ** value for value in horizontal)
        axis.set_xlim(*horizontal)
        axis.set_ylim(*vertical)
    # The instrument draws spectral bars upward from the viewport floor.
    for x, y, color, label, stroke in bars:
        axis.vlines(x, axis.get_ylim()[0], y, label=label, color=color, linewidth=stroke)
    show_grid = style.grid and image is None if options.show_grid is None else options.show_grid
    _ticks(axis, snapshot, scale, show_grid, palette, style.grid_detail, heatmap=image is not None)
    if image is not None:
        colorbar = figure.colorbar(image, ax=axis, ticks=np.linspace(-style.heat_range, 0, 5), pad=0.025)
        colorbar.set_label('Level (dB)', color=ink, size=10 * scale)
        colorbar.outline.set_edgecolor(_color(palette.axis))
        colorbar.ax.tick_params(width=0.75 * scale, labelsize=9 * scale, labelcolor=_color(palette.muted),
                                color=_color(palette.axis), length=3 * scale)
    _legend(axis, style, options, palette, scale)
    if options.show_cursors:
        _draw_cursors(axis, snapshot.cursors, palette, scale, heatmap=image is not None)
    title = snapshot.title if options.title is None else options.title
    if options.show_title and title:
        wrapped = fill(title, width=max(24, min(140, int(width / scale * 9))), replace_whitespace=False)
        figure.suptitle(wrapped, x=0.035, ha='left', fontsize=14 * scale, fontweight='bold',
                        color=ink, parse_math=False)
    return figure, title


def render_plot_image(snapshot, options=PlotImageOptions(), *, format='png'):
    """Return PNG, SVG or PDF document bytes for a file or clipboard."""
    format = format.casefold()
    if format not in IMAGE_FORMATS:
        raise ValueError('Choose PNG, SVG, or PDF.')
    with _RENDER_LOCK, io.BytesIO() as stream:
        figure, title = _create_figure(snapshot, options)
        marks = ', '.join(f'{point.label} ({point.x:g}, {point.y:g})' + (
            f' at {point.level:g} dB' if point.level is not None else '') for point in snapshot.cursors)
        description = f'{snapshot.x_label} by {snapshot.y_label}'.strip()
        if options.show_cursors and marks:
            description += f'. Markers {marks}.'
        if format == 'png':
            from matplotlib.backends.backend_agg import FigureCanvasAgg

            FigureCanvasAgg(figure).print_png(stream, metadata={'Title': title, 'Description': description,
                                                                'Software': 'Fluttery'})
        elif format == 'svg':
            from matplotlib.backends.backend_svg import FigureCanvasSVG

            FigureCanvasSVG(figure).print_svg(stream, metadata={'Title': title, 'Description': description,
                                                                'Creator': 'Fluttery'})
        else:
            from matplotlib.backends.backend_pdf import FigureCanvasPdf

            FigureCanvasPdf(figure).print_pdf(stream, metadata={'Title': title, 'Subject': description,
                                                                'Creator': 'Fluttery'})
        return stream.getvalue()


def write_plot_image(path, snapshot, options=PlotImageOptions(), *, format=None):
    """Save beside the destination before replacing it, preserving a prior file on failure."""
    path = Path(path)
    data = render_plot_image(snapshot, options, format=(format or path.suffix.lstrip('.') or 'png'))
    atomic_write(path, data)
