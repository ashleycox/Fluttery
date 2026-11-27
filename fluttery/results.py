"""Native session results, history, comparison, and report commands."""

from __future__ import annotations

import io
import time
from collections import deque
from copy import deepcopy
from pathlib import Path
from threading import Thread
from zipfile import ZIP_DEFLATED, ZipFile

import wx
from wx import dataview

from .audio.session_capture import CapturedInput, write_input_wav
from .charts import LinePlot, set_control_help
from .plot_export_ui import save_plot_image, save_results_graph
from .plot_style import PlotStyle
from .preferences import DEFAULT_EXPORT
from .reporting import (
    DISTORTION_HEADERS,
    GRAPH_FILENAMES,
    MEASUREMENT_LABELS,
    SAMPLE_HEADERS,
    build_report,
    comparison_statistics,
    distortion_history,
    distortion_results,
    graph_png,
    has_raw_response_comparison,
    history_graph,
    history_table,
    measurement_graph,
    measurement_summary,
    measurement_table,
    report_filename,
    report_logo_png,
    report_sections,
    session_graphs,
    session_statistics,
    signal_loss_table,
    spectrogram_graph,
    spectrum_graph,
    time_text,
    write_export,
)
from .sessions import METADATA_FIELDS, RUN_KEYS, load_session, save_session
from .staging import atomic_output

VIEWS = {
    "fft": "Spectrum (FFT)",
    "graph": "Wow and flutter",
    "drift": "Speed drift",
    "spectrogram": "Spectrogram",
    "table": "Session samples",
    "metadata": "Session metadata",
    "measurements": "Recorded measurements",
    "thd": "THD+N results",
    "stats": "Session statistics",
}
EXPORTS = {
    "pdf": "PDF report",
    "csv": "CSV data",
    "json": "JSON session data",
    "html": "Interactive HTML report",
    "png": "Plot image (PNG, SVG or PDF)",
    "zip": "ZIP report bundle",
}
DESKTOP_HELP = {
    "open": "Open a Fluttery session or JSON export in Results.",
    "save": "Save the current measurements, session details and measurement settings.",
    "export": "Choose a report or data format. Report contents are set in Settings under Reports.",
    "new": "Start an empty session. Fluttery asks about any unsaved changes first.",
    "save-as": "Save the current session under a different name and continue with that file.",
    "compare": "Plot measurements from a saved session alongside the current wow and flutter or speed drift readings.",
    "read-comparison": "Compare the peak reading, average frequency and sample count for each session.",
    "clear-comparison": "Remove the comparison overlay and comparison statistics.",
    "compare-raw": "Show or hide the raw and corrected values saved with this frequency response.",
    "print": "Open the system print dialog for a report prepared with your current report settings.",
    "plot_data": "Move focus to the text that describes the plot and the selected point.",
    "plot_controls": "Show or hide point, trace and zoom buttons for the active plot.",
    "fit_plot": "Restore the original axes for the active plot. Press Home with the plot focused.",
    "graph_settings": "Set graph contrast, grid lines and the spectrogram colour scale in Settings.",
    "plot_export": "Save the visible plot with its current zoom and comparison markers.",
    "input_audio_export": "Export the captured input without changing the session.",
    "input_audio_remove": "Remove the captured audio to reduce the size of the saved session file.",
    "signal_loss": "Read the recorded signal losses, clipping and capture interruptions for the selected run.",
    "review_run_settings": "Compare the selected run's recorded settings with the working setup.",
}
METADATA_HELP = {
    "technician": "Enter the name to include when you export a report with session details.",
    "equipment": "Enter the equipment name or identifier to keep with the session.",
    "notes": "Enter observations or test conditions to keep with the session.",
}
SESSION_FILTER = "Fluttery sessions and JSON reports (*.fluttery;*.json)|*.fluttery;*.json"
CONTROL_PAD = 6
TABLE_PAGE_SIZE = 80
LIVE_SPECTRUM_WAITING = "Waiting for a usable reference tone."


def _help(control, text, *, name=None):
    set_control_help(control, text)
    if name:
        control.SetAccessibleName(name)


def _action(parent, label, action, help_text):
    button = wx.Button(parent, label=label)
    button.Bind(wx.EVT_BUTTON, lambda _event: action())
    _help(button, help_text)
    return button


def _readonly(parent, name, help_text, *, size=wx.DefaultSize):
    # A read-only single-line edit is skipped by Tab on Windows.
    field = wx.TextCtrl(parent, value="", size=size, style=wx.TE_READONLY | wx.TE_MULTILINE, name=name)
    _help(field, help_text, name=name)
    return field


def _set_text(field, value):
    if field.GetValue() != value:
        field.ChangeValue(value)


def _labelled_row(label, field, grow=0):
    row = wx.BoxSizer(wx.HORIZONTAL)
    row.Add(label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, CONTROL_PAD)
    row.Add(field, grow, wx.EXPAND)
    if text := field.GetToolTipText():
        _help(label, text)
    return row


class _Table(wx.Panel):
    """Use each OS's native list control and keep keyboard row selection."""

    def __init__(self, parent, name, headers, help_text, *, on_select=None):
        super().__init__(parent)
        self._is_windows = wx.Platform == "__WXMSW__"
        self._headers = ()
        self._rows = []
        self._selection = None
        self._on_select = on_select
        if self._is_windows:
            self.list = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL, name=name)
            self.list.Bind(wx.EVT_LIST_ITEM_SELECTED, self._selected)
        else:
            self.list = dataview.DataViewListCtrl(self, style=dataview.DV_SINGLE | dataview.DV_ROW_LINES)
            self.list.SetName(name)
            self.list.Bind(dataview.EVT_DATAVIEW_SELECTION_CHANGED, self._selected)
        _help(self.list, help_text, name=name)
        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(self.list, 1, wx.EXPAND)
        self.SetSizer(layout)
        self.set_rows(headers, [])

    def _column_width(self, header):
        if header in ("Time", "Recorded at"):
            return self.FromDIP(172)
        if header == "Measurement":
            return self.FromDIP(145)
        return self.FromDIP(max(100, min(170, len(header) * 8)))

    def set_rows(self, headers, rows, *, selection=None):
        headers, rows = tuple(headers), list(rows)
        if headers == self._headers and rows == self._rows:
            if selection is not None and selection != self._selection:
                self._select_row(selection)
            return
        self._rows = rows
        self._selection = selection
        self.list.Freeze()
        try:
            self.list.DeleteAllItems()
            if headers != self._headers:
                self.list.DeleteAllColumns() if self._is_windows else self.list.ClearColumns()
                for index, header in enumerate(headers):
                    width = self._column_width(header)
                    if self._is_windows:
                        self.list.InsertColumn(index, header, width=width)
                    else:
                        self.list.AppendTextColumn(header, width=width, flags=dataview.DATAVIEW_COL_RESIZABLE)
                self._headers = headers
            for row_index, row in enumerate(rows):
                if self._is_windows:
                    self.list.InsertItem(row_index, row[0])
                    for index, value in enumerate(row[1:], 1):
                        self.list.SetItem(row_index, index, value)
                else:
                    self.list.AppendItem(list(row))
            if selection is not None:
                self._select_row(selection)
        finally:
            self.list.Thaw()

    def _select_row(self, row):
        if not 0 <= row < len(self._rows):
            return
        self._selection = row
        if self._is_windows:
            self.list.Select(row)
            self.list.EnsureVisible(row)
        else:
            self.list.SelectRow(row)
            self.list.EnsureVisible(self.list.RowToItem(row))

    def _selected(self, event):
        if self._is_windows:
            row = event.GetIndex()
        else:
            selection = self.list.GetSelection()
            row = self.list.ItemToRow(selection) if selection.IsOk() else -1
        if 0 <= row < len(self._rows) and row != self._selection:
            self._selection = row
            if self._on_select is not None:
                self._on_select(row)
        event.Skip()


