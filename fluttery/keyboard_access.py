"""Keyboard access to the native commands and controls in a Fluttery window."""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from dataclasses import dataclass

import wx
from wx import dataview

MODIFIER = 'Command' if sys.platform == 'darwin' else 'Control'
KEY_PREFIX = f'{MODIFIER}+Shift+K'
HELP_KEY = f'{MODIFIER}+Shift+H'
LETTERS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'


def plain_label(label):
    label = label.split('\t', 1)[0]
    return re.sub(r'(?<!&)&(?=\w)', '', label).replace('&&', '&').strip()


def label_of(widget):
    label = plain_label(widget.GetLabel())
    if label and isinstance(widget, (wx.StaticText, wx.StaticBox, wx.Button, wx.CheckBox, wx.RadioButton)):
        return label
    name = widget.GetName()
    if name and name.lower() not in {'window', 'panel', 'text', 'choice', 'button', 'gauge', 'notebook', 'dataview',
                                     'checkbox', 'statictext', 'spinctrl', 'spinctrldouble', 'listctrl',
                                     'scrollbar', 'scrolledwindow', 'wxbutton', 'list', 'radiobutton'}:
        return plain_label(name)
    return label


def describe(widget, text):
    """Register authored help before native rendering, including in a new dialog."""
    widget._fluttery_help = text
    root = wx.GetTopLevelParent(widget)
    presentation = text if getattr(root, '_fluttery_help_enabled', True) else ''
    widget.SetToolTip(presentation)
    widget.SetHelpText(presentation)


def description_of(widget):
    authored = getattr(widget, '_description', None) or getattr(widget, '_fluttery_help', None)
    if authored is None:
        authored = widget.GetToolTipText() or widget.GetHelpText() or fallback_help(widget)
        widget._fluttery_help = authored
    return authored


def fallback_help(widget):
    label = label_of(widget)
    if not label:
        return ''
    if isinstance(widget, wx.Button):
        return f'Choose “{label}”.'
    if isinstance(widget, wx.CheckBox):
        return f'Select or clear “{label}”.'
    if isinstance(widget, wx.RadioButton):
        return f'Select “{label}”.'
    if isinstance(widget, (wx.Choice, wx.ComboBox)):
        return f'Press Space to open the “{label}” list, then use the arrow keys to choose an option.'
    if isinstance(widget, (wx.SpinCtrl, wx.SpinCtrlDouble, wx.Slider)):
        return f'Use the arrow keys to adjust the value of “{label}”.'
    if isinstance(widget, wx.TextCtrl):
        if widget.IsEditable():
            return f'Enter or edit the text in “{label}”.'
        return f'Read or copy the text in “{label}”.'
    if isinstance(widget, (wx.StaticText, wx.StaticBox, wx.Gauge, wx.Notebook, dataview.DataViewCtrl, wx.ListBox)):
        return label.rstrip('.') + '.'
    return ''


def widget_tree(root, *, include_composite_children=False):
    """Yield app controls without descending into the internals of composite controls."""
    yield root
    if (not include_composite_children and isinstance(root, wx.Control)
            and not isinstance(root, (wx.BookCtrlBase, wx.StaticBox))):
        return
    for child in root.GetChildren():
        if not isinstance(child, wx.TopLevelWindow):
            yield from widget_tree(child, include_composite_children=include_composite_children)


def apply_help(root, enabled):
    if root is None:
        return
    root._fluttery_help_enabled = enabled
    for widget in widget_tree(root, include_composite_children=True):
        details = description_of(widget)
        tag = details if enabled else ''
        if widget.GetToolTipText() != tag:
            widget.SetToolTip(tag)
        if widget.GetHelpText() != tag:
            widget.SetHelpText(tag)


@dataclass(frozen=True, slots=True)
class Command:
    label: str
    action: Callable[[], object]
    description: str
    category: str
    native_shortcut: str = ''
    available: bool = True


