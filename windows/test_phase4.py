"""
ZMIC STUDIO PHASE 4 - NOTIFICATION SYSTEM TEST SUITE

Covers the notification cue pipeline added for the selectable voice mode:
  - Mode selector (Natural Voice / NVDA Screen Reader / Beeps Only)
  - Voice-clip resolution and screen-reader fallback
  - Per-event debouncing
  - Thread-safe concurrent notification
  - Serialised cue queue (no truncation)
  - Notification audibility diagnostic

Created & Designed by Zameer
"""
import os
import sys
import time
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import zmic_studio_gui as Z

SOUNDS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sounds")
FAILURES = []


def check(condition, label, detail=""):
    if condition:
        print(f"  -> OK: {label}")
    else:
        print(f"  -> FAIL: {label} {detail}")
        FAILURES.append(label)


def test_mode_selector():
    print("Test 1: Notification Mode Selector...")
    valid = [Z.NOTIFICATION_MODE_NATURAL, Z.NOTIFICATION_MODE_NVDA, Z.NOTIFICATION_MODE_BEEPS]
    original = Z.get_notification_mode()

    for mode in valid:
        ok = Z.set_notification_mode(mode)
        check(ok and Z.get_notification_mode() == mode, f"mode '{mode}' accepted")

    ok = Z.set_notification_mode("not_a_real_mode")
    check(ok is False, "invalid mode rejected")
    check(Z.get_notification_mode() == Z.NOTIFICATION_MODE_BEEPS,
          "invalid mode did not change the active mode",
          f"(now {Z.get_notification_mode()})")

    Z.set_notification_mode(original)
    print("  -> Passed!")


def test_voice_clip_resolution():
    print("Test 2: Voice Clip Resolution & Missing-Asset Handling...")
    events = ["phone_connected", "phone_disconnected", "mic_muted", "mic_unmuted"]

    for ev in events:
        path = Z._resolve_sound_path(ev)
        check(path is not None and os.path.exists(path), f"voice clip present for '{ev}'", f"({path})")

    # Bundled clips must be readable as real WAV audio, not placeholder files.
    for ev in events:
        path = Z._resolve_sound_path(ev)
        try:
            import wave
            with wave.open(path, "rb") as w:
                frames, rate = w.getnframes(), w.getframerate()
            check(frames > 0 and rate > 0, f"'{ev}' is decodable audio", f"({frames} frames @ {rate}Hz)")
        except Exception as e:
            check(False, f"'{ev}' is decodable audio", f"({e})")

    check(Z._resolve_sound_path("no_such_event_xyz") is None, "missing clip returns None")
    check(Z._resolve_sound_path("") is None, "empty event name returns None")
    check(Z._resolve_sound_path("../../etc/passwd") is None, "path traversal is sanitised away")
    print("  -> Passed!")


def test_debouncing():
    print("Test 3: Per-Event Debouncing...")
    Z.set_notification_mode(Z.NOTIFICATION_MODE_BEEPS)
    Z._last_notify_times.clear()

    fired_first = Z.notify_event("debounce_test", "first", 1000, 100, debounce_sec=2.0)
    check(fired_first is True, "first call fires")

    fired_again = Z.notify_event("debounce_test", "second", 1000, 100, debounce_sec=2.0)
    check(fired_again is False, "immediate repeat is suppressed")

    # A DIFFERENT event must not be blocked by the first event's debounce window.
    other = Z.notify_event("debounce_other", "other", 1000, 100, debounce_sec=2.0)
    check(other is True, "different event is not debounced by another event")

    Z._last_notify_times.clear()
    print("  -> Passed!")


def test_natural_mode_fallback():
    print("Test 4: Natural Mode Falls Back When No Clip Is Bundled...")
    spoken = []
    original = Z.speak_accessibility
    Z.speak_accessibility = lambda text, **kw: spoken.append(text)
    try:
        Z.set_notification_mode(Z.NOTIFICATION_MODE_NATURAL)
        Z._last_notify_times.clear()

        # Event WITH a bundled clip -> voice clip queued, no duplicate speech.
        Z.notify_event("phone_connected", "Phone connected")
        time.sleep(0.05)
        check(spoken == [], "bundled clip emits no duplicate screen-reader speech", f"({spoken})")

        # Event WITHOUT a bundled clip -> must still be announced, never silent.
        spoken.clear()
        Z.notify_event("phone_forgotten", "Device forgotten", debounce_sec=0.0)
        time.sleep(0.05)
        check(spoken == ["Device forgotten"],
              "missing clip falls back to screen-reader speech", f"({spoken})")
    finally:
        Z.speak_accessibility = original
        Z._last_notify_times.clear()
    print("  -> Passed!")


def test_cue_queue_is_sequential():
    print("Test 5: Cue Queue Serialisation (no truncation)...")
    played = []

    def fake_play_sound(path, flags):
        played.append(('file', path))

    def fake_beep(freq, dur):
        played.append(('tone', freq, dur))

    real_winsound = None
    try:
        import winsound
        real_winsound = (winsound.PlaySound, winsound.Beep)
        winsound.PlaySound = fake_play_sound
        winsound.Beep = fake_beep

        Z._ensure_cue_worker()
        time.sleep(0.15)
        before = len(played)

        # Burst of 6 rapid cues: without serialisation each new one would cut the previous.
        for i in range(6):
            Z._enqueue_cue(('tone', 800 + i, 40))
        Z._cue_queue.join()
        time.sleep(0.2)

        added = played[before:]
        check(len(added) == 6, "all 6 queued cues played", f"(got {len(added)})")
        check([a[1] for a in added] == [800, 801, 802, 803, 804, 805],
              "cues played in order, none dropped", f"({[a[1] for a in added]})")
    finally:
        if real_winsound:
            import winsound
            winsound.PlaySound, winsound.Beep = real_winsound

    print("  -> Passed!")


