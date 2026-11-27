"""Native macOS permission, accessibility and update integration."""

from __future__ import annotations

import ctypes as ct
import plistlib
import sys
from functools import lru_cache
from pathlib import Path

Pointer = ct.c_void_p
MICROPHONE_UNDETERMINED = 0
MICROPHONE_AUTHORIZED = 3


@lru_cache(maxsize=1)
def _runtime():
    library = ct.CDLL('/usr/lib/libobjc.A.dylib')
    for name in ('objc_getClass', 'sel_registerName'):
        function = getattr(library, name)
        function.argtypes = [ct.c_char_p]
        function.restype = Pointer
    return library


@lru_cache(maxsize=20)
def _send(restype, argtypes):
    # A fixed signature per selector call is important for ARM64's calling convention.
    return ct.CFUNCTYPE(restype, Pointer, Pointer, *argtypes)(('objc_msgSend', _runtime()))


def _call(target, selector, *arguments, result=Pointer, argtypes=()):
    return _send(result, argtypes)(target, _runtime().sel_registerName(selector), *arguments)


def _class(name):
    return _runtime().objc_getClass(name)


@lru_cache(maxsize=1)
def _appkit():
    framework = ct.CDLL('/System/Library/Frameworks/AppKit.framework/AppKit')
    framework.NSAccessibilityPostNotificationWithUserInfo.argtypes = [Pointer, Pointer, Pointer]
    framework.NSAccessibilityPostNotificationWithUserInfo.restype = None
    return framework


def announce(message):
    """Post an announcement through the current NSWindow without changing focus."""
    appkit = _appkit()
    application = _call(_class(b'NSApplication'), b'sharedApplication')
    window = _call(application, b'keyWindow') or _call(application, b'mainWindow')
    if not window:
        return
    key = Pointer.in_dll(appkit, 'NSAccessibilityAnnouncementKey')
    event = Pointer.in_dll(appkit, 'NSAccessibilityAnnouncementRequestedNotification')
    text = _call(_class(b'NSString'), b'stringWithUTF8String:', message.encode('utf-8'), argtypes=(ct.c_char_p,))
    info = _call(_class(b'NSDictionary'), b'dictionaryWithObject:forKey:', text, key, argtypes=(Pointer, Pointer))
    appkit.NSAccessibilityPostNotificationWithUserInfo(window, event, info)


def _string(value):
    return _call(_class(b'NSString'), b'stringWithUTF8String:', value, argtypes=(ct.c_char_p,))


def accessibility_group(view, label):
    """Keep a stock wx container as a named group in AppKit's accessibility tree."""
    _call(view, b'setAccessibilityElement:', True, result=None, argtypes=(ct.c_bool,))
    _call(view, b'setAccessibilityRole:', _string(b'AXGroup'), result=None, argtypes=(Pointer,))
    _call(view, b'setAccessibilityLabel:', _string(label.encode('utf-8')), result=None, argtypes=(Pointer,))


def keep_window_menu():
    """Keep Fluttery's own Window commands when wx installs a menu bar."""
    import wx

    if setter := getattr(wx.MenuBar, 'SetAutoWindowMenu', None):
        setter(False)
        return True
    # Phoenix omits wxMenuBar::SetAutoWindowMenu, an inline C++ setter.
    # Resolve its backing flag from the same loaded extension's dependencies.
    # With the flag on, wx clears the native Window menu on every activation.
    try:
        core = ct.CDLL(wx._core.__file__)
        ct.c_bool.in_dll(core, '_ZN9wxMenuBar19s_macAutoWindowMenuE').value = False
    except (OSError, ValueError):
        return False
    return True


def arrange_windows():
    application = _call(_class(b'NSApplication'), b'sharedApplication')
    _call(application, b'arrangeInFront:', None, result=None, argtypes=(Pointer,))


def hide_menu_shortcuts(menu, titles):
    """Keep Cocoa key equivalents available without adding visible global commands."""
    import wx
    from wx import siplib

    # wxMenu::GetHMenu exposes its NSMenu in C++, but is not wrapped by Phoenix.
    try:
        get_menu = ct.CDLL(wx._core.__file__)._ZNK6wxMenu8GetHMenuEv
    except (AttributeError, OSError):
        return False
    get_menu.argtypes = [Pointer]
    get_menu.restype = Pointer
    native_menu = get_menu(siplib.unwrapinstance(menu))
    items = [_call(native_menu, b'itemWithTitle:', _string(title.encode('utf-8')), argtypes=(Pointer,)) for title in titles]
    if not all(items):
        return False
    for item in items:
        _call(item, b'setAllowsKeyEquivalentWhenHidden:', True, result=None, argtypes=(ct.c_bool,))
        _call(item, b'setHidden:', True, result=None, argtypes=(ct.c_bool,))
    return True


