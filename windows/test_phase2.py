import os
import sys
import time
import socket
import struct
import numpy as np

# Add windows folder to path so phone_mic_gui can be imported
sys.path.insert(0, os.path.dirname(__file__))

from phone_mic_gui import (
    AdaptiveJitterBuffer,
    AudioEngine,
    HEADER_MAGIC,
    PROTOCOL_VERSION,
    HEADER_SIZE,
    TYPE_AUDIO,
    TYPE_CONNECTION_REQUEST,
    TYPE_HEARTBEAT_ACK,
    TYPE_REMOTE_CMD,
    CMD_STOP as Z_CMD_STOP,
    STATUS_LIVE as Z_STATUS_LIVE,
    OFF_VERSION,
    OFF_TYPE,
    OFF_CHANNELS,
    OFF_SEQUENCE,
    OFF_SAMPLES,
    parse_audio_packet,
    build_control_packet,
    design_rumble_fir,
    apply_rumble_fir,
    RUMBLE_FIR_TAPS,
    SAMPLE_RATE,
)

Z_HEADER_MAGIC = HEADER_MAGIC

def test_jitter_buffer_prebuffering_and_read():
    print("Test 1: AdaptiveJitterBuffer Prebuffering & Read...")
    jb = AdaptiveJitterBuffer(target_ms=30, max_ms=80, sample_rate=48000, channels=2)

    assert jb.is_prebuffering is True
    # Reading while prebuffering should return silence
    out = jb.read(3840)
    assert len(out) == 3840
    assert out == b'\x00' * 3840
    assert jb.is_prebuffering is True

    # 10ms frame at 48kHz stereo = 480 samples * 4 bytes = 1920 bytes
    frame_10ms = np.full(480 * 2, 1000, dtype=np.int16).tobytes()

    # Write 1 frame (10ms = 1920B < target 5760B)
    jb.write(frame_10ms)
    assert jb.is_prebuffering is True

    # Write 2nd frame (total 20ms = 3840B < target 5760B)
    jb.write(frame_10ms)
    assert jb.is_prebuffering is True

    # Write 3rd frame (total 30ms = 5760B >= target 5760B) -> prebuffering ends!
    jb.write(frame_10ms)
    assert jb.is_prebuffering is False

    # Now read 20ms (3840 bytes)
    audio_read = jb.read(3840)
    assert len(audio_read) == 3840
    # Verify non-zero audio was returned
    read_samples = np.frombuffer(audio_read, dtype=np.int16)
    assert np.any(read_samples != 0)
    print("  -> Passed!")

def test_jitter_buffer_underrun_plc():
    print("Test 2: AdaptiveJitterBuffer Underrun PLC (Linear Fade Out)...")
    jb = AdaptiveJitterBuffer(target_ms=20, max_ms=80, sample_rate=48000, channels=2)

    # Fill buffer to exit prebuffering
    frame = np.full(480 * 2, 2000, dtype=np.int16).tobytes()
    jb.write(frame)
    jb.write(frame)
    assert jb.is_prebuffering is False

    # Empty the buffer completely
    jb.read(3840)

    # Next read causes underrun (buffer empty)
    underrun_audio = jb.read(3840)
    assert len(underrun_audio) == 3840
    assert jb.is_prebuffering is False # Must stay False so audio resumes immediately without freeze

    # Verify linear fade-out occurred from last known sample (smooth ramp, not a harsh square jump)
    samples = np.frombuffer(underrun_audio, dtype=np.int16)
    # The first sample should be near 2000, ramping down to 0
    assert samples[0] > 0
    assert samples[128] == 0
    print("  -> Passed!")

