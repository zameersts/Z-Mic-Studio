# ZMic Studio System Architecture
**Created & Designed by Zameer**

## Overview
ZMic Studio transforms an Android smartphone into a low-latency, studio-fidelity virtual microphone for a Windows PC over local Wi-Fi. It features zero-allocation audio streaming, continuous spectral noise reduction, and direct WASAPI virtual cable routing.

---

## Data Flow Pipeline

```
[ Android Microphone Hardware ]
  (Bottom Mic: Left | Top Mic: Right)
               │
               ▼
     [ AudioRecorder Engine ]
   (48 kHz, 16-bit PCM, 960-byte frames)
               │
               ▼
     [ AudioStreamService ]
 (Foreground Service + Low Latency Wi-Fi Lock)
               │
               ▼
      [ UdpAudioSender ]
 (Protocol v2: 9-byte 'PM' header + 960-byte PCM)
               │
═══════════════╡ UDP Local Wi-Fi (Port 8088)
               ▼
     [ Windows AudioEngine ]
    (Socket thread, 0.5s recv timeout)
               │
               ▼
    [ Real-Time DSP Engine ]
 ├─ Wiener Spectral Denoise (Continuous NR)
 ├─ 80Hz Rumble Cut (scipy, optional)
 ├─ Mid/Side Stereo 3D Spatial Width
 ├─ Center Mono (L+R)/2 Downmixing
 ├─ Master Volume Gain Multiplier
 └─ Circular Buffer Studio Echo & Delay
               │
               ▼
   [ Adaptive Jitter Buffer ]
 (Target: 60ms | Ceiling: 100ms | Linear PLC Fade-Out | Incremental Overrun Trim)
               │
               ▼
     [ WASAPI Audio Stream ]
 (Low-Latency Output to "CABLE Input (VB-Audio)")
               │
               ▼
   [ Virtual Microphone Output ]
 ("CABLE Output" in Voice Apps & Production Suites)
```

---

## Notification Cue Pipeline

```
       [ UI action | UDP event | discovery response ]
                          │
                          ▼
                 notify_event(event, speech, freq, ms)
                          │
              ┌───────────┴───────────┐
              │  per-event 2s debounce │  (lock-guarded dict)
              │  _last_notify_times    │
              └───────────┬───────────┘
                          ▼
                 mode = _notification_mode
                          │
      ┌───────────────────┼───────────────────┐
      ▼                   ▼                   ▼
  NATURAL               NVDA                BEEPS
      │                   │                   │
 _resolve_sound_path()    │                   │
      │                   │                   │
 ┌────┴─────┐        speak_accessibility()   │
 │ WAV hit? │              │            _enqueue_cue()
 └──┬────┬──┘              │            ('tone',f,ms)
    │ no │                 │                   │
    │    ▼                 │                   │
    │  speak_             │                   │
    │  accessibility()     │                   │
    │  (never silent)      │                   │
    │                      │                   │
    └──────────┬───────────┴───────────────────┘
               ▼
      ┌─────────────────────────────┐
      │  _cue_queue (bounded, 16)   │  _enqueue_cue(('file', path))
      │  drop-oldest on saturation  │
      └──────────────┬──────────────┘
                     ▼
        [ ZMicCueWorker: single thread ]
          winsound.PlaySound(SND_SYNC)   -> sequential, no truncation
          winsound.Beep                  -> sequential, no truncation
                     ▼
        Windows DEFAULT output device
```

Key properties:
- **Serialised.** `winsound.PlaySound` and `winsound.Beep` share one system sound facility; a new sound normally cuts off the current one. A single worker with the *synchronous* flag guarantees every clip is heard in full.
- **Natural Voice emits no duplicate speech.** The recorded clip already states the message, so `speak_accessibility` is not called when a clip plays.
- **Never silent.** If no clip is bundled for an event, Natural mode falls back to screen-reader speech.
- **Alerts bypass the mode.** Stream errors, hot-swap failures and the physical-speaker warning call `play_audio_cue` and `speak_accessibility` directly so a failure is reported regardless of the selected mode.
- **Audibility is checked at startup.** Cues play on the Windows default output device; if that device is a virtual sink the app prints a warning, because audio routed into a cable cannot be heard.

