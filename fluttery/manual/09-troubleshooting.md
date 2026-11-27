# Troubleshooting

## No audio reaches Fluttery

1. Open **Tools → Audio → Audio Settings…**. Check **Audio input**, **Input channels** and **Sample rate**, then choose **Apply Settings**. If your interface refuses the sample rate you chose, try **Device default**.
2. If your interface is missing from the list, connect it and choose **Refresh device list** in Audio I/O. On Linux, check which input is selected and how its level is set in the desktop sound settings.
3. Check that Fluttery has permission to use the audio input. On current macOS versions, open **System Settings → Privacy & Security → Microphone**. On earlier versions, open **System Preferences → Security & Privacy → Privacy → Microphone**. On Windows, look for **Microphone** under **Settings → Privacy & security** or **Settings → Privacy** and allow microphone access for desktop applications.
4. Choose **Tools → Audio → Start Live Input**. Open the Oscilloscope to check for a signal, or read **Settings → Diagnostics → Audio device and stream status**.
5. Check the input and gain on the interface itself. If your turntable has a phono-level output, connect it through a suitable phono preamp before feeding it into a line-level input. Choose **Stereo** when you need to compare the two channels.

Once you see a signal, start a run in the tool you want to use. Live input can stay on between runs. If you stopped it manually, choose **Start Live Input** to turn it back on.

