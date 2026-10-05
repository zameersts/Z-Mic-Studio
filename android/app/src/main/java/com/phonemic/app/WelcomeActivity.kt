package com.phonemic.app

import android.Manifest
import android.app.Activity
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.util.Log
import android.view.View
import android.widget.Button
import android.widget.CheckBox
import android.widget.TextView

/**
 * Onboarding screen shown only on first app launch.
 * Explains the required permissions and requests Microphone & Notification access.
 *
 * A denied microphone permission does NOT silently complete onboarding: the user is told plainly
 * what is blocked and given a direct route to the system Settings screen, so they are never
 * stranded on a misleading "Ready" state with no way to recover.
 */
class WelcomeActivity : Activity() {

    /** Used only to repeat the terms introduction to TalkBack after the view is built. */
    private lateinit var tvTermsAnnounce: TextView

    private var audioGranted = true

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val prefs = getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        val legacyPrefs = getSharedPreferences(LEGACY_PREFS_NAME, Context.MODE_PRIVATE)
        if (prefs.getBoolean(KEY_ONBOARDING_COMPLETED, false) ||
            legacyPrefs.getBoolean(KEY_ONBOARDING_COMPLETED, false)
        ) {
            startMainActivityAndFinish()
            return
        }

        setContentView(R.layout.activity_welcome)

        val btnContinue = findViewById<Button>(R.id.btnWelcomeContinue)
        val btnOpenSettings = findViewById<Button>(R.id.btnWelcomeOpenSettings)
        val chkAgree = findViewById<CheckBox>(R.id.chkWelcomeAgree)
        val txtTerms = findViewById<TextView>(R.id.txtWelcomeTerms)

        audioGranted = hasAudioPermission()

        // Terms are shown before anything else. Continuing is only possible once the box
        // is ticked, so the agreement is a real action and not a silent acceptance.
        txtTerms.text = Terms.asText()
        chkAgree.text = Terms.CHECKBOX_LABEL
        chkAgree.setOnCheckedChangeListener { _, isChecked ->
            btnContinue.isEnabled = isChecked
        }
        btnContinue.isEnabled = false

        btnContinue.setOnClickListener {
            if (!chkAgree.isChecked) return@setOnClickListener
            Terms.recordAccepted(this)
            checkAndRequestPermissions()
        }
        btnOpenSettings.setOnClickListener { openAppSettings() }

        updatePermissionStatus(audioGranted && hasNotificationPermission())
        findViewById<View>(R.id.spacer).importantForAccessibility = View.IMPORTANT_FOR_ACCESSIBILITY_NO

        tvTermsAnnounce = findViewById<TextView>(R.id.tvWelcomeTermsAnnounce)
        tvTermsAnnounce.announceForAccessibility(
            getString(R.string.welcome_terms_intro)
        )
    }

    /**
     * Prevents silently bypassing onboarding. On Android 13+ the predictive back gesture is used
     * when available; on older versions onBackPressed is still the correct override target.
     */
    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        finishAffinity()
    }

    private fun hasAudioPermission(): Boolean =
        Build.VERSION.SDK_INT < Build.VERSION_CODES.M ||
            checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED

    private fun checkAndRequestPermissions() {
        val permissions = mutableListOf<String>()

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M && !hasAudioPermission()) {
            permissions.add(Manifest.permission.RECORD_AUDIO)
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) {
            permissions.add(Manifest.permission.POST_NOTIFICATIONS)
        }

        if (permissions.isEmpty() || Build.VERSION.SDK_INT < Build.VERSION_CODES.M) {
            completeOnboarding()
            return
        }

        requestPermissions(permissions.toTypedArray(), REQUEST_CODE_WELCOME_PERMISSIONS)
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode != REQUEST_CODE_WELCOME_PERMISSIONS) return

        audioGranted = hasAudioPermission()
        val notificationsGranted = hasNotificationPermission()

        updatePermissionStatus(audioGranted && notificationsGranted)

        if (audioGranted && notificationsGranted) {
            completeOnboarding()
            return
        }

        // Denied: stay on this screen. The live region has already spoken the new state, so
        // no manual announcement here -- that would double-speak.
        if (!audioGranted && !shouldShowRequestPermissionRationale(Manifest.permission.RECORD_AUDIO)) {
            showPermissionDeniedDialog()
        }
    }

    private fun hasNotificationPermission(): Boolean =
        Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU ||
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED

    private fun updatePermissionStatus(granted: Boolean) {
        val btnOpenSettings = findViewById<Button>(R.id.btnWelcomeOpenSettings)
        val tvPermissionStatus = findViewById<TextView>(R.id.tvPermissionStatus)
        val btnContinue = findViewById<Button>(R.id.btnWelcomeContinue)

        if (granted) {
            tvPermissionStatus.setText(R.string.welcome_permissions_granted)
            btnOpenSettings.visibility = View.GONE
        } else {
            tvPermissionStatus.setText(R.string.status_permission_denied_action)
            btnOpenSettings.visibility = View.VISIBLE
        }
        btnContinue.visibility = View.VISIBLE
    }

    private fun showPermissionDeniedDialog() {
        android.app.AlertDialog.Builder(this)
            .setTitle(R.string.dialog_permission_title)
            .setMessage(R.string.dialog_permission_message)
            .setPositiveButton(R.string.btn_open_settings) { _, _ -> openAppSettings() }
            .setNegativeButton(R.string.btn_close, null)
            .show()
    }

    private fun openAppSettings() {
        try {
            startActivity(
                Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS).apply {
                    data = Uri.fromParts("package", packageName, null)
                }
            )
        } catch (e: Exception) {
            Log.e(TAG, "Could not open app settings", e)
        }
    }

    private fun completeOnboarding() {
        getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE).edit()
            .putBoolean(KEY_ONBOARDING_COMPLETED, true)
            .apply()
        startMainActivityAndFinish()
    }

    private fun startMainActivityAndFinish() {
        startActivity(Intent(this, MainActivity::class.java))
        finish()
    }

    companion object {
        private const val TAG = "ZMicWelcome"
        const val PREFS_NAME = "ZMicPrefs"
        const val LEGACY_PREFS_NAME = "PhoneMicPrefs"
        const val KEY_ONBOARDING_COMPLETED = "onboarding_completed"
        private const val REQUEST_CODE_WELCOME_PERMISSIONS = 2001
    }
}