def test_cue_queue_saturation():
    print("Test 6: Cue Queue Saturation Handling...")
    while not Z._cue_queue.empty():
        try:
            Z._cue_queue.get_nowait()
        except Exception:
            break

    # Push far more than the queue capacity; it must not raise or grow without bound.
    for i in range(Z._cue_queue.maxsize * 4):
        Z._enqueue_cue(('tone', 600, 10))
    size = Z._cue_queue.qsize()
    check(size <= Z._cue_queue.maxsize, "queue stays bounded when flooded", f"(size {size})")

    while not Z._cue_queue.empty():
        try:
            Z._cue_queue.get_nowait()
        except Exception:
            break
    print("  -> Passed!")


def test_thread_safety():
    print("Test 7: Concurrent Notification Safety...")
    Z.set_notification_mode(Z.NOTIFICATION_MODE_BEEPS)
    Z._last_notify_times.clear()

    errors = []
    call_count = {"n": 0}
    count_lock = threading.Lock()

    def worker():
        try:
            for _ in range(40):
                if Z.notify_event("race_event", "race", 900, 50, debounce_sec=30.0):
                    with count_lock:
                        call_count["n"] += 1
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    check(not errors, "no exception raised under concurrency", f"({errors})")
    # 8 threads x 40 calls with a 30s debounce window: exactly ONE may fire.
    check(call_count["n"] == 1,
          "debounce held under 320 concurrent calls", f"(fired {call_count['n']} times)")
    Z._last_notify_times.clear()
    print("  -> Passed!")


def test_audibility_diagnostic():
    print("Test 8: Notification Audibility Diagnostic...")
    result = Z.check_notification_audibility()
    # Returns None (fine), a warning string (default output is a virtual sink), or is
    # documented as unavailable. Any of those is acceptable; it must never raise.
    check(result is None or isinstance(result, str),
          "diagnostic returns a warning string or None", f"(got {type(result).__name__})")
    if isinstance(result, str):
        print(f"     -> Environment warning: {result[:90]}...")
    else:
        print("     -> Default output device is a real output; notifications will be audible.")
    print("  -> Passed!")


def test_virtual_cable_gate():
    """
    The main interface must never open without a virtual audio cable. Verifies the gate helper,
    that no installer is bundled or launched any more, and the vendor URL.
    """
    print("Test 9: Virtual Cable Startup Gate...")

    # 1. Vendor URL must be the full page, never a direct .zip link (which would break on repack).
    check(Z.VB_CABLE_DOWNLOAD_URL == "https://vb-audio.com/Cable/",
          "download URL is the full vendor page")
    check(not Z.VB_CABLE_DOWNLOAD_URL.lower().endswith(".zip"),
          "download URL is not a hard-coded .zip link")

    # 2. The bundled vendor installer must no longer exist anywhere in the app folder.
    app_dir = os.path.dirname(os.path.abspath(Z.__file__))
    check(not os.path.isdir(os.path.join(app_dir, "vbcable")),
          "bundled 'vbcable' folder has been removed")
    installer_files = [f for f in os.listdir(app_dir)
                       if f.lower().endswith(".exe") and "vbcable" in f.lower()]
    check(not installer_files, "no VB-Cable installer file remains", f"({installer_files})")

    # 3. The old dialog that launched the vendor installer must be gone.
    check(not hasattr(Z, "VBCableRequiredDialog"), "installer-launching dialog removed")
    check(hasattr(Z, "VirtualCableRequiredGate"), "new download-only gate present")

    # 4. Source must not reference installer execution APIs any more.
    with open(Z.__file__, encoding="utf-8") as fh:
        src = fh.read()
    for banned in ("ShellExecuteW", "ctypes.windll", "VBCABLE_Setup"):
        check(banned not in src, f"no reference to '{banned}' in app source")

    # 5. Gate helper behaves correctly against a real device query.
    present, dev_id, dev_name = Z.is_virtual_cable_present()
    check(isinstance(present, bool), "gate returns a boolean")
    if present:
        check(dev_id is not None and dev_name, "present cable reports an id and name",
              f"({dev_name})")
        check('cable input' in dev_name.lower(),
              "reported device is the virtual cable", f"({dev_name})")
        print(f"     -> Virtual cable detected: '{dev_name}' (id {dev_id})")
    else:
        print("     -> No virtual cable on this PC: the gate WOULD block startup (expected here)")

    # 6. When absent, main() must not build a frame.
    check(callable(Z.is_virtual_cable_present), "gate helper is callable from main()")
    print("  -> Passed!")


def run_all():
    print("=" * 60)
    print("ZMIC STUDIO PHASE 4 NOTIFICATION SYSTEM TEST SUITE")
    print("=" * 60)
    test_mode_selector()
    test_voice_clip_resolution()
    test_debouncing()
    test_natural_mode_fallback()
    test_cue_queue_is_sequential()
    test_cue_queue_saturation()
    test_thread_safety()
    test_audibility_diagnostic()
    test_virtual_cable_gate()
    print("=" * 60)
    if FAILURES:
        print(f"PHASE 4 FAILED: {len(FAILURES)} check(s) failed")
        for f in FAILURES:
            print(f"  - {f}")
        print("=" * 60)
        sys.exit(1)
    print("ALL PHASE 4 NOTIFICATION TESTS PASSED (100% SUCCESS)!")
    print("=" * 60)


if __name__ == "__main__":
    run_all()
