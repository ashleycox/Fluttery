# Keyboard shortcuts

Use **Help > Keyboard Commands…** to find the commands and controls in the current window. Some commands only become available when a tool has readings to work with or a control is enabled. To come back here, choose **Help > Keyboard Shortcuts**.

<!-- platform: macos -->
If your keyboard uses the function row for volume or brightness, hold `Fn` to get the function key. On a compact Apple keyboard, press `Fn+Left Arrow` for `Home`.
<!-- endplatform -->

## Open tools and manage windows

Use the **Window** menu to switch tools in the current window or select a tab. **Settings** opens in its own window.

| Command | macOS | Windows and Linux |
| --- | --- | --- |
| Wow & Flutter | `Command+1` | `Control+1` |
| Oscilloscope | `Command+2` | `Control+2` |
| Turntable | `Command+3` | `Control+3` |
| Distortion Analyzer | `Command+4` | `Control+4` |
| Signal Generator | `Command+5` | `Control+5` |
| Session Results | `Command+6` | `Control+6` |
| Settings | `Command+7` | `Control+7` |
| Bring Settings forward | `Command+,` | `Control+,` |
| New Window in the same session | `Command+Shift+N` | `Control+Shift+N` |
| Close the current window | `Command+W` | `Control+W` |
| Quit Fluttery | `Command+Q` | `Control+Q` |
| Larger text | `Command+=` | `Control+=` |
| Smaller text | `Command+-` | `Control+-` |
| Reset text size | `Command+0` | `Control+0` |

| Command | Shortcut |
| --- | --- |
| Toggle full screen | `F11` |
| Open the manual | `F1` |

## Measure and read current values

| Command | macOS | Windows and Linux |
| --- | --- | --- |
| Start or stop a wow and flutter measurement | `Command+Shift+Space` | `Control+Space` |
| Read current values in the active tool | `Command+Shift+R` | `Control+Shift+R` |
| Clear wow and flutter history | `Command+R` | `Control+R` |
| Reset Counter in Wow & Flutter | `Command+Shift+T` | `Control+Shift+T` |
| Find the current tool’s actions and controls | `Command+Shift+K` | `Control+Shift+K` |

You can start or stop a measurement or clear the history while Wow & Flutter is in the background. To start live input, freeze the scope or use another tool command, look it up in **Keyboard Commands**. Select it and press `Return`, or type its two-letter code while the list has focus. The codes depend on what’s available in that window.