---

## Component Architecture

### 1. Android Client (`android/app`)
- **AudioRecorder (`audio/AudioRecorder.kt`)**:
  - Captures dual-mic stereo PCM using `AudioRecord` with `MediaRecorder.AudioSource.CAMCORDER`, falling back to `MIC` stereo then `MIC` mono.
  - Format: 48,000 Hz, 16-bit PCM. Frame payload is always **960 bytes**:
    - Stereo: 240 samples/channel = **5 ms**
    - Mono: 480 samples/channel = **10 ms**
  - 960 bytes keeps every datagram at 969 bytes, inside the 1472-byte Wi-Fi MTU, so there is no IP fragmentation.
  - Zero heap allocation in the capture loop: frame buffer, silence buffer and (on API 21/22) a direct `ByteBuffer` are all pre-allocated before the loop.
  - On API 21/22 it uses `read(ByteBuffer)` because `read(byte[],int,int)` is non-blocking there and would spin the CPU.
- **UdpAudioSender (`network/UdpAudioSender.kt`)**:
  - Encapsulates PCM frames with the Protocol v2 binary header.
  - Transmit buffer, `DatagramPacket` wrapper and the control buffer are pre-allocated, so `send()` performs zero allocations.
  - Rejects any datagram whose version byte does not match, and ignores packets from any host other than the streaming target.
- **AudioStreamService (`AudioStreamService.kt`)**:
  - Foreground Service with `foregroundServiceType="microphone"`.
  - Manages `PARTIAL_WAKE_LOCK` and `WIFI_MODE_FULL_LOW_LATENCY`.
  - Broadcasts a discovery beacon on UDP port 8089 to both the limited broadcast address and the subnet-directed address derived from the DHCP lease.
  - All state mutation is serialised on a single `streamLock`, so an `AudioRecord`, `DatagramSocket` or `WakeLock` can never be created twice or orphaned, no matter which thread calls in.
  - Authorization requires a `TYPE_CONNECTION_REQUEST` whose claimed hostname matches the stored trusted name. A heartbeat ACK proves liveness, never identity, so it never grants authorization.
- **UI Activities**:
  - `WelcomeActivity`: Accessible onboarding. A denied microphone permission does **not** silently complete onboarding; the user is told what is blocked and given a direct route to system Settings.
  - `MainActivity`: Primary TalkBack-accessible control panel with live accessibility regions, status indicator, paired-PC management, and an "Open App Settings" button that appears when permission is missing.

### 2. Windows Companion (`windows/`)
- **Main GUI (`zmic_studio_gui.py`)**:
  - `phone_mic_gui.py` is a thin backward-compatibility forwarder that re-exports `zmic_studio_gui`; the real application is `zmic_studio_gui.py`.
  - Accessible wxPython application with standard Win32 controls (`SysTabControl32`, `Button`, `TrackBar`).
  - Screen reader integration with non-blocking NVDA and SAPI speech dispatch and 2.5-second deduplication.
  - Background audio cue chimes for connection state transitions.
  - System-wide global hotkey `Ctrl+Alt+M` for instant microphone mute/unmute.
  - Acoustic feedback guard: output is restricted to virtual sinks (VB-Cable, VoiceMeeter, VAC...). Physical speakers are blocked unless the user explicitly opts in via the "Allow Physical Speakers" checkbox, which states the feedback-loop consequence.
- **AudioEngine**:
  - Background receiver thread bound to UDP port 8088 using a blocking `recvfrom` with a 0.5 s timeout.
  - Real-time DSP chain operating directly on NumPy arrays.
  - Auto-approves a phone that is already recorded in `paired_phone.json`, so a trusted phone is never asked to pair again on a later session.
- **AdaptiveJitterBuffer**:
  - Prebuffering target 60 ms with dynamic underrun Packet Loss Concealment (PLC).
  - Hard latency ceiling of 100 ms. Overruns are corrected incrementally (20 ms per write) rather than in one large cut, because dropping a large block of audio at once is audible as a gap.
- **WASAPI Output**:
  - Uses `sounddevice` / PortAudio with shared, event-driven WASAPI (`WasapiSettings(auto_convert=True)`).
  - Auto-binds to `CABLE Input (VB-Audio Virtual Cable)` in the background without user configuration.

