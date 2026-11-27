# Fluttery development

Fluttery is a native Python and wxPython desktop app for audio measurement and tape machine and turntable calibration. It supports macOS, Windows and Linux. Keep the application runnable without a browser engine or web view. Standalone HTML reports are export files.

## Requirements and completion

- Before editing, list every agreed requirement, including corrections and constraints from earlier turns that still apply. Keep that list current as the work changes, and pair each item with the expected behavior and the check that will prove it. Every agreed item stays in scope until the maintainer changes it.
- Before calling the task complete, make a separate pass against the agreed requirements, then review the affected workflows and regression results end to end. Fix every gap you find and repeat the relevant checks until a pass finds no remaining issues you can address. Passing tests or a successful build alone does not establish completion.
- In the handoff, state a summary of changes, what was verified, on which platforms, and what remains unverified or incomplete. Identify any blocked requirement as incomplete and explain the blocker.

## Code quality and measurement correctness

- Use the highest practical standard of code, with clear names, small interfaces, explicit units, and simple control flow. Comment non-obvious behavior throughout, particularly DSP assumptions, calibration formulas, stream ownership, failure recovery and accessibility decisions. Keep comments current.
- Keep device I/O, DSP, the wx event loop, persistent settings, session data and reports separated at their boundaries. Keep allocation, blocking calls and GUI work outside time-sensitive PortAudio callbacks. Preserve state between audio blocks and handle arbitrary block sizes, dropped frames, channel count and negotiated sample rate.
- Keep output safe to operate. Start and stop signal generation explicitly. Save settings and sessions atomically where possible. Verify legacy `.fluttery` and JSON data can still be loaded, and verify reports from empty as well as populated sessions.
- Define each measurement’s units, reference, sign convention, frequency band and valid input conditions. Check sample clocks, resampling, windowing, normalization, clipping, noise floors and calibration limits where they affect the result. Use the applicable primary standard or method reference, and document the edition and any limits on what the implementation measures.
- Validate numerical changes with independent known signals and justified error tolerances. Include cases that expose bias or failure, such as off-bin tones, band edges, weak or clipped input and interrupted captures. Verify those cases at representative sample rates and through the output paths that use the result. Record the tested rates and tolerances. Keep missing readings, noise-limited bounds and valid zero readings distinguishable in the UI, saved data and exports.
- Run the standard development checks in [contributing.md](contributing.md).

## Native interface and accessibility

- Accessibility is part of each feature. Keep forms and menus keyboard operable. Prefer native controls, visible labels, meaningful names and predictable focus. Provide a text equivalent and keyboard exploration for plots. Keep rapid updates from interrupting speech or moving the reading caret.
- The maintainer is blind. Take responsibility for all visual aspects of the application interface, including icons, graphs, layouts, legibility, contrast, high DPI and light/dark modes. Inspect rendered windows yourself at useful sizes and with realistic data. Describe visual decisions and issues plainly.
- Target the oldest practical OS versions that preserve the full feature set. Keep the working compatibility matrix and packaging details accurate.

## Documentation and repository work

- Maintain the illustrated [user manual](fluttery/manual/README.md) alongside the feature. Describe the current app from the end user's perspective. Keep change history and development information out of the manual.
- Reconcile topics, workflow steps, command names, platform shortcuts and settings reference tables with the actual application. Verify the online links and the corresponding bundled topic search, references and entry points. Check for missing features after the first review and repeat the check after making corrections.
- Update screenshots when the interface or workflow they show changes. Capture the actual target-platform UI with synthetic equipment names, signals and sessions. Keep meaningful alt text and use highlights only where they help locate a control or reading. Inspect the full-size images and their rendered presentation yourself for contrast, legibility, clipping and accidental inclusion of personal data.
- Use `python -m scripts.capture_manual` in a native desktop session to refresh the synthetic examples. Run it with `--manual-only` after the manual’s figure links are in place to capture the reader itself. Test packaging whenever a new kind of manual resource is added.
- Touch [`README.md`](README.md) only when the maintainer explicitly asks. Ask for permission before editing [`contributing.md`](contributing.md). Keep that guide focused on local development and testing.
- Keep maintainer release procedures in the local, gitignored `.fluttery-release.md` file. Only Ashley Cox publishes Fluttery releases.
- Preserve the contribution author’s credit. Keep private keys, release credentials, local signing secrets and private environment configuration out of the repository and review output.