class _SampleTable(wx.Panel):
    """Page bounded native lists without replacing the underlying history."""

    def __init__(self, parent, name, headers, *, format_rows=None):
        super().__init__(parent)
        self.headers, self.rows, self.page = tuple(headers), [], 0
        self._format_rows = format_rows
        self.table = _Table(self, name, self.headers,
                            "Read exact values in each row. Use the page buttons to move between groups of samples.")
        self.status = wx.StaticText(self, label="", name="Sample table page")
        _help(self.status, "Shows the page number, the number of pages and the total number of samples.")
        self.previous = _action(self, "Previous Page", lambda: self._move(-1), "Show the previous group of samples.")
        self.next = _action(self, "Next Page", lambda: self._move(1), "Show the next group of samples.")
        controls = wx.BoxSizer(wx.HORIZONTAL)
        for item in (self.previous, self.next, self.status):
            controls.Add(item, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, CONTROL_PAD)
        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(self.table, 1, wx.EXPAND)
        layout.Add(controls, 0, wx.TOP, CONTROL_PAD)
        self.SetSizer(layout)
        self._move(0)

    def set_rows(self, headers, rows, *, reset=False):
        self.headers, self.rows = tuple(headers), rows
        if reset:
            self.page = 0
        self._move(0)

    def _move(self, direction):
        count = len(self.rows)
        pages = max(1, (count + TABLE_PAGE_SIZE - 1) // TABLE_PAGE_SIZE)
        self.page = min(pages - 1, max(0, self.page + direction))
        start = self.page * TABLE_PAGE_SIZE
        rows = self.rows[start:start + TABLE_PAGE_SIZE]
        self.table.set_rows(self.headers, self._format_rows(rows) if self._format_rows else rows)
        status = f"Page {self.page + 1} of {pages} · {count} rows"
        if status != self.status.GetLabel():
            self.status.SetLabel(status)
        self.previous.Enable(self.page > 0)
        self.next.Enable(self.page + 1 < pages)


class _MeasurementsPanel(wx.Panel):
    def __init__(self, parent, title, on_view):
        super().__init__(parent)
        self.entries = []
        self.all_entries = []
        self.selection = None
        self._displayed_entry = None
        self.has_raw_comparison = False
        controls = wx.BoxSizer(wx.HORIZONTAL)
        filter_label = wx.StaticText(self, label="Type")
        self.filter = wx.Choice(self, choices=["All measurement types", *MEASUREMENT_LABELS.values()],
                                name="Measurement type filter")
        self.filter.SetSelection(0)
        _help(self.filter, "Filter the log by measurement type. The session keeps all recorded measurements.",
              name="Measurement type filter")
        self.filter.Bind(wx.EVT_CHOICE, lambda _event: self._filter_entries())
        controls.Add(_labelled_row(filter_label, self.filter), 0, wx.RIGHT, self.FromDIP(14))
        view_label = wx.StaticText(self, label="Display")
        self.detail_view = wx.Choice(self, choices=["Details", "Graph", "Sample table"], name="Selected measurement view")
        self.detail_view.SetSelection(0)
        _help(self.detail_view, "Show a written summary, a graph or the exact samples for the selected measurement.",
              name="Selected measurement view")
        self.on_view = on_view
        self.detail_view.Bind(wx.EVT_CHOICE, self._choose_detail_view)
        controls.Add(_labelled_row(view_label, self.detail_view), 0)
        self.history = _Table(self, title, ("Recorded at", "Measurement", "Result"),
                              "Select a recorded measurement to review its details, graph or samples.",
                              on_select=self._choose)
        self.history.SetMinSize(self.FromDIP((0, 105)))

        self.compare_raw = wx.CheckBox(self, label="Compare with Raw")
        _help(self.compare_raw, DESKTOP_HELP["compare-raw"], name="Compare with Raw")
        self.compare_raw.Bind(wx.EVT_CHECKBOX, self._raw_comparison_changed)
        self.detail_book = wx.Simplebook(self)
        self.detail = _readonly(self.detail_book, "Measurement details",
                                "Read the type, time and summary of the selected measurement.",
                                size=self.FromDIP((-1, 90)))
        self.plot = LinePlot(self.detail_book, title=title, controls=False)
        self.points = _SampleTable(self.detail_book, "Measurement samples", ("Time", "Value"))
        for item, label in ((self.detail, "Details"), (self.plot, "Graph"), (self.points, "Sample table")):
            self.detail_book.AddPage(item, label)

        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(controls, 0, wx.EXPAND | wx.BOTTOM, CONTROL_PAD)
        self.history_layout = layout.Add(self.history, 1, wx.EXPAND)
        self.raw_layout = layout.Add(self.compare_raw, 0, wx.TOP, self.FromDIP(8))
        self.detail_layout = layout.Add(self.detail_book, 0, wx.EXPAND | wx.TOP, self.FromDIP(12))
        self.SetSizer(layout)
        self.graph = None
        _set_text(self.detail, "No measurements in this view.")
        self._choose_detail_view(None)

    def _choose_detail_view(self, event):
        selected = self.detail_view.GetSelection()
        self.detail_book.SetSelection(selected)
        # Give the log most of the space when the selection has only a brief
        # written summary. Graphs and point tables need a larger lower pane.
        self.history_layout.SetProportion(2 if selected == 0 else 1)
        self.detail_layout.SetProportion(0 if selected == 0 else 2)
        self.detail_book.SetMinSize(self.FromDIP((-1, 105)) if selected == 0 else wx.DefaultSize)
        self.raw_layout.Show(self.has_raw_comparison and selected != 0)
        self.Layout()
        if event is not None:
            self.on_view()

    def toggle_raw_comparison(self):
        if not self.has_raw_comparison:
            return
        if self.detail_view.GetSelection() == 0:
            self.detail_view.SetSelection(1)
            self._choose_detail_view(None)
            self.detail_view.SetFocus()
        self.compare_raw.SetValue(not self.compare_raw.GetValue())
        self._raw_comparison_changed(None)

    def _raw_comparison_changed(self, _event):
        self._draw_measurement(reset_samples=False)
        self.on_view()

    def set_entries(self, entries):
        if len(entries) == len(self.all_entries) and all(left is right for left, right in zip(entries, self.all_entries)):
            return
        self.all_entries = list(entries)
        self._filter_entries()

    def select_latest(self, kind):
        self.filter.SetSelection(tuple(MEASUREMENT_LABELS).index(kind) + 1)
        self.selection = None
        self._filter_entries()

    def _filter_entries(self):
        filter_index = self.filter.GetSelection()
        kind = tuple(MEASUREMENT_LABELS)[filter_index - 1] if filter_index > 0 else None
        old = self.entries[self.selection] if self.selection is not None and self.selection < len(self.entries) else None
        entries = (entry for entry in self.all_entries if kind is None or entry["type"] == kind)
        self.entries = sorted(entries, key=lambda entry: entry["timestamp"], reverse=True)
        selection = next((index for index, entry in enumerate(self.entries) if entry is old), None)
        if self.entries and selection is None:
            selection = 0
        rows = [(time_text(entry["timestamp"]), MEASUREMENT_LABELS.get(entry["type"], entry["type"]),
                 measurement_summary(entry)) for entry in self.entries]
        self.history.set_rows(("Recorded at", "Measurement", "Result"), rows, selection=selection)
        self._choose(selection)

    def _choose(self, index):
        self.selection = index
        entry = self.entries[index] if index is not None else None
        if entry is self._displayed_entry:
            return
        self._displayed_entry = entry
        had_comparison = self.has_raw_comparison
        self.has_raw_comparison = entry is not None and has_raw_response_comparison(entry)
        if had_comparison != self.has_raw_comparison:
            self.compare_raw.SetValue(False)
            self._choose_detail_view(None)
        if entry is None:
            _set_text(self.detail, "No measurements in this view.")
            self.graph = None
            self.plot.clear()
            self.points.set_rows(("Time", "Value"), [], reset=True)
        else:
            summary = measurement_summary(entry)
            type_label = MEASUREMENT_LABELS.get(entry["type"], entry["type"])
            _set_text(self.detail, f"{type_label}\n{time_text(entry['timestamp'])}\n\n{summary}")
            self._draw_measurement(reset_samples=True)
        if had_comparison != self.has_raw_comparison:
            self.on_view()

    def _draw_measurement(self, *, reset_samples):
        compare_raw = self.compare_raw.GetValue()
        # Comparison can replace the saved primary trace. Clear markers and
        # the trace selection so that neither points at the previous view.
        self.plot.clear()
        self.graph = measurement_graph(self._displayed_entry, compare_raw=compare_raw)
        _show_plot(self.plot, self.graph, fit=True)
        self.points.set_rows(*measurement_table(self._displayed_entry, compare_raw=compare_raw), reset=reset_samples)


def _show_plot(panel, graph, *, fit=False):
    panel.set_labels(graph.title, graph.x_label, graph.y_label)
    panel.log_x = graph.log_x
    if graph.heatmap is not None:
        panel.set_heatmap(*graph.heatmap, fit=fit)
    else:
        panel.set_series(graph.traces, fit=fit, empty_message=graph.empty_message)


class _DistortionPanel(wx.Panel):
    """Review all distortion analyzer and tracking readings on one timeline."""

    def __init__(self, parent, on_view):
        super().__init__(parent)
        self._entries = None
        self.graph = None
        self.summary = _readonly(self, "Session distortion statistics",
                                 "Read the average, minimum and peak THD+N for the session.",
                                 size=self.FromDIP((-1, 65)))
        label = wx.StaticText(self, label="Display")
        self.view = wx.Choice(self, choices=["Graph", "Sample table"], name="Distortion history view")
        _help(self.view, "Show the distortion history graph or the exact sample values and their source.",
              name="Distortion history view")
        self.view.SetSelection(0)
        self.book = wx.Simplebook(self)
        self.plot = LinePlot(self.book, title=VIEWS["thd"], controls=False)
        self.samples = _SampleTable(self.book, "Session distortion samples", DISTORTION_HEADERS)
        self.book.AddPage(self.plot, "Graph")
        self.book.AddPage(self.samples, "Sample table")

        def choose_view(_event):
            self.book.ChangeSelection(self.view.GetSelection())
            on_view()

        self.view.Bind(wx.EVT_CHOICE, choose_view)
        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(self.summary, 0, wx.EXPAND | wx.BOTTOM, CONTROL_PAD)
        layout.Add(_labelled_row(label, self.view), 0, wx.EXPAND | wx.BOTTOM, CONTROL_PAD)
        layout.Add(self.book, 1, wx.EXPAND)
        self.SetSizer(layout)
        self.set_entries([])

    def set_entries(self, entries):
        if self._entries is not None and len(entries) == len(self._entries) and all(
            current is previous for current, previous in zip(entries, self._entries, strict=True)
        ):
            return
        reset = not self._entries or len(entries) < len(self._entries) or any(
            current is not previous for current, previous in zip(entries, self._entries)
        )
        if reset:
            self.plot.clear()
        self._entries = list(entries)
        text, self.graph, rows = distortion_results(distortion_history(entries))
        _set_text(self.summary, text)
        _show_plot(self.plot, self.graph, fit=reset)
        self.samples.set_rows(DISTORTION_HEADERS, rows, reset=reset)


class ResultsPage(wx.Panel):
    """Browse and save Fluttery's continuous and discrete measurements."""

    def __init__(self, parent, controller):
        super().__init__(parent)
        self.controller = controller
        self.session_path = None
        self.comparison = None
        self.comparison_name = "Comparison"
        self._live_spectrum = None
        self._live_rows = deque(maxlen=200)
        self._live_fft_waiting = False
        self._plotted_spectrum = None
        self._history_key = None
        self._graph_cache = {}
        self._plot_keys = {}
        self._export_thread = None
        self._print_data = wx.PrintDialogData()

        self._summary_key = None
        self._export_message = ""
        self._run_key = None
        self.run_choice = wx.Choice(self, choices=["Current run"], name="Saved run")
        self.run_choice.SetSelection(0)
        _help(self.run_choice, "Review the current measurement or an earlier run saved in this session.")
        self.run_choice.Bind(wx.EVT_CHOICE, self._choose_run)
        run_label = wx.StaticText(self, label="Saved run")
        self.run_row = _labelled_row(run_label, self.run_choice)
        view_label = wx.StaticText(self, label="Show")
        self.view_choice = wx.Choice(self, choices=list(VIEWS.values()), name="Results view")
        _help(self.view_choice, "Choose which graph, table, session details, measurement history or statistics to view.",
              name="Results view")
        self.view_choice.Bind(wx.EVT_CHOICE, self._change_view)
        self.session_label = wx.StaticText(self, label="Unsaved session", style=wx.ST_ELLIPSIZE_END,
                                           name="Session and comparison status")
        _help(self.session_label, "Shows the open session and any comparison, or the status of an export.")

        self.pages = wx.Simplebook(self)
        self.plots = {}
        for view in ("fft", "graph", "drift", "spectrogram"):
            plot = LinePlot(self.pages, title=VIEWS[view], controls=False)
            self.plots[view] = plot
            self.pages.AddPage(plot, VIEWS[view])
        self.history_table = _SampleTable(self.pages, "Session samples", SAMPLE_HEADERS, format_rows=history_table)
        self.pages.AddPage(self.history_table, VIEWS["table"])
        self.metadata_fields = {}
        self.pages.AddPage(self._metadata_panel(), VIEWS["metadata"])
        self.measurements = _MeasurementsPanel(self.pages, VIEWS["measurements"], self._update_commands)
        self.pages.AddPage(self.measurements, VIEWS["measurements"])
        self.distortion = _DistortionPanel(self.pages, self._update_commands)
        self.pages.AddPage(self.distortion, VIEWS["thd"])
        self.summary = _readonly(self.pages, "Session and comparison statistics",
                                  "Read sample counts, wow and flutter levels and frequency drift for both sessions.")
        self.pages.AddPage(self.summary, VIEWS["stats"])

        header = wx.BoxSizer(wx.HORIZONTAL)
        header.Add(_labelled_row(view_label, self.view_choice), 0, wx.ALIGN_CENTER_VERTICAL)
        header.Add(self.session_label, 1, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, self.FromDIP(12))

        layout = wx.BoxSizer(wx.VERTICAL)
        layout.Add(header, 0, wx.EXPAND | wx.BOTTOM, self.FromDIP(12))
        self._run_item = layout.Add(self.run_row, 0, wx.EXPAND | wx.BOTTOM, self.FromDIP(8))
        self._run_item.Show(False)
        layout.Add(self.pages, 1, wx.EXPAND)
        margin = wx.BoxSizer(wx.VERTICAL)
        margin.Add(layout, 1, wx.EXPAND | wx.ALL, self.FromDIP(12))
        self.SetSizer(margin)
        self._default_view = self.controller.settings.get("defaultResultsView", "fft")
        self.refresh_settings()
        self.set_view(self._default_view)

    def desktop_actions(self):
        return (
            ("open", "Open Session…", self.open_session),
            ("save", "Save Session", self.save_session),
            ("export", "Export Report…", self.choose_export_format),
        )

    def desktop_options(self):
        options = (
            ("new", "New Session", self.new_session),
            ("save-as", "Save Session As…", lambda: self.save_session(save_as=True)),
            ("compare", "Compare With Session…", self.open_comparison),
            ("print", "Print Report…", self.print_report),
        )
        if self.comparison is not None:
            options += (
                ("read-comparison", "Read Comparison…", self.read_comparison),
                ("clear-comparison", "Clear Comparison", self.clear_comparison),
            )
        if self.view == "measurements" and self.measurements.has_raw_comparison:
            title = "Hide Raw Comparison" if self.measurements.compare_raw.GetValue() else "Compare with Raw"
            options += (("compare-raw", f"{title}\tCtrl+Shift+B", self.measurements.toggle_raw_comparison),)
        run = self._displayed_run()
        if isinstance(run.get("inputAudio"), CapturedInput):
            if run["inputAudio"].blocks:
                options += (("input_audio_export", "Export Original Input Audio…", self.export_input_audio),)
            options += (("input_audio_remove", "Remove Original Input Audio…", self.remove_input_audio),)
        if run.get("signalLoss"):
            options += (("signal_loss", "Read Signal Losses…", self.read_signal_losses),)
        if run.get("runSettings") or run.get("settings"):
            options += (("review_run_settings", "Review Saved Run Settings…", self.review_run_settings),)
        if (plot := self._current_plot()) is not None:
            title = "Hide Plot Controls" if plot.controls_visible else "Show Plot Controls"
            options += (
                ("plot_data", "Read Plot Data", plot.focus_data),
                ("plot_controls", title, lambda: plot.set_controls_visible(not plot.controls_visible)),
                ("fit_plot", "Fit Plot", plot.fit),
                ("plot_export", "Save Plot Image…", lambda: save_plot_image(plot)),
                ("graph_settings", "Graph Settings…", lambda: self.controller.navigate("settings", "graphs")),
            )
        return options

    def desktop_help(self, key):
        return DESKTOP_HELP.get(key, "")

    def _current_plot(self):
        if self.view == "measurements" and self.measurements.detail_book.GetSelection() == 1:
            return self.measurements.plot
        if self.view == "thd" and self.distortion.book.GetSelection() == 0:
            return self.distortion.plot
        return self.plots.get(self.view)

    def keyboard_actions(self):
        plot = self._current_plot()
        return plot.keyboard_actions() if plot is not None else ()

    @property
    def is_exporting(self):
        return self._export_thread is not None

    def refresh_settings(self):
        expanded = self.controller.settings.get("useAccessibility", True)
        for plot in (*self.plots.values(), self.measurements.plot, self.distortion.plot):
            plot.set_expanded_descriptions(expanded)
            plot.set_appearance(self.controller.settings)
        default = self.controller.settings.get("defaultResultsView", "fft")
        if default != self._default_view:
            self._default_view = default
            self.set_view(default)

    def _metadata_panel(self):
        panel = wx.Panel(self.pages)
        _help(panel, "Fluttery saves these details with the session and includes them in reports when report metadata is enabled.")
        layout = wx.BoxSizer(wx.VERTICAL)
        for key in METADATA_FIELDS:
            label = wx.StaticText(panel, label=key.title())
            options = {"style": wx.TE_MULTILINE} if key == "notes" else {}
            field = wx.TextCtrl(panel, name=key.title(), **options)
            _help(label, METADATA_HELP[key])
            _help(field, METADATA_HELP[key], name=key.title())
            field.Bind(wx.EVT_TEXT, lambda _event, key=key: self._metadata_changed(key))
            self.metadata_fields[key] = field
            layout.Add(label, 0, wx.TOP, CONTROL_PAD)
            layout.Add(field, int(key == "notes"), wx.EXPAND | wx.BOTTOM, CONTROL_PAD)
        panel.SetSizer(layout)
        return panel

    def _metadata_changed(self, field):
        self.controller.session["metadata"][field] = self.metadata_fields[field].GetValue()
        self.controller.session_changed()

    def _change_view(self, _event):
        self.set_view(tuple(VIEWS)[self.view_choice.GetSelection()])

    def _update_commands(self):
        if (workspace := getattr(self.controller, "workspace", None)) is not None:
            workspace.update_commands()

    def set_view(self, view):
        if view not in VIEWS:
            view = "fft"
        self.view = view
        index = tuple(VIEWS).index(view)
        self.view_choice.SetSelection(index)
        self.pages.SetSelection(index)
        self.refresh_results()
        self._update_commands()

    def review_measurements(self, kind):
        """Reveal the most recent capture from the tool that recorded it."""
        self.controller.navigate("results")
        self.set_view("measurements")
        self.measurements.select_latest(kind)

    def refresh_live(self, snapshot):
        """Receive the current audio snapshot from the controller's UI timer."""
        if not self.IsShownOnScreen() or self.view not in ("fft", "spectrogram") or not self._live_plot_active():
            return
        spectrum = self._capture_spectrum(snapshot)
        active = self._spectrum_input_active(snapshot)
        self._live_fft_waiting = active and spectrum is None
        if spectrum is None or self._live_spectrum is None or spectrum["spectrum"] is not self._live_spectrum["spectrum"]:
            self._live_spectrum = spectrum
        if self.view == "fft":
            self._draw_current_spectrum()
        elif active or (self._live_rows and self._live_rows[-1].get("valid") is not False):
            self._append_live_spectrum(spectrum)

    def _append_live_spectrum(self, spectrum):
        previous = self._live_rows[-1] if self._live_rows else None
        gap = spectrum is None
        if not gap and previous and previous.get("spectrum") is spectrum["spectrum"]:
            return
        now = time.monotonic()
        # A transition can occur between the usual display refreshes. Keep
        # both its entry and recovery even in a brief interruption.
        if previous is None or (previous.get("valid") is False) != gap or now - previous["timestamp"] >= 0.12:
            row = {"valid": False} if gap else spectrum
            self._live_rows.append({**row, "timestamp": now})
            self._draw_live_spectrogram()

    def _draw_live_spectrogram(self, *, fit=False):
        key = ("live", self._live_rows[-1]["timestamp"])
        if self._plot_keys.get("spectrogram") != key:
            rows = list(self._live_rows)
            graph = spectrogram_graph(rows, spectra=rows, use_sample_times=True)
            graph.empty_message = LIVE_SPECTRUM_WAITING
            self._graph_cache["spectrogram"] = graph
            self._plot_keys["spectrogram"] = key
            _show_plot(self.plots["spectrogram"], graph, fit=fit)

    @staticmethod
    def _spectrum_input_active(snapshot):
        health = snapshot.get("health", {})
        legacy_loopback = (
            snapshot.get("loopback") and snapshot.get("generator", {}).get("enabled")
        )
        return bool(health.get("inputActive") or health.get("loopbackActive", legacy_loopback))

    @classmethod
    def _capture_spectrum(cls, snapshot):
        source = snapshot.get("flutter_spectrum") or {}
        levels = source.get("db", ())
        frequencies = source.get("frequencies", ())
        if cls._spectrum_input_active(snapshot) and len(levels) >= 2 and len(levels) == len(frequencies):
            return {
                "spectrum": levels,
                "spectrumEncoding": "db",
                "spectrumStartHz": float(frequencies[0]),
                "spectrumBinHz": float(frequencies[1] - frequencies[0]),
                "timestamp": time.time(),
            }
        return None

    def refresh_results(self):
        session = self.controller.session
        self._refresh_run_choice(session)
        run = self._displayed_run()
        history = run["history"]
        spectra = run.get("spectra")
        comparison_history = self.comparison["history"] if self.comparison else []
        graph_source = self._graph_source()
        # A capped list keeps its identity and length while the displayed time
        # range moves. Use the recorded endpoints rather than recyclable IDs.
        endpoints = (history[0].get("timestamp"), history[-1].get("timestamp")) if history else (None, None)
        session_key = id(session), id(history), len(history), endpoints, id(self.comparison), graph_source
        new_history = (self._history_key is None or self._history_key[:2] != session_key[:2]
                       or (not history and self._history_key[2] > 0))
        self._history_key = session_key

        if new_history:
            self._graph_cache.clear()
            self._plot_keys.clear()
            for plot in self.plots.values():
                plot.clear()
            self._plotted_spectrum = None
            self._live_spectrum = None
            self._live_fft_waiting = False
            self._live_rows.clear()
        self._refresh_status()
        if self.view == "fft":
            self._draw_current_spectrum(fit=new_history)
        elif self.view == "spectrogram":
            if self._live_plot_active() and self._live_rows:
                self._draw_live_spectrogram(fit=new_history)
            else:
                last_spectrum = spectra[-1].get("timestamp") if spectra else None
                key = (*session_key, last_spectrum)
                if self._plot_keys.get(self.view) != key:
                    graph = spectrogram_graph(history, spectra=spectra, signal_loss=run.get("signalLoss") or ())
                    self._plot_keys[self.view] = key
                    self._graph_cache[self.view] = graph
                    _show_plot(self.plots[self.view], graph, fit=new_history)
        elif self.view in ("graph", "drift"):
            if self._plot_keys.get(self.view) != session_key:
                graph = history_graph(self.view, history, graph_source, comparison_history, self.comparison_name,
                                      run_label=self._run_label())
                self._plot_keys[self.view] = session_key
                self._graph_cache[self.view] = graph
                _show_plot(self.plots[self.view], graph, fit=new_history)
        elif self.view == "table":
            if self._plot_keys.get(self.view) != session_key:
                self._plot_keys[self.view] = session_key
                self.history_table.set_rows(SAMPLE_HEADERS, history, reset=new_history)
        elif self.view == "metadata":
            for key, field in self.metadata_fields.items():
                _set_text(field, session["metadata"].get(key, ""))
        elif self.view == "measurements":
            self.measurements.set_entries(session["measurementLog"])
        elif self.view == "thd":
            self.distortion.set_entries(session["measurementLog"])
        elif self.view == "stats" and self._summary_key != session_key:
            self._summary_key = session_key
            current_stats = session_statistics(history, graph_source)
            compare_stats = session_statistics(comparison_history, graph_source)
            rows = [self.run_choice.GetStringSelection() or "Current run"]
            rows.extend(f"{label}: {value}" for label, value in current_stats)
            if isinstance(audio := run.get("inputAudio"), CapturedInput):
                rows.append(f"Original input audio: {audio.seconds:.2f} seconds, {audio.stored_bytes / 1048576:.2f} MiB compressed.")
                if audio.incomplete_reason:
                    rows.append(audio.incomplete_reason)
            if not current_stats:
                rows.append("No continuous samples.")
            if compare_stats:
                rows.extend(("", f"Comparison: {self.comparison_name}"))
                rows.extend(f"{label}: {value}" for label, value in compare_stats)
                rows.extend(("", *self._comparison_text()))
            _set_text(self.summary, "\n".join(rows))

    def _refresh_run_choice(self, session):
        archived = session.get("previousRuns", ())
        key = id(session), len(archived)
        if self._run_key == key:
            return
        choices = ["Current run"]
        for index, run in enumerate(archived, 1):
            first = run.get("history", [])
            recorded = time_text(first[0].get("timestamp")) if first else "Audio capture"
            choices.append(f"Run {index} · {recorded}")
        selected = self.run_choice.GetSelection() if self._run_key and self._run_key[0] == key[0] else 0
        self.run_choice.Set(choices)
        self.run_choice.SetSelection(min(max(0, selected), len(choices) - 1))
        self._run_item.Show(bool(archived))
        self.Layout()
        self._run_key = key

    def _choose_run(self, _event):
        self.refresh_results()
        self._update_commands()

    def _displayed_run(self):
        session = self.controller.session
        selection = self.run_choice.GetSelection()
        archived = session.get("previousRuns", ())
        return archived[selection - 1] if 0 < selection <= len(archived) else session

    def _run_label(self):
        return self.run_choice.GetStringSelection() if self.run_choice.GetSelection() > 0 else ""

    def _graph_source(self):
        run = self._displayed_run()
        settings = {} if self._live_plot_active() else run.get("runSettings") or run.get("settings") or {}
        return settings.get("graphSource", self.controller.settings.get("graphSource", "detectorPeak"))

    def _live_plot_active(self):
        if self.run_choice.GetSelection() > 0:
            return False
        if getattr(self.controller, "is_measuring", False):
            return True
        run = self.controller.session
        return not run.get("recordedAudio") and not (self.session_path and run["history"])

    def _comparison_text(self):
        history = self._displayed_run()["history"]
        other = self.comparison["history"]
        source = self._graph_source()
        return [f"{label}. Current: {current}. Comparison: {comparison}. Difference: {delta}."
                for label, current, comparison, delta in comparison_statistics(history, other, source)]

    def read_signal_losses(self):
        rows = signal_loss_table(self._displayed_run().get("signalLoss") or ())
        if rows:
            text = "\n".join(f"{start} to {end}. {reason.rstrip('.')}." for start, end, reason in rows)
            self.controller._show_text("Signal losses", text)

    def read_comparison(self):
        if self.comparison is not None:
            self.controller._show_text("Session comparison", "\n".join([self.comparison_name, *self._comparison_text()]))

    def read_current(self):
        if self.view == "stats":
            text = self.summary.GetValue()
        elif self.view == "thd":
            text = self.distortion.summary.GetValue()
        elif self.view == "measurements":
            text = self.measurements.detail.GetValue()
        elif self.view == "metadata":
            text = "\n".join(f"{key.title()}: {field.GetValue()}" for key, field in self.metadata_fields.items())
        elif graph := self._graph_cache.get(self.view):
            text = graph.description()
        else:
            text = self.history_table.status.GetLabel()
        self.controller._show_text(VIEWS[self.view], text)

    def _refresh_status(self):
        if self._export_message:
            text = self._export_message
        else:
            text = self.session_path.name if self.session_path is not None else "Unsaved session"
            if self.comparison is not None:
                text += f" · Comparing with {self.comparison_name}"
        if self.session_label.GetLabel() != text:
            self.session_label.SetLabel(text)
            self.Layout()


    def _draw_current_spectrum(self, *, fit=False):
        run = self._displayed_run()
        history = run["history"]
        spectra = run.get("spectra")
        live = self._live_spectrum if self._live_plot_active() else None
        waiting = self._live_fft_waiting and self._live_plot_active()
        if waiting:
            key = ("waiting",)
        elif live is not None:
            key = "live", live["timestamp"]
        else:
            rows = history if spectra is None else spectra
            key = "archive", rows[-1].get("timestamp") if rows else None
        if key != self._plotted_spectrum or "fft" not in self._graph_cache:
            if waiting:
                graph = spectrum_graph([])
                graph.empty_message = LIVE_SPECTRUM_WAITING
            else:
                graph = spectrum_graph(history, live, spectra=spectra)
            self._graph_cache["fft"] = graph
            self._plotted_spectrum = key
            _show_plot(self.plots["fft"], graph, fit=fit)

    def _show_error(self, title, error):
        self.controller.show_error(title, error)

    def _file_path(self, title, wildcard, *, save=False, name="", extension=None):
        style = wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT if save else wx.FD_OPEN | wx.FD_FILE_MUST_EXIST
        with wx.FileDialog(getattr(self.controller, "dialog_parent", self.GetTopLevelParent()), title, defaultFile=name, wildcard=wildcard, style=style) as dialog:
            dialog.SetHelpText("Choose where to save the selected file." if save else DESKTOP_HELP["open"])
            if dialog.ShowModal() != wx.ID_OK:
                return None
            path = Path(dialog.GetPath())
        if extension and not path.suffix:
            path = path.with_suffix(f".{extension}")
            if save and path.exists() and not self.controller.confirm(f'Replace “{path.name}”?', title):
                return None
        return path

    def open_session(self):
        path = self._file_path("Open Fluttery session", SESSION_FILTER)
        if path is None or not self.controller.confirm_replace_session():
            return False
        try:
            session = load_session(path)
        except (OSError, ValueError, RecursionError) as error:
            self._show_error("Could not open session", error)
            return False
        if not self.controller.session_loaded(session):
            return False
        self.session_path = path if path.suffix.lower() == ".fluttery" else None
        self.controller.navigate("results")
        self.controller.announce("Session loaded.")
        self.refresh_results()
        return True

    def save_session(self, save_as=False, *, before_write=None):
        session = self.controller.session
        path = self.session_path
        if save_as or path is None:
            path = self._file_path("Save Fluttery session", "Fluttery session (*.fluttery)|*.fluttery", save=True,
                                   name=f"fluttery_session_{time.time_ns() // 1_000_000}.fluttery", extension="fluttery")
        if path is None:
            return False
        try:
            # A cancelled file picker must leave live measurements running.
            if before_write is not None:
                before_write()
            prepare = getattr(self.controller, "prepare_session_save", None)
            if callable(prepare) and prepare() is False:
                return False
            # Opening a measurement for review may leave the current bench
            # untouched. Its saved settings still describe the recorded run.
            settings = session.get("settings") or self.controller.session_settings()
            save_session(path, session, settings=settings)
        except (OSError, ValueError, TypeError, RecursionError) as error:
            self._show_error("Could not save session", error)
            return False
        self.session_path = path
        self.controller.session_saved()
        self._refresh_status()
        self._update_commands()
        self.controller.announce("Session saved.")
        return True

    def remove_input_audio(self):
        run = self._displayed_run()
        if not isinstance(run.get("inputAudio"), CapturedInput):
            return False
        if not self.controller.confirm("Remove the original input audio from this run? Fluttery will keep the readings.",
                                       "Remove Original Input Audio"):
            return False
        del run["inputAudio"]
        self.controller.session_changed()
        self._summary_key = None
        self.refresh_results()
        self.controller.workspace.update_commands()
        self.controller.announce("Fluttery removed the input audio. Save the session to update the file.")
        return True

    def export_input_audio(self):
        audio = self._displayed_run().get("inputAudio")
        if not isinstance(audio, CapturedInput) or not audio.blocks or self.is_exporting:
            return False
        segments = audio.segments()
        separate = len(segments) > 1
        extension = "zip" if separate else "wav"
        type_label = "WAV segments" if separate else "Audio WAV"
        path = self._file_path("Export Original Input Audio", f"{type_label} (*.{extension})|*.{extension}",
                               save=True, name=f"fluttery-input.{extension}", extension=extension)
        if path is None:
            return False

        def write():
            with atomic_output(path) as stream:
                if not separate:
                    write_input_wav(stream, segments[0])
                    return
                # A different rate, channel layout or gap starts a separate
                # WAV. Joining them would discard the actual capture timing.
                with ZipFile(stream, "w", compression=ZIP_DEFLATED) as archive:
                    for index, chunks in enumerate(segments, 1):
                        with archive.open(f"input-{index:03}-{chunks[0].rate}Hz.wav", "w") as entry:
                            write_input_wav(entry, chunks)

        return self._start_export("original input audio", write)

    def new_session(self):
        if not self.controller.confirm_replace_session() or not self.controller.session_loaded():
            return False
        self.session_path = None
        self.clear_comparison(announce=False)
        self.controller.announce("Session cleared.")
        self.refresh_results()
        self._update_commands()
        return True

    def review_run_settings(self):
        run = self._displayed_run()
        saved = run.get("runSettings") or run.get("settings")
        if saved:
            return self.controller.review_session_setup(saved)
        return False

    def open_comparison(self):
        path = self._file_path("Open comparison session", SESSION_FILTER)
        if path is None:
            return False
        try:
            comparison = load_session(path)
            if not comparison["history"]:
                raise ValueError("The selected session contains no continuous measurements to compare.")
        except (OSError, ValueError, RecursionError) as error:
            self._show_error("Could not open comparison", error)
            return False
        self.comparison, self.comparison_name = comparison, path.name
        # Attach the native pane to its visible owner before changing the book.
        self.controller.navigate("results")
        self.set_view("graph")
        self.controller.announce(f"Comparison session loaded: {path.name}.")
        return True

    def clear_comparison(self, *, announce=True):
        self.comparison = None
        self.refresh_results()
        if announce:
            self.controller.announce("Comparison cleared.")

    def choose_export_format(self):
        if self.is_exporting:
            self.controller.announce("A report export is already in progress.")
            return False
        with wx.SingleChoiceDialog(getattr(self.controller, "dialog_parent", self.GetTopLevelParent()), "Choose an export format.", "Export report", list(EXPORTS.values())) as dialog:
            dialog.SetHelpText(DESKTOP_HELP["export"])
            if dialog.ShowModal() != wx.ID_OK:
                return False
            format_name = tuple(EXPORTS)[dialog.GetSelection()]
        return self.export_report(format_name)

    def _report(self):
        settings = {**self.controller.session_settings(), "graphSource": self._graph_source()}
        return build_report(self._report_session(), settings, comparison=self.comparison,
                            comparison_name=self.comparison_name, live_spectrum=self._report_spectrum(),
                            plot_style=PlotStyle.from_settings(self.controller.settings), run_label=self._run_label())

    def _report_session(self):
        session = {key: value for key, value in self.controller.session.items()
                   if key not in (*RUN_KEYS, "previousRuns")}
        run = self._displayed_run()
        if self.run_choice.GetSelection() > 0:
            session["settings"] = run.get("runSettings") or {}
        session.update({key: value for key, value in run.items()
                        if key in RUN_KEYS and key != "inputAudio"})
        return session

    def _report_spectrum(self):
        run = self._displayed_run()
        if self._live_plot_active() and not run.get("spectra"):
            return self._capture_spectrum(self.controller.audio.snapshot()) or self._live_spectrum
        return None

    def _export_graph(self):
        self.refresh_results()
        graph = {"measurements": self.measurements.graph, "thd": self.distortion.graph}.get(
            self.view, self._graph_cache.get(self.view),
        )
        if graph is not None and (graph.traces or graph.heatmap is not None):
            return self.view, graph
        comparison = self.comparison["history"] if self.comparison else ()
        run = self._displayed_run()
        graphs = session_graphs(run["history"], self._graph_source(),
                                comparison=comparison, comparison_name=self.comparison_name,
                                live_spectrum=self._report_spectrum(), spectra=run.get("spectra"),
                                signal_loss=run.get("signalLoss") or (), run_label=self._run_label())
        if not graphs:
            self._show_error("No graph to export", "Record or open measurements before exporting a graph image.")
            return None
        if len(graphs) == 1:
            return next(iter(graphs.items()))
        with wx.SingleChoiceDialog(self.controller.dialog_parent, "Choose a graph to export.",
                                   "Export graph image", [graph.title for graph in graphs.values()]) as dialog:
            _help(dialog, "Select a stored graph using the arrow keys. Press Return to choose image settings.")
            if dialog.ShowModal() != wx.ID_OK:
                return None
            return tuple(graphs.items())[dialog.GetSelection()]

    def export_report(self, format_name):
        """Choose a destination on the UI thread and start one export at a time."""
        if self._export_thread is not None:
            self.controller.announce("A report export is already in progress.")
            return False
        format_name = format_name.lower()
        settings = {**self.controller.session_settings(), "graphSource": self._graph_source()}
        options = settings.get("exportSettings", {})
        template = options.get("filenameTemplate", DEFAULT_EXPORT["filenameTemplate"])
        # The metadata opt-out also keeps names and equipment out of the
        # suggested filename and the entry names in a ZIP archive.
        filename = report_filename(template, self.controller.session["metadata"],
                                   include_metadata=options.get("includeMetadata", True))
        if format_name == "png":
            selection = self._export_graph()
            if selection is None:
                return False
            graph_kind, graph = selection
            filename += "_" + GRAPH_FILENAMES.get(graph_kind, graph_kind)
            if graph_kind == self.view and (plot := self._current_plot()) is not None:
                return save_plot_image(plot, filename=filename)
            return save_results_graph(self, graph, filename)
        path = self._file_path(f"Export {EXPORTS[format_name]}", f"{EXPORTS[format_name]} (*.{format_name})|*.{format_name}",
                               save=True, name=f"{filename}.{format_name}", extension=format_name)
        if path is None:
            return False

        # Timers can append or trim history while rendering. Hand the worker a
        # private snapshot of session data, plot arrays and options so one report
        # always describes a single moment in time even on a busy measurement.
        session = deepcopy(self._report_session())
        comparison = deepcopy(self.comparison)
        comparison_name = self.comparison_name
        live_spectrum = deepcopy(self._report_spectrum())
        plot_style = PlotStyle.from_settings(self.controller.settings)
        run_label = self._run_label()

        def write():
            report = build_report(session, settings, comparison=comparison, comparison_name=comparison_name,
                                  live_spectrum=live_spectrum, plot_style=plot_style, run_label=run_label)
            write_export(path, format_name, report)

        return self._start_export(f"{format_name.upper()} report", write)

    def _start_export(self, label, write):
        def render():
            error = ""
            try:
                write()
            except Exception as failure:  # noqa: BLE001 - background task boundary
                # Renderer and file errors have a single UI entry point. The
                # atomic writer preserves any report previously at this path.
                error = str(failure) or type(failure).__name__
            app = wx.GetApp()
            if app is not None and app.IsMainLoopRunning():
                wx.CallAfter(self._export_finished, label, error)

        self._export_message = f"Exporting {label}…"
        self._refresh_status()
        self.controller.announce(f"Exporting {label}.")
        # Let a confirmed write finish if the window closes mid-export.
        self._export_thread = Thread(target=render, name="Fluttery export")
        try:
            self._export_thread.start()
        except RuntimeError as error:
            self._export_finished(label, str(error))
            return False
        return True

    def _export_finished(self, label, error):
        self._export_thread = None
        if not self or self.IsBeingDeleted():
            return
        self._export_message = ""
        self._refresh_status()
        if error:
            self._show_error(f"Could not export {label}", error)
        else:
            self.controller.announce(f"Exported {label}.")

    def print_report(self):
        if self._export_thread is not None:
            self.controller.announce("Wait for the current report export to finish before printing.")
            return False
        report = self._report()
        try:
            with wx.BusyCursor():
                printout = _ReportPrintout(report)
        except (OSError, ValueError, RuntimeError) as error:
            self._show_error("Could not prepare report for printing", error)
            return False
        paper = getattr(wx, "PAPER_" + report.options["paperSize"], wx.PAPER_A4)
        self._print_data.GetPrintData().SetPaperId(paper)
        printer = wx.Printer(self._print_data)
        if not printer.Print(getattr(self.controller, "dialog_parent", self.GetTopLevelParent()), printout, prompt=True):
            if wx.Printer.GetLastError() == wx.PRINTER_ERROR:
                self._show_error("Could not print report", "The system printer reported an error.")
            return False
        self._print_data = wx.PrintDialogData(printer.GetPrintDialogData())
        self.controller.announce("Report sent to the printer.")
        return True


class _ReportPrintout(wx.Printout):
    """Render the report's text, tables and graphs through native wx printing."""

    WIDTH = 690
    HEIGHT = 900
    MARGIN = 38

    def __init__(self, report):
        super().__init__("Fluttery measurement report")
        self.pages = [[]]
        self.line_height = 19
        self.cursor = 72
        # At the design width, monospaced wrapping is predictable on each
        # printer. Rows and images remain together when jumping to a new page.
        from textwrap import wrap

        self._lines([report.date_label])
        logo = report_logo_png(report.options)
        if logo:
            image = wx.Image(io.BytesIO(logo))
            scale = min(260 / image.GetWidth(), 68 / image.GetHeight())
            width = max(1, round(image.GetWidth() * scale))
            height = max(1, round(image.GetHeight() * scale))
            bitmap = wx.Bitmap(image.Scale(width, height, wx.IMAGE_QUALITY_HIGH))
            self.pages[-1].append(("bitmap", self.cursor, bitmap))
            self.cursor += height + 8

        for kind, name, content in report_sections(report):
            if kind == "graph":
                self._reserve(252)
                self._lines([content.title], heading=True)
                image = wx.Image(io.BytesIO(graph_png(content, plot_style=report.plot_style)))
                width = min(self.WIDTH - 2 * self.MARGIN, round(590 * report.options["scaleFactor"]))
                height = round(width * image.GetHeight() / image.GetWidth())
                bitmap = wx.Bitmap(image.Scale(width, height, wx.IMAGE_QUALITY_HIGH))
                self.pages[-1].append(("bitmap", self.cursor, bitmap))
                self.cursor += height + 8
                if report.options["includeAccessibility"]:
                    self._lines(wrap(content.description(), 86))
            elif kind == "text":
                self._reserve(3 * self.line_height)
                self._lines([name], heading=True)
                self._lines([line for paragraph in content.splitlines() for line in wrap(paragraph, 86)])
            else:
                self._reserve(4 * self.line_height)
                headers, rows = content
                width = 86 // len(headers)

                def columns(row, width=width):
                    wrapped = [wrap(str(value), width - 2) or [""] for value in row]
                    return ["".join(
                        (cell[index] if index < len(cell) else "").ljust(width)
                        for cell in wrapped
                    ).rstrip() for index in range(max(map(len, wrapped)))]

                self._lines([name], heading=True)
                self._lines(columns(headers), heading=True)
                for row in rows:
                    lines = columns(row)
                    if self._reserve(len(lines) * self.line_height + self.line_height):
                        self._lines(columns(headers), heading=True)
                    self._lines(lines)
            self.cursor += self.line_height // 2

    def _reserve(self, height):
        if self.cursor + height <= self.HEIGHT - 54:
            return False
        self.pages.append([])
        self.cursor = 72
        return True

    def _lines(self, lines, *, heading=False):
        for line in lines:
            self._reserve(self.line_height)
            self.pages[-1].append(("heading" if heading else "text", self.cursor, line))
            self.cursor += self.line_height

    def GetPageInfo(self):
        return 1, len(self.pages), 1, len(self.pages)

    def HasPage(self, page):
        return 1 <= page <= len(self.pages)

    def OnPrintPage(self, page):
        dc = self.GetDC()
        width, height = dc.GetSize()
        scale = min(width / self.WIDTH, height / self.HEIGHT)
        dc.SetUserScale(scale, scale)
        dc.SetDeviceOrigin(round((width - self.WIDTH * scale) / 2), round((height - self.HEIGHT * scale) / 2))
        dc.SetTextForeground(wx.BLACK)
        body = wx.Font(10, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL)
        emphasis = wx.Font(10, wx.FONTFAMILY_TELETYPE, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)
        header = wx.Font(17, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)
        dc.SetFont(header)
        dc.DrawText("Fluttery measurement report", self.MARGIN, 32)
        for kind, cursor, value in self.pages[page - 1]:
            if kind == "bitmap":
                dc.DrawBitmap(value, self.MARGIN, cursor)
            else:
                dc.SetFont(emphasis if kind == "heading" else body)
                dc.DrawText(value, self.MARGIN, cursor)
        dc.SetFont(body)
        dc.DrawText(f"Page {page} of {len(self.pages)}", self.MARGIN, self.HEIGHT - 40)
        return True
