package com.phonemic.app.network

import android.util.Log
import com.phonemic.app.audio.AudioConfig
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.net.SocketTimeoutException

/**
 * High-performance UDP audio transmitter and bi-directional control receiver.
 *
 * The audio hot path ([send]) performs ZERO heap allocations: the transmit buffer,
 * the [DatagramPacket] wrapper and all control packets are pre-allocated in [open].
 */
class UdpAudioSender(
    private val host: String,
    private val port: Int,
    private val onError: ((String) -> Unit)? = null
) {
    init {
        require(port in 1..65535) { "Port must be in range 1-65535, got: $port" }
        require(host.isNotBlank()) { "Host must not be blank" }
    }

    private var socket: DatagramSocket? = null
    private var targetAddress: InetAddress? = null
    private var receiverThread: Thread? = null

    // ---- Pre-allocated reusable buffers (zero-allocation audio hot path) ----
    private val reusableBuffer = ByteArray(AudioConfig.MAX_PACKET_SIZE)
    private var reusablePacket: DatagramPacket? = null

    /** Reused for every outbound control datagram; only [length] changes. */
    private val controlBuffer = ByteArray(AudioConfig.MAX_PC_NAME_BYTES + AudioConfig.MIN_PACKET_SIZE)
    private val controlPacket = DatagramPacket(controlBuffer, controlBuffer.size)

    @Volatile
    private var isConnected = false

    @Volatile
    private var isPcAcknowledged = false

    @Volatile
    private var lastAckTime = 0L

    private var connectionStartTime = 0L
    private var hasReportedUnreachable = false

    /**
     * Address the PC is currently answering from. Starts as the address we connected to and
     * is updated when the same PC is seen on a second local address.
     */
    @Volatile
    private var peerAddress: InetAddress? = null

    @Volatile
    var activePcName: String = AudioConfig.DEFAULT_PC_NAME
        private set

    // Callbacks for bi-directional state sync
    var onHeartbeatReceived: ((status: Byte, vuLevel: Int) -> Unit)? = null
    var onConnectionStateChanged: ((connected: Boolean, pcName: String) -> Unit)? = null
    var onConnectionRequestReceived: ((pcName: String) -> Unit)? = null
    var onConnectionTimeout: (() -> Unit)? = null
    var onRemoteCommandReceived: ((cmd: Byte) -> Unit)? = null

    private var consecutiveFailures = 0
    private val failureThreshold = 50 // ~1 second of frames at 5ms
    private var sequenceNumber = 0

    fun open() {
        if (isConnected) return

        val resolved = try {
            InetAddress.getByName(host)
        } catch (e: Exception) {
            Log.e(TAG, "Cannot resolve host '$host'", e)
            throw IllegalArgumentException("Cannot resolve host '$host': ${e.message}", e)
        }
        // Guard against the empty-host case, which InetAddress silently resolves to loopback.
        require(!resolved.isLoopbackAddress || host == "127.0.0.1" || host == "localhost") {
            "Refusing to stream to loopback address resolved from host '$host'"
        }

        val sock = DatagramSocket()
        sock.soTimeout = 500
        socket = sock
        targetAddress = resolved
        reusablePacket = DatagramPacket(reusableBuffer, reusableBuffer.size, resolved, port)
        isConnected = true
        isPcAcknowledged = false
        lastAckTime = 0L
        connectionStartTime = System.currentTimeMillis()
        hasReportedUnreachable = false
        consecutiveFailures = 0
        sequenceNumber = 0
        peerAddress = resolved

        Log.i(TAG, "Streaming to $host:$port (${resolved.hostAddress}), protocol v${AudioConfig.PROTOCOL_VERSION}")

        receiverThread = Thread({
            val recvBuffer = ByteArray(1024)
            val recvPacket = DatagramPacket(recvBuffer, recvBuffer.size)

            while (isConnected && !sock.isClosed) {
                try {
                    sock.receive(recvPacket)

                    // A PC with two adapters in one subnet answers from whichever one the
                    // route table picks. Plugging the Ethernet cable out does not remove its
                    // address on Windows, so the PC can keep 192.168.100.2 on a dead adapter
                    // while Wi-Fi is on 192.168.100.5, and both claim 192.168.100.0/24 with the
                    // same metric. Rejecting the second address meant the phone threw away
                    // every heartbeat, declared the PC gone after 2.5s and stopped streaming,
                    // which the user experiences as the phone simply having no voice.
                    //
                    // So the peer address is remembered rather than fixed, and an address on
                    // the same link is accepted and adopted. Anything else is still refused,
                    // which keeps the check meaningful against an unrelated host.
                    val from = recvPacket.address
                    if (from != peerAddress) {
                        if (isOnSameLink(from, resolved)) {
                            Log.i(
                                TAG,
                                "PC is now answering from ${from.hostAddress}, previously " +
                                    "${resolved.hostAddress}. Same link, accepting it."
                            )
                            peerAddress = from
                        } else {
                            Log.w(TAG, "Discarded control packet from unauthorized host ${from.hostAddress}")
                            continue
                        }
                    }

                    if (recvPacket.length < AudioConfig.MIN_PACKET_SIZE) continue
                    if (recvBuffer[0] != AudioConfig.MAGIC_BYTE_1 || recvBuffer[1] != AudioConfig.MAGIC_BYTE_2) continue

                    // Explicit protocol version gate -- a mismatch is never treated as audio.
                    val version = recvBuffer[AudioConfig.OFF_VERSION]
                    if (version != AudioConfig.PROTOCOL_VERSION) {
                        Log.w(
                            TAG,
                            "Discarded packet: protocol v${version.toInt() and 0xFF} but we speak " +
                                "v${AudioConfig.PROTOCOL_VERSION.toInt() and 0xFF}"
                        )
                        continue
                    }

                    when (recvBuffer[AudioConfig.OFF_TYPE]) {
                        AudioConfig.TYPE_HEARTBEAT_ACK -> {
                            val status = recvBuffer[4]
                            val vu = if (recvPacket.length >= 5) recvBuffer[5].toInt() and 0xFF else 0
                            lastAckTime = System.currentTimeMillis()
                            onHeartbeatReceived?.invoke(status, vu)

                            if (!isPcAcknowledged) {
                                isPcAcknowledged = true
                                hasReportedUnreachable = false
                                // The PC name is only trusted if it came from a CONNECTION_REQUEST.
                                // An ACK alone never authorizes anything -- the service decides.
                                onConnectionStateChanged?.invoke(true, activePcName)
                            }
                        }

                        AudioConfig.TYPE_CONNECTION_REQUEST -> {
                            val nameLen = recvBuffer[4].toInt() and 0xFF
                            val name = if (
                                recvPacket.length >= AudioConfig.MIN_PACKET_SIZE + nameLen && nameLen > 0
                            ) {
                                String(recvBuffer, AudioConfig.MIN_PACKET_SIZE, nameLen, Charsets.UTF_8)
                            } else {
                                AudioConfig.DEFAULT_PC_NAME
                            }
                            activePcName = name
                            lastAckTime = System.currentTimeMillis()
                            onConnectionRequestReceived?.invoke(activePcName)
                        }

                        AudioConfig.TYPE_REMOTE_CMD -> {
                            onRemoteCommandReceived?.invoke(recvBuffer[4])
                        }

                        else -> Log.d(TAG, "Ignoring unexpected packet type 0x${recvBuffer[3].toInt().toString(16)}")
                    }
                } catch (_: SocketTimeoutException) {
                    val now = System.currentTimeMillis()
                    if (!isPcAcknowledged && !hasReportedUnreachable && (now - connectionStartTime > 4000L)) {
                        hasReportedUnreachable = true
                        Log.w(TAG, "PC at $host:$port never acknowledged within 4s")
                        onConnectionTimeout?.invoke()
                    }

                    if (isPcAcknowledged && lastAckTime > 0 && (now - lastAckTime > 2500L)) {
                        isPcAcknowledged = false
                        hasReportedUnreachable = true
                        Log.w(TAG, "Heartbeat lost from $host:$port")
                        onConnectionStateChanged?.invoke(false, "")
                    }
                } catch (e: Exception) {
                    if (isConnected) Log.w(TAG, "Control receiver loop ending", e)
                    break
                }
            }
        }, "ZMic-UdpControlReceiver").apply {
            isDaemon = true
            start()
        }
    }

    /**
     * Streams one PCM frame. Allocation-free on the happy path.
     * @param samplesPerChannel samples represented by [length] (mono) or [length]/2 (stereo)
     */
    fun send(buffer: ByteArray, length: Int, isStereo: Boolean, samplesPerChannel: Int) {
        val currentSocket = socket ?: return
        val currentPacket = reusablePacket ?: return
        if (!isConnected || currentSocket.isClosed || length <= 0) return

        val totalSize = AudioConfig.HEADER_SIZE + length
        if (totalSize > reusableBuffer.size) {
            Log.e(TAG, "Dropping oversized frame: $totalSize > ${reusableBuffer.size}")
            return
        }

        try {
            // Header
            reusableBuffer[0] = AudioConfig.MAGIC_BYTE_1
            reusableBuffer[1] = AudioConfig.MAGIC_BYTE_2
            reusableBuffer[2] = AudioConfig.PROTOCOL_VERSION
            reusableBuffer[3] = AudioConfig.TYPE_AUDIO
            reusableBuffer[4] = (if (isStereo) 2 else 1).toByte()

            val seq = sequenceNumber and 0xFFFF
            sequenceNumber = (sequenceNumber + 1) and 0xFFFF
            reusableBuffer[5] = (seq shr 8).toByte()
            reusableBuffer[6] = (seq and 0xFF).toByte()

            reusableBuffer[7] = (samplesPerChannel shr 8).toByte()
            reusableBuffer[8] = (samplesPerChannel and 0xFF).toByte()

            System.arraycopy(buffer, 0, reusableBuffer, AudioConfig.HEADER_SIZE, length)

            currentPacket.length = totalSize
            currentSocket.send(currentPacket)
            consecutiveFailures = 0
        } catch (e: Exception) {
            consecutiveFailures++
            if (consecutiveFailures == failureThreshold) {
                Log.e(TAG, "Network send failure to $host:$port (${consecutiveFailures} consecutive)", e)
                onError?.invoke("Network send failure to $host:$port: ${e.localizedMessage ?: "Unknown error"}")
            }
        }
    }

    fun sendRemoteCommand(cmd: Byte) {
        sendControl(AudioConfig.TYPE_REMOTE_CMD, byteArrayOf(cmd))
    }

    fun sendConnectionResponse(allowed: Boolean) {
        sendControl(AudioConfig.TYPE_CONNECTION_RESPONSE, byteArrayOf(if (allowed) 0x01 else 0x00))
        if (allowed) {
            isPcAcknowledged = true
            hasReportedUnreachable = false
        }
    }

    /** Sends one PC heartbeat ACK: [PM][ver][TYPE_HEARTBEAT_ACK][live|muted][vu]. */
    fun sendHeartbeatAck(muted: Boolean, vuLevel: Int) {
        sendControl(
            AudioConfig.TYPE_HEARTBEAT_ACK,
            byteArrayOf(
                if (muted) AudioConfig.STATUS_MUTED else AudioConfig.STATUS_LIVE,
                (vuLevel.coerceIn(0, 255)).toByte()
            )
        )
    }

    /**
     * Single zero-allocation control transmit path shared by every control message.
     * Copies [payload] into the pre-allocated control buffer, then reuses one DatagramPacket.
     */
    private fun sendControl(type: Byte, payload: ByteArray) {
        val currentSocket = socket ?: return
        val currentAddress = targetAddress ?: return
        if (!isConnected || currentSocket.isClosed) return
        if (AudioConfig.MIN_PACKET_SIZE + payload.size > controlBuffer.size) {
            Log.e(TAG, "Control payload too large: ${payload.size}")
            return
        }

        try {
            controlBuffer[0] = AudioConfig.MAGIC_BYTE_1
            controlBuffer[1] = AudioConfig.MAGIC_BYTE_2
            controlBuffer[2] = AudioConfig.PROTOCOL_VERSION
            controlBuffer[3] = type
            System.arraycopy(payload, 0, controlBuffer, AudioConfig.MIN_PACKET_SIZE, payload.size)

            controlPacket.length = AudioConfig.MIN_PACKET_SIZE + payload.size
            controlPacket.address = currentAddress
            controlPacket.port = port
            currentSocket.send(controlPacket)
        } catch (e: Exception) {
            Log.w(TAG, "Failed to send control type 0x${type.toInt().toString(16)}", e)
        }
    }

    fun close() {
        isConnected = false
        isPcAcknowledged = false
        try {
            socket?.close()
        } catch (e: Exception) {
            Log.w(TAG, "Error closing UDP socket", e)
        } finally {
            socket = null
            targetAddress = null
            reusablePacket = null
            peerAddress = null
        }

        // Join the control receiver so it cannot mutate state belonging to a future session.
        val thread = receiverThread
        receiverThread = null
        if (thread != null && thread !== Thread.currentThread()) {
            try {
                thread.join(600)
            } catch (e: InterruptedException) {
                Log.w(TAG, "Interrupted while joining control receiver", e)
                Thread.currentThread().interrupt()
            }
        }
    }

    companion object {
        private const val TAG = "ZMicUdpSender"

        /**
         * True when [candidate] is an IPv4 host on the same /24 link as [reference].
         *
         * Only private and link-local addresses qualify, so a host out on the internet can
         * never satisfy this however it routes. Someone already on the local network could,
         * but such a host could equally spoof the exact PC address, so this does not weaken
         * anything that the address check was actually protecting against.
         */
        private fun isOnSameLink(candidate: InetAddress, reference: InetAddress): Boolean {
            val c = candidate.address
            val r = reference.address
            if (c.size != 4 || r.size != 4) return false
            if (!candidate.isSiteLocalAddress && !candidate.isLinkLocalAddress) return false
            for (i in 0..2) {
                if (c[i] != r[i]) return false
            }
            return true
        }
    }
}
