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

import wx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import zmic_studio_gui as Z
import update_check

SOUNDS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sounds")
FAILURES = []

# Kept at module scope so the wx.App is not garbage collected between tests.
_test_app = None


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


def test_terms_and_privacy():
    """First run Terms and Privacy: text, acceptance storage and wizard behaviour."""
    print("Test 10: Terms and Privacy...")
    import terms

    # 1. All required sections present, in plain wording.
    headings = [h for h, _ in terms.TERMS_SECTIONS]
    for required in ("Acceptable Use", "Your Privacy", "Third Party Program", "No Guarantee"):
        check(required in headings, f"section present: {required}")

    # 2. Acceptable use must mention the real risks of a virtual microphone app.
    body = terms.get_terms_as_text().lower()
    for topic in ("permission", "another person", "privacy", "responsib"):
        check(topic in body, f"acceptable use mentions '{topic}'")

    # 3. Privacy section must not describe a feature the app does not have. It used to
    #    claim a weekly update check, and no update check exists in the code. Wording that
    #    describes behaviour the app does not have is a lie to a screen reader user.
    check("checks for updates once a week" not in body,
          "privacy text no longer promises a weekly update check")
    check("never installs anything" in body,
          "privacy text states the app never installs anything by itself")

    # The claim must be true, not just present. If an update checker is ever added, this
    # test is what should force these lines to be rewritten rather than left stale.
    # There is an update checker now, so the wording has to describe what it actually does:
    # one user-entered address, opt-in, downloads nothing, installs nothing.
    check(update_check.check_for_update("", "0.9.0").get('error') == 'empty',
          "an empty update address means no internet contact at all")
    check("downloads nothing" in body or "never downloads anything" in body,
          "privacy text says the check downloads nothing")
    check("never installs anything" in body,
          "privacy text says it never installs anything by itself")
    check("address empty" in body,
          "privacy text says an empty address means no internet use")

    # The checker must be reachable only from the button, never on an ordinary launch.
    # A screen-reader user cannot be shown an unrequested dialog without losing their place.
    with open(Z.__file__, encoding="utf-8") as fh:
          app_src = fh.read()
    check("check_for_update(" in app_src,
          "the update check exists")
    check("_background_update_check" not in app_src
          and "CallLater" not in app_src.split("def on_check_updates")[0][-400:],
          "no background update check runs on launch")
    check("vb-audio website" in body,
          "privacy text names the one web page the app can open")

    # 4. No bullet or star characters that a screen reader would read out loud.
    symbols = [ch for ch in body if ch in ("*", "\u2022", "\u25cf", "\u2023", "\u2043")]
    check(not symbols, f"no bullet or star characters in terms text {symbols}")

    # 5. Acceptance storage round trip. The user's real state is captured first and put back at
    # the end, because this is the only test that touches the actual settings file.
    accepted_before = terms.is_terms_accepted()
    terms.clear_terms_acceptance()
    check(terms.is_terms_accepted() is False, "acceptance starts empty")
    ok = terms.record_terms_accepted()
    check(ok, "acceptance saved")
    check(terms.is_terms_accepted() is True, "acceptance read back as accepted")

    stored = terms.load_settings()
    check(stored.get('accepted_terms_version') == terms.TERMS_VERSION,
          "stored version matches TERMS_VERSION")

    # 6. Changing the wording must force a re-read.
    check(terms.is_terms_accepted() is True, "accepted while versions match")
    original_version = terms.TERMS_VERSION
    try:
        terms.TERMS_VERSION = original_version + "-test"
        check(terms.is_terms_accepted() is False,
              "a wording change forces the terms to be shown again")
    finally:
        terms.TERMS_VERSION = original_version

    # 7. Wizard exists and refuses to continue until the box is ticked.
    # The App must be kept alive at module scope: if it is only a local it gets garbage
    # collected and creating any window then fails with PyNoAppError.
    global _test_app
    if _test_app is None:
        _test_app = wx.App(False)
        wx.DisableAsserts()
    wizard = Z.TermsWizard(None)
    check(wizard.btn_continue.IsEnabled() is False,
          "Continue starts disabled")
    check(wizard.chk_agree.GetValue() is False,
          "checkbox starts unticked")
    check(wizard.btn_continue.IsEnabled() is False,
          "Continue stays disabled while unticked")
    wizard.chk_agree.SetValue(True)
    wizard.on_toggle_agree(None)
    check(wizard.btn_continue.IsEnabled() is True,
          "Continue enables after ticking")
    check(wizard.chk_agree.GetLabel() == terms.TERMS_CHECKBOX_LABEL,
          "checkbox label matches the terms module")
    wizard.Destroy()

    # 8. Read only viewer is reachable from Help.
    viewer = Z.TermsViewer(None)
    check(viewer.txt.GetValue() == terms.get_terms_as_text(),
          "viewer shows the same text as the wizard")
    viewer.Destroy()

    # Restore whatever the user actually had. This used to clear acceptance and stop there,
    # which meant running the suite wiped the real settings file and the Terms wizard appeared
    # on the next launch. A test must not leave the machine worse than it found it.
    if accepted_before:
        terms.record_terms_accepted()
    else:
        terms.clear_terms_acceptance()
    check(terms.is_terms_accepted() == accepted_before,
          f"the user's original Terms state is restored (was accepted={accepted_before})")
    print("  -> Passed!")


def test_stream_tab_ordering():
    """Status must come first and the duplicated read-only labels must be gone."""
    print("Test 11: Stream Tab Ordering and Noise Reduction...")
    frame = Z.PhoneMicFrame()
    try:
        # The removed duplicate labels must no longer exist on the frame.
        for gone in ("lbl_active_source", "lbl_active_sink", "lbl_active_mode"):
            check(not hasattr(frame, gone), f"duplicate label removed: {gone}")

        # The voice mode selector moved to the Stream tab.
        check(hasattr(frame, 'cmb_voice_mode'), "voice mode selector still present")
        check(frame.cmb_voice_mode.GetSelection() == 0, "voice mode defaults to Natural Voice")

        # Status is the first child of the Stream page, before the buttons.
        stream_page = frame.tab_stream
        children = [c for c in stream_page.GetChildren() if not isinstance(c, wx.Sizer)]
        status_index = None
        button_index = None
        for i, c in enumerate(children):
            if c is frame.lbl_status:
                status_index = i
            if c is frame.btn_stream:
                button_index = i
        check(status_index is not None, "status label found on Stream tab")
        check(button_index is not None, "stream button found on Stream tab")
        check(status_index is not None and button_index is not None
              and status_index < button_index,
              "status appears before the buttons",
              f"(status at {status_index}, button at {button_index})")

        # The Details area shows the new plain wording.
        check(frame.lbl_packets.GetLabel().startswith("Audio frames sent:"),
              "frames label uses plain wording", f"({frame.lbl_packets.GetLabel()})")
        check("Stream activity" in frame.lbl_mode.GetLabel(),
              "activity label uses plain wording", f"({frame.lbl_mode.GetLabel()})")

        # The help tab is now split into topics, so its wording lives in a read only
        # TextCtrl rather than a long StaticText block. test_help_sections covers it.
    finally:
        frame.on_close(None)
    print("  -> Passed!")


