# ZMic Studio Technical Audit Report
**Created & Designed by Zameer**

---

## 1. Executive Summary

ZMic Studio is a low-latency virtual microphone system connecting an Android physical device to a Windows workstation over local Wi-Fi.

### Core Active Codebase:
1. **Android Client (`android/app`)**:
   - Kotlin 2.0 implementation targeting Android SDK API 26 (Android 8.0) through API 35 (Android 15), with `minSdk 21`.
   - Zero-allocation audio capture loop (`AudioRecord`) with dual-microphone capture (`MediaRecorder.AudioSource.CAMCORDER`) and transparent mono fallback.
   - Android Foreground Service with partial wake lock and low-latency Wi-Fi lock.
   - Screen-reader accessible interface built with standard native widgets for Google TalkBack, with a full dark theme.
2. **Windows Companion (`windows/zmic_studio_gui.py`)**:
   - Accessible desktop application built with `wxPython` (wxWidgets 4.3.1 Win32 native controls).
   - Real-time low-latency audio rendering via `sounddevice` using the Windows Audio Session API (WASAPI).
   - Vector DSP engine powered by NumPy (Wiener filter spectral denoise, 80 Hz rumble cut, Mid/Side 3D stereo widening, studio echo and ping-pong delay).
   - Integrated screen-reader accessibility for NVDA, JAWS and Windows Narrator with non-blocking speech dispatch and audio cue chimes.

---

## 2. Active Project Structure

```
D:\PhoneMic\
├── .agents/                    # Assistant workspace rules (ADB environment config)
├── .gitignore                  # Git ignore patterns for build outputs and logs
├── AGENTS.md                   # Environment and platform-tools rules
├── AUDIT_REPORT.md             # This document
├── ZMIC_STUDIO_PROJECT_REPORT.md
├── ZMicStudio.apk              # Debug build of the Android client (see section 7)
├── android/                    # Android Studio project root
│   ├── gradlew / gradlew.bat   # Gradle wrapper (Gradle 8.13)
│   ├── build.gradle.kts        # Root Gradle build script
│   ├── settings.gradle.kts     # Repository configuration
│   └── app/                    # Application module
│       ├── build.gradle.kts    # App dependencies and compile SDK configuration
│       └── src/main/           # Android source tree
│           ├── AndroidManifest.xml
│           ├── java/com/phonemic/app/
│           │   ├── WelcomeActivity.kt     # Accessible onboarding activity
│           │   ├── MainActivity.kt        # Primary control panel activity
│           │   ├── AudioStreamService.kt  # Foreground audio streaming service
│           │   ├── audio/
│           │   │   ├── AudioConfig.kt     # Audio + wire-protocol constants
│           │   │   └── AudioRecorder.kt   # Zero-allocation capture loop
│           │   └── network/
│           │       └── UdpAudioSender.kt  # Protocol v2 packet serializer
│           └── res/                       # Accessible layouts, strings, day/night colours, vector icons
├── docs/                       # Architecture and accessibility standards
│   ├── ACCESSIBILITY.md
│   └── ARCHITECTURE.md
└── windows/                    # Windows companion root
    ├── Start_ZMic_Studio.bat  # 1-Click desktop launcher (pythonw background execution)
    ├── Reset_Firewall_Rules.bat
    ├── phone_mic_gui.py       # Backward-compatibility forwarder -> zmic_studio_gui
    ├── zmic_studio_gui.py     # Active Windows companion GUI application
    ├── requirements.txt       # Python dependencies
    ├── test_phase1.py         # Automated test: AudioEngine lifecycle & silence detection
    ├── test_phase2.py         # Automated test: jitter buffer, PLC, protocol, DSP, pairing
    ├── test_phase3.py         # Automated test: NVDA accessibility, hotkeys, mute toggle
    ├── test_phase4.py         # Automated test: notification modes, cue queue, debouncing
    ├── sounds/                # Recorded voice clips for the Natural Voice notification mode
    └── requirements.txt       # Python dependencies
```