See the operating system instructions for [macOS microphone access](https://support.apple.com/en-gb/guide/mac-help/mchla1b1e1fe/mac) or [Windows microphone access](https://support.microsoft.com/en-us/windows/privacy/turn-on-app-permissions-for-your-microphone-in-windows).

## The signal is present but the measurement is wrong

- **The meter is unlocked or drops out.** Check that you're feeding it a steady test tone at a level high enough to detect without clipping. Match **Settings → Measurement → Reference frequency (Hz)** to the recorded tone. For either of the two standard tones, you can use **Automatically recognise 3,000 and 3,150 Hz references**. If the level varies a lot, check **Adapt signal lock to the input level**.
- **The input clips.** Turn down the level going into the interface or lower its input gain. Leave some headroom below 0 dBFS, which is digital full scale.
- **Speed is consistently high or low.** Check the disc's test frequency and nominal speed in **Settings → Measurement**. Before adjusting the turntable, check the interface's clock correction at the sample rate you're using.
- **Measurements made on different days disagree.** Check that you used the same reference frequency, measurement standard, weighting profile, detector timing and **History graph source**. Use the same signal path and calibration for each reading you compare.
- **The pass/fail result is unavailable.** Turn on **Enable pass/fail quality control**, set **Quality control limit (%)** and record a run with a locked reference. Fluttery calculates the result from locked readings and displays it at the end of the run.
- **Clock or channel calibration fails.** Read **Calibration status**. Use a steady source and keep the input running throughout the capture. Leave the device and sample rate as they are until the capture finishes. For auto-balance, set the input to stereo and feed the same tone to both channels.
- **A response plot is still showing measured gain.** In **Frequency Response** or **Tape Recording Response**, check **Interface correction profile** and turn off **Bypass Correction**. **Response activity** tells you if the audio route differs or if more than one profile matches. Select the profile you made with your current settings, or [measure a new loopback](10-frequency-response.md#calibrate-an-interface) if the route has changed. Tape playback and imported returns with no known output route show the measured gain.

## Generator or monitoring audio is missing

In **Settings → Audio I/O**, check that **Audio output** is set to the device you want to hear. In the Signal Generator, choose **Start Generator** and check **Output volume (%)**. **Options → Enable Internal Loopback** sends the tone straight to Fluttery's instruments. If Fluttery can't open the selected output, its instruments may still receive the tone through loopback while the physical output is silent. Choose **Read Current Values…** in the generator to check where the tone is going.

To hear the hardware input, turn on **Play the input through the selected output** in **Audio I/O**. Check **Monitor channels** and **Monitor volume (0 to 1)**. Use headphones or a separate output to avoid microphone feedback. If you can't hear the sounds Fluttery plays for its controls, check **Settings → General → Play interface sounds** and the operating system's default output.

## A window, graph or report is hard to find

- **A tool is missing from the current window.** Choose the instrument from **Window**, or look under **Tools** for a specialist test or assistant. Use **File → New Window** to see more than one tool for the same session. You can switch between **Single Tool per Window** and **Tabbed Workspace** from **View → Workspace Layout**. Settings opens in its own window.
- **The graph is zoomed or the readings are off screen.** Focus the graph and choose **Fit Plot** or press Home. Use **Show Plot Controls** if your tool offers it. Where available, **Read Plot Data** takes you to the plot's text and data controls.
- **The graph or sample table is empty.** Make sure you recorded or captured the measurement and have the right session open. In **Recorded measurements**, set **Type** to **All measurement types** and select an entry. If it has a summary, you'll find it under **Details**.
- **The session will not open.** Stop any active wow and flutter run, then try **File → Open Session…** with a Fluttery session or a JSON file exported by Fluttery. To compare two sessions, the second file must contain at least one continuous wow and flutter sample.
- **A report omits a measurement.** Look for the entry in **Recorded measurements** and check that its section is on under **Settings → Reports**. Turn on **Include data tables** and **Include graph text descriptions** if you want those in the report too.
- **A control needs an explanation.** Focus it and select **Help → Describe Focused Control…**. Turn on **Show tooltips and help tags** in **Settings → General** to see help tags when pointing at controls.

## An update cannot be downloaded or installed

Choose **Help → Check for Updates…**. If Fluttery can't check for an update, open the releases page in your browser before you try again. [Fluttery release page](https://github.com/ashleycox/Fluttery/releases/latest)

<!-- platform: macos -->
If Fluttery offers to open the release page, follow [Getting started](01-getting-started.md#install-or-update-fluttery) to find and install the Mac download for your computer. If Fluttery offers the update directly, follow the prompts in its updater.
<!-- endplatform -->

<!-- platform: windows -->
Fluttery opens the release page for Windows updates. Download the installer for your processor, close Fluttery and follow [Getting started](01-getting-started.md#install-or-update-fluttery).
<!-- endplatform -->

<!-- platform: linux -->
To update inside Fluttery, run the AppImage from a folder where your account can replace the file. If Fluttery opens the release page or says it couldn't install the update, download the AppImage or Debian package for your computer and follow [Getting started](01-getting-started.md#install-or-update-fluttery). Reopen Fluttery after installation to use the new version.
<!-- endplatform -->

## Collect audio diagnostics

1. Open **Settings → Diagnostics**. Read **Audio device and stream status** while you repeat the steps that cause the problem. Check signal lock, input level, detected frequency, analysis timing, dropouts and any stream counters that are available.
2. Select **Capture audio and application diagnostics**, then **Apply Settings**.
3. Reproduce the problem with logging on. Use **Refresh audio status** or **Refresh log** as needed. Fluttery pauses updates to a readout while it has keyboard focus so you can read it at your own pace. Move focus away to let it update again.
4. Use **Copy log** or **Save log…** to keep the diagnostic messages. **Clear log** empties the current log.
5. To report the issue, choose **Help → Send Feedback…**. Include your Fluttery version from **About Fluttery**, operating system, audio interface, sample rate, channel setting, the steps that trigger the problem and the relevant part of the log.

## Keyboard shortcuts

| Command | macOS | Windows and Linux |
| --- | --- | --- |
| Open Settings from Window | `Command+7` | `Control+7` |
| Open Session Results | `Command+6` | `Control+6` |
| Open Keyboard Commands | `Command+Shift+K` | `Control+Shift+K` |
| Read Current Values | `Command+Shift+R` | `Control+Shift+R` |
| Describe the focused control | `Command+Shift+H` | `Control+Shift+H` |
| Start or stop a wow and flutter measurement | `Command+Shift+Space` | `Control+Space` |
| Fit Plot | `Home` with the plot focused | `Home` with the plot focused |

Search in **Keyboard Commands** for **Audio Settings**, **Start Live Input**, **Read Calibration progress**, **Read Response Activity** or **Read Plot Data**. In the generator, search for **Focus Internal Loopback**. In Audio I/O, look for **Focus Play the input through the selected output** or **Monitor volume**. Press `Space` to change a focused checkbox.

For updates, search for **Check for Updates** or **Check for updates now**. In Diagnostics, search for **Focus Capture audio and application diagnostics**, **Refresh audio status**, **Refresh log**, **Copy log**, **Save log** or **Clear log**. Use **Send Feedback** to start an email.

## Support Fluttery

If you like Fluttery and you’d like to support my work, you can [make a donation](https://www.paypal.com/donate/?hosted_button_id=WMLX4PLED9PRE). There’s no pressure or hard sell. Fluttery is and always will be free.