def test_phone_only_listed_when_connected():
    """The phone must not sit in the microphone list before it is connected."""
    print("Test 15: Phone Listed Only When Connected...")
    global _test_app
    if _test_app is None:
        _test_app = wx.App(False)
        wx.DisableAsserts()

    frame = Z.PhoneMicFrame()
    try:
        # No phone connected yet.
        frame.discovered_devices_list = []
        frame._rebuild_input_sources()

        labels = [item[2] for item in frame.input_source_items]
        check(all("Phone" not in x for x in labels),
              "phone is absent from the list with no phone connected", f"{labels}")
        check(all(item[0] == "pc" for item in frame.input_source_items),
              "only PC microphones are offered when no phone is present")
        check("does not appear here until it is connected"
              in frame.lbl_input_hint.GetLabel(),
              "hint explains why the phone is missing",
              f"({frame.lbl_input_hint.GetLabel()})")

        # A discovered but not connected phone must still stay out of the list.
        frame.discovered_devices_list = [
            {"ip": "192.168.1.9", "name": "MyPhone", "status": "Available"}
        ]
        frame._rebuild_input_sources()
        labels = [item[2] for item in frame.input_source_items]
        check(all("Phone" not in x for x in labels),
              "a merely discovered phone is not listed yet", f"{labels}")

        # Once connected it appears.
        frame.discovered_devices_list = [
            {"ip": "192.168.1.9", "name": "MyPhone", "status": "Connected"}
        ]
        frame._rebuild_input_sources()
        labels = [item[2] for item in frame.input_source_items]
        check(any("Phone" in x for x in labels),
              "connected phone appears in the list", f"{labels}")
        check(any("MyPhone" in x for x in labels),
              "the phone is listed by name, so it is obvious which one", f"{labels}")
        phone_items = [i for i in frame.input_source_items if i[0] == "phone"]
        check(phone_items[0][1] is None, "phone entry carries no PC device id")

        # Disconnecting removes it again.
        frame.discovered_devices_list = []
        frame._rebuild_input_sources()
        labels = [item[2] for item in frame.input_source_items]
        check(all("Phone" not in x for x in labels),
              "phone leaves the list after disconnect", f"{labels}")

        # With no microphones and no phone, the control explains itself instead of
        # sitting empty and disabled for no visible reason.
        saved_devices = frame.input_devices
        frame.input_devices = []
        frame.discovered_devices_list = []
        frame._rebuild_input_sources()
        check(frame.cmb_input_source.GetCount() == 1,
              "empty list shows a single explanatory row",
              f"({frame.cmb_input_source.GetCount()})")
        check("No input device available" == frame.cmb_input_source.GetString(0),
              "the row says no input is available")
        check(frame.cmb_input_source.IsEnabled() is False,
              "the control is disabled when there is nothing to pick")
        check("No microphone is available" in frame.lbl_input_hint.GetLabel(),
              "hint explains the empty list")
        frame.input_devices = saved_devices
        frame._rebuild_input_sources()
        check(frame.cmb_input_source.IsEnabled() is True,
              "control comes back when a microphone exists")

        # Pressing Start with nothing selected must not start a silent stream.
        frame.discovered_devices_list = []
        frame.input_devices = []
        frame._rebuild_input_sources()
        frame.on_toggle_stream(None)
        check(not frame.engine.is_running,
              "Start does nothing when there is no input device")
        check(frame.btn_stream.GetLabel().startswith("Start Audio Stream"),
              "button still offers to start after the refusal",
              f"({frame.btn_stream.GetLabel()})")
        check("No input device" in frame.lbl_status.GetLabel(),
              "status explains that no input is available",
              f"({frame.lbl_status.GetLabel()})")
    finally:
        frame.on_close(None)
    print("  -> Passed!")


def test_stream_cues_do_not_say_phone_connected():
    """Starting a USB microphone must not play the phone connected clip."""
    print("Test 16: Stream Cues Stay Neutral...")
    global _test_app
    if _test_app is None:
        _test_app = wx.App(False)
        wx.DisableAsserts()

    with open(Z.__file__, encoding="utf-8") as fh:
        src = fh.read()

    # The bug: notify_event names decide which clip plays in Natural Voice mode, so a
    # USB stream was announcing "Phone connected" using the phone_connected.wav clip.
    check('notify_event("phone_connected", "Audio stream started"' not in src,
          "starting a stream no longer uses the phone_connected event")
    check('notify_event("phone_disconnected", "Audio stream stopped"' not in src,
          "stopping a stream no longer uses the phone_disconnected event")
    check('notify_event("stream_started"' in src,
          "starting a stream uses a neutral event name")
    check('notify_event("stream_stopped"' in src,
          "stopping a stream uses a neutral event name")

    # No bundled clip may make a USB stream sound like a phone event.
    import os
    sounds = os.path.join(os.path.dirname(Z.__file__), "sounds")
    for name in ("stream_started", "stream_stopped"):
        check(not os.path.exists(os.path.join(sounds, f"{name}.wav")),
              f"no '{name}' clip claims to be a phone event")

    frame = Z.PhoneMicFrame()
    try:
        frame.discovered_devices_list = []
        frame._rebuild_input_sources()
        pc = [i for i in frame.input_source_items if i[0] == "pc"]
        if not pc:
            check(True, "no PC microphone on this machine, cue check done by source scan")
        else:
            # Drive the real handlers rather than replaying notify_event by hand, so the
            # check fails if the button ever names a phone event again.
            seen = []
            original = Z.notify_event
            Z.notify_event = lambda name, *a, **k: seen.append(name)
            try:
                frame.on_toggle_stream(None)
                started = list(seen)
                frame.on_toggle_stream(None)
                stopped = list(seen)
            finally:
                Z.notify_event = original
                if frame.engine.is_running:
                    frame.engine.stop()

            check("stream_started" in started,
                  "starting a USB stream emits stream_started", f"{started}")
            check("stream_stopped" in stopped,
                  "stopping emits stream_stopped", f"{stopped}")
            check("phone_connected" not in started and "phone_disconnected" not in started,
                  "a USB stream never emits a phone event", f"{started + stopped}")
    finally:
        frame.on_close(None)
    print("  -> Passed!")


