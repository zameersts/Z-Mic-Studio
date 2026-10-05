# ZMic Studio Accessibility Standards
**Created & Designed by Zameer**

## Overview
ZMic Studio is designed screen-reader-first, providing blind and visually impaired users an intuitive, responsive, and tactile experience on both platforms:
- **Android**: Google TalkBack support.
- **Windows**: NVDA, JAWS, and Windows Narrator support.

---

## Android Accessibility Standards

1. **Semantic Native Views**:
   - Built exclusively with standard Android widgets (`Button`, `TextView`) ensuring native role, state, and name announcements to TalkBack.
   - Zero custom drawn or mouse-only canvas views.

2. **Live Regions**:
   - `tvStatus` uses `android:accessibilityLiveRegion="polite"` so connection, mute and error transitions are spoken automatically.
   - `tvPairedDevice` is also a live region, so pairing and forgetting a PC are announced rather than only displayed.
   - `tvPermissionStatus` on the welcome screen is a live region, so permission grant or denial is spoken as it changes.

3. **Names, Headings and Panes**:
   - Every non-text control carries a `contentDescription` that states its action, not just its label.
   - Screen titles and section titles use `android:accessibilityHeading="true"` so TalkBack heading navigation works.
   - Each screen sets `android:accessibilityPaneTitle` so TalkBack announces the page name on entry.

4. **Permission Recovery**:
   - A denied microphone permission never resolves to a misleading "Ready" state. The status line states plainly that streaming is blocked, and an "Open App Settings" button appears and leads directly to the app's permission page.
   - If permission was denied permanently, a dialog explains that Android will not ask again and offers the same Settings route.

5. **Logical Focus Order**:
   - Layout is organized linearly with touch targets of 48dp or larger.
   - Spacer views are marked `importantForAccessibility="no"` so they are not focus stops.

6. **Colour and Contrast**:
   - All text/background pairs meet WCAG AA (4.5:1 or better) in both the light and dark palettes.
   - A full dark theme is provided via `values-night/`; the system theme is no longer forced to light.

7. **Localisation**:
   - All user-facing strings live in `strings.xml`; nothing is hardcoded in a layout.
   - `supportsRtl` is honoured, so margins use `paddingStart`/`paddingEnd` and translatable strings avoid baked-in left-to-right word order.

---

## Windows Accessibility Standards

1. **Standard Win32 Controls**:
   - Built using standard Win32 controls wrapped via `wxPython` (`SysTabControl32`, `Button`, `TrackBar`, `CheckBox`).
   - Native keyboard focus traversal using `Tab` and `Shift+Tab`.
   - Full keyboard activation via both `Space` and `Enter` keys on all buttons.

2. **Screen Reader Integration & Deduplication**:
   - Non-blocking speech dispatch via `accessible_output2` (NVDA, JAWS, System SAPI).
   - Speech deduplication engine: suppresses identical speech announcements within 2.5 seconds to prevent synthesizer chatter.

3. **Audible Audio Cues**:
   - Connect confirmation, disconnect notification and microphone mute/unmute are announced through the selectable **Notification Voice Mode** documented in the dedicated section at the end of this file.

4. **Keyboard Accelerators & Global Hotkeys**:
   - `Ctrl+Alt+M`: system-wide Win32 global hotkey for instant microphone mute/unmute (works from any application or when minimised).
   - `Ctrl+1` to `Ctrl+4`: instant tab navigation (Stream, Devices, Audio Effects, Help).
   - `Alt+R`: Start/Stop stream toggle.
   - `Alt+M`: Mute toggle.
   - `Alt+I`: jump to the audio input selector.
   - `Alt+H`: headphone monitor toggle.
   - `Alt+N`: noise reduction toggle.
   - `Alt+E`: studio echo toggle.
   - `Alt+P`: ping-pong delay toggle.
   - `Alt+A`: About dialog.

> Note: tab navigation uses **Ctrl**, not Alt. `Alt+1`..`Alt+4` are not bound.

4. **Control Naming**:
   - Every control has an explicit `SetName()` that describes its action and, for sliders, its current value and unit.
   - Labels are updated live as values change, and the accessible name is updated at the same time so a screen-reader user always hears the current value.

5. **Feedback Protection Disclosure**:
   - "Allow Physical Speakers" is off by default. Turning it on produces both a spoken warning about the echoing feedback loop and a status message advising headphones, so the risk is never silent.

---

## Notification Voice Mode (Windows)

A selectable **Notification Voice Mode** on the Devices tab offers three options:

| Mode | Behaviour |
|---|---|
| **Natural Voice** (default) | Plays the bundled recorded voice clips from `windows/sounds/`. The clip itself conveys the message, so no duplicate screen-reader speech is emitted. If no clip is bundled for an event it falls back to screen-reader speech, so an event is never silent. |
| **NVDA Screen Reader Only** | Announces through `accessible_output2` / SAPI only. |
| **Beeps / Tone Cues Only** | Tone cues only, for users who do not want speech at all. |

Bundled clips: `phone_connected`, `phone_disconnected`, `mic_muted`, `mic_unmuted`.

### Cue delivery integrity
- All cues pass through a **single serialised worker queue**. `winsound.PlaySound` and `winsound.Beep` share one system sound facility, and starting a new sound normally **cuts off** whatever is playing; serialisation guarantees every 1.2–1.3 second clip is heard in full.
- Each event is debounced for 2 seconds **under a lock**, so a burst of events cannot double-play. Verified by test under 320 concurrent calls.
- Notification audio plays on the **Windows default output device**. On startup the app checks whether that device is a virtual sink (for example if the user routed all system audio into `CABLE Input`) and prints a clear warning, because cues routed into a cable are inaudible.
- **Alert and error paths deliberately bypass the mode selector**, so a failure is never silent regardless of the selected mode.
