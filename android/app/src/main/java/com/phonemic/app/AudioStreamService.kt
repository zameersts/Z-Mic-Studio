package com.phonemic.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.SharedPreferences
import android.content.pm.ServiceInfo
import android.graphics.drawable.Icon
import android.media.AudioAttributes
import android.media.RingtoneManager
import android.net.wifi.WifiManager
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.util.Log
import com.phonemic.app.audio.AudioConfig
import com.phonemic.app.audio.AudioRecorder
import com.phonemic.app.audio.PacedAudioSender
import com.phonemic.app.network.UdpAudioSender
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.SocketTimeoutException
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.ThreadPoolExecutor
import java.util.concurrent.TimeUnit

/**
 * Foreground Service that provides:
 *  1. Wi-Fi discovery beacon and connection handling on UDP port 8089.
 *  2. Raw PCM audio capture and UDP streaming on port 8088 (Protocol v2).
 *  3. Bi-directional heartbeats, remote mute control, and accessible TalkBack cues.
 *
 * Threading model: three threads touch this class (main, ZMicDiscovery, ZMic-UdpControlReceiver,
 * plus the ZMic-CaptureThread). Every mutating entry point is guarded by [streamLock] so that
 * AudioRecord / DatagramSocket / WakeLock instances can never be created twice nor orphaned.
 */
class AudioStreamService : Service() {

    // ---------------------------------------------------------------------
    // Resources (all guarded by streamLock)
    // ---------------------------------------------------------------------
    private var wakeLock: PowerManager.WakeLock? = null
    private var wifiLock: WifiManager.WifiLock? = null
    private var highPerfWifiLock: WifiManager.WifiLock? = null
    private var sender: UdpAudioSender? = null
    private var recorder: AudioRecorder? = null

    /** Owns the paced transmission thread. Closed before the UDP socket. */
    private var pacedSender: PacedAudioSender? = null

    private val streamLock = Any()

    // ---------------------------------------------------------------------
    // Discovery server (guarded by discoveryLock)
    // ---------------------------------------------------------------------
    private val discoveryLock = Any()
    private var discoverySocket: DatagramSocket? = null
    private var discoveryThread: Thread? = null

    @Volatile
    private var isDiscoveryRunning = false

    /** Bounded, single-threaded executor for discovery replies (no unbounded thread creation). */
    private val replyExecutor = ThreadPoolExecutor(
        1, 1, 30L, TimeUnit.SECONDS,
        ArrayBlockingQueue(8),
        { r -> Thread(r, "ZMicDiscoveryReply").apply { isDaemon = true } },
        ThreadPoolExecutor.DiscardPolicy()
    )

    // ---------------------------------------------------------------------
    // Shared state
    // ---------------------------------------------------------------------
    @Volatile
    private var isStreaming = false

    @Volatile
    private var isMuted = false

    @Volatile
    private var isConnectedToPc = false

    @Volatile
    private var isPcUnreachable = false

    @Volatile
    private var isPcAuthorized = false

    /**
     * Hostname of the peer, set ONLY after a TYPE_CONNECTION_REQUEST whose claimed name matched
     * the trusted name. An ACK proves liveness, never identity, so it must not set this.
     */
    @Volatile
    private var verifiedPcName: String = ""

    @Volatile
    private var currentHost = ""

    @Volatile
    private var currentPort = AudioConfig.DEFAULT_PORT

    @Volatile
    private var currentPcName = AudioConfig.DEFAULT_PC_NAME

    @Volatile
    private var currentFormat = FORMAT_MONO

    @Volatile
    private var lastHeartbeatSentAt = 0L

    /** Cached trusted PC name so we never hit disk on the audio/control hot path. */
    @Volatile
    private var cachedTrustedPc: String? = null
    private var trustedPcLoaded = false

    @Volatile
    private var lastSoundTime = 0L

    @Volatile
    private var lastBroadcastSignature = ""
    private var lastBroadcastTime = 0L

    /** Pre-allocated silence frame, reused whenever the mic is muted. */
    private val silenceBuffer = ByteArray(AudioConfig.MAX_PACKET_SIZE)

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        loadTrustedPc()
        createNotificationChannels()
        startDiscoveryServer()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val action = intent?.action
        Log.d(TAG, "onStartCommand action=$action")

        when (action) {
            ACTION_STOP -> {
                stopStreaming()
                stopDiscoveryServer()
                cancelNotifications()
                stopForegroundCompat()
                stopSelf()
                return START_NOT_STICKY
            }

            ACTION_FORGET_PAIRED_PC -> {
                val host = currentHost
                if (host.isNotBlank()) {
                    sendDiscoveryReply(host, AudioConfig.FORGET_PREFIX)
                }
                clearTrustedPc()
                resetPeerState()
                sender?.sendConnectionResponse(false)
                stopStreaming()
                broadcastStatus(STATUS_STOPPED)
                return START_NOT_STICKY
            }

            ACTION_ALLOW_REQUEST -> {
                cancelRequestNotification()
                val pcName = intent.getStringExtra(EXTRA_PC_NAME)?.takeIf { it.isNotBlank() }
                    ?: AudioConfig.DEFAULT_PC_NAME
                val pcIp = intent.getStringExtra(EXTRA_IP)?.takeIf { it.isNotBlank() }
                    ?: currentHost
                saveTrustedPc(pcName, pcIp)
                currentPcName = pcName
                currentHost = pcIp
                verifiedPcName = pcName
                isPcAuthorized = true
                Companion.isPcAuthorized = true
                sendDiscoveryReply(pcIp, "${AudioConfig.ACCEPT_PREFIX}$deviceDisplayName")
                playDefaultNotificationSound()
                startStreaming(pcIp, AudioConfig.DEFAULT_PORT)
                return START_NOT_STICKY
            }

            ACTION_DECLINE_REQUEST -> {
                cancelRequestNotification()
                val pcIp = intent.getStringExtra(EXTRA_IP)?.takeIf { it.isNotBlank() } ?: currentHost
                sendDiscoveryReply(pcIp, "${AudioConfig.DECLINE_PREFIX}$deviceDisplayName")
                resetPeerState()
                stopStreaming()
                return START_NOT_STICKY
            }

            ACTION_MUTE -> {
                setMuteState(true)
                return START_NOT_STICKY
            }

            ACTION_UNMUTE -> {
                setMuteState(false)
                return START_NOT_STICKY
            }

            ACTION_TOGGLE_MUTE -> {
                setMuteState(!isMuted)
                return START_NOT_STICKY
            }
        }