@lru_cache(maxsize=1)
def _capture_api():
    framework = ct.CDLL('/System/Library/Frameworks/AVFoundation.framework/AVFoundation', mode=ct.RTLD_GLOBAL)
    return _class(b'AVCaptureDevice'), Pointer.in_dll(framework, 'AVMediaTypeAudio')


def microphone_authorization():
    """Return AVFoundation's status, or defer to the host when running from Python."""
    bundle = _call(_class(b'NSBundle'), b'mainBundle')
    bundle_id = _call(bundle, b'bundleIdentifier')
    if not bundle_id:
        return None
    identifier = _call(bundle_id, b'UTF8String', result=ct.c_char_p)
    if identifier not in (b'com.ashleycox.fluttery', b'com.ashleycox.fluttery.local'):
        return None
    description = _call(bundle, b'objectForInfoDictionaryKey:', _string(b'NSMicrophoneUsageDescription'),
                        argtypes=(Pointer,))
    if not description:
        raise RuntimeError('The application bundle is missing its microphone usage description.')
    device, media = _capture_api()
    return _call(device, b'authorizationStatusForMediaType:', media, result=ct.c_long, argtypes=(Pointer,))


_PermissionCompletion = ct.CFUNCTYPE(None, Pointer, ct.c_bool)


class _BlockDescriptor(ct.Structure):
    _fields_ = [('reserved', ct.c_ulong), ('size', ct.c_ulong)]


class _PermissionBlock(ct.Structure):
    _fields_ = [('isa', Pointer), ('flags', ct.c_int), ('reserved', ct.c_int),
                ('invoke', _PermissionCompletion), ('descriptor', ct.POINTER(_BlockDescriptor))]


_permission_callback = None


@lru_cache(maxsize=1)
def _permission_block():
    # A process-lifetime global Objective-C block needs no copy/dispose hooks.
    # Keep its Python trampoline alive while AVFoundation calls it on its queue.
    @_PermissionCompletion
    def complete(_block, granted):
        global _permission_callback
        callback, _permission_callback = _permission_callback, None
        if callback is not None:
            callback(granted)

    system = ct.CDLL('/usr/lib/libSystem.B.dylib')
    block_class = ct.addressof(Pointer.in_dll(system, '_NSConcreteGlobalBlock'))
    descriptor = _BlockDescriptor(0, ct.sizeof(_PermissionBlock))
    return _PermissionBlock(block_class, 1 << 28, 0, complete, ct.pointer(descriptor))


def request_microphone_access(completed):
    """Ask macOS for real input access and deliver its decision on the system queue."""
    global _permission_callback
    if _permission_callback is not None:
        raise RuntimeError('A microphone access request is already open.')
    device, media = _capture_api()
    block = _permission_block()
    _permission_callback = completed
    try:
        _call(device, b'requestAccessForMediaType:completionHandler:', media, ct.addressof(block),
              result=None, argtypes=(Pointer, Pointer))
    except (OSError, RuntimeError, ValueError):
        _permission_callback = None
        raise


class Sparkle:
    """Create the standard Cocoa updater on the same thread as the wx event loop."""

    @classmethod
    def for_current_bundle(cls):
        if sys.platform != 'darwin' or not getattr(sys, 'frozen', False):
            return None
        contents = Path(sys.executable).parent.parent
        framework = contents / 'Frameworks' / 'Sparkle.framework' / 'Sparkle'
        if not framework.is_file():
            return None
        with (contents / 'Info.plist').open('rb') as stream:
            configuration = plistlib.load(stream)
        if not (configuration.get('SUPublicEDKey') and configuration.get('SUFeedURL')):
            return None
        return cls(framework)

    def __init__(self, framework):
        self.framework = ct.CDLL(str(framework), mode=ct.RTLD_GLOBAL)
        instance = _call(_class(b'SPUStandardUpdaterController'), b'alloc')
        self.controller = _call(instance, b'initWithStartingUpdater:updaterDelegate:userDriverDelegate:',
                                True, None, None, argtypes=(ct.c_bool, Pointer, Pointer))
        if not self.controller:
            raise RuntimeError('Sparkle could not initialise its update controller.')
        self.updater = _call(self.controller, b'updater')

    def set_automatic(self, enabled):
        current = _call(self.updater, b'automaticallyChecksForUpdates', result=ct.c_bool)
        if current != enabled:
            _call(self.updater, b'setAutomaticallyChecksForUpdates:', enabled, result=None, argtypes=(ct.c_bool,))

    def check(self):
        _call(self.controller, b'checkForUpdates:', None, result=None, argtypes=(Pointer,))
