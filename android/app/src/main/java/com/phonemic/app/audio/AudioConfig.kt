package com.phonemic.app.audio

import android.media.AudioFormat

/**
 * Audio hardware, capture and wire-protocol parameters for high-fidelity low-latency streaming.
 *
 * PROTOCOL v2 (9-byte header) -- version byte and type byte are now SEPARATE fields.
 * Protocol v1 reused offset 2 for both "version" and "type", which only worked because
 * TYPE_AUDIO happened to equal 1. v2 removes that coincidence entirely.
 */
object AudioConfig {
    const val SAMPLE_RATE = 48000
    const val AUDIO_FORMAT = AudioFormat.ENCODING_PCM_16BIT

    // Channel configurations
    const val CHANNEL_CONFIG_STEREO = AudioFormat.CHANNEL_IN_STEREO
    const val CHANNELS_STEREO = 2

    const val CHANNEL_CONFIG_MONO = AudioFormat.CHANNEL_IN_MONO
    const val CHANNELS_MONO = 1

    const val BYTES_PER_SAMPLE = 2 // 16-bit PCM = 2 bytes

    // MTU-Optimized Frame Durations (Zero Wi-Fi IP fragmentation: strictly < 1472 bytes MTU payload)
    // 10ms Mono  = 480 samples * 1ch * 2 bytes =  960 bytes payload
    // 5ms Stereo = 240 samples * 2ch * 2 bytes =  960 bytes payload
    const val SAMPLES_PER_FRAME_MONO_10MS = 480   // 48000 * 0.010
    const val SAMPLES_PER_FRAME_STEREO_5MS = 240  // 48000 * 0.005

    // 960 bytes per frame -- both Mono and Stereo fit completely inside a standard 1500-byte
    // MTU without IP fragmentation (968-byte datagram with the v2 header).
    const val FRAME_BYTES_STEREO = SAMPLES_PER_FRAME_STEREO_5MS * BYTES_PER_SAMPLE * CHANNELS_STEREO // 960
    const val FRAME_BYTES_MONO = SAMPLES_PER_FRAME_MONO_10MS * BYTES_PER_SAMPLE * CHANNELS_MONO       // 960

    // Maximum datagram size (9-byte header + 960-byte payload = 969, rounded up for headroom)
    const val MAX_PACKET_SIZE = 2048

    // ---------------------------------------------------------------------
    // Protocol v2 Binary Header (9 bytes)
    //   [0]      Magic byte 1  'P' (0x50)
    //   [1]      Magic byte 2  'M' (0x4D)
    //   [2]      Protocol version
    //   [3]      Packet type
    //   [4]      Channels (audio) / reserved (control)
    //   [5..6]   Sequence number  (uint16, big-endian)
    //   [7..8]   Samples per channel (uint16, big-endian) -- audio only
    // ---------------------------------------------------------------------
    const val MAGIC_BYTE_1: Byte = 0x50 // 'P'
    const val MAGIC_BYTE_2: Byte = 0x4D // 'M'

    /** Wire protocol version. The PC receiver rejects any other value explicitly. */
    const val PROTOCOL_VERSION: Byte = 0x02
    const val HEADER_SIZE = 9

    // Header field offsets
    const val OFF_VERSION = 2
    const val OFF_TYPE = 3
    const val OFF_CHANNELS = 4
    const val OFF_SEQUENCE = 5      // uint16 big-endian
    const val OFF_SAMPLES = 7       // uint16 big-endian
    const val MIN_PACKET_SIZE = 4   // magic(2) + version(1) + type(1)

    // Packet types for bi-directional handshake and audio
    const val TYPE_AUDIO: Byte = 0x01                // Phone -> PC: [PM][2][1][ch][seq][samples] + PCM
    const val TYPE_HEARTBEAT_ACK: Byte = 0x02        // PC -> Phone: [PM][2][2][muted][vu]
    const val TYPE_CONNECTION_REQUEST: Byte = 0x03   // PC -> Phone: [PM][2][3][name_len] + UTF-8 name
    const val TYPE_CONNECTION_RESPONSE: Byte = 0x04  // Phone -> PC: [PM][2][4][1=Allowed/0=Declined]
    const val TYPE_REMOTE_CMD: Byte = 0x05           // PC <-> Phone: [PM][2][5][1=Mute/2=Unmute/3=Stop]

    // Heartbeat ACK status byte
    const val STATUS_LIVE: Byte = 0x01
    const val STATUS_MUTED: Byte = 0x02

    // Remote Commands
    const val CMD_MUTE: Byte = 0x01
    const val CMD_UNMUTE: Byte = 0x02
    const val CMD_STOP: Byte = 0x03

    const val DEFAULT_PORT = 8088
    const val DISCOVERY_PORT = 8089

    /** Maximum PC hostname length we are willing to encode into a single control datagram. */
    const val MAX_PC_NAME_BYTES = 32

    // UDP Discovery & Pairing Signatures (ASCII, UTF-8, port 8089)
    const val BEACON_PREFIX = "PM_BEACON:"
    const val CONNECT_PREFIX = "PM_CONNECT:"
    const val ACCEPT_PREFIX = "PM_ACCEPT:"
    const val DECLINE_PREFIX = "PM_DECLINE:"
    const val FORGET_PREFIX = "PM_FORGET:"

    /** Fallback label when the real PC name has not been learned yet. */
    const val DEFAULT_PC_NAME = "Windows PC"
}