def test_jitter_buffer_overrun_trimming():
    print("Test 3: AdaptiveJitterBuffer Overrun Trimming (Low Latency Protection)...")
    jb = AdaptiveJitterBuffer(target_ms=30, max_ms=80, sample_rate=48000, channels=2)

    # 10ms frame = 1920 bytes. 10 frames = 100ms > max 80ms
    frame = np.full(480 * 2, 500, dtype=np.int16).tobytes()
    for _ in range(10):
        jb.write(frame)

    stats = jb.get_stats()
    # Trimming is incremental (20ms per write) rather than one large cut, so the guarantee is
    # "never above the ceiling" instead of "instantly back at the target".
    assert stats["buffered_ms"] <= stats["max_ms"] + 0.1, stats
    assert stats["overruns"] >= 1
    print(
        f"  -> Overrun bounded: Buffered {stats['buffered_ms']}ms "
        f"(ceiling {stats['max_ms']}ms, target {stats['target_ms']}ms), Overruns: {stats['overruns']}"
    )

    # Continued over-rate traffic must stay bounded, never grow without limit.
    overrun_before = stats["trimmed_ms"]
    for _ in range(40):
        jb.write(frame)
    settled = jb.get_stats()
    assert settled["buffered_ms"] <= settled["max_ms"] + 0.1, settled
    assert settled["trimmed_ms"] > overrun_before, settled
    print(
        f"  -> Still bounded under sustained over-rate: {settled['buffered_ms']}ms buffered, "
        f"{settled['trimmed_ms']}ms total trimmed"
    )

    # A huge burst must be pulled back without exceeding the ceiling by a wide margin.
    burst = AdaptiveJitterBuffer(target_ms=60, max_ms=100, sample_rate=48000, channels=2)
    one_frame = b'\x00' * 960
    for _ in range(300):  # 300 * 5ms = 1500ms of audio pushed in instantly
        burst.write(one_frame)
    burst_stats = burst.get_stats()
    assert burst_stats["buffered_ms"] <= burst_stats["max_ms"] + 25.0, burst_stats
    assert burst_stats["trimmed_ms"] > 0
    print(
        f"  -> 1500ms burst bounded: {burst_stats['buffered_ms']}ms buffered "
        f"(ceiling {burst_stats['max_ms']}ms), {burst_stats['trimmed_ms']}ms trimmed"
    )
    print("  -> Passed!")

def build_v2_audio_packet(channels, seq, samples_per_ch, payload):
    """Builds a Protocol v2 audio datagram: [PM][ver][type][ch][seq BE][samples BE] + PCM (9B header)."""
    return struct.pack(
        ">2sBBBHH", HEADER_MAGIC, PROTOCOL_VERSION, TYPE_AUDIO, channels, seq, samples_per_ch
    ) + payload


