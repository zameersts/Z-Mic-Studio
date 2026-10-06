package com.phonemic.app

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.util.Log
import android.view.View
import android.widget.Button
import android.widget.TextView
import com.phonemic.app.audio.AudioConfig

/**
 * Accessible main Activity designed for Google TalkBack compatibility.
 * Automatically discovers and connects to the Windows PC over Wi-Fi UDP.
 * Provides live mic muting, pairing management, and deduplicated screen status.
 */
class MainActivity : Activity() {

    private lateinit var btnToggleStream: Button
    private lateinit var btnMuteStream: Button
    private lateinit var btnOpenSettings: Button
    private lateinit var tvStatus: TextView
    private lateinit var tvPairedDevice: TextView
    private lateinit var btnForgetPaired: Button
    private lateinit var btnViewTerms: Button

    private var isStreaming = false
    private var isMuted = false
    private var lastStatusText: String = ""
    private var lastStatusTime: Long = 0L
    private var pendingRequestDialog: AlertDialog? = null

    /**
     * Sticky record that the microphone permission is denied. Without this the status line
     * silently reverts to "Ready" on every resume, leaving the user with no way to tell
     * why audio is not working and no route to recovery.
     */
    private var permissionPermanentlyDenied = false

    private var receiverRegistered = false

    private val statusReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            when (intent?.action) {
                AudioStreamService.ACTION_STATUS_UPDATE -> {
                    val status = intent.getStringExtra(AudioStreamService.EXTRA_STATUS) ?: return
                    val host = intent.getStringExtra(AudioStreamService.EXTRA_IP) ?: ""
                    val port = intent.getIntExtra(AudioStreamService.EXTRA_PORT, AudioConfig.DEFAULT_PORT)
                    val format = intent.getStringExtra(AudioStreamService.EXTRA_FORMAT)
                        ?: getString(R.string.format_mono)
                    val muted = intent.getBooleanExtra(AudioStreamService.EXTRA_MUTED, false)
                    // PC name and error text are separate slots; never conflate them.
                    val pcName = intent.getStringExtra(AudioStreamService.EXTRA_PC_NAME)
                    val error = intent.getStringExtra(AudioStreamService.EXTRA_ERROR)

                    updateUiState(status, host, port, format, muted, pcName, error)
                }

                AudioStreamService.ACTION_CONNECTION_REQUEST -> {
                    val pcName = intent.getStringExtra(AudioStreamService.EXTRA_PC_NAME)
                        ?: AudioConfig.DEFAULT_PC_NAME
                    val pcIp = intent.getStringExtra(AudioStreamService.EXTRA_IP) ?: ""
                    showConnectionRequestDialog(pcName, pcIp)
                }
            }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val prefs = getSharedPreferences(AudioStreamService.PREFS_NAME, Context.MODE_PRIVATE)
        if (!prefs.getBoolean(WelcomeActivity.KEY_ONBOARDING_COMPLETED, false)) {
            startActivity(Intent(this, WelcomeActivity::class.java))
            finish()
            return
        }

        setContentView(R.layout.activity_main)

        btnToggleStream = findViewById(R.id.btnToggleStream)
        btnMuteStream = findViewById(R.id.btnMuteStream)
        btnOpenSettings = findViewById(R.id.btnOpenSettings)
        tvStatus = findViewById(R.id.tvStatus)
        tvPairedDevice = findViewById(R.id.tvPairedDevice)
        btnForgetPaired = findViewById(R.id.btnForgetPaired)
        btnViewTerms = findViewById(R.id.btnViewTerms)

        lastStatusText = tvStatus.text.toString()
        lastStatusTime = System.currentTimeMillis()

        permissionPermanentlyDenied = !hasAudioPermission()
        updatePairedDeviceUi()
        updatePermissionUi()

        btnToggleStream.setOnClickListener {
            if (isStreaming || AudioStreamService.isServiceRunning) {
                stopStreamService()
            } else {
                checkPermissionsAndStart()
            }
        }

        btnMuteStream.setOnClickListener {
            if (isStreaming) {
                startService(
                    Intent(this, AudioStreamService::class.java).apply {
                        action = AudioStreamService.ACTION_TOGGLE_MUTE
                    }
                )
            }
        }