        // Promote to foreground before anything else, so Android 11+ never throttles the mic.
        val targetIp = intent?.getStringExtra(EXTRA_IP)?.takeIf { it.isNotBlank() } ?: currentHost
        val targetPort = intent?.getIntExtra(EXTRA_PORT, currentPort) ?: currentPort
        promoteToForeground(buildOngoingNotification(targetIp, targetPort, isMuted, FORMAT_READY, currentPcName))

        if (action == ACTION_START_DISCOVERY) {
            startDiscoveryServer()
            broadcastStatus(STATUS_STOPPED)
            return START_NOT_STICKY
        }

        if (action == ACTION_START && targetIp.isNotBlank()) {
            currentHost = targetIp
            currentPort = targetPort
            intent.getStringExtra(EXTRA_PC_NAME)?.takeIf { it.isNotBlank() }?.let {
                currentPcName = it
                verifiedPcName = it
            }
            startStreaming(targetIp, targetPort)
        }

        return START_NOT_STICKY
    }

    // =====================================================================
    // Streaming lifecycle
    // =====================================================================

    /**
     * Starts capture + UDP streaming. Safe to call from any thread and safe to call twice:
     * the [streamLock] guard plus the early claim of [isStreaming] guarantee that at most one
     * AudioRecord, DatagramSocket and WakeLock set is ever created.
     */
    private fun startStreaming(host: String, port: Int) {
        val safeHost = host.trim()
        if (safeHost.isEmpty()) {
            Log.e(TAG, "Refusing to stream: no PC address known")
            broadcastStatus(STATUS_ERROR, host, port, currentFormat, getString(R.string.error_no_pc_address))
            return
        }

        synchronized(streamLock) {
            if (isStreaming) {
                Log.i(TAG, "startStreaming($safeHost) ignored: already streaming to $currentHost")
                return
            }
            // Claim the slot BEFORE allocating anything so a concurrent caller cannot interleave.
            isStreaming = true

            val trusted = trustedPcName()
            isPcAuthorized = trusted != null
            Companion.isPcAuthorized = isPcAuthorized
            if (trusted != null) {
                currentPcName = trusted
                verifiedPcName = trusted
            }

            promoteToForeground(
                buildOngoingNotification(
                    safeHost, port, isMuted, FORMAT_CONNECTING, currentPcName
                )
            )
            acquireWakeLock()
            acquireWifiLock()

            var newSender: UdpAudioSender? = null
            var newRecorder: AudioRecorder? = null
            try {
                newSender = UdpAudioSender(safeHost, port) { errMsg ->
                    if (!isStreaming) {
                        Log.d(TAG, "Ignoring send error after stop: $errMsg")
                        return@UdpAudioSender
                    }
                    broadcastStatus(STATUS_ERROR, safeHost, port, currentFormat, errMsg)
                }

                newSender.onConnectionTimeout = {
                    isPcUnreachable = true
                    isConnectedToPc = false
                    Companion.isPcUnreachable = true
                    Companion.isConnectedToPc = false
                    updateNotification()
                    broadcastStatus(STATUS_PC_UNREACHABLE, safeHost, port, currentFormat)
                }

                newSender.onConnectionStateChanged = { connected, _ ->
                    handleAckState(connected, safeHost, port)
                }

                newSender.onConnectionRequestReceived = { pcName ->
                    handleConnectionRequest(pcName, safeHost, port)
                }

                newSender.onRemoteCommandReceived = { cmd ->
                    when (cmd) {
                        AudioConfig.CMD_MUTE -> setMuteState(true, fromRemote = true)
                        AudioConfig.CMD_UNMUTE -> setMuteState(false, fromRemote = true)
                        AudioConfig.CMD_STOP -> {
                            Log.i(TAG, "Remote stop requested by PC")
                            stopStreaming()
                            broadcastStatus(STATUS_STOPPED)
                        }
                        else -> Log.w(TAG, "Unknown remote command 0x${cmd.toInt().toString(16)}")
                    }
                }

                newSender.open()
                // Publish before starting the recorder so the capture thread always sees a sender.
                sender = newSender

                // Transmission runs on its own paced thread. Sending inline on the capture
                // thread made the arrival pattern clump badly enough to sound like a cut, and
                // let a slow send stall the microphone read entirely.
                val paced = PacedAudioSender { data, len, stereo, samples ->
                    newSender.send(data, len, stereo, samples)
                }
                pacedSender = paced
                paced.start()

                val rec = AudioRecorder { pcmBytes, bytesRead, isStereo, samplesPerChannel ->
                    if (!isStreaming) return@AudioRecorder
                    paced.enqueue(
                        if (isMuted) silenceBuffer else pcmBytes,
                        bytesRead, isStereo, samplesPerChannel
                    )
                    maybeSendHeartbeat()
                }
                rec.onCaptureError = { msg ->
                    Log.e(TAG, "Capture error: $msg")
                    broadcastStatus(STATUS_ERROR, safeHost, port, currentFormat, msg)
                    stopStreaming()
                }

                rec.start()
                newRecorder = rec
                recorder = rec

                val format = if (rec.isStereo) FORMAT_STEREO else FORMAT_MONO
                currentFormat = format
                activeHost = safeHost
                activePort = port
                activeFormat = format
                activePcName = currentPcName
                activeIsMuted = isMuted
                isServiceRunning = true

                isConnectedToPc = isPcAuthorized
                isPcUnreachable = false
                Companion.isConnectedToPc = isPcAuthorized
                Companion.isPcUnreachable = false

                cancelRequestNotification()
                updateNotification()
                broadcastStatus(STATUS_CONNECTED_PC, safeHost, port, format, currentPcName)
                Log.i(TAG, "Streaming started to $safeHost:$port as $format")
            } catch (e: Exception) {
                Log.e(TAG, "Failed to start streaming to $safeHost:$port", e)
                isStreaming = false
                isServiceRunning = false
                runCatching { newRecorder?.stop() }
                runCatching { pacedSender?.close() }
                pacedSender = null
                runCatching { newSender?.close() }
                recorder = null
                sender = null
                releaseWakeLock()
                releaseWifiLock()
                broadcastStatus(STATUS_ERROR, safeHost, port, currentFormat, e.localizedMessage)
            }
        }
    }

    /**
     * An ACK proves the peer is alive, NOT that it is the PC the user paired with.
     * Authorization therefore requires a previously verified hostname.
     */
    private fun handleAckState(connected: Boolean, host: String, port: Int) {
        if (!connected) {
            isConnectedToPc = false
            isPcUnreachable = false
            Companion.isConnectedToPc = false
            Companion.isPcUnreachable = false
            updateNotification()
            broadcastStatus(STATUS_PC_DISCONNECTED, host, port, currentFormat)
            return
        }

        val trusted = trustedPcName()
        if (trusted != null && verifiedPcName == trusted) {
            isConnectedToPc = true
            isPcUnreachable = false
            Companion.isConnectedToPc = true
            Companion.isPcUnreachable = false
            playDefaultNotificationSound()
            cancelRequestNotification()
            updateNotification()
            broadcastStatus(STATUS_CONNECTED_PC, host, port, currentFormat, currentPcName)
        } else {
            Log.w(TAG, "ACK received from $host but peer hostname is unverified - not authorizing")
        }
    }

    /**
     * A TYPE_CONNECTION_REQUEST carries the claimant's hostname. This is the only path allowed
     * to establish identity, and only if the name matches the stored trusted name.
     */
    private fun handleConnectionRequest(pcName: String, host: String, port: Int) {
        val trusted = trustedPcName()
        Log.i(TAG, "Connection request from '$pcName' ($host:$port), trusted='$trusted'")

        if (trusted != null && trusted == pcName) {
            verifiedPcName = pcName
            currentPcName = pcName
            isPcAuthorized = true
            Companion.isPcAuthorized = true
            sender?.sendConnectionResponse(true)
            isConnectedToPc = true
            isPcUnreachable = false
            Companion.isConnectedToPc = true
            Companion.isPcUnreachable = false
            playDefaultNotificationSound()
            cancelRequestNotification()
            updateNotification()
            broadcastStatus(STATUS_CONNECTED_PC, host, port, currentFormat, pcName)
        } else {
            Log.w(TAG, "Rejecting untrusted connection request from '$pcName'")
            verifiedPcName = ""
            isPcAuthorized = false
            Companion.isPcAuthorized = false
            isConnectedToPc = false
            Companion.isConnectedToPc = false
            // Use the host we are actually talking to -- never a stale field value.
            currentHost = host
            showConnectionRequestNotification(pcName, host)
            sendBroadcast(
                Intent(ACTION_CONNECTION_REQUEST).apply {
                    putExtra(EXTRA_PC_NAME, pcName)
                    putExtra(EXTRA_IP, host)
                    setPackage(packageName)
                }
            )
        }
    }

    /** Sends one heartbeat per second so the PC can tell "phone alive but silent" from "gone". */
    private fun maybeSendHeartbeat() {
        val now = System.currentTimeMillis()
        if (now - lastHeartbeatSentAt < 1000L) return
        lastHeartbeatSentAt = now
        sender?.sendHeartbeatAck(isMuted, 0)
    }

    /** Stops capture and networking and releases every resource exactly once. */
    private fun stopStreaming() {
        synchronized(streamLock) {
            val wasStreaming = isStreaming
            isStreaming = false
            isMuted = false
            isConnectedToPc = false
            isPcUnreachable = false
            isPcAuthorized = false
            isServiceRunning = false
            activeIsMuted = false
            Companion.isConnectedToPc = false
            Companion.isPcUnreachable = false
            Companion.isPcAuthorized = false
            lastHeartbeatSentAt = 0L

            val rec = recorder
            recorder = null
            runCatching { rec?.stop() }
                .onFailure { Log.w(TAG, "Error stopping recorder", it) }

            // Order matters: capture first so nothing new is queued, then the paced thread so
            // any frame already held is sent, and only then the socket. Closing the socket
            // earlier would drop the last frame and close the phone mid-sentence.
            val paced = pacedSender
            pacedSender = null
            runCatching { paced?.close() }
                .onFailure { Log.w(TAG, "Error stopping paced sender", it) }

            val snd = sender
            sender = null
            runCatching { snd?.close() }
                .onFailure { Log.w(TAG, "Error closing sender", it) }

            releaseWakeLock()
            releaseWifiLock()

            if (wasStreaming) {
                Log.i(TAG, "Streaming stopped")
                broadcastStatus(STATUS_STOPPED)
            }

            cancelRequestNotification()

            if (isDiscoveryRunning) {
                // Keep the foreground notification so a PC reconnect still has mic access.
                updateNotification()
            } else {
                cancelNotifications()
                stopForegroundCompat()
            }
        }
    }

    // =====================================================================
    // Discovery server
    // =====================================================================

    private fun startDiscoveryServer() {
        synchronized(discoveryLock) {
            if (isDiscoveryRunning) {
                Log.d(TAG, "Discovery already running")
                return
            }
            val sock = try {
                DatagramSocket(null).apply {
                    reuseAddress = true
                    broadcast = true
                    bind(InetSocketAddress("0.0.0.0", AudioConfig.DISCOVERY_PORT))
                    soTimeout = DISCOVERY_SOCKET_TIMEOUT_MS
                }
            } catch (e: Exception) {
                isDiscoveryRunning = false
                Log.e(TAG, "Discovery socket bind on UDP ${AudioConfig.DISCOVERY_PORT} failed", e)
                broadcastStatus(
                    STATUS_ERROR, "", AudioConfig.DISCOVERY_PORT, currentFormat,
                    getString(R.string.error_discovery_bind)
                )
                return
            }

            isDiscoveryRunning = true
            discoverySocket = sock
            val thread = Thread({ discoveryLoop(sock) }, "ZMicDiscovery").apply {
                isDaemon = true
            }
            discoveryThread = thread
            thread.start()
            Log.i(TAG, "Discovery server listening on UDP ${AudioConfig.DISCOVERY_PORT}")
        }
    }

    private fun discoveryLoop(sock: DatagramSocket) {
        val rxBuffer = ByteArray(2048)
        val rxPacket = DatagramPacket(rxBuffer, rxBuffer.size)
        var lastBeaconTime = 0L

        try {
            while (isDiscoveryRunning && !sock.isClosed) {
                val now = System.currentTimeMillis()
                if (now - lastBeaconTime >= BEACON_INTERVAL_MS) {
                    lastBeaconTime = now
                    broadcastBeacon(sock)
                }

                try {
                    sock.receive(rxPacket)
                    val text = String(rxPacket.data, 0, rxPacket.length, Charsets.UTF_8).trim()
                    val senderIp = rxPacket.address?.hostAddress ?: ""

                    when {
                        text.startsWith(AudioConfig.CONNECT_PREFIX) ->
                            handleConnectFromPc(text.removePrefix(AudioConfig.CONNECT_PREFIX).trim(), senderIp)

                        text.startsWith(AudioConfig.FORGET_PREFIX) ->
                            handleForgetFromPc()

                        else -> Log.d(TAG, "Ignoring discovery payload '$text' from $senderIp")
                    }
                } catch (_: SocketTimeoutException) {
                    // Expected: loop back to re-check the beacon interval.
                }
            }
        } catch (e: Exception) {
            if (isDiscoveryRunning) Log.e(TAG, "Discovery server failed", e)
        } finally {
            runCatching { sock.close() }
                .onFailure { Log.w(TAG, "Error closing discovery socket", it) }
            synchronized(discoveryLock) {
                // Identity guard: never tear down a newer server that already replaced us.
                if (discoverySocket === sock) {
                    discoverySocket = null
                    discoveryThread = null
                    isDiscoveryRunning = false
                    Log.i(TAG, "Discovery server stopped")
                }
            }
        }
    }

    private fun stopDiscoveryServer() {
        val thread = synchronized(discoveryLock) {
            isDiscoveryRunning = false
            val t = discoveryThread
            discoveryThread = null
            runCatching { discoverySocket?.close() }
                .onFailure { Log.w(TAG, "Error closing discovery socket", it) }
            discoverySocket = null
            t
        }
        // Join outside the lock: the loop's finally block needs this same lock to finish.
        if (thread != null && thread !== Thread.currentThread()) {
            try {
                thread.join(2000)
                if (thread.isAlive) Log.w(TAG, "Discovery thread did not exit within 2000ms")
            } catch (e: InterruptedException) {
                Log.w(TAG, "Interrupted while joining discovery thread", e)
                Thread.currentThread().interrupt()
            }
        }
    }

    /**
     * Broadcasts the beacon to the limited broadcast address AND to the subnet-directed address
     * derived from the DHCP lease. The latter is what works on multi-VLAN networks where
     * 255.255.255.255 is filtered.
     */
    private fun broadcastBeacon(sock: DatagramSocket) {
        val msg = "${AudioConfig.BEACON_PREFIX}$deviceDisplayName:${AudioConfig.DEFAULT_PORT}"
        val bytes = msg.toByteArray(Charsets.UTF_8)
        if (bytes.size > rxBufferCapacityForBeacon) {
            Log.w(TAG, "Beacon too large ($bytes bytes), truncating")
        }
        val payload = if (bytes.size > rxBufferCapacityForBeacon) bytes.copyOf(rxBufferCapacityForBeacon) else bytes

        var sent = 0
        for (addr in broadcastAddresses()) {
            try {
                sock.send(DatagramPacket(payload, payload.size, addr, AudioConfig.DISCOVERY_PORT))
                sent++
            } catch (e: Exception) {
                Log.w(TAG, "Beacon send to $addr failed", e)
            }
        }
        Log.d(TAG, "Beacon '$msg' sent to $sent address(es)")
    }

    private fun broadcastAddresses(): List<InetAddress> {
        val result = LinkedHashSet<InetAddress>()

        runCatching { result.add(InetAddress.getByName(LIMITED_BROADCAST)) }
            .onFailure { Log.w(TAG, "Could not resolve limited broadcast", it) }

        runCatching {
            val wm = applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
            @Suppress("DEPRECATION")
            val dhcp = wm.dhcpInfo
            val ip = intToIpv4(dhcp.ipAddress)
            val mask = intToIpv4(dhcp.netmask)
            if (ip != null && mask != null) {
                val broadcastInt = (ipToIntv4(ip) and ipToIntv4(mask)) or ipToIntv4(mask).inv()
                val subnetBroadcast = intToIpv4(broadcastInt)
                if (subnetBroadcast != null) {
                    Log.d(TAG, "Using subnet-directed broadcast $subnetBroadcast (ip=$ip mask=$mask)")
                    result.add(InetAddress.getByName(subnetBroadcast))
                }
            } else {
                Log.w(TAG, "No usable DHCP lease; relying on limited broadcast only")
            }
        }.onFailure { Log.w(TAG, "Could not derive subnet broadcast address", it) }

        return result.toList()
    }

    private fun intToIpv4(value: Int): String? {
        if (value == 0) return null
        return "${value and 0xFF}.${(value shr 8) and 0xFF}.${(value shr 16) and 0xFF}.${(value shr 24) and 0xFF}"
    }

    private fun ipToIntv4(ip: String): Int {
        val parts = ip.split('.')
        var out = 0
        for (p in parts) {
            out = (out shl 8) or (p.toIntOrNull() ?: 0)
        }
        return out
    }

    private fun handleConnectFromPc(pcName: String, pcIp: String) {
        val trusted = trustedPcName()
        Log.i(TAG, "PM_CONNECT from '$pcName' ($pcIp), trusted='$trusted'")

        if (trusted != null && trusted == pcName) {
            val movedToNewAddress = isStreaming && !currentHost.equals(pcIp, ignoreCase = true)

            if (movedToNewAddress) {
                // The same, already trusted PC is now answering from a different local address.
                // That is what happens when a PC changes how it reaches the network: the
                // Ethernet cable is pulled, the old address dies, and Wi-Fi or a different
                // adapter takes over. The stream is pointed at the address that is gone, so
                // nothing can arrive, the heartbeat is missed, and after 2.5 seconds the phone
                // stops streaming and reports that the PC is gone. It used to refuse this
                // outright as a duplicate connection, which is why switching the PC between
                // Ethernet and Wi-Fi left the user with no voice and no explanation until they
                // restarted the phone app.
                Log.i(
                    TAG,
                    "Trusted PC '$pcName' moved from $currentHost to $pcIp. Following it."
                )
                stopStreaming()
            }

            currentPcName = pcName
            verifiedPcName = pcName
            currentHost = pcIp
            isPcAuthorized = true
            Companion.isPcAuthorized = true
            sendDiscoveryReply(pcIp, "${AudioConfig.ACCEPT_PREFIX}$deviceDisplayName")
            playDefaultNotificationSound()
            startStreaming(pcIp, AudioConfig.DEFAULT_PORT)
        } else {
            currentHost = pcIp
            currentPcName = pcName
            verifiedPcName = ""
            isPcAuthorized = false
            Companion.isPcAuthorized = false
            showConnectionRequestNotification(pcName, pcIp)
            sendBroadcast(
                Intent(ACTION_CONNECTION_REQUEST).apply {
                    putExtra(EXTRA_PC_NAME, pcName)
                    putExtra(EXTRA_IP, pcIp)
                    setPackage(packageName)
                }
            )
        }
    }

    private fun handleForgetFromPc() {
        Log.i(TAG, "PM_FORGET received; clearing paired PC")
        clearTrustedPc()
        resetPeerState()
        stopStreaming()
        broadcastStatus(STATUS_STOPPED)
    }

    private fun resetPeerState() {
        isPcAuthorized = false
        Companion.isPcAuthorized = false
        isConnectedToPc = false
        Companion.isConnectedToPc = false
        verifiedPcName = ""
        currentPcName = AudioConfig.DEFAULT_PC_NAME
    }

    private val deviceDisplayName: String
        get() = "${Build.MANUFACTURER} ${Build.MODEL}".trim()

    /**
     * Queues a discovery reply on the bounded executor. Never allocates a thread or socket per call.
     */
    private fun sendDiscoveryReply(targetIp: String, msg: String) {
        if (targetIp.isBlank()) {
            Log.w(TAG, "Dropping discovery reply '$msg' with no target IP")
            return
        }
        val bytes = msg.toByteArray(Charsets.UTF_8)
        replyExecutor.execute {
            var s: DatagramSocket? = null
            try {
                s = DatagramSocket()
                s.send(
                    DatagramPacket(
                        bytes, bytes.size,
                        InetAddress.getByName(targetIp), AudioConfig.DISCOVERY_PORT
                    )
                )
                Log.d(TAG, "Discovery reply '$msg' sent to $targetIp")
            } catch (e: Exception) {
                Log.w(TAG, "Discovery reply '$msg' to $targetIp failed", e)
            } finally {
                runCatching { s?.close() }
            }
        }
    }

    // =====================================================================
    // Mute / state
    // =====================================================================

    private fun setMuteState(muted: Boolean, fromRemote: Boolean = false) {
        isMuted = muted
        activeIsMuted = muted
        recorder?.setMuted(muted)
        if (!fromRemote) {
            sender?.sendRemoteCommand(if (muted) AudioConfig.CMD_MUTE else AudioConfig.CMD_UNMUTE)
        }
        updateNotification()
        if (muted) {
            broadcastStatus(STATUS_MUTED, currentHost, currentPort, currentFormat)
        } else if (isConnectedToPc) {
            broadcastStatus(STATUS_CONNECTED_PC, currentHost, currentPort, currentFormat, currentPcName)
        } else if (isPcUnreachable) {
            broadcastStatus(STATUS_PC_UNREACHABLE, currentHost, currentPort, currentFormat)
        } else {
            broadcastStatus(STATUS_CONNECTING, currentHost, currentPort, currentFormat)
        }
    }

    private fun playDefaultNotificationSound() {
        val now = System.currentTimeMillis()
        synchronized(this) {
            if (now - lastSoundTime < SOUND_DEBOUNCE_MS) return
            lastSoundTime = now
        }
        try {
            val uri = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_NOTIFICATION) ?: return
            val ringtone = RingtoneManager.getRingtone(applicationContext, uri) ?: return
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
                ringtone.isLooping = false
            }
            ringtone.play()
        } catch (e: Exception) {
            Log.w(TAG, "Could not play notification sound", e)
        }
    }

    // =====================================================================
    // Wake / Wi-Fi locks
    // =====================================================================

    private fun acquireWakeLock() {
        if (wakeLock != null) return
        try {
            val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
            wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "ZMic::AudioStreamWakeLock").apply {
                setReferenceCounted(false)
                // No-arg acquire() is used on purpose: the compileSdk stubs only expose
                // acquire(long), which does not exist on API < 33 devices. The lock is
                // non-reference-counted and released in the guarded stopStreaming() path.
                acquire()
            }
        } catch (e: Exception) {
            Log.e(TAG, "Could not acquire wake lock", e)
            wakeLock = null
        }
    }

    /**
     * release() on a non-reference-counted lock throws if it is not held, and stopStreaming() can
     * be entered from three different threads. Always null the field and never propagate.
     */
    private fun releaseWakeLock() {
        val lock = wakeLock
        wakeLock = null
        if (lock == null) return
        try {
            if (lock.isHeld) lock.release()
        } catch (e: Exception) {
            Log.w(TAG, "Ignoring wake lock release error", e)
        }
    }

    private fun acquireWifiLock() {
        try {
            val wm = applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager

            // High performance: keeps the Wi-Fi radio out of power-save while the screen is off.
            if (highPerfWifiLock == null) {
                @Suppress("DEPRECATION")
                highPerfWifiLock = wm.createWifiLock(
                    WifiManager.WIFI_MODE_FULL_HIGH_PERF, "ZMic::AudioHighPerfWifiLock"
                ).apply {
                    setReferenceCounted(false)
                    acquire()
                }
            }

            // Low latency: disables packet TX aggregation for minimum audio latency (Android 10+).
            if (wifiLock == null && Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                wifiLock = wm.createWifiLock(
                    WifiManager.WIFI_MODE_FULL_LOW_LATENCY, "ZMic::AudioLowLatencyWifiLock"
                ).apply {
                    setReferenceCounted(false)
                    acquire()
                }
            }
        } catch (e: Exception) {
            Log.e(TAG, "Could not acquire Wi-Fi locks", e)
        }
    }

    private fun releaseWifiLock() {
        val low = wifiLock
        wifiLock = null
        try {
            if (low != null && low.isHeld) low.release()
        } catch (e: Exception) {
            Log.w(TAG, "Ignoring low-latency Wi-Fi lock release error", e)
        }

        val high = highPerfWifiLock
        highPerfWifiLock = null
        try {
            if (high != null && high.isHeld) high.release()
        } catch (e: Exception) {
            Log.w(TAG, "Ignoring high-performance Wi-Fi lock release error", e)
        }
    }

    // =====================================================================
    // Notifications
    // =====================================================================

    private fun createNotificationChannels() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val manager = getSystemService(NotificationManager::class.java)

            val streamChannel = NotificationChannel(
                CHANNEL_ID,
                getString(R.string.notification_channel_name),
                NotificationManager.IMPORTANCE_LOW
            ).apply {
                description = getString(R.string.notification_channel_desc)
                setShowBadge(false)
            }
            manager.createNotificationChannel(streamChannel)

            val defaultSoundUri = RingtoneManager.getDefaultUri(RingtoneManager.TYPE_NOTIFICATION)
            val audioAttributes = AudioAttributes.Builder()
                .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                .setUsage(AudioAttributes.USAGE_NOTIFICATION)
                .build()

            val requestChannel = NotificationChannel(
                REQUEST_CHANNEL_ID,
                getString(R.string.notification_request_channel_name),
                NotificationManager.IMPORTANCE_HIGH
            ).apply {
                description = getString(R.string.notification_request_channel_desc)
                setSound(defaultSoundUri, audioAttributes)
                enableVibration(true)
                setShowBadge(true)
            }
            manager.createNotificationChannel(requestChannel)
        }
    }

    private fun pendingFlags(): Int =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        } else {
            PendingIntent.FLAG_UPDATE_CURRENT
        }

    private fun servicePendingIntent(action: String, requestCode: Int): PendingIntent {
        val intent = Intent(this, AudioStreamService::class.java).apply { this.action = action }
        return PendingIntent.getService(this, requestCode, intent, pendingFlags())
    }

    private fun buildOngoingNotification(
        host: String, port: Int, muted: Boolean, format: String, pcName: String
    ): Notification {
        val openAppIntent = Intent(this, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_SINGLE_TOP
        }
        val openAppPendingIntent = PendingIntent.getActivity(this, 0, openAppIntent, pendingFlags())

        val text = when {
            muted -> getString(R.string.notification_text_muted)
            isPcUnreachable -> getString(R.string.notification_text_pc_not_responding)
            !isConnectedToPc || !isStreaming -> getString(R.string.notification_text_ready, pcName)
            else -> getString(R.string.notification_text, pcName, format)
        }

        val builder = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            Notification.Builder(this, CHANNEL_ID)
        } else {
            @Suppress("DEPRECATION")
            Notification.Builder(this)
        }

        builder.setContentTitle(getString(R.string.notification_title))
            .setContentText(text)
            .setSmallIcon(R.drawable.ic_mic_notification)
            .setContentIntent(openAppPendingIntent)
            .setOngoing(true)
            .setShowWhen(false)

        // Stop is always offered, including in discovery-only mode where isStreaming == false.
        // Without this the user cannot leave discovery mode from the UI at all.
        builder.addAction(
            buildAction(
                R.drawable.ic_action_stop,
                getString(R.string.action_stop),
                servicePendingIntent(ACTION_STOP, 1)
            )
        )

        if (isStreaming) {
            builder.addAction(
                buildAction(
                    R.drawable.ic_action_mute,
                    if (muted) getString(R.string.action_unmute) else getString(R.string.action_mute),
                    servicePendingIntent(if (muted) ACTION_UNMUTE else ACTION_MUTE, 2)
                )
            )
        }

        return builder.build()
    }

    private fun buildAction(iconRes: Int, label: String, pendingIntent: PendingIntent): Notification.Action =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            Notification.Action.Builder(
                Icon.createWithResource(this, iconRes), label, pendingIntent
            ).build()
        } else {
            @Suppress("DEPRECATION")
            Notification.Action(iconRes, label, pendingIntent)
        }

    private fun showConnectionRequestNotification(pcName: String, pcIp: String) {
        val openAppIntent = Intent(this, MainActivity::class.java).apply {
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP)
        }
        val contentPendingIntent = PendingIntent.getActivity(this, 0, openAppIntent, pendingFlags())

        val allowIntent = Intent(this, AudioStreamService::class.java).apply {
            action = ACTION_ALLOW_REQUEST
            putExtra(EXTRA_PC_NAME, pcName)
            // Always the address this request actually came from, never a stale field.
            putExtra(EXTRA_IP, pcIp)
        }
        val declineIntent = Intent(this, AudioStreamService::class.java).apply {
            action = ACTION_DECLINE_REQUEST
            putExtra(EXTRA_IP, pcIp)
        }

        val builder = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            Notification.Builder(this, REQUEST_CHANNEL_ID)
        } else {
            @Suppress("DEPRECATION")
            Notification.Builder(this)
        }

        val notification = builder
            .setContentTitle(getString(R.string.notification_request_title))
            .setContentText(getString(R.string.notification_request_text, pcName))
            .setSmallIcon(R.drawable.ic_pairing_notification)
            .setContentIntent(contentPendingIntent)
            .addAction(
                buildAction(
                    R.drawable.ic_action_allow,
                    getString(R.string.btn_allow),
                    PendingIntent.getService(this, 10, allowIntent, pendingFlags())
                )
            )
            .addAction(
                buildAction(
                    R.drawable.ic_action_deny,
                    getString(R.string.btn_decline),
                    PendingIntent.getService(this, 11, declineIntent, pendingFlags())
                )
            )
            .setAutoCancel(true)
            .build()

        getSystemService(NotificationManager::class.java).notify(REQUEST_NOTIFICATION_ID, notification)
        Log.i(TAG, "Pairing request notification shown for '$pcName' ($pcIp)")
    }

    private fun promoteToForeground(notification: Notification) {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                startForeground(
                    NOTIFICATION_ID, notification,
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
                )
            } else {
                startForeground(NOTIFICATION_ID, notification)
            }
        } catch (e: Exception) {
            Log.e(TAG, "startForeground failed", e)
        }
    }

    private fun stopForegroundCompat() {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
                stopForeground(STOP_FOREGROUND_REMOVE)
            } else {
                @Suppress("DEPRECATION")
                stopForeground(true)
            }
        } catch (e: Exception) {
            Log.w(TAG, "stopForeground failed", e)
        }
    }

    private fun updateNotification() {
        if (!isStreaming && !isDiscoveryRunning) return
        val manager = getSystemService(NotificationManager::class.java)
        manager.notify(
            NOTIFICATION_ID,
            buildOngoingNotification(currentHost, currentPort, isMuted, currentFormat, currentPcName)
        )
    }

    private fun cancelRequestNotification() {
        getSystemService(NotificationManager::class.java).cancel(REQUEST_NOTIFICATION_ID)
    }

    private fun cancelNotifications() {
        val manager = getSystemService(NotificationManager::class.java)
        manager.cancel(REQUEST_NOTIFICATION_ID)
        manager.cancel(NOTIFICATION_ID)
    }

    // =====================================================================
    // Preferences
    // =====================================================================

    private fun loadTrustedPc() {
        if (trustedPcLoaded) return
        synchronized(this) {
            if (trustedPcLoaded) return
            val primary = getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
            var trusted = primary.getString(KEY_TRUSTED_PC, null)
            if (trusted.isNullOrBlank()) {
                // Migrate from the legacy prefs file.
                val legacy = getSharedPreferences(LEGACY_PREFS_NAME, Context.MODE_PRIVATE)
                trusted = legacy.getString(KEY_TRUSTED_PC, null)
                if (!trusted.isNullOrBlank()) {
                    primary.edit()
                        .putString(KEY_TRUSTED_PC, trusted)
                        .putString(KEY_LAST_IP, legacy.getString(KEY_LAST_IP, null))
                        .putInt(KEY_LAST_PORT, legacy.getInt(KEY_LAST_PORT, AudioConfig.DEFAULT_PORT))
                        .apply()
                }
            }
            cachedTrustedPc = trusted
            trustedPcLoaded = true
            Log.i(TAG, "Trusted PC loaded: '$trusted'")
        }
    }

    private fun trustedPcName(): String? {
        loadTrustedPc()
        return cachedTrustedPc?.takeIf { it.isNotBlank() }
    }

    private fun saveTrustedPc(pcName: String, pcIp: String) {
        // KEY_LAST_IP must be written here too, otherwise pairing via the notification leaves the
        // fast-reconnect path permanently disabled.
        getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE).edit()
            .putString(KEY_TRUSTED_PC, pcName)
            .putString(KEY_LAST_IP, pcIp)
            .putInt(KEY_LAST_PORT, AudioConfig.DEFAULT_PORT)
            .apply()
        getSharedPreferences(LEGACY_PREFS_NAME, Context.MODE_PRIVATE).edit()
            .putString(KEY_TRUSTED_PC, pcName)
            .putString(KEY_LAST_IP, pcIp)
            .putInt(KEY_LAST_PORT, AudioConfig.DEFAULT_PORT)
            .apply()
        cachedTrustedPc = pcName
        trustedPcLoaded = true
        Log.i(TAG, "Trusted PC saved: '$pcName' ($pcIp)")
    }

    private fun clearTrustedPc() {
        getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE).edit()
            .remove(KEY_TRUSTED_PC).remove(KEY_LAST_IP).remove(KEY_LAST_PORT).apply()
        getSharedPreferences(LEGACY_PREFS_NAME, Context.MODE_PRIVATE).edit()
            .remove(KEY_TRUSTED_PC).remove(KEY_LAST_IP).remove(KEY_LAST_PORT).apply()
        cachedTrustedPc = null
        trustedPcLoaded = true
    }

    // =====================================================================
    // Status broadcast
    // =====================================================================

    private fun broadcastStatus(
        status: String,
        host: String = "",
        port: Int = AudioConfig.DEFAULT_PORT,
        format: String = FORMAT_MONO,
        info: String? = null
    ) {
        val now = System.currentTimeMillis()
        synchronized(this) {
            val signature = "$status|$host|$port|$format|$isMuted|$info"
            if (signature == lastBroadcastSignature && (now - lastBroadcastTime) < 2000L) return
            lastBroadcastSignature = signature
            lastBroadcastTime = now
        }

        sendBroadcast(
            Intent(ACTION_STATUS_UPDATE).apply {
                putExtra(EXTRA_STATUS, status)
                putExtra(EXTRA_IP, host)
                putExtra(EXTRA_PORT, port)
                putExtra(EXTRA_FORMAT, format)
                putExtra(EXTRA_MUTED, isMuted)
                // PC name and error text are distinct slots: an error string must never be
                // rendered in the "Connected to PC: <name>" field.
                if (status == STATUS_ERROR) putExtra(EXTRA_ERROR, info) else putExtra(EXTRA_PC_NAME, info)
                setPackage(packageName)
            }
        )
    }

    override fun onDestroy() {
        Log.i(TAG, "onDestroy")
        stopStreaming()
        stopDiscoveryServer()
        replyExecutor.shutdownNow()
        cancelNotifications()
        stopForegroundCompat()
        super.onDestroy()
    }

    companion object {
        private const val TAG = "ZMicAudioService"

        const val PREFS_NAME = "ZMicPrefs"
        const val LEGACY_PREFS_NAME = "PhoneMicPrefs"
        const val KEY_TRUSTED_PC = "trusted_pc_name"
        const val KEY_LAST_IP = "last_ip"
        const val KEY_LAST_PORT = "last_port"

        private const val FORMAT_STEREO = "Stereo"
        private const val FORMAT_MONO = "Mono"
        private const val FORMAT_READY = "Ready"
        private const val FORMAT_CONNECTING = "Connecting..."

        private const val BEACON_INTERVAL_MS = 2000L
        private const val DISCOVERY_SOCKET_TIMEOUT_MS = 1500
        private const val SOUND_DEBOUNCE_MS = 2500L
        private const val LIMITED_BROADCAST = "255.255.255.255"
        private const val rxBufferCapacityForBeacon = 512

        const val CHANNEL_ID = "zmic_stream_channel"
        const val REQUEST_CHANNEL_ID = "zmic_request_channel"
        const val NOTIFICATION_ID = 1001
        const val REQUEST_NOTIFICATION_ID = 1002

        const val ACTION_START = "com.phonemic.app.ACTION_START"
        const val ACTION_START_DISCOVERY = "com.phonemic.app.ACTION_START_DISCOVERY"
        const val ACTION_STOP = "com.phonemic.app.ACTION_STOP"
        const val ACTION_MUTE = "com.phonemic.app.ACTION_MUTE"
        const val ACTION_UNMUTE = "com.phonemic.app.ACTION_UNMUTE"
        const val ACTION_TOGGLE_MUTE = "com.phonemic.app.ACTION_TOGGLE_MUTE"
        const val ACTION_ALLOW_REQUEST = "com.phonemic.app.ACTION_ALLOW_REQUEST"
        const val ACTION_DECLINE_REQUEST = "com.phonemic.app.ACTION_DECLINE_REQUEST"
        const val ACTION_FORGET_PAIRED_PC = "com.phonemic.app.ACTION_FORGET_PAIRED_PC"
        const val ACTION_CONNECTION_REQUEST = "com.phonemic.app.ACTION_CONNECTION_REQUEST"
        const val ACTION_STATUS_UPDATE = "com.phonemic.app.ACTION_STATUS_UPDATE"

        const val EXTRA_IP = "extra_target_ip"
        const val EXTRA_PORT = "extra_target_port"
        const val EXTRA_FORMAT = "extra_format"
        const val EXTRA_MUTED = "extra_muted"
        const val EXTRA_STATUS = "extra_status"
        const val EXTRA_ERROR = "extra_error"
        const val EXTRA_PC_NAME = "extra_pc_name"

        const val STATUS_CONNECTING = "connecting"
        const val STATUS_CONNECTED_PC = "connected_pc"
        const val STATUS_PC_UNREACHABLE = "pc_unreachable"
        const val STATUS_PC_DISCONNECTED = "pc_disconnected"
        const val STATUS_MUTED = "muted"
        const val STATUS_STOPPED = "stopped"
        const val STATUS_ERROR = "error"

        @Volatile
        var isServiceRunning: Boolean = false
            internal set

        @Volatile
        var isPcAuthorized: Boolean = false
            internal set

        @Volatile
        var isConnectedToPc: Boolean = false
            internal set

        @Volatile
        var isPcUnreachable: Boolean = false
            internal set

        @Volatile
        var activeHost: String = ""
            internal set

        @Volatile
        var activePort: Int = AudioConfig.DEFAULT_PORT
            internal set

        @Volatile
        var activeFormat: String = FORMAT_MONO
            internal set

        @Volatile
        var activePcName: String = AudioConfig.DEFAULT_PC_NAME
            internal set

        @Volatile
        var activeIsMuted: Boolean = false
            internal set
    }
}