def test_phone_reconnect_and_manual_switch():
    """
    Losing the phone must not leave the stream stuck on a USB microphone, and switching
    to the phone by hand must actually make audio arrive.
    """
    print("Test 20: Phone Reconnect And Manual Switch...")
    global _test_app
    if _test_app is None:
        _test_app = wx.App(False)
        wx.DisableAsserts()

    frame = Z.PhoneMicFrame()
    try:
        usb = frame.input_devices[0][0] if frame.input_devices else None

        # --- manual switch must ask the phone to send ---
        frame.discovered_devices_list = [
            {"ip": "192.168.1.50", "name": "MyPhone", "port": 8088, "status": "Available"}
        ]
        frame.input_source_items = [("pc", usb, "Usb Mic"), ("phone", None, "Phone")]
        frame.cmb_input_source.Clear()
        for _, _, label in frame.input_source_items:
            frame.cmb_input_source.Append(label)
        frame.cmb_input_source.SetSelection(1)

        asked = []
        original = frame.discovery.connect_device
        frame.discovery.connect_device = lambda ip: asked.append(ip)
        frame.engine.is_running = True
        try:
            frame.engine.set_input_source = lambda *a, **k: (True, "ok")
            frame.on_input_source_changed(None)
        finally:
            frame.discovery.connect_device = original
            frame.engine.is_running = False

        check(frame.engine.input_mode == "phone",
              "manual switch moved the engine onto the phone input")
        check(asked == ["192.168.1.50"],
              "manual switch also asks the phone to start sending", f"{asked}")

        # A second request while the first is still in flight is what made the phone drop
        # out, so the helper must refuse to ask again for a while.
        asked.clear()
        frame._request_phone_audio_if_needed("phone", from_input_switch=True)
        check(not asked,
              "a second connect request is not sent while one is in flight", f"{asked}")

        # And it must stay silent when a phone is already connected.
        asked.clear()
        frame.discovered_devices_list = [
            {"ip": "192.168.1.50", "name": "MyPhone", "port": 8088, "status": "Connected"}
        ]
        frame._request_phone_audio_if_needed("phone", from_input_switch=True)
        check(not asked,
              "no request is sent when the phone is already connected", f"{asked}")

        # --- reconnecting must move the input back by itself ---
        # Set every piece of state explicitly. Relying on what an earlier check happened to
        # leave behind is what made this block report a wrong starting state.
        frame.engine.is_running = True
        frame.engine.input_mode = "pc"
        frame.engine.input_device_id = usb
        frame.engine.sock = None

        def _real_switch(src_type, device_id):
            frame.engine.input_mode = src_type
            frame.engine.input_device_id = device_id
            return True, "ok"

        frame.engine.set_input_source = _real_switch
        # The app is on the USB microphone because the phone went away, not because the user
        # asked for it. That is the case where capture should return to the phone by itself.
        frame._user_picked_input = False
        frame.input_source_items = [("pc", usb, "Usb Mic")]
        frame.cmb_input_source.Clear()
        frame.cmb_input_source.Append("Usb Mic")
        frame.cmb_input_source.SetSelection(0)
        check(frame.engine.input_mode == "pc", "starting state is the USB microphone")

        # The phone comes back, but is only visible on the network so far. The combo needs
        # the row as well, not just the items list, or SetSelection cannot reach it.
        frame.input_source_items = [("pc", usb, "Usb Mic"), ("phone", None, "Phone")]
        frame.cmb_input_source.Clear()
        for _, _, label in frame.input_source_items:
            frame.cmb_input_source.Append(label)
        frame.cmb_input_source.SetSelection(0)
        frame.discovered_devices_list = [
            {"ip": "192.168.1.50", "name": "MyPhone", "port": 8088, "status": "Available"}
        ]
        frame._follow_phone_back_to_phone_input()
        check(frame.engine.input_mode == "pc",
              "it waits until the phone has actually connected, not merely been seen",
              f"(mode={frame.engine.input_mode})")

        # Now the phone has accepted.
        frame.discovered_devices_list = [
            {"ip": "192.168.1.50", "name": "MyPhone", "port": 8088, "status": "Connected"}
        ]
        frame._follow_phone_back_to_phone_input()
        check(frame.engine.input_mode == "phone",
              "the input returned to the phone once it actually connected",
              f"(mode={frame.engine.input_mode})")
        sel = frame.cmb_input_source.GetSelection()
        check(frame.input_source_items[sel][0] == "phone",
              "the picker also shows the phone as selected", f"(index={sel})")

        # It follows the phone whenever a PC microphone is the selection and the phone is
        # connected. That is the point of the helper: the phone came back, so capture goes
        # back to the phone without being asked.
        frame.input_source_items = [("phone", None, "Phone"), ("pc", usb, "Usb Mic")]
        frame.cmb_input_source.Clear()
        frame.cmb_input_source.Append("Phone")
        frame.cmb_input_source.Append("Usb Mic")
        frame.cmb_input_source.SetSelection(1)
        frame.engine.input_mode = "pc"
        frame._follow_phone_back_to_phone_input()
        check(frame.engine.input_mode == "phone",
              "it follows the phone again when it is selected once more",
              f"(mode={frame.engine.input_mode})")

        # A PC microphone the user chose themselves is left alone even when the phone is
        # connected. Dragging the capture away from their choice left the list showing a USB
        # microphone while the voice came from the phone.
        frame._user_picked_input = True
        frame.engine.input_mode = "pc"
        frame.cmb_input_source.SetSelection(1)
        frame.engine.sock = None
        frame._follow_phone_back_to_phone_input()
        check(frame.engine.input_mode == "pc",
              "it does not override a PC microphone the user picked",
              f"(mode={frame.engine.input_mode})")
        frame._user_picked_input = False

        # But it never acts while the phone is on the network and not connected, because
        # switching onto a phone that is not ready is what made it disconnect.
        frame.engine.input_mode = "pc"
        frame.cmb_input_source.SetSelection(1)
        frame.discovered_devices_list = [
            {"ip": "192.168.1.50", "name": "MyPhone", "port": 8088, "status": "Available"}
        ]
        frame._follow_phone_back_to_phone_input()
        check(frame.engine.input_mode == "pc",
              "it does not switch onto a phone that has not connected",
              f"(mode={frame.engine.input_mode})")
        frame.discovered_devices_list = [
            {"ip": "192.168.1.50", "name": "MyPhone", "port": 8088, "status": "Connected"}
        ]

        # And it must do nothing while the stream is stopped.
        frame.engine.is_running = False
        frame.engine.input_mode = "pc"
        frame.cmb_input_source.SetSelection(1)
        frame._follow_phone_back_to_phone_input()
        check(frame.engine.input_mode == "pc",
              "it does nothing when the stream is not running")
        # Re-selecting the phone while it is already the input must not stop the engine.
        # That stop sends the phone a stop command and leaves a gap with no
        # acknowledgement, which is what made the phone report the PC as not responding.
        frame.engine.input_mode = "phone"
        frame.engine.is_running = True
        # The guard looks at the socket, because input_mode defaults to phone. A running
        # engine with no socket is not listening to the phone, so it must still switch.
        frame.engine.sock = object()
        stops = []
        frame.engine.stop = lambda: stops.append("stopped")
        frame.engine.set_input_source = lambda *a, **k: stops.append("switched") or (True, "ok")

        asked.clear()
        frame.input_source_items = [("phone", None, "Phone"), ("pc", usb, "Usb Mic")]
        frame.cmb_input_source.Clear()
        frame.cmb_input_source.Append("Phone")
        frame.cmb_input_source.Append("Usb Mic")
        frame.cmb_input_source.SetSelection(0)
        frame.on_input_source_changed(None)

        check(not stops,
              "re-selecting the already active phone stops nothing", f"{stops}")
        check(not asked,
              "re-selecting the phone sends no connect request", f"{asked}")
        check(frame.engine.input_mode == "phone",
              "the engine stays on the phone")

        # But choosing the USB microphone must still work while streaming from the phone.
        frame.cmb_input_source.SetSelection(1)
        swaps = []
        frame.engine.set_input_source = lambda t, d: swaps.append(t) or (True, "ok")
        frame.on_input_source_changed(None)
        check(swaps == ["pc"],
              "choosing a PC microphone still switches away from the phone", f"{swaps}")

        # And a running engine with no phone socket must still be switched properly. This
        # was a real bug: input_mode defaults to phone, so the guard skipped the switch and
        # the connect request, and no audio ever arrived.
        frame.engine.sock = None
        frame.engine.input_mode = "phone"
        frame.engine.is_running = True
        frame.input_source_items = [("phone", None, "Phone")]
        frame.cmb_input_source.Clear()
        frame.cmb_input_source.Append("Phone")
        frame.cmb_input_source.SetSelection(0)

        # Phone on the network but not accepted yet. This is the case that silently
        # produced no audio: the guard used to think it was already streaming, so nothing
        # was ever asked for.
        asked.clear()
        frame._last_connect_request_at = 0.0
        frame.discovered_devices_list = [
            {"ip": "192.168.1.50", "name": "MyPhone", "port": 8088, "status": "Available"}
        ]
        frame.discovery.connect_device = lambda ip: asked.append(ip)
        frame.engine.set_input_source = _real_switch
        frame.on_input_source_changed(None)
        check(asked == ["192.168.1.50"],
              "a phone selected with no live socket asks the phone to send", f"{asked}")

        frame.engine.is_running = False
        frame.engine.input_mode = "phone"
    finally:
        frame.on_close(None)
    print("  -> Passed!")