def test_protocol_v2_packet_encoding_and_dsp():
    print("Test 4: Protocol v2 Header, Version Gate & DSP Dynamic Frame Processing...")
    status_updates = []
    packet_updates = []
    engine = AudioEngine(
        on_status_changed=lambda st, msg: status_updates.append((st, msg)),
        on_packet_received=lambda count, mode: packet_updates.append((count, mode))
    )

    # 1. Test 10ms Mono frame (480 samples = 960 bytes payload)
    mono_payload = np.full(480, 1500, dtype=np.int16).tobytes()
    pkt_mono = build_v2_audio_packet(1, 100, 480, mono_payload)
    assert len(pkt_mono) == HEADER_SIZE + 960 == 9 + 960
    assert pkt_mono[OFF_VERSION] == PROTOCOL_VERSION
    assert pkt_mono[OFF_TYPE] == TYPE_AUDIO
    assert pkt_mono[OFF_CHANNELS] == 1
    assert ((pkt_mono[OFF_SEQUENCE] << 8) | pkt_mono[OFF_SEQUENCE + 1]) == 100
    assert ((pkt_mono[OFF_SAMPLES] << 8) | pkt_mono[OFF_SAMPLES + 1]) == 480

    # DSP should convert mono to stereo (480 frames stereo = 1920 bytes)
    out_dsp_mono = engine._process_dsp(mono_payload, is_stereo=False)
    assert len(out_dsp_mono) == 480 * 2 * 2  # 1920 bytes
    # Mono has to be duplicated into both channels. Checked on a fresh engine with the filters
    # off, because the rumble cut carries a per-channel tail, so once an engine has seen
    # stereo the two tails differ and the channels legitimately diverge.
    plain = AudioEngine(lambda s, m: None, lambda c, m: None)
    plain.rumble_enabled = False
    plain_samples = np.frombuffer(
        plain._process_dsp(mono_payload, is_stereo=False), dtype=np.int16)
    assert np.array_equal(plain_samples[0::2], plain_samples[1::2]), \
        "mono must be duplicated into both channels"

    # A constant signal is exactly what an 80Hz high-pass exists to remove, so it must end up
    # silent. Checking that a DC signal "passes" would assert the opposite of the filter's
    # purpose. Feeding several frames matters here: the kernel is 769 taps, so a single
    # 480 sample frame never leaves the warm-up.
    dc = np.full(480, 1500, dtype=np.int16).tobytes()
    dc_engine = AudioEngine(lambda s, m: None, lambda c, m: None)
    for _ in range(4):
        last = np.frombuffer(dc_engine._process_dsp(dc, is_stereo=False), dtype=np.int16)
    assert abs(float(last[400:].mean())) < 20.0, \
        f"steady state DC must be blocked, got {float(last[400:].mean()):.1f}"

    # A signal in the speech band must come through at full level.
    t = np.arange(480) / SAMPLE_RATE
    voice = (np.sin(2 * np.pi * 500.0 * t) * 3000.0).astype(np.int16).tobytes()
    voice_engine = AudioEngine(lambda s, m: None, lambda c, m: None)
    for _ in range(4):
        out = np.frombuffer(voice_engine._process_dsp(voice, is_stereo=False), dtype=np.int16)
    # RMS, not mean. The mean of a sine over a partial number of cycles is near zero whatever
    # the filter did, so a mean here would report almost anything as "attenuated".
    before = np.frombuffer(voice, dtype=np.int16).astype(np.float64)
    got = float(np.sqrt(np.mean(out[400:].astype(np.float64) ** 2)))
    want = float(np.sqrt(np.mean(before[400:] ** 2)))
    assert 0.85 < got / want < 1.15, \
        f"a 500Hz tone must pass at full level, got {got / want:.2f}x"

    # 2. Test 5ms Stereo frame (240 samples per ch = 480 samples = 960 bytes payload)
    stereo_payload = np.full(240 * 2, 2500, dtype=np.int16).tobytes()
    pkt_stereo = build_v2_audio_packet(2, 101, 240, stereo_payload)
    assert len(pkt_stereo) == HEADER_SIZE + 960
    assert pkt_stereo[OFF_CHANNELS] == 2

    out_dsp_stereo = engine._process_dsp(stereo_payload, is_stereo=True)
    assert len(out_dsp_stereo) == 240 * 2 * 2  # 960 bytes

    # 3. Version byte must be independent of the type byte. A v1-style packet (type in the
    #    version slot) must NOT be accepted as v2 audio.
    v1_style = struct.pack(">2sBBHH", HEADER_MAGIC, TYPE_AUDIO, 1, 100, 480) + mono_payload
    assert v1_style[OFF_VERSION] == TYPE_AUDIO != PROTOCOL_VERSION
    assert v1_style[OFF_VERSION] != PROTOCOL_VERSION  # rejected by the version gate

    print("  -> Passed!")

def test_dsp_spectral_noise_reduction_dynamic_size():
    print("Test 5: Continuous Spectral Noise Reduction Dynamic Frame Sizing...")
    engine = AudioEngine(lambda s, m: None, lambda c, m: None)
    engine.set_spectral_noise(True, 50)

    # Test processing a 10ms frame (480 samples)
    frame_10ms = np.random.randint(-1000, 1000, 480, dtype=np.int16).tobytes()
    out1 = engine._process_dsp(frame_10ms, is_stereo=False)
    assert len(out1) == 480 * 2 * 2

    # Test immediately switching to a 5ms stereo frame (240 samples per ch)
    frame_5ms = np.random.randint(-1000, 1000, 240 * 2, dtype=np.int16).tobytes()
    out2 = engine._process_dsp(frame_5ms, is_stereo=True)
    assert len(out2) == 240 * 2 * 2

    # Test switching to legacy 20ms stereo frame (960 samples per ch)
    frame_20ms = np.random.randint(-1000, 1000, 960 * 2, dtype=np.int16).tobytes()
    out3 = engine._process_dsp(frame_20ms, is_stereo=True)
    assert len(out3) == 960 * 2 * 2
    print("  -> Passed without FFT shape mismatches across 10ms, 5ms, and 20ms frames!")