> VB-Audio Virtual Cable is **not bundled**. The app detects whether `CABLE Input` is present at
> startup; when it is missing the main interface is never built and the user is directed to the
> vendor's own download page. See section 8.

---

## 3. Audio Pipeline & DSP Engine

### Data Flow Specifications:
- **Sampling Rate:** 48,000 Hz
- **Bit Depth:** 16-bit signed integer PCM
- **Channels:** Dual-channel (Stereo) or Mono, negotiated at capture time
- **Frame Payload:** 960 bytes per packet in both modes
  - Stereo: 240 samples/channel = **5 ms** per packet, 200 packets/s
  - Mono: 480 samples/channel = **10 ms** per packet, 100 packets/s
- **Datagram Size:** 969 bytes (9-byte header + 960-byte payload), inside the 1472-byte Wi-Fi MTU so there is no IP fragmentation
- **Network Protocol:** UDP, **Protocol v2** with a 9-byte binary header
  `[0x50 'P'][0x4D 'M'][version][type][channels][seq uint16 BE][samples uint16 BE]`

> Protocol v1 used offset 2 for both the version and the packet type, so audio was only recognised because `TYPE_AUDIO` happened to equal `1`. Protocol v2 separates the two bytes and rejects mismatches explicitly, which prevents a version change from silently routing audio into a legacy path that played header bytes as full-scale noise.

### Signal Processing Chain (Windows `AudioEngine`):
1. **Security & Version Gate:** drops any datagram without the `PM` magic bytes, then drops any whose version byte is not the supported version. A version-1 phone produces a clear "please update the Android app" log entry instead of silence.
2. **Payload Validation:** the declared sample count is checked against the actual payload length, and any excess is trimmed, so a truncated or corrupt datagram cannot desynchronise the stream.
3. **Wiener Spectral Denoise:** continuous estimation of the stationary acoustic noise floor across dynamic frame blocks using frequency-domain power-spectrum subtraction.
4. **80 Hz Rumble Cut:** second-order high-pass filter removing fan motor hum and wind noise. Requires SciPy; when SciPy is absent the filter is disabled and a prominent error is written to the log.
5. **Mid/Side Spatial Width:** computes Mid `(L+R)/2` and Side `(L-R)/2`, scaling spatial expansion for immersive stereo.
6. **Center Mono Downmixing:** channel averaging `(L+R) * 0.5` applied to both channels when mono operation is selected.
7. **Master Volume Gain:** linear amplitude scaling with clipping prevention.
8. **Circular Delay & Reverb:** lock-free circular audio buffer generating studio echo and ping-pong delay.
9. **Adaptive Jitter Buffer:**
   - Prebuffering target of **60 ms** to cushion against local network latency spikes.
   - Hard latency ceiling of **100 ms**. Overruns are corrected incrementally (20 ms per write) instead of one large cut, because discarding a large block at once is audible as a gap.
   - Dynamic Packet Loss Concealment (PLC) using a linear fade-out to prevent clicks during underruns.
10. **WASAPI Stream Injection:** pushes 48 kHz 16-bit stereo PCM into `CABLE Input (VB-Audio Virtual Cable)` using event-driven callback synchronisation.

---

## 4. Accessibility Compliance