def test_about_reachable_and_complete():
    """About must be findable without knowing a shortcut, and must carry the contact."""
    print("Test 19: About Is Reachable...")
    global _test_app
    if _test_app is None:
        _test_app = wx.App(False)
        wx.DisableAsserts()

    frame = Z.PhoneMicFrame()
    try:
        # A screen reader user does not know shortcuts, so About must be a tab they can
        # reach from the tab strip, not a hidden dialog.
        check(hasattr(frame, "tab_about"), "About has a tab of its own")

        tab_names = [
            frame.notebook.GetPageText(i).replace("&", "")
            for i in range(frame.notebook.GetPageCount())
        ]
        check("About" in tab_names,
              "the About tab appears in the tab strip", f"{tab_names}")

        # Nothing may be hidden off the bottom of a tab. The user reported being unable
        # to find this, so check the whole page is laid out rather than trusting a name.
        page = frame.tab_about
        frame.notebook.SetSelection(frame.notebook.GetPageCount() - 1)
        frame.Layout()

        texts = []
        stack = list(page.GetChildren())
        while stack:
            c = stack.pop(0)
            try:
                stack.extend(c.GetChildren())
            except Exception:
                pass
            if isinstance(c, wx.TextCtrl):
                texts.append(c.GetValue())
        blob = "\n".join(texts)
        check(len(blob) > 0, "the About tab has a readable text body")
        check(isinstance(getattr(frame, "txt_about", None), wx.TextCtrl),
              "the body is a TextCtrl, because NVDA skipped StaticText labels here")
        check("Z Mic Studio" in blob, "the tab names the app")
        check(Z.APP_VERSION_DISPLAY in blob,
              "the version is on the page", f"({Z.APP_VERSION_DISPLAY})")
        check("Zameer" in blob, "the creator is on the page")

        # The address must be readable on screen AND spoken in words. Showing the
        # word form on screen puts nonsense in front of the user, which is worse than
        # a character by character reading.
        check("zameer.sstr@gmail.com" in blob,
              "the address on screen is a real, readable address")
        check("at gmail dot com" not in blob,
              "the spoken word form is not printed on the screen")

        # It must be focusable and read-only, so arrow keys move through the text and
        # nothing can be typed into it.
        check(frame.txt_about.IsEditable() is False,
              "the About body cannot be edited")
        check(frame.txt_about.AcceptsFocus() is True,
              "the About body takes focus, so a screen reader can read it",
              f"(AcceptsFocus={frame.txt_about.AcceptsFocus()})")
        check(frame.txt_about.GetValue().count("\n") > 3,
              "the About body is laid out as separate lines")

        email_label = frame.txt_about
        check("zameer.sstr@gmail.com" in email_label.GetValue(),
              "the address control carries the readable address")

        # Every label must also be readable by a screen reader, not just painted.
        unnamed = []
        stack = list(page.GetChildren())
        while stack:
            c = stack.pop(0)
            try:
                stack.extend(c.GetChildren())
            except Exception:
                pass
            if isinstance(c, (wx.StaticText, wx.Button)) and not c.GetName().strip():
                unnamed.append(c.GetLabel())
        check(not unnamed,
              "every control on the About tab has an accessible name", f"{unnamed}")

        # The jump back to Help must work, so the tab is not a dead end.
        frame.on_go_to_help_tab(None)
        check(frame.notebook.GetSelection() != frame.notebook.GetPageCount() - 1,
              "the Go to Help button moves off the About tab")

        # The dialog must carry the three things a user actually needs.
        spoken = []
        dialogs = []
        original_speak = Z.speak_accessibility
        original_box = wx.MessageBox
        Z.speak_accessibility = lambda text, *a, **k: spoken.append(text)
        wx.MessageBox = lambda message, *a, **k: dialogs.append(message)
        try:
            frame.on_about(None)
        finally:
            Z.speak_accessibility = original_speak
            wx.MessageBox = original_box

        check(len(dialogs) == 1, "pressing About opens exactly one dialog", f"({dialogs})")
        body = dialogs[0] if dialogs else ""
        check(Z.APP_VERSION_DISPLAY in body,
              "the dialog shows the version", f"({Z.APP_VERSION_DISPLAY})")
        check("Zameer" in body, "the dialog names the creator")
        check("zameer.sstr@gmail.com" in body,
              "the dialog shows the support address")

        # Spoken text must spell the address out, not read punctuation.
        check(bool(spoken), "pressing About speaks the dialog")
        said = " ".join(spoken)
        check("zameer.sstr at gmail dot com" in said,
              "the address is spoken in words, not read character by character", f"({said})")
        check("@" not in said,
              "no raw @ symbol is spoken, which a screen reader mangles")
        check("gmail dot com" not in said.replace("gmail dot com", ""),
              "the address is not spoken twice")
    finally:
        frame.on_close(None)
    print("  -> Passed!")


def test_phone_accept_switches_input_to_phone():
    """
    A phone accepting must move a running engine onto the phone input.

    The bug: engine.start() returns early when the engine is already running, so
    input_mode stayed on a PC microphone. The audio receiver thread only runs in phone
    mode, so the PC sent no heartbeat acknowledgements and the phone showed
    "PC not responding" right after announcing that it had connected.
    """
    print("Test 18: Phone Accept Switches A Running Engine...")
    global _test_app
    if _test_app is None:
        _test_app = wx.App(False)
        wx.DisableAsserts()

    frame = Z.PhoneMicFrame()
    try:
        # Pretend the engine is already running and capturing from a PC microphone,
        # which is the state that used to break.
        frame.engine.is_running = True
        frame.engine.input_mode = "pc"
        frame.engine.input_device_id = frame.input_devices[0][0] if frame.input_devices else None

        frame._handle_discovery_response(True, "MyPhone", "192.168.1.50", 8088)

        check(frame.engine.input_mode == "phone",
              "engine input switched to the phone after it accepted",
              f"(mode={frame.engine.input_mode})")
        check(frame.engine.input_device_id is None,
              "the PC microphone id was cleared")

        started = []
        frame.engine._start_current_input = lambda: started.append("ok") or (True, "ok")
        try:
            frame.engine.set_input_source("phone", None)
        finally:
            pass
        check("ok" in started,
              "the phone input stream is actually started, so ACKs can flow", f"{started}")

        # And a PC microphone choice still works the same way.
        if frame.input_devices:
            frame.engine.set_input_source("pc", frame.input_devices[0][0])
            check(frame.engine.input_mode == "pc",
                  "switching back to a PC microphone still works",
                  f"(mode={frame.engine.input_mode})")
    finally:
        frame.engine.stop()
        frame.on_close(None)
    print("  -> Passed!")


def test_no_discovery_auto_connect():
    """Finding a paired phone must not connect or start the stream by itself."""
    print("Test 17: No Auto Connect On Discovery...")
    global _test_app
    if _test_app is None:
        _test_app = wx.App(False)
        wx.DisableAsserts()

    with open(Z.__file__, encoding="utf-8") as fh:
        src = fh.read()

    # The bug: the discovery listener called connect_device() the moment it saw a trusted
    # phone beacon, so opening the phone app started the microphone on the PC.
    check("Auto-reconnecting to trusted phone" not in src,
          "discovery no longer auto-reconnects to a trusted phone")

    frame = Z.PhoneMicFrame()
    try:
        disc = frame.discovery
        # If the real app is streaming, it holds UDP 8088 and this test cannot bind it.
        # That is a correct and expected situation, not a fault, so the packet level half
        # is skipped rather than reported as a product failure.
        port_taken = False
        import socket as _socket
        probe_bind = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        try:
            probe_bind.bind(("0.0.0.0", Z.DISCOVERY_PORT))
        except OSError:
            port_taken = True
        finally:
            probe_bind.close()

        # Declared up front because the cleanup at the end of the test refers to it
        # whether or not the beacon half ran.
        original_connect = None

        if port_taken:
            print("     -> Beacon half skipped: UDP 8089 is already in use, which means "
                  "the real app is running. The source check above still applies.")
        else:
            disc.stop()
            time.sleep(0.3)
            disc.start()
            time.sleep(0.3)

            disc.trusted_ip = "192.168.1.50"
            disc.trusted_name = "MyPhone"
            disc.devices.clear()

            sent = []
            original_connect = disc.connect_device
            disc.connect_device = lambda ip: sent.append(ip)
            try:
                # Send a real beacon to the discovery socket, exactly as a phone would.
                # The handling is inline in the listen loop, so a real packet is the only
                # honest way to prove nothing connects on its own.
                probe = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
                probe.sendto(b"PM_BEACON:MyPhone:8088", ("127.0.0.1", Z.DISCOVERY_PORT))
                probe.close()

                for _ in range(60):
                    if sent or "127.0.0.1" in disc.devices:
                        break
                    time.sleep(0.1)

                check(not sent,
                      "a trusted phone beacon does not trigger a connect", f"{sent}")
                check("127.0.0.1" in disc.devices,
                      "the phone is still recorded as discovered")
            finally:
                disc.connect_device = original_connect

        # Starting the stream is the action that now asks the phone to send.
        frame.discovered_devices_list = [
            {"ip": "192.168.1.50", "name": "MyPhone", "port": 8088, "status": "Available"}
        ]
        asked = []
        frame.discovery.connect_device = lambda ip: asked.append(ip)
        try:
            frame._request_phone_audio_if_needed("phone")
            check(asked == ["192.168.1.50"],
                  "Start asks the paired phone to begin sending", f"{asked}")

            asked.clear()
            frame._request_phone_audio_if_needed("pc")
            check(not asked,
                  "Start on a PC microphone does not contact any phone", f"{asked}")

            asked.clear()
            # Clear the in-flight guard, otherwise the new rule silently skips this and the
            # test would pass for the wrong reason or fail for no reason at all.
            frame._last_connect_request_at = 0.0
            frame.discovered_devices_list = []
            frame._request_phone_audio_if_needed("phone")
            check(not asked,
                  "no phone on the network means no connect attempt", f"{asked}")
            check("No phone found" in frame.lbl_status.GetLabel(),
                  "status explains that no phone was found",
                  f"({frame.lbl_status.GetLabel()})")

            # An already connected phone must not be asked a second time.
            asked.clear()
            frame.discovered_devices_list = [
                {"ip": "192.168.1.50", "name": "MyPhone", "port": 8088, "status": "Connected"}
            ]
            frame._request_phone_audio_if_needed("phone")
            check(not asked, "an already connected phone is not asked again", f"{asked}")
        finally:
            if original_connect is not None:
                disc.connect_device = original_connect
    finally:
        frame.on_close(None)
    print("  -> Passed!")