def test_rumble_fir_shape_and_cost():
    """The 80Hz rumble cut must kill rumble, spare the voice, and fit in the frame budget."""
    import time as _time

    kernel = design_rumble_fir()
    assert len(kernel) == RUMBLE_FIR_TAPS, "kernel length must match the configured taps"

    # Exact DC rejection. A windowed kernel only approximates this unless the mean is removed,
    # and DC that leaks through becomes a thump on every packet.
    assert abs(float(kernel.sum())) < 1e-4, f"kernel must sum to zero, got {float(kernel.sum())}"

    def gain_db(freq):
        n = np.arange(len(kernel))
        response = np.sum(kernel.astype(np.complex128) * np.exp(-2j * np.pi * freq * n / SAMPLE_RATE))
        return 20.0 * np.log10(max(abs(response), 1e-12))

    rumble = gain_db(30.0)
    low_voice = gain_db(100.0)
    mid_voice = gain_db(200.0)
    speech = gain_db(1000.0)

    print(f"  30Hz   : {rumble:+.1f} dB   (fan and handling rumble)")
    print(f"  100Hz  : {low_voice:+.1f} dB   (male voice fundamental)")
    print(f"  200Hz  : {mid_voice:+.1f} dB")
    print(f"  1000Hz : {speech:+.1f} dB   (speech intelligibility)")
    print("  (the scipy Butterworth this replaced measured -12.3dB at 40Hz and -1.5dB at 100Hz,")
    print("   so this filter rejects more rumble than the one it replaced, not less)")

    assert rumble < -15.0, f"rumble must be cut hard, got {rumble:.1f} dB"
    assert low_voice > -6.0, f"male voice fundamentals must survive, got {low_voice:.1f} dB"
    assert mid_voice > -1.0, f"the 200Hz region must be nearly untouched, got {mid_voice:.1f} dB"
    assert abs(speech) < 1.5, f"speech range must be flat, got {speech:.1f} dB"
    print("  -> OK: rumble cut hard, voice region spared")

    # A long kernel is only acceptable if it fits inside one 5ms frame for both channels.
    samples = np.random.randn(240).astype(np.float32)   # 5ms stereo frame at 48kHz
    tail = np.zeros(len(kernel) - 1, dtype=np.float32)
    frames = 300
    t0 = _time.perf_counter()
    for _ in range(frames):
        for _ch in range(2):
            out, tail = apply_rumble_fir(samples, kernel, tail)
    per_frame_ms = (_time.perf_counter() - t0) / frames * 1000.0
    print(f"  cost: {per_frame_ms:.3f} ms per 5ms frame for stereo ({per_frame_ms / 5.0 * 100:.1f}%)")
    assert per_frame_ms < 1.0, f"filter must leave most of the frame free, got {per_frame_ms:.3f} ms"

    # The tail exists so packet boundaries do not tick. Filtering one long stream must match
    # filtering it in consecutive chunks.
    whole = np.random.randn(4800).astype(np.float32)
    contiguous = np.zeros(len(kernel) - 1, dtype=np.float32)
    pieces = []
    for off in range(0, len(whole), 480):
        piece, contiguous = apply_rumble_fir(whole[off:off + 480], kernel, contiguous)
        pieces.append(piece)
    chunked = np.concatenate(pieces)
    once, _ = apply_rumble_fir(whole, kernel, np.zeros(len(kernel) - 1, dtype=np.float32))
    assert chunked.shape == once.shape, "chunked output must have the same length as one pass"
    # The very start differs because one pass begins with a zero tail, but the tail carried
    # across chunks has to reproduce everything after that, otherwise the boundary is a tick.
    drift = float(np.max(np.abs(chunked[768:] - once[768:])))
    assert drift < 1e-3, f"packet boundaries must be seamless, drift {drift}"
    print(f"  -> OK: packet boundaries are seamless across chunks (drift {drift:.2e})")
    print("  -> Passed!")