| Requirement | Implementation Details | Status |
|---|---|---|
| **TalkBack (Android)** | Standard native controls only. Live regions on the status line, the paired-device label and the permission status. Accessible headings, pane titles, and `contentDescription` on every non-text control. | Implemented |
| **WCAG AA contrast (Android)** | Every text/background pair meets 4.5:1 or better in both the light and dark palettes. The previous subtitle colour reached only 4.11:1 and was darkened. | Compliant |
| **Dark theme (Android)** | `values-night/` colour and theme resources; the system theme is no longer forced to light. | Implemented |
| **Permission recovery (Android)** | Denied permission never resolves to a misleading "Ready" state. An "Open App Settings" button appears and opens the app's permission page directly. | Implemented |
| **NVDA / JAWS (Windows)** | Wrapped Win32 native controls (`SysTabControl32`, `Button`, `TrackBar`). Non-blocking speech output via `accessible_output2` with SAPI fallback. | Implemented |
| **Speech Deduplication** | Identical announcements within 2.5 seconds are suppressed to eliminate screen-reader chatter. | Implemented |
| **Notification Voice Mode** | Selectable `Natural Voice` / `NVDA Screen Reader Only` / `Beeps Only` on the Devices tab. Natural Voice plays bundled recorded clips and deliberately emits **no** duplicate screen-reader speech, because the clip already conveys the message. Falls back to screen-reader speech when a clip is not bundled, so an event is never silent. | Implemented |
| **Cue Delivery Integrity** | All cues pass through one serialised worker queue, because `winsound.PlaySound`/`Beep` otherwise cut off the currently playing clip. Per-event 2 s debounce is lock-guarded and verified under 320 concurrent calls. Alert/error paths bypass the mode selector so failures are never silent. | Implemented |
| **Notification Audibility** | On startup the app checks whether the Windows default output device is a virtual sink; if it is, notification audio would be routed into the cable and become inaudible, so a clear warning is printed. | Implemented |
| **Global Win32 Hotkey** | `Ctrl+Alt+M` registered via `RegisterHotKey`, works from any application. | Implemented |
| **Keyboard Navigation** | Full tab order, button activation on `Space` and `Enter`, arrow-key radio group navigation, and accelerators `Ctrl+1..4` for tabs plus `Alt+R/M/I/N/E/P/H/A` for actions. | Implemented |

---

## 5. Automated Regression Test Verification

All three automated suites execute with 100% pass rates:

1. **`windows/test_phase1.py`** — log directory resolution, VB-Cable detection, `AudioEngine` lifecycle, and automatic disconnect after 2 s of silence using a Protocol v2 sender.
2. **`windows/test_phase2.py`** — jitter buffer prebuffering, PLC fade-out, incremental overrun trimming (including a 1500 ms burst absorbed without breaching the ceiling), Protocol v2 header encoding, **version-gate rejection** of a v1-style packet, dynamic DSP frame sizing, live UDP streaming with loss detection, and **auto-approval of an already-paired phone with no repeat pairing prompt**.
3. **`windows/test_phase3.py`** — non-blocking screen-reader speech and audio cue dispatch, Win32 global hotkey registration and cleanup, and UI mute-toggle state verification.

4. **`windows/test_phase4.py`** — notification system: mode selector (including rejection of an invalid mode), voice-clip resolution and WAV decodability, path-traversal sanitisation, per-event debouncing, Natural-mode fallback when no clip is bundled, serialised cue queue under a burst, queue saturation under flood, debounce correctness under 320 concurrent calls, and the audibility diagnostic.

Both Python suites and the Android build run clean with `pyflakes` reporting no findings.

### Notification Assets
- `windows/sounds/` ships four recorded voice clips (44.1 kHz, mono, 16-bit): `phone_connected`, `phone_disconnected`, `mic_muted`, `mic_unmuted`.
- Events without a bundled clip (`phone_declined`, `phone_forgotten`, `input_switched`) fall back to screen-reader speech so they are never silent.

---

## 6. Resource Safety & Threading

