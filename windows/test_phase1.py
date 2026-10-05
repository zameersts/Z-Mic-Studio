import sys
import os
import time
import socket

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import phone_mic_gui

print("=== TEST 1: Log Directory ===")
log_dir = phone_mic_gui.get_log_dir()
print(f"Log dir: {log_dir} (Exists: {os.path.exists(log_dir)})")

print("\n=== TEST 2: Output Devices & VB-Cable Detection ===")
devices, cable_idx = phone_mic_gui.get_output_devices()
print(f"Discovered {len(devices)} output devices. Auto-selected Cable index: {cable_idx}")
target_dev = devices[cable_idx][0] if cable_idx != -1 else None
cable_name = devices[cable_idx][1] if cable_idx != -1 else "None"
print(f"Target device ID: {target_dev} ({cable_name})")

print("\n=== TEST 3: AudioEngine Start & Clean Lifecycle ===")
status_history = []
packet_history = []

def on_status(state, msg):
    print(f"  [STATUS UPDATE] State=\"{state}\" | Message=\"{msg}\"")
    status_history.append((state, msg))

def on_packets(count, mode):
    print(f"  [PACKET UPDATE] Count={count} | Mode={mode}")
    packet_history.append((count, mode))

engine = phone_mic_gui.AudioEngine(on_status, on_packets)
ok, msg = engine.start(9988, target_dev)
print(f"Start Result: ok={ok}, msg={msg}")

if ok:
    print("\n=== TEST 4: Sending 2 Protocol v2 UDP Packets from Test Sender ===")
    import struct
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # Protocol v2 audio header: [PM][ver][type][ch][seq BE][samples BE] = 9 bytes
    mono_payload = b"\x00" * 960  # 480 samples mono @ 48kHz = 10ms
    hdr = struct.pack(
        '>2sBBBHH',
        phone_mic_gui.HEADER_MAGIC,
        phone_mic_gui.PROTOCOL_VERSION,
        phone_mic_gui.TYPE_AUDIO,
        1,      # mono
        1,      # sequence
        480,    # samples per channel
    )
    assert len(hdr) == phone_mic_gui.HEADER_SIZE == 9
    dummy_mono = hdr + mono_payload
    sock.sendto(dummy_mono, ("127.0.0.1", 9988))
    time.sleep(0.1)
    sock.sendto(dummy_mono, ("127.0.0.1", 9988))
    time.sleep(0.3)
    
    print("\n=== TEST 5: Silence Test (Waiting 2.4s to verify Disconnect Detection) ===")
    time.sleep(2.4)
    
    print("\n=== TEST 6: Stopping AudioEngine ===")
    engine.stop()
    sock.close()

print("\n=== VERIFICATION SUMMARY ===")
connected_seen = any(s == "Connected" for s, m in status_history)
disconnected_seen = any("Disconnected" in m for s, m in status_history)
print(f"1. Start succeeded: {ok}")
print(f"2. Connected state detected: {connected_seen}")
print(f"3. Disconnect state detected on 2s silence: {disconnected_seen}")

if ok and connected_seen and disconnected_seen:
    print("\n>>> ALL AUTOMATED TESTS PASSED WITH 100% SUCCESS! <<<")
    sys.exit(0)
else:
    print("\n>>> TEST FAILED! <<<")
    sys.exit(1)