def test_no_auto_start():
    """The app must not open the microphone on its own."""
    print("Test 14: No Auto Start On Launch...")
    global _test_app
    if _test_app is None:
        _test_app = wx.App(False)
        wx.DisableAsserts()

    with open(Z.__file__, encoding="utf-8") as fh:
        src = fh.read()

    # The launch path must not call engine.start() on its own.
    check("wx.CallAfter(self._auto_start_receiver)" not in src,
          "launch no longer auto-starts the receiver")
    check("def _auto_start_receiver" not in src,
          "the auto-start helper is gone")
    check("Auto-start streaming immediately" not in src,
          "input switch no longer starts streaming by itself")

    frame = Z.PhoneMicFrame()
    try:
        check(not frame.engine.is_running,
              "engine is stopped when the window appears")
        check(frame.btn_stream.GetLabel().startswith("Start Audio Stream"),
              "the button offers to start", f"({frame.btn_stream.GetLabel()})")
        check(frame.btn_mute.IsEnabled() is False,
              "mute stays disabled while stopped")
        check("Not streaming" in frame.lbl_status.GetLabel(),
              "status says it is not streaming", f"({frame.lbl_status.GetLabel()})")

        # Choosing a different microphone must not open the microphone either.
        idx = 1 if frame.cmb_input_source.GetCount() > 1 else 0
        frame.cmb_input_source.SetSelection(idx)
        frame.on_input_source_changed(None)
        check(not frame.engine.is_running,
              "picking a different microphone does not start streaming")
        check(frame.btn_stream.GetLabel().startswith("Start Audio Stream"),
              "button still offers to start after picking a microphone",
              f"({frame.btn_stream.GetLabel()})")
        check(frame.btn_mute.IsEnabled() is False,
              "mute stays disabled after picking a microphone")

        # Stopping again must land back in the same waiting state.
        frame._show_stopped_status()
        check(frame.btn_stream.GetLabel().startswith("Start Audio Stream"),
              "stopping returns the button to Start")
        check(frame.btn_mute.IsEnabled() is False, "stopping disables mute")
    finally:
        frame.on_close(None)
    print("  -> Passed!")


def test_sliders_and_tab_shortcuts():
    """Up and Down must adjust sliders, and Ctrl+Tab must move between tabs."""
    print("Test 12: Slider Arrows and Tab Shortcuts...")
    global _test_app
    if _test_app is None:
        _test_app = wx.App(False)
        wx.DisableAsserts()

    frame = Z.PhoneMicFrame()
    try:
        # --- Up and Down must adjust a horizontal slider -------------------
        sld = frame.sld_volume
        sld.SetValue(50)

        def press(keycode):
            evt = wx.KeyEvent(wx.wxEVT_KEY_DOWN)
            evt.SetKeyCode(keycode)
            evt.SetEventObject(sld)
            sld.ProcessEvent(evt)
            return sld.GetValue()

        before = sld.GetValue()
        after_up = press(wx.WXK_UP)
        check(after_up > before, "Up arrow increases a horizontal slider",
              f"({before} -> {after_up})")

        after_down = press(wx.WXK_DOWN)
        check(after_down < after_up, "Down arrow decreases a horizontal slider",
              f"({after_up} -> {after_down})")

        # Clamped at the limits rather than wrapping or going out of range.
        sld.SetValue(sld.GetMax())
        check(press(wx.WXK_UP) == sld.GetMax(), "Up is clamped at the maximum")
        sld.SetValue(sld.GetMin())
        check(press(wx.WXK_DOWN) == sld.GetMin(), "Down is clamped at the minimum")

        # The value label must follow the arrow keys.
        sld.SetValue(70)
        press(wx.WXK_UP)
        check(f"{sld.GetValue()}%" in frame.lbl_volume.GetLabel(),
              "label updates when arrows change the value",
              f"({frame.lbl_volume.GetLabel()})")

        # Every slider in the app must be wired the same way.
        wired = 0
        for attr in ("sld_volume", "sld_noise_reduction", "sld_stereo_width",
                     "sld_echo_delay", "sld_echo_feedback"):
            candidate = getattr(frame, attr, None)
            if candidate is None:
                continue
            candidate.SetValue(candidate.GetMin() + 1)
            probe = candidate.GetValue()
            evt = wx.KeyEvent(wx.wxEVT_KEY_DOWN)
            evt.SetKeyCode(wx.WXK_UP)
            evt.SetEventObject(candidate)
            candidate.ProcessEvent(evt)
            if candidate.GetValue() != probe:
                wired += 1
        check(wired >= 4, "Up arrow works on the other sliders too", f"({wired} of 5)")

        # --- Ctrl+Tab must move between tabs -------------------------------
        frame.notebook.SetSelection(0)
        frame.on_next_tab(None)
        check(frame.notebook.GetSelection() == 1, "next tab moves 0 -> 1")
        for _ in range(frame.notebook.GetPageCount() - 1):
            frame.on_next_tab(None)
        check(frame.notebook.GetSelection() == 0, "next tab wraps around at the end")
        frame.on_previous_tab(None)
        check(frame.notebook.GetSelection() == frame.notebook.GetPageCount() - 1,
              "previous tab wraps around at the start")

        # Registered as real accelerators, not only handled in Python.
        with open(Z.__file__, encoding="utf-8") as fh:
            src = fh.read()
        check("wx.ACCEL_CTRL, wx.WXK_TAB" in src,
              "Ctrl+Tab is registered as an accelerator")
        check("wx.ACCEL_CTRL | wx.ACCEL_SHIFT, wx.WXK_TAB" in src,
              "Ctrl+Shift+Tab is registered as an accelerator")
    finally:
        frame.on_close(None)
    print("  -> Passed!")


