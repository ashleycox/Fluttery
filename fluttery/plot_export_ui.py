"""Native image options, file picker and clipboard actions shared by the tools."""

from __future__ import annotations

import io
import re
from pathlib import Path
from threading import Thread

import wx

from .keyboard_access import describe
from .plot_export import (
    MAX_IMAGE_PIXELS,
    MAX_IMAGE_SIZE,
    MIN_IMAGE_HEIGHT,
    MIN_IMAGE_WIDTH,
    PlotCursor,
    PlotImageOptions,
    PlotSnapshot,
    PlotTrace,
    render_plot_image,
    write_plot_image,
)
from .plot_style import TRACE_COLORS, PlotStyle
from .preferences import DEFAULT_PLOT_IMAGE, normalize_plot_image

FORMATS = {'png': 'PNG image', 'svg': 'SVG vector image', 'pdf': 'PDF vector document'}
BACKGROUNDS = {'light': 'Light', 'dark': 'Dark', 'display': 'Match plot', 'transparent': 'Transparent'}
VISIBILITY = {None: 'Use plot setting', True: 'Show', False: 'Hide'}
REGIONS = {False: 'Current view', True: 'Full data range'}
SIZES = {
    'Slide (1600 × 900)': (1600, 900),
    'Report (1800 × 1200)': (1800, 1200),
    'Square (1400 × 1400)': (1400, 1400),
    'Wide (2400 × 1000)': (2400, 1000),
}