        btnOpenSettings.setOnClickListener { openAppSettings() }

        btnForgetPaired.setOnClickListener { forgetPairedPc() }

        btnViewTerms.setOnClickListener { showTermsDialog() }

        checkPermissionsAndStartDiscovery()
    }

    override fun onResume() {
        super.onResume()

        val filter = IntentFilter().apply {
            addAction(AudioStreamService.ACTION_STATUS_UPDATE)
            addAction(AudioStreamService.ACTION_CONNECTION_REQUEST)
        }
        if (!receiverRegistered) {
            try {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                    registerReceiver(statusReceiver, filter, RECEIVER_NOT_EXPORTED)
                } else {
                    registerReceiver(statusReceiver, filter)
                }
                receiverRegistered = true
            } catch (e: Exception) {
                Log.e(TAG, "Failed to register status receiver", e)
            }
        }

        // Permission may have been granted from the Settings screen while we were away.
        permissionPermanentlyDenied = !hasAudioPermission()
        updatePermissionUi()
        updatePairedDeviceUi()

        if (AudioStreamService.isServiceRunning && AudioStreamService.isConnectedToPc) {
            val initialStatus = if (AudioStreamService.activeIsMuted) {
                AudioStreamService.STATUS_MUTED
            } else {
                AudioStreamService.STATUS_CONNECTED_PC
            }
            updateUiState(
                initialStatus,
                AudioStreamService.activeHost,
                AudioStreamService.activePort,
                AudioStreamService.activeFormat,
                AudioStreamService.activeIsMuted,
                AudioStreamService.activePcName,
                null
            )
        } else {
            updateUiState(AudioStreamService.STATUS_STOPPED)
        }
    }

    override fun onPause() {
        super.onPause()
        dismissRequestDialog()
        if (receiverRegistered) {
            try {
                unregisterReceiver(statusReceiver)
            } catch (e: IllegalArgumentException) {
                Log.w(TAG, "Receiver was not registered", e)
            }
            receiverRegistered = false
        }
    }

    // -----------------------------------------------------------------
    // Paired device
    // -----------------------------------------------------------------

    /**
     * Shows the paired PC name.
     *
     * Only the name belongs here. The live link state is already on the status line at the
     * top, and showing it in both places made a screen reader say Connected twice.
     */
    private fun updatePairedDeviceUi(announce: Boolean = true) {
        val prefs = getSharedPreferences(AudioStreamService.PREFS_NAME, Context.MODE_PRIVATE)
        val pairedPc = prefs.getString(AudioStreamService.KEY_TRUSTED_PC, null)
            ?.takeIf { it.isNotBlank() }

        if (pairedPc != null) {
            tvPairedDevice.text = getString(R.string.label_paired_device, pairedPc)
            btnForgetPaired.visibility = View.VISIBLE
            // Pairing changes are spoken. The live link state is not repeated here,
            // because the status line above already says Connected or Disconnected.
            if (announce) {
                tvPairedDevice.announceForAccessibility(
                    getString(R.string.label_paired_device, pairedPc)
                )
            }
        } else {
            tvPairedDevice.text = getString(R.string.label_no_paired_device)
            btnForgetPaired.visibility = View.GONE
        }
    }

    private fun forgetPairedPc() {
        val prefs = getSharedPreferences(AudioStreamService.PREFS_NAME, Context.MODE_PRIVATE)
        val previousName = prefs.getString(AudioStreamService.KEY_TRUSTED_PC, null)
            ?.takeIf { it.isNotBlank() }

        // apply(), not commit(): commit() is synchronous disk I/O on the main thread.
        prefs.edit()
            .remove(AudioStreamService.KEY_TRUSTED_PC)
            .remove(AudioStreamService.KEY_LAST_IP)
            .remove(AudioStreamService.KEY_LAST_PORT)
            .apply()
        getSharedPreferences(AudioStreamService.LEGACY_PREFS_NAME, Context.MODE_PRIVATE).edit()
            .remove(AudioStreamService.KEY_TRUSTED_PC)
            .remove(AudioStreamService.KEY_LAST_IP)
            .remove(AudioStreamService.KEY_LAST_PORT)
            .apply()

        updatePairedDeviceUi()
        updateUiState(AudioStreamService.STATUS_STOPPED)

        startService(
            Intent(this, AudioStreamService::class.java).apply {
                action = AudioStreamService.ACTION_FORGET_PAIRED_PC
            }
        )
        stopStreamService()

        tvStatus.announceForAccessibility(
            if (previousName == null) {
                getString(R.string.status_no_pc_paired)
            } else {
                getString(R.string.status_pc_forgotten, previousName)
            }
        )
    }

    // -----------------------------------------------------------------
    // Permissions
    // -----------------------------------------------------------------

    private fun checkPermissionsAndStart() {
        if (!hasAudioPermission()) {
            requestAudioPermission()
        } else {
            permissionPermanentlyDenied = false
            updatePermissionUi()
            startStreamingOrListening()
        }
    }

    private fun checkPermissionsAndStartDiscovery() {
        if (hasAudioPermission()) {
            startDiscoveryService()
        }
    }

    private fun startDiscoveryService() {
        val serviceIntent = Intent(this, AudioStreamService::class.java).apply {
            action = AudioStreamService.ACTION_START_DISCOVERY
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(serviceIntent)
        } else {
            startService(serviceIntent)
        }
    }

    private fun hasAudioPermission(): Boolean {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            return checkSelfPermission(Manifest.permission.RECORD_AUDIO) ==
                PackageManager.PERMISSION_GRANTED
        }
        return true
    }

    private fun hasNotificationPermission(): Boolean =
        Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU ||
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) ==
            PackageManager.PERMISSION_GRANTED

    private fun requestAudioPermission() {
        val perms = mutableListOf<String>()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M &&
            !hasAudioPermission()
        ) {
            perms.add(Manifest.permission.RECORD_AUDIO)
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU && !hasNotificationPermission()) {
            perms.add(Manifest.permission.POST_NOTIFICATIONS)
        }

        if (perms.isEmpty()) {
            permissionPermanentlyDenied = false
            updatePermissionUi()
            startStreamingOrListening()
            return
        }

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            requestPermissions(perms.toTypedArray(), REQUEST_CODE_PERMISSIONS)
        } else {
            permissionPermanentlyDenied = false
            updatePermissionUi()
            startStreamingOrListening()
        }
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode != REQUEST_CODE_PERMISSIONS) return

        var audioGranted = true
        var notificationsGranted = true
        for (i in permissions.indices) {
            val granted = grantResults[i] == PackageManager.PERMISSION_GRANTED
            when (permissions[i]) {
                Manifest.permission.RECORD_AUDIO -> audioGranted = granted
                Manifest.permission.POST_NOTIFICATIONS -> notificationsGranted = granted
            }
        }

        permissionPermanentlyDenied = !audioGranted
        updatePermissionUi()

        if (audioGranted) {
            startStreamingOrListening()
            return
        }

        if (!shouldShowRequestPermissionRationale(Manifest.permission.RECORD_AUDIO)) {
            // Denied permanently: the system dialog will no longer appear, so the only recovery
            // path is the app's Settings screen. Say so explicitly.
            showPermissionDeniedDialog()
        } else {
            tvStatus.announceForAccessibility(getString(R.string.status_permission_denied))
        }

        if (!notificationsGranted) {
            tvStatus.announceForAccessibility(getString(R.string.status_notifications_denied))
        }
    }

    private fun showPermissionDeniedDialog() {
        AlertDialog.Builder(this)
            .setTitle(R.string.dialog_permission_title)
            .setMessage(R.string.dialog_permission_message)
            .setPositiveButton(R.string.btn_open_settings) { _, _ -> openAppSettings() }
            .setNegativeButton(R.string.btn_close, null)
            .setCancelable(true)
            .show()
    }

    private fun showTermsDialog() {
        // Read only view of the same text the welcome screen uses.
        val scroll = android.widget.ScrollView(this).apply {
            isFillViewport = true
        }
        val text = TextView(this).apply {
            setTextIsSelectable(true)
            textSize = 15f
            setTextColor(getColor(R.color.text_primary))
            setPadding(28, 28, 28, 28)
            setText(Terms.asText())
        }
        scroll.addView(text)

        AlertDialog.Builder(this)
            .setTitle(R.string.dialog_view_terms_title)
            .setView(scroll)
            .setPositiveButton(R.string.btn_close, null)
            .show()
    }

    private fun openAppSettings() {
        try {
            startActivity(
                Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS).apply {
                    data = Uri.fromParts("package", packageName, null)
                    addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                }
            )
        } catch (e: Exception) {
            Log.e(TAG, "Could not open app settings", e)
            tvStatus.announceForAccessibility(getString(R.string.status_error, getString(R.string.error_open_settings)))
        }
    }

    private fun updatePermissionUi() {
        if (permissionPermanentlyDenied) {
            btnOpenSettings.visibility = View.VISIBLE
        } else {
            btnOpenSettings.visibility = View.GONE
        }
    }

    // -----------------------------------------------------------------
    // Service control
    // -----------------------------------------------------------------

    private fun startStreamingOrListening() {
        val prefs = getSharedPreferences(AudioStreamService.PREFS_NAME, Context.MODE_PRIVATE)
        val trustedPc = prefs.getString(AudioStreamService.KEY_TRUSTED_PC, null)
            ?.takeIf { it.isNotBlank() }
        val lastIp = prefs.getString(AudioStreamService.KEY_LAST_IP, null)
            ?.takeIf { it.isNotBlank() }

        val serviceIntent = Intent(this, AudioStreamService::class.java)
        if (trustedPc != null && lastIp != null) {
            serviceIntent.action = AudioStreamService.ACTION_START
            serviceIntent.putExtra(AudioStreamService.EXTRA_IP, lastIp)
            serviceIntent.putExtra(AudioStreamService.EXTRA_PORT, AudioConfig.DEFAULT_PORT)
            serviceIntent.putExtra(AudioStreamService.EXTRA_PC_NAME, trustedPc)
        } else {
            serviceIntent.action = AudioStreamService.ACTION_START_DISCOVERY
        }

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(serviceIntent)
        } else {
            startService(serviceIntent)
        }
    }

    private fun stopStreamService() {
        startService(
            Intent(this, AudioStreamService::class.java).apply {
                action = AudioStreamService.ACTION_STOP
            }
        )
        isStreaming = false
        updateUiState(AudioStreamService.STATUS_STOPPED)
    }

    // -----------------------------------------------------------------
    // Connection request dialog
    // -----------------------------------------------------------------

    private fun showConnectionRequestDialog(pcName: String, pcIp: String) {
        if (pcIp.isBlank()) {
            Log.w(TAG, "Ignoring connection request with no IP")
            return
        }
        dismissRequestDialog()
        val dialog = AlertDialog.Builder(this)
            .setTitle(R.string.dialog_request_title)
            .setMessage(getString(R.string.dialog_request_message, pcName))
            .setPositiveButton(R.string.btn_allow) { _, _ ->
                // apply(), not commit(): avoids synchronous disk I/O on the main thread.
                getSharedPreferences(AudioStreamService.PREFS_NAME, Context.MODE_PRIVATE).edit()
                    .putString(AudioStreamService.KEY_TRUSTED_PC, pcName)
                    .putString(AudioStreamService.KEY_LAST_IP, pcIp)
                    .putInt(AudioStreamService.KEY_LAST_PORT, AudioConfig.DEFAULT_PORT)
                    .apply()
                updatePairedDeviceUi()

                startService(
                    Intent(this, AudioStreamService::class.java).apply {
                        action = AudioStreamService.ACTION_ALLOW_REQUEST
                        putExtra(AudioStreamService.EXTRA_PC_NAME, pcName)
                        putExtra(AudioStreamService.EXTRA_IP, pcIp)
                    }
                )
            }
            .setNegativeButton(R.string.btn_decline) { _, _ ->
                startService(
                    Intent(this, AudioStreamService::class.java).apply {
                        action = AudioStreamService.ACTION_DECLINE_REQUEST
                        putExtra(AudioStreamService.EXTRA_IP, pcIp)
                    }
                )
            }
            .setCancelable(false)
            .create()

        pendingRequestDialog = dialog
        dialog.show()
    }

    private fun dismissRequestDialog() {
        pendingRequestDialog?.let {
            if (it.isShowing) it.dismiss()
        }
        pendingRequestDialog = null
    }

    // -----------------------------------------------------------------
    // UI state
    // -----------------------------------------------------------------

    private fun updateStatusText(newStatus: String) {
        val now = System.currentTimeMillis()
        if (lastStatusText == newStatus && (now - lastStatusTime) < 3000L) return
        lastStatusText = newStatus
        lastStatusTime = now
        tvStatus.text = newStatus
    }

    private fun updateUiState(
        status: String,
        host: String = "",
        port: Int = 0,
        format: String = "",
        muted: Boolean = false,
        pcName: String? = null,
        errorText: String? = null
    ) {
        isMuted = muted

        // The paired PC box shows the live link state, so it has to follow every change.
        // Silent, because the status line below already speaks the transition.
        updatePairedDeviceUi(announce = false)

        when (status) {
            AudioStreamService.STATUS_CONNECTING -> {
                setStreamingUi(true, muteEnabled = false)
                updateStatusText(getString(R.string.status_connecting, pcName ?: host))
            }

            AudioStreamService.STATUS_PC_UNREACHABLE -> {
                setStreamingUi(true, muteEnabled = false)
                updateStatusText(getString(R.string.status_pc_not_responding))
            }

            AudioStreamService.STATUS_CONNECTED_PC -> {
                setStreamingUi(true, muteEnabled = true)
                val name = pcName ?: AudioConfig.DEFAULT_PC_NAME
                val displayFormat = format.ifBlank { getString(R.string.format_mono) }
                updateStatusText(getString(R.string.status_connected_pc, name, displayFormat))
                updatePairedDeviceUi(announce = false)
                dismissRequestDialog()
            }

            AudioStreamService.STATUS_PC_DISCONNECTED -> {
                setStreamingUi(false, muteEnabled = false)
                updateStatusText(getString(R.string.status_pc_disconnected))
            }

            AudioStreamService.STATUS_MUTED -> {
                setStreamingUi(true, muteEnabled = true)
                updateStatusText(getString(R.string.status_muted))
            }

            AudioStreamService.STATUS_STOPPED -> {
                setStreamingUi(false, muteEnabled = false)
                updateStatusText(stoppedStatusText())
                dismissRequestDialog()
            }

            AudioStreamService.STATUS_ERROR -> {
                setStreamingUi(false, muteEnabled = false)
                updateStatusText(
                    getString(
                        R.string.status_error,
                        errorText ?: getString(R.string.error_unknown)
                    )
                )
                dismissRequestDialog()
            }
        }
    }

    /** A permission failure must never be reported as a healthy "Ready" state. */
    private fun stoppedStatusText(): String {
        if (permissionPermanentlyDenied) return getString(R.string.status_permission_denied_action)
        val prefs = getSharedPreferences(AudioStreamService.PREFS_NAME, Context.MODE_PRIVATE)
        val pairedPc = prefs.getString(AudioStreamService.KEY_TRUSTED_PC, null)
            ?.takeIf { it.isNotBlank() }
        return if (pairedPc == null) {
            getString(R.string.status_ready)
        } else {
            getString(R.string.status_stopped)
        }
    }

    private fun setStreamingUi(streaming: Boolean, muteEnabled: Boolean) {
        isStreaming = streaming
        btnToggleStream.setText(
            if (streaming) R.string.btn_stop_streaming else R.string.btn_start_streaming
        )
        btnMuteStream.isEnabled = muteEnabled
        btnMuteStream.setText(if (isMuted) R.string.btn_unmute else R.string.btn_mute)

        // Keep the content description equal to the visible label. A description that
        // never changes makes a screen reader report the same generic sentence no matter
        // what the button currently does, so the user cannot tell start from stop.
        btnToggleStream.contentDescription = btnToggleStream.text
        btnMuteStream.contentDescription = btnMuteStream.text
    }

    companion object {
        private const val TAG = "ZMicMainActivity"
        private const val REQUEST_CODE_PERMISSIONS = 2001
    }
}
