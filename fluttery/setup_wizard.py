"""First-run setup using the same native forms as the Settings window."""

from __future__ import annotations

import sys
from dataclasses import dataclass

import wx
from wx.lib.wordwrap import wordwrap

from .desktop import ICON
from .keyboard_access import describe, widget_tree
from .settings_ui import SETTINGS_HELP, SettingsPage
from .updates import REPOSITORY

DEVELOPER_SITE = 'https://ashleycox.co.uk/'

STEP_HELP = {
    'general': 'Choose how Fluttery looks and sounds, and which tool opens when you start it.',
    'audio': 'Configure your sound card or audio interface.',
    'measurement': 'Choose the reference tone on your test tape or record. A built-in profile can set up the meter for you.',
    'calibration': 'Use a known reference tone to check the sample clock or match the levels of the two inputs.',
    'graphs': 'Choose how plots look on screen and how Fluttery saves them as images.',
    'reports': 'Choose what you see in exported reports. You can set the paper size, add a logo and enter your name here.',
    'diagnostics': 'If you need to trace an audio issue, you can turn on the troubleshooting log here.',
}


@dataclass(frozen=True, slots=True)
class _SettingsStep:
    section: str
    groups: tuple
    part: int
    count: int


class SetupWizard(wx.Dialog):
    def __init__(self, parent, controller, *, first_launch=False):
        super().__init__(parent, title='Fluttery Setup Wizard', style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.controller = controller
        self.step = 0
        self._wrap_width = 0
        self._instruction_text = ''
        self._steps = []
        self._resize_pending = False
        self.progress = wx.StaticText(self)
        describe(self.progress, 'Check your progress through the setup wizard.')
        self.heading = wx.StaticText(self)
        self.heading.SetFont(self.heading.GetFont().Bold().Larger().Larger())
        describe(self.heading, 'This setup step.')
        self.instructions = wx.StaticText(self)
        self.book = wx.Simplebook(self)
        self.book.SetMinSize((1, 1))
        self.welcome = self._welcome_page()
        self.updates_page = self._updates_page()
        self.form = SettingsPage(self.book, controller, guided=True)
        self.book.AddPage(self.form, 'Settings')

        cancel_label = 'Set Up &Later' if first_launch else '&Close'
        self.cancel = wx.Button(self, wx.ID_CANCEL, cancel_label)
        self.manual = wx.Button(self, label='User &Manual…')
        self.back = wx.Button(self, wx.ID_BACKWARD, '&Back')
        self.next = wx.Button(self, wx.ID_OK, 'Next')
        describe(self.cancel, 'Close the wizard and keep the settings you already saved.')
        describe(self.manual, 'Open the illustrated Fluttery user manual without leaving setup.')
        describe(self.back, 'Return to the previous setup step.')
        describe(self.next, 'Save the settings on this page and continue.')
        self.back.Bind(wx.EVT_BUTTON, lambda event: self._show_step(self.step - 1))
        self.manual.Bind(wx.EVT_BUTTON, lambda event: self.controller._show_guide())
        self.next.Bind(wx.EVT_BUTTON, self._advance)
        self.SetEscapeId(wx.ID_CANCEL)
        self.SetAffirmativeId(wx.ID_OK)
        self.next.SetDefault()

        body = wx.BoxSizer(wx.VERTICAL)
        header = wx.BoxSizer(wx.VERTICAL)
        header.Add(self.progress, 0, wx.BOTTOM, self.FromDIP(4))
        header.Add(self.heading, 0, wx.BOTTOM, self.FromDIP(7))
        header.Add(self.instructions, 0, wx.EXPAND)
        body.Add(header, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, self.FromDIP(22))
        body.Add(self.book, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, self.FromDIP(12))
        body.Add(wx.StaticLine(self), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, self.FromDIP(18))
        buttons = wx.BoxSizer(wx.HORIZONTAL)
        buttons.Add(self.cancel)
        buttons.Add(self.manual, 0, wx.LEFT, self.FromDIP(8))
        buttons.AddStretchSpacer()
        buttons.Add(self.back, 0, wx.RIGHT, self.FromDIP(8))
        buttons.Add(self.next)
        body.Add(buttons, 0, wx.EXPAND | wx.ALL, self.FromDIP(16))
        self.SetSizer(body)
        self.SetMinSize(self._size_on_display((525, 430), parent or self))
        self.SetSize(self._size_on_display((700, 610), parent or self))
        if parent is None:
            self.CentreOnScreen()
        else:
            self.CentreOnParent()
        self.Bind(wx.EVT_SIZE, self._on_size)
        self.refit_steps()

    @property
    def total_steps(self):
        return 2 + len(self._steps)

    @property
    def settings_step(self):
        return self._steps[self.step - 2] if self.step >= 2 else None

    def first_step(self, section):
        return next(index for index, step in enumerate(self._steps, 2) if step.section == section)

    def _intro_page(self, title):
        page = wx.Panel(self.book)
        content = wx.BoxSizer(wx.VERTICAL)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(content, 0, wx.EXPAND | wx.ALL, self.FromDIP(12))
        page.SetSizer(outer)
        self.book.AddPage(page, title)
        return page, content

    def _welcome_page(self):
        page, body = self._intro_page('Welcome')
        scale = self.GetContentScaleFactor()
        pixels = round(self.FromDIP(72) * scale)
        image = wx.Image(str(ICON), wx.BITMAP_TYPE_PNG).Scale(pixels, pixels, wx.IMAGE_QUALITY_HIGH)
        icon = wx.Bitmap(image)
        icon.SetScaleFactor(scale)
        mark = wx.StaticBitmap(page, bitmap=icon, name='Fluttery app icon')
        body.Add(mark, 0, wx.BOTTOM, self.FromDIP(20))
        for label, address in (('Fluttery on GitHub', REPOSITORY), ('Ashley Cox’s website', DEVELOPER_SITE)):
            # wx.HyperlinkCtrl has no native accessibility node on macOS.
            link = wx.Button(page, label=label)
            describe(link, f'Open {label} in your browser.')
            link.Bind(wx.EVT_BUTTON, lambda event, address=address: self._open_link(address))
            body.Add(link, 0, wx.BOTTOM, self.FromDIP(12))
        return page

    def _open_link(self, address):
        if not wx.LaunchDefaultBrowser(address):
            self.controller.show_error('Could not open the website', address)

    def _updates_page(self):
        page, body = self._intro_page('Automatic updates')
        self.enable_updates = wx.CheckBox(page, label='Automatically check for updates')
        self.enable_updates.SetValue(self.controller.settings['checkForUpdates'])
        describe(self.enable_updates, SETTINGS_HELP['checkForUpdates'])
        body.Add(self.enable_updates)
        return page

    def _on_size(self, event):
        if self.GetClientSize().width != self._wrap_width:
            self.relayout()
        self.queue_refit()
        event.Skip()

    def apply_text_scale(self, factor):
        # Leave room for the navigation buttons at larger font sizes while
        # keeping the window inside the available display on a small screen.
        minimum = self._size_on_display(tuple(round(size * max(1.0, factor)) for size in (525, 430)), self)
        self.SetMinSize(minimum)
        size = tuple(max(current, limit) for current, limit in zip(self.GetSize(), minimum, strict=True))
        if size != tuple(self.GetSize()):
            self.SetSize(size)

    def _size_on_display(self, dimensions, window):
        display_index = wx.Display.GetFromWindow(window)
        display = wx.Display(display_index).GetClientArea() if display_index >= 0 else wx.GetClientDisplayRect()
        width, height = self.FromDIP(dimensions)
        return min(width, display.width - 24), min(height, display.height - 40)

    def queue_refit(self):
        if not self._resize_pending:
            self._resize_pending = True
            wx.CallAfter(self.refit_steps)

    def refit_steps(self):
        if not self or self.IsBeingDeleted():
            return
        self._resize_pending = False
        # Give settings pages the same height allowance across steps. The
        # introduction has a longer paragraph and uses its own page.
        chrome = (self.FromDIP(85) + self.progress.GetCharHeight() + self.heading.GetCharHeight()
                  + self.instructions.GetCharHeight() * 2 + self.next.GetBestSize().height)
        height = self.GetClientSize().height - chrome
        previous = self.settings_step
        steps = []
        for index, section in enumerate(self.form.SECTIONS):
            parts = list(self.form.book.GetPage(index).setup_parts(height))
            steps.extend(_SettingsStep(section, groups, part, len(parts)) for part, groups in enumerate(parts, 1))
        if steps == self._steps:
            return
        self._steps = steps
        if previous is None:
            self._show_step(self.step)
        else:
            # Keep the first group of the current step in view. If resizing
            # splits this step, Next will still reach the other groups.
            index = next(index for index, item in enumerate(steps, 2)
                         if item.section == previous.section and previous.groups[0] in item.groups)
            self._show_step(index, keep_focus=True)

    def relayout(self):
        self._wrap_width = self.GetClientSize().width
        width = max(self.FromDIP(240), self._wrap_width - self.FromDIP(44))
        # Set explicit line breaks and height. Native static text can cache its
        # initial single-line height before a modal window has been displayed.
        context = wx.ClientDC(self.instructions)
        context.SetFont(self.instructions.GetFont())
        label = wordwrap(self._instruction_text, width, context, breakLongWords=False)
        self.instructions.SetLabel(label)
        self.instructions.SetMinSize((-1, context.GetMultiLineTextExtent(label).height + self.FromDIP(2)))
        self.instructions.Show(bool(label))
        self.Layout()

    def _show_step(self, step, *, keep_focus=False):
        if not 0 <= step < self.total_steps:
            return
        self.step = step
        if step == 0:
            title = 'Welcome to Fluttery'
            settings_menu = 'Fluttery > Settings' if sys.platform == 'darwin' else 'Edit > Settings'
            summary = ('Fluttery is a tool for measuring and aligning tape machines and turntables, and for analysing '
                       'audio signals through your sound card or an audio interface. This wizard guides you through '
                       f'Fluttery’s settings. You can find these settings under {settings_menu}, or reopen this wizard '
                       'from the Help menu at any time.')
        elif step == 1:
            title = 'Automatic updates'
            summary = 'When automatic update checks are on, Fluttery lets you know when a new version is available.'
            if sys.platform == 'win32':
                summary += ' Fluttery opens the release page when you choose to download a Windows update.'
        else:
            part = self.settings_step
            index = self.form.SECTIONS.index(part.section)
            title = self.form.book.GetPageText(index)
            summary = STEP_HELP[part.section] if part.part == 1 else ''
            if part.count > 1:
                title += f' ({part.part} of {part.count})'
            self.form.select_section(part.section)
            self.form.book.GetPage(index).show_setup_part(part.groups)

        progress = f'Step {step + 1} of {self.total_steps}'
        self.progress.SetLabel(progress)
        self.heading.SetLabel(title)
        self.SetTitle(f'Fluttery Setup Wizard — {title} — {progress}')
        self._instruction_text = summary
        describe(self.instructions, summary)
        self.book.ChangeSelection(min(step, 2))
        self.back.Enable(step > 0)
        self.manual.Show(step in (0, self.total_steps - 1))
        label = 'Finish' if step == self.total_steps - 1 else 'Next'
        self.next.SetLabel(label if sys.platform == 'darwin' else f'&{label}')
        self.next.SetDefault()
        self.relayout()
        focus = wx.Window.FindFocus() if keep_focus else None
        if not focus or not self.IsDescendant(focus) or not focus.IsShownOnScreen():
            wx.CallAfter(self._focus_step, step)

    def _focus_step(self, step):
        if not self or not self.IsShown() or step != self.step:
            return
        # wx may measure a static label before its native font is available.
        self.relayout()
        focus = self.next
        if step == 1:
            focus = self.enable_updates
        elif step >= 2:
            # On Windows a focused push button temporarily takes over Return.
            # Keep Next as the default when a settings step only has actions.
            focus = next((widget for widget in widget_tree(self.form.book.GetCurrentPage())
                          if isinstance(widget, wx.Control) and not isinstance(widget, wx.Button) and widget.IsShownOnScreen()
                          and widget.AcceptsFocusFromKeyboard() and widget.IsEnabled()), self.next)
        if sys.platform == 'darwin':
            # Windows and GTK use the native title and focus events for each
            # step, without posting a separate system notification.
            self.controller.announce(f'{self.progress.GetLabel()}. {self.heading.GetLabel()}.')
        focus.SetFocus()

    def _advance(self, event):
        if self.step == 1 and not self.controller.apply_settings(checkForUpdates=self.enable_updates.GetValue()):
            return
        if (part := self.settings_step) and not self.form.apply(feedback=False, section=part.section, groups=part.groups):
            return
        if self.step == self.total_steps - 1:
            self.EndModal(wx.ID_OK)
        else:
            self._show_step(self.step + 1)