def numbered_commands(commands):
    # Allocate chords in traversal order, including temporarily unavailable
    # commands. Disabled fields keep their codes when options are enabled.
    return {
        LETTERS[index // len(LETTERS)] + LETTERS[index % len(LETTERS)]: command
        for index, command in enumerate(commands)
    }


def _dispatch(handler, event_type, identifier, widget=None):
    event = wx.CommandEvent(event_type, identifier)
    if widget is not None:
        event.SetEventObject(widget)
    handler.ProcessEvent(event)


def _dispatch_menu(handler, identifier, label):
    # The active instrument can update its commands while the picker is open.
    # A menu ID reused for a different command must not run the new action.
    if (handler and (bar := handler.GetMenuBar()) is not None and (item := bar.FindItemById(identifier)) is not None
            and item.IsEnabled() and plain_label(item.GetItemLabel()) == label):
        _dispatch(handler, wx.EVT_MENU.typeId, identifier)


def _command_source(menu, handler, category, description, hidden_ids):
    for item in menu.GetMenuItems():
        if item.IsSeparator() or item.GetId() in hidden_ids:
            continue
        name = plain_label(item.GetItemLabel())
        if item.GetSubMenu() is not None:
            yield from _command_source(item.GetSubMenu(), handler, category + ' › ' + name, description, hidden_ids)
            continue
        if item.GetId() in (wx.ID_SEPARATOR, wx.ID_NONE):
            continue
        key = item.GetId()
        yield Command(name, lambda key=key, name=name: _dispatch_menu(handler, key, name),
                      description(key) or name.rstrip('….') + '.', category,
                      item.GetItemLabel().partition('\t')[2].replace('Ctrl', MODIFIER), item.IsEnabled())


class KeyboardAccess(wx.EventFilter):
    """Expose commands by two-letter shortcuts while preserving native accelerators."""

    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self._showing = False
        wx.EvtHandler.AddFilter(self)

    @property
    def show_help(self):
        return self.controller.settings['showHelpTags']

    def refresh(self):
        wx.ToolTip.Enable(self.show_help)
        for window in self.controller.workspace.windows.values():
            apply_help(window, self.show_help)
        for dialog in (self.controller._setup_wizard, self.controller._sweep_dialog,
                       *self.controller.instrument_dialogs.values()):
            if dialog:
                apply_help(dialog, self.show_help)

    def close(self):
        wx.EvtHandler.RemoveFilter(self)

    def _owner(self, window):
        # Follow the parent of an application-owned modal dialog too.
        while window:
            if (self.controller.workspace.owns(window) or self.controller._setup_wizard is window
                    or self.controller._sweep_dialog is window or window in self.controller.instrument_dialogs.values()):
                return window
            window = window.GetParent()
        return None

    def FilterEvent(self, event):
        event_type = event.GetEventType()
        if event_type == wx.EVT_SHOW.typeId and event.IsShown():
            widget = event.GetEventObject()
            if isinstance(widget, wx.Dialog) and self._owner(widget) is not None:
                apply_help(widget, self.show_help)
        if event_type != wx.EVT_CHAR_HOOK.typeId or self._showing:
            return wx.EventFilter.Event_Skip
        if not event.CmdDown() or not event.ShiftDown() or event.AltDown():
            return wx.EventFilter.Event_Skip
        key = event.GetKeyCode()
        if key not in (ord('K'), ord('H')):
            return wx.EventFilter.Event_Skip
        target = event.GetEventObject()
        focus = target if isinstance(target, wx.Window) else wx.Window.FindFocus()
        window = wx.GetTopLevelParent(focus) if focus is not None else wx.GetActiveWindow()
        if self._owner(window) is None:
            return wx.EventFilter.Event_Skip
        action = self.show_commands if key == ord('K') else self.show_focused_help
        wx.CallAfter(action, window)
        return wx.EventFilter.Event_Processed

    def _reveal_page(self, widget):
        settings = self.controller.settings_page
        page = widget
        while page is not None and page.GetParent() != settings.book:
            page = page.GetParent()
        if page is not None:
            index = settings.book.FindPage(page)
            if index != wx.NOT_FOUND:
                settings.select_section(settings.SECTIONS[index])

    def _focus_control(self, widget, *, reveal=False):
        if reveal:
            self._reveal_page(widget)
        widget.SetFocus()

    def _run_control(self, widget, *, reveal=False):
        if reveal:
            self._reveal_page(widget)
        _dispatch(widget.GetEventHandler(), wx.EVT_BUTTON.typeId, widget.GetId(), widget)

    def _read_element(self, widget, label):
        read = getattr(widget, '_fluttery_read_value', widget.GetLabel)
        value = str(read())
        text = '\n'.join(dict.fromkeys(part for part in (description_of(widget), value) if part))
        self.controller._show_text(label, text)

    def _control_commands(self, window, widgets):
        settings = window is self.controller.settings_page.GetTopLevelParent()
        controls = {label_of(widget) for widget in widgets if isinstance(widget, wx.Control)
                    and not isinstance(widget, (wx.StaticText, wx.StaticBox))}
        for widget in widgets:
            if isinstance(widget, (wx.StaticLine, wx.StatusBar, wx.ToolBar, wx.TopLevelWindow, wx.Simplebook)):
                continue
            label = label_of(widget)
            if not label or not isinstance(widget, wx.Control):
                continue
            accepts_focus = widget.AcceptsFocusFromKeyboard()
            read_only = isinstance(widget, (wx.StaticText, wx.Gauge, wx.StaticBox))
            if not accepts_focus and not read_only:
                continue
            if isinstance(widget, wx.StaticText) and label in controls and not hasattr(widget, '_fluttery_read_value'):
                continue  # The label and its field are one destination.
            if read_only:
                action = lambda widget=widget, label=label: self._read_element(widget, label)
                title = 'Read ' + label
            elif isinstance(widget, wx.Button):
                action = lambda widget=widget: self._run_control(widget, reveal=settings)
                title = label
            else:
                action = lambda widget=widget: self._focus_control(widget, reveal=settings)
                title = 'Focus ' + label
            parent = widget.GetParent()
            group = f'Controls › {plain_label(parent.GetLabel())}' if isinstance(parent, wx.StaticBox) else 'Controls'
            yield Command(title, action, description_of(widget), group,
                          available=widget.IsEnabled() and (widget.IsShownOnScreen() or settings))

    def commands(self, window):
        workspace = self.controller.workspace
        if (slot := workspace._slot_for_frame(window)) not in (None, 'settings'):
            panel = workspace._active_panel(slot)
            widgets = tuple(widget_tree(panel)) if panel is not None else ()
            if window is workspace.main and workspace.layout == 'tabs':
                widgets = (workspace.book, *widgets)
        elif window is self.controller._setup_wizard:
            widgets = tuple(widget for widget in widget_tree(window) if widget.IsShownOnScreen())
        else:
            widgets = tuple(widget_tree(window))
        menus = []
        if isinstance(window, wx.Frame) and (bar := window.GetMenuBar()) is not None:
            hidden_ids = {item.GetId() for item in getattr(window, 'hidden_measurement_items', ())}
            for index in range(bar.GetMenuCount()):
                label = plain_label(bar.GetMenuLabel(index))
                if label:
                    menus.extend(_command_source(bar.GetMenu(index), window, label, workspace.menu_description, hidden_ids))
            menus.extend(Command(label, action, details, 'Wow & Flutter', shortcut.replace('Ctrl', MODIFIER))
                         for label, shortcut, action, details in workspace.background_measurement_shortcuts(window))
        # Prefer the native menu entry when it also gives the same local
        # command a direct shortcut, such as Reset Counter.
        accelerated = {command.label for command in menus if command.native_shortcut}
        commands = []
        declared = set()
        for widget in widgets:
            if widget.IsShownOnScreen() and callable(actions := getattr(widget, 'keyboard_actions', None)):
                for label, action, details in actions():
                    if label in declared or label in accelerated:
                        continue
                    declared.add(label)
                    category = 'Plot' if widget.__class__.__name__ == 'LinePlot' else 'Actions'
                    commands.append(Command(label, action, details, category))
        # Put local controls first. The operating system can rearrange native menu items on activation.
        commands.extend(command for command in self._control_commands(window, widgets)
                        if command.label not in declared and command.label not in accelerated)
        if isinstance(window, wx.Frame):
            commands.extend(menus)
            status = window.GetStatusBar()
            if status is not None:
                def read_status():
                    values = [status.GetStatusText(i) for i in range(status.GetFieldsCount())]
                    self.controller._show_text('Window status', '\n'.join(filter(None, values)))

                commands.append(Command('Read window status', read_status, 'Read the status and signal state.', 'Window'))
        return numbered_commands(commands)

    def show_focused_help(self, window=None):
        if window is not None and not window:
            return
        window = window or self.controller.dialog_parent
        focus = wx.Window.FindFocus()
        if focus is not None:
            details = description_of(focus)
            if details:
                self.controller._show_text(label_of(focus) or 'Control Help', details, parent=window)
                return
        self.controller._show_text('Control Help', f'Use {KEY_PREFIX} to find a command or control in this window.', parent=window)

    def show_commands(self, window=None):
        if self._showing or (window is not None and not window):
            return
        window = window or self.controller.dialog_parent
        if window is None:
            window = self.controller.navigate('measure').GetTopLevelParent()
        self._showing = True
        try:
            with CommandDialog(window, self.commands(window)) as dialog:
                chosen = dialog.chosen if dialog.ShowModal() == wx.ID_OK else None
        finally:
            self._showing = False
        if chosen is not None:
            # Let native modal teardown restore the parent before moving focus or opening another dialog.
            wx.CallAfter(chosen.action)


class CommandDialog(wx.Dialog):
    def __init__(self, parent, commands):
        super().__init__(parent, title='Keyboard Commands', style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.commands = commands
        self.chosen = None
        self._first_key = ''
        self._rows = []
        self.instructions = wx.StaticText(
            self, label='Type a command’s two letters or select it and press Return.\n'
                        'F6 switches between search and the list. F7 moves to the description.',
        )
        search_label = wx.StaticText(self, label='Search commands and controls')
        self.search = wx.TextCtrl(self, name='Search commands and controls')
        self.table = dataview.DataViewListCtrl(self, style=dataview.DV_SINGLE)
        self.table.SetName('Keyboard commands')
        self.table.AppendTextColumn('Keys', width=self.FromDIP(78))
        self.table.AppendTextColumn('Command or control', width=self.FromDIP(565))
        self.details = wx.TextCtrl(self, name='Command description', style=wx.TE_MULTILINE | wx.TE_READONLY,
                                  size=self.FromDIP((-1, 62)))
        details_label = wx.StaticText(self, label='Command description')
        for widget, text in (
            (self.search, 'Filter by command name, group or shortcut letters. Press F6 to return to the list.'),
            (self.table, 'Use the Up and Down arrow keys to review the list, or type the two letters shown for a command.'),
            (self.details, 'Read what the selected command does and which keyboard shortcut to use. Press F7 to reach this description.'),
        ):
            describe(widget, text)
        run = wx.Button(self, wx.ID_OK, 'Run')
        cancel = wx.Button(self, wx.ID_CANCEL, 'Cancel')
        describe(run, 'Run the selected command, or focus the selected control.')
        describe(cancel, 'Close this list and return to the current window.')
        buttons = wx.StdDialogButtonSizer()
        buttons.AddButton(run)
        buttons.AddButton(cancel)
        buttons.Realize()
        layout = wx.BoxSizer(wx.VERTICAL)
        for control in (self.instructions, search_label, self.search):
            layout.Add(control, 0, wx.EXPAND | wx.ALL, self.FromDIP(5))
        layout.Add(self.table, 1, wx.EXPAND | wx.ALL, self.FromDIP(5))
        for control in (details_label, self.details, buttons):
            layout.Add(control, 0, wx.EXPAND | wx.ALL, self.FromDIP(5))
        self.SetSizer(layout)
        self.SetSize(self.FromDIP((680, 520)))
        self.CentreOnParent()
        self.search.Bind(wx.EVT_TEXT, lambda event: self._filter())
        self.table.Bind(dataview.EVT_DATAVIEW_SELECTION_CHANGED, lambda event: self._selection_changed())
        self.table.Bind(dataview.EVT_DATAVIEW_ITEM_ACTIVATED, lambda event: self._choose_selected())
        run.Bind(wx.EVT_BUTTON, lambda event: self._choose_selected())
        self.Bind(wx.EVT_CHAR_HOOK, self._key)
        self.Bind(wx.EVT_SHOW, self._shown)
        self._filter()
        self.table.SetFocus()

    def _shown(self, event):
        event.Skip()
        if event.GetEventObject() is self and event.IsShown():
            # A native modal can restore its default control during activation.
            # Set the initial focus when the list can receive its two-letter command.
            wx.CallAfter(self._focus_list_when_shown)

    def _focus_list_when_shown(self):
        if self and self.IsShown():
            self.table.SetFocus()

    def _filter(self):
        self._first_key = ''
        text = self.search.GetValue().strip().casefold()
        self.table.DeleteAllItems()
        self._rows = [code for code, command in self.commands.items()
                      if text in f'{code} {" ".join(code)} {command.category} {command.label}'.casefold()]
        for code in self._rows:
            command = self.commands[code]
            suffix = '' if command.available else ' (unavailable)'
            self.table.AppendItem([' '.join(code), f'{command.category} — {command.label}{suffix}'])
        if self._rows:
            self.table.SelectRow(0)
        self._selection_changed()

    def _selection_changed(self):
        index = self.table.GetSelectedRow()
        if 0 <= index < len(self._rows):
            code = self._rows[index]
            command = self.commands[code]
            extra = f' You can also press {command.native_shortcut}.' if command.native_shortcut else ''
            self.details.ChangeValue(f'Press {KEY_PREFIX}, then type {" ".join(code)}. {command.description}{extra}')
        else:
            self.details.ChangeValue('No commands match your search.')

    def _choose_selected(self):
        index = self.table.GetSelectedRow()
        if 0 <= index < len(self._rows):
            self._choose(self._rows[index])

    def _choose(self, code):
        command = self.commands.get(code)
        if command is not None and command.available:
            self.chosen = command
            self.EndModal(wx.ID_OK)
        elif command is not None:
            self.instructions.SetLabel(f'“{command.label}” is currently unavailable.')
        else:
            self.instructions.SetLabel(f'No command uses {" ".join(code)}.')

    def _key(self, event):
        key = event.GetKeyCode()
        if key == wx.WXK_F6:
            self._first_key = ''
            (self.table if self.search.HasFocus() else self.search).SetFocus()
            return
        if key == wx.WXK_F7:
            self._first_key = ''
            self.details.SetInsertionPoint(0)
            self.details.SetFocus()
            return
        focus = wx.Window.FindFocus()
        in_table = focus is not None and (focus == self.table or self.table.IsDescendant(focus))
        if in_table and not event.HasAnyModifiers() and ord('A') <= key <= ord('Z'):
            if self._first_key:
                code, self._first_key = self._first_key + chr(key), ''
                self._choose(code)
            else:
                self._first_key = chr(key)
                self.instructions.SetLabel(f'{self._first_key}. Type the second letter.')
            return
        self._first_key = ''
        if key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER) and (focus == self.search or in_table):
            self._choose_selected()
        else:
            event.Skip()
