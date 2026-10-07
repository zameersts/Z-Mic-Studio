"""
ZMic Studio Windows Companion - Accessible wxPython GUI
Created & Designed by Zameer
Provides a screen-reader friendly (NVDA compatible) tabbed interface for:
- Connecting to the Android ZMic Studio stream over UDP or capturing PC Microphones.
- Real-time DSP audio processing (Continuous Spectral Noise Reduction, Stereo 3D, Volume Gain, Studio Echo, Ping-Pong Delay).
- Outputting directly into "CABLE Input (VB-Audio Virtual Cable)" via WASAPI.
- Live microphone monitoring, global mute hotkeys, and accessible keyboard navigation.
"""

import sys
import os
import time
import socket
import queue
import threading
import logging
from logging.handlers import RotatingFileHandler
import numpy as np
import struct
import sounddevice as sd
import wx
import terms

def get_log_dir():
    appdata = os.environ.get('APPDATA')
    if appdata:
        log_dir = os.path.join(appdata, 'ZMicStudio')
        try:
            os.makedirs(log_dir, exist_ok=True)
            return log_dir
        except Exception:
            pass
    return os.path.dirname(os.path.abspath(__file__))

LOG_DIR = get_log_dir()
LOG_FILE = os.path.join(LOG_DIR, 'zmic_studio.log')
CRASH_LOG_FILE = os.path.join(LOG_DIR, 'zmic_gui.log')

