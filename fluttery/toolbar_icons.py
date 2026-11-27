"""Small resolution-aware instrument symbols drawn with wx's native renderer."""

from __future__ import annotations

from math import pi

import wx


def _polyline(context, points, *, closed=False, filled=False):
    path = context.CreatePath()
    path.MoveToPoint(*points[0])
    for point in points[1:]:
        path.AddLineToPoint(*point)
    if closed:
        path.CloseSubpath()
    if filled:
        context.FillPath(path)
    else:
        context.StrokePath(path)


def _draw(context, key):
    match key:
        case 'start':
            _polyline(context, [(7, 4), (18, 11), (7, 18)], closed=True, filled=True)
        case 'stop':
            context.DrawRoundedRectangle(6, 6, 10, 10, 1)
        case 'pause':
            context.DrawRoundedRectangle(6, 5, 3, 12, 0.6)
            context.DrawRoundedRectangle(13, 5, 3, 12, 0.6)
        case 'record':
            context.DrawEllipse(5, 5, 12, 12)
        case 'freeze':
            for start, end in (((11, 3), (11, 19)), ((4, 7), (18, 15)), ((4, 15), (18, 7))):
                _polyline(context, [start, end])
        case 'reset':
            ring = context.CreatePath()
            ring.AddArc(11, 11, 6, -pi * 0.81, pi * 0.78, True)
            context.StrokePath(ring)
            _polyline(context, [(5.2, 4.4), (4.6, 9), (9, 8.5)])
        case 'open':
            _polyline(context, [(3, 17), (3, 6), (9, 6), (11, 8), (19, 8), (19, 17)], closed=True)
        case 'save':
            _polyline(context, [(5, 3), (13, 3), (17, 7), (17, 19), (5, 19)], closed=True)
            _polyline(context, [(9, 11), (11, 13), (15, 9)])
        case 'export':
            _polyline(context, [(4, 10), (4, 18), (18, 18), (18, 10)])
            _polyline(context, [(7, 7), (11, 3), (15, 7)])
            _polyline(context, [(11, 3), (11, 13)])
        case 'options':
            for x in (5, 11, 17):
                context.DrawEllipse(x - 1, 10, 2, 2)
        case 'sweep':
            path = context.CreatePath()
            path.MoveToPoint(3, 17)
            path.AddCurveToPoint(11, 17, 15, 13, 19, 4)
            context.StrokePath(path)
        case _:
            for y in (6, 11, 16):
                _polyline(context, [(5, y), (17, y)])


def _bitmap(key, colour, size):
    bitmap = wx.Bitmap.FromRGBA(size, size, 0, 0, 0, 0)
    dc = wx.MemoryDC(bitmap)
    context = wx.GraphicsContext.Create(dc)
    context.Scale(size / 22, size / 22)
    context.SetPen(wx.Pen(colour, 2))
    context.SetBrush(wx.Brush(colour))
    _draw(context, key)
    del context
    dc.SelectObject(wx.NullBitmap)
    return bitmap


def toolbar_icon(key, label):
    if label.startswith('Stop'):
        symbol = 'stop'
    elif label.startswith('Pause'):
        symbol = 'pause'
    elif key in {'measure', 'analysis', 'generate', 'input'}:
        symbol = 'start'
    elif key == 'reset_counter':
        symbol = 'reset'
    elif 'option' in key:
        symbol = 'options'
    else:
        symbol = key
    colour = wx.SystemSettings.GetColour(wx.SYS_COLOUR_BTNTEXT)
    return wx.BitmapBundle.FromBitmaps([_bitmap(symbol, colour, 22), _bitmap(symbol, colour, 44)])