class ImageOptionsDialog(wx.Dialog):
    def __init__(self, parent, defaults, *, title=None, plot_size=None):
        super().__init__(parent, title='Save Plot Image' if title is not None else 'Image Export Defaults')
        self.result = None
        self.remember = None
        self._choices = {}
        self._sizing = False

        layout = wx.BoxSizer(wx.VERTICAL)
        image = self._section(layout, 'Image')
        self.format = self._choice(image, 'Format', FORMATS, defaults['format'],
                                   'Use PNG for a pixel image, or SVG or PDF for scalable text and traces.')
        self.background = self._choice(image, 'Background', BACKGROUNDS, defaults['background'],
                                       'Match the plot, choose light or dark, or use a transparent background.')
        self.formats = {**SIZES}
        if plot_size is not None:
            self.formats['Current plot proportions'] = plot_size
        sizes = {'Custom': 'Custom', **{name: name for name in self.formats}}
        self.size = self._choice(image, 'Size preset', sizes, 'Custom',
                                 'Choose a common image size, or type the width and height.')
        self.width = self._spin(image, 'Width (px)', MIN_IMAGE_WIDTH, MAX_IMAGE_SIZE, defaults['width'],
                                'Image width in pixels, or the width at the chosen resolution for a vector file.')
        self.height = self._spin(image, 'Height (px)', MIN_IMAGE_HEIGHT, MAX_IMAGE_SIZE, defaults['height'],
                                 'Image height in pixels, or the height at the chosen resolution for a vector file.')
        self.dpi = self._spin(image, 'Resolution (DPI)', 72, 600, defaults['dpi'],
                              'Set the print resolution. For PNG files, width and height are still measured in pixels.')
        self.size.Bind(wx.EVT_CHOICE, self._size_selected)
        for widget in (self.width, self.height):
            widget.Bind(wx.EVT_TEXT, self._size_changed)
        self._match_size()

        drawing = self._section(layout, 'Plot contents')
        self.region = self._choice(drawing, 'Axes', REGIONS, defaults['full_view'],
                                   'Save the view you zoomed into, or fit the entire captured data range.')
        self.legend = self._choice(drawing, 'Legend', VISIBILITY, defaults['show_legend'],
                                    'Choose whether to label the traces in the exported image.')
        self.grid = self._choice(drawing, 'Grid', VISIBILITY, defaults['show_grid'],
                                  'Choose whether to show grid lines in the exported image.')
        self.show_title = wx.CheckBox(drawing.GetStaticBox(), label='Include plot title')
        self.show_title.SetValue(defaults['show_title'])
        describe(self.show_title, 'Show the plot title above the data.')
        drawing.Add(self.show_title)
        self.title = None
        if title is not None:
            self.title = wx.TextCtrl(drawing.GetStaticBox(), value=title, name='Image title')
            self.title.SetMaxLength(150)
            self._row(drawing, 'Image title', self.title, 'Type a title for this image. Leave it blank to use the plot title.')
            self.title.Enable(self.show_title.GetValue())
            self.show_title.Bind(wx.EVT_CHECKBOX, lambda event: self.title.Enable(event.IsChecked()))
        self.cursors = wx.CheckBox(drawing.GetStaticBox(), label='Include comparison markers')
        self.cursors.SetValue(defaults['show_cursors'])
        describe(self.cursors, 'Include markers A and B when they are set on the plot.')
        drawing.Add(self.cursors)
        if title is not None:
            self.remember = wx.CheckBox(self, label='Use these options as defaults')
            describe(self.remember, 'Use these settings when exporting or copying other plots. The title applies only to this image.')
            layout.Add(self.remember, 0, wx.LEFT | wx.TOP, self.FromDIP(16))

        buttons = self.CreateSeparatedButtonSizer(wx.OK | wx.CANCEL)
        accepted = self.FindWindowById(wx.ID_OK)
        accepted.SetLabel('Choose File…' if title is not None else 'Save Defaults')
        describe(accepted, 'Choose where to save the image.' if title is not None else 'Store these defaults for plot images.')
        describe(self.FindWindowById(wx.ID_CANCEL), 'Close without applying these changes.')
        accepted.Bind(wx.EVT_BUTTON, self._accept)
        layout.Add(buttons, 0, wx.EXPAND | wx.ALL, self.FromDIP(12))
        self.SetSizerAndFit(layout)
        self.SetMinSize(self.GetSize())
        self.CentreOnParent()
        self.format.SetFocus()

    def _section(self, layout, label):
        section = wx.StaticBoxSizer(wx.VERTICAL, self, label)
        layout.Add(section, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, self.FromDIP(12))
        return section

    def _row(self, section, label, control, help_text):
        row = wx.BoxSizer(wx.HORIZONTAL)
        caption = wx.StaticText(section.GetStaticBox(), label=label, size=self.FromDIP((123, -1)))
        describe(caption, help_text)
        describe(control, help_text)
        row.Add(caption, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, self.FromDIP(6))
        row.Add(control, 1, wx.ALIGN_CENTER_VERTICAL)
        section.Add(row, 0, wx.EXPAND | wx.ALL, self.FromDIP(3))

    def _choice(self, section, label, labels, value, help_text):
        control = wx.Choice(section.GetStaticBox(), choices=list(labels.values()), name=label)
        self._choices[control] = tuple(labels)
        control.SetSelection(self._choices[control].index(value))
        self._row(section, label, control, help_text)
        return control

    def _spin(self, section, label, low, high, value, help_text):
        control = wx.SpinCtrl(section.GetStaticBox(), min=low, max=high, initial=value, name=label)
        self._row(section, label, control, help_text)
        return control

    def _value(self, control):
        return self._choices[control][control.GetSelection()]

    def _match_size(self):
        size = self.width.GetValue(), self.height.GetValue()
        name = next((name for name, dimensions in self.formats.items() if dimensions == size), 'Custom')
        self.size.SetStringSelection(name)

    def _size_selected(self, _event):
        if (dimensions := self.formats.get(self._value(self.size))) is not None:
            self._sizing = True
            try:
                self.width.SetValue(dimensions[0])
                self.height.SetValue(dimensions[1])
            finally:
                self._sizing = False

    def _size_changed(self, event):
        event.Skip()
        if not self._sizing:
            self._match_size()

    def _accept(self, _event):
        width, height = self.width.GetValue(), self.height.GetValue()
        if width * height > MAX_IMAGE_PIXELS:
            wx.MessageBox('Choose an image with at most 32 million pixels.', 'Image is too large',
                          wx.ICON_INFORMATION | wx.OK, self)
            self.width.SetFocus()
            return
        self.result = {
            'format': self._value(self.format), 'width': width, 'height': height, 'dpi': self.dpi.GetValue(),
            'background': self._value(self.background), 'full_view': self._value(self.region),
            'show_title': self.show_title.GetValue(), 'show_legend': self._value(self.legend),
            'show_grid': self._value(self.grid), 'show_cursors': self.cursors.GetValue(),
        }
        self.image_title = self.title.GetValue().strip() if self.title is not None else None
        self.EndModal(wx.ID_OK)