For [frequency sweeps](06-signals.md#create-a-frequency-sweep), open **Window > Signal Generator** and search **Frequency Sweep** in **Keyboard Commands**. In the sweep window, search **Play Sweep**, **Stop Sweep**, **Advanced Rules**, **Save WAV** or **Manage Presets**. In the rules table, use `Up Arrow` or `Down Arrow` to select a rule.

Search for the instrument by name. Once it’s open, use **Keyboard Commands** again to find its buttons and settings.

| Work area | Search terms |
| --- | --- |
| Wow and flutter | **Measure Settings**, **Show Detector Statistics**, **Show Live Flutter Spectrum** |
| File analysis and speed capture | **Analyse Recorded Tone WAV**, **Save Unweighted Speed WAV** |
| Response and calibration | **Frequency Response**, **Interface Response Calibration**, **Voltage Calibration** |
| Compare response correction | **Interface correction profile**, **Bypass Correction**, **Compare with Raw**, **Manage Profiles** |
| Electrical bench instruments | **AC Voltmeter**, **A-weighted Signal-to-Noise** |
| Scope | **Show Trigger Settings**, **Select Trigger Mode**, **Arm Single Capture**, **Edit Scope Vertical Scale** |
| Spectrum and distortion | **Spectrum and Distortion Tests**, **Peak Hold**, **Measure Input**, **Generate and Measure** |
| Turntable separation | **Crosstalk**, **Capture Left**, **Capture Right**, **Save Readings** |
| Cartridge setup | **Anti-skate Assistant**, **Tracking Force Assistant**, **Vertical Tracking Angle**, **Phono Loading**, **Phono Gain** |
| Tape deck | **Tape Playback Response**, **Tape Recording Response**, **Tape Head Azimuth**, **Tape Bias and Record Level** |

## Sessions, printing and text editing

| Command | macOS | Windows and Linux |
| --- | --- | --- |
| New Session in its own window | `Command+N` | `Control+N` |
| Open session | `Command+O` | `Control+O` |
| Save session | `Command+S` | `Control+S` |
| Save session as | `Command+Shift+S` | `Control+Shift+S` |
| Clear Session… | `Command+Shift+E` | `Control+Shift+E` |
| Compare with Raw in Recorded measurements | `Command+Shift+B` | `Control+Shift+B` |
| Print report | `Command+P` | `Control+P` |
| Undo the last edit in the focused text field | `Command+Z` | `Control+Z` |
| Redo the last undo in the focused text field | `Command+Shift+Z` | `Control+Shift+Z` |
| Cut selection | `Command+X` | `Control+X` |
| Copy selection | `Command+C` | `Control+C` |
| Paste in the focused text field | `Command+V` | `Control+V` |
| Select all in the focused text field | `Command+A` | `Control+A` |

To inspect a saved run, look for **Focus Saved run**, **Review Saved Run Settings** or **Read Signal Losses**. If the run includes input audio, you can also find **Export Original Input Audio** and **Remove Original Input Audio**. For a response saved with correction, look for **Compare with Raw** or **Hide Raw Comparison**. In **Review Session Settings**, use the arrow keys to choose a difference, `Space` to tick it and `Tab` to reach the apply buttons.

## Plots and readouts

Focus the plot with `Tab`. To move to its text summary and selected-point reading, search **Read Plot Data** in **Keyboard Commands**.

| Command | Shortcut |
| --- | --- |
| Select the previous or next point | `Left Arrow` or `Right Arrow` |
| Move by a larger step | `Page Up` or `Page Down` |
| Select the first or last point | `Shift+Home` or `Shift+End` |
| Read the selected point and any marker differences | `Return` |
| Set comparison markers or clear both | `A`, `B`, or `Escape` to clear |
| Type exact axis limits | `V` |
| Restore the previous zoom or pan | `Backspace` |
| Change trace or spectrogram frequency | `Up Arrow` or `Down Arrow` |
| Pan horizontally or vertically | `Shift+Arrow` |
| Zoom in | `+` or `=` |
| Zoom out | `-` |
| Fit the entire plot | `Home` |
| Open the Plot Menu | `Shift+F10` |
| Find visible traces, image copies or image export | Search by name in Keyboard Commands |

<!-- platform: windows -->
On keyboards with a Menu key, it also opens the plot context menu.
<!-- endplatform -->

## Find commands and navigate dialogs

| Command | macOS | Windows and Linux |
| --- | --- | --- |
| Open Keyboard Commands | `Command+Shift+K` | `Control+Shift+K` |
| Describe the focused control | `Command+Shift+H` | `Control+Shift+H` |
| Focus Search help in the manual | `Command+F` | `Control+F` |

| Command | Shortcut |
| --- | --- |
| Move forward or backward through controls | `Tab` or `Shift+Tab` |
| Change Settings category while the category table has focus | `Up Arrow` or `Down Arrow` |
| Toggle the focused checkbox or activate the focused button | `Space` |
| Move between search and the commands list in Keyboard Commands | `F6` |
| Read the description of the selected command | `F7` |
| Use the highlighted Keyboard Commands result | `Return` |
| Use a command by its displayed code when the list has focus | The two displayed letters |
| Move through Search, Topics and the active pane in the manual | `F6` or `Shift+F6` |
| From the manual’s Search, move to the topic results | `Return` |
| Close the manual, Keyboard Commands, Setup Wizard or another cancellable dialog | `Escape` |

Search **Donate** in **Keyboard Commands** to open the donation page from an instrument window.

## Support Fluttery

If you like Fluttery and you’d like to support my work, you can [make a donation](https://www.paypal.com/donate/?hosted_button_id=WMLX4PLED9PRE). There’s no pressure or hard sell. Fluttery is and always will be free.