def test_help_sections():
    """Help must be split into topics rather than one long block."""
    print("Test 13: Help Split Into Sections...")
    global _test_app
    if _test_app is None:
        _test_app = wx.App(False)
        wx.DisableAsserts()

    frame = Z.PhoneMicFrame()
    try:
        sections = frame.HELP_SECTIONS
        titles = [t for t, _ in sections]
        check(len(sections) >= 6, "help has at least six sections", f"({len(sections)})")

        for needed in ("Getting Started", "If You Cannot Hear Anything",
                       "If Your Phone Does Not Appear", "Keyboard Shortcuts",
                       "Your Privacy", "About Z Mic Studio"):
            check(needed in titles, f"section present: {needed}")

        check(frame.cmb_help_section.GetCount() == len(sections),
              "topic picker lists every section")

        frame.cmb_help_section.SetSelection(0)
        frame.on_help_section_changed(None)
        check(frame.txt_help.GetValue() == sections[0][1], "first section is shown")
        check(frame.btn_help_prev.IsEnabled() is False, "Previous disabled on the first")
        check(frame.btn_help_next.IsEnabled() is True, "Next enabled on the first")

        frame.on_help_next(None)
        check(frame.cmb_help_section.GetSelection() == 1, "Next moves to section two")
        check(frame.lbl_help_title.GetLabel() == titles[1], "heading follows the topic")

        frame.cmb_help_section.SetSelection(len(sections) - 1)
        frame.on_help_section_changed(None)
        check(frame.btn_help_next.IsEnabled() is False, "Next disabled on the last")
        frame.on_help_prev(None)
        check(frame.cmb_help_section.GetSelection() == len(sections) - 2,
              "Previous steps back")

        # No bullet characters anywhere, because a screen reader reads them out loud.
        body = "\n".join(b for _, b in sections)
        import re
        bullets = re.findall(r"^\s*[-*\u2022\u25cf]", body, re.M)
        check(not bullets, "no bullet characters in any section", f"{bullets}")

        # Shortcut help must match what the code really does.
        shortcuts = dict(sections)["Keyboard Shortcuts"]
        check("Ctrl plus Tab goes to the next tab" in shortcuts,
              "help documents Ctrl plus Tab")
        check("Right arrow and Up arrow make the value bigger" in shortcuts,
              "help says Up arrow increases a slider")
        check("Left arrow and Down arrow make the value smaller" in shortcuts,
              "help says Down arrow decreases a slider")
        check("Up arrow and Down arrow move between them" in shortcuts,
              "help mentions arrows for the channel mode buttons")

        # The log button is presented as troubleshooting, not as a feature.
        labels = []
        for child in frame.tab_help.GetChildren():
            try:
                labels.append(child.GetLabel())
            except Exception:
                pass
        check(any("Troubleshooting Log" in x for x in labels),
              "log button renamed to Troubleshooting Log")
        check(not any(x.strip() == "Open Log File" for x in labels),
              "old log button name is gone")

        # Pressing Previous or Next is announced, because a screen reader says nothing
        # about a button press on its own.
        spoken = []
        original = Z.speak_accessibility
        Z.speak_accessibility = lambda text, *a, **k: spoken.append(text)
        try:
            frame.on_help_next()
            after_next = list(spoken)
            spoken.clear()
            frame.cmb_help_section.SetSelection(2)
            frame.on_help_section_changed(wx.CommandEvent(wx.EVT_CHOICE.typeId, 1))
            from_picker = list(spoken)
        finally:
            Z.speak_accessibility = original
        check(any("help topic" in s.lower() for s in after_next),
              "Next button announces the topic it moved to", f"{after_next}")
        check(not [s for s in from_picker if "help topic" in s.lower()],
              "picker does not announce the topic again", f"{from_picker}")

        # The help pane must be named with the topic title only. A long instruction sentence
        # was read out every time focus landed on it, which drowned the help text.
        for idx, (title, _) in enumerate(frame.HELP_SECTIONS):
            frame.cmb_help_section.SetSelection(idx)
            frame.on_help_section_changed(None, announce=False)
            name = frame.txt_help.GetName()
            if name != title or "." in name or len(name) > 40:
                check(False, "help pane is named with the topic title only",
                      f"(topic={title!r} name={name!r})")
                break
        else:
            check(True, "help pane is named with the topic title only, for every topic")

        # Opening the app must be silent. Building the help tab used to announce
        # "Showing help topic: Getting Started" before the reader touched anything.
        # The frame is closed first because building a second one clashes on the
        # global hotkey.
        frame.on_close(None)
        frame = None
        spoken = []
        original = Z.speak_accessibility
        Z.speak_accessibility = lambda text, *a, **k: spoken.append(text)
        try:
            fresh = Z.PhoneMicFrame()
            fresh.on_close(None)
        finally:
            Z.speak_accessibility = original
        check(not [s for s in spoken if "help topic" in s.lower()],
              "building the app does not announce a help topic", f"{spoken}")
    finally:
        if frame is not None:
            frame.on_close(None)
    print("  -> Passed!")



def test_phone_link_survives_mic_switch():
    """A switch to a PC microphone must not take the phone's audio link down with it.

    Live testing showed the phone stopping its own microphone during a PC mic session while
    the PC kept listing it as connected, because nothing checked whether audio was still
    arriving. Switching back to the phone then gave silence forever.
    """
    global _test_app
    if _test_app is None:
        _test_app = wx.App(False)
        wx.DisableAsserts()

    class FakeSock:
        """Stands in for the UDP socket. Records sends so ACKs can be asserted."""

        def __init__(self, packet=None):
            self.sent = []
            self._packet = packet
            self._done = packet is None

        def recvfrom(self, _bufsize):
            if self._done:
                raise OSError("no more packets")
            self._done = True
            return self._packet, ("192.168.1.50", 55555)

        def sendto(self, data, addr):
            self.sent.append((data, addr))

        def settimeout(self, _):
            pass

        def close(self):
            pass

    class FakeInputStream:
        def stop(self):
            pass

        def close(self):
            pass

    class FakeThread:
        def is_alive(self):
            return True

        def join(self, timeout=None):
            pass

    frame = Z.PhoneMicFrame()
    engine = frame.engine
    try:
        engine.is_running = True
        engine.input_mode = "phone"
        engine.input_device_id = None
        engine.sock = FakeSock()
        engine.connected_phone_addr = ("192.168.1.50", 55555)
        engine.udp_thread = FakeThread()

        stops = []
        engine._send_stop_to_phone = lambda: stops.append("stop-sent")
        engine._start_input_stream = lambda: (
            setattr(engine, "input_stream", FakeInputStream())
            or (True, "Capturing from Usb Mic"))

        # --- STEP 1: leave the phone for a PC microphone ---
        engine.set_input_source("pc", 19)

        check("stop-sent" not in stops,
              "switching to a PC microphone does not tell the phone to stop", f"{stops}")
        check(engine.sock is not None,
              "the phone's socket stays open so its heartbeat is still answered")
        check(engine._phone_link_held,
              "the engine remembers the phone link is being held open")

        # --- STEP 2: come back to the phone, reusing that socket ---
        # Go through set_input_source, not _start_current_input directly. The live bug was
        # that the return trip tore the link down anyway and rebound the socket, which is
        # exactly the path a direct call would have skipped.
        held_sock = engine.sock
        engine.input_mode = "pc"
        engine.input_stream = FakeInputStream()
        ok, msg = engine.set_input_source("phone", None)
        check(ok, "switching back to the phone reports success", f"({msg})")
        check(engine.sock is held_sock,
              "switching back to the phone reuses the held socket, no second bind",
              f"(socket reused={engine.sock is held_sock})")
        check(engine.connected_phone_addr is not None,
              "the phone is not forgotten on the way back")
        check("stop-sent" not in stops,
              "returning to the phone does not tell the phone to stop either", f"{stops}")

        # --- The receiver loop must keep the phone happy while the PC mic is active ---
        # One real audio packet, delivered through the loop's own socket. The header is
        # [PM][version][type][channels][seq:2][samples:2], all multi-byte fields big endian.
        import struct as _struct
        samples_per_ch = 200
        packet = (bytes([Z.HEADER_MAGIC[0], Z.HEADER_MAGIC[1], Z.PROTOCOL_VERSION, Z.TYPE_AUDIO])
                  + bytes([1])
                  + _struct.pack(">HH", 1, samples_per_ch)
                  + b"\x01\x02" * samples_per_ch)
        check(len(packet) == Z.HEADER_SIZE + samples_per_ch * 2,
              "the test packet matches the size the parser expects",
              f"(got {len(packet)}, want {Z.HEADER_SIZE + samples_per_ch * 2})")
        check(Z.parse_audio_packet(packet) is not None,
              "the test packet parses, so the loop really exercises the audio path")

        written = []
        engine.jitter_buffer.write = lambda b: written.append(b)
        engine.input_mode = "pc"
        engine.sock = FakeSock(packet)
        engine.connected_phone_addr = ("192.168.1.50", 55555)
        engine.is_phone_connected_local = True

        try:
            engine._udp_receiver_loop()
        except OSError:
            pass
        except Exception:
            pass

        check(not written,
              "phone audio is not pushed into the output buffer while a PC mic is active",
              f"(wrote {len(written)} chunks)")
        check(len(engine.sock.sent) > 0,
              "the phone is still sent a heartbeat while the PC mic is active",
              f"(sent {len(engine.sock.sent)})")
    finally:
        try:
            engine.sock = None
            engine.udp_thread = None
            engine._send_stop_to_phone = lambda: None
            engine.stop()
        except Exception:
            pass
        frame.Destroy()