def _controller(parent):
    current = parent
    while current:
        if getattr(current, 'workspace', None) is not None and hasattr(current, 'apply_settings'):
            return current
        if (owner := getattr(current, 'controller', None)) is not None and hasattr(owner, 'apply_settings'):
            return owner
        current = current.GetParent()
    app = wx.GetApp()
    main = app.GetTopWindow() if app is not None else None
    return main if main is not None and hasattr(main, 'apply_settings') else None


def _message(parent, title, text, *, error=False, owner=None):
    if error:
        with wx.MessageDialog(parent, text, title, wx.OK | wx.ICON_ERROR) as dialog:
            dialog.ShowModal()
    elif (controller := _controller(owner if owner is not None else parent)) is not None:
        controller.announce(text)
    else:
        wx.LogMessage(text)


def _dialog_parent(plot):
    window = plot.GetTopLevelParent()
    if not window.IsShown() and (controller := _controller(plot)) is not None:
        return controller.dialog_parent
    return window


def _plot_dimensions(plot):
    width, height = plot.canvas.GetClientSize()
    scale = min(4, 1600 / max(width, 1), MAX_IMAGE_SIZE / max(height, 1))
    return (max(MIN_IMAGE_WIDTH, round(width * scale)), max(MIN_IMAGE_HEIGHT, round(height * scale)))


def _snapshot(plot):
    raw = plot.snapshot()
    if raw is None:
        return None
    return PlotSnapshot(
        title=raw['title'], x_label=raw['x_label'], y_label=raw['y_label'],
        traces=tuple(PlotTrace(**trace) for trace in raw['traces']), heatmap=raw['heatmap'],
        log_x=raw['log_x'], square_axes=raw['square_axes'], x_ticks=raw['x_ticks'] or None,
        x_bounds=raw['xlim'], y_bounds=raw['ylim'],
        full_x_bounds=raw.get('full_xlim'), full_y_bounds=raw.get('full_ylim'),
        cursors=tuple(PlotCursor(**item) for item in raw.get('markers', ())),
        appearance=raw.get('style', plot.appearance),
        system_dark=raw.get('system_dark', wx.SystemSettings.GetAppearance().IsDark()),
        grid_steps=raw.get('grid_steps', (None, None)),
        mark_time_origin=raw.get('mark_time_origin', False),
    )


def _destination(parent, title, image_format):
    name = re.sub(r'[\x00-\x1f<>:"/\\|?*]+', ' ', title).strip(' .')[:90] or 'Fluttery plot'
    suffix = '.' + image_format
    description = FORMATS[image_format]
    with wx.FileDialog(parent, 'Save Plot Image', defaultFile=name + suffix,
                       wildcard=f'{description} (*{suffix})|*{suffix}',
                       style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT) as dialog:
        if dialog.ShowModal() != wx.ID_OK:
            return None
        chosen = Path(dialog.GetPath())
    if chosen.suffix.lower() == suffix:
        return chosen
    destination = chosen.with_suffix(suffix)
    if destination.exists():
        answer = wx.MessageBox(f'Replace {destination.name}?', 'Replace Plot Image',
                               wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION, parent)
        if answer != wx.YES:
            return None
    return destination


def _clipboard_image(data):
    image = wx.Image(io.BytesIO(data), wx.BITMAP_TYPE_PNG)
    if not image.IsOk() or not wx.TheClipboard.Open():
        raise RuntimeError('The image clipboard is unavailable.')
    try:
        if not wx.TheClipboard.SetData(wx.BitmapDataObject(wx.Bitmap(image))):
            raise RuntimeError('The clipboard would not accept the plot image.')
        wx.TheClipboard.Flush()
    finally:
        wx.TheClipboard.Close()


