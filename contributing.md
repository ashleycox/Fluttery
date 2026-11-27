# Contributing to Fluttery

I welcome contributions to Fluttery. See below for instructions on building from source.

## Platform support

These minimums are based on the runtimes, native libraries and operating-system APIs required by the packaged app. Fluttery may run on older platforms, but this has not been verified.

| System | Minimum requirement |
| --- | --- |
| macOS, Intel | macOS 10.15 Catalina |
| macOS, Apple silicon | macOS 12.3 Monterey |
| Windows, x64 | Windows 8.1 with the Universal CRT update and a compatible x64 Visual C++ v14 runtime |
| Windows, ARM64 | Windows 10 on Arm with the ARM64 Visual C++ v14 runtime |
| Linux, x64 and ARM64 | Ubuntu 24.04 LTS or a compatible GTK 3 desktop with glibc 2.39 or newer and PortAudio |

- **macOS.** The ARM64 SciPy 1.18.1 wheel has a `macosx_12_0` filename, but its binaries require macOS 12.3. On Intel, Mojave 10.14 is possible in theory when running from source with Python 3.12, SciPy 1.17.1 and cffi 2.0.0. [SciPy 1.18.1 wheels](https://pypi.org/project/scipy/1.18.1/#files), [SciPy 1.17.1 wheels](https://pypi.org/project/scipy/1.17.1/#files).
- **Windows.** The native wheels need a compatible Visual C++ v14 runtime that supplies `msvcp140.dll`. Windows 8.1 also needs the Universal C Runtime update. On Windows 10 on Arm, use ARM64 Python. X64 emulation on Arm requires Windows 11. Native dark appearance requires Windows 10 version 1903 or later. [Universal CRT update](https://support.microsoft.com/en-us/servicing/os/windows/2020/04/update-for-universal-c-runtime-in-windows), [Visual C++ runtime requirements](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist), [Windows on Arm emulation](https://learn.microsoft.com/en-us/windows/arm/apps-on-arm-x86-emulation), [wxWidgets dark-mode requirement](https://github.com/wxWidgets/wxWidgets/blob/v3.3.4/src/msw/darkmode.cpp#L215-L225).
- **Linux.** Ubuntu 24.04 is the baseline. On another distribution, install its equivalents for the GTK and PortAudio dependencies listed below.

## Run from source

Fluttery uses CPython, wxPython, NumPy, SciPy and the PortAudio library provided through `sounddevice`. Report generation uses Matplotlib and ReportLab. The steps below use CPython 3.12.

On macOS and Linux, use a terminal at the repository root.

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
python -m fluttery
```

On Windows, run PowerShell at the repository root.

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -m fluttery
```

Use a Python interpreter that matches your system architecture. macOS asks for microphone access when starting live input. Grant access to the launching application or terminal when running from source.

On Ubuntu 24.04, install GTK and the shared audio libraries first.

```sh
sudo apt install python3.12 python3.12-venv python3.12-dev build-essential pkg-config libgtk-3-dev libgl1-mesa-dev libglu1-mesa-dev libportaudio2 libnotify4 libsdl2-2.0-0 libxtst6 libwebpdemux2 libcurl4t64 libpcre2-32-0
```

Install the published [wxPython wheel](https://wxpython.org/pages/downloads/) matching the Linux distribution, architecture and interpreter when available. Otherwise use `python -m pip install "wxPython>=4.3.2,<4.4"` to compile it against system libraries. Use the distro's PortAudio where the Python wheel leaves it to the host. GTK and Orca testing should use an active desktop session.

## Checks

```sh
python -m ruff check .
python -m pytest -q
python -m compileall -q fluttery packaging
FLUTTERY_GUI_TESTS=1 python -m pytest -q tests/test_desktop.py
```

Set `FLUTTERY_GUI_TESTS=1` with `$env:FLUTTERY_GUI_TESTS = "1"` in PowerShell. The GUI test runs inside a desktop session and uses software loopback.

Use the native app for keyboard, visibility, high-DPI and light/dark verification in both layouts. Switch layout during a measurement and with the generator active.

Open Settings beside each workspace, check that it has focus, and verify that closing it returns to the current tool. Check device selection, stereo compatibility, generator and monitoring routes, start/stop and timed measurement, calibration, old session imports, reporting and error recovery.

Exercise keyboard-only operation and inspect accessible names, focus, stability of changing readouts and graph navigation with a screen reader. Use VoiceOver on macOS, NVDA, Narrator or JAWS on Windows, or Orca on Linux.

Physically connect a stereo USB device and measure a known input at the supported rates. Check levels with a hardware loopback, and use an independently clocked calibrated source for clock accuracy.

## Support Fluttery

If you like Fluttery and you’d like to support my work, you can [make a donation](https://www.paypal.com/donate/?hosted_button_id=WMLX4PLED9PRE). There’s no pressure or hard sell. Fluttery is and always will be free.