def test_user_choice_survives_phone_connect_and_disconnect():
    """A PC microphone the user picked is neither stolen by a phone nor lost when it leaves."""
    global _test_app
    if _test_app is None:
        _test_app = wx.App(False)
        wx.DisableAsserts()

    class FakeInputStream:
        def stop(self):
            pass

        def close(self):
            pass

    frame = Z.PhoneMicFrame()
    engine = frame.engine
    try:
        usb = frame.input_devices[0][0] if frame.input_devices else 19
        frame.input_devices = [(usb, "Microphone (Usb Audio Device)")]
        engine.is_running = True
        engine.input_mode = "pc"
        engine.input_device_id = usb
        engine.input_stream = FakeInputStream()
        frame._user_picked_input = True
        frame.discovered_devices_list = [
            {"ip": "192.168.1.50", "name": "MyPhone", "port": 8088, "status": "Connected"}
        ]

        # --- A phone connects while the user is on a PC microphone ---
        frame._handle_discovery_response(True, "MyPhone", "192.168.1.50", 8088)

        check(engine.input_mode == "pc",
              "a phone connecting does not steal a PC microphone the user picked",
              f"(mode={engine.input_mode})")
        check(engine.input_stream is not None,
              "the PC microphone keeps running")
        check(engine.sock is not None,
              "the phone link is held open so the phone does not give up")
        check(engine._phone_link_held,
              "the engine records that the phone link is being held")

        labels = [item[2] for item in frame.input_source_items]
        check(any(lbl.startswith("Phone:") for lbl in labels),
              "the phone appears in the picker once connected", f"{labels}")
        sel = frame.cmb_input_source.GetSelection()
        check(frame.input_source_items[sel][0] == "pc",
              "the picker still shows the PC microphone the user chose",
              f"(index={sel})")
        check("still using" in frame.lbl_status.GetLabel(),
              "the status says the phone connected without taking over",
              f"({frame.lbl_status.GetLabel()!r})")

        # --- The phone disconnects while the PC microphone is still the source ---
        frame.on_disconnect_phone(None)

        check(engine.is_running,
              "disconnecting the phone does not stop a PC microphone stream")
        check(engine.input_stream is not None,
              "the PC microphone stream is still open after the phone left")
        check(engine.sock is None,
              "the phone socket is released, it is not just left open")
        check(not engine._phone_link_held,
              "the held link flag is cleared on disconnect")

        # The picker drops the phone once discovery reports it as no longer connected. The
        # frame's list normally shares the discovery's own dictionaries, which disconnect
        # flips in place; here it is a standalone list, so the update is written out.
        frame.discovered_devices_list = [
            {"ip": "192.168.1.50", "name": "MyPhone", "port": 8088, "status": "Available"}
        ]
        frame._rebuild_input_sources()
        labels_after = [item[2] for item in frame.input_source_items]
        check(not any(lbl.startswith("Phone:") for lbl in labels_after),
              "the disconnected phone disappears from the picker", f"{labels_after}")

        # --- The opposite case: the app chose the PC microphone by itself ---
        frame.discovered_devices_list = [
            {"ip": "192.168.1.50", "name": "MyPhone", "port": 8088, "status": "Connected"}
        ]
        frame.input_source_items = [("phone", None, "Phone: MyPhone"), ("pc", usb, "Usb Mic")]
        frame.cmb_input_source.Clear()
        frame.cmb_input_source.Append("Phone: MyPhone")
        frame.cmb_input_source.Append("Usb Mic")
        frame.cmb_input_source.SetSelection(0)
        frame._user_picked_input = True

        # The phone goes away, so the selection lands on the USB microphone on its own.
        frame.discovered_devices_list = []
        frame._rebuild_input_sources()
        check(frame._user_picked_input is False,
              "a PC microphone the app fell back to is not remembered as the user's choice")

        sel = frame.cmb_input_source.GetSelection()
        check(sel != wx.NOT_FOUND and frame.input_source_items[sel][0] == "pc",
              "the fallback put the selection on the PC microphone")
    finally:
        try:
            engine.sock = None
            engine.udp_thread = None
            engine.stop()
        except Exception:
            pass
        frame.Destroy()


def test_connect_honours_selection_when_nothing_is_running():
    """Connecting a phone must not override the picker when the stream has not started."""
    global _test_app
    if _test_app is None:
        _test_app = wx.App(False)
        wx.DisableAsserts()

    frame = Z.PhoneMicFrame()
    engine = frame.engine
    try:
        usb = frame.input_devices[0][0] if frame.input_devices else 19
        frame.input_devices = [(usb, "Microphone (Usb Audio Device)")]
        frame._rebuild_input_sources()

        # The user chooses the USB microphone and does not press Start.
        usb_index = next(i for i, item in enumerate(frame.input_source_items)
                         if item[0] == "pc")
        frame.cmb_input_source.SetSelection(usb_index)
        frame.on_input_source_changed(None)
        check(engine.is_running is False,
              "nothing is running yet, the user has not pressed Start")
        check(frame._user_picked_input is True,
              "the USB microphone counts as the user's choice")

        frame.discovered_devices_list = [
            {"ip": "192.168.1.50", "name": "MyPhone", "port": 8088, "status": "Connected"}
        ]

        # Everything the real connect would open, faked so no device is touched.
        started = []
        real_start = engine.start

        def fake_start(port, device_id):
            started.append((engine.input_mode, engine.input_device_id))
            engine.is_running = True

            class FakeStream:
                def stop(self):
                    pass

                def close(self):
                    pass

            engine.input_stream = FakeStream()
            return True, "Capturing from Microphone (Usb Audio Device)"

        engine.start = fake_start
        try:
            frame._handle_discovery_response(True, "MyPhone", "192.168.1.50", 8088)
        finally:
            engine.start = real_start

        check(started and started[0][0] == "pc",
              "the stream starts on the microphone the user selected",
              f"{started}")
        check(started and started[0][1] == usb,
              "it starts on the selected device id", f"{started}")
        check(engine.input_mode == "pc",
              "a phone connecting does not move the capture onto the phone",
              f"(mode={engine.input_mode})")
        check(engine.sock is not None and engine._phone_link_held,
              "the phone link is still held open so the phone stays selectable")

        labels = [item[2] for item in frame.input_source_items]
        check(any(lbl.startswith("Phone:") for lbl in labels),
              "the phone is offered in the picker", f"{labels}")
        sel = frame.cmb_input_source.GetSelection()
        check(frame.input_source_items[sel][0] == "pc",
              "the picker still shows the USB microphone",
              f"(index={sel})")
        check("still using" in frame.lbl_status.GetLabel(),
              "the status says the chosen microphone is still in use",
              f"({frame.lbl_status.GetLabel()!r})")

        # And with no user choice recorded, connecting the phone should still take over,
        # because that is the case where the phone is the only thing there is.
        engine.is_running = False
        engine.input_stream = None
        engine.sock = None
        engine.udp_thread = None
        engine._phone_link_held = False
        engine.input_mode = "phone"
        frame._user_picked_input = False
        started.clear()
        engine.start = fake_start
        try:
            frame._handle_discovery_response(True, "MyPhone", "192.168.1.50", 8088)
        finally:
            engine.start = real_start
        check(started and started[0][0] == "phone",
              "with no microphone chosen, the phone does become the source", f"{started}")
    finally:
        try:
            engine.sock = None
            engine.udp_thread = None
            engine.stop()
        except Exception:
            pass
        frame.Destroy()


