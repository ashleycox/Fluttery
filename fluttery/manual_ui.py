"""The offline manual uses native document, list and table controls."""

from __future__ import annotations

from collections import Counter

import wx
import wx.dataview as dv

from .keyboard_access import describe
from .manual_content import CONTENTS, Manual


def _labelled(parent, layout, name, widget, help_text, *, stretch=0):
    label = wx.StaticText(parent, label=name)
    widget.SetName(name)
    if hasattr(widget, 'SetAccessibleName'):
        widget.SetAccessibleName(name)
    describe(widget, help_text)
    layout.Add(label, 0, wx.BOTTOM, parent.FromDIP(4))
    layout.Add(widget, stretch, wx.EXPAND)
    return widget


class ManualDialog(wx.Dialog):
    def __init__(self, parent, topic=CONTENTS):
        super().__init__(parent, title='Fluttery Help', style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.manual = Manual()
        self.topic = None
        self.matches = ()
        self._image = None
        self._resizing = None
        self._selecting = False
        self._shown_table = None
        body = wx.BoxSizer(wx.HORIZONTAL)
        self._make_navigation(body)
        self._make_document(body)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(body, 1, wx.EXPAND | wx.ALL, self.FromDIP(12))
        close = wx.Button(self, wx.ID_CANCEL, 'Close')
        describe(close, 'Close the manual and return to Fluttery.')
        outer.Add(close, 0, wx.ALIGN_RIGHT | wx.RIGHT | wx.BOTTOM, self.FromDIP(12))
        self.SetSizer(outer)
        self.SetEscapeId(wx.ID_CANCEL)
        self.SetMinSize(self.FromDIP((605, 430)))
        desktop = wx.GetClientDisplayRect()
        width, height = self.FromDIP((1040, 685))
        self.SetSize((min(width, desktop.width), min(height, desktop.height)))
        if parent is None:
            self.CentreOnScreen()
        else:
            self.CentreOnParent()
        self.Bind(wx.EVT_CHAR_HOOK, self._key)
        self._populate(topic)
        self.search.SetFocus()

    def _make_navigation(self, body):
        navigation = wx.Panel(self)
        controls = wx.BoxSizer(wx.VERTICAL)
        self.search = _labelled(navigation, controls, 'Search help', wx.TextCtrl(navigation, style=wx.TE_PROCESS_ENTER),
                                'Search topic titles and text. Press Return to move to the list of matching topics.')
        self.search.Bind(wx.EVT_TEXT, lambda _event: self._populate())
        self.search.Bind(wx.EVT_TEXT_ENTER, lambda _event: self.topics.SetFocus())
        self.match_count = wx.StaticText(navigation)
        controls.Add(self.match_count, 0, wx.TOP | wx.BOTTOM, self.FromDIP(8))
        self.topics = _labelled(navigation, controls, 'Topics and subtopics',
                                dv.DataViewListCtrl(navigation, style=dv.DV_SINGLE | dv.DV_NO_HEADER),
                                'Use the arrow keys to choose a topic or subtopic. Press F6 to move to the article.', stretch=1)
        self.topics.AppendTextColumn('Topic', width=self.FromDIP(275))
        self.topics.Bind(dv.EVT_DATAVIEW_SELECTION_CHANGED, self._on_topic)
        navigation.SetSizer(controls)
        navigation.SetMinSize(self.FromDIP((285, -1)))
        body.Add(navigation, 0, wx.EXPAND | wx.RIGHT, self.FromDIP(14))

    def _make_document(self, body):
        page = wx.Panel(self)
        content = wx.BoxSizer(wx.VERTICAL)
        self.title = wx.StaticText(page)
        self.title.SetFont(self.title.GetFont().Bold().Larger())
        content.Add(self.title, 0, wx.BOTTOM, self.FromDIP(8))
        self.mode = _labelled(page, content, 'View', wx.Choice(page),
                              'Show the article, illustrations or reference tables for this topic.')
        self.mode.Bind(wx.EVT_CHOICE, self._choose_mode)

        self.views = wx.Simplebook(page)
        article = wx.Panel(self.views)
        article_layout = wx.BoxSizer(wx.VERTICAL)
        self.article = _labelled(article, article_layout, 'Article',
                                 wx.TextCtrl(article, style=wx.TE_MULTILINE | wx.TE_READONLY),
                                 'Read or copy the text and illustration descriptions for the selected topic.', stretch=1)
        links = wx.Panel(article)
        link_layout = wx.BoxSizer(wx.HORIZONTAL)
        self.links = _labelled(links, link_layout, 'Related links', wx.Choice(links), 'Choose a related topic or website.', stretch=1)
        follow = wx.Button(links, label='Open Link')
        describe(follow, 'Open the selected topic or website.')
        follow.Bind(wx.EVT_BUTTON, self._open_link)
        link_layout.Add(follow, 0, wx.LEFT, self.FromDIP(8))
        links.SetSizer(link_layout)
        self.link_row = links
        article_layout.Add(links, 0, wx.EXPAND | wx.TOP, self.FromDIP(6))
        article.SetSizer(article_layout)
        self.views.AddPage(article, 'Article')

        pictures = wx.Panel(self.views)
        figures = wx.BoxSizer(wx.VERTICAL)
        self.figures = _labelled(pictures, figures, 'Illustration', wx.Choice(pictures), 'Choose an illustration from this topic.')
        self.figures.Bind(wx.EVT_CHOICE, self._choose_figure)
        self.caption = wx.StaticText(pictures)
        figures.Add(self.caption, 0, wx.EXPAND | wx.TOP | wx.BOTTOM, self.FromDIP(6))
        enlarge = wx.Button(pictures, label='Open Full-Size Image')
        describe(enlarge, 'Open the selected illustration in your system image viewer at its original resolution.')
        enlarge.Bind(wx.EVT_BUTTON, self._open_full_size)
        figures.Add(enlarge, 0, wx.BOTTOM, self.FromDIP(6))
        self.canvas = wx.Panel(pictures)
        self.bitmap = wx.StaticBitmap(self.canvas)
        self.canvas.Bind(wx.EVT_SIZE, self._schedule_picture)
        figures.Add(self.canvas, 1, wx.EXPAND)
        pictures.SetSizer(figures)
        self.views.AddPage(pictures, 'Illustrations')

        reference = wx.Panel(self.views)
        reference_layout = wx.BoxSizer(wx.VERTICAL)
        self.table_choice = _labelled(reference, reference_layout, 'Reference table', wx.Choice(reference),
                                      'Choose a reference table from this topic.')
        self.table_choice.Bind(wx.EVT_CHOICE, self._choose_table)
        self.table = _labelled(reference, reference_layout, 'Table entries', dv.DataViewListCtrl(reference),
                               'Use the arrow keys to read each row. The Article view also contains this table.', stretch=1)
        self.table.Bind(dv.EVT_DATAVIEW_SELECTION_CHANGED, self._read_table_row)
        self.table_detail = _labelled(reference, reference_layout, 'Selected row', wx.TextCtrl(
            reference, size=self.FromDIP((-1, 84)), style=wx.TE_MULTILINE | wx.TE_READONLY,
        ), 'Read or copy every column of the selected row, including long descriptions.')
        reference.SetSizer(reference_layout)
        self.views.AddPage(reference, 'Reference tables')
        content.Add(self.views, 1, wx.EXPAND | wx.TOP, self.FromDIP(6))
        page.SetSizer(content)
        body.Add(page, 1, wx.EXPAND)

    def _populate(self, requested=None):
        selected = self.manual.resolve(requested) if requested else self.topic
        query = self.search.GetValue().strip()
        self.matches = self.manual.search(query)
        self._selecting = True
        try:
            self.topics.DeleteAllItems()
            for topic in self.matches:
                # The full contents already groups every subtopic under its
                # chapter. Put the action first so it stays visible when narrow.
                label = f'{topic.title}  ·  {topic.chapter}' if query and topic.anchor else topic.title
                self.topics.AppendItem([('    ' if not query and topic.anchor else '') + label])
            if self.matches:
                selected = selected if selected in self.matches else self.matches[0]
                row = self.matches.index(selected)
                self.topics.SelectRow(row)
                self.topics.EnsureVisible(self.topics.RowToItem(row))
            count = len(self.matches)
            self.match_count.SetLabel(f'{count} topic' if count == 1 else f'{count} topics')
            self._show_topic(selected if self.matches else None)
        finally:
            self._selecting = False

    def _on_topic(self, event):
        row = self.topics.ItemToRow(event.GetItem())
        if not self._selecting and 0 <= row < len(self.matches):
            self._show_topic(self.matches[row])

    def _show_topic(self, topic):
        self.topic = topic
        self._shown_table = None
        self._modes = [(0, 'Article')]
        if topic and topic.illustrations:
            self._modes.append((1, 'Illustrations'))
        if topic and topic.tables:
            self._modes.append((2, 'Reference tables'))
        self.mode.Set([label for _, label in self._modes])
        self.mode.SetSelection(0)
        self.mode.Enable(len(self._modes) > 1)
        self.views.ChangeSelection(0)
        if topic:
            title, text = topic.title, topic.article
        elif self.manual.topics:
            title, text = 'No matching topics', 'Try a different word or clear the search to see all topics.'
        else:
            title, text = 'User manual unavailable', 'The user manual files are missing from this copy of Fluttery.'
        self.title.SetLabel(title)
        self.article.ChangeValue(text)
        self.article.SetInsertionPoint(0)
        self.links.Set([name for name, _ in topic.links] if topic else [])
        self.link_row.Show(bool(topic and topic.links))
        if topic and topic.links:
            self.links.SetSelection(0)
        parent = self.article.GetParent()
        parent.Layout()
        self.views.GetParent().Layout()

    def _choose_mode(self, _event):
        index = self.mode.GetSelection()
        if not 0 <= index < len(self._modes):
            return
        page, _ = self._modes[index]
        self.views.ChangeSelection(page)
        if page == 1:
            count = len(self.topic.illustrations)
            self.figures.Set([f'Figure {index + 1} of {count}' for index in range(count)])
            self.figures.SetSelection(0)
            self._choose_figure()
        elif page == 2:
            tables = self.topic.tables
            counts = Counter(table.title for table in tables)
            occurrences = Counter()
            labels = []
            for table in tables:
                occurrences[table.title] += 1
                count = counts[table.title]
                labels.append(f'{table.title} ({occurrences[table.title]} of {count})' if count > 1 else table.title)
            self.table_choice.Set(labels)
            self.table_choice.SetSelection(0)
            self._choose_table()

    def _choose_figure(self, _event=None):
        figure = self._selected_figure()
        if figure is None:
            return
        self.caption.SetLabel(figure.description)
        self.caption.Wrap(max(self.FromDIP(260), self.canvas.GetClientSize().width))
        self.caption.GetParent().Layout()
        with wx.LogNull():
            self._image = wx.Image(str(figure.path))
        if self._image.IsOk():
            self._draw_picture()
        else:
            self.bitmap.SetBitmap(wx.NullBitmap)
            self.caption.SetLabel(f'{figure.description}\nThis illustration is unavailable.')

    def _selected_figure(self):
        index = self.figures.GetSelection()
        if self.topic is not None and 0 <= index < len(self.topic.illustrations):
            return self.topic.illustrations[index]
        return None

    def _open_full_size(self, _event):
        if (figure := self._selected_figure()) is not None and not wx.LaunchDefaultApplication(str(figure.path)):
            wx.MessageBox('Fluttery could not open the selected illustration.', 'User manual', wx.OK | wx.ICON_INFORMATION, self)

    def _schedule_picture(self, event):
        if self._resizing is not None:
            self._resizing.Stop()
        self._resizing = wx.CallLater(55, self._draw_picture)
        event.Skip()

    def _draw_picture(self):
        if not self or self._image is None or not self._image.IsOk() or not self.canvas.IsShownOnScreen():
            return
        width, height = self.canvas.GetClientSize()
        scale = self.canvas.GetContentScaleFactor()
        fit = min(width * scale / self._image.GetWidth(), height * scale / self._image.GetHeight(), 1)
        if fit <= 0:
            return
        image = self._image.Scale(max(1, round(self._image.GetWidth() * fit)),
                                  max(1, round(self._image.GetHeight() * fit)), wx.IMAGE_QUALITY_HIGH)
        bitmap = wx.Bitmap(image)
        bitmap.SetScaleFactor(scale)
        self.bitmap.SetBitmap(bitmap)
        self.bitmap.Move((max(0, (width - self.bitmap.GetSize().width) // 2), 0))

    def _choose_table(self, _event=None):
        index = self.table_choice.GetSelection()
        if self.topic is None or not 0 <= index < len(self.topic.tables):
            return
        table = self.topic.tables[index]
        self.table.DeleteAllItems()
        self._shown_table = table
        self.table.ClearColumns()
        width = max(self.FromDIP(125), self.table.GetClientSize().width // len(table.columns) - 3)
        for heading in table.columns:
            self.table.AppendTextColumn(heading, width=width, flags=dv.DATAVIEW_COL_RESIZABLE)
        for row in table.rows:
            self.table.AppendItem(list(row))
        if table.rows:
            self.table.SelectRow(0)
            self._show_table_row(0)
        else:
            self.table_detail.ChangeValue('')

    def _read_table_row(self, event):
        if event.GetItem().IsOk():
            self._show_table_row(self.table.ItemToRow(event.GetItem()))

    def _show_table_row(self, index):
        table = self._shown_table
        if table is not None and 0 <= index < len(table.rows):
            text = '\n'.join(f'{heading} — {value}' for heading, value in zip(table.columns, table.rows[index], strict=True))
            self.table_detail.ChangeValue(text)
            self.table_detail.SetInsertionPoint(0)

    def _open_link(self, _event):
        index = self.links.GetSelection()
        if self.topic is None or not 0 <= index < len(self.topic.links):
            return
        _, target = self.topic.links[index]
        if target.startswith('https://'):
            if not wx.LaunchDefaultBrowser(target):
                wx.MessageBox('Fluttery could not open the selected link.', 'User manual', wx.OK | wx.ICON_INFORMATION, self)
        elif (topic := self.manual.resolve(target, self.topic.file)) is not None:
            self.search.ChangeValue('')
            self._populate(topic.address)
            self.article.SetFocus()

    def _key(self, event):
        if event.GetKeyCode() == ord('F') and event.CmdDown() and not (event.ShiftDown() or event.AltDown()):
            self.search.SetFocus()
            self.search.SelectAll()
            return
        if event.GetKeyCode() != wx.WXK_F6:
            event.Skip()
            return
        reading = (self.article, self.figures, self.table)[self.views.GetSelection()]
        focus = wx.Window.FindFocus()
        order = (self.search, self.topics, reading)
        index = next((i for i, widget in enumerate(order) if focus and (widget == focus or widget.IsDescendant(focus))), 2)
        order[(index + (-1 if event.ShiftDown() else 1)) % len(order)].SetFocus()
