"""Native plots with standard controls for exploring the plotted values."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise
from math import ceil, floor, log10

import numpy as np
import wx

from .keyboard_access import describe
from .plot_style import (
    DEFAULT_PLOT_STYLE,
    TRACE_COLORS,
    PlotStyle,
    axis_ticks,
    cell_edges,
    equal_axis_spans,
    heat_image,
    peak_sample_indices,
)


def set_control_help(window, description):
    """Expose the same description to native tooltips and contextual help."""
    window._description = description
    describe(window, description)


@dataclass
class _Series:
    label: str
    x: np.ndarray
    y: np.ndarray
    color: str
    style: str
    plot_x: np.ndarray
    ordered: bool
    slot: int
    raw: tuple[np.ndarray, np.ndarray]
    runs: tuple[tuple[int, int], ...]

    @property
    def key(self):
        return self.label, self.slot


@dataclass(frozen=True, slots=True)
class _Marker:
    x: float
    y: float
    trace: str
    level: float | None = None


@dataclass(slots=True)
class _Drag:
    origin: wx.Point
    bounds: tuple[tuple[float, float], tuple[float, float]]
    zoom: bool
    moved: bool = False


class LinePlot(wx.Panel):
    """A small wx canvas with keyboard access to values and viewport controls.

    The drawing and the readout share data coordinates. Drawing may decimate a
    long trace to fit the available pixels, while the readout visits every point.
    """

    def __init__(self, parent, *, title="", x_label="", y_label="", log_x=False,
                 x_formatter=None, y_formatter=None, controls=True):
        super().__init__(parent)
        self.title = title
        self.x_label = x_label
        self.y_label = y_label
        self.log_x = log_x
        self.square_axes = False
        self.x_formatter = x_formatter
        self.y_formatter = y_formatter
        self.x_ticks: tuple[tuple[float, str], ...] | None = None
        self.grid_steps = None, None
        self.mark_time_origin = False
        self._all_series: list[_Series] = []
        self._series: list[_Series] = []
        self._hidden_traces = set()
        self._legend_items = []
        self._heatmap: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None
        self._data_xlim, self._data_ylim = (0, 1), (0, 1)
        self._xlim = self._ylim = None
        self._default_xlim = self._default_ylim = None
        self._selection = self._hover = None
        self._markers: dict[str, _Marker] = {}
        self._prior_views = []
        self._expanded_descriptions = True
        self._drag = None
        self._zoom_rect = None
        self._mouse_cursor = wx.CURSOR_DEFAULT
        self._fit_signature = None
        self._heat_pixels = self._heat_edges = None
        self._bitmap = None
        self._scale_bitmap = None
        self._summary = "No plot data."
        self._empty_message = "No plot data."
        self.appearance = DEFAULT_PLOT_STYLE

        self.canvas = wx.Panel(self, style=wx.WANTS_CHARS, name=title or "Plot")
        self.canvas.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.canvas.SetMinSize(self.FromDIP((260, 140)))
        self.canvas.Bind(wx.EVT_PAINT, self._paint)
        self.canvas.Bind(wx.EVT_SIZE, self._resize)
        self.canvas.Bind(wx.EVT_KEY_DOWN, self._key)
        self.canvas.Bind(wx.EVT_LEFT_DOWN, self._mouse_down)
        self.canvas.Bind(wx.EVT_LEFT_UP, self._mouse_up)
        self.canvas.Bind(wx.EVT_MOTION, self._mouse_move)
        self.canvas.Bind(wx.EVT_LEAVE_WINDOW, self._mouse_leave)
        self.canvas.Bind(wx.EVT_MOUSE_CAPTURE_LOST, self._capture_lost)
        self.canvas.Bind(wx.EVT_MOUSEWHEEL, self._wheel)
        self.canvas.Bind(wx.EVT_LEFT_DCLICK, lambda _event: self.fit())
        self.canvas.Bind(wx.EVT_CONTEXT_MENU, self._plot_menu)
        self.canvas.Bind(wx.EVT_SET_FOCUS, self._canvas_focus)
        self.canvas.Bind(wx.EVT_KILL_FOCUS, self._canvas_focus)
        self._describe_canvas()

        readout_label = wx.StaticText(self, label="Plot data")
        self.readout = wx.TextCtrl(
            self, value="No plot data.", style=wx.TE_READONLY | wx.TE_MULTILINE,
            size=self.FromDIP((-1, 34)), name="Plot data",
        )
        if wx.Platform == "__WXMSW__":
            self._sync_readout_colours()
            self.readout.Bind(wx.EVT_SYS_COLOUR_CHANGED, self._readout_colours_changed)
        readout_help = (
            "Shows the graph summary, selected point and comparison markers. "
            "Press Shift+F10 for commands. Values stay still while you read them."
        )
        set_control_help(readout_label, readout_help)
        set_control_help(self.readout, readout_help)
        self.readout.Bind(wx.EVT_CONTEXT_MENU, self._plot_menu)
        self.readout.Bind(wx.EVT_KEY_DOWN, self._readout_key)
        self.readout.Bind(wx.EVT_KILL_FOCUS, self._readout_blur)
        readout_row = wx.BoxSizer(wx.HORIZONTAL)
        readout_row.Add(readout_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, self.FromDIP(6))
        readout_row.Add(self.readout, 1, wx.EXPAND)
        self.menu_button = wx.Button(self, label="Plot Menu")
        set_control_help(self.menu_button, "Open options for exploring, styling and exporting this graph.")
        self.menu_button.Bind(wx.EVT_BUTTON, lambda _event: self._plot_menu(anchor=self.menu_button))
        readout_row.Add(self.menu_button, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, self.FromDIP(6))
        self.toolbar = wx.WrapSizer(wx.HORIZONTAL)
        self._commands = self._make_commands()
        for label, action, description in self._commands:
            button = wx.Button(self, label=label)
            button.Bind(wx.EVT_BUTTON, lambda _event, action=action: action())
            set_control_help(button, description)
            self.toolbar.Add(button, 0, wx.RIGHT, self.FromDIP(4))

        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(self.canvas, 1, wx.EXPAND)
        layout.Add(readout_row, 0, wx.EXPAND | wx.TOP, self.FromDIP(4))
        layout.Add(self.toolbar, 0, wx.EXPAND | wx.TOP, self.FromDIP(4))
        self.SetSizer(layout)
        self.set_controls_visible(controls)

    def _make_commands(self):
        return (
            ("Previous Point", lambda: self._move_point(-1),
             "Select an earlier sample or frequency bin. With the plot focused, press Left Arrow."),
            ("Next Point", lambda: self._move_point(1),
             "Select a later sample or frequency bin. With the plot focused, press Right Arrow."),
            ("Previous Trace", lambda: self._move_trace(-1),
             "Switch to the previous line or spectrogram row. With the plot focused, press Down Arrow."),
            ("Next Trace", lambda: self._move_trace(1),
             "Switch to the next line or spectrogram row. With the plot focused, press Up Arrow."),
            ("Zoom In", lambda: self._zoom(0.65),
             "Magnify the plot around its centre. With the plot focused, press +."),
            ("Zoom Out", lambda: self._zoom(1.55),
             "Show a wider range of data. With the plot focused, press −."),
            ("Fit Plot", self.fit,
             "Restore the default axes so the standard view fits. With the plot focused, press Home."),
        )

    def keyboard_actions(self):
        from .plot_export_ui import plot_export_actions

        return (*self._commands, (
            "Set Axis Limits…", self._edit_limits,
            "Enter exact horizontal and vertical bounds. With the plot focused, press V.",
        ), (
            "Previous View", self._previous_view, "Restore the previous zoom or pan. With the plot focused, press Backspace.",
        ), (
            "Pan Plot Left", lambda: self._pan(-0.2, 0), "Move the view left. With the plot focused, press Shift+Left Arrow.",
        ), (
            "Pan Plot Right", lambda: self._pan(0.2, 0), "Move the view right. With the plot focused, press Shift+Right Arrow.",
        ), (
            "Pan Plot Up", lambda: self._pan(0, 0.2), "Move the view up. With the plot focused, press Shift+Up Arrow.",
        ), (
            "Pan Plot Down", lambda: self._pan(0, -0.2), "Move the view down. With the plot focused, press Shift+Down Arrow.",
        ), (
            "Set Marker A", lambda: self._set_marker("A"), "Pin the current reading as A. With the plot focused, press A.",
        ), (
            "Set Marker B", lambda: self._set_marker("B"), "Pin the current reading as B. With the plot focused, press B.",
        ), (
            "Clear Markers", self._clear_markers, "Remove both comparison markers. With the plot focused, press Escape.",
        ), (
            "Choose Visible Traces…", self._choose_traces,
            "Show or hide individual traces. Click a legend entry to toggle it or Shift-click to isolate it.",
        ), (
            "Read Plot Data", self.focus_data,
            "Focus the graph values. With the plot focused, press Return. Use the arrow keys to read the text.",
        ), *plot_export_actions(self), (
            "Plot Commands", self._plot_menu,
            "Open the menu of point, trace and zoom commands. Press Shift+F10 from the plot or its data field.",
        ))

    def _describe_canvas(self):
        axes = f" The horizontal axis is {self.x_label} and the vertical axis is {self.y_label}." if self.x_label and self.y_label else ""
        heatmap = " Colour represents signal level in dB." if self._heatmap is not None else ""
        description = (
            f"{self.title or 'Plot'}.{axes}{heatmap} Use Left and Right Arrow to choose a point, "
            "Page Up or Page Down to move faster, and Up and Down Arrow to choose a trace. "
            "Press A or B to set a marker, Return to read the data, or Shift+F10 for all plot commands. "
            "Drag to pan, Shift-drag to zoom to an area, or scroll to zoom around the pointer."
        )
        set_control_help(self.canvas, description)

    def set_controls_visible(self, visible):
        self.controls_visible = visible
        self.GetSizer().Show(self.toolbar, visible, recursive=True)
        self.Layout()
        self.InvalidateBestSize()
        if self.GetParent().GetSizer() is not None:
            self.GetParent().Layout()

    def focus_data(self):
        self._update_readout(force=True)
        self.readout.SetInsertionPoint(0)
        self.readout.SetFocus()

    def _plot_menu(self, event=None, *, anchor=None):
        menu = wx.Menu()
        try:
            self._populate_menu(menu)
            if anchor is not None:
                point = anchor.ClientToScreen(wx.Point(0, anchor.GetClientSize().height))
                self.PopupMenu(menu, self.ScreenToClient(point))
            elif event is None or event.GetPosition() == wx.DefaultPosition:
                self.PopupMenu(menu)
            else:
                self.PopupMenu(menu, self.ScreenToClient(event.GetPosition()))
        finally:
            menu.Destroy()

    def _populate_menu(self, menu):
        commands = {label: (action, description) for label, action, description in self.keyboard_actions()[:-1]}

        def add(target, labels):
            for label in labels:
                if (command := commands.pop(label, None)) is None:
                    continue
                action, description = command
                item = target.Append(wx.ID_ANY, label, description)
                target.Bind(wx.EVT_MENU, lambda _event, action=action: action(), item)
                if label == "Choose Visible Traces…":
                    item.Enable(len(self._all_series) > 1)
                elif label == "Previous View":
                    item.Enable(bool(self._prior_views))

        add(menu, ("Save Plot Image…", "Quick Save PNG…", "Copy Plot Image"))
        menu.AppendSeparator()
        add(menu, ("Read Plot Data", "Fit Plot", "Previous View", "Set Axis Limits…", "Choose Visible Traces…"))
        navigation = wx.Menu()
        add(navigation, tuple(label for label in commands if label.startswith(("Previous ", "Next ", "Zoom ", "Pan "))))
        menu.AppendSubMenu(navigation, "Navigation")
        markers = wx.Menu()
        add(markers, ("Set Marker A", "Set Marker B", "Clear Markers"))
        menu.AppendSubMenu(markers, "Markers")
        menu.AppendSeparator()
        add(menu, tuple(commands))

    def _readout_blur(self, event):
        event.Skip()
        self._update_readout(force=True)

    @staticmethod
    def _is_menu_key(event):
        key = event.GetKeyCode()
        return key == wx.WXK_WINDOWS_MENU or (key == wx.WXK_F10 and event.ShiftDown())

    def _readout_key(self, event):
        if self._is_menu_key(event):
            self._plot_menu()
        else:
            event.Skip()

    def _canvas_focus(self, event):
        self.canvas.Refresh(False)
        event.Skip()

    def set_labels(self, title=None, x_label=None, y_label=None):
        changed = False
        if title is not None and title != self.title:
            self.title = title
            self.canvas.SetName(title or "Plot")
            changed = True
        axes_changed = False
        if x_label is not None and x_label != self.x_label:
            self.x_label = x_label
            axes_changed = True
        if y_label is not None and y_label != self.y_label:
            self.y_label = y_label
            axes_changed = True
        if axes_changed:
            self._reset_interaction()
        if changed or axes_changed:
            self._describe_canvas()
            self._refresh()

    def set_expanded_descriptions(self, enabled):
        """Keep exact point navigation available with or without plot summaries."""
        self._expanded_descriptions = bool(enabled)
        self._update_readout()

    def set_appearance(self, settings):
        if wx.Platform == "__WXMSW__":
            self._sync_readout_colours()
        appearance = PlotStyle.from_settings(settings)
        if appearance != self.appearance:
            heat_colors_changed = (appearance.colormap, appearance.heat_range) != (
                self.appearance.colormap, self.appearance.heat_range,
            )
            self.appearance = appearance
            if heat_colors_changed and self._heatmap is not None:
                self._recolor_heatmap()
            self._refresh()

    def _readout_colours_changed(self, event):
        self._sync_readout_colours()
        event.Skip()

    def _sync_readout_colours(self):
        # Windows can keep a read-only edit's light background when switching
        # to dark appearance. Set both colours together so the data stays legible.
        background = wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOW)
        foreground = wx.SystemSettings.GetColour(wx.SYS_COLOUR_WINDOWTEXT)
        self.readout.SetBackgroundColour(background)
        self.readout.SetForegroundColour(foreground)
        self.readout.Refresh(False)

    def set_series(self, series, *, fit=False, empty_message=""):
        """Update traces on the wx thread without discarding a manual viewport."""
        self._empty_message = empty_message or "No plot data."
        was_heatmap = self._heatmap is not None
        if was_heatmap:
            self._reset_interaction()
        self._heatmap = None
        self._heat_pixels = self._heat_edges = self._bitmap = None
        prepared = []
        for index, trace in enumerate(series):
            x = np.asarray(trace["x"], dtype=float).reshape(-1)
            y = np.asarray(trace["y"], dtype=float).reshape(-1)
            if x.size != y.size:
                raise ValueError("Plot coordinates must have the same length.")
            valid = np.isfinite(x) & np.isfinite(y)
            if self.log_x:
                valid &= x > 0
            if valid.any():
                raw = x, y
                if valid.all():
                    runs = ((0, x.size),)
                else:
                    # Preserve breaks in the drawing and exported data. The
                    # keyboard cursor still skips missing or invalid samples.
                    positions = np.flatnonzero(valid)
                    breaks = np.r_[0, np.flatnonzero(np.diff(positions) > 1) + 1, len(positions)]
                    runs = tuple(pairwise(breaks))
                    raw = x, np.where(valid, y, np.nan)
                    x, y = x[valid], y[valid]
                plot_x = np.log10(x) if self.log_x else x
                prepared.append(_Series(
                    trace.get("label", ""), x, y,
                    trace.get("color", TRACE_COLORS[index % len(TRACE_COLORS)]), trace.get("style", "line"),
                    plot_x, bool(np.all(plot_x[1:] >= plot_x[:-1])), index, raw, runs,
                ))
        if not prepared:
            self._reset_interaction()
        self._all_series = prepared
        self._prepare_data_view(fit, "lines")
        self._apply_hidden_traces()
        if was_heatmap:
            self._describe_canvas()

    def set_heatmap(self, x, y, values, *, fit=False):
        """Set a spectrogram with time columns and frequency rows."""
        x, y, values = (np.asarray(item, dtype=float) for item in (x, y, values))
        if not x.size or not y.size:
            self.clear()
            return
        if x.ndim != 1 or y.ndim != 1 or values.shape != (y.size, x.size):
            raise ValueError("A heatmap must have one value for each pair of x and y coordinates.")
        if not all(np.isfinite(axis).all() and np.all(axis[1:] > axis[:-1]) for axis in (x, y)):
            raise ValueError("Heatmap coordinates must be finite and in ascending order.")
        if self.log_x and x[0] <= 0:
            raise ValueError("A logarithmic heatmap starts above zero.")
        type_changed = self._heatmap is None
        if type_changed:
            self._reset_interaction()
        self._all_series = self._series = []
        self._heatmap = (x, y, values)
        self._heat_edges = cell_edges(np.log10(x) if self.log_x else x), cell_edges(y)
        self._data_xlim, self._data_ylim = ((float(edges[0]), float(edges[-1])) for edges in self._heat_edges)
        if type_changed:
            self._describe_canvas()
        self._prepare_data_view(fit, "heatmap")
        self._recolor_heatmap()
        peak = self._heat_peak(values, y)
        time_unit = "time point" if x.size == 1 else "time points"
        frequency_unit = "frequency" if y.size == 1 else "frequencies"
        self._summary = f"{x.size} {time_unit}. {y.size} {frequency_unit}. {peak}"
        if not np.isfinite(values[:, -1]).any():
            self._summary += " No level measurement in the latest column."
        self._refresh()

    @staticmethod
    def _heat_peak(values, frequencies):
        finite = np.isfinite(values)
        if not finite.any():
            return "No level measurements."
        row, col = np.unravel_index(np.argmax(np.where(finite, values, -np.inf)), values.shape)
        return f"Peak {values[row, col]:.1f} dB at {frequencies[row]:.5g} Hz."

    def _prepare_data_view(self, fit, kind):
        signature = self.title, self.x_label, self.y_label, self.log_x, kind
        if fit and signature != self._fit_signature:
            # Many live plots request auto sizing on each update. Once the
            # user pans or zooms a specific view, retain it on later frames.
            self._xlim = self._ylim = None
            self._prior_views.clear()
            if self._fit_signature is not None:
                self._selection = self._hover = None
                self._markers.clear()
        self._fit_signature = signature

    def _apply_hidden_traces(self):
        shown = [trace for trace in self._all_series if trace.key not in self._hidden_traces]
        if not shown and self._all_series:
            self._hidden_traces.clear()
            shown = self._all_series
        self._series = shown
        self._cache_traces()
        self._refresh()

    def _cache_traces(self):
        if not self._series:
            self._data_xlim, self._data_ylim = (0, 1), (0, 1)
            self._summary = "No plot data."
            return
        self._data_xlim = self._range([trace.plot_x for trace in self._series])
        self._data_ylim = self._range([trace.y for trace in self._series])
        trace = self._series[0]
        extra = f"{len(self._series)} visible traces. " if len(self._series) > 1 else ""
        label = f"{trace.label}. " if trace.label else ""
        point_unit = "point" if trace.x.size == 1 else "points"
        self._summary = (f"{extra}{label}{trace.x.size} {point_unit}. "
                         f"Range {np.min(trace.y):.5g} to {np.max(trace.y):.5g}.")

    def _recolor_heatmap(self):
        # Preserve raw dB measurements for inspection and exports.
        self._heat_pixels = heat_image(self._heatmap[2], self.appearance)
        self._bitmap = None
        self._scale_bitmap = None

    def clear(self):
        self._reset_interaction()
        self.set_series([])

    def fit(self):
        if self._xlim is not None or self._ylim is not None:
            self._remember_view()
        self._xlim = self._ylim = None
        self._bitmap = None
        self.canvas.Refresh(False)

    def _reset_interaction(self):
        self._xlim = self._ylim = None
        self._selection = self._hover = None
        self._fit_signature = None
        self._markers.clear()
        self._prior_views.clear()
        self._hidden_traces.clear()
        self._bitmap = None

    def _remember_view(self):
        view = self._xlim, self._ylim
        if not self._prior_views or self._prior_views[-1] != view:
            self._prior_views.append(view)
            del self._prior_views[:-30]

    def _previous_view(self):
        if self._prior_views:
            self._xlim, self._ylim = self._prior_views.pop()
            self._bitmap = None
            self.canvas.Refresh(False)

    def _edit_limits(self):
        if self._point() is None:
            return
        with wx.Dialog(self, title="Axis Limits") as dialog:
            layout = wx.BoxSizer(wx.VERTICAL)
            fields = wx.FlexGridSizer(cols=2, vgap=self.FromDIP(6), hgap=self.FromDIP(10))
            fields.AddGrowableCol(1)
            horizontal, vertical = self._bounds()
            editors = []
            for label, value in zip(("Horizontal minimum", "Horizontal maximum", "Vertical minimum", "Vertical maximum"),
                                    (*map(self._physical_x, horizontal), *vertical), strict=True):
                axis = self.x_label if label.startswith("Horizontal") else self.y_label
                text = wx.StaticText(dialog, label=label)
                editor = wx.TextCtrl(dialog, value=f"{value:.12g}", name=label)
                set_control_help(text, f"Set the {label.lower()} in {axis or 'plot'} units.")
                set_control_help(editor, f"Enter the {label.lower()} in {axis or 'plot'} units.")
                fields.Add(text, 0, wx.ALIGN_CENTER_VERTICAL)
                fields.Add(editor, 1, wx.EXPAND)
                editors.append(editor)
            layout.Add(fields, 1, wx.EXPAND | wx.ALL, self.FromDIP(16))
            layout.Add(dialog.CreateSeparatedButtonSizer(wx.OK | wx.CANCEL), 0, wx.EXPAND | wx.ALL, self.FromDIP(8))
            dialog.SetSizerAndFit(layout)
            dialog.CentreOnParent()
            editors[0].SetFocus()
            while dialog.ShowModal() == wx.ID_OK:
                try:
                    numbers = [float(editor.GetValue()) for editor in editors]
                    xlim = self._checked_limits(numbers[:2], logarithmic=self.log_x)
                    ylim = self._checked_limits(numbers[2:])
                    if self._set_viewport(xlim, ylim):
                        break
                except ValueError:
                    pass
                description = "Enter a finite minimum and maximum for each axis. Each minimum must be below its maximum."
                if self.log_x:
                    description += " Both horizontal limits must be above zero."
                wx.MessageBox(description, "Invalid axis limits", wx.OK | wx.ICON_WARNING, dialog)

    def _choose_traces(self):
        if len(self._all_series) < 2:
            return
        traces = tuple(self._all_series)
        labels = [trace.label or f"Trace {i + 1}" for i, trace in enumerate(traces)]
        # A native checklist exposes every trace and its checked state.
        with wx.MultiChoiceDialog(self, "Choose the traces to show.", "Visible Traces", labels) as dialog:
            dialog.SetSelections([i for i, trace in enumerate(traces) if trace.key not in self._hidden_traces])
            if dialog.ShowModal() != wx.ID_OK:
                return
            selected = dialog.GetSelections()
            if not selected:
                wx.MessageBox("Select at least one trace to show.", "Visible Traces", wx.OK | wx.ICON_INFORMATION, self)
                return
            self._show_traces({traces[i].key for i in selected})

    def _show_traces(self, keys):
        if not keys:
            return
        self._hidden_traces = {trace.key for trace in self._all_series if trace.key not in keys}
        self._selection = self._hover = None
        self._apply_hidden_traces()

    def _toggle_legend(self, key, *, isolate=False):
        keys = {trace.key for trace in self._series}
        if isolate:
            keys = {trace.key for trace in self._all_series} if keys == {key} else {key}
        else:
            keys ^= {key}
        self._show_traces(keys)

    def _checked_limits(self, limits, *, logarithmic=False):
        if limits is None:
            return None
        low, high = map(float, limits)
        if not np.isfinite((low, high)).all() or low >= high or (logarithmic and low <= 0):
            raise ValueError("Invalid plot limits.")
        return (log10(low), log10(high)) if logarithmic else (low, high)

    def set_view(self, xlim=None, ylim=None):
        if xlim is not None:
            self._xlim = self._checked_limits(xlim, logarithmic=self.log_x)
        if ylim is not None:
            self._ylim = self._checked_limits(ylim)
        self._bitmap = None
        self.canvas.Refresh(False)

    def set_default_view(self, xlim=None, ylim=None):
        """Set the preferred viewport restored by Fit Plot."""
        self._default_xlim = self._checked_limits(xlim, logarithmic=self.log_x)
        self._default_ylim = self._checked_limits(ylim)
        self._bitmap = None
        self.canvas.Refresh(False)

    def set_grid_steps(self, x_step=None, y_step=None):
        """Set fixed physical graticule spacing without changing cursor units."""
        if any(step is not None and (not np.isfinite(step) or step <= 0) for step in (x_step, y_step)):
            raise ValueError("Graticule divisions must be positive and finite.")
        self.grid_steps = x_step, y_step
        self.canvas.Refresh(False)

    def _refresh(self, *, force_readout=False):
        self._update_readout(force=force_readout)
        self.canvas.Refresh(False)

    @staticmethod
    def _range(values, pad=0.04):
        low = min(float(np.min(value)) for value in values)
        high = max(float(np.max(value)) for value in values)
        extra = (high - low) * pad
        if high == low:
            extra = max(abs(high) * 0.02, 0.01)
        return low - extra, high + extra

    def _bounds(self):
        x = self._xlim or self._default_xlim or self._data_xlim
        y = self._ylim or self._default_ylim or self._data_ylim
        if self.square_axes:
            # Equal coordinate scales are essential when reading XY phase.
            return equal_axis_spans(x, y)
        return x, y

    def _physical_x(self, value):
        return 10 ** value if self.log_x else value

    def snapshot(self):
        """Freeze the current presentation and keep unrounded source coordinates."""
        if self._point() is None:
            return None
        xlim, ylim = self._bounds()
        return {
            "title": self.title, "x_label": self.x_label, "y_label": self.y_label,
            "log_x": self.log_x, "square_axes": self.square_axes,
            "x_ticks": tuple((self._physical_x(x), label) for x, label in self.x_ticks) if self.x_ticks else None,
            "grid_steps": self.grid_steps,
            "mark_time_origin": self.mark_time_origin,
            "traces": tuple({"label": t.label, "color": t.color, "style": t.style, "palette_index": t.slot,
                             "x": t.raw[0], "y": t.raw[1]} for t in self._series),
            "heatmap": self._heatmap,
            "xlim": tuple(map(self._physical_x, xlim)), "ylim": ylim,
            "full_xlim": tuple(map(self._physical_x, self._data_xlim)), "full_ylim": self._data_ylim,
            "markers": tuple({"label": label, "x": p.x, "y": p.y, "level": p.level}
                             for label, p in sorted(self._markers.items())),
            "style": self.appearance, "system_dark": wx.SystemSettings.GetAppearance().IsDark(),
            "width": self.canvas.GetClientSize().width, "height": self.canvas.GetClientSize().height,
        }

    def _plot_rect(self):
        width, height = self.canvas.GetClientSize()
        margin = max(0, self.canvas.GetCharHeight() - self.FromDIP(14))
        right = 79 if self._heatmap is not None else 26
        left, right, top, bottom = (self.FromDIP(n) + margin for n in (82, right, 29, 43))
        chart_width = max(1, width - left - right)
        legend_width = min(width - self.FromDIP(18), max(chart_width, self.FromDIP(400))) if self.square_axes else chart_width
        _, rows = self._legend_layout(legend_width, height)
        top += rows * (self.canvas.GetCharHeight() + self.FromDIP(5))
        chart_height = max(1, height - top - bottom)
        if self.square_axes:
            # Equal scales preserve the angle and aspect of stereo phase traces.
            side = min(chart_width, chart_height)
            left += (chart_width - side) // 2
            top += (chart_height - side) // 2
            chart_width = chart_height = side
        return wx.Rect(left, top, chart_width, chart_height)

    def _legend_layout(self, width, height):
        total = len(self._all_series)
        if self.appearance.legend == "hidden" or not total or (total == 1 and self.appearance.legend == "auto"):
            return 0, 0
        columns = min(total, max(1, width // self.FromDIP(136)), 4)
        row_height = self.canvas.GetCharHeight() + self.FromDIP(5)
        rows = min((total + columns - 1) // columns, max(1, (height - self.FromDIP(105)) // row_height), 3)
        return columns, rows

    def _data_at(self, point):
        box = self._plot_rect()
        (left, right), (bottom, top) = self._bounds()
        return (left + (point.x - box.x + 0.5) / box.width * (right - left),
                top - (point.y - box.y + 0.5) / box.height * (top - bottom))

    def _point(self, selection=None):
        if not self._series and self._heatmap is None:
            return None
        trace_index, index = selection or self._selection or (0, 0)
        if self._heatmap is not None:
            x, y, values = self._heatmap
            trace_index = trace_index % y.size
            index = max(0, min(index, x.size - 1))
            val_x, val_y = float(x[index]), float(y[trace_index])
            value = values[trace_index, index]
            label = f"Level {value:.1f} dB" if np.isfinite(value) else "No level in this bin"
            return trace_index, index, val_x, val_y, label
        trace_index = trace_index % len(self._series)
        trace = self._series[trace_index]
        index = max(0, min(index, trace.x.size - 1))
        return trace_index, index, float(trace.x[index]), float(trace.y[index]), trace.label

    def _format(self, x, y):
        return ((self.x_formatter(x) if self.x_formatter else f"{x:.9g}"),
                (self.y_formatter(y) if self.y_formatter else f"{y:.9g}"))

    def _marker_point(self, point):
        trace, index, x, y, label = point
        level = None
        if self._heatmap is not None:
            value = self._heatmap[2][trace, index]
            level = float(value) if np.isfinite(value) else None
            label = "Spectrogram"
        return _Marker(x, y, label, level)

    def _set_marker(self, name, selection=None):
        existing = selection or self._hover or self._selection
        if existing is None:
            box = self._plot_rect()
            existing = self._nearest(wx.Point(box.x + box.width // 2, box.y + box.height // 2))
        point = self._point(existing) if existing is not None else None
        if point is not None:
            self._markers[name] = self._marker_point(point)
            self._refresh(force_readout=True)

    def _clear_markers(self):
        self._markers.clear()
        self._refresh(force_readout=True)

    def _marker_text(self):
        info = []
        for name, marker in sorted(self._markers.items()):
            x, y = self._format(marker.x, marker.y)
            trace = f" {marker.trace}." if marker.trace else ""
            level = f" Level {marker.level:.7g} dB." if marker.level is not None else ""
            info.append(f"Marker {name}.{trace} {self.x_label} {x}. {self.y_label} {y}.{level}")
        if len(self._markers) == 2:
            a, b = self._markers["A"], self._markers["B"]
            x_label = "horizontal steps" if self.x_ticks else self.x_label or "horizontal"
            level = f" Level {b.level - a.level:+.7g} dB." if a.level is not None and b.level is not None else ""
            info.insert(0, f"B minus A. {x_label} {b.x - a.x:+.9g}. {self.y_label} {b.y - a.y:+.9g}.{level}")
        return "\n".join(info)

    def _update_readout(self, *, force=False):
        if self.readout.HasFocus() and not force:
            return
        point = self._point(self._hover)
        if point is None:
            info = self._empty_message
        elif self._selection is None and self._hover is None:
            if not self._expanded_descriptions:
                info = "Use Previous Point or Next Point to read plot values."
            elif self._heatmap is not None:
                info = f"{self._summary} Colour scale −{self.appearance.heat_range} to 0 dB."
            else:
                info = self._summary
        else:
            _, index, x, y, label = point
            x_text, y_text = self._format(x, y)
            label = f"{label}. " if label else ""
            info = f"{label}Point {index + 1}. {self.x_label} {x_text}. {self.y_label} {y_text}."
        if self._markers:
            info = f"{self._marker_text()}\n{info}"
        # Updating a read-only text control keeps the exact value available to
        # assistive technology without speaking each frame of a live trace.
        if self.readout.GetValue() != info:
            self.readout.ChangeValue(info)

    def _move_point(self, delta, *, fast=False, end=False):
        point = self._point()
        if point is None:
            return
        trace, index, *_ = point
        self._hover = None
        length = self._heatmap[0].size if self._heatmap is not None else self._series[trace].x.size
        if end:
            index = length - 1 if delta > 0 else 0
        elif fast:
            index += delta * max(1, length // 20)
        elif self._selection is not None:
            index += delta
        self._selection = (trace, index)
        selected = self._point()
        self._selection = selected[0:2]
        self._reveal(selected)
        self._refresh(force_readout=True)

    def _move_trace(self, delta):
        point = self._point()
        if point is not None:
            trace, index, x, *_ = point
            self._hover = None
            updated = trace + delta
            if self._heatmap is None:
                updated %= len(self._series)
                series = self._series[updated]
                index = self._nearest_x(series.x, x, series.ordered)
            self._selection = (updated, index)
            self._reveal(self._point())
            self._refresh(force_readout=True)

    @staticmethod
    def _nearest_x(values, target, ordered=True):
        if not ordered:
            return int(np.argmin(abs(values - target)))
        right = min(int(np.searchsorted(values, target)), values.size - 1)
        left = max(0, right - 1)
        return right if abs(values[right] - target) < abs(values[left] - target) else left

    def _reveal(self, point):
        _, _, x, y, _ = point
        if self.log_x:
            x = log10(x)
        (left, right), (bottom, top) = self._bounds()
        # Move just far enough to leave a little space around the keyboard
        # cursor when it travels beyond the current zoomed viewport.
        for value, low, high, axis in ((x, left, right, "_xlim"), (y, bottom, top, "_ylim")):
            span = high - low
            if value < low:
                setattr(self, axis, (value - span * 0.08, value + span * 0.92))
            elif value > high:
                setattr(self, axis, (value - span * 0.92, value + span * 0.08))
        self._bitmap = None

    def _resize(self, event):
        self._bitmap = None
        self._scale_bitmap = None
        self._legend_items = []
        self.canvas.Refresh(False)
        event.Skip()

    def _capture_lost(self, _event):
        self._drag = None
        self._zoom_rect = None
        self._set_mouse_cursor(wx.CURSOR_DEFAULT)
        self.canvas.Refresh(False)

    def _mouse_leave(self, event):
        if self._hover is not None:
            self._hover = None
            self._refresh()
        if self._drag is None:
            self._set_mouse_cursor(wx.CURSOR_DEFAULT)
        event.Skip()

    def _set_mouse_cursor(self, cursor):
        if self._mouse_cursor != cursor:
            self._mouse_cursor = cursor
            self.canvas.SetCursor(wx.Cursor(cursor))

    def _mouse_down(self, event):
        self.canvas.SetFocus()
        self._hover = None
        point = event.GetPosition()
        for rect, key in self._legend_items:
            if rect.Contains(point):
                if key is None:
                    self._choose_traces()
                else:
                    self._toggle_legend(key, isolate=event.ShiftDown())
                return
        if self._plot_rect().Contains(point) and self._point() is not None:
            if event.CmdDown() or event.AltDown():
                selection = self._nearest(point)
                if selection is not None:
                    self._set_marker("B", selection)
                return
            self._drag = _Drag(point, self._bounds(), event.ShiftDown())
            self.canvas.CaptureMouse()

    def _mouse_move(self, event):
        position = event.GetPosition()
        if self._drag is None:
            point = self._nearest(position)
            legend = any(rect.Contains(position) for rect, _key in self._legend_items)
            self._set_mouse_cursor(wx.CURSOR_HAND if legend else wx.CURSOR_CROSS if point is not None else wx.CURSOR_DEFAULT)
            if point != self._hover:
                self._hover = point
                self._refresh()
            return
        if not event.Dragging():
            return
        drag = self._drag
        origin, ((left, right), (bottom, top)) = drag.origin, drag.bounds
        dx, dy = position.x - origin.x, position.y - origin.y
        if not drag.moved and max(abs(dx), abs(dy)) < self.FromDIP(4):
            return
        box = self._plot_rect()
        self._set_mouse_cursor(wx.CURSOR_CROSS if drag.zoom else wx.CURSOR_SIZING)
        if drag.zoom:
            x = max(box.x, min(position.x, box.GetRight()))
            y = max(box.y, min(position.y, box.GetBottom()))
            self._zoom_rect = wx.Rect(min(x, origin.x), min(y, origin.y), abs(x - origin.x) + 1, abs(y - origin.y) + 1)
            self.canvas.Refresh(False)
        else:
            shift_x = -dx / box.width * (right - left)
            shift_y = dy / box.height * (top - bottom)
            self._set_viewport((left + shift_x, right + shift_x), (bottom + shift_y, top + shift_y),
                               remember=not drag.moved)
        drag.moved = True

    def _mouse_up(self, event):
        if self._drag is None:
            return
        drag = self._drag
        self._drag = None
        if self.canvas.HasCapture():
            self.canvas.ReleaseMouse()
        inside = self._plot_rect().Contains(event.GetPosition())
        self._set_mouse_cursor(wx.CURSOR_CROSS if inside else wx.CURSOR_DEFAULT)
        zoom_rect, self._zoom_rect = self._zoom_rect, None
        if not drag.moved:
            selection = self._nearest(drag.origin)
            if selection is not None:
                self._selection = selection
                if drag.zoom:
                    self._set_marker("A", selection)
                else:
                    self._refresh()
        elif zoom_rect is not None:
            left, top = self._data_at(zoom_rect.GetTopLeft())
            right, bottom = self._data_at(zoom_rect.GetBottomRight())
            # Allow a deliberately horizontal or vertical drag to zoom one axis.
            horizontal = (left, right) if zoom_rect.width > self.FromDIP(4) else None
            vertical = (bottom, top) if zoom_rect.height > self.FromDIP(4) else None
            if self.square_axes and (horizontal is None or vertical is None):
                # A one-axis gesture on an XY display uses the same factor on both axes.
                x_bounds, y_bounds = self._bounds()
                horizontal, vertical = horizontal or x_bounds, vertical or y_bounds
                size = min(horizontal[1] - horizontal[0], vertical[1] - vertical[0])
                horizontal, vertical = ((sum(axis) / 2 - size / 2, sum(axis) / 2 + size / 2)
                                        for axis in (horizontal, vertical))
            self._set_viewport(horizontal, vertical)
            self.canvas.Refresh(False)

    def _nearest(self, point):
        """Hit-test data in viewport coordinates so either axis can be zoomed."""
        if not self._plot_rect().Contains(point):
            return None
        x, y = self._data_at(point)
        if self._heatmap is not None:
            x_edges, y_edges = self._heat_edges
            column = int(np.searchsorted(x_edges, x, side="left")) - 1
            row = int(np.searchsorted(y_edges, y, side="left")) - 1
            return (row, column) if 0 <= row < len(y_edges) - 1 and 0 <= column < len(x_edges) - 1 else None
        if not self._series:
            return None
        (left, right), (bottom, top) = self._bounds()
        box = self._plot_rect()
        x_pixels, y_pixels = box.width / (right - left), box.height / (top - bottom)
        best_distance, best = float("inf"), None
        for slot, trace in enumerate(self._series):
            start, stop = 0, trace.x.size
            if trace.ordered:
                # No point outside this horizontal radius can improve the
                # current best pixel distance, however large the full trace.
                radius = best_distance ** 0.5 / x_pixels
                start = int(np.searchsorted(trace.plot_x, max(left, x - radius), side="left"))
                stop = int(np.searchsorted(trace.plot_x, min(right, x + radius), side="right"))
                if best is None and start < stop:
                    index = start + self._nearest_x(trace.plot_x[start:stop], x)
                    sample = trace.y[index]
                    if bottom <= sample <= top:
                        best_distance = ((trace.plot_x[index] - x) * x_pixels) ** 2 + ((sample - y) * y_pixels) ** 2
                        best = (slot, index)
                        radius = best_distance ** 0.5 / x_pixels
                        start = max(start, int(np.searchsorted(trace.plot_x, x - radius, side="left")))
                        stop = min(stop, int(np.searchsorted(trace.plot_x, x + radius, side="right")))
            if start >= stop:
                continue
            xs, ys = trace.plot_x[start:stop], trace.y[start:stop]
            distances = ((xs - x) * x_pixels) ** 2 + ((ys - y) * y_pixels) ** 2
            visible = (ys >= bottom) & (ys <= top)
            if not trace.ordered:
                visible &= (xs >= left) & (xs <= right)
            distances[~visible] = np.inf
            index = int(np.argmin(distances))
            if distances[index] < best_distance:
                best_distance = float(distances[index])
                best = (slot, start + index)
        return best

    def _zoom(self, factor, center=None, *, axis=None):
        if self._point() is None:
            return
        (left, right), (bottom, top) = self._bounds()
        x, y = center or ((left + right) / 2, (bottom + top) / 2)
        horizontal = (x + (left - x) * factor, x + (right - x) * factor)
        vertical = (y + (bottom - y) * factor, y + (top - y) * factor)
        if not self.square_axes:
            horizontal = None if axis == "y" else horizontal
            vertical = None if axis == "x" else vertical
        self._set_viewport(horizontal, vertical)

    def _pan(self, x_fraction, y_fraction):
        if self._point() is None:
            return
        (left, right), (bottom, top) = self._bounds()
        dx, dy = (right - left) * x_fraction, (top - bottom) * y_fraction
        self._set_viewport((left + dx, right + dx) if x_fraction else None,
                           (bottom + dy, top + dy) if y_fraction else None)

    def _set_viewport(self, horizontal, vertical, *, remember=True):
        # Bound pathological navigation values before tick calculations and
        # bitmap coordinates. The ranges are far beyond any supported signal.
        limit = 250 if self.log_x else 1e12
        for bounds, maximum in ((horizontal, limit), (vertical, 1e12)):
            if bounds is not None:
                low, high = bounds
                if not (-maximum < low < high < maximum and high - low >= 1e-12):
                    return False
        if remember:
            self._remember_view()
        if horizontal is not None:
            self._xlim = horizontal
        if vertical is not None:
            self._ylim = vertical
        self._bitmap = None
        self.canvas.Refresh(False)
        return True

    def _wheel(self, event):
        if (not self._plot_rect().Contains(event.GetPosition()) or not event.GetWheelRotation()
                or self._point() is None or self._drag is not None):
            event.Skip()
            return
        if event.GetWheelAxis() == wx.MOUSE_WHEEL_HORIZONTAL:
            self._pan(-event.GetWheelRotation() / max(1, event.GetWheelDelta()) * 0.1, 0)
            return
        steps = np.clip(event.GetWheelRotation() / max(1, event.GetWheelDelta()), -8, 8)
        axis = "x" if event.ShiftDown() else "y" if event.CmdDown() or event.AltDown() else None
        self._zoom(0.8 ** steps, self._data_at(event.GetPosition()), axis=axis)

    def _key(self, event):
        key = event.GetKeyCode()
        if self._is_menu_key(event):
            self._plot_menu()
        elif event.CmdDown() or event.AltDown():
            event.Skip()
        elif event.ShiftDown() and key in (wx.WXK_LEFT, wx.WXK_RIGHT, wx.WXK_UP, wx.WXK_DOWN):
            self._pan(*{wx.WXK_LEFT: (-0.2, 0), wx.WXK_RIGHT: (0.2, 0),
                        wx.WXK_UP: (0, 0.2), wx.WXK_DOWN: (0, -0.2)}[key])
        elif key == wx.WXK_LEFT:
            self._move_point(-1)
        elif key == wx.WXK_RIGHT:
            self._move_point(1)
        elif key in (wx.WXK_UP, wx.WXK_DOWN):
            self._move_trace(1 if key == wx.WXK_UP else -1)
        elif key in (wx.WXK_PAGEUP, wx.WXK_PAGEDOWN):
            self._move_point(1 if key == wx.WXK_PAGEDOWN else -1, fast=True)
        elif key == wx.WXK_END:
            self._move_point(1, end=True)
        elif key == wx.WXK_BACK and self._prior_views:
            self._previous_view()
        elif key in (ord("+"), ord("="), wx.WXK_NUMPAD_ADD):
            self._zoom(0.65)
        elif key in (ord("-"), wx.WXK_NUMPAD_SUBTRACT):
            self._zoom(1.55)
        elif key == wx.WXK_HOME:
            self._move_point(-1, end=True) if event.ShiftDown() else self.fit()
        elif key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            self.focus_data()
        elif key in (ord("A"), ord("B"), ord("a"), ord("b")):
            self._set_marker(chr(key).upper())
        elif key in (ord("V"), ord("v")):
            self._edit_limits()
        elif key == wx.WXK_ESCAPE and self._markers:
            self._clear_markers()
        else:
            event.Skip()

    def _paint(self, _event):
        dc = wx.AutoBufferedPaintDC(self.canvas)
        colors = self.appearance.colors(wx.SystemSettings.GetAppearance().IsDark())
        panel = wx.Colour(*colors.panel)
        foreground = wx.Colour(*colors.text)
        divider = wx.Colour(*colors.axis)
        dc.SetBackground(wx.Brush(wx.Colour(*colors.background)))
        dc.Clear()
        dc.SetTextForeground(foreground)
        font = self.canvas.GetFont()
        box = self._plot_rect()
        (left, right), (bottom, top) = self._bounds()

        def px(value):
            position = box.x + (value - left) * box.width / (right - left)
            return round(max(-100_000, min(100_000, position)))

        def py(value):
            position = box.y + box.height - (value - bottom) * box.height / (top - bottom)
            return round(max(-100_000, min(100_000, position)))

        decoration = box
        if self.square_axes:
            # A short XY view must keep a square coordinate box. Let its title
            # and legend use the otherwise empty horizontal canvas space.
            canvas_width = self.canvas.GetClientSize().width
            x_padding = self.FromDIP(9)
            decoration_width = max(1, min(canvas_width - 2 * x_padding, max(box.width, self.FromDIP(400))))
            decoration_x = max(self.FromDIP(9), min(canvas_width - decoration_width - self.FromDIP(9),
                               box.x + (box.width - decoration_width) // 2))
            decoration = wx.Rect(decoration_x, box.y, decoration_width, box.height)
        dc.SetFont(font)
        horizontal_y = bool(self.y_label and dc.GetTextExtent(self.y_label).width > box.height)
        self._draw_heading(dc, decoration, colors, horizontal_y)
        self._draw_legend(dc, decoration, colors)
        dc.SetPen(wx.Pen(divider))
        dc.SetBrush(wx.Brush(panel))
        dc.DrawRectangle(box)
        if self._point() is None:
            dc.SetTextForeground(wx.Colour(*colors.muted))
            inset = self.FromDIP(8)
            message = wx.Control.Ellipsize(self._empty_message, dc, wx.ELLIPSIZE_END, max(1, box.width - 2 * inset))
            dc.DrawText(message, box.x + inset, box.y + inset)
            self._draw_focus(dc, colors)
            return
        dc.SetClippingRegion(box)
        if self._heatmap is not None:
            self._draw_heatmap(dc, box, (left, right), (bottom, top))
        dc.DestroyClippingRegion()
        self._draw_axes(dc, box, (left, right), (bottom, top), px, py, colors, horizontal_y)
        dc.SetClippingRegion(box)
        for trace in self._series:
            self._draw_trace(dc, box, trace, colors.panel, px, py, (left, right), bottom)
        for name, marker in sorted(self._markers.items()):
            self._draw_cursor(dc, box, px(log10(marker.x) if self.log_x else marker.x), py(marker.y), colors, name)
        if self._selection is not None or self._hover is not None:
            _, _, x, y, _label = self._point(self._hover)
            self._draw_cursor(dc, box, px(log10(x) if self.log_x else x), py(y), colors)
        if self._zoom_rect is not None:
            dc.SetPen(wx.Pen(panel, self.FromDIP(3)))
            dc.SetBrush(wx.TRANSPARENT_BRUSH)
            dc.DrawRectangle(self._zoom_rect)
            dc.SetPen(wx.Pen(wx.Colour(*colors.accent), self.FromDIP(2), wx.PENSTYLE_SHORT_DASH))
            dc.DrawRectangle(self._zoom_rect)
        dc.DestroyClippingRegion()
        if self._heatmap is not None:
            self._draw_color_scale(dc, box, divider)
        self._draw_focus(dc, colors)

    def _draw_focus(self, dc, colors):
        width, height = self.canvas.GetClientSize()
        if self.canvas.HasFocus() and width > 2 and height > 2:
            dc.SetPen(wx.Pen(wx.Colour(*colors.accent), self.FromDIP(2)))
            dc.SetBrush(wx.TRANSPARENT_BRUSH)
            dc.DrawRectangle(1, 1, width - 2, height - 2)

    def _draw_heading(self, dc, box, colors, horizontal_y):
        width = box.width
        y = self.FromDIP(4)
        font = dc.GetFont()
        if horizontal_y:
            # A short graph may not fit a vertical axis label. Use the top
            # margin to keep the physical units visible at useful window sizes.
            maximum = max(1, int(width * 0.46)) if self.title else width
            label = wx.Control.Ellipsize(self.y_label, dc, wx.ELLIPSIZE_END, maximum)
            extent = dc.GetTextExtent(label).width
            dc.SetTextForeground(wx.Colour(*colors.muted))
            dc.DrawText(label, box.GetRight() - extent + 1, y)
            dc.SetTextForeground(wx.Colour(*colors.text))
            width = max(1, width - extent - self.FromDIP(12))
        dc.SetFont(font.Bold())
        title = wx.Control.Ellipsize(self.title, dc, wx.ELLIPSIZE_END, width)
        x = box.x
        if self.square_axes and not horizontal_y:
            x += (width - dc.GetTextExtent(title).width) // 2
        dc.DrawText(title, x, y)
        dc.SetFont(font)

    def _draw_cursor(self, dc, box, x, y, colors, name=None):
        if not (box.x <= x <= box.GetRight() + 1 and box.y <= y <= box.GetBottom() + 1):
            return
        color = wx.Colour(*(colors.accent if name else colors.cursor))
        contrast = wx.Colour(*colors.panel)
        if self._heatmap is not None:
            color, contrast = wx.WHITE, wx.Colour(25, 29, 35)
        dot = self.FromDIP(4 if name else 5)
        if name or self._heatmap is None:
            if self._heatmap is not None:
                dc.SetPen(wx.Pen(contrast, self.FromDIP(2)))
                dc.DrawLine(x, box.y, x, box.GetBottom())
            dc.SetPen(wx.Pen(color, 1, wx.PENSTYLE_SHORT_DASH if name else wx.PENSTYLE_DOT))
            dc.DrawLine(x, box.y, x, box.GetBottom())
            if not name:
                dc.DrawLine(box.x, y, box.GetRight(), y)
        dc.SetBrush(wx.TRANSPARENT_BRUSH)
        dc.SetPen(wx.Pen(contrast, self.FromDIP(4)))
        dc.DrawCircle(x, y, dot)
        dc.SetPen(wx.Pen(color, self.FromDIP(2)))
        dc.DrawCircle(x, y, dot)
        if name:
            old_font = dc.GetFont()
            dc.SetFont(old_font.Bold())
            label = dc.GetTextExtent(name)
            label_x = min(box.GetRight() - label.width - self.FromDIP(13), x + self.FromDIP(9))
            label_y = box.y + self.FromDIP(5 if name == "A" else 24)
            outline = wx.Colour(*colors.accent)
            dc.SetBrush(wx.Brush(wx.Colour(*colors.panel)))
            dc.SetPen(wx.Pen(outline))
            dc.DrawRoundedRectangle(label_x, label_y, label.width + self.FromDIP(10), label.height + self.FromDIP(4),
                                    self.FromDIP(3))
            dc.SetTextForeground(wx.Colour(*colors.text))
            dc.DrawText(name, label_x + self.FromDIP(5), label_y + self.FromDIP(2))
            dc.SetFont(old_font)

    def _draw_axes(self, dc, box, x_bounds, y_bounds, px, py, colors, horizontal_y):
        width, _height = self.canvas.GetClientSize()
        x_count = max(2, min(8, box.width // self.FromDIP(84) + 1))
        y_count = max(2, min(8, box.height // max(self.FromDIP(29), dc.GetCharHeight() + self.FromDIP(8)) + 1))
        if self.x_ticks is None:
            x_labels = axis_ticks(*x_bounds, x_count, logarithmic=self.log_x, step=self.grid_steps[0])
        else:
            labels = [(value, label) for value, label in self.x_ticks if x_bounds[0] <= value <= x_bounds[1]]
            # Keep ordered category labels readable at small window sizes.
            # The accessible point readout still uses the full setting label.
            space = max(self.FromDIP(36), box.width // max(1, min(x_count, len(labels))) - self.FromDIP(10))
            x_labels = [(value, wx.Control.Ellipsize(label, dc, wx.ELLIPSIZE_END, space)) for value, label in labels]
        y_labels = axis_ticks(*y_bounds, y_count, step=self.grid_steps[1])
        show_grid = self.appearance.grid and self._heatmap is None
        if show_grid and self.appearance.grid_detail == "minor":
            self._draw_minor_grid(dc, box, x_bounds, x_labels, y_labels, px, py, colors)
        dc.SetPen(wx.Pen(wx.Colour(*colors.grid)))
        dc.SetTextForeground(wx.Colour(*colors.muted))
        x_labels_every = self._graticule_label_period(
            self.grid_steps[0], x_bounds, box.width,
            max((dc.GetTextExtent(label).width for _, label in x_labels), default=0) + self.FromDIP(7),
        )
        y_labels_every = self._graticule_label_period(
            self.grid_steps[1], y_bounds, box.height, dc.GetCharHeight() + self.FromDIP(2),
        )
        last_end = -1
        for value, label in x_labels:
            label_width = dc.GetTextExtent(label).width
            x = px(value)
            label_left = max(0, min(width - label_width, x - label_width // 2))
            if label_left > last_end and self._on_graticule_label(value, x_labels_every):
                dc.DrawText(label, label_left, box.GetBottom() + self.FromDIP(5))
                last_end = label_left + label_width + self.FromDIP(6)
            if show_grid:
                dc.DrawLine(x, box.y, x, box.GetBottom())
        next_label = box.GetBottom() + dc.GetCharHeight()
        for value, label in y_labels:
            extent = dc.GetTextExtent(label)
            y = py(value)
            if y <= next_label and self._on_graticule_label(value, y_labels_every):
                dc.DrawText(label, max(self.FromDIP(18), box.x - extent.width - self.FromDIP(7)),
                            y - extent.height // 2)
                next_label = y - extent.height - self.FromDIP(2)
            if show_grid:
                dc.DrawLine(box.x, y, box.GetRight(), y)
        dc.SetPen(wx.Pen(wx.Colour(*colors.axis)))
        if show_grid and y_bounds[0] < 0 < y_bounds[1]:
            dc.DrawLine(box.x, py(0), box.GetRight(), py(0))
        if show_grid and self.square_axes and x_bounds[0] < 0 < x_bounds[1]:
            dc.DrawLine(px(0), box.y, px(0), box.GetBottom())
        if self.mark_time_origin and x_bounds[0] < 0 < x_bounds[1]:
            dc.SetPen(wx.Pen(wx.Colour(*colors.accent), style=wx.PENSTYLE_SHORT_DASH))
            dc.DrawLine(px(0), box.y, px(0), box.GetBottom())
            dc.SetPen(wx.Pen(wx.Colour(*colors.axis)))
        dc.SetBrush(wx.TRANSPARENT_BRUSH)
        dc.DrawRectangle(box)
        dc.SetTextForeground(wx.Colour(*colors.text))
        if self.x_label:
            label = wx.Control.Ellipsize(self.x_label, dc, wx.ELLIPSIZE_END, box.width)
            dc.DrawText(label, box.x + (box.width - dc.GetTextExtent(label).width) // 2,
                        box.GetBottom() + dc.GetCharHeight() + self.FromDIP(9))
        if self.y_label and not horizontal_y:
            label_x = max(self.FromDIP(4), box.x - self.FromDIP(77))
            dc.DrawRotatedText(self.y_label, label_x, box.y + (box.height + dc.GetTextExtent(self.y_label).width) // 2, 90)

    @staticmethod
    def _graticule_label_period(step, bounds, pixels, spacing):
        if step is None or not 1 <= (bounds[1] - bounds[0]) / step <= 30:
            return None
        return step * max(1, ceil(spacing * (bounds[1] - bounds[0]) / (max(1, pixels) * step)))

    @staticmethod
    def _on_graticule_label(value, period):
        if period is None:
            return True
        index = value / period
        return abs(index - round(index)) < 1e-7

    def _draw_minor_grid(self, dc, box, x_bounds, x_labels, y_labels, px, py, colors):
        dc.SetPen(wx.Pen(wx.Colour(*colors.minor_grid)))
        if self.x_ticks is None:
            if self.log_x and 0.5 <= x_bounds[1] - x_bounds[0] <= 7:
                minor = (decade + log10(k) for decade in range(floor(x_bounds[0]), ceil(x_bounds[1])) for k in range(2, 10))
            else:
                minor = ((a[0] + b[0]) / 2 for a, b in pairwise(x_labels))
            for value in minor:
                if x_bounds[0] < value < x_bounds[1]:
                    x = px(value)
                    dc.DrawLine(x, box.y, x, box.GetBottom())
        for a, b in pairwise(y_labels):
            y = py((a[0] + b[0]) / 2)
            dc.DrawLine(box.x, y, box.GetRight(), y)

    def _trace_pen(self, trace, background):
        color = wx.Colour(*self.appearance.trace_rgb(trace.color, trace.slot, background))
        style = wx.PENSTYLE_SHORT_DASH if trace.style == "dashed" else wx.PENSTYLE_SOLID
        return wx.Pen(color, self.FromDIP(round(self.appearance.line_width)), style)

    def _draw_trace(self, dc, box, trace, background, px, py, x_bounds, bottom):
        pen = self._trace_pen(trace, background)
        dc.SetPen(pen)
        visible_start, visible_stop = 0, trace.x.size
        if trace.ordered:
            low = np.searchsorted(trace.plot_x, x_bounds[0], side="left")
            high = np.searchsorted(trace.plot_x, x_bounds[1], side="right")
            # Include one neighbor at each boundary so a crossing segment
            # remains connected at the edge of the current viewport.
            visible_start, visible_stop = max(0, low - 1), min(trace.x.size, high + 1)
        show_points = self.appearance.point_markers and visible_stop - visible_start <= box.width // self.FromDIP(9)
        for run_start, run_stop in trace.runs:
            start, stop = max(run_start, visible_start), min(run_stop, visible_stop)
            if start >= stop:
                continue
            x, y = trace.plot_x[start:stop], trace.y[start:stop]
            if x.size > max(4, box.width * 2) and trace.style != "points" and trace.ordered:
                left, right = x_bounds
                columns = np.floor(np.clip((x - left) / (right - left), -1, 2) * box.width)
                selected = peak_sample_indices(columns, y)
                x, y = x[selected], y[selected]
            points = [(px(a), py(b)) for a, b in zip(x, y, strict=True)]
            if trace.style == "bars":
                baseline = py(bottom)
                for point_x, point_y in points:
                    dc.DrawLine(point_x, baseline, point_x, point_y)
                continue
            if trace.style != "points" and len(points) > 1:
                dc.DrawLines(points)
            if show_points or trace.style == "points" or len(points) == 1:
                dc.SetBrush(wx.Brush(pen.GetColour()))
                for point in points:
                    dc.DrawCircle(point, self.FromDIP(2))

    def _draw_legend(self, dc, box, colors):
        self._legend_items = []
        columns, rows = self._legend_layout(box.width, self.canvas.GetClientSize().height)
        if not rows:
            return
        row_height = dc.GetCharHeight() + self.FromDIP(5)
        width = max(1, box.width // columns)
        swatch = self.FromDIP(17)
        count = min(len(self._all_series), rows * columns)
        for index, trace in enumerate(self._all_series[:count]):
            row, column = divmod(index, columns)
            x = box.x + column * width
            y = self.FromDIP(11) + dc.GetCharHeight() + row * row_height
            key = trace.key
            if index == count - 1 and count < len(self._all_series):
                key, name = None, "More traces…"
                text_color = colors.accent
            else:
                name = trace.label or f"Trace {index + 1}"
                text_color = colors.text
                if key in self._hidden_traces:
                    text_color = colors.muted
                    name += " (hidden)"
                    pen = wx.Pen(wx.Colour(*colors.axis), 1, wx.PENSTYLE_DOT)
                else:
                    pen = self._trace_pen(trace, colors.panel)
                dc.SetPen(pen)
                dc.DrawLine(x, y + dc.GetCharHeight() // 2, x + swatch, y + dc.GetCharHeight() // 2)
            dc.SetTextForeground(wx.Colour(*text_color))
            label = wx.Control.Ellipsize(name, dc, wx.ELLIPSIZE_END, max(1, width - swatch - self.FromDIP(9)))
            dc.DrawText(label, x + (swatch + self.FromDIP(5) if key is not None else 0), y)
            self._legend_items.append((wx.Rect(x, y, width, row_height), key))
        dc.SetTextForeground(wx.Colour(*colors.text))

    def _draw_color_scale(self, dc, box, divider):
        x = box.GetRight() + self.FromDIP(12)
        width = self.FromDIP(10)
        if self._scale_bitmap is None or self._scale_bitmap.GetHeight() != box.height:
            ramp = np.linspace(0, -self.appearance.heat_range, box.height)[:, None]
            rgb = np.repeat(heat_image(ramp, self.appearance), width, axis=1)
            self._scale_bitmap = wx.Bitmap(wx.Image(width, box.height, rgb.tobytes()))
        dc.DrawBitmap(self._scale_bitmap, x, box.y)
        dc.SetPen(wx.Pen(divider))
        dc.SetBrush(wx.TRANSPARENT_BRUSH)
        dc.DrawRectangle(x, box.y, width, box.height)
        levels = [(0, "0 dB"), (1, f"−{self.appearance.heat_range}")]
        if box.height > dc.GetCharHeight() * 5:
            levels.append((0.5, f"−{self.appearance.heat_range // 2}"))
        for fraction, label in levels:
            y = box.y + round((box.height - 1) * fraction)
            dc.DrawLine(x + width, y, x + width + self.FromDIP(4), y)
            dc.DrawText(label, x + width + self.FromDIP(7), y - dc.GetCharHeight() // 2)

    def _draw_heatmap(self, dc, box, x_bounds, y_bounds):
        if self._bitmap is None:
            # Map each visible pixel's centre to its original bin, so the
            # rendered colour and the value under the cursor agree when zoomed.
            left, right = x_bounds
            bottom, top = y_bounds
            x = left + (np.arange(box.width) + 0.5) * ((right - left) / box.width)
            y = top - (np.arange(box.height) + 0.5) * ((top - bottom) / box.height)
            x_edges, y_edges = self._heat_edges
            x_bin = np.searchsorted(x_edges, x, side="left") - 1
            y_bin = np.searchsorted(y_edges, y, side="left") - 1
            columns = np.flatnonzero((0 <= x_bin) & (x_bin < len(x_edges) - 1))
            rows = np.flatnonzero((0 <= y_bin) & (y_bin < len(y_edges) - 1))
            if not len(rows) or not len(columns):
                return
            visible = np.ix_(y_bin[rows], x_bin[columns])
            rgb = self._heat_pixels[visible]
            image = wx.Image(len(columns), len(rows), rgb.tobytes())
            valid = np.isfinite(self._heatmap[2][visible])
            if not valid.all():
                image.SetAlpha(np.where(valid, 255, 0).astype(np.uint8).tobytes())
            self._bitmap = wx.Bitmap(image), box.x + int(columns[0]), box.y + int(rows[0])
        bitmap, x, y = self._bitmap
        dc.DrawBitmap(bitmap, x, y, useMask=True)