def setup_logger():
    logger = logging.getLogger("ZMicStudio")
    logger.setLevel(logging.DEBUG)
    handler = RotatingFileHandler(LOG_FILE, maxBytes=5*1024*1024, backupCount=3, encoding='utf-8')
    formatter = logging.Formatter('%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    handler.setFormatter(formatter)
    if not logger.handlers:
        logger.addHandler(handler)
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(formatter)
        logger.addHandler(console)
    return logger

log = setup_logger()

PORT_DEFAULT = 8088
DISCOVERY_PORT = 8089
SAMPLE_RATE = 48000

# Single source of truth for the product version. Must match versionName in
# android/app/build.gradle.kts.
APP_NAME = "Z Mic Studio"
APP_VERSION = "0.9.0-beta"
APP_VERSION_DISPLAY = "v" + APP_VERSION.replace("beta", "Beta")

BLOCK_SIZE = 960  # output frames per callback = 20ms at 48kHz (960 frames x 2ch x 2B = 3840 B)

# ---------------------------------------------------------------------
# Protocol v2 Binary Header (9 bytes)
#   [0]      Magic byte 1  'P' (0x50)
#   [1]      Magic byte 2  'M' (0x4D)
#   [2]      Protocol version
#   [3]      Packet type
#   [4]      Channels (audio) / payload byte 0 (control)
#   [5..6]   Sequence number     (uint16, big-endian)
#   [7..8]   Samples per channel (uint16, big-endian)
#
# v1 reused offset 2 for BOTH version and type, so audio was only recognised because
# TYPE_AUDIO happened to equal 1. v2 gives version and type their own bytes.
# ---------------------------------------------------------------------
HEADER_MAGIC = b'PM'
PROTOCOL_VERSION = 2
HEADER_SIZE = 9
OFF_VERSION = 2
OFF_TYPE = 3
OFF_CHANNELS = 4
OFF_SEQUENCE = 5
OFF_SAMPLES = 7
MIN_PACKET_SIZE = 4  # magic(2) + version(1) + type(1)

# Packet types (must mirror com.phonemic.app.audio.AudioConfig)
TYPE_AUDIO = 0x01                # Phone -> PC: [PM][2][1][ch][seq][samples] + PCM
TYPE_HEARTBEAT_ACK = 0x02        # Phone -> PC: [PM][2][2][live|muted][vu]
TYPE_CONNECTION_REQUEST = 0x03   # PC -> Phone
TYPE_CONNECTION_RESPONSE = 0x04  # Phone -> PC: [PM][2][4][1=Allowed/0=Declined]
TYPE_REMOTE_CMD = 0x05           # PC <-> Phone: [PM][2][5][1=Mute/2=Unmute/3=Stop]

STATUS_LIVE = 0x01
STATUS_MUTED = 0x02

# Remote commands
CMD_MUTE = 0x01
CMD_UNMUTE = 0x02
CMD_STOP = 0x03

# Legacy headerless payload sizes, used only when a datagram carries NO magic bytes at all.
LEGACY_STEREO_PACKET_SIZE = 3840   # 20ms stereo
LEGACY_MONO_PACKET_SIZE = 1920     # 20ms mono
MIN_LEGACY_PACKET_SIZE = 128

# Jitter buffer tuning (milliseconds)
#
# Measured on a real 75 second stream at 48kHz stereo over home Wi-Fi:
#   14,732 packets arrived, none lost, but the buffer ran dry twice and trimmed twice.
#   Four audible cuts in 75 seconds, roughly one every 19 seconds.
#
# So the network is clean and the cause is uneven arrival timing, which needs room for
# the bursts.
#
# Two measured runs over home Wi-Fi:
#   60/100ms, 75s: 2 underruns, 2 trims
#   80/100ms, 39s: 2 underruns, 4 trims
#
# Raising the target alone made trimming worse, because the ceiling was left only 20ms
# above it. Wi-Fi delivers audio in clumps, so with a narrow window the buffer filled to
# the ceiling and audio was thrown away, which is a cut of its own. The ceiling is now
# twice the target, so a clump has somewhere to go.
#
# This is a deliberate trade. More room means fewer cuts and more delay. Peak latency is
# the ceiling, so a caller who cannot accept 160ms should lower this value.
JITTER_TARGET_MS = 80
JITTER_MAX_MS = 160

# 80Hz rumble cut, as a numpy FIR. The tap count is what keeps speech intact: too few taps
# and the filter rolls off into the 150-250Hz range where the voice lives. Measured against
# the scipy Butterworth this replaces, 769 taps is within 2dB at 100Hz and within 0.1dB from
# 180Hz upwards, while rejecting more rumble.
RUMBLE_CUTOFF_HZ = 80
RUMBLE_FIR_TAPS = 769

# Maximum PC hostname length encoded into a pairing request
MAX_PC_NAME_BYTES = 32

# Substrings that mark an audio device as a virtual sink rather than a physical speaker
VIRTUAL_SINK_HINTS = ('cable', 'voicemeeter', 'virtual', 'vac', 'line', 'vb-audio', 'sine')

# Official VB-Audio download page. The full page is used rather than a direct .zip link because
# VB-Audio repackages the driver periodically, which invalidates any hard-coded file URL.
VB_CABLE_DOWNLOAD_URL = "https://vb-audio.com/Cable/"


# Accessibility: NVDA / Screen Reader Speech & Non-blocking Audio Cues
_accessibility_speaker = None
_accessibility_lock = threading.Lock()
_last_spoken_text = ""
_last_spoken_time = 0.0
_speech_lock = threading.Lock()

def get_accessibility_speaker():
    """Lazily initializes accessible_output2 / SAPI speech provider."""
    global _accessibility_speaker
    with _accessibility_lock:
        if _accessibility_speaker is None:
            try:
                import accessible_output2.outputs.auto
                _accessibility_speaker = accessible_output2.outputs.auto.Auto()
            except Exception:
                try:
                    import comtypes.client
                    _accessibility_speaker = comtypes.client.CreateObject("SAPI.SpVoice")
                except Exception:
                    _accessibility_speaker = False
        return _accessibility_speaker

def speak_accessibility(text, interrupt=True):
    """Speaks text via active screen reader (NVDA, JAWS, Narrator) asynchronously with deduplication."""
    global _last_spoken_text, _last_spoken_time
    clean = text.strip()
    if not clean:
        return
    now = time.time()
    with _speech_lock:
        if clean.lower() == _last_spoken_text.lower() and (now - _last_spoken_time) < 2.5:
            return  # Suppress duplicate speech within 2.5 seconds
        _last_spoken_text = clean
        _last_spoken_time = now

    def _worker():
        try:
            spk = get_accessibility_speaker()
            if spk and hasattr(spk, 'speak'):
                spk.speak(clean, interrupt=interrupt)
            elif spk and hasattr(spk, 'Speak'):
                spk.Speak(clean, 1)  # SVSFlagsAsync
        except Exception:
            pass
    threading.Thread(target=_worker, daemon=True).start()

def _resolve_sound_path(event_name):
    """Absolute path of the WAV asset for an event, or None when it is not bundled."""
    if not event_name:
        return None
    safe = "".join(ch for ch in str(event_name) if ch.isalnum() or ch == "_")
    if not safe:
        return None
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sounds", f"{safe}.wav")
    return path if os.path.exists(path) else None


def play_audio_cue(frequency, duration_ms):
    """
    Queues a tone cue. Playback happens on a single dedicated worker thread so that cues are
    sequential rather than interrupting each other.
    """
    _enqueue_cue(('tone', frequency, duration_ms))


# ----------------------------------------------------------------------------
# Notification cue queue
# ----------------------------------------------------------------------------
# winsound.PlaySound and winsound.Beep share one system sound facility: starting a new
# sound immediately CUTS OFF whatever is playing. With 1.2-1.3 second voice clips and
# bursts of events that clipped announcements mid-word. A single serialised worker
# guarantees every cue is heard in full.
_cue_queue = queue.Queue(maxsize=16)
_cue_worker_lock = threading.Lock()
_cue_worker_started = False


def _cue_worker_loop():
    while True:
        item = _cue_queue.get()
        try:
            if item is None:  # shutdown sentinel
                break
            try:
                import winsound
                if item[0] == 'file':
                    # SND_SYNC (no SND_ASYNC) so this worker blocks until finished.
                    winsound.PlaySound(item[1], winsound.SND_FILENAME)
                else:
                    winsound.Beep(item[1], item[2])
            except Exception as e:
                log.debug(f"Audio cue playback failed: {e}")
        finally:
            _cue_queue.task_done()


def _ensure_cue_worker():
    global _cue_worker_started
    with _cue_worker_lock:
        if _cue_worker_started:
            return
        _cue_worker_started = True
        threading.Thread(target=_cue_worker_loop, daemon=True, name="ZMicCueWorker").start()


def _enqueue_cue(item):
    """Enqueues a cue, dropping the OLDEST one if the queue is saturated."""
    _ensure_cue_worker()
    try:
        _cue_queue.put_nowait(item)
    except queue.Full:
        try:
            _cue_queue.get_nowait()      # discard oldest
            _cue_queue.put_nowait(item)
        except (queue.Empty, queue.Full):
            log.debug("Cue queue saturated; cue dropped.")


NOTIFICATION_MODE_NATURAL = "natural"   # Recorded natural voice (.wav)
NOTIFICATION_MODE_NVDA = "nvda"         # Screen reader TTS only
NOTIFICATION_MODE_BEEPS = "beeps"       # Tone beeps only

_notification_mode = NOTIFICATION_MODE_NATURAL
_last_notify_times = {}
_notify_lock = threading.Lock()


def set_notification_mode(mode):
    global _notification_mode
    if mode not in (NOTIFICATION_MODE_NATURAL, NOTIFICATION_MODE_NVDA, NOTIFICATION_MODE_BEEPS):
        log.warning(f"Ignoring unknown notification mode: {mode!r}")
        return False
    _notification_mode = mode
    log.info(f"Notification mode set to: {mode}")
    return True


def get_notification_mode():
    return _notification_mode


def notify_event(event_name, speech_text, beep_freq=1000, beep_duration=150, debounce_sec=2.0):
    """
    Emits a notification according to the selected mode, with per-event debouncing.

    Natural Voice mode plays the recorded voice clip; when no clip is bundled for the event it
    falls back to screen-reader speech so the event is never silent. The voice clip already
    conveys the message, so no duplicate speech is emitted when the clip plays.
    """
    now = time.time()
    with _notify_lock:
        if now - _last_notify_times.get(event_name, 0.0) < debounce_sec:
            return False
        _last_notify_times[event_name] = now

    mode = _notification_mode

    if mode == NOTIFICATION_MODE_NATURAL:
        sound_path = _resolve_sound_path(event_name)
        if sound_path:
            _enqueue_cue(('file', sound_path))
            return True
        log.debug(f"No voice clip for '{event_name}'; falling back to screen-reader speech.")
        speak_accessibility(speech_text)
        return True

    if mode == NOTIFICATION_MODE_NVDA:
        speak_accessibility(speech_text)
        return True

    play_audio_cue(beep_freq, beep_duration)
    return True


def check_notification_audibility():
    """
    Diagnostics: winsound plays through the Windows DEFAULT output device. If the user has set
    that to a virtual sink (for example CABLE Input, to route all system audio into the mic),
    notification audio is routed into the cable and becomes inaudible. Warn rather than fail.
    """
    try:
        default_out = sd.default.device[1]
        if default_out is None:
            return None
        info = sd.query_devices(default_out)
        name = info.get('name', '')
        if any(v in name.lower() for v in VIRTUAL_SINK_HINTS):
            msg = (
                f"Windows default output device is '{name}', which is a virtual audio sink. "
                "Notification sounds will play into that device and may not be audible. "
                "Set your Windows default output to your speakers or headphones."
            )
            log.warning(msg)
            return msg
        return None
    except Exception as e:
        log.debug(f"Could not check notification audibility: {e}")
        return None


def get_local_ips():
    """Returns a list of active non-loopback local IPv4 addresses."""
    ips = []
    try:
        hostname = socket.gethostname()
        for ip in socket.gethostbyname_ex(hostname)[2]:
            if not ip.startswith("127."):
                ips.append(ip)
    except Exception:
        pass
    return ips if ips else ["127.0.0.1"]

def is_wasapi_device(device_id):
    """Returns True if the given device ID uses the Windows WASAPI host API."""
    try:
        if device_id is None:
            return False
        dev_info = sd.query_devices(device_id)
        api_info = sd.query_hostapis(dev_info['hostapi'])
        return 'WASAPI' in api_info['name']
    except Exception:
        return False

def get_output_devices():
    """Dynamically queries and returns audio output devices with their IDs and names."""
    devices = []
    default_cable_idx = -1
    try:
        raw_devices = sd.query_devices()
        wasapi_outputs = []
        other_outputs = []
        for idx, d in enumerate(raw_devices):
            if d.get('max_output_channels', 0) > 0:
                hostapi = sd.query_hostapis(d['hostapi'])['name']
                name = d['name']
                item = (idx, name)
                if 'WASAPI' in hostapi:
                    wasapi_outputs.append(item)
                else:
                    other_outputs.append(item)
        devices = wasapi_outputs if wasapi_outputs else other_outputs

        # Auto-select CABLE Input if available
        for i, (dev_id, name) in enumerate(devices):
            if 'cable input' in name.lower():
                default_cable_idx = i
                break
        if default_cable_idx != -1:
            log.info(f"Discovered {len(devices)} output audio device(s). Auto-selected CABLE Input at index {default_cable_idx}.")
        else:
            log.warning("VB-Audio Virtual Cable ('CABLE Input') was NOT detected among output audio devices.")
    except Exception as e:
        log.error(f"Error querying output devices: {e}", exc_info=True)
        devices.append((0, f"Default Output Device ({e})"))
    return devices, default_cable_idx

def is_virtual_cable_present():
    """
    Startup gate check.

    Returns (present, device_id, device_name). ZMic Studio cannot function without a virtual
    audio cable, because that is what makes the phone's audio appear to other applications as
    an ordinary Windows microphone. When it is missing the main interface is never built.

    VB-Audio Virtual Cable is downloaded and installed by the end user from the vendor's own
    website; ZMic Studio does not bundle or launch its installer.
    """
    try:
        devices, cable_idx = get_output_devices()
    except Exception as e:
        log.error(f"Cable presence check failed: {e}", exc_info=True)
        return False, None, None

    if cable_idx == -1 or not devices:
        log.warning("Startup gate: VB-Audio Virtual Cable not found. Main interface will not open.")
        return False, None, None

    dev_id, dev_name = devices[cable_idx]
    log.info(f"Startup gate passed: virtual cable '{dev_name}' (id {dev_id}).")
    return True, dev_id, dev_name

def get_input_devices():
    """Dynamically queries and returns all hardware audio input devices (microphones) on this PC."""
    devices = []
    try:
        raw_devices = sd.query_devices()
        wasapi_inputs = []
        other_inputs = []
        for idx, d in enumerate(raw_devices):
            if d.get('max_input_channels', 0) > 0:
                hostapi = sd.query_hostapis(d['hostapi'])['name']
                name = d['name']
                # Ignore loopback cable output from microphone list to avoid feedback loops
                if 'cable' in name.lower():
                    continue
                item = (idx, name)
                if 'WASAPI' in hostapi:
                    wasapi_inputs.append(item)
                else:
                    other_inputs.append(item)
        devices = wasapi_inputs if wasapi_inputs else other_inputs
        log.info(f"Discovered {len(devices)} hardware input microphone(s) via sounddevice:")
        for dev_id, name in devices:
            log.info(f"  - [{dev_id}] {name}")
    except Exception as e:
        log.error(f"Error querying input microphones: {e}", exc_info=True)
    return devices


class AdaptiveJitterBuffer:
    """
    Adaptive jitter buffer for real-time PCM audio streaming.
    Provides pre-buffering, packet loss concealment (linear fade on underrun),
    and overrun trimming to guarantee low latency.
    """
    def __init__(self, target_ms=JITTER_TARGET_MS, max_ms=JITTER_MAX_MS, sample_rate=SAMPLE_RATE, channels=2):
        self.sample_rate = sample_rate
        self.channels = channels
        self.bytes_per_frame = channels * 2  # 16-bit PCM = 2 bytes per sample
        self.bytes_per_ms = (sample_rate * self.bytes_per_frame) // 1000

        self.target_bytes = int(target_ms * self.bytes_per_ms)
        self.max_bytes = int(max_ms * self.bytes_per_ms)

        # Overruns are corrected in small steps rather than one large cut, because dropping
        # hundreds of milliseconds at once is audible as a gap.
        self.trim_step_bytes = 20 * self.bytes_per_ms
        self.retain_bytes = self.target_bytes + self.trim_step_bytes

        self._lock = threading.Lock()
        self._buffer = bytearray()
        self.is_prebuffering = True
        self._had_underrun = False
        self._consecutive_underruns = 0

        # Last sample amplitudes for smooth fade-in/fade-out PLC
        self._last_sample_l = 0.0
        self._last_sample_r = 0.0

        # Telemetry metrics
        self.packets_received = 0
        self.packets_lost = 0
        self.underruns = 0
        self.overruns = 0
        self.trimmed_bytes = 0

    def write(self, pcm_bytes: bytes):
        """Pushes raw PCM stereo bytes into the jitter buffer."""
        if not pcm_bytes:
            return
        with self._lock:
            self._buffer.extend(pcm_bytes)
            self.packets_received += 1

            # End startup prebuffering once target depth is achieved
            if self.is_prebuffering and len(self._buffer) >= self.target_bytes:
                self.is_prebuffering = False
                self._consecutive_underruns = 0

            # Overrun protection: trim back toward the target in bounded steps. Correcting in
            # one shot produced a large audible gap because latency could reach max_ms first.
            if len(self._buffer) > self.max_bytes:
                excess = len(self._buffer) - self.retain_bytes
                if excess > 0:
                    trim_bytes = min(excess, self.trim_step_bytes)
                    trim_bytes = (trim_bytes // self.bytes_per_frame) * self.bytes_per_frame
                    if trim_bytes > 0:
                        del self._buffer[:trim_bytes]
                        self.overruns += 1
                        self.trimmed_bytes += trim_bytes

    def read(self, num_bytes: int) -> bytes:
        """Pulls exact num_bytes from buffer for the audio callback."""
        with self._lock:
            if self.is_prebuffering:
                return b'\x00' * num_bytes

            available = len(self._buffer)
            if available >= num_bytes:
                self._consecutive_underruns = 0
                chunk = bytes(self._buffer[:num_bytes])
                del self._buffer[:num_bytes]

                # Only apply fade-in once when recovering from an underrun gap (eliminates 50Hz 'tir-tir' buzz)
                if self._had_underrun:
                    chunk = self._apply_fade_in(chunk)
                    self._had_underrun = False

                if len(chunk) >= 4:
                    l_val, r_val = struct.unpack_from('<hh', chunk, len(chunk) - 4)
                    self._last_sample_l = float(l_val)
                    self._last_sample_r = float(r_val)

                return chunk
            else:
                # Underrun occurred: audio buffer does not have enough frames for this callback.
                self.underruns += 1
                self._had_underrun = True
                self._consecutive_underruns += 1

                # If underruns persist (2 or more in a row), re-enter prebuffering to rebuild the cushion
                if self._consecutive_underruns >= 2:
                    self.is_prebuffering = True

                if available > 0:
                    partial = bytes(self._buffer[:available])
                    self._buffer.clear()
                    partial_faded = self._apply_fade_out(partial)
                    silence_needed = num_bytes - len(partial_faded)
                    return partial_faded + (b'\x00' * silence_needed)
                else:
                    return self._generate_fade_to_zero(num_bytes)

    def _apply_fade_in(self, chunk: bytes, ramp_frames=64) -> bytes:
        """Applies a smooth linear fade-in ramp over ramp_frames to eliminate clicks."""
        samples = np.frombuffer(chunk, dtype=np.int16).copy()
        total_frames = len(samples) // 2
        actual_ramp = min(ramp_frames, total_frames)
        if actual_ramp > 0:
            ramp = np.linspace(0.0, 1.0, actual_ramp, dtype=np.float32)
            samples[0:actual_ramp*2:2] = (samples[0:actual_ramp*2:2].astype(np.float32) * ramp).astype(np.int16)
            samples[1:actual_ramp*2:2] = (samples[1:actual_ramp*2:2].astype(np.float32) * ramp).astype(np.int16)
        if len(samples) >= 2:
            self._last_sample_l = float(samples[-2])
            self._last_sample_r = float(samples[-1])
        else:
            self._last_sample_l = 0.0
            self._last_sample_r = 0.0
        return samples.tobytes()

    def _apply_fade_out(self, chunk: bytes, ramp_frames=64) -> bytes:
        """Applies a smooth linear fade-out ramp to zero at the end of the available chunk."""
        samples = np.frombuffer(chunk, dtype=np.int16).copy()
        total_frames = len(samples) // 2
        actual_ramp = min(ramp_frames, total_frames)
        if actual_ramp > 0:
            start_frame = total_frames - actual_ramp
            ramp = np.linspace(1.0, 0.0, actual_ramp, dtype=np.float32)
            samples[start_frame*2::2] = (samples[start_frame*2::2].astype(np.float32) * ramp).astype(np.int16)
            samples[start_frame*2+1::2] = (samples[start_frame*2+1::2].astype(np.float32) * ramp).astype(np.int16)
        self._last_sample_l = 0.0
        self._last_sample_r = 0.0
        return samples.tobytes()

    def _generate_fade_to_zero(self, num_bytes: int, ramp_frames=64) -> bytes:
        """Ramps from last known sample down to zero silence."""
        total_frames = num_bytes // self.bytes_per_frame
        if self._last_sample_l == 0.0 and self._last_sample_r == 0.0:
            return b'\x00' * num_bytes
        out_samples = np.zeros(total_frames * 2, dtype=np.int16)
        actual_ramp = min(ramp_frames, total_frames)
        if actual_ramp > 0:
            ramp = np.linspace(1.0, 0.0, actual_ramp, dtype=np.float32)
            out_samples[0:actual_ramp*2:2] = (self._last_sample_l * ramp).astype(np.int16)
            out_samples[1:actual_ramp*2:2] = (self._last_sample_r * ramp).astype(np.int16)
        self._last_sample_l = 0.0
        self._last_sample_r = 0.0
        return out_samples.tobytes()

    def reset(self):
        """Clears the buffer and resets states and metrics."""
        with self._lock:
            self._buffer.clear()
            self.is_prebuffering = True
            self._had_underrun = False
            self._consecutive_underruns = 0
            self._last_sample_l = 0.0
            self._last_sample_r = 0.0

    def get_stats(self):
        """Returns buffer health metrics."""
        with self._lock:
            current_ms = len(self._buffer) / self.bytes_per_ms if self.bytes_per_ms > 0 else 0
            return {
                "buffered_ms": round(current_ms, 1),
                "target_ms": round(self.target_bytes / self.bytes_per_ms, 1),
                "max_ms": round(self.max_bytes / self.bytes_per_ms, 1),
                "is_prebuffering": self.is_prebuffering,
                "underruns": self.underruns,
                "overruns": self.overruns,
                "trimmed_ms": round(self.trimmed_bytes / self.bytes_per_ms, 1) if self.bytes_per_ms > 0 else 0,
                "packets_received": self.packets_received,
                "packets_lost": self.packets_lost,
            }


def parse_audio_packet(data):
    """
    Parses a Protocol v2 audio datagram and validates its payload.

    Pure function: no instance state is read or written, so it is trivially testable.
    Returns a dict, or None when the packet is malformed and must be dropped.
    """
    if len(data) < HEADER_SIZE:
        log.warning("Truncated audio header dropped.")
        return None

    channels = data[OFF_CHANNELS]
    sequence = (data[OFF_SEQUENCE] << 8) | data[OFF_SEQUENCE + 1]
    samples_per_ch = (data[OFF_SAMPLES] << 8) | data[OFF_SAMPLES + 1]

    if channels not in (1, 2):
        log.warning(f"Audio packet with invalid channel count {channels} dropped.")
        return None

    pcm = data[HEADER_SIZE:]

    # The advertised sample count is used to validate the payload rather than ignored.
    # A mismatch means a truncated or corrupt datagram.
    expected_bytes = samples_per_ch * channels * 2
    if samples_per_ch <= 0 or len(pcm) < expected_bytes:
        log.warning(
            f"Audio payload too short: got {len(pcm)} B, header declares "
            f"{samples_per_ch} samples x {channels}ch = {expected_bytes} B. Dropped."
        )
        return None
    if len(pcm) > expected_bytes:
        pcm = pcm[:expected_bytes]

    is_stereo = (channels == 2)
    return {
        'channels': channels,
        'sequence': sequence,
        'samples_per_channel': samples_per_ch,
        'is_stereo': is_stereo,
        'pcm': pcm,
        'mode': "Dual-Mic Stereo (v2)" if is_stereo else "Single-Mic Mono (v2)",
    }


def build_control_packet(packet_type, payload_bytes=b''):
    """Builds a Protocol v2 control datagram (header + payload). Pure function."""
    return bytes([HEADER_MAGIC[0], HEADER_MAGIC[1], PROTOCOL_VERSION, packet_type]) + payload_bytes


def design_rumble_fir(taps=RUMBLE_FIR_TAPS, cutoff=RUMBLE_CUTOFF_HZ, fs=SAMPLE_RATE):
    """Builds a windowed-sinc high-pass for the 80Hz rumble cut.

    This replaced a second order Butterworth from scipy.signal. scipy existed in this project
    for this one filter and nothing else, and it was expensive both ways: 109MB on disk, and
    it dragged in scipy.integrate whose generated _lebedev table alone took 12 minutes to
    compile and pulled 39MB of OpenBLAS into the bundle, for a 45 minute build.

    A short kernel was not acceptable. At 129 taps the filter was already 8.8dB down at
    200Hz, which is where speech fundamentals sit, so it thinned the voice. At 769 taps the
    response is within 2dB of the Butterworth at 100Hz and within 0.1dB from 180Hz up, and it
    rejects more rumble rather than less. It costs about 6% of one 5ms frame, which is nothing
    beside a jitter buffer an order of magnitude deeper than the latency it adds.
    """
    n = np.arange(taps, dtype=np.float64)
    mid = (taps - 1) / 2.0
    fc = cutoff / fs
    h = np.where(np.abs(n - mid) < 1e-12,
                 1.0 - 2.0 * fc,
                 -2.0 * fc * np.sinc(2.0 * fc * (n - mid)))
    h = h * np.blackman(taps)
    # Subtracting the mean makes DC rejection exact rather than approximate, which a window
    # on its own does not guarantee.
    h -= h.mean()
    return h.astype(np.float32)


def apply_rumble_fir(samples, kernel, tail):
    """Filters one channel, carrying the tail across packets.

    np.convolve returns the whole convolution, so the leading outputs of this packet depend
    on the previous one. Discarding that tail would leave a discontinuity at every 5ms
    boundary, which is audible as a tick. The tail is returned so the next packet continues.
    """
    history = np.concatenate((tail, samples))
    # np.convolve is full convolution, so index n of its result is sum_m history[n-m]*kernel[m].
    # Output for the current samples therefore sits at index len(kernel)-1, not at 0. Taking
    # the first len(samples) entries instead reads the zero-padded warm-up region and returns
    # signal that is misaligned by the whole kernel length, which sounds like garbage.
    start = len(kernel) - 1
    filtered = np.convolve(history, kernel)[start:start + len(samples)]
    new_tail = history[-start:] if start else np.empty(0, dtype=np.float32)
    return filtered, new_tail


class AudioEngine:
    """Handles audio capture (Phone UDP or PC Microphones), DSP processing, and WASAPI output."""
    def __init__(self, on_status_changed, on_packet_received):
        self.on_status_changed = on_status_changed
        self.on_packet_received = on_packet_received

        self.is_running = False
        self.is_muted = False
        self.on_remote_mute = None
        self.is_monitoring = False
        self.target_device_id = None
        self.port = PORT_DEFAULT

        # Input source configuration: 'phone' or 'pc'
        self.input_mode = "phone" # 'phone' (UDP) or 'pc' (Hardware Microphones)
        self.input_device_id = None
        self.input_stream = None
        self.pc_packet_count = 0

        # DSP Configuration
        self.volume_gain = 1.0
        self.mode = "Stereo 3D" # Options: Stereo 3D, Center Mono, Left Mic, Right Mic
        self.stereo_width = 3.5 # 3.5x Mid/Side spatial width expander
        self.echo_enabled = False
        self.ping_pong_enabled = False # Ping-Pong Stereo bouncing echo
        self.echo_delay_ms = 240 # Milliseconds
        self.echo_feedback = 0.40 # 0.0 to 0.85

        # Audio threads, sockets, and jitter buffers
        self.sock = None
        self.udp_thread = None
        self.stream = None
        self.monitor_stream = None
        self.connected_phone_addr = None

        # True while the phone's UDP socket and receiver thread stay alive purely to keep
        # answering the phone's heartbeat, even though a PC microphone is the active source.
        # See _stop_current_input for why a swap must not tear that link down.
        self._phone_link_held = False

        # Jitter buffer tuning. target=60ms cushion, hard ceiling=100ms so latency can never
        # balloon to a quarter second before being corrected (which caused audible gaps).
        self.jitter_buffer = AdaptiveJitterBuffer(target_ms=JITTER_TARGET_MS, max_ms=JITTER_MAX_MS)
        self.monitor_jitter_buffer = AdaptiveJitterBuffer(target_ms=JITTER_TARGET_MS, max_ms=JITTER_MAX_MS)

        # Pairing state persisted across sessions so a trusted phone is never re-prompted.
        self.trusted_phone_ip = None

        # Set by the UI when the user explicitly accepts feedback risk on physical speakers.
        self.allow_physical_speakers = False

        # Our own hostname, encoded once for every pairing request we send.
        try:
            self.pc_name_bytes = socket.gethostname().encode('utf-8')[:MAX_PC_NAME_BYTES]
        except Exception:
            self.pc_name_bytes = b"Windows PC"

        # Unit tests / diagnostics: skip the pairing handshake entirely.
        self.auto_approve_all = bool(getattr(sys, '_running_tests', False)) or \
            os.environ.get('ZMIC_TEST_MODE') == '1'

        # Continuous Spectral Noise Reduction (Wiener Filter) & 80Hz Low-Cut
        self.spectral_noise_enabled = False # Default OFF, toggleable via Alt+N
        # The rumble cut has its own switch, so the voice can be compared with it on and off.
        self.rumble_enabled = True
        self.noise_reduction_level = 40     # 40% default balanced suppression
        self.noise_profile_l = np.full(BLOCK_SIZE // 2 + 1, 50.0, dtype=np.float32)
        self.noise_profile_r = np.full(BLOCK_SIZE // 2 + 1, 50.0, dtype=np.float32)
        self._init_filter()

        # Echo buffer state
        self._init_echo_buffer()

    def _init_echo_buffer(self):
        """Initializes circular ring buffers for studio echo and ping-pong delay."""
        self.delay_buf_size = SAMPLE_RATE # 1.0 second max buffer at 48000 Hz
        self.delay_buf_l = np.zeros(self.delay_buf_size, dtype=np.float32)
        self.delay_buf_r = np.zeros(self.delay_buf_size, dtype=np.float32)
        self.delay_ptr = 0

    def _init_filter(self):
        """Initializes the 80Hz rumble-cut filter and its state variables."""
        self.rumble_kernel = design_rumble_fir()
        self.rumble_tail_l = np.zeros(len(self.rumble_kernel) - 1, dtype=np.float32)
        self.rumble_tail_r = np.zeros(len(self.rumble_kernel) - 1, dtype=np.float32)
        log.info(
            f"80Hz rumble-cut filter ready ({RUMBLE_FIR_TAPS} tap numpy FIR, no scipy needed)."
        )

    def start(self, port, target_device_id):
        """Starts audio capture and WASAPI output stream."""
        if self.is_running:
            log.warning("AudioEngine.start called but engine is already running.")
            return True, "Already running"

        # Pre-cleanup: ensure any stale streams or threads are closed cleanly first
        self.stop()

        self.port = port
        self.target_device_id = target_device_id
        self.is_running = True
        log.info(f"Starting AudioEngine: input_mode={self.input_mode}, port={port}, target_device_id={target_device_id}")

        # 1. Start input capture (Phone UDP or PC Mic)
        ok, msg = self._start_current_input()
        if not ok:
            log.error(f"Failed to start input capture ({self.input_mode}): {msg}")
            self.stop()
            return False, msg

        # 2. Start output stream to target device (VB-Cable)
        ok_out, msg_out = self._start_output_stream()
        if not ok_out:
            log.error(f"Failed to start output stream: {msg_out}")
            self.stop()
            return False, msg_out

        # 3. Start monitoring stream if active
        if self.is_monitoring:
            self._start_monitor_stream()

        status_msg = f"Listening on UDP port {self.port}" if self.input_mode == "phone" else "Capturing PC Mic"
        log.info(f"AudioEngine started successfully: {status_msg}")
        return True, status_msg

    def _send_stop_to_phone(self):
        """Best-effort CMD_STOP so the phone can release the microphone and Wi-Fi locks."""
        if not (self.sock and self.connected_phone_addr):
            return
        try:
            cmd_pkt = build_control_packet(TYPE_REMOTE_CMD, bytes([CMD_STOP]))
            self.sock.sendto(cmd_pkt, self.connected_phone_addr)
        except Exception as e:
            log.debug(f"Could not notify phone to stop: {e}")

    def stop(self):
        """Stops the audio engine and closes all streams cleanly."""
        log.info("AudioEngine stopping...")
        self._send_stop_to_phone()
        self.connected_phone_addr = None
        self.is_running = False
        self._stop_current_input()

        if self.stream:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception as e:
                log.warning(f"Error closing output stream: {e}")
            self.stream = None

        if self.monitor_stream:
            try:
                self.monitor_stream.stop()
                self.monitor_stream.close()
            except Exception as e:
                log.warning(f"Error closing monitor stream: {e}")
            self.monitor_stream = None

        # Audio health reporting, reset on every stop so each session is judged on its own.
        self._last_audio_health_log = 0.0
        self._last_reported_underruns = 0
        self._max_packet_gap_ms = 0.0
        self._big_gap_count = 0
        self._last_packet_arrival = None

        # Reset jitter buffers safely
        if self.jitter_buffer is not None:
            self.jitter_buffer.reset()
        if self.monitor_jitter_buffer is not None:
            self.monitor_jitter_buffer.reset()
        log.info("AudioEngine stopped cleanly.")
        self.on_status_changed("Stopped", "Idle")

    def _release_phone_link(self):
        """Tears down only the phone side: socket, receiver thread, and the phone's address."""
        self._send_stop_to_phone()
        self.connected_phone_addr = None

        if self.sock:
            try:
                self.sock.close()
            except Exception as e:
                log.warning(f"Error closing audio UDP socket: {e}")
            self.sock = None

        if self.udp_thread and self.udp_thread.is_alive():
            try:
                self.udp_thread.join(timeout=1.5)
            except Exception as e:
                log.warning(f"Error joining UDP receiver thread: {e}")
            self.udp_thread = None

        self._phone_link_held = False

    def release_phone_link(self):
        """Drops the phone connection while local capture carries on.

        Disconnecting the phone used to stop the whole engine, so a user capturing from a PC
        microphone lost their stream because a phone they were not even using went away.
        """
        if not (self.sock or self._phone_link_held or self.connected_phone_addr):
            return
        log.info("Releasing the phone's audio link; local capture continues.")
        self._release_phone_link()

    def hold_phone_link(self):
        """Brings the phone's socket up without making it the capture source.

        Used when a phone connects while the user has chosen a PC microphone. The phone has
        to be receiving something from the PC, otherwise it decides the PC has gone and stops
        streaming, and then it cannot be switched to when it is finally wanted.
        """
        if self.input_mode == "phone":
            return False
        if self.sock and self.connected_phone_addr and \
                self.udp_thread and self.udp_thread.is_alive():
            return False
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
            except Exception:
                pass
            sock.bind(("0.0.0.0", self.port))
            sock.settimeout(0.5)
            self.sock = sock
            self.udp_thread = threading.Thread(target=self._udp_receiver_loop, daemon=True)
            self.udp_thread.start()
            self._phone_link_held = True
            log.info(f"Phone link held open on port {self.port} without taking over capture.")
            return True
        except Exception as e:
            self.sock = None
            log.warning(f"Could not hold the phone link open: {e}")
            return False

    def input_device_name(self):
        """Human readable name of whatever is actually capturing right now."""
        if self.input_mode == "phone":
            addr = self.connected_phone_addr
            return f"the phone at {addr[0]}" if addr else "the phone"
        if self.input_device_id is None:
            return "the PC microphone"
        try:
            return sd.query_devices(self.input_device_id).get("name", "the PC microphone")
        except Exception:
            return "the PC microphone"

    def _stop_current_input(self, keep_phone_link=False):
        """Stops current input capture stream/socket without stopping output.

        keep_phone_link leaves the phone's socket and receiver thread running. The phone
        treats a missing heartbeat as a dead PC and stops streaming on its own, so tearing
        the link down just to capture from a PC microphone left the phone silent the moment
        the user switched back, with the PC still reporting the phone as connected. Keeping
        the socket open costs a few kilobytes of discarded audio and keeps the phone alive.
        """
        if keep_phone_link and self._phone_link_held and self.sock and self.connected_phone_addr:
            log.info("Keeping the phone's audio link open so its heartbeat is still answered.")
        else:
            self._release_phone_link()

        if self.input_stream:
            try:
                self.input_stream.stop()
                self.input_stream.close()
            except Exception as e:
                log.warning(f"Error closing PC mic stream: {e}")
            self.input_stream = None

    def _start_current_input(self):
        """Starts capture for the currently selected input_mode (phone or pc)."""
        if self.input_mode == "phone":
            # Coming back from a PC microphone with the link still held: the socket is
            # already bound and the receiver thread is already answering the phone, so
            # binding a second socket here would only risk a port clash.
            if (self._phone_link_held and self.sock
                    and self.udp_thread and self.udp_thread.is_alive()):
                log.info("Phone's audio link was kept open; resuming audio from it.")
                self.on_status_changed("Listening", f"Resuming audio from {self.port}...")
                return True, f"Resuming phone audio on port {self.port}"
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                    try:
                        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                    except Exception:
                        pass
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024) # 1 MB UDP socket receive buffer
                except Exception:
                    pass
                sock.bind(("0.0.0.0", self.port))
                sock.settimeout(0.5)
                self.sock = sock
                log.info(f"UDP socket bound to 0.0.0.0:{self.port} successfully.")
            except Exception as e:
                self.sock = None
                log.error(f"Failed to bind UDP port {self.port}: {e}", exc_info=True)
                return False, f"Failed to bind UDP port {self.port}: {e}"

            self.udp_thread = threading.Thread(target=self._udp_receiver_loop, daemon=True)
            self.udp_thread.start()
            self.on_status_changed("Listening", f"Waiting for phone on port {self.port}...")
            return True, f"Listening on port {self.port}"
        else:
            return self._start_input_stream()

    def set_input_source(self, source_type, device_id=None):
        """Seamlessly hot-swaps audio input source between Phone and PC Microphones."""
        previous_mode = self.input_mode
        log.info(f"Hot-swapping audio input source: {previous_mode} -> {source_type} (dev_id={device_id})")
        self.input_mode = source_type
        self.input_device_id = device_id
        if self.is_running:
            # Leaving the phone for a PC microphone must not send the phone a stop command
            # or close its socket. It used to, so the phone shut its microphone down and
            # never came back, and switching to the phone again produced silence while the
            # PC still listed the phone as connected.
            if (previous_mode == "phone" and source_type == "pc"
                    and self.sock and self.connected_phone_addr):
                self._phone_link_held = True

            # The link has to be kept in BOTH directions. Tearing it down on the way back to
            # the phone rebinds the socket, and the phone is left talking to a socket that
            # no longer knows the session it belongs to.
            keep_link = bool(self._phone_link_held and self.sock and self.connected_phone_addr)
            self._stop_current_input(keep_phone_link=keep_link)
            ok, msg = self._start_current_input()
            if not ok:
                log.error(f"Hot-swap failed: {msg}")
                self.on_status_changed("Error", msg)
            else:
                log.info(f"Hot-swap successful: {msg}")
            return ok, msg
        return True, "Input source updated"

    def _start_input_stream(self):
        """Opens WASAPI input stream for PC Microphone."""
        if self.input_device_id is None:
            log.error("Cannot start input stream: input_device_id is None.")
            return False, "No PC mic selected"
        try:
            dev_info = sd.query_devices(self.input_device_id)
            channels = min(2, max(1, dev_info.get('max_input_channels', 1)))
            dev_name = dev_info['name']
            self.pc_packet_count = 0
            log.info(f"Opening RawInputStream on device [{self.input_device_id}] '{dev_name}' with {channels} channel(s) at {SAMPLE_RATE}Hz...")

            def input_callback(indata, frames, time_info, status):
                if status:
                    log.warning(f"RawInputStream status alert: {status}")
                if not self.is_running or self.input_mode != "pc":
                    return
                self.pc_packet_count += 1
                is_stereo = (channels == 2)
                processed_bytes = self._process_dsp(bytes(indata), is_stereo=is_stereo)
                self.jitter_buffer.write(processed_bytes)

                if self.is_monitoring and hasattr(self, 'monitor_jitter_buffer') and self.monitor_jitter_buffer is not None:
                    self.monitor_jitter_buffer.write(processed_bytes)

                if self.pc_packet_count % 50 == 0:
                    self.on_packet_received(self.pc_packet_count, dev_name)

            extra_settings = None
            if is_wasapi_device(self.input_device_id) and hasattr(sd, 'WasapiSettings'):
                try:
                    extra_settings = sd.WasapiSettings(auto_convert=True)
                except Exception:
                    pass

            self.input_stream = sd.RawInputStream(
                samplerate=SAMPLE_RATE,
                blocksize=BLOCK_SIZE,
                channels=channels,
                dtype='int16',
                device=self.input_device_id,
                extra_settings=extra_settings,
                callback=input_callback
            )
            self.input_stream.start()
            log.info(f"Hardware mic stream started on '{dev_name}'. Audio frames flowing into DSP pipeline.")
            self.on_status_changed("Connected", f"Capturing from {dev_name}")
            self.on_packet_received(0, dev_name)
            return True, f"Capturing from {dev_name}"
        except Exception as e:
            log.error(f"Failed to open PC mic [{self.input_device_id}]: {e}", exc_info=True)
            return False, f"Failed to open PC mic: {e}"

    def set_target_device(self, device_id):
        """Changes the audio output target device at runtime."""
        self.target_device_id = device_id
        if self.is_running:
            if self.stream:
                try:
                    self.stream.stop()
                    self.stream.close()
                except Exception:
                    pass
                self.stream = None
            self._start_output_stream()

    def set_muted(self, muted):
        """Toggles microphone mute state and tells the phone to do the same."""
        self.is_muted = muted
        if self.sock and self.connected_phone_addr:
            try:
                cmd = CMD_MUTE if muted else CMD_UNMUTE
                pkt = build_control_packet(TYPE_REMOTE_CMD, bytes([cmd]))
                self.sock.sendto(pkt, self.connected_phone_addr)
            except Exception as e:
                log.warning(f"Could not send mute command to phone: {e}")

    def set_trusted_phone_ip(self, ip):
        """Marks a phone as already paired so no repeat pairing prompt is shown."""
        self.trusted_phone_ip = ip
        log.info(f"Trusted phone IP set to {ip}")

    def set_allow_physical_speakers(self, allow):
        """
        Allows routing to a physical output device. This creates a feedback loop risk, so it is
        opt-in only and the UI states the consequence plainly.
        """
        self.allow_physical_speakers = bool(allow)
        log.info(f"Physical speaker output {'ENABLED (feedback risk)' if allow else 'blocked'}")

    def set_volume(self, percent):
        """Sets linear volume gain multiplier (0% to 100%)."""
        self.volume_gain = percent / 100.0

    def set_mode(self, mode_name):
        """Sets channel routing mode (Stereo 3D, Center Mono, Left Mic, Right Mic)."""
        self.mode = mode_name

    def set_stereo_width(self, width):
        """Sets 3D spatial stereo width multiplier (1.0x to 6.0x)."""
        self.stereo_width = width

    def set_echo(self, enabled, delay_ms, feedback):
        """Updates echo DSP parameters."""
        self.echo_enabled = enabled
        self.echo_delay_ms = delay_ms
        self.echo_feedback = feedback
        if not enabled:
            if hasattr(self, 'delay_buf_l'):
                self.delay_buf_l.fill(0)
                self.delay_buf_r.fill(0)

    def set_ping_pong(self, enabled):
        """Toggles Ping-Pong stereo bounce mode."""
        self.ping_pong_enabled = enabled
        if hasattr(self, 'delay_buf_l'):
            self.delay_buf_l.fill(0)
            self.delay_buf_r.fill(0)

    def set_spectral_noise(self, enabled, level):
        """Updates continuous spectral noise reduction parameters."""
        self.spectral_noise_enabled = enabled
        self.noise_reduction_level = level

    def _apply_spectral_denoise(self, samples, noise_prof, level):
        """Applies continuous frequency-domain Wiener filter denoising with smooth room floor."""
        X = np.fft.rfft(samples)
        mag = np.abs(X)

        # Track stationary background noise floor smoothly
        noise_prof[:] = np.minimum(
            noise_prof * 1.002,
            np.where(mag < noise_prof * 1.35, noise_prof * 0.95 + mag * 0.05, noise_prof * 1.002)
        )

        snr = mag / (noise_prof + 1e-6)
        alpha = 0.5 + (level / 100.0) * 2.5
        # Floor 0.10 keeps natural room tone, zero muting
        gain = np.clip(1.0 - (alpha / (snr + 1e-3)), 0.10, 1.0)
        # 3-tap spectral smoothing eliminates musical/watery artifacts
        gain = np.convolve(gain, [0.15, 0.70, 0.15], mode='same')

        clean_X = X * gain
        return np.fft.irfft(clean_X, n=len(samples)).astype(np.float32)

    def set_monitoring(self, enabled):
        """Toggles local monitoring (hearing own voice through headphones)."""
        self.is_monitoring = enabled
        if enabled and self.is_running:
            self._start_monitor_stream()
        elif not enabled and self.monitor_stream:
            try:
                self.monitor_stream.stop()
                self.monitor_stream.close()
            except Exception:
                pass
            self.monitor_stream = None

    def _start_output_stream(self):
        """Opens WASAPI stereo raw output stream to target device (VB-Cable)."""
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None

        def stream_callback(outdata, frames, time_info, status):
            try:
                outdata[:] = self.jitter_buffer.read(len(outdata))
            except Exception:
                outdata[:] = b'\x00' * len(outdata)

        # Acoustic feedback guard: output must go to a virtual sink (VB-Cable, VoiceMeeter, VAC...)
        # unless the user has explicitly accepted the feedback risk.
        dev_info = None
        try:
            dev_info = sd.query_devices(self.target_device_id)
        except Exception as e:
            log.warning(f"Could not query output device {self.target_device_id}: {e}")

        dev_name = dev_info['name'].lower() if dev_info and 'name' in dev_info else ""
        if not dev_name:
            err_msg = f"Output device {self.target_device_id} not found. Select a valid output."
            log.error(err_msg)
            self.on_status_changed("Error", err_msg)
            return False, err_msg

        if not any(v in dev_name for v in VIRTUAL_SINK_HINTS):
            if not self.allow_physical_speakers:
                err_msg = (
                    f"Feedback protection: '{dev_info['name']}' is a physical speaker. "
                    "Select 'CABLE Input', or enable 'Allow Physical Speakers' in the Devices tab."
                )
                log.warning(err_msg)
                self.on_status_changed("Error", err_msg)
                return False, err_msg
            log.warning(
                f"Routing to physical device '{dev_info['name']}' with feedback protection disabled."
            )

        try:
            extra_settings = None
            if is_wasapi_device(self.target_device_id) and hasattr(sd, 'WasapiSettings'):
                try:
                    extra_settings = sd.WasapiSettings(auto_convert=True)
                except Exception:
                    pass

            self.stream = sd.RawOutputStream(
                samplerate=SAMPLE_RATE,
                blocksize=BLOCK_SIZE,
                channels=2, # Always 2 channels (Stereo) for VB-Cable
                dtype='int16',
                device=self.target_device_id,
                latency='low',
                extra_settings=extra_settings,
                callback=stream_callback
            )
            self.stream.start()
            return True, "Output stream started"
        except Exception as e:
            self.stream = None
            err_msg = f"Audio device error: {e}"
            log.error(f"Failed to open output stream on device {self.target_device_id}: {e}", exc_info=True)
            self.on_status_changed("Error", err_msg)
            return False, err_msg

    def _start_monitor_stream(self):
        """Opens a secondary stream to default audio device for headphone monitoring."""
        def monitor_callback(outdata, frames, time_info, status):
            try:
                outdata[:] = self.monitor_jitter_buffer.read(len(outdata))
            except Exception:
                outdata[:] = b'\x00' * len(outdata)

        try:
            default_out = None
            try:
                default_out = sd.default.device[1]
            except Exception:
                pass

            extra_settings = None
            if is_wasapi_device(default_out) and hasattr(sd, 'WasapiSettings'):
                try:
                    extra_settings = sd.WasapiSettings(auto_convert=True)
                except Exception:
                    pass

            self.monitor_stream = sd.RawOutputStream(
                samplerate=SAMPLE_RATE,
                blocksize=BLOCK_SIZE,
                channels=2,
                dtype='int16',
                extra_settings=extra_settings,
                callback=monitor_callback
            )
            self.monitor_stream.start()
        except Exception:
            pass

    @staticmethod
    def _read_circ(buf, ptr, n):
        end = ptr + n
        if end <= len(buf):
            return buf[ptr:end].copy()
        return np.concatenate((buf[ptr:], buf[:end - len(buf)]))

    @staticmethod
    def _write_circ(buf, ptr, data):
        n = len(data)
        end = ptr + n
        if end <= len(buf):
            buf[ptr:end] = data
        else:
            split = len(buf) - ptr
            buf[ptr:] = data[:split]
            buf[:end - len(buf)] = data[split:]

    def _process_dsp(self, raw_bytes, is_stereo=True):
        """Applies Volume, Channel Routing, Ping-Pong Echo DSP to audio frames."""
        if not raw_bytes:
            return b''

        # Fast path: nothing in the chain would change a single sample, so hand the bytes
        # straight back instead of round tripping them through float32. At 200 packets a
        # second the round trip was three NumPy allocations per packet on the receiver
        # thread, which measurably delayed that thread and made the arrival pattern at the
        # PC worse. Skipping it took the gap distribution much closer to what a bare socket
        # sees. The packet count, the header and the wire format are untouched.
        if (is_stereo
                and not self.is_muted
                and self.mode == "Stereo 3D"
                and self.stereo_width == 1.0
                and not self.spectral_noise_enabled
                and not self.rumble_enabled
                and not self.echo_enabled
                and self.volume_gain == 1.0
                and len(raw_bytes) % 2 == 0):
            return raw_bytes

        # 1. Unpack into NumPy int16 array
        # A datagram can be truncated in transit, so the byte count may not divide evenly
        # into 16-bit samples. np.frombuffer raises on that, and the receiver runs this per
        # packet, so one malformed datagram would otherwise throw on every frame after it.
        if len(raw_bytes) % 2 != 0:
            raw_bytes = raw_bytes[:-1]
            if not raw_bytes:
                return b''

        if is_stereo:
            samples = np.frombuffer(raw_bytes, dtype=np.int16)
            if len(samples) % 2 != 0:
                samples = samples[:-1]
        else:
            mono = np.frombuffer(raw_bytes, dtype=np.int16)
            # Duplicate mono samples into stereo (L=M, R=M)
            samples = np.repeat(mono, 2)

        if len(samples) == 0:
            return b''

        if self.is_muted:
            return b'\x00' * (len(samples) * 2)

        # 2. Channel Mode Processing (Optimized: zero redundant allocations for default Stereo 3D)
        float_samples = samples.astype(np.float32)

        if self.mode == "Center Mono":
            left = float_samples[0::2]
            right = float_samples[1::2]
            mono_mix = (left + right) * 0.5
            float_samples[0::2] = mono_mix
            float_samples[1::2] = mono_mix
        elif self.mode == "Left Mic":
            left = float_samples[0::2]
            float_samples[1::2] = left
        elif self.mode == "Right Mic":
            right = float_samples[1::2]
            float_samples[0::2] = right
        elif self.mode == "Stereo 3D":
            if self.stereo_width != 1.0:
                left = float_samples[0::2]
                right = float_samples[1::2]
                mid = (left + right) * 0.5
                side = (left - right) * 0.5
                float_samples[0::2] = mid + (side * self.stereo_width)
                float_samples[1::2] = mid - (side * self.stereo_width)

        # 3a. 80Hz Sub-Bass Rumble Cut (cuts fan motor hum, wind pop)
        #
        # This used to sit inside the spectral noise reduction block, which is off by default.
        # That meant the rumble cut never ran unless the user turned noise reduction on, so a
        # checkbox labelled "80Hz rumble cut" described something that was silently inactive.
        # It is independent now, with its own switch.
        if self.rumble_enabled and self.rumble_kernel is not None:
            try:
                ch_l = float_samples[0::2]
                ch_r = float_samples[1::2]
                ch_l, self.rumble_tail_l = apply_rumble_fir(
                    ch_l, self.rumble_kernel, self.rumble_tail_l)
                ch_r, self.rumble_tail_r = apply_rumble_fir(
                    ch_r, self.rumble_kernel, self.rumble_tail_r)
                float_samples[0::2] = ch_l
                float_samples[1::2] = ch_r
            except Exception:
                log.warning("Rumble cut skipped for this packet", exc_info=True)

        # 3b. Continuous Spectral Noise Reduction (Wiener Filter & 80Hz Low-Cut)
        if self.spectral_noise_enabled:
            try:
                ch_l = float_samples[0::2]
                ch_r = float_samples[1::2]

                # B. Continuous Spectral Noise Reduction (Wiener Filter - NEVER mutes the mic!)
                if self.noise_reduction_level > 0:
                    n_bins = len(ch_l) // 2 + 1
                    if len(self.noise_profile_l) != n_bins:
                        self.noise_profile_l = np.full(n_bins, 50.0, dtype=np.float32)
                    if len(self.noise_profile_r) != n_bins:
                        self.noise_profile_r = np.full(n_bins, 50.0, dtype=np.float32)

                    ch_l = self._apply_spectral_denoise(ch_l, self.noise_profile_l, self.noise_reduction_level)
                    ch_r = self._apply_spectral_denoise(ch_r, self.noise_profile_r, self.noise_reduction_level)

                float_samples[0::2] = ch_l
                float_samples[1::2] = ch_r
            except Exception:
                pass

        # 4. Volume Gain
        if self.volume_gain != 1.0:
            float_samples *= self.volume_gain

        # 5. Studio Echo & Ping-Pong Stereo Delay DSP
        if self.echo_enabled and self.echo_feedback > 0.0:
            try:
                n_frames = len(float_samples) // 2
                delay_samples = int((self.echo_delay_ms / 1000.0) * SAMPLE_RATE)
                delay_samples = max(n_frames, min(self.delay_buf_size - n_frames, delay_samples))
                read_ptr = (self.delay_ptr - delay_samples) % self.delay_buf_size

                d_l = AudioEngine._read_circ(self.delay_buf_l, read_ptr, n_frames)
                d_r = AudioEngine._read_circ(self.delay_buf_r, read_ptr, n_frames)

                left_ch = float_samples[0::2]
                right_ch = float_samples[1::2]
                fb = min(0.85, self.echo_feedback)

                if self.ping_pong_enabled:
                    mono_in = (left_ch + right_ch) * 0.5
                    write_l = (mono_in * fb) + (d_r * fb)
                    write_r = d_l * fb

                    AudioEngine._write_circ(self.delay_buf_l, self.delay_ptr, write_l)
                    AudioEngine._write_circ(self.delay_buf_r, self.delay_ptr, write_r)

                    float_samples[0::2] = left_ch + d_l
                    float_samples[1::2] = right_ch + d_r
                else:
                    write_l = left_ch + (d_l * fb)
                    write_r = right_ch + (d_r * fb)

                    AudioEngine._write_circ(self.delay_buf_l, self.delay_ptr, write_l)
                    AudioEngine._write_circ(self.delay_buf_r, self.delay_ptr, write_r)

                    float_samples[0::2] = left_ch + d_l
                    float_samples[1::2] = right_ch + d_r

                self.delay_ptr = (self.delay_ptr + n_frames) % self.delay_buf_size
            except Exception:
                pass

        out_int16 = np.clip(float_samples, -32768, 32767).astype(np.int16)
        return out_int16.tobytes()

    def _udp_receiver_loop(self):
        """Listens for incoming UDP audio packets from the phone.

        The loop keeps running while a PC microphone is the active source, provided the phone
        link is being held open, because its heartbeat ACK is the only thing telling the phone
        the PC is still there. It used to exit as soon as input_mode stopped being "phone",
        which silently stopped answering and made the phone release its microphone.
        """
        sock = self.sock
        if not sock:
            return

        connected_sender = None
        packet_count = 0
        detected_mode = "Unknown"
        last_packet_time = 0.0
        last_ack_time = 0.0
        # Seeded from the paired-phone record so an already-trusted phone is NOT asked to
        # pair again on every single session.
        is_phone_connected = False
        is_phone_approved = False
        is_pairing_requested = False
        expected_seq = None

        def _is_preapproved(ip):
            """True when this sender is the phone we have already paired with."""
            if self.trusted_phone_ip and ip == self.trusted_phone_ip:
                return True
            if self.auto_approve_all:
                return True
            return False

        while self.is_running and (self.input_mode == "phone" or self._phone_link_held):
            try:
                data, addr = sock.recvfrom(65536)
                if not self.is_running:
                    break

                # ---- 1. Security gate: no magic bytes means this is not our protocol ----
                if len(data) < MIN_PACKET_SIZE or data[0:2] != HEADER_MAGIC:
                    continue

                # ---- 2. Explicit protocol version gate ----
                version = data[OFF_VERSION]
                if version != PROTOCOL_VERSION:
                    if version == 1:
                        log.error(
                            f"Phone at {addr[0]} speaks protocol v1; this app requires v{PROTOCOL_VERSION}. "
                            "Please update the Android app."
                        )
                    else:
                        log.warning(
                            f"Ignoring unsupported protocol v{version} from {addr[0]} "
                            f"(expected v{PROTOCOL_VERSION})"
                        )
                    continue

                ptype = data[OFF_TYPE]

                # ---- 3. Connection Response (phone accepted or declined our request) ----
                if ptype == TYPE_CONNECTION_RESPONSE:
                    allowed = (data[OFF_CHANNELS] == 0x01)
                    if not allowed:
                        log.warning(f"Phone at {addr[0]} DECLINED pairing request.")
                        is_phone_connected = False
                        is_phone_approved = False
                        is_pairing_requested = False
                        connected_sender = None
                        self.connected_phone_addr = None
                        self.on_status_changed("Declined", f"Connection declined by phone ({addr[0]})")
                        notify_event("phone_declined", "Connection declined by phone", 400, 300)
                    else:
                        log.info(f"Phone at {addr[0]} approved pairing request.")
                        is_phone_approved = True
                        if not is_phone_connected:
                            is_phone_connected = True
                            self.connected_phone_addr = addr
                            self.on_status_changed("Connected", f"Receiving audio from {addr[0]}")
                            notify_event("phone_connected", "Phone connected", 1000, 150)
                    continue

                now = time.time()

                # ---- 4. Phone heartbeat: proves liveness without carrying audio ----
                if ptype == TYPE_HEARTBEAT_ACK:
                    last_packet_time = now
                    continue

                # ---- 5. Remote commands from the phone ----
                if ptype == TYPE_REMOTE_CMD:
                    cmd = data[OFF_CHANNELS]
                    if cmd == CMD_MUTE:
                        self.is_muted = True
                        if self.on_remote_mute:
                            wx.CallAfter(self.on_remote_mute, True)
                    elif cmd == CMD_UNMUTE:
                        self.is_muted = False
                        if self.on_remote_mute:
                            wx.CallAfter(self.on_remote_mute, False)
                    elif cmd == CMD_STOP:
                        is_phone_connected = False
                        is_phone_approved = False
                        connected_sender = None
                        self.connected_phone_addr = None
                        self.on_status_changed("Listening", "Phone stopped streaming.")
                    continue

                # ---- 6. Control datagrams we send but never expect back ----
                if ptype in (TYPE_CONNECTION_REQUEST, TYPE_REMOTE_CMD):
                    continue

                # First-sender IP lock: release lock after > 2.0s of silence
                if connected_sender is not None and (now - last_packet_time) > 2.0:
                    connected_sender = None
                    is_pairing_requested = False
                    # NOTE: is_phone_approved is intentionally NOT cleared. It is derived from
                    # the saved paired-phone record, so a trusted phone never has to re-pair.

                if connected_sender is None:
                    connected_sender = addr[0]

                # Drop packets from unauthorized second senders while lock active
                if addr[0] != connected_sender:
                    continue

                last_packet_time = now

                # Localhost auto-approve for unit test suites (e.g. test_phase2.py)
                if addr[0] in ("127.0.0.1", "localhost"):
                    is_phone_approved = True
                    if not is_phone_connected:
                        is_phone_connected = True
                        self.connected_phone_addr = addr
                        self.on_status_changed("Connected", f"Receiving audio from {addr[0]}")

                # Already-paired phone: approve without prompting the user again.
                if not is_phone_approved and _is_preapproved(addr[0]):
                    is_phone_approved = True
                    log.info(f"Auto-approved already-paired phone at {addr[0]}.")

                # If connection hasn't been approved yet:
                if not is_phone_approved:
                    self.connected_phone_addr = addr
                    if not is_pairing_requested:
                        is_pairing_requested = True
                        log.info(f"Phone detected from {addr[0]}:{addr[1]}. Requesting pairing permission...")
                        self.on_status_changed("Connecting", f"Requesting pairing permission from phone at {addr[0]}...")
                        try:
                            pc_bytes = self.pc_name_bytes
                            ident_pkt = build_control_packet(
                                TYPE_CONNECTION_REQUEST,
                                bytes([len(pc_bytes)]) + pc_bytes
                            )
                            sock.sendto(ident_pkt, addr)
                        except Exception as e:
                            log.warning(f"Could not send pairing request: {e}")
                    # While awaiting user approval on phone, do NOT announce connected and do NOT buffer audio
                    continue
                else:
                    self.connected_phone_addr = addr
                    if not is_phone_connected:
                        is_phone_connected = True
                        self.on_status_changed("Connected", f"Receiving audio from {addr[0]}")
                        notify_event("phone_connected", "Phone connected", 1000, 150)

                # Bi-directional Heartbeat ACK: send status to phone every 1.0s
                if is_phone_connected and (now - last_ack_time >= 1.0):
                    last_ack_time = now
                    try:
                        status_code = STATUS_MUTED if self.is_muted else STATUS_LIVE
                        ack_pkt = build_control_packet(
                            TYPE_HEARTBEAT_ACK, bytes([status_code, 0x00])
                        )
                        sock.sendto(ack_pkt, addr)
                    except Exception as e:
                        log.debug(f"Heartbeat ACK send failed: {e}")

                packet_count += 1

                # How evenly are packets arriving? A steady stream means the network is
                # the problem, and a stream that arrives in clumps means the phone is
                # the problem. Those two need opposite fixes, so measure rather than guess.
                received_at = time.time()
                # Read defensively. This loop is also driven by tests that build an engine
                # without going through start(), so the counters may not exist yet. Using
                # getattr here rather than self._x keeps one missing field from throwing on
                # every single packet and silently discarding the whole stream.
                max_gap = getattr(self, "_max_packet_gap_ms", 0.0)
                big_gaps = getattr(self, "_big_gap_count", 0)
                previous_arrival = getattr(self, "_last_packet_arrival", None)
                if previous_arrival is not None:
                    gap_ms = (received_at - previous_arrival) * 1000.0
                    if gap_ms > max_gap:
                        max_gap = gap_ms
                    if gap_ms > 30.0:
                        big_gaps += 1
                self._max_packet_gap_ms = max_gap
                self._big_gap_count = big_gaps
                self._last_packet_arrival = received_at

                # Protocol v2 audio parsing:
                # [0x50, 0x4D, ver, 0x01, ch, seq_hi, seq_lo, samples_hi, samples_lo, ...PCM]
                if ptype == TYPE_AUDIO:
                    parsed = parse_audio_packet(data)
                    if parsed is None:
                        continue

                    seq = parsed['sequence']
                    raw_pcm = parsed['pcm']
                    is_stereo = parsed['is_stereo']
                    detected_mode = parsed['mode']

                    # Track packet loss and discard stale out-of-order packets
                    if expected_seq is not None:
                        diff = (seq - expected_seq) & 0xFFFF
                        if 0 < diff < 30000:
                            self.jitter_buffer.packets_lost += diff
                            if self.monitor_jitter_buffer is not None:
                                self.monitor_jitter_buffer.packets_lost += diff
                        elif diff > 30000:
                            # Stale / late out-of-order packet (drop to avoid phase glitch)
                            continue
                    expected_seq = (seq + 1) & 0xFFFF
                else:
                    # Legacy headerless packets: ONLY for datagrams that carry no magic bytes.
                    # A datagram that reached this point already passed the magic check, so this
                    # branch can never splice header bytes into the audio stream.
                    if len(data) >= LEGACY_STEREO_PACKET_SIZE:
                        is_stereo = True
                        raw_pcm = data[:LEGACY_STEREO_PACKET_SIZE]
                        detected_mode = "Dual-Mic Stereo (Legacy)"
                    elif len(data) >= LEGACY_MONO_PACKET_SIZE:
                        is_stereo = False
                        raw_pcm = data[:LEGACY_MONO_PACKET_SIZE]
                        detected_mode = "Single-Mic Mono (Legacy)"
                    elif len(data) >= MIN_LEGACY_PACKET_SIZE:
                        is_stereo = False
                        raw_pcm = data
                        detected_mode = "Single-Mic Mono (Legacy Short)"
                    else:
                        # Drop tiny packets to prevent control noise/clicks entering the pipeline.
                        log.debug(f"Dropped undersized datagram ({len(data)} B) from {addr[0]}")
                        continue

                try:
                    # While a PC microphone is the active source, this audio belongs to
                    # nobody. It is still received and still acknowledged, because the
                    # phone stops streaming if it stops hearing from us, but pushing it
                    # into the buffer would fight the PC mic for the same output stream.
                    if self.input_mode != "phone":
                        packet_count += 1
                        continue

                    processed_bytes = self._process_dsp(raw_pcm, is_stereo=is_stereo)

                    # Route to Adaptive Jitter Buffer for Virtual Audio Cable
                    self.jitter_buffer.write(processed_bytes)

                    # Route to Headphone Monitor if active
                    if self.is_monitoring and self.monitor_jitter_buffer is not None:
                        self.monitor_jitter_buffer.write(processed_bytes)

                    if packet_count % 50 == 0:
                        stats = self.jitter_buffer.get_stats()
                        status_detail = f"{detected_mode} (Buf: {stats['buffered_ms']}ms, Loss: {stats['packets_lost']})"
                        self.on_packet_received(packet_count, status_detail)

                        # Audio health, written down. A cut in the voice is either the
                        # buffer running dry (underrun) or packets going missing on the
                        # network, and those two have completely different fixes. Without
                        # this, the log only says audio arrived, which cannot explain a
                        # cut that is already happened.
                        now = time.time()
                        if now - getattr(self, "_last_audio_health_log", 0.0) >= 10.0:
                            self._last_audio_health_log = now
                            log.info(
                                "AUDIO HEALTH: buffered=%.1fms underruns=%d lost=%d "
                                "overruns=%d trimmed=%.1fms pkts=%d maxgap=%.0fms "
                                "gaps>30ms=%d",
                                stats["buffered_ms"], stats["underruns"],
                                stats["packets_lost"], stats["overruns"],
                                stats["trimmed_ms"], stats["packets_received"],
                                self._max_packet_gap_ms, self._big_gap_count,
                            )
                            self._max_packet_gap_ms = 0.0
                            self._big_gap_count = 0

                        # An underrun is silence pushed into the output. Log the moment it
                        # happens, with the buffer depth at that instant, because that is
                        # the exact cause of the audible cut.
                        if stats["underruns"] > getattr(self, "_last_reported_underruns", 0):
                            self._last_reported_underruns = stats["underruns"]
                            log.warning(
                                "AUDIO CUT (buffer ran dry): underrun #%d, buffer was "
                                "%.1fms, packets lost so far %d",
                                stats["underruns"], stats["buffered_ms"],
                                stats["packets_lost"],
                            )
                except Exception as dsp_err:
                    log.warning(f"Error processing packet DSP: {dsp_err}")

            except socket.timeout:
                now_t = time.time()
                # Check for 2-second phone disconnect / silence
                if is_phone_connected and (now_t - last_packet_time) > 2.0:
                    is_phone_connected = False
                    is_pairing_requested = False
                    # is_phone_approved survives: it is re-derived from the paired-phone record
                    # when this phone is seen again, so no repeat pairing prompt is needed.
                    log.info(f"Phone disconnected (silence timeout): {connected_sender}")
                    connected_sender = None
                    self.connected_phone_addr = None
                    expected_seq = None
                    self.jitter_buffer.reset()
                    if self.monitor_jitter_buffer is not None:
                        self.monitor_jitter_buffer.reset()
                    self.on_status_changed("Listening", "Phone Disconnected. Searching for phone on Wi-Fi...")
                    notify_event("phone_disconnected", "Phone disconnected", 400, 250)
                elif is_pairing_requested and (now_t - last_packet_time) > 2.0:
                    is_pairing_requested = False
                    connected_sender = None
                    self.connected_phone_addr = None
                    self.on_status_changed("Listening", f"Listening on UDP port {self.port}")
                continue
            except OSError:
                # Socket was closed cleanly by stop()
                break
            except Exception as e:
                log.warning(f"Transient UDP socket exception: {e}")
                continue

        if self.is_running and self.input_mode == "phone":
            self.stop()
            self.on_status_changed("Stopped", "UDP connection closed")


class PhoneDiscovery:
    """
    Background UDP discovery manager on port 8089.
    - Discovers Android devices on local Wi-Fi broadcasting 'PM_BEACON:<DeviceName>:<Port>'.
    - Tracks active devices with a 6-second heartbeat expiration.
    - Sends 'PM_CONNECT:<HostName>' to initiate pairing.
    - Sends 'PM_FORGET:' to forget device.
    - Receives 'PM_ACCEPT:<DeviceName>', 'PM_DECLINE:<DeviceName>', 'PM_FORGET:'.
    """
    def __init__(self, on_devices_changed, on_connection_response):
        self.on_devices_changed = on_devices_changed
        self.on_connection_response = on_connection_response  # (success: bool, dev_name: str, ip: str, port: int)
        self.devices = {}  # ip -> {'ip': str, 'name': str, 'port': int, 'last_seen': float, 'status': str}
        self.trusted_ip = None
        self.trusted_name = None
        self.sock = None
        self.is_running = False
        self.lock = threading.Lock()
        self._load_trusted_device()

    def _load_trusted_device(self):
        cfg_path = os.path.join(LOG_DIR, 'paired_phone.json')
        try:
            if os.path.exists(cfg_path):
                import json
                with open(cfg_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.trusted_ip = data.get('ip')
                    self.trusted_name = data.get('name')
        except Exception:
            pass

    def _save_trusted_device(self, ip, name):
        cfg_path = os.path.join(LOG_DIR, 'paired_phone.json')
        try:
            import json
            with open(cfg_path, 'w', encoding='utf-8') as f:
                json.dump({'ip': ip, 'name': name}, f)
            self.trusted_ip = ip
            self.trusted_name = name
        except Exception:
            pass

    def _clear_trusted_device(self):
        self.trusted_ip = None
        self.trusted_name = None
        cfg_path = os.path.join(LOG_DIR, 'paired_phone.json')
        try:
            if os.path.exists(cfg_path):
                os.remove(cfg_path)
        except Exception:
            pass

    def start(self):
        if self.is_running:
            return
        self.is_running = True
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.bind(('0.0.0.0', DISCOVERY_PORT))
            self.sock.settimeout(1.0)
            log.info(f"PhoneDiscovery bound to UDP port {DISCOVERY_PORT} successfully.")
        except Exception as e:
            log.error(f"Failed to bind discovery socket on port {DISCOVERY_PORT}: {e}")
            self.is_running = False
            return

        threading.Thread(target=self._listen_loop, daemon=True, name="DiscoveryListener").start()
        threading.Thread(target=self._expiration_loop, daemon=True, name="DiscoveryExpiration").start()

    def stop(self):
        self.is_running = False
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

    def connect_device(self, ip):
        with self.lock:
            if ip in self.devices:
                self.devices[ip]['status'] = 'Connecting...'
        self.on_devices_changed(self.get_device_list())
        try:
            pc_name = socket.gethostname()
            msg = f"PM_CONNECT:{pc_name}".encode('utf-8')
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.sendto(msg, (ip, DISCOVERY_PORT))
            s.close()
            log.info(f"Sent PM_CONNECT to {ip}:{DISCOVERY_PORT}")
        except Exception as e:
            log.warning(f"Error sending PM_CONNECT to {ip}: {e}")

    def forget_device(self, ip):
        try:
            msg = b"PM_FORGET:"
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.sendto(msg, (ip, DISCOVERY_PORT))
            s.close()
            log.info(f"Sent PM_FORGET to {ip}:{DISCOVERY_PORT}")
        except Exception:
            pass
        with self.lock:
            if ip in self.devices:
                self.devices[ip]['status'] = 'Available'
        self._clear_trusted_device()
        self.on_devices_changed(self.get_device_list())

    def disconnect_device(self, ip):
        with self.lock:
            if ip in self.devices:
                self.devices[ip]['status'] = 'Available'
        self.on_devices_changed(self.get_device_list())

    def get_device_list(self):
        with self.lock:
            return list(self.devices.values())

    def _listen_loop(self):
        while self.is_running and self.sock:
            try:
                data, addr = self.sock.recvfrom(2048)
                text = data.decode('utf-8', errors='ignore').strip()
                ip = addr[0]
                now = time.time()

                if text.startswith("PM_BEACON:"):
                    payload = text[len("PM_BEACON:"):]
                    parts = payload.rsplit(":", 1)
                    dev_name = parts[0] if parts else "Android Phone"
                    port = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else PORT_DEFAULT

                    updated = False
                    is_new = False
                    with self.lock:
                        if ip not in self.devices:
                            status = "Available"
                            self.devices[ip] = {
                                'ip': ip,
                                'name': dev_name,
                                'port': port,
                                'last_seen': now,
                                'status': status
                            }
                            updated = True
                            is_new = True
                        else:
                            self.devices[ip]['last_seen'] = now
                            if self.devices[ip]['name'] != dev_name:
                                self.devices[ip]['name'] = dev_name
                                updated = True

                    if is_new:
                        log.info(f"Discovered ZMic Studio beacon from {dev_name} ({ip}:{port})")
                        # Deliberately no auto-connect here. Connecting on discovery
                        # meant that opening the phone app started the microphone on
                        # the PC with nobody having pressed anything. The user starts
                        # the stream with the Start Audio Stream button, which asks the
                        # paired phone to send audio.

                    if updated:
                        self.on_devices_changed(self.get_device_list())

                elif text.startswith("PM_ACCEPT:"):
                    dev_name = text[len("PM_ACCEPT:"):].strip()
                    with self.lock:
                        if ip in self.devices:
                            self.devices[ip]['status'] = 'Connected'
                            if dev_name:
                                self.devices[ip]['name'] = dev_name
                        port = self.devices.get(ip, {}).get('port', PORT_DEFAULT)
                    self._save_trusted_device(ip, dev_name or "Android Phone")
                    self.on_devices_changed(self.get_device_list())
                    self.on_connection_response(True, dev_name or "Android Phone", ip, port)

                elif text.startswith("PM_DECLINE:"):
                    with self.lock:
                        if ip in self.devices:
                            self.devices[ip]['status'] = 'Available'
                    self.on_devices_changed(self.get_device_list())
                    self.on_connection_response(False, "Android Phone", ip, PORT_DEFAULT)

                elif text.startswith("PM_FORGET:"):
                    with self.lock:
                        if ip in self.devices:
                            self.devices[ip]['status'] = 'Available'
                    self._clear_trusted_device()
                    self.on_devices_changed(self.get_device_list())

            except socket.timeout:
                continue
            except OSError:
                break
            except Exception as e:
                log.warning(f"Discovery listener exception: {e}")

    def _expiration_loop(self):
        while self.is_running:
            time.sleep(1.5)
            now = time.time()
            expired_ips = []
            with self.lock:
                for ip, dev in list(self.devices.items()):
                    if now - dev['last_seen'] > 6.0:
                        expired_ips.append(ip)
                for ip in expired_ips:
                    del self.devices[ip]
            if expired_ips:
                self.on_devices_changed(self.get_device_list())


class NotebookAccessible(wx.Accessible):
    """Ensures screen readers (NVDA/JAWS) report exact tab count (e.g. 1 of 3) rather than counting internal HWND child panels."""
    def __init__(self, win):
        super().__init__(win)

    def GetChildCount(self):
        win = self.GetWindow()
        if win and hasattr(win, 'GetPageCount'):
            return (wx.ACC_OK, win.GetPageCount())
        return (wx.ACC_OK, 3)


class TermsWizard(wx.Dialog):
    """
    First run dialog. The user must read the Terms and Privacy and tick the checkbox
    before the Continue button becomes active.

    The dialog cannot be dismissed by closing it or pressing Escape. Leaving without
    ticking the box is treated as declining, because that is not agreement.
    """
    def __init__(self, parent=None):
        super().__init__(
            parent,
            title=f"{APP_NAME} - Terms and Privacy",
            style=wx.DEFAULT_DIALOG_STYLE
        )
        self.CentreOnScreen()
        self.SetBackgroundColour(wx.Colour(30, 30, 30))

        sizer = wx.BoxSizer(wx.VERTICAL)

        title_lbl = wx.StaticText(self, wx.ID_ANY, "Before you start")
        title_lbl.SetFont(wx.Font(12, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        title_lbl.SetForegroundColour(wx.Colour(255, 204, 0))
        sizer.Add(title_lbl, 0, wx.ALL, 14)

        subtitle = wx.StaticText(self, wx.ID_ANY, "Please read the Terms and Privacy below.")
        subtitle.SetForegroundColour(wx.Colour(230, 230, 230))
        sizer.Add(subtitle, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 14)

        # Scrollable read-only text box so every section is reachable with the keyboard.
        self.txt_terms = wx.TextCtrl(
            self,
            wx.ID_ANY,
            value=terms.get_terms_as_text(),
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP | wx.HSCROLL
        )
        self.txt_terms.SetBackgroundColour(wx.Colour(250, 250, 250))
        self.txt_terms.SetForegroundColour(wx.Colour(20, 20, 20))
        self.txt_terms.SetName("Terms and Privacy text. Use the arrow keys to scroll.")
        self.txt_terms.SetFocus()
        sizer.Add(self.txt_terms, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 14)

        # Checkbox starts UNCHECKED on purpose. A pre-ticked box is not a real agreement.
        self.chk_agree = wx.CheckBox(self, wx.ID_ANY, terms.TERMS_CHECKBOX_LABEL)
        self.chk_agree.SetName(terms.TERMS_CHECKBOX_LABEL)
        self.chk_agree.SetValue(False)
        self.chk_agree.SetForegroundColour(wx.Colour(230, 230, 230))
        self.chk_agree.Bind(wx.EVT_CHECKBOX, self.on_toggle_agree)
        sizer.Add(self.chk_agree, 0, wx.ALL, 14)

        self.btn_continue = wx.Button(self, wx.ID_OK, "&Continue to Z Mic Studio")
        self.btn_continue.SetName("Continue to Z Mic Studio")
        self.btn_continue.Enable(False)  # disabled until the box is ticked
        self.btn_continue.Bind(wx.EVT_BUTTON, self.on_continue)
        sizer.Add(self.btn_continue, 0, wx.ALIGN_RIGHT | wx.LEFT | wx.RIGHT | wx.BOTTOM, 14)

        self.SetSizerAndFit(sizer)
        self.SetSize(640, 560)
        self.CentreOnScreen()

        # Closing or Escape means "not agreed", so cancel is disabled on purpose.
        self.Bind(wx.EVT_CLOSE, self.on_declined)

        speak_accessibility(
            "Before you start. Please read the Terms and Privacy. "
            "When you have read them, tick the box that says you agree, "
            "then press Continue."
        )

    def on_toggle_agree(self, event):
        self.btn_continue.Enable(self.chk_agree.GetValue())

    def on_continue(self, event):
        if not self.chk_agree.GetValue():
            return
        terms.record_terms_accepted()
        log.info("Terms and Privacy accepted.")
        self.EndModal(wx.ID_OK)

    def on_declined(self, event):
        log.info("Terms dialog closed without agreement.")
        self.EndModal(wx.ID_CANCEL)


class TermsViewer(wx.Dialog):
    """Read only view of the Terms and Privacy, opened from the Help tab."""

    def __init__(self, parent=None):
        super().__init__(
            parent,
            title=f"{APP_NAME} - Terms and Privacy",
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER
        )
        self.CentreOnScreen()
        self.SetBackgroundColour(wx.Colour(30, 30, 30))

        sizer = wx.BoxSizer(wx.VERTICAL)

        heading = wx.StaticText(self, wx.ID_ANY, "Terms and Privacy")
        heading.SetFont(wx.Font(11, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        heading.SetForegroundColour(wx.Colour(255, 204, 0))
        sizer.Add(heading, 0, wx.ALL, 12)

        self.txt = wx.TextCtrl(
            self, wx.ID_ANY, value=terms.get_terms_as_text(),
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP | wx.HSCROLL
        )
        self.txt.SetBackgroundColour(wx.Colour(250, 250, 250))
        self.txt.SetForegroundColour(wx.Colour(20, 20, 20))
        self.txt.SetName("Terms and Privacy text. Use the arrow keys to scroll.")
        self.txt.SetFocus()
        sizer.Add(self.txt, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 12)

        btn_close = wx.Button(self, wx.ID_CANCEL, "&Close")
        btn_close.SetName("Close Terms and Privacy")
        sizer.Add(btn_close, 0, wx.ALIGN_RIGHT | wx.ALL, 12)

        self.SetSizerAndFit(sizer)
        self.SetSize(620, 520)
        self.CentreOnScreen()
        self.Bind(wx.EVT_BUTTON, lambda e: self.EndModal(wx.ID_CANCEL), id=wx.ID_CANCEL)


class VirtualCableRequiredGate(wx.Dialog):
    """
    Startup gate shown when VB-Audio Virtual Cable ('CABLE Input') is not installed.

    The main application interface is never built in this state. The user is offered exactly
    two actions: open the vendor's official download page in their default browser, or exit.

    ZMic Studio deliberately does NOT bundle or launch the VB-Audio installer. Launching it
    from inside this application would integrate the vendor package into another software's
    installation procedure, which the VB-CABLE licence does not permit. Instead the user
    downloads and installs it from the vendor's own site, which also matches the vendor's
    instruction to reboot after installation.
    """
    def __init__(self, parent=None):
        super().__init__(
            parent,
            title=f"{APP_NAME} - Virtual Audio Cable Required",
            style=wx.DEFAULT_DIALOG_STYLE | wx.STAY_ON_TOP
        )
        self.CentreOnScreen()
        self.SetBackgroundColour(wx.Colour(30, 30, 30))

        sizer = wx.BoxSizer(wx.VERTICAL)

        title_lbl = wx.StaticText(self, wx.ID_ANY, "Virtual Audio Cable Required")
        title_lbl.SetFont(wx.Font(11, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        title_lbl.SetForegroundColour(wx.Colour(255, 204, 0))
        sizer.Add(title_lbl, 0, wx.ALL, 14)

        msg = (
            "Z Mic Studio needs a virtual audio cable to appear as a microphone\n"
            "inside Windows applications such as Discord, OBS or Zoom.\n\n"
            "'CABLE Input (VB-Audio Virtual Cable)' was not found on this PC.\n\n"
            "Download and install it from the official VB-Audio website, then restart\n"
            "your computer, then open Z Mic Studio again."
        )
        msg_lbl = wx.StaticText(self, wx.ID_ANY, msg)
        msg_lbl.SetForegroundColour(wx.Colour(230, 230, 230))
        msg_lbl.SetName(msg)
        sizer.Add(msg_lbl, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 14)

        sizer.Add(wx.StaticLine(self), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 14)

        note = (
            "Z Mic Studio will close now. Installing the driver requires a computer restart,\n"
            "so it must be reopened afterwards."
        )
        note_lbl = wx.StaticText(self, wx.ID_ANY, note)
        note_lbl.SetForegroundColour(wx.Colour(180, 180, 180))
        note_lbl.SetName(note)
        sizer.Add(note_lbl, 0, wx.ALL, 14)

        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)

        self.btn_download = wx.Button(self, wx.ID_ANY, "&Download Virtual Cable")
        self.btn_download.SetName("Download Virtual Cable. Opens the official VB-Audio website in your default browser.")
        self.btn_download.SetDefault()
        self.btn_download.Bind(wx.EVT_BUTTON, self.on_download)
        btn_sizer.Add(self.btn_download, 0, wx.RIGHT, 10)

        self.btn_exit = wx.Button(self, wx.ID_CANCEL, "&Exit")
        self.btn_exit.SetName("Exit Z Mic Studio")
        self.btn_exit.Bind(wx.EVT_BUTTON, self.on_exit)
        btn_sizer.Add(self.btn_exit, 0, 0)

        sizer.Add(btn_sizer, 0, wx.ALIGN_RIGHT | wx.ALL, 14)

        self.SetSizerAndFit(sizer)
        self.Bind(wx.EVT_CLOSE, self.on_exit)

        speak_accessibility(
            "Virtual Audio Cable is required. Press Enter to open the official VB-Audio download "
            "page in your default browser, or press Escape to exit. Z Mic Studio will close, and "
            "must be reopened after your computer restarts."
        )

    def on_download(self, event):
        """Opens the vendor page in the default browser, then closes the application."""
        log.info(f"Opening VB-Cable download page in default browser: {VB_CABLE_DOWNLOAD_URL}")
        opened = False
        try:
            import webbrowser
            opened = webbrowser.open(VB_CABLE_DOWNLOAD_URL, new=2)
        except Exception as e:
            log.error(f"Could not open the default browser: {e}", exc_info=True)

        if not opened:
            wx.MessageBox(
                f"Your browser could not be opened automatically.\n\n"
                f"Please open this address manually:\n\n{VB_CABLE_DOWNLOAD_URL}",
                "Open Manually",
                wx.OK | wx.ICON_INFORMATION,
                parent=self
            )
        self.EndModal(wx.ID_OK)

    def on_exit(self, event):
        self.EndModal(wx.ID_CANCEL)


class ZMicStudioFrame(wx.Frame):
    """Main window with accessible Tabbed interface."""
    def __init__(self):
        super().__init__(
            parent=None,
            id=wx.ID_ANY,
            title=f"{APP_NAME} {APP_VERSION_DISPLAY} - Virtual Studio Microphone",
            size=(640, 660),
            style=wx.DEFAULT_FRAME_STYLE
        )

        self.engine = AudioEngine(self.on_engine_status, self.on_engine_packets)
        self.engine.on_remote_mute = self._on_remote_mute_received
        self.devices, self.default_cable_idx = get_output_devices()
        if self.default_cable_idx != -1 and self.devices:
            self.target_cable_id = self.devices[self.default_cable_idx][0]
            self.target_cable_name = self.devices[self.default_cable_idx][1]
        else:
            self.target_cable_id = 0
            self.target_cable_name = "CABLE Input (Not detected)"
        self.input_devices = get_input_devices()
        self.discovered_devices_list = []
        # True when the current input was picked by the user rather than chosen for them.
        # Connecting a phone must not drag a deliberately chosen PC microphone off the list,
        # but when the phone drops and the app falls back to a PC microphone by itself, the
        # choice was not the user's and capture should return to the phone on reconnect.
        self._user_picked_input = False
        # Set while the app moves the picker itself, so the change handler can tell an
        # automatic switch apart from the user choosing something.
        self._suppress_input_pick_mark = False
        self.discovery = PhoneDiscovery(self._on_discovery_device_list, self._on_discovery_connection_response)

        self._build_ui()
        self._setup_accelerators()
        self._setup_global_hotkeys()
        self.Bind(wx.EVT_CLOSE, self.on_close)
        self.Bind(wx.EVT_CHAR_HOOK, self.on_char_hook)

        # Start Wi-Fi discovery daemon
        self.discovery.start()

        # Streaming waits for the user to press Start Audio Stream on the Stream tab.
        # Starting it on launch gave the user no control over when the microphone opened.

    def _setup_global_hotkeys(self):
        """Registers system-wide global hotkey Ctrl+Alt+M via Win32 RegisterHotKey."""
        self.id_global_hotkey_mute = 10001
        try:
            ok = self.RegisterHotKey(self.id_global_hotkey_mute, wx.MOD_CONTROL | wx.MOD_ALT, ord('M'))
            if ok:
                self.Bind(wx.EVT_HOTKEY, self.on_toggle_mute, id=self.id_global_hotkey_mute)
                log.info("System-wide Win32 global hotkey Ctrl+Alt+M registered successfully.")
            else:
                log.warning("Could not register system-wide global hotkey Ctrl+Alt+M.")
        except Exception as e:
            log.warning(f"Exception registering system-wide global hotkey Ctrl+Alt+M: {e}")

    def _setup_accelerators(self):
        """Sets up global hotkeys that work from any tab or focused control."""
        self.id_hotkey_stream = wx.NewIdRef()
        self.id_hotkey_mute = wx.NewIdRef()
        self.id_hotkey_input = wx.NewIdRef()
        self.id_hotkey_noise = wx.NewIdRef()
        self.id_hotkey_rumble = wx.NewIdRef()
        self.id_hotkey_echo = wx.NewIdRef()
        self.id_hotkey_pingpong = wx.NewIdRef()
        self.id_hotkey_monitor = wx.NewIdRef()
        self.id_hotkey_about = wx.NewIdRef()
        self.id_tab_stream = wx.NewIdRef()
        self.id_tab_devices = wx.NewIdRef()
        self.id_tab_effects = wx.NewIdRef()
        self.id_tab_help = wx.NewIdRef()
        self.id_tab_next = wx.NewIdRef()
        self.id_tab_prev = wx.NewIdRef()

        entries = [
            wx.AcceleratorEntry(wx.ACCEL_ALT, ord('R'), self.id_hotkey_stream.GetId()),
            wx.AcceleratorEntry(wx.ACCEL_ALT, ord('r'), self.id_hotkey_stream.GetId()),
            wx.AcceleratorEntry(wx.ACCEL_ALT, ord('M'), self.id_hotkey_mute.GetId()),
            wx.AcceleratorEntry(wx.ACCEL_ALT, ord('m'), self.id_hotkey_mute.GetId()),
            wx.AcceleratorEntry(wx.ACCEL_ALT, ord('I'), self.id_hotkey_input.GetId()),
            wx.AcceleratorEntry(wx.ACCEL_ALT, ord('i'), self.id_hotkey_input.GetId()),
            wx.AcceleratorEntry(wx.ACCEL_ALT, ord('N'), self.id_hotkey_noise.GetId()),
            wx.AcceleratorEntry(wx.ACCEL_ALT, ord('n'), self.id_hotkey_noise.GetId()),
            wx.AcceleratorEntry(wx.ACCEL_ALT, ord('B'), self.id_hotkey_rumble.GetId()),
            wx.AcceleratorEntry(wx.ACCEL_ALT, ord('b'), self.id_hotkey_rumble.GetId()),
            wx.AcceleratorEntry(wx.ACCEL_ALT, ord('E'), self.id_hotkey_echo.GetId()),
            wx.AcceleratorEntry(wx.ACCEL_ALT, ord('e'), self.id_hotkey_echo.GetId()),
            wx.AcceleratorEntry(wx.ACCEL_ALT, ord('P'), self.id_hotkey_pingpong.GetId()),
            wx.AcceleratorEntry(wx.ACCEL_ALT, ord('p'), self.id_hotkey_pingpong.GetId()),
            wx.AcceleratorEntry(wx.ACCEL_ALT, ord('H'), self.id_hotkey_monitor.GetId()),
            wx.AcceleratorEntry(wx.ACCEL_ALT, ord('h'), self.id_hotkey_monitor.GetId()),
            wx.AcceleratorEntry(wx.ACCEL_ALT, ord('A'), self.id_hotkey_about.GetId()),
            wx.AcceleratorEntry(wx.ACCEL_ALT, ord('a'), self.id_hotkey_about.GetId()),
            wx.AcceleratorEntry(wx.ACCEL_CTRL, ord('1'), self.id_tab_stream.GetId()),
            wx.AcceleratorEntry(wx.ACCEL_CTRL, ord('2'), self.id_tab_devices.GetId()),
            wx.AcceleratorEntry(wx.ACCEL_CTRL, ord('3'), self.id_tab_effects.GetId()),
            wx.AcceleratorEntry(wx.ACCEL_CTRL, ord('4'), self.id_tab_help.GetId()),
            # Ctrl+Tab moves to the next tab. Registered explicitly because wx.Notebook does not
            # provide it reliably, and the Help tab documents it.
            wx.AcceleratorEntry(wx.ACCEL_CTRL, wx.WXK_TAB, self.id_tab_next.GetId()),
            wx.AcceleratorEntry(wx.ACCEL_CTRL | wx.ACCEL_SHIFT, wx.WXK_TAB, self.id_tab_prev.GetId()),
        ]
        self.SetAcceleratorTable(wx.AcceleratorTable(entries))

        self.Bind(wx.EVT_MENU, self.on_toggle_stream, id=self.id_hotkey_stream.GetId())
        self.Bind(wx.EVT_MENU, self.on_toggle_mute, id=self.id_hotkey_mute.GetId())
        self.Bind(wx.EVT_MENU, self.on_hotkey_input, id=self.id_hotkey_input.GetId())
        self.Bind(wx.EVT_MENU, self.on_hotkey_spectral_noise, id=self.id_hotkey_noise.GetId())
        self.Bind(wx.EVT_MENU, self.on_hotkey_rumble_cut, id=self.id_hotkey_rumble.GetId())
        self.Bind(wx.EVT_MENU, self.on_hotkey_echo, id=self.id_hotkey_echo.GetId())
        self.Bind(wx.EVT_MENU, self.on_hotkey_pingpong, id=self.id_hotkey_pingpong.GetId())
        self.Bind(wx.EVT_MENU, self.on_hotkey_monitor, id=self.id_hotkey_monitor.GetId())
        self.Bind(wx.EVT_MENU, self.on_about, id=self.id_hotkey_about.GetId())
        self.Bind(wx.EVT_MENU, lambda e: self.notebook.SetSelection(0), id=self.id_tab_stream.GetId())
        self.Bind(wx.EVT_MENU, lambda e: self.notebook.SetSelection(1), id=self.id_tab_devices.GetId())
        self.Bind(wx.EVT_MENU, lambda e: self.notebook.SetSelection(2), id=self.id_tab_effects.GetId())
        self.Bind(wx.EVT_MENU, lambda e: self.notebook.SetSelection(3), id=self.id_tab_help.GetId())
        self.Bind(wx.EVT_MENU, self.on_next_tab, id=self.id_tab_next.GetId())
        self.Bind(wx.EVT_MENU, self.on_previous_tab, id=self.id_tab_prev.GetId())

    def on_next_tab(self, event=None):
        """Ctrl+Tab: move to the next tab, wrapping around at the end."""
        count = self.notebook.GetPageCount()
        self.notebook.SetSelection((self.notebook.GetSelection() + 1) % count)

    def on_previous_tab(self, event=None):
        """Ctrl+Shift+Tab: move to the previous tab, wrapping around at the start."""
        count = self.notebook.GetPageCount()
        self.notebook.SetSelection((self.notebook.GetSelection() - 1) % count)

    def on_char_hook(self, event):
        """Global key hook. Enter on a button must activate it, for screen reader users."""
        key = event.GetKeyCode()
        focus = wx.Window.FindFocus()
        if focus and isinstance(focus, wx.Button) and key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            cmd = wx.CommandEvent(wx.EVT_BUTTON.typeId, focus.GetId())
            cmd.SetEventObject(focus)
            focus.GetEventHandler().ProcessEvent(cmd)
            return
        # Sliders are left alone here so that Up and Down keep adjusting the value.
        # Screen reader users expect Up to mean increase and Down to mean decrease
        # on any slider, not only on vertical ones.
        event.Skip()

    def _make_button_accessible(self, btn, callback):
        """Ensures buttons respond to standard click/activation cleanly without duplicate triggers."""
        btn.Bind(wx.EVT_BUTTON, callback)

    def _make_slider_accessible(self, slider, on_change):
        """
        Sliders answer to Left and Right, and also to Up and Down.

        wxWidgets only handles Left and Right on a horizontal slider, so Up and Down are
        implemented here. A screen reader user expects Up to mean larger and Down to mean
        smaller on any slider, and the arrow keys must not fall through to the notebook
        and switch tabs.
        """
        slider.Bind(wx.EVT_SLIDER, on_change)

        def on_slider_key(evt):
            key = evt.GetKeyCode()
            if key not in (wx.WXK_UP, wx.WXK_DOWN):
                evt.Skip()
                return

            step = slider.GetLineSize()
            current = slider.GetValue()
            new_value = current + step if key == wx.WXK_UP else current - step
            new_value = max(slider.GetMin(), min(slider.GetMax(), new_value))

            if new_value != current:
                slider.SetValue(new_value)
                slider.SetFocus()
                on_change(wx.CommandEvent(wx.EVT_SLIDER.typeId, slider.GetId()))
            # Always consumed: Up and Down must never reach the notebook.

        slider.Bind(wx.EVT_KEY_DOWN, on_slider_key)

    def _show_stopped_status(self, reason=None):
        """
        Puts the Stream tab into its waiting state.

        Used when the engine is not running, so the buttons and the status line always
        match what the engine is actually doing.
        """
        self.btn_stream.SetLabel("Start Audio Stream (Alt+&R)")
        self.btn_stream.SetName("Start Audio Stream (Alt+R)")
        self.btn_mute.Disable()
        if reason:
            self.lbl_status.SetLabel(f"Status: {reason}")
        else:
            self.lbl_status.SetLabel(
                "Status: Not streaming. Press Start Audio Stream on this tab to begin."
            )

    def _build_ui(self):
        panel = wx.Panel(self)
        main_sizer = wx.BoxSizer(wx.VERTICAL)

        # Tabbed Notebook
        self.notebook = wx.Notebook(panel, wx.ID_ANY)
        if hasattr(wx, 'Accessible'):
            try:
                self.notebook.SetAccessible(NotebookAccessible(self.notebook))
            except Exception:
                pass

        self.tab_stream = self._create_stream_tab(self.notebook)
        self.tab_devices = self._create_devices_tab(self.notebook)
        self.tab_effects = self._create_effects_tab(self.notebook)
        self.tab_help = self._create_help_tab(self.notebook)
        self.tab_about = self._create_about_tab(self.notebook)

        self.notebook.AddPage(self.tab_stream, "&Stream")
        self.notebook.AddPage(self.tab_devices, "&Devices")
        self.notebook.AddPage(self.tab_effects, "Audio &Effects")
        self.notebook.AddPage(self.tab_help, "&Help and Shortcuts")
        self.notebook.AddPage(self.tab_about, "A&bout")

        main_sizer.Add(self.notebook, 1, wx.EXPAND | wx.ALL, 10)
        panel.SetSizer(main_sizer)

        self.Centre()

    def _create_stream_tab(self, parent):
        page = wx.Panel(parent)
        sizer = wx.BoxSizer(wx.VERTICAL)

        # 1. Status comes FIRST. It is the one thing a user always wants to know, and a
        #    screen reader should not have to pass several controls to reach it.
        self.lbl_status = wx.StaticText(
            page, wx.ID_ANY,
            "Status: Not streaming. Press Start Audio Stream on this tab to begin."
        )
        self.lbl_status.SetFont(self.lbl_status.GetFont().Bold())
        self.lbl_status.SetName("Status")
        sizer.Add(self.lbl_status, 0, wx.EXPAND | wx.ALL, 10)

        sizer.Add(wx.StaticLine(page), 0, wx.EXPAND | wx.TOP | wx.BOTTOM, 8)

        # 2. Main actions
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_stream = wx.Button(page, wx.ID_ANY, "Start Audio Stream (Alt+&R)")
        self.btn_stream.SetName("Start Audio Stream (Alt+R)")
        self.btn_mute = wx.Button(page, wx.ID_ANY, "Mute Microphone (Ctrl+Alt+&M)")
        self.btn_mute.SetName("Mute Microphone (Ctrl+Alt+M)")
        self.btn_mute.Disable()

        self._make_button_accessible(self.btn_stream, self.on_toggle_stream)
        self._make_button_accessible(self.btn_mute, self.on_toggle_mute)

        btn_sizer.Add(self.btn_stream, 0, wx.RIGHT, 10)
        btn_sizer.Add(self.btn_mute, 0)
        sizer.Add(btn_sizer, 0, wx.ALL, 8)

        # 2. Mic Volume Slider directly in Stream tab for instant accessibility
        self.lbl_volume = wx.StaticText(page, wx.ID_ANY, "Mic Volume: 100%")
        sizer.Add(self.lbl_volume, 0, wx.LEFT | wx.TOP, 8)
        self.sld_volume = wx.Slider(page, wx.ID_ANY, value=100, minValue=0, maxValue=100, style=wx.SL_HORIZONTAL)
        self.sld_volume.SetName("Mic Volume Slider, 100 percent")
        self.sld_volume.SetLineSize(5)
        self.sld_volume.SetPageSize(10)
        self._make_slider_accessible(self.sld_volume, self.on_volume_changed)
        sizer.Add(self.sld_volume, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        sizer.Add(wx.StaticLine(page), 0, wx.EXPAND | wx.TOP | wx.BOTTOM, 8)

        # 3. Details. Only values that actually change while running are shown here.
        #    The old tab repeated the input device, output sink and channel mode, but those
        #    already appear as controls on the Devices and Effects tabs, so a screen reader
        #    had to hear the same fact several times.
        lbl_details_head = wx.StaticText(page, wx.ID_ANY, "Details")
        lbl_details_head.SetFont(lbl_details_head.GetFont().Bold())
        lbl_details_head.SetName("Details")
        sizer.Add(lbl_details_head, 0, wx.LEFT | wx.TOP, 8)

        self.lbl_mode = wx.StaticText(page, wx.ID_ANY, "Stream activity: Waiting for phone")
        self.lbl_mode.SetName("Stream activity: Waiting for phone")
        sizer.Add(self.lbl_mode, 0, wx.LEFT | wx.RIGHT, 8)

        self.lbl_packets = wx.StaticText(page, wx.ID_ANY, "Audio frames sent: 0")
        self.lbl_packets.SetName("Audio frames sent: 0")
        sizer.Add(self.lbl_packets, 0, wx.ALL, 8)

        # 4. Notification Voice Mode. This belongs with streaming rather than with devices,
        #    because it is a preference a user changes while a stream is running.
        sizer.Add(wx.StaticLine(page), 0, wx.EXPAND | wx.TOP | wx.BOTTOM, 8)

        lbl_notify_head = wx.StaticText(page, wx.ID_ANY, "Notification Voice Mode:")
        lbl_notify_head.SetFont(lbl_notify_head.GetFont().Bold())
        sizer.Add(lbl_notify_head, 0, wx.LEFT | wx.TOP, 6)

        self.cmb_voice_mode = wx.Choice(page, wx.ID_ANY, choices=[
            "Natural Voice",
            "NVDA Screen Reader Only",
            "Beeps / Tone Cues Only"
        ])
        self.cmb_voice_mode.SetName(
            "Notification Voice Mode. Choose between Natural Voice, NVDA Screen Reader, or Beeps."
        )
        self.cmb_voice_mode.SetSelection(0)
        self.cmb_voice_mode.Bind(wx.EVT_CHOICE, self.on_voice_mode_changed)
        sizer.Add(self.cmb_voice_mode, 0, wx.EXPAND | wx.ALL, 6)

        page.SetSizer(sizer)
        return page

    def _create_devices_tab(self, parent):
        page = wx.Panel(parent)
        sizer = wx.BoxSizer(wx.VERTICAL)

        # 1. Phone Microphones (Wi-Fi Auto-Discovery)
        lbl_dev_list = wx.StaticText(page, wx.ID_ANY, "Available Phone Microphones (Press Enter to connect, Applications key for options):")
        lbl_dev_list.SetFont(lbl_dev_list.GetFont().Bold())
        sizer.Add(lbl_dev_list, 0, wx.ALL, 6)

        self.lst_phones = wx.ListBox(page, wx.ID_ANY, size=(-1, 110), style=wx.LB_SINGLE)
        self.lst_phones.SetName("Available Phone Microphones. Press Enter to connect, or Applications key for options")
        self.lst_phones.Append("Searching for ZMic Studio on local Wi-Fi...")
        self.lst_phones.Bind(wx.EVT_LISTBOX, self._on_phone_list_select)
        self.lst_phones.Bind(wx.EVT_LISTBOX_DCLICK, self.on_connect_phone)
        self.lst_phones.Bind(wx.EVT_KEY_DOWN, self.on_phone_list_key_down)
        self.lst_phones.Bind(wx.EVT_CONTEXT_MENU, self.on_phone_context_menu)
        sizer.Add(self.lst_phones, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        # Action Buttons for Discovered Phones
        dev_btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_connect_phone = wx.Button(page, wx.ID_ANY, "Connect Phone (&Enter)")
        self.btn_connect_phone.SetName("Connect Phone (Enter)")
        self.btn_disconnect_phone = wx.Button(page, wx.ID_ANY, "&Disconnect Phone")
        self.btn_disconnect_phone.SetName("Disconnect Phone")
        self.btn_forget_phone = wx.Button(page, wx.ID_ANY, "&Forget Device")
        self.btn_forget_phone.SetName("Forget Device")

        self.btn_connect_phone.Disable()
        self.btn_disconnect_phone.Disable()
        self.btn_forget_phone.Disable()

        self._make_button_accessible(self.btn_connect_phone, self.on_connect_phone)
        self._make_button_accessible(self.btn_disconnect_phone, self.on_disconnect_phone)
        self._make_button_accessible(self.btn_forget_phone, self.on_forget_phone)

        dev_btn_sizer.Add(self.btn_connect_phone, 0, wx.RIGHT, 8)
        dev_btn_sizer.Add(self.btn_disconnect_phone, 0, wx.RIGHT, 8)
        dev_btn_sizer.Add(self.btn_forget_phone, 0)
        sizer.Add(dev_btn_sizer, 0, wx.ALL, 6)

        sizer.Add(wx.StaticLine(page), 0, wx.EXPAND | wx.TOP | wx.BOTTOM, 8)

        # 2. Audio Hardware Devices (Microphone Input & Output Sink)
        lbl_dev_head = wx.StaticText(page, wx.ID_ANY, "Audio Hardware Devices:")
        lbl_dev_head.SetFont(lbl_dev_head.GetFont().Bold())
        sizer.Add(lbl_dev_head, 0, wx.LEFT | wx.TOP, 6)

        # Microphone Input Source Selector (Alt+I)
        sizer.Add(wx.StaticText(page, wx.ID_ANY, "Audio Input Device (Microphone) (Alt+&I):"), 0, wx.LEFT | wx.TOP, 6)
        self.cmb_input_source = wx.Choice(page, wx.ID_ANY, choices=[])
        self.cmb_input_source.SetName("Audio Input Device (Microphone) (Alt+I)")
        self.cmb_input_source.Bind(wx.EVT_CHOICE, self.on_input_source_changed)
        sizer.Add(self.cmb_input_source, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        # Explains the list, because an empty list on its own is not an explanation.
        self.lbl_input_hint = wx.StaticText(page, wx.ID_ANY, "")
        self.lbl_input_hint.SetForegroundColour(wx.Colour(90, 90, 90))
        sizer.Add(self.lbl_input_hint, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        self._rebuild_input_sources()

        # Virtual Audio Cable Status (Background Auto-bound)
        if self.default_cable_idx != -1 and self.devices:
            status_cable = f"Virtual Audio Output: Ready ({self.target_cable_name})"
        else:
            status_cable = "Virtual Audio Output: 'CABLE Input' not detected (Install VB-Audio Cable)"

        self.lbl_cable_status = wx.StaticText(page, wx.ID_ANY, status_cable)
        self.lbl_cable_status.SetName(status_cable)
        sizer.Add(self.lbl_cable_status, 0, wx.ALL, 6)

        # Feedback-risk opt-in. Physical speakers route the mic straight back into the room.
        self.chk_allow_speakers = wx.CheckBox(page, wx.ID_ANY, "Allow Physical Speakers (feedback risk)")
        self.chk_allow_speakers.SetName(
            "Allow Physical Speakers. Warning: this can create an echoing feedback loop."
        )
        self.chk_allow_speakers.SetValue(False)
        self.chk_allow_speakers.SetToolTip(
            "By default ZMic Studio only outputs to a virtual cable such as CABLE Input.\n"
            "Enable this only if you understand that using real speakers creates feedback."
        )
        self.chk_allow_speakers.Bind(wx.EVT_CHECKBOX, self.on_allow_speakers_changed)
        sizer.Add(self.chk_allow_speakers, 0, wx.ALL, 6)

        page.SetSizer(sizer)
        return page

    def _create_effects_tab(self, parent):
        page = wx.Panel(parent)
        sizer = wx.BoxSizer(wx.VERTICAL)

        # -------------------------------------------------------------
        # Section 1: Continuous Noise Reduction & Clean Filter (Alt+N)
        # -------------------------------------------------------------
        lbl_noise_head = wx.StaticText(page, wx.ID_ANY, "Continuous Noise Reduction & Clean Filter:")
        lbl_noise_head.SetFont(lbl_noise_head.GetFont().Bold())
        sizer.Add(lbl_noise_head, 0, wx.LEFT | wx.TOP, 6)

        self.chk_spectral_noise = wx.CheckBox(page, wx.ID_ANY, "Enable Continuous Noise Reduction (Alt+&N)")
        self.chk_spectral_noise.SetName("Enable Continuous Noise Reduction (Alt+N)")
        self.chk_spectral_noise.SetValue(False)
        self.chk_spectral_noise.Bind(wx.EVT_CHECKBOX, self.on_spectral_noise_changed)
        sizer.Add(self.chk_spectral_noise, 0, wx.LEFT | wx.TOP | wx.BOTTOM, 4)

        self.lbl_noise_reduction = wx.StaticText(page, wx.ID_ANY, "Noise Reduction Intensity: 40% (Balanced)")
        sizer.Add(self.lbl_noise_reduction, 0, wx.LEFT | wx.TOP, 4)
        self.sld_noise_reduction = wx.Slider(page, wx.ID_ANY, value=40, minValue=0, maxValue=100, style=wx.SL_HORIZONTAL)
        self.sld_noise_reduction.SetName("Noise Reduction Intensity Slider, 40 percent")
        self.sld_noise_reduction.SetLineSize(5)
        self.sld_noise_reduction.SetPageSize(10)
        self._make_slider_accessible(self.sld_noise_reduction, self.on_spectral_noise_changed)
        sizer.Add(self.sld_noise_reduction, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        # The rumble cut gets its own switch so the voice can be compared with it on and off,
        # which is the only honest way to judge a filter change by ear.
        self.chk_rumble_cut = wx.CheckBox(page, wx.ID_ANY, "Cut Rumble Below 80Hz (Alt+&B)")
        self.chk_rumble_cut.SetName("Cut Rumble Below 80Hz, Alt+B")
        self.chk_rumble_cut.SetValue(True)
        self.chk_rumble_cut.SetToolTip(
            "Removes low fan hum, air conditioning rumble and handling noise.\n"
            "Turn this off to hear the completely unfiltered voice for comparison."
        )
        self.chk_rumble_cut.Bind(wx.EVT_CHECKBOX, self.on_rumble_cut_changed)
        sizer.Add(self.chk_rumble_cut, 0, wx.LEFT | wx.RIGHT, 4)

        self.lbl_rumble_cut = wx.StaticText(page, wx.ID_ANY, "Rumble cut 80Hz: On")
        sizer.Add(self.lbl_rumble_cut, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 4)

        sizer.Add(wx.StaticLine(page), 0, wx.EXPAND | wx.TOP | wx.BOTTOM, 8)

        # -------------------------------------------------------------
        # Section 2: Channel Routing Mode (Dual-Mic Spatial Panning)
        # -------------------------------------------------------------
        lbl_mode_head = wx.StaticText(page, wx.ID_ANY, "Channel Routing Mode (Dual-Mic Spatial Panning):")
        lbl_mode_head.SetFont(lbl_mode_head.GetFont().Bold())
        sizer.Add(lbl_mode_head, 0, wx.LEFT | wx.TOP, 6)

        self.rb_stereo = wx.RadioButton(page, wx.ID_ANY, "Stereo 3D (Dual Mic Spatial Panning)", style=wx.RB_GROUP)
        self.rb_mono = wx.RadioButton(page, wx.ID_ANY, "Center Mono (Balanced Left and Right)")
        self.rb_left = wx.RadioButton(page, wx.ID_ANY, "Left Mic Only")
        self.rb_right = wx.RadioButton(page, wx.ID_ANY, "Right Mic Only")
        self.rb_stereo.SetValue(True)

        sizer.Add(self.rb_stereo, 0, wx.LEFT | wx.TOP | wx.BOTTOM, 3)
        sizer.Add(self.rb_mono, 0, wx.LEFT | wx.BOTTOM, 3)
        sizer.Add(self.rb_left, 0, wx.LEFT | wx.BOTTOM, 3)
        sizer.Add(self.rb_right, 0, wx.LEFT | wx.BOTTOM, 3)

        self.lbl_mode_status = wx.StaticText(page, wx.ID_ANY, "Selected Mode: Stereo 3D (Dual Mic Spatial Panning)")
        self.lbl_mode_status.SetFont(self.lbl_mode_status.GetFont().Bold())
        sizer.Add(self.lbl_mode_status, 0, wx.LEFT | wx.BOTTOM, 6)

        radio_items = [
            (self.rb_stereo, "Stereo 3D"),
            (self.rb_mono, "Center Mono"),
            (self.rb_left, "Left Mic"),
            (self.rb_right, "Right Mic"),
        ]
        for idx, (rb, name) in enumerate(radio_items):
            rb.Bind(wx.EVT_RADIOBUTTON, lambda evt, m=name, btn=rb: self._set_channel_mode(m, btn))
            rb.Bind(wx.EVT_SET_FOCUS, lambda evt, m=name, btn=rb: (self._set_channel_mode(m, btn), evt.Skip()))

            def make_radio_nav(cur_i):
                def on_radio_key(evt):
                    key = evt.GetKeyCode()
                    if key in (wx.WXK_DOWN, wx.WXK_RIGHT):
                        next_i = (cur_i + 1) % len(radio_items)
                        target_rb, target_name = radio_items[next_i]
                        target_rb.SetFocus()
                        self._set_channel_mode(target_name, target_rb)
                        return
                    elif key in (wx.WXK_UP, wx.WXK_LEFT):
                        prev_i = (cur_i - 1) % len(radio_items)
                        target_rb, target_name = radio_items[prev_i]
                        target_rb.SetFocus()
                        self._set_channel_mode(target_name, target_rb)
                        return
                    evt.Skip()
                return on_radio_key
            rb.Bind(wx.EVT_KEY_DOWN, make_radio_nav(idx))

        self.lbl_stereo_width = wx.StaticText(page, wx.ID_ANY, "Stereo 3D Separation Width: 3.5x")
        sizer.Add(self.lbl_stereo_width, 0, wx.LEFT | wx.TOP, 4)
        self.sld_stereo_width = wx.Slider(page, wx.ID_ANY, value=35, minValue=10, maxValue=60, style=wx.SL_HORIZONTAL)
        self.sld_stereo_width.SetName("Stereo 3D Separation Width Slider, 3.5x")
        self.sld_stereo_width.SetLineSize(2)
        self.sld_stereo_width.SetPageSize(5)
        self._make_slider_accessible(self.sld_stereo_width, self.on_stereo_width_changed)
        sizer.Add(self.sld_stereo_width, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        sizer.Add(wx.StaticLine(page), 0, wx.EXPAND | wx.TOP | wx.BOTTOM, 8)

        # -------------------------------------------------------------
        # Section 3: Studio Echo & Ping-Pong Delay
        # -------------------------------------------------------------
        lbl_echo_head = wx.StaticText(page, wx.ID_ANY, "Studio Echo & Ping-Pong Delay:")
        lbl_echo_head.SetFont(lbl_echo_head.GetFont().Bold())
        sizer.Add(lbl_echo_head, 0, wx.LEFT | wx.TOP, 6)

        self.chk_echo = wx.CheckBox(page, wx.ID_ANY, "Enable Studio Echo (Alt+&E)")
        self.chk_echo.SetName("Enable Studio Echo (Alt+E)")
        self.chk_echo.Bind(wx.EVT_CHECKBOX, self.on_echo_changed)
        sizer.Add(self.chk_echo, 0, wx.LEFT | wx.TOP | wx.BOTTOM, 3)

        self.chk_pingpong = wx.CheckBox(page, wx.ID_ANY, "Ping-Pong Stereo Bounce (Alt+&P)")
        self.chk_pingpong.SetName("Ping-Pong Stereo Bounce (Alt+P)")
        self.chk_pingpong.Bind(wx.EVT_CHECKBOX, self.on_pingpong_changed)
        sizer.Add(self.chk_pingpong, 0, wx.LEFT | wx.BOTTOM, 4)

        self.lbl_echo_delay = wx.StaticText(page, wx.ID_ANY, "Echo Delay Time: 240 ms")
        sizer.Add(self.lbl_echo_delay, 0, wx.LEFT | wx.TOP, 4)
        self.sld_echo_delay = wx.Slider(page, wx.ID_ANY, value=240, minValue=100, maxValue=600, style=wx.SL_HORIZONTAL)
        self.sld_echo_delay.SetName("Echo Delay Time Slider, 240 ms")
        self.sld_echo_delay.SetLineSize(10)
        self.sld_echo_delay.SetPageSize(50)
        self._make_slider_accessible(self.sld_echo_delay, self.on_echo_changed)
        sizer.Add(self.sld_echo_delay, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 4)

        self.lbl_echo_feedback = wx.StaticText(page, wx.ID_ANY, "Echo Feedback Intensity: 40%")
        sizer.Add(self.lbl_echo_feedback, 0, wx.LEFT | wx.TOP, 4)
        self.sld_echo_feedback = wx.Slider(page, wx.ID_ANY, value=40, minValue=0, maxValue=100, style=wx.SL_HORIZONTAL)
        self.sld_echo_feedback.SetName("Echo Feedback Intensity Slider, 40 percent")
        self.sld_echo_feedback.SetLineSize(5)
        self.sld_echo_feedback.SetPageSize(10)
        self._make_slider_accessible(self.sld_echo_feedback, self.on_echo_changed)
        sizer.Add(self.sld_echo_feedback, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)

        sizer.Add(wx.StaticLine(page), 0, wx.EXPAND | wx.TOP | wx.BOTTOM, 8)

        # -------------------------------------------------------------
        # Section 4: Hear Myself in Headphones (Monitoring)
        # -------------------------------------------------------------
        self.chk_monitor = wx.CheckBox(page, wx.ID_ANY, "Hear Myself in Headphones (Alt+&H)")
        self.chk_monitor.SetName("Hear Myself in Headphones (Alt+H)")
        self.chk_monitor.Bind(wx.EVT_CHECKBOX, self.on_monitor_changed)
        sizer.Add(self.chk_monitor, 0, wx.ALL, 6)

        page.SetSizer(sizer)
        return page

    def _create_about_tab(self, parent):
        """
        Builds the About tab.

        This is a tab of its own rather than a button on the Help tab, because a button
        buried at the bottom of a tab is easy to never find. The version number and the
        support address are the two things a user needs most when something has gone
        wrong, so they are a single tab press away.

        Everything here is readable text, not a dialog, so a screen reader can read the
        whole page top to bottom without any hunting.
        """
        page = wx.Panel(parent)
        sizer = wx.BoxSizer(wx.VERTICAL)

        # Read only so it cannot be typed into, but focusable so a screen reader can put the
        # caret in it and read the text with the arrow keys. Not focusable is why NVDA
        # never announced this tab's content.
        #
        # The body is a read only TextCtrl, not a set of StaticText labels. NVDA skipped the
        # labels on this tab and read only the buttons, while the Help tab, which uses a
        # TextCtrl for the very same purpose, reads perfectly. So the control type
        # matters here, not just the wording.
        self.txt_about = wx.TextCtrl(
            page, wx.ID_ANY,
            value=(
                f"{APP_NAME}\n\n"
                f"Version {APP_VERSION_DISPLAY}\n\n"
                "Created and designed by Zameer.\n\n"
                "Z Mic Studio turns your Android phone into a studio-grade microphone "
                "for your Windows computer, over your local Wi-Fi network.\n\n"
                "ACCESSIBILITY\n\n"
                "This app was built screen-reader first. It works with NVDA on Windows "
                "and TalkBack on Android.\n\n"
                "FEEDBACK AND SUPPORT\n\n"
                "Email: zameer.sstr@gmail.com\n\n"
                "Open your email app and write that address there yourself.\n\n"
                "This app cannot send anything by itself. Nothing about you or your "
                "computer is sent anywhere."
            ),
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP | wx.HSCROLL
        )
        self.txt_about.SetBackgroundColour(wx.Colour(250, 250, 250))
        self.txt_about.SetForegroundColour(wx.Colour(20, 20, 20))
        # Deliberately no SetName. A custom accessible name replaces what the screen
        # reader reads out, so naming it would stop the reader reaching the text at all.
        sizer.Add(self.txt_about, 1, wx.EXPAND | wx.ALL, 12)

        # Terms stay reachable from here too, so a user who came for the version has
        # not to go back to another tab to read them.
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_about_terms = wx.Button(page, wx.ID_ANY, "View &Terms and Privacy")
        self.btn_about_terms.SetName("View Terms and Privacy")
        self._make_button_accessible(self.btn_about_terms, self.on_view_terms)
        btn_sizer.Add(self.btn_about_terms, 0)

        self.btn_about_help = wx.Button(page, wx.ID_ANY, "Go to &Help")
        self.btn_about_help.SetName("Go to the Help tab")
        self.btn_about_help.Bind(wx.EVT_BUTTON, self.on_go_to_help_tab)
        btn_sizer.Add(self.btn_about_help, 0, wx.LEFT, 10)

        sizer.Add(btn_sizer, 0, wx.ALL, 12)

        page.SetSizer(sizer)
        return page

    def on_go_to_help_tab(self, event=None):
        """Moves to the Help tab, so the jump back is a button rather than a hunt."""
        for index in range(self.notebook.GetPageCount()):
            if self.notebook.GetPage(index) is self.tab_help:
                self.notebook.SetSelection(index)
                self.notebook.SetFocus()
                return

    def _create_help_tab(self, parent):
        page = wx.Panel(parent)
        sizer = wx.BoxSizer(wx.VERTICAL)

        # Wording rules for this tab:
        # 1. Plain English, short sentences.
        # 2. No bullet or star characters. A screen reader reads each bullet aloud,
        #    which breaks the rhythm of the sentence and adds noise.
        # 3. One idea per line, with a blank line between sections.
        #
        # The text is split into sections and only one is shown at a time. Reading a
        # single long block through a screen reader is tiring, and most people only
        # need one part of it.
        self.HELP_SECTIONS = [
            (
                "Getting Started",
                "Step 1. On your computer, install VB-Audio Virtual Cable.\n"
                "You can get it from www.vb-cable.com\n"
                "After installing it, restart your computer.\n"
                "\n"
                "Step 2. Open Z Mic Studio on your phone.\n"
                "\n"
                "Step 3. Go to the Devices tab.\n"
                "\n"
                "Step 4. Your phone will appear in the list after a few seconds.\n"
                "Move to it and press Enter to connect.\n"
                "\n"
                "Step 5. Go to the Stream tab and check the status at the top."
            ),
            (
                "If You Cannot Hear Anything",
                "Check these one at a time.\n"
                "\n"
                "First, open the Stream tab and read the status at the top.\n"
                "\n"
                "Second, open Discord, OBS or Zoom and choose CABLE Output\n"
                "as the microphone. Do not choose CABLE Input.\n"
                "\n"
                "Third, make sure the microphone is not muted.\n"
                "Press Alt plus M to check.\n"
                "\n"
                "Fourth, check that your phone and computer are on the same\n"
                "Wi-Fi network.\n"
                "\n"
                "Fifth, press the Troubleshooting Log button below and read\n"
                "the last few lines. That will tell you what went wrong."
            ),
            (
                "If Your Phone Does Not Appear",
                "Check these one at a time.\n"
                "\n"
                "First, make sure the Z Mic Studio app is open on your phone.\n"
                "\n"
                "Second, check that your phone and computer are on the same\n"
                "Wi-Fi network.\n"
                "\n"
                "Third, some Wi-Fi networks stop devices from finding each\n"
                "other. A normal home Wi-Fi usually works."
            ),
            (
                "Keyboard Shortcuts",
                "Moving between tabs.\n"
                "Ctrl plus Tab goes to the next tab.\n"
                "Ctrl plus Shift plus Tab goes to the previous tab.\n"
                "Ctrl plus 1 goes to the Stream tab.\n"
                "Ctrl plus 2 goes to the Devices tab.\n"
                "Ctrl plus 3 goes to the Audio Effects tab.\n"
                "Ctrl plus 4 goes to the Help tab.\n"
                "\n"
                "Main actions. These work from any tab.\n"
                "Alt plus R starts or stops the audio stream.\n"
                "Alt plus M mutes or unmutes the microphone.\n"
                "Alt plus I goes to the audio input selector.\n"
                "Alt plus N turns noise reduction on or off.\n"
                "Alt plus E turns studio echo on or off.\n"
                "Alt plus P turns ping pong delay on or off.\n"
                "Alt plus H turns headphone monitoring on or off.\n"
                "Alt plus A opens the About window.\n"
                "\n"
                "Global shortcut.\n"
                "Ctrl plus Alt plus M mutes or unmutes the microphone from\n"
                "any program on your computer, even when Z Mic Studio is not\n"
                "in front.\n"
                "\n"
                "Working with buttons and boxes.\n"
                "Enter presses the button you are on.\n"
                "Space ticks or unticks the box you are on.\n"
                "\n"
                "Working with a slider.\n"
                "Right arrow and Up arrow make the value bigger.\n"
                "Left arrow and Down arrow make the value smaller.\n"
                "Page Up and Page Down change it in bigger steps.\n"
                "\n"
                "Working with the channel mode buttons.\n"
                "Up arrow and Down arrow move between them and also change\n"
                "the channel mode."
            ),
            (
                "Your Privacy",
                "Your audio only travels between your phone and your computer.\n"
                "It is never sent to a website or to a server.\n"
                "\n"
                "This app has no account and no advertising.\n"
                "There is no tracking and no analytics.\n"
                "\n"
                "Once a week the app checks whether a new version is ready.\n"
                "That check does not send anything about you or your computer.\n"
                "\n"
                "Your settings and log files stay on your computer only.\n"
                "\n"
                "You can read the full Terms and Privacy with the button below."
            ),
            (
                "About Z Mic Studio",
                f"Version {APP_VERSION_DISPLAY}\n"
                "Created and designed by Zameer\n"
                "\n"
                "Z Mic Studio turns your phone into a microphone for your computer.\n"
                "\n"
                "It is made to work with screen readers such as NVDA, JAWS and\n"
                "Windows Narrator, and with Google TalkBack on Android."
            ),
        ]

        lbl_help_head = wx.StaticText(page, wx.ID_ANY, "Help")
        lbl_help_head.SetFont(lbl_help_head.GetFont().Bold())
        lbl_help_head.SetName("Help")
        sizer.Add(lbl_help_head, 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)

        # Section picker
        self.cmb_help_section = wx.Choice(page, wx.ID_ANY,
                                          choices=[t for t, _ in self.HELP_SECTIONS])
        self.cmb_help_section.SetName("Help topic")
        self.cmb_help_section.SetSelection(0)
        self.cmb_help_section.Bind(wx.EVT_CHOICE, self.on_help_section_changed)
        sizer.Add(self.cmb_help_section, 0, wx.EXPAND | wx.ALL, 10)

        self.lbl_help_title = wx.StaticText(page, wx.ID_ANY, self.HELP_SECTIONS[0][0])
        self.lbl_help_title.SetFont(self.lbl_help_title.GetFont().Bold())
        self.lbl_help_title.SetName(self.HELP_SECTIONS[0][0])
        sizer.Add(self.lbl_help_title, 0, wx.LEFT | wx.RIGHT, 10)

        self.txt_help = wx.TextCtrl(
            page, wx.ID_ANY, value=self.HELP_SECTIONS[0][1],
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP | wx.HSCROLL
        )
        self.txt_help.SetBackgroundColour(wx.Colour(250, 250, 250))
        self.txt_help.SetForegroundColour(wx.Colour(20, 20, 20))
        # The accessible name is reset to the topic title once the tab is built.
        sizer.Add(self.txt_help, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        # Previous / Next let a screen reader user step through one topic at a time.
        nav_sizer = wx.BoxSizer(wx.HORIZONTAL)

        self.btn_help_prev = wx.Button(page, wx.ID_ANY, "&Previous Topic")
        self.btn_help_prev.SetName("Previous Help Topic")
        self.btn_help_prev.Bind(wx.EVT_BUTTON, self.on_help_prev)
        nav_sizer.Add(self.btn_help_prev, 0, wx.RIGHT, 10)

        self.btn_help_next = wx.Button(page, wx.ID_ANY, "&Next Topic")
        self.btn_help_next.SetName("Next Help Topic")
        self.btn_help_next.Bind(wx.EVT_BUTTON, self.on_help_next)
        nav_sizer.Add(self.btn_help_next, 0, wx.RIGHT, 10)

        sizer.Add(nav_sizer, 0, wx.ALL, 10)

        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)

        # Terms: always reachable, not only on the first run.
        self.btn_view_terms = wx.Button(page, wx.ID_ANY, "View &Terms and Privacy")
        self.btn_view_terms.SetName("View Terms and Privacy")
        self._make_button_accessible(self.btn_view_terms, self.on_view_terms)
        btn_sizer.Add(self.btn_view_terms, 0, wx.RIGHT, 10)

        # About has its own tab now, so there is deliberately no About button here.
        # Two routes to the same screen is one more thing to decide between, and the tab
        # is always visible in the tab strip.

        # The log is only needed when something is not working, so it is offered as a
        # troubleshooting aid rather than as a general feature.
        self.btn_open_log = wx.Button(page, wx.ID_ANY, "Open &Troubleshooting Log")
        self.btn_open_log.SetName("Open Troubleshooting Log")
        self.btn_open_log.SetToolTip(f"Opens the Z Mic Studio diagnostic log:\n{LOG_FILE}")
        self._make_button_accessible(self.btn_open_log, self.on_open_log)
        btn_sizer.Add(self.btn_open_log, 0)

        sizer.Add(btn_sizer, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        self.lbl_log_path = wx.StaticText(
            page, wx.ID_ANY,
            "The troubleshooting log records what the app did on this computer. "
            "It is only useful if something is not working."
        )
        self.lbl_log_path.SetForegroundColour(wx.Colour(150, 150, 150))
        self.lbl_log_path.SetName(self.lbl_log_path.GetLabel())
        sizer.Add(self.lbl_log_path, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        page.SetSizer(sizer)
        # Built quietly: nothing is spoken while the app is opening.
        self.on_help_section_changed(None, announce=False)
        return page

    def on_help_section_changed(self, event, announce=True, force_speak=False):
        """
        Shows the chosen help topic and keeps the Previous and Next buttons in step.

        speak defaults to False so the app stays quiet when the tab is built, and so
        moving through the picker with Up and Down is not read out twice. A screen
        reader already announces the item the reader moved to, which made every key
        press say the title twice.

        force_speak is set by the Previous and Next buttons. Pressing a button is
        silent to a screen reader, so those name the topic they moved to.
        """
        index = self.cmb_help_section.GetSelection()
        if index == wx.NOT_FOUND:
            return
        title, body = self.HELP_SECTIONS[index]
        self.lbl_help_title.SetLabel(title)
        self.txt_help.SetValue(body)
        # The name is kept to the topic title on purpose. A long instruction sentence here
        # is read out every single time focus lands on the help, which drowns the help
        # itself. The control text is what the reader scrolls through.
        self.txt_help.SetName(title)
        self.txt_help.SetInsertionPoint(0)

        count = len(self.HELP_SECTIONS)
        self.btn_help_prev.Enable(index > 0)
        self.btn_help_next.Enable(index < count - 1)

        # Spoken only when the Previous or Next buttons asked for it. See the docstring.
        if announce and force_speak:
            speak_accessibility(f"Showing help topic: {title}")

    def on_help_next(self, event=None):
        index = self.cmb_help_section.GetSelection()
        if index != wx.NOT_FOUND and index < len(self.HELP_SECTIONS) - 1:
            self.cmb_help_section.SetSelection(index + 1)
            # Announced, because pressing a button is silent to a screen reader.
            self.on_help_section_changed(None, announce=True, force_speak=True)

    def on_help_prev(self, event=None):
        index = self.cmb_help_section.GetSelection()
        if index != wx.NOT_FOUND and index > 0:
            self.cmb_help_section.SetSelection(index - 1)
            # Announced, because pressing a button is silent to a screen reader.
            self.on_help_section_changed(None, announce=True, force_speak=True)

    def on_view_terms(self, event=None):
        """Opens the read only Terms and Privacy window."""
        log.info("User opened the Terms and Privacy window.")
        dlg = TermsViewer(self)
        dlg.ShowModal()
        dlg.Destroy()
        speak_accessibility("Terms and Privacy window closed.")


    def _set_channel_mode(self, mode_name, btn):
        """Updates DSP channel mode and synchronizes all UI indicators."""
        if not btn.GetValue():
            btn.SetValue(True)
        self.engine.set_mode(mode_name)
        mode_full_names = {
            "Stereo 3D": "Stereo 3D (Dual Mic Spatial Panning)",
            "Center Mono": "Center Mono (Balanced Left and Right)",
            "Left Mic": "Left Mic Only",
            "Right Mic": "Right Mic Only",
        }
        full = mode_full_names.get(mode_name, mode_name)
        self.lbl_mode_status.SetLabel(f"Selected Mode: {full}")
        log.info(f"Channel routing mode set to: {full}")

    # Event Handlers
    def on_phone_list_key_down(self, event):
        key = event.GetKeyCode()
        if key in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER):
            self.on_connect_phone(event)
        else:
            event.Skip()

    def on_phone_context_menu(self, event):
        """Application key, Shift+F10, or right-click context menu for selected phone."""
        if not self.discovered_devices_list:
            return
        sel = self.lst_phones.GetSelection()
        if sel == wx.NOT_FOUND or sel >= len(self.discovered_devices_list):
            return

        dev = self.discovered_devices_list[sel]
        status = dev.get('status', 'Available')
        is_connected = (status.lower() == 'connected')

        menu = wx.Menu()
        item_connect = menu.Append(wx.ID_ANY, "Connect (Enter)")
        item_disconnect = menu.Append(wx.ID_ANY, "Disconnect")
        item_forget = menu.Append(wx.ID_ANY, "Forget Device")

        if is_connected:
            item_connect.Enable(False)
            item_disconnect.Enable(True)
            item_forget.Enable(True)
        else:
            item_connect.Enable(True)
            item_disconnect.Enable(False)
            item_forget.Enable(False)

        self.Bind(wx.EVT_MENU, self.on_connect_phone, item_connect)
        self.Bind(wx.EVT_MENU, self.on_disconnect_phone, item_disconnect)
        self.Bind(wx.EVT_MENU, self.on_forget_phone, item_forget)

        self.PopupMenu(menu)
        menu.Destroy()

    def _on_phone_list_select(self, event=None):
        self._update_device_button_states()
        if event:
            event.Skip()

    def _connected_phone_name(self):
        """Returns the name of the phone currently connected, or None."""
        for dev in self.discovered_devices_list:
            if dev.get('status', '').lower() == 'connected':
                return dev.get('name')
        return None

    def _rebuild_input_sources(self):
        """
        Rebuilds the microphone list.

        The phone only appears once a phone is actually connected. Listing it
        permanently made it look like a phone was available when none was, so the user
        could pick it and press Start with nothing on the other end.
        """
        if not hasattr(self, 'cmb_input_source') or self.cmb_input_source is None:
            return

        previous = None
        sel = self.cmb_input_source.GetSelection()
        if sel != wx.NOT_FOUND and sel < len(self.input_source_items):
            previous = self.input_source_items[sel]

        self.input_source_items = []
        phone_name = self._connected_phone_name()
        if phone_name:
            self.input_source_items.append(("phone", None, f"Phone: {phone_name}"))
        for dev_id, name in self.input_devices:
            self.input_source_items.append(("pc", dev_id, name))

        if not self.input_source_items:
            # Nothing to capture from yet. Show a placeholder so the control is not empty.
            self.cmb_input_source.Clear()
            self.cmb_input_source.Append("No input device available")
            self.cmb_input_source.SetSelection(0)
            self.cmb_input_source.Disable()
            self.lbl_input_hint.SetLabel(self._input_hint_text())
            self.lbl_input_hint.SetName(self.lbl_input_hint.GetLabel())
            log.warning("No input sources available: no connected phone and no PC microphones.")
            return

        self.cmb_input_source.Enable()
        self.cmb_input_source.Clear()
        for _, _, label in self.input_source_items:
            self.cmb_input_source.Append(label)

        # Keep the user's choice across a rebuild where possible.
        target = 0
        if previous is not None:
            for i, item in enumerate(self.input_source_items):
                if item[0] == previous[0] and item[1] == previous[1]:
                    target = i
                    break

        # When the phone vanishes from the list, the selection lands on a PC microphone
        # without the user asking. That fallback is not a choice, so it must not be treated
        # as one, otherwise reconnecting would leave capture stuck on the PC microphone.
        if previous is not None and previous[0] == "phone" and self.input_source_items:
            if self.input_source_items[target][0] == "pc":
                self._user_picked_input = False

        self.cmb_input_source.SetSelection(target)
        self.lbl_input_hint.SetLabel(self._input_hint_text())
        self.lbl_input_hint.SetName(self.lbl_input_hint.GetLabel())

    def _refresh_input_hint(self):
        if getattr(self, 'lbl_input_hint', None) is not None:
            self.lbl_input_hint.SetLabel(self._input_hint_text())
            self.lbl_input_hint.SetName(self.lbl_input_hint.GetLabel())

    def _input_hint_text(self):
        if not self.input_source_items:
            return "No microphone is available. Connect a phone on the Phones tab, " \
                   "or plug in a USB microphone, then this list will fill up."
        if self._connected_phone_name() is None:
            return "Your phone does not appear here until it is connected. " \
                   "Open the Phones tab to connect it."
        return "This is what the stream will capture from."

    def _update_device_button_states(self):
        """Update Connect, Disconnect, and Forget button states based on selection and device status."""
        if not self.discovered_devices_list:
            self.btn_connect_phone.Disable()
            self.btn_disconnect_phone.Disable()
            self.btn_forget_phone.Disable()
            return

        sel = self.lst_phones.GetSelection()
        if sel == wx.NOT_FOUND or sel >= len(self.discovered_devices_list):
            self.btn_connect_phone.Disable()
            self.btn_disconnect_phone.Disable()
            self.btn_forget_phone.Disable()
            return

        dev = self.discovered_devices_list[sel]
        status = dev.get('status', 'Available')

        if status.lower() == 'connected':
            self.btn_connect_phone.Disable()
            self.btn_disconnect_phone.Enable()
            self.btn_forget_phone.Enable()
        elif status.lower() == 'connecting...':
            self.btn_connect_phone.Disable()
            self.btn_disconnect_phone.Disable()
            self.btn_forget_phone.Disable()
        else:
            self.btn_connect_phone.Enable()
            self.btn_disconnect_phone.Disable()
            self.btn_forget_phone.Disable()

    def on_connect_phone(self, event=None):
        if not self.discovered_devices_list:
            return
        sel = self.lst_phones.GetSelection()
        if sel == wx.NOT_FOUND or sel >= len(self.discovered_devices_list):
            sel = 0
        dev = self.discovered_devices_list[sel]
        ip = dev['ip']
        name = dev['name']
        self.lbl_status.SetLabel(f"Status: Connecting to {name} ({ip})...")
        speak_accessibility(f"Connecting to {name}")
        self.discovery.connect_device(ip)
        self._update_device_button_states()

    def on_disconnect_phone(self, event=None):
        if not self.discovered_devices_list:
            return
        sel = self.lst_phones.GetSelection()
        if sel == wx.NOT_FOUND or sel >= len(self.discovered_devices_list):
            sel = 0
        dev = self.discovered_devices_list[sel]
        ip = dev['ip']
        name = dev['name']
        self.discovery.disconnect_device(ip)
        if self.engine.is_running:
            # Only tear the stream down if the phone was the thing being captured. A user on
            # a PC microphone lost their whole stream because a phone they were not using
            # disconnected.
            if self.engine.input_mode == "phone":
                self.engine.stop()
                self.btn_stream.SetLabel("Start Audio Stream (Alt+&R)")
                self.btn_stream.SetName("Start Audio Stream (Alt+R)")
                self.btn_mute.Disable()
            else:
                self.engine.release_phone_link()
        self.lbl_status.SetLabel(f"Status: Disconnected from {name}")
        if self.engine.is_running:
            self.lbl_status.SetLabel(
                f"Status: Disconnected from {name} - still capturing from "
                f"{self.engine.input_device_name()}")
        notify_event("phone_disconnected", f"Disconnected from {name}", 400, 200)
        # The phone row has to go away now, or the list offers a phone that is not there.
        self._rebuild_input_sources()
        self._update_device_button_states()

    def on_forget_phone(self, event=None):
        if not self.discovered_devices_list:
            return
        sel = self.lst_phones.GetSelection()
        if sel == wx.NOT_FOUND or sel >= len(self.discovered_devices_list):
            sel = 0
        dev = self.discovered_devices_list[sel]
        ip = dev['ip']
        name = dev['name']
        self.discovery.forget_device(ip)
        if self.engine.is_running:
            self.engine.stop()
            self.btn_stream.SetLabel("Start Audio Stream (Alt+&R)")
            self.btn_stream.SetName("Start Audio Stream (Alt+R)")
            self.btn_mute.Disable()
        self.lbl_status.SetLabel(f"Status: Forgot device {name}")
        notify_event("phone_forgotten", f"Device {name} forgotten", 400, 200)
        self._update_device_button_states()

    def _on_discovery_device_list(self, devices):
        wx.CallAfter(self._update_discovery_ui, devices)

    def _update_discovery_ui(self, devices):
        try:
            sel_idx = self.lst_phones.GetSelection()
            sel_ip = None
            if sel_idx != wx.NOT_FOUND and sel_idx < len(self.discovered_devices_list):
                sel_ip = self.discovered_devices_list[sel_idx].get('ip')

            self.discovered_devices_list = devices
            self.lst_phones.Clear()
            # The phone joins the microphone list only once connected, so rebuild here
            # rather than at build time.
            self._rebuild_input_sources()
            self._follow_phone_back_to_phone_input()

            if not devices:
                self.lst_phones.Append("Searching for ZMic Studio on local Wi-Fi...")
                self._update_device_button_states()
            else:
                new_sel = 0
                for i, dev in enumerate(devices):
                    status_text = dev.get('status', 'Available')
                    item_label = f"{dev['name']} ({dev['ip']}) - {status_text}"
                    self.lst_phones.Append(item_label)
                    if dev['ip'] == sel_ip:
                        new_sel = i
                self.lst_phones.SetSelection(new_sel)
                self._update_device_button_states()

                # If devices found and status is in ready/searching/waiting state, prompt user
                cur_status = self.lbl_status.GetLabel().lower()
                if "waiting" in cur_status or "searching" in cur_status or "ready" in cur_status:
                    if len(devices) == 1:
                        self.lbl_status.SetLabel(f"Status: Discovered {devices[0]['name']} ({devices[0]['ip']}). Press Enter or click Connect Phone.")
                    else:
                        self.lbl_status.SetLabel(f"Status: {len(devices)} ZMic device(s) available. Select and press Enter to connect.")
        except Exception:
            pass

    def _on_discovery_connection_response(self, success, name, ip, port):
        wx.CallAfter(self._handle_discovery_response, success, name, ip, port)

    def _handle_discovery_response(self, success, name, ip, port):
        if success:
            log.info(f"Phone '{name}' ({ip}:{port}) accepted connection.")
            # Remember the pairing so the UDP receiver auto-approves this phone next time
            # instead of asking for permission again on every session.
            self.engine.set_trusted_phone_ip(ip)

            # What does the picker actually say the user wants? Reading it here is what keeps the
            # running input and the list from disagreeing. Forcing the phone while the list
            # showed a USB microphone is what put the phone's voice out of a USB mic.
            #
            # The switch has to be explicit because engine.start() returns early when the
            # engine is already running. The audio receiver thread only runs in phone mode,
            # so without this the PC never acknowledges the phone and the phone reports
            # "PC not responding" right after saying connected.
            sel_index = self.cmb_input_source.GetSelection()
            selected = None
            if sel_index != wx.NOT_FOUND and sel_index < len(self.input_source_items):
                selected = self.input_source_items[sel_index]

            user_wants_pc = (selected is not None and selected[0] == "pc"
                             and self._user_picked_input)

            if self.engine.is_running and user_wants_pc:
                # Keep the chosen microphone, but still answer the phone so it stays alive
                # and can be picked the moment the user wants it.
                held = self.engine.hold_phone_link()
                ok, msg = True, ("Keeping the selected PC microphone"
                                 if held else "PC microphone kept, phone link unavailable")
                log.info(f"Phone connected while {selected[2]} was selected by the user. "
                         f"Keeping it, and keeping the phone link open.")
            elif self.engine.is_running:
                swapped_ok, swap_msg = self.engine.set_input_source("phone", None)
                if not swapped_ok:
                    log.error(f"Could not switch input to the phone: {swap_msg}")
                    self.lbl_status.SetLabel(f"Status Error: {swap_msg}")
                    play_audio_cue(400, 250)
                    speak_accessibility(f"Could not switch input to the phone: {swap_msg}")
                    self._update_device_button_states()
                    return
                ok, msg = True, swap_msg
            elif user_wants_pc:
                # Nothing is running yet and the user picked a PC microphone, so that is
                # what gets started. Starting the phone here instead is what made the voice
                # come from the phone while the list showed a USB microphone.
                self.engine.input_mode = "pc"
                self.engine.input_device_id = selected[1]
                ok, msg = self.engine.start(port, self.target_cable_id)
                if ok:
                    self.engine.hold_phone_link()
                log.info(f"Starting on {selected[2]}, the microphone the user selected.")
            else:
                self.engine.input_mode = "phone"
                self.engine.input_device_id = None
                ok, msg = self.engine.start(port, self.target_cable_id)

            if ok:
                self.btn_stream.SetLabel("Stop Audio Stream (Alt+&R)")
                self.btn_stream.SetName("Stop Audio Stream (Alt+R)")
                self.btn_mute.Enable()
                # Set the label only after the engine has started, otherwise start()'s own
                # status callback overwrites it and the user never sees the confirmation.
                if user_wants_pc:
                    self.lbl_status.SetLabel(f"Status: {name} connected - still using "
                                             f"{self.engine.input_device_name()}")
                    log.info(f"{name} is connected and selectable. Capturing from "
                             f"{self.engine.input_device_name()}.")
                else:
                    self.lbl_status.SetLabel(f"Status: Connected to {name} ({ip}) - Streaming Active")
                    log.info(f"Active input device is now {name} over Wi-Fi.")
                    notify_event("phone_connected", f"Connected to {name}", 1000, 150)
                # The phone only appears in the picker once a rebuild has run, so the list and
                # the running input must be refreshed together or they disagree.
                self._rebuild_input_sources()
            else:
                self.lbl_status.SetLabel(f"Status Error: {msg}")
                play_audio_cue(400, 250)
                speak_accessibility(f"Stream error: {msg}")
            self._update_device_button_states()
        else:
            log.warning(f"Connection declined by phone '{name}' ({ip}).")
            self.lbl_status.SetLabel(f"Status: Connection declined by {name}")
            notify_event("phone_declined", "Connection declined by phone", 400, 250)
            self._update_device_button_states()

    def _follow_phone_back_to_phone_input(self):
        """
        Puts the input back on the phone when the phone reconnects.

        Without this, losing the phone leaves the input sitting on a USB microphone. When
        the phone comes back it appears in the list but is not selected, so the stream
        silently keeps capturing the wrong device and the user has to notice and switch by
        hand.

        Only done while the stream is running and while a PC microphone is selected, so it
        never interrupts a stream the user set up deliberately, and never overrides a
        choice they are making at that moment.

        A PC microphone the user picked themselves is left alone even here. The list saying
        one thing while the voice comes from another is worse than having to switch by hand.
        """
        if not self.engine.is_running:
            return
        if self._user_picked_input:
            return
        sel = self.cmb_input_source.GetSelection()
        if sel == wx.NOT_FOUND or sel >= len(self.input_source_items):
            return
        if self.input_source_items[sel][0] != "pc":
            return
        if not any(item[0] == "phone" for item in self.input_source_items):
            return

        # Being seen on the network is not the same as connected. Switching onto a phone
        # that is not ready is what made the phone drop the connection, so wait until the
        # phone has actually accepted.
        if not any(
            dev.get('status', '').lower() == 'connected'
            for dev in self.discovered_devices_list
        ):
            return

        for index, item in enumerate(self.input_source_items):
            if item[0] == "phone":
                log.info("Phone is connected and the stream was on a PC microphone; "
                         "switching the input back to the phone.")
                self.cmb_input_source.SetSelection(index)
                self._suppress_input_pick_mark = True
                try:
                    self.on_input_source_changed(None)
                finally:
                    self._suppress_input_pick_mark = False
                return

    def _request_phone_audio_if_needed(self, src_type, from_input_switch=False):
        """
        Asks a phone to start sending, when the phone is the chosen input.

        Switching the input only makes the PC listen. Without this the phone never sends,
        the PC never acknowledges, and the phone reports that the PC is not responding,
        which is what made the phone disconnect when the input was switched by hand.

        Does nothing for a PC microphone, and nothing when a phone is already connected.
        Nothing when a request is already in flight either: asking a second time made the
        phone see a fresh pairing request while the first was still being accepted, and it
        dropped the connection instead of starting to send.
        """
        if src_type != "phone":
            return

        for dev in self.discovered_devices_list:
            if dev.get('status', '').lower() == 'connected':
                return

        if time.time() - getattr(self, "_last_connect_request_at", 0.0) < 8.0:
            log.info("Not asking again: a connect request is already on its way.")
            return

        for dev in self.discovered_devices_list:
            ip = dev.get('ip')
            if not ip:
                continue
            self._last_connect_request_at = time.time()
            reason = "Input switched to the phone" if from_input_switch else "Start pressed"
            log.info(f"{reason}: requesting audio from {dev.get('name')} ({ip}).")
            self.discovery.connect_device(ip)
            self.lbl_status.SetLabel(f"Status: Connecting to {dev.get('name')}...")
            return

        log.info("Phone chosen as input, but no phone was found on the network.")
        self.lbl_status.SetLabel(
            "Status: No phone found. Open the Phones tab and press Connect Phone, then try again."
        )

    def on_toggle_stream(self, event):
        if not self.engine.is_running:
            # Defensive only: the startup gate already blocked launch without a cable.
            if self.default_cable_idx == -1:
                self.lbl_status.SetLabel("Status: Virtual audio cable lost. Please restart Z Mic Studio.")
                play_audio_cue(400, 250)
                speak_accessibility("Virtual audio cable was lost. Please restart Z Mic Studio.")
                return

            sel_in = self.cmb_input_source.GetSelection()
            src_label = "the selected input"
            if sel_in != wx.NOT_FOUND and sel_in < len(self.input_source_items):
                src_type, dev_id, src_label = self.input_source_items[sel_in]
                self.engine.input_mode = src_type
                self.engine.input_device_id = dev_id
            else:
                # The list is empty or the selection is stale, so there is nothing to
                # capture from. Say so instead of starting a stream that captures silence.
                self._show_stopped_status("No input device. Connect a phone or plug in a microphone.")
                speak_accessibility(
                    "No input device is available. Connect a phone or plug in a microphone, "
                    "then press Start Audio Stream again."
                )
                return

            ok, msg = self.engine.start(PORT_DEFAULT, self.target_cable_id)
            if ok:
                self.btn_stream.SetLabel("Stop Audio Stream (Alt+&R)")
                self.btn_stream.SetName("Stop Audio Stream (Alt+R)")
                self.btn_mute.Enable()

                # Starting a phone stream also asks the phone to begin sending. The
                # engine only listens, so without this the button would look active while
                # no audio ever arrived. Auto-connect on discovery was removed, so this
                # is the single action the user needs.
                self._request_phone_audio_if_needed(src_type)

                self.lbl_status.SetLabel(f"Status: Streaming Active ({msg})")
                # Event name must not be "phone_connected". In Natural Voice mode that
                # name plays the phone_connected.wav clip, which says "Phone connected"
                # even when the user started a USB microphone with no phone present.
                notify_event("stream_started", f"Streaming started. Capturing from {src_label}.",
                             1000, 150)
            else:
                self.btn_stream.SetLabel("Start Audio Stream (Alt+&R)")
                self.btn_stream.SetName("Start Audio Stream (Alt+R)")
                self.btn_mute.Disable()
                self.lbl_status.SetLabel(f"Status Error: {msg}")
                play_audio_cue(400, 250)
                speak_accessibility(f"Stream error: {msg}")
        else:
            self.engine.stop()
            self.btn_stream.SetLabel("Start Audio Stream (Alt+&R)")
            self.btn_stream.SetName("Start Audio Stream (Alt+R)")
            self.btn_mute.Disable()
            self.lbl_status.SetLabel("Status: Stopped")
            # Likewise not "phone_disconnected", for the same reason.
            notify_event("stream_stopped", "Streaming stopped.", 400, 200)

    def _apply_mute_ui(self, muted):
        """Single source of truth for the mute button label, status line and audio cue."""
        if muted:
            self.btn_mute.SetLabel("Unmute Microphone (Ctrl+Alt+&M)")
            self.btn_mute.SetName("Unmute Microphone (Ctrl+Alt+M)")
            self.lbl_status.SetLabel("Status: Microphone OFF (Muted)")
            notify_event("mic_muted", "Microphone off", 400, 180)
        else:
            self.btn_mute.SetLabel("Mute Microphone (Ctrl+Alt+&M)")
            self.btn_mute.SetName("Mute Microphone (Ctrl+Alt+M)")
            self.lbl_status.SetLabel("Status: Microphone ON (Active)")
            notify_event("mic_unmuted", "Microphone on", 1000, 150)

    def on_toggle_mute(self, event=None):
        muted = not self.engine.is_muted
        self.engine.set_muted(muted)
        self._apply_mute_ui(muted)

    def _on_remote_mute_received(self, muted):
        """Invoked when phone notification toggles mute remotely."""
        self._apply_mute_ui(muted)

    def on_volume_changed(self, event):
        val = self.sld_volume.GetValue()
        self.lbl_volume.SetLabel(f"Mic Volume: {val}%")
        self.sld_volume.SetName(f"Mic Volume Slider, {val} percent")
        self.engine.set_volume(val)

    def on_stereo_width_changed(self, event):
        val = self.sld_stereo_width.GetValue() / 10.0
        self.lbl_stereo_width.SetLabel(f"Stereo 3D Separation Width: {val:.1f}x")
        self.sld_stereo_width.SetName(f"Stereo 3D Separation Width Slider, {val:.1f}x")
        self.engine.set_stereo_width(val)

    def on_echo_changed(self, event):
        enabled = self.chk_echo.GetValue()
        delay = self.sld_echo_delay.GetValue()
        feedback = self.sld_echo_feedback.GetValue() / 100.0

        self.lbl_echo_delay.SetLabel(f"Echo Delay Time: {delay} ms")
        self.sld_echo_delay.SetName(f"Echo Delay Time Slider, {delay} ms")
        self.lbl_echo_feedback.SetLabel(f"Echo Feedback Intensity: {int(feedback * 100)}%")
        self.sld_echo_feedback.SetName(f"Echo Feedback Intensity Slider, {int(feedback * 100)} percent")
        self.engine.set_echo(enabled, delay, feedback)

    def on_pingpong_changed(self, event):
        enabled = self.chk_pingpong.GetValue()
        if enabled and not self.chk_echo.GetValue():
            self.chk_echo.SetValue(True)
            self.on_echo_changed(None)
        self.engine.set_ping_pong(enabled)

    def on_hotkey_echo(self, event):
        val = not self.chk_echo.GetValue()
        self.chk_echo.SetValue(val)
        self.on_echo_changed(None)

    def on_hotkey_pingpong(self, event):
        val = not self.chk_pingpong.GetValue()
        self.chk_pingpong.SetValue(val)
        self.on_pingpong_changed(None)

    def on_hotkey_input(self, event):
        self.notebook.SetSelection(1) # Switch to Audio Settings
        self.cmb_input_source.SetFocus()

    def on_input_source_changed(self, event):
        sel = self.cmb_input_source.GetSelection()
        if sel != wx.NOT_FOUND and sel < len(self.input_source_items):
            src_type, dev_id, label = self.input_source_items[sel]
            # Reaching this handler means the user picked it, so a later phone connection must
            # not silently switch the capture away from it. The app's own switches set the
            # suppression flag, because a helper changing the picker is not the user choosing.
            if not self._suppress_input_pick_mark:
                self._user_picked_input = True
            self.lbl_packets.SetLabel("Audio frames sent: 0")

            if src_type == "phone":
                self.lbl_mode.SetLabel("Stream activity: Waiting for phone")

                # Already listening to the phone. Switching again would stop the engine,
                # which sends the phone a stop command and leaves a gap with no
                # acknowledgement. That is what made the phone report the PC as not
                # responding and disconnect.
                #
                # The socket is the honest test here, not input_mode. input_mode defaults
                # to "phone" on a fresh engine, so a running engine that had never listened
                # looked like it was already streaming from the phone, the connect request
                # was skipped, and no audio ever arrived.
                already_listening = (
                    self.engine.is_running
                    and self.engine.input_mode == "phone"
                    and self.engine.sock is not None
                )
                if already_listening:
                    log.info("Already listening to the phone; nothing to switch.")
                    self.lbl_status.SetLabel("Status: Streaming from the phone.")
                    return

                log.info(f"Input device set to {label}.")
                if self.engine.is_running:
                    # set_input_source performs the switch and reports it honestly. Setting
                    # input_mode beforehand made it log "phone to phone", which hid the fact
                    # that the engine was coming off a PC microphone.
                    ok, msg = self.engine.set_input_source("phone", None)
                    if ok:
                        self.lbl_status.SetLabel("Status: Waiting for phone on Wi-Fi...")
                        speak_accessibility("Switched to Phone. Waiting for Wi-Fi connection.")
                        self._request_phone_audio_if_needed("phone", from_input_switch=True)
                    else:
                        self.lbl_status.SetLabel(f"Status Error: {msg}")
                        play_audio_cue(400, 250)
                        # Do NOT also announce success here: the switch failed.
                        speak_accessibility(f"Failed to switch input: {msg}")
                else:
                    self.engine.input_mode = "phone"
                    self.engine.input_device_id = None
                    self._request_phone_audio_if_needed("phone", from_input_switch=True)
                    speak_accessibility("Switched to Phone. Waiting for Wi-Fi connection.")
            else:
                log.info(f"Input device set to {label}.")
                self.lbl_mode.SetLabel(f"Stream activity: Capturing from {label}")

                # If engine is already running, seamlessly hot-swap to the selected PC mic
                if self.engine.is_running:
                    ok, msg = self.engine.set_input_source(src_type, dev_id)
                    if ok:
                        self.lbl_status.SetLabel(f"Status: Streaming active ({msg})")
                        notify_event("input_switched",
                                     f"Switched to {label}. Audio streaming active.", 1000, 150)
                    else:
                        self.lbl_status.SetLabel(f"Status Error: {msg}")
                        play_audio_cue(400, 250)
                        speak_accessibility(f"Failed to switch mic: {msg}")
                else:
                    # Stream is stopped, so record the choice and leave it stopped.
                    # Picking a microphone used to start streaming by itself, which took
                    # that decision away from the user. They press Start Audio Stream when
                    # they are ready, and this choice is what it will capture from.
                    self.engine.input_mode = src_type
                    self.engine.input_device_id = dev_id
                    log.info(f"Input set to {label}. Streaming stays stopped until Start is pressed.")
                    self._show_stopped_status()

    def on_spectral_noise_changed(self, event):
        enabled = self.chk_spectral_noise.GetValue()
        val = self.sld_noise_reduction.GetValue()
        if val == 0:
            self.lbl_noise_reduction.SetLabel("Noise Reduction Intensity: 0% (Bypassed / Rumble Cut Only)")
            self.sld_noise_reduction.SetName("Noise Reduction Intensity Slider, 0 percent, Bypassed")
        else:
            label_tag = "Mild" if val < 30 else ("Balanced" if val <= 60 else "Strong")
            self.lbl_noise_reduction.SetLabel(f"Noise Reduction Intensity: {val}% ({label_tag})")
            self.sld_noise_reduction.SetName(f"Noise Reduction Intensity Slider, {val} percent, {label_tag}")
        self.engine.set_spectral_noise(enabled, val)

    def on_rumble_cut_changed(self, event):
        """Turns only the 80Hz rumble cut on or off, so the voice can be compared."""
        enabled = self.chk_rumble_cut.GetValue()
        self.engine.rumble_enabled = enabled
        label = "Rumble cut 80Hz: " + ("On" if enabled else "Off (unfiltered voice)")
        self.lbl_rumble_cut.SetLabel(label)
        self.lbl_rumble_cut.SetName(label)
        log.info(label)

    def on_hotkey_rumble_cut(self, event):
        val = not self.chk_rumble_cut.GetValue()
        self.chk_rumble_cut.SetValue(val)
        self.on_rumble_cut_changed(None)

    def on_hotkey_spectral_noise(self, event):
        val = not self.chk_spectral_noise.GetValue()
        self.chk_spectral_noise.SetValue(val)
        self.on_spectral_noise_changed(None)

    def on_monitor_changed(self, event):
        enabled = self.chk_monitor.GetValue()
        self.engine.set_monitoring(enabled)

    def on_hotkey_monitor(self, event):
        val = not self.chk_monitor.GetValue()
        self.chk_monitor.SetValue(val)
        self.on_monitor_changed(None)

    def on_engine_status(self, state, message):
        wx.CallAfter(self._update_status_ui, state, message)

    def _update_status_ui(self, state, message):
        try:
            if not self or not hasattr(self, 'lbl_status') or not self.lbl_status:
                return
            self.lbl_status.SetLabel(f"Status: {message}")
            if state == "Stopped" and self.btn_stream and self.btn_stream.GetLabel().startswith("Stop"):
                self.btn_stream.SetLabel("Start Audio Stream (Alt+&R)")
                self.btn_stream.SetName("Start Audio Stream (Alt+R)")
                self.btn_mute.Disable()
            elif state in ("Connected", "Listening") and self.btn_stream and self.btn_stream.GetLabel().startswith("Start"):
                self.btn_stream.SetLabel("Stop Audio Stream (Alt+&R)")
                self.btn_stream.SetName("Stop Audio Stream (Alt+R)")
                self.btn_mute.Enable()
        except Exception:
            pass

    def on_engine_packets(self, count, mode_str):
        wx.CallAfter(self._update_packet_ui, count, mode_str)

    def _update_packet_ui(self, count, mode_str):
        try:
            if not self or not hasattr(self, 'lbl_packets') or not self.lbl_packets:
                return
            self.lbl_packets.SetLabel(f"Audio frames sent: {count}")
            mode_label = f"Stream activity: Active ({mode_str})"
            self.lbl_mode.SetLabel(mode_label)
            self.lbl_mode.SetName(mode_label)
        except Exception:
            pass

    def on_about(self, event=None):
        """
        Displays the About dialog: version, creator, and the support address.

        The address is also written out in spoken form, because a screen reader reads a
        bare email address character by character, which is tedious to hear. "at" and
        "dot" are spoken instead of @ and .
        """
        about_text = (
            f"ZMic Studio {APP_VERSION_DISPLAY}\n\n"
            "Created & Designed by Zameer\n\n"
            "An accessible, studio-grade virtual microphone system bridging your smartphone\n"
            "and Windows PC over local Wi-Fi with crystal-clear 48kHz dual-mic stereo sound.\n\n"
            "Engineered with screen-reader first accessibility for NVDA and Google TalkBack.\n"
            "High-performance DSP with continuous noise reduction and 3D spatial width.\n\n"
            "Feedback and support:\n"
            "zameer.sstr@gmail.com"
        )
        speak_accessibility(
            f"About {APP_NAME}. Version {APP_VERSION_DISPLAY}. Created and Designed by Zameer. "
            "For feedback or support, email zameer.sstr at gmail dot com."
        )
        wx.MessageBox(about_text, "About ZMic Studio", wx.OK | wx.ICON_INFORMATION, self)

    def on_allow_speakers_changed(self, event):
        """Feedback protection is opt-in; make the consequence explicit when it is turned on."""
        enabled = self.chk_allow_speakers.GetValue()
        self.engine.set_allow_physical_speakers(enabled)

        if enabled and not self.engine.is_running:
            self.lbl_status.SetLabel(
                "Status: Physical speakers allowed. Feedback loop is possible. Use headphones."
            )
            play_audio_cue(1000, 150)
            speak_accessibility(
                "Physical speakers allowed. Expect an echoing feedback loop. Use headphones instead."
            )

    def on_voice_mode_changed(self, event):
        """Updates notification voice mode when user selects from dropdown."""
        sel = self.cmb_voice_mode.GetSelection()
        modes = [NOTIFICATION_MODE_NATURAL, NOTIFICATION_MODE_NVDA, NOTIFICATION_MODE_BEEPS]
        if 0 <= sel < len(modes):
            set_notification_mode(modes[sel])
            labels = ["Natural Voice selected", "NVDA Screen Reader selected", "Beeps only selected"]
            speak_accessibility(labels[sel])

    def on_open_log(self, event=None):
        log.info("User requested to open diagnostic log file.")
        try:
            if os.path.exists(LOG_FILE):
                os.startfile(LOG_FILE)
            else:
                wx.MessageBox(f"Log file not found at:\n{LOG_FILE}", "ZMic Studio Log", wx.OK | wx.ICON_INFORMATION)
        except Exception as e:
            log.error(f"Failed to open log file: {e}", exc_info=True)

    def on_close(self, event):
        log.info("ZMicStudioFrame closing, releasing audio engine resources...")
        try:
            if hasattr(self, 'id_global_hotkey_mute'):
                self.UnregisterHotKey(self.id_global_hotkey_mute)
        except Exception as e:
            log.warning(f"Error unregistering global hotkey: {e}")
        if hasattr(self, 'discovery') and self.discovery:
            self.discovery.stop()
        # Detach callbacks first so stop() does not post CallAfter to a destroyed frame
        self.engine.on_status_changed = lambda *args: None
        self.engine.on_packet_received = lambda *args: None
        self.engine.stop()
        self.Destroy()
        log.info("ZMicStudioFrame destroyed.")


# Backward compatibility alias for test suites
PhoneMicFrame = ZMicStudioFrame


def global_excepthook(exctype, value, tb):
    try:
        import traceback
        log.critical("UNCAUGHT APPLICATION CRASH EXCEPTION:", exc_info=(exctype, value, tb))
        with open(CRASH_LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(f"\n[Crash at {time.ctime()}]\n")
            traceback.print_exception(exctype, value, tb, file=f)
    except Exception:
        pass

def main():
    sys.excepthook = global_excepthook
    threading.excepthook = lambda args: global_excepthook(args.exc_type, args.exc_value, args.exc_traceback)
    log.info("=" * 60)
    log.info("ZMic Studio Windows Companion starting up...")
    log.info(f"Python: {sys.version.split()[0]} | Platform: {sys.platform}")
    log.info(f"Log File Location: {LOG_FILE}")
    log.info(f"Protocol: v{PROTOCOL_VERSION} | Audio port {PORT_DEFAULT} | Discovery port {DISCOVERY_PORT}")
    log.info(f"Notification mode: {get_notification_mode()}")

    # Notification audio is played on the Windows DEFAULT output device. If the user has set
    # that to a virtual sink, cues route into the cable and become inaudible -- say so loudly
    # rather than leaving the user wondering why they hear no notifications.
    startup_warning = check_notification_audibility()
    if startup_warning:
        print("=" * 60)
        print("  WARNING: " + startup_warning)
        print("=" * 60)

    app = None
    try:
        app = wx.App(False)
        wx.DisableAsserts()

        # ---- Startup gate -------------------------------------------------
        # The main interface is never built without a virtual audio cable. Without one, other
        # applications cannot see the phone as a microphone, so there is nothing useful to show.
        # Both gate actions (Download, Exit) end the process: installing the driver requires a
        # computer restart, so the app must be reopened afterwards anyway.
        cable_present, cable_id, cable_name = is_virtual_cable_present()
        if not cable_present:
            log.info("No virtual audio cable installed. Showing requirement gate and exiting.")
            gate = VirtualCableRequiredGate(None)
            gate.ShowModal()
            gate.Destroy()
            log.info("Closed at the virtual cable gate. Install the driver, restart, reopen.")
            return 0

        # ---- Terms and Privacy --------------------------------------------
        # Shown on the first run, and again whenever the wording changes. Declining closes
        # the application. Skipped under test so automated suites are never blocked by a
        # modal dialog.
        is_test_run = getattr(sys, '_running_tests', False) or os.environ.get('ZMIC_TEST_MODE') == '1'
        if not is_test_run and not terms.is_terms_accepted():
            log.info("Showing first run Terms and Privacy.")
            wizard = TermsWizard(None)
            result = wizard.ShowModal()
            wizard.Destroy()
            if result != wx.ID_OK:
                log.info("Terms and Privacy were not accepted. Closing.")
                return 0
        else:
            log.info(f"Terms and Privacy already accepted (version {terms.TERMS_VERSION}).")

        frame = ZMicStudioFrame()
        frame.Show()
        log.info("GUI Frame shown, entering MainLoop...")
        app.MainLoop()
        log.info("MainLoop exited cleanly.")
        return 0
    except Exception as e:
        log.critical(f"Fatal crash in main(): {e}", exc_info=True)
        global_excepthook(*sys.exc_info())
        return 1
    finally:
        # Let atexit handlers, logging shutdown and temp-file cleanup run.
        # os._exit() here would skip them and could truncate the crash log.
        log.info("ZMic Studio shutting down.")

if __name__ == "__main__":
    sys.exit(main())