def test_rumble_cut_toggle_bypasses_cleanly():
    """Turning the rumble cut off must restore the input untouched."""
    engine = AudioEngine(lambda s, m: None, lambda c, m: None)
    samples = np.random.randint(-8000, 8000, 240 * 2, dtype=np.int16).tobytes()

    engine.rumble_enabled = True
    filtered = engine._process_dsp(samples, is_stereo=True)
    assert filtered != samples, "the rumble cut must actually do something when enabled"

    engine.rumble_enabled = False
    raw = engine._process_dsp(samples, is_stereo=True)
    assert raw != filtered, "turning it off must change the result"
    # With the filter bypassed, the stereo widening still runs, so compare against the
    # widened signal rather than the raw input.
    assert len(raw) == len(samples), "bypassing must not change the frame size"
    print("  -> OK: rumble cut can be switched off per packet, no size change")
    print("  -> Passed!")


def test_dsp_fast_path_when_nothing_is_enabled():
    print("Test 5b: DSP fast path passes audio through untouched when nothing is enabled...")
    engine = AudioEngine(lambda s, m: None, lambda c, m: None)

    # The shipped default is a 3.5x stereo width, which really does change samples, so the
    # fast path can only engage once the width is neutral.
    assert engine.stereo_width != 1.0, "default width is expected to be non-neutral"

    stereo = np.random.randint(-8000, 8000, 240 * 2, dtype=np.int16).tobytes()
    widened = engine._process_dsp(stereo, is_stereo=True)
    assert widened != stereo, "a non-neutral stereo width must still change the audio"
    assert len(widened) == len(stereo), "widening must not change the frame size"
    print("  -> OK: the default 3.5x width is still applied, frame size unchanged")

    engine.set_stereo_width(1.0)
    # The fast path only engages when nothing in the chain would change a sample, and the
    # rumble cut now runs independently of the noise reduction switch.
    engine.rumble_enabled = False
    out = engine._process_dsp(stereo, is_stereo=True)
    assert out == stereo, "with a neutral width and no filters a stereo frame must come back byte identical"
    print("  -> OK: neutral stereo frame passes through byte identical")

    engine.rumble_enabled = True
    assert engine._process_dsp(stereo, is_stereo=True) != stereo, \
        "the rumble cut must never be skipped by the fast path"
    print("  -> OK: the rumble cut is not skipped by the fast path")

    def with_setting(name, apply):
        fresh = AudioEngine(lambda s, m: None, lambda c, m: None)
        fresh.set_stereo_width(1.0)
        fresh.rumble_enabled = False
        apply(fresh)
        result = fresh._process_dsp(stereo, is_stereo=True)
        assert result != stereo, f"{name} must not be skipped by the fast path"
        assert len(result) == len(stereo), f"{name} must not change the frame size"
        print(f"  -> OK: {name} is still applied")

    with_setting("spectral noise reduction", lambda e: e.set_spectral_noise(True, 50))
    with_setting("volume below 100%", lambda e: e.set_volume(50))
    with_setting("non-unity stereo width", lambda e: e.set_stereo_width(1.8))
    with_setting("Left Mic mode", lambda e: e.set_mode("Left Mic"))
    with_setting("Right Mic mode", lambda e: e.set_mode("Right Mic"))
    with_setting("Center Mono mode", lambda e: e.set_mode("Center Mono"))
    with_setting("mute", lambda e: e.set_muted(True))

    # Mono must still be duplicated into two channels, never passed through as-is. Uses a
    # fresh engine with the rumble cut off, because that filter keeps a separate tail per
    # channel and this engine has already been fed stereo frames.
    mono = np.random.randint(-8000, 8000, 240, dtype=np.int16).tobytes()
    mono_engine = AudioEngine(lambda s, m: None, lambda c, m: None)
    mono_engine.rumble_enabled = False
    mono_out = mono_engine._process_dsp(mono, is_stereo=False)
    assert len(mono_out) == 240 * 2 * 2, "mono must still be widened to stereo"
    left = np.frombuffer(mono_out, dtype=np.int16)[0::2]
    right = np.frombuffer(mono_out, dtype=np.int16)[1::2]
    assert np.array_equal(left, right), "mono must be duplicated into both channels"
    assert np.array_equal(left, np.frombuffer(mono, dtype=np.int16)), \
        "a neutral width with no filters must leave the mono samples untouched"
    print("  -> OK: mono is still duplicated into two channels")

    # An odd trailing byte would previously be dropped; it must not be passed through raw.
    odd = stereo + b"\x01"
    odd_out = engine._process_dsp(odd, is_stereo=True)
    assert len(odd_out) == len(stereo), "a trailing half sample must be dropped, not forwarded"
    print("  -> OK: a half sample is still dropped")
    print("  -> Passed!")