- **Single-writer streaming state (Android):** every mutating entry point into `AudioStreamService` is serialised on `streamLock`, and the streaming flag is claimed before any hardware is allocated. An `AudioRecord`, `DatagramSocket` or `WakeLock` can therefore never be created twice or orphaned, regardless of whether the call arrives from the main thread, the discovery thread or the UDP control thread.
- **Wake and Wi-Fi lock release** is wrapped so that a double release on a non-reference-counted lock cannot raise an uncaught exception and crash the process.
- **Discovery thread lifecycle:** the discovery loop closes its own socket and only clears shared state if it is still the active server, and the thread is joined on shutdown. A previous race could let a stale thread close a newly started server and permanently disable discovery.
- **Reply executor (Android):** discovery replies use a bounded single-thread executor with a daemon thread, instead of creating an unbounded thread and socket per reply.
- **UDP receiver teardown:** the control receiver thread is closed and joined before its state is published, so a stale thread cannot mutate a new session.
- **Clean shutdown (Windows):** the application returns from `main()` normally so that logging and `atexit` cleanup run, rather than forcing `os._exit()`.

### Known Limitations
- The 8-byte notification small icons are vector silhouettes so the system tint is applied correctly; the previous full-colour framework asset rendered as an unreadable blob.
- SciPy is an optional dependency. Without it the 80 Hz rumble cut is disabled and this is recorded as an error in the log.
- Discovery relies on UDP broadcast. Networks with client isolation or filtered broadcast traffic will prevent automatic discovery, and manual connection is not available in this version.

---

## 7. Virtual Audio Cable Requirement (Startup Gate)

ZMic Studio needs a virtual audio cable so that other Windows applications (Discord, OBS, Zoom)
see the phone as an ordinary microphone. Without one the app has nothing useful to show, so the
main interface is **never built**.

### Startup flow
```
main()
  │
  ├─ is_virtual_cable_present()
  │      looks for an output device whose name contains "cable input"
  │
  ├── PRESENT ──► ZMicStudioFrame() builds normally
  │
  └── ABSENT ──► VirtualCableRequiredGate  (only two buttons)
                   [ Download Virtual Cable ]  →  opens https://vb-audio.com/Cable/
                                                    in the default browser, then closes the app
                   [ Exit ]                     →  closes the app
```

Both actions end the process. This is intentional: installing the driver requires a computer
restart, so the application must be reopened afterwards anyway. The gate reappears on every
launch until the driver is installed.

### Licensing
ZMic Studio **does not bundle and does not launch the VB-Audio installer**. The VB-CABLE licence
states the package may be copied and distributed as-is, but *"it is not allowed to integrate the
VB-CABLE package in another software installation procedure without Author agreement."* Launching
a bundled installer from inside the application was exactly that. Directing the user to the
vendor's own download page avoids the issue entirely, and keeps the distribution smaller.

The link points at the **full vendor page** rather than a direct `.zip` URL, because VB-Audio
repackages the driver periodically and a hard-coded file link would eventually break.

If the browser cannot be opened programmatically, the address is shown in a message box so the
user can copy it manually.

### Removed
- `windows/vbcable/` folder (31 files, 3.31 MB) — deleted
- `VBCableRequiredDialog` — deleted
- All `ShellExecuteW` / `ctypes` / `VBCABLE_Setup` references — deleted
- `!windows/vbcable/*` exceptions in `.gitignore` — removed

### Regression coverage
`test_phase4.py` Test 9 asserts the vendor URL, the absence of the bundled folder and installer,
the removal of the installer-launching dialog, and that no `ShellExecuteW` / `ctypes` /
`VBCABLE_Setup` reference remains anywhere in the application source.

---

## 8. Build & Release Status

- **Gradle wrapper is committed** (`gradlew`, `gradlew.bat`, `gradle/wrapper/`), pinned to Gradle 8.13, so the project builds reproducibly without a preinstalled Gradle.
- **`ZMicStudio.apk` in the project root is a DEBUG build**, not a production build. It is byte-identical to `app/build/outputs/apk/debug/app-debug.apk` and is marked `android:debuggable`, so it is signed with the shared Android debug keystore.
- **No release signing configuration exists yet.** A release build requires a private keystore to be provisioned and a `signingConfigs` block added to `android/app/build.gradle.kts`.
- `versionName` is `0.9.0-beta`, matching `APP_VERSION` in `windows/zmic_studio_gui.py`.
