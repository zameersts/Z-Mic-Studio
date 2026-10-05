# ZMic Studio - Project Progress & Memory Tracker

## 📌 Project Overview
- **System**: Real-time microphone streaming from Android phone to Windows PC.
- **Audio Spec**: 48,000 Hz, 16-bit PCM, Dual-mic Stereo (5ms / 10ms frame sizes).
- **Network**: UDP Socket transport over Local Wi-Fi.
  - *Note*: a USB `adb reverse` tunnel is not implemented or documented. The PC binds
    `0.0.0.0:8088`, so it would technically work, but there is no script or guide for it.
- **Ports**: 
  - Audio Stream: `UDP 8088`
  - Heartbeat & Control: `UDP 8089`
- **Primary Accessibility**: Google TalkBack (Android) & NVDA/JAWS (Windows) support, with a
  selectable notification voice mode (Natural Voice clips / Screen Reader / Beeps only).

---

## 🛠️ Environment & Tool Paths
- **ADB Executable**: `C:\Users\Zameer\Downloads\platform-tools-latest-windows\platform-tools\adb.exe`
- **Virtual Audio Device**: `CABLE Input (VB-Audio Virtual Cable)`
  - Not bundled. If absent at startup the main interface does not open; the app opens the
    official VB-Audio page in the default browser and then closes (a reboot is required).
- **Windows Companion**: `windows/zmic_studio_gui.py`
- **Android App Module**: `android/app`
- **Python Dependencies**: `windows/requirements.txt`

---

## ✅ Completed Milestones

### Phase 1: Core Audio Engine & Streaming
- [x] Android hardware capture using `AudioRecorder` + `MediaRecorder.AudioSource.CAMCORDER` at 48kHz, with MIC stereo / MIC mono fallback.
- [x] Zero-allocation binary UDP packet sender with 9-byte header protocol (v2) and an explicit version gate on the PC.
- [x] Windows UDP socket receiver with 1MB OS socket buffer.
- [x] WASAPI low-latency output feeding VB-Audio Cable.

### Phase 2: Jitter Buffer & Realtime DSP
- [x] Adaptive jitter buffer (60ms target, 100ms ceiling, incremental trimming, packet-loss concealment).
- [x] NumPy vectorized audio DSP (stereo expansion, center mono, software gain, echo prevention).
- [x] Phase 1 & 2 integration and stress tests passing.

### Phase 3: Control Protocol & System Integration
- [x] Bi-directional UDP heartbeat (1.0s interval) for live connection detection.
- [x] Global system hotkeys on Windows (remote mute/unmute).
- [x] Foreground notification service on Android with auto-recovery and cleanup.
- [x] Thread-serialised streaming state (`streamLock`) so AudioRecord / socket / wakelock cannot be leaked.
- [x] Phase 3 validation test suite passing.

### Phase 4: Notification & Audio Cue System
- [x] Pre-recorded natural voice prompts (WAV, 44.1 kHz mono) with UI mode selector
      (`Natural Voice`, `NVDA Screen Reader Only`, `Beeps Only`).
- [x] Natural Voice mode intentionally emits **no** duplicate screen-reader speech, because the
      recorded clip already conveys the message.
- [x] Serialised cue queue so clips are never truncated by an incoming cue.
- [x] Lock-guarded per-event debouncing, verified under concurrent load.
- [x] Screen-reader fallback when an event has no bundled clip, so no event is ever silent.
- [x] Startup audibility check that warns when the Windows default output is a virtual sink.
- [x] Dedicated `test_phase4.py` suite covering all of the above.

### Accessibility & UX
- [x] Full TalkBack support on Android (labeled buttons, live regions, WCAG AA contrast, dark theme).
- [x] Full NVDA / JAWS screen reader speech integration on the Windows companion.
- [x] Physical-speaker output blocked by default with an explicit, spoken opt-in warning.
- [x] Firewall helper scripts (`Reset_Firewall_Rules.bat`, `Start_ZMic_Studio.bat`).
- [x] Gradle wrapper committed for reproducible Android builds.

---

## ⏳ Current Status & Next Steps
- **Status**: Core system functional and covered by 4 automated test suites (all passing).
- **Known Gap**: Release signing is not configured yet; the shipped APK is a debug build.
- **Next Backlog**: Awaiting next user feature or enhancement request.