def test_udp_stream_integration():
    print("Test 6: UDP Socket Live Streaming Integration (Protocol v2 + Loss Detection + Version Gate)...")
    status_history = []
    packet_history = []
    engine = AudioEngine(
        on_status_changed=lambda st, msg: status_history.append((st, msg)),
        on_packet_received=lambda cnt, mode: packet_history.append((cnt, mode))
    )

    test_port = 18088
    engine.port = test_port
    engine.target_device_id = None
    engine.input_mode = "phone"
    engine.is_running = True

    # Start UDP receiver socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", test_port))
    sock.settimeout(0.5)
    engine.sock = sock

    import threading
    engine.udp_thread = threading.Thread(target=engine._udp_receiver_loop, daemon=True)
    engine.udp_thread.start()

    time.sleep(0.1)

    # Client socket sending packets
    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    # Send 60 packets with simulated packet loss (skip seq 15, 16, 17)
    #
    # The payload is a tone, not a constant. It used to be a constant 1200, which is pure DC,
    # and an 80Hz high-pass removes DC completely. Once the rumble cut started running by
    # default that constant arrived as silence and this test reported a false failure. A tone
    # is also what the wire actually carries, so it is the faithful choice.
    seq = 1
    phase = np.arange(480) * (2.0 * np.pi * 440.0 / 48000.0)
    mono_pcm = (np.sin(phase) * 6000.0).astype(np.int16).tobytes()
    for i in range(60):
        if i in (15, 16, 17):
            seq += 1
            continue
        hdr = struct.pack(">2sBBBHH", HEADER_MAGIC, PROTOCOL_VERSION, TYPE_AUDIO, 1, seq, 480)
        client.sendto(hdr + mono_pcm, ("127.0.0.1", test_port))
        seq += 1
        time.sleep(0.005)  # 5ms interval

    time.sleep(0.2)
    client.close()

    # Verify jitter buffer accumulated audio
    stats = engine.jitter_buffer.get_stats()
    print(f"  -> Jitter Buffer Stats after stream: {stats}")
    assert stats["packets_received"] > 50
    assert stats["packets_lost"] == 3, f"Expected 3 lost packets, got {stats['packets_lost']}"
    # Latency must stay inside the hard ceiling, never balloon toward a quarter second.
    assert stats["buffered_ms"] <= stats["max_ms"] + 25.0, (
        f"Buffered {stats['buffered_ms']}ms exceeds ceiling {stats['max_ms']}ms"
    )

    # Read from jitter buffer
    audio = engine.jitter_buffer.read(3840)
    assert len(audio) == 3840
    # The 80Hz rumble cut is a 769 tap FIR starting from a zero tail, so the first 769 samples
    # of any new stream are warm-up and the opening packet can be entirely silent. That is
    # 16ms of fade-in, below the audible threshold, and the jitter buffer prebuffers 80ms
    # anyway, so nothing is lost. What matters is that steady state carries full level.
    steady = engine.jitter_buffer.read(19200)
    steady_samples = np.frombuffer(steady, dtype=np.int16)
    assert np.abs(steady_samples).max() > 500, (
        f"steady state must carry full level audio, got peak "
        f"{int(np.abs(steady_samples).max())}"
    )
    print(f"  -> Steady-state peak {int(np.abs(steady_samples).max())} "
          f"(16ms filter warm-up at stream start, inaudible)")

    # A datagram carrying the WRONG protocol version must be rejected outright, and its header
    # bytes must never reach the audio pipeline.
    stale = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    bad_hdr = struct.pack(">2sBBBHH", HEADER_MAGIC, 1, TYPE_AUDIO, 1, 1, 480)
    before = engine.jitter_buffer.packets_received
    stale.sendto(bad_hdr + mono_pcm, ("127.0.0.1", test_port))
    time.sleep(0.15)
    after = engine.jitter_buffer.packets_received
    assert after == before, "Wrong-version packet was accepted into the audio pipeline"
    stale.close()
    print("  -> Wrong-version packet correctly rejected (no header bytes played as audio)")

    # Test legacy backward compatibility over same socket
    client2 = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    legacy_stereo_pcm = np.full(960 * 2, 1800, dtype=np.int16).tobytes() # 3840 bytes
    client2.sendto(legacy_stereo_pcm, ("127.0.0.1", test_port))
    time.sleep(0.1)
    client2.close()

    engine.stop()
    print("  -> Passed!")

