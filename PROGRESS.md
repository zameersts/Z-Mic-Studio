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

## Done Recently

- Terms and Privacy added to both apps. Same wording on PC and phone.
- First run shows the Terms once. The user must tick a box before Continue works.
- Acceptance is remembered. Change the wording and the user is asked again.
- Stream tab rearranged. Status is now at the top, before the buttons.
- Removed three duplicate status lines that were repeated on other tabs.
- Notification Voice Mode moved from Devices to Stream, where it belongs.
- Help tab rewritten in plain English. Added getting started and troubleshooting.
- Help text no longer uses dash bullets, because screen readers read them out loud.
- Virtual Cable gate added earlier: no cable, no interface.
- Code pushed to a private GitHub repository.

---

## Still To Do

These are decided but not started yet.

1. Build a Windows executable.
   - Use Nuitka, because it gives real protection. PyInstaller can be unpacked in
     minutes with a public script, so it is not good enough.
   - The C compiler is already ready. Nuitka downloaded it by itself.
   - Still needed: a Windows icon file. There is none yet. The Android icon is a
     vector, so a new one has to be made.
   - Next step is a small test app that only loads wx, sounddevice, numpy and
     scipy. If that compiles, the full app will compile. sounddevice uses CFFI,
     which is the most likely thing to fail.
   - Test the result on a computer that has no Python installed.

2. Release signing.
   - Android: create a release keystore soon. If the app is distributed with the
     debug key first, a later release build cannot install over it. The debug key
     is the same on every developer computer in the world.
   - Windows: code signing costs money each year. Not needed while the app is
     free and shared with a few people.

3. Publishing the executable.
   - The code repository stays private.
   - A separate public repository would hold only the executable, the phone app
     and a version file. That way the code is not exposed.
   - The downloaded file should be a link to the official site, not a direct
     download, because the vendor changes the file from time to time.

4. Update checker.
   - Reads a version file from the public downloads location.
   - Only checks once a week, in the background, so startup stays fast.
   - Opens the browser. It never installs anything by itself.
   - The privacy wording in the Terms already describes this.

5. Update the documentation once the above is done.

---

## ⏳ Current Status & Next Steps
- **Status**: Core system working. Four automated test suites pass.
- **Known gap**: There is no Windows executable yet, so the app cannot be
  handed to someone who does not have Python installed.
- **Next Backlog**: Build the executable, starting with the small Nuitka test.

---

## Network setup (do not get this wrong again)

- **The PC has no Wi-Fi adapter in use. It is connected by Ethernet cable.**
  - PC address: `192.168.100.2` (wired)
  - Phone address: `192.168.100.3` (Wi-Fi, Samsung SM-A065F)
- Therefore the audio path is: phone `--Wi-Fi--> router `--Ethernet--> PC`.
- Consequence for diagnosing audio problems:
  - Any remaining packet jitter is on the **phone's Wi-Fi hop or the router**, never on the
    PC's own network. The PC's wired side adds nothing and cannot be blamed.
  - Do not suggest Wi-Fi settings on the PC. There are none.
  - The PC side that *can* still hurt audio is its **receive path**: the wx main thread
    competing with the UDP receiver thread for the GIL, and the jitter buffer depth.
- Measured on 2026-10-07 after the paced sender fix: roughly 2 gaps per second in the
  30-90ms range, all in transit, with zero packet loss.