def _run_export(plot, snapshot, options, image_format, path):
    # Save the snapshot before starting a worker. Neither drawing a vector file
    # nor encoding a large PNG should compete with the native event loop.
    plot._image_export_running = True

    def complete(data, error):
        if not plot or plot.IsBeingDeleted():
            return
        plot._image_export_running = False
        parent = _dialog_parent(plot)
        if error is None and path is None:
            try:
                _clipboard_image(data)
            except RuntimeError as failure:
                error = str(failure)
        if error is None:
            message = f'Saved {path.name}.' if path is not None else 'Plot image copied to the clipboard.'
            _message(parent, 'Plot image', message, owner=plot)
        else:
            _message(parent, 'Could not export plot image', error, error=True)

    def render():
        data, error = None, None
        try:
            if path is None:
                data = render_plot_image(snapshot, options, format='png')
            else:
                write_plot_image(path, snapshot, options, format=image_format)
        except Exception as failure:  # noqa: BLE001 - worker boundary
            error = str(failure) or type(failure).__name__
        app = wx.GetApp()
        if app is not None and app.IsMainLoopRunning():
            wx.CallAfter(complete, data, error)

    try:
        Thread(target=render, name='Fluttery plot image export').start()
    except RuntimeError as failure:
        complete(None, str(failure))
        return False
    return True


def save_plot_image(plot, *, quick=False, clipboard=False, filename=None):
    try:
        snapshot = _snapshot(plot)
    except ValueError as failure:
        _message(_dialog_parent(plot), 'Could not capture plot', str(failure), error=True)
        return False
    return _save_image(plot, snapshot, quick=quick, clipboard=clipboard, plot_size=_plot_dimensions(plot), filename=filename)


def save_results_graph(owner, graph, suggested_name):
    appearance = PlotStyle.from_settings(owner.controller.settings)
    try:
        traces = tuple(PlotTrace(palette_index=index, **{"color": TRACE_COLORS[index % len(TRACE_COLORS)], **trace})
                       for index, trace in enumerate(graph.traces))
        snapshot = PlotSnapshot(graph.title, graph.x_label, graph.y_label, traces=traces, heatmap=graph.heatmap,
                                log_x=graph.log_x, appearance=appearance,
                                system_dark=wx.SystemSettings.GetAppearance().IsDark())
    except ValueError as failure:
        _message(_dialog_parent(owner), 'Could not capture plot', str(failure), error=True)
        return False
    return _save_image(owner, snapshot, filename=suggested_name)


def _save_image(plot, snapshot, *, quick=False, clipboard=False, plot_size=None, filename=None):
    parent = _dialog_parent(plot)
    if getattr(plot, '_image_export_running', False):
        _message(parent, 'Plot image', 'A plot image is already being prepared.', owner=plot)
        return False
    if snapshot is None:
        _message(parent, 'No Plot Data', 'Capture or open data before exporting a plot image.', error=True)
        return False
    controller = _controller(plot)
    settings = controller.settings if controller is not None else {}
    defaults = settings.get('plotImageExport', DEFAULT_PLOT_IMAGE)
    options = defaults.copy()
    title = None
    remember = False
    if not (quick or clipboard):
        with ImageOptionsDialog(parent, defaults, title=snapshot.title, plot_size=plot_size) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return False
            options, title = dialog.result, dialog.image_title
            remember = dialog.remember.GetValue()
    image_format = 'png' if quick or clipboard else options['format']
    path = None if clipboard else _destination(parent, filename or title or snapshot.title, image_format)
    if path is None and not clipboard:
        return False
    if remember and controller is not None:
        controller.apply_settings(plotImageExport=options)
    image = PlotImageOptions(**{key: value for key, value in options.items() if key != 'format'}, title=title or None)
    return _run_export(plot, snapshot, image, image_format, path)


def configure_export_defaults(parent, controller):
    defaults = normalize_plot_image(controller.settings.get('plotImageExport'))
    with ImageOptionsDialog(parent, defaults) as dialog:
        if dialog.ShowModal() == wx.ID_OK:
            return controller.apply_settings(plotImageExport=dialog.result)
    return False


def plot_export_actions(plot):
    controller = _controller(plot)
    actions = (
        ('Save Plot Image…', lambda: save_plot_image(plot),
         'Save this plot as PNG, SVG or PDF. Choose the size, background and details to include.'),
        ('Quick Save PNG…', lambda: save_plot_image(plot, quick=True),
         'Choose a filename and save using the current image export defaults.'),
        ('Copy Plot Image', lambda: save_plot_image(plot, clipboard=True),
         'Copy a PNG of this plot to the clipboard using the current image export defaults.'),
    )
    if controller is not None:
        return (*actions, ('Graph Settings…', lambda: controller.navigate('settings', 'graphs'),
                           'Set plot appearance and image export defaults.'))
    return actions