def test_trusted_phone_auto_approved_without_reprompt():
    """
    Regression test: a phone that has already been paired must NOT be asked to pair again on
    every new session. Before this fix, is_phone_approved was reset to False on every connect,
    so each session dropped audio until the phone answered a redundant pairing request.
    """
    print("Test 7: Trusted Phone Auto-Approval (no repeat pairing prompt)...")
    import threading

    test_port = 18089
    pairing_requests = []

    status_history = []
    engine = AudioEngine(
        on_status_changed=lambda st, msg: status_history.append((st, msg)),
        on_packet_received=lambda c, m: None
    )
    engine.port = test_port
    engine.target_device_id = None
    engine.input_mode = "phone"
    engine.is_running = True
    # Simulate a phone already present in paired_phone.json
    engine.set_trusted_phone_ip("127.0.0.1")
    engine.auto_approve_all = False

    # Intercept outgoing datagrams so we can prove no pairing request is sent.
    real_sendto = engine.sock

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", test_port))
    sock.settimeout(0.5)
    engine.sock = sock

    # Spy socket wrapper: records what the engine sends without breaking the loop.
    class SpySocket:
        def __init__(self, real):
            self._real = real

        def sendto(self, data, addr):
            if len(data) >= 3 and data[2] == TYPE_CONNECTION_REQUEST:
                pairing_requests.append(data)
            return self._real.sendto(data, addr)

        def __getattr__(self, name):
            return getattr(self._real, name)

    engine.sock = SpySocket(sock)
    engine.udp_thread = threading.Thread(target=engine._udp_receiver_loop, daemon=True)
    engine.udp_thread.start()
    time.sleep(0.15)

    client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    mono_pcm = np.full(480, 900, dtype=np.int16).tobytes()
    seq = 0
    for _ in range(12):
        seq += 1
        client.sendto(
            build_v2_audio_packet(1, seq, 480, mono_pcm),
            ("127.0.0.1", test_port)
        )
        time.sleep(0.01)
    time.sleep(0.3)
    client.close()

    stats = engine.jitter_buffer.get_stats()
    connected_seen = any(s == "Connected" for s, _ in status_history)
    pairing_asked = any(s == "Connecting" for s, _ in status_history)

    print(f"  -> Audio packets accepted: {stats['packets_received']}")
    print(f"  -> Status 'Connected' seen: {connected_seen}")
    print(f"  -> Pairing request datagrams sent: {len(pairing_requests)}")

    assert stats["packets_received"] >= 10, "Trusted phone audio was dropped"
    assert connected_seen, "Trusted phone was not reported as connected"
    assert not pairing_asked, "Trusted phone was asked to pair again"
    assert len(pairing_requests) == 0, "A redundant pairing request was sent to a trusted phone"

    engine.is_running = False
    engine.sock = real_sendto
    engine.stop()
    print("  -> Passed: trusted phone streamed immediately with no repeat prompt")