---

## Virtual Audio Cable Requirement (Startup Gate)

Other Windows applications can only see the phone as a microphone if audio is routed through a
virtual cable, so the app refuses to present its interface without one.

```
        main()
           │
           ▼
  is_virtual_cable_present()
     output device whose name contains "cable input"
           │
     ┌─────┴─────┐
     │           │
  PRESENT     ABSENT
     │           │
     ▼           ▼
ZMicStudioFrame   VirtualCableRequiredGate   ← ONLY the gate, no main frame
 builds normally       │
                       ├── [ Download Virtual Cable ]
                       │      webbrowser.open("https://vb-audio.com/Cable/")
                       │      then the process ends
                       └── [ Exit ]
                              the process ends
```

- The gate is the only window that appears when the cable is missing, and it reappears on every
  launch until the driver is installed.
- Both actions close the application on purpose: installing the driver requires a reboot.
- The vendor page URL is used rather than a direct `.zip` link because VB-Audio repackages the
  driver periodically, which would invalidate a hard-coded file URL.
- The app never bundles or launches the vendor installer. The VB-CABLE licence forbids
  integrating its package into another software's installation procedure, so the user is sent to
  the vendor's own site and installs it themselves.
- `VIRTUAL_SINK_HINTS` still guards the *feedback* check at stream start, so if the user
  deliberately selects a non-cable virtual device (for example VoiceMeeter) the audio-routing
  guard still recognises it as a virtual sink.

---

## Network Protocol (Protocol v2)

### Header (9 bytes)

| Offset | Size | Field | Encoding |
|---|---|---|---|
| 0 | 1 | Magic byte 1 | `0x50` = `'P'` |
| 1 | 1 | Magic byte 2 | `0x4D` = `'M'` |
| 2 | 1 | Protocol version | `0x02` |
| 3 | 1 | Packet type | see table below |
| 4 | 1 | Channels (audio) / payload byte 0 (control) | `1` = mono, `2` = stereo |
| 5 | 2 | Sequence number | uint16, **big-endian**, wraps mod 65536 |
| 7 | 2 | Samples per channel | uint16, **big-endian** (audio only) |

PCM payload is signed 16-bit **little-endian**, interleaved.

> **Why v2?** Protocol v1 reused offset 2 for both the version and the packet type, so audio was only recognised because `TYPE_AUDIO` happened to equal `1`. Any change to either value silently routed audio into a legacy branch that played the header bytes as full-scale noise. v2 gives version and type separate bytes and rejects mismatches with an explicit log line.

### Packet types

| Type | Direction | Payload after header |
|---|---|---|
| `0x01` Audio | Phone → PC | PCM frames |
| `0x02` Heartbeat ACK | Phone → PC (new) | `[live\|muted][vu]` |
| `0x02` Heartbeat ACK | PC → Phone | `[live\|muted][vu]` |
| `0x03` Connection Request | PC → Phone | `[name_len] + UTF-8 hostname` |
| `0x04` Connection Response | Phone → PC | `[1 = allowed / 0 = declined]` |
| `0x05` Remote Command | both | `1 = mute, 2 = unmute, 3 = stop` |

### Audio packet size
- Stereo: 240 samples/channel × 2 ch × 2 B = **960 B** payload (5 ms), 200 packets/s
- Mono: 480 samples/channel × 1 ch × 2 B = **960 B** payload (10 ms), 100 packets/s
- Datagram: 9 + 960 = **969 bytes**, sent 100 or 200 times per second.

The PC validates the payload against the declared sample count and drops any datagram that is short, so a truncated packet cannot desynchronise the stream.

### Ports
- **Audio**: 8088 (UDP)
- **Discovery**: 8089 (UDP)

### Discovery (ASCII, UTF-8, port 8089)
- Phone broadcasts `PM_BEACON:<Manufacturer> <Model>:8088` every 2 s.
- PC sends `PM_CONNECT:<PC hostname>` to request pairing.
- Phone replies `PM_ACCEPT:<name>` or `PM_DECLINE:<name>`.
- Either side can send `PM_FORGET:` to unpair.