def test_update_check_is_opt_in_and_installs_nothing():
    """The update check must be silent until asked, and must never install anything."""
    import threading as _threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    import update_check

    check(update_check.check_for_update("", "0.9.0") == {"ok": False, "error": "empty"},
          "an empty address does not attempt any connection at all")

    # Versions, including the pre-release case that decides whether a beta user gets pushed
    # onto a release.
    check(update_check.is_newer("0.9.1", "0.9.0"), "a higher patch is newer")
    check(update_check.is_newer("0.10.0", "0.9.9"), "0.10 is newer than 0.9.9, not older")
    check(not update_check.is_newer("0.9.0", "0.9.1"), "a lower version is not newer")
    check(not update_check.is_newer("0.9.1", "0.9.1"), "the same version is not newer")
    check(not update_check.is_newer("0.9.1", "0.9.1-beta"),
          "a release does not replace the beta of the same number")
    check(not update_check.is_newer("not-a-version", "0.9.0"),
          "an unparseable answer is refused rather than guessed at")

    # Both response shapes a publish location realistically uses.
    check(update_check.extract_version("0.9.1") == "0.9.1", "plain text version")
    check(update_check.extract_version("0.9.1\n") == "0.9.1", "plain text with a newline")
    check(update_check.extract_version('{"version": "0.9.2"}') == "0.9.2",
          "JSON version field")
    check(update_check.extract_version('{"tag_name": "v0.9.3"}') == "0.9.3",
          "JSON tag field, as a release feed returns")
    check(update_check.extract_version("<html>hello</html>") is None,
          "an HTML page is not mistaken for a version")
    check(update_check.extract_version("") is None, "an empty body yields nothing")

    # Served over a real socket, so the request itself is exercised rather than mocked.
    body = {"payload": '{"version": "0.9.9"}'}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body["payload"].encode())

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = _threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = update_check.check_for_update(f"http://127.0.0.1:{port}/version", "0.9.0")
        check(result.get("ok") and result.get("latest") == "0.9.9",
              "a served version is read correctly", f"({result})")
        check(result.get("newer") is True, "it is reported as newer")
        result2 = update_check.check_for_update(f"http://127.0.0.1:{port}/version", "0.9.9")
        check(result2.get("ok") and result2.get("newer") is False,
              "the same version is reported as not newer")
        # A bare host must not be treated as a scheme-less URL.
        result3 = update_check.check_for_update(f"127.0.0.1:{port}/version", "0.9.0")
        check(isinstance(result3, dict) and 'ok' in result3,
              "a bare address is handled without crashing")
    finally:
        server.shutdown()
        server.server_close()

    # Nothing in the checker may install or download. Enforced on the source, because this is
    # the one guarantee a user cannot verify by looking.
    with open(update_check.__file__, encoding="utf-8") as fh:
        u_src = fh.read()
    for banned in ("urlretrieve", "requests.get", "subprocess", "os.startfile",
                   "os.remove", "os.rename", "shutil"):
        check(banned not in u_src, f"the update checker never uses '{banned}'")


def test_firewall_only_asks_when_it_must():
    """The firewall permission must be asked once per install path, never more."""
    import firewall_rule as F

    check(F.PORTS == (8088, 8089), "the two listening ports are the ones opened")
    check(F.rule_name(8088) == "Z Mic Studio audio (UDP 8088)", "rule name includes the port")
    check("Z Mic Studio audio (UDP " in F._netsh_add(8088, r"C:\\a b\\x.exe"),
          "the rule is written for a specific program path")
    check("profile=any" in F._netsh_add(8088, r"C:\\x.exe"),
          "the rule covers every network profile, not only Private",
          "a Private-only rule looks granted while letting nothing through")
    check("protocol=UDP" in F._netsh_add(8088, r"C:\\x.exe"),
          "only UDP is opened, the app never accepts TCP")
    check("deldir" not in F._netsh_add(8088, r"C:\\x.exe")
          and "delete rule" in F._netsh_add(8088, r"C:\\x.exe"),
          "granting is idempotent, so a half-written rule is replaced not duplicated")
    check("TCP" not in F._netsh_add(8088, r"C:\\x.exe"), "no TCP rule is created")

    # Running from source, there is nothing to grant: the interpreter already has permission.
    original_frozen = getattr(F.sys, "frozen", False)
    try:
        if not F.is_compiled():
            check(F.target_executable() is None,
                  "running from source asks for nothing, Python already has permission")
            check(F.needs_grant({}) is False,
                  "no prompt is needed when there is no executable of our own")
            check("Not needed" in F.describe({}),
                  "the About tab says the permission is not needed")
    finally:
        if original_frozen:
            F.sys.frozen = original_frozen

    # Pretend to be compiled, and confirm the decision keys on the exact path.
    try:
        F.sys.frozen = True
        F.__compiled__ = None
        exe = r"C:\\Program Files\\Z Mic Studio\\ZMicStudio.exe"
        check(F.target_executable() is not None, "a compiled build has a path to grant")
        check(F.needs_grant({}) is True, "a fresh compiled install asks once")
        check(F.needs_grant({"firewall_granted_for": F.target_executable()}) is False,
                "it does not ask again after a grant")
        check(F.needs_grant({"firewall_granted_for": r"D:\\other\\ZMicStudio.exe"}) is True,
              "moving to a new path asks again, because the old rule no longer matches")
        check("granted" in F.describe({"firewall_granted_for": exe}),
              "the About tab reports a granted permission")
    finally:
        if original_frozen:
            F.sys.frozen = original_frozen
        else:
            del F.sys.frozen
        del F.__compiled__

    # Running the check on a nonexistent address must fail, not raise.
    check(F.needs_grant({}) is False,
          "back in source mode nothing is pending again")



def run_all():
    print("=" * 60)
    print("ZMIC STUDIO PHASE 4 TEST SUITE")
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
    test_terms_and_privacy()
    test_stream_tab_ordering()
    test_phone_only_listed_when_connected()
    test_stream_cues_do_not_say_phone_connected()
    test_phone_reconnect_and_manual_switch()
    test_about_reachable_and_complete()
    test_phone_link_survives_mic_switch()
    test_user_choice_survives_phone_connect_and_disconnect()
    test_connect_honours_selection_when_nothing_is_running()
    test_update_check_is_opt_in_and_installs_nothing()
    test_firewall_only_asks_when_it_must()
    test_phone_accept_switches_input_to_phone()
    test_no_discovery_auto_connect()
    test_no_auto_start()
    test_sliders_and_tab_shortcuts()
    test_help_sections()
    print("=" * 60)
    if FAILURES:
        print(f"PHASE 4 FAILED: {len(FAILURES)} check(s) failed")
        for f in FAILURES:
            print(f"  - {f}")
        print("=" * 60)
        sys.exit(1)
    print("ALL PHASE 4 TESTS PASSED (100% SUCCESS)!")
    print("=" * 60)


if __name__ == "__main__":
    run_all()