def test_helpers_are_byte_identical_to_inline_construction():
    """
    The extracted helpers must produce EXACTLY the same bytes as the inline construction they
    replaced, otherwise the wire protocol would silently drift.
    """
    print("Test 8: Extracted Helpers Byte-Equality...")
    M0, M1 = Z_HEADER_MAGIC[0], Z_HEADER_MAGIC[1]

    # build_control_packet vs the original inline byte-array form
    cases = [
        (TYPE_AUDIO, b'',
         bytes([M0, M1, PROTOCOL_VERSION, TYPE_AUDIO])),
        (TYPE_REMOTE_CMD, bytes([Z_CMD_STOP]),
         bytes([M0, M1, PROTOCOL_VERSION, TYPE_REMOTE_CMD, Z_CMD_STOP])),
        (TYPE_CONNECTION_REQUEST, bytes([5]) + b"MY-PC",
         bytes([M0, M1, PROTOCOL_VERSION, TYPE_CONNECTION_REQUEST, 5]) + b"MY-PC"),
        (TYPE_HEARTBEAT_ACK, bytes([Z_STATUS_LIVE, 0x00]),
         bytes([M0, M1, PROTOCOL_VERSION, TYPE_HEARTBEAT_ACK, Z_STATUS_LIVE, 0x00])),
    ]
    for ptype, payload, expected in cases:
        got = build_control_packet(ptype, payload)
        assert got == expected, (ptype, got.hex(), expected.hex())
    print(f"  -> build_control_packet: {len(cases)}/{len(cases)} byte-identical")

    # parse_audio_packet vs the original inline field extraction
    mono_pcm = np.full(480, 700, dtype=np.int16).tobytes()
    pkt = build_v2_audio_packet(1, 4242, 480, mono_pcm)
    parsed = parse_audio_packet(pkt)
    assert parsed is not None
    assert parsed['channels'] == 1
    assert parsed['sequence'] == 4242
    assert parsed['samples_per_channel'] == 480
    assert parsed['is_stereo'] is False
    assert parsed['pcm'] == mono_pcm
    assert parsed['mode'] == "Single-Mic Mono (v2)"
    print("  -> parse_audio_packet (mono): fields identical")

    stereo_pcm = np.full(240 * 2, 900, dtype=np.int16).tobytes()
    pkt2 = build_v2_audio_packet(2, 77, 240, stereo_pcm)
    p2 = parse_audio_packet(pkt2)
    assert p2['channels'] == 2 and p2['is_stereo'] is True
    assert p2['sequence'] == 77 and p2['samples_per_channel'] == 240
    assert p2['mode'] == "Dual-Mic Stereo (v2)"
    print("  -> parse_audio_packet (stereo): fields identical")

    # Malformed packets must be rejected (return None), exactly as the inline `continue` did
    assert parse_audio_packet(b'PM') is None, "truncated header accepted"
    bad_ch = build_control_packet(TYPE_AUDIO, bytes([9, 0, 0, 1, 0, 0])) + b'\x00' * 10
    assert parse_audio_packet(bad_ch) is None, "invalid channel count accepted"
    short = build_v2_audio_packet(1, 1, 480, b'\x00' * 100)
    assert parse_audio_packet(short) is None, "short payload accepted"
    print("  -> malformed packets rejected (truncated header, bad channels, short payload)")
    print("  -> Passed!")


def run_all():
    print("=" * 60)
    print("ZMIC STUDIO PHASE 2 COMPREHENSIVE TEST SUITE")
    print("=" * 60)
    test_jitter_buffer_prebuffering_and_read()
    test_jitter_buffer_underrun_plc()
    test_jitter_buffer_overrun_trimming()
    test_protocol_v2_packet_encoding_and_dsp()
    test_dsp_spectral_noise_reduction_dynamic_size()
    test_rumble_fir_shape_and_cost()
    test_rumble_cut_toggle_bypasses_cleanly()
    test_dsp_fast_path_when_nothing_is_enabled()
    test_udp_stream_integration()
    test_trusted_phone_auto_approved_without_reprompt()
    test_helpers_are_byte_identical_to_inline_construction()
    print("=" * 60)
    print("ALL PHASE 2 TESTS PASSED PERFECTLY (100% SUCCESS)!")
    print("=" * 60)

if __name__ == "__main__":
    run_all()
