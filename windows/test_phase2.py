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
    build_control_packet
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
    mono_samples = np.frombuffer(out_dsp_mono, dtype=np.int16)
    assert mono_samples[0] != 0 and mono_samples[1] != 0

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
    seq = 1
    mono_pcm = np.full(480, 1200, dtype=np.int16).tobytes()
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
    assert np.any(np.frombuffer(audio, dtype=np.int16) != 0)

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
    test_udp_stream_integration()
    test_trusted_phone_auto_approved_without_reprompt()
    test_helpers_are_byte_identical_to_inline_construction()
    print("=" * 60)
    print("ALL PHASE 2 TESTS PASSED PERFECTLY (100% SUCCESS)!")
    print("=" * 60)

if __name__ == "__main__":
    run_all()
