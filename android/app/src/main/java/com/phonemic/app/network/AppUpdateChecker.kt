package com.phonemic.app.network

import android.app.AlertDialog
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.widget.Toast
import com.phonemic.app.R
import org.json.JSONObject
import java.io.BufferedReader
import java.io.InputStreamReader
import java.net.HttpURLConnection
import java.net.URL
import kotlin.concurrent.thread

object AppUpdateChecker {
    private const val TAG = "AppUpdateChecker"
    const val GITHUB_LATEST_RELEASE_URL =
        "https://api.github.com/repos/zameersts/Z-Mic-Studio-Releases/releases/latest"

    data class UpdateResult(
        val hasUpdate: Boolean,
        val newVersion: String,
        val downloadUrl: String,
        val releaseNotes: String = ""
    )

    fun checkForUpdates(
        context: Context,
        silent: Boolean = false,
        onComplete: ((UpdateResult?) -> Unit)? = null
    ) {
        thread(isDaemon = true, name = "UpdateCheckThread") {
            try {
                val url = URL(GITHUB_LATEST_RELEASE_URL)
                val conn = url.openConnection() as HttpURLConnection
                conn.requestMethod = "GET"
                conn.connectTimeout = 8000
                conn.readTimeout = 8000
                conn.setRequestProperty("User-Agent", "ZMicStudio-Android-UpdateCheck")
                conn.setRequestProperty("Accept", "application/vnd.github+json, application/json")

                val responseCode = conn.responseCode
                if (responseCode == 200) {
                    val reader = BufferedReader(InputStreamReader(conn.inputStream))
                    val body = reader.readText()
                    reader.close()

                    val json = JSONObject(body)
                    val tagName = json.optString("tag_name", "").removePrefix("v").trim()
                    val bodyNotes = json.optString("body", "")

                    // Look specifically for Android APK asset
                    var apkDownloadUrl = ""
                    var apkVersion = ""
                    val assets = json.optJSONArray("assets")
                    if (assets != null) {
                        for (i in 0 until assets.length()) {
                            val asset = assets.getJSONObject(i)
                            val name = asset.optString("name", "")
                            if (name.endsWith(".apk", ignoreCase = true)) {
                                apkDownloadUrl = asset.optString("browser_download_url", "")
                                val versionMatch = Regex("""\d+\.\d+(?:\.\d+)?""").find(name)
                                apkVersion = versionMatch?.value ?: tagName
                                break
                            }
                        }
                    }

                    // Only consider an update if an APK asset actually exists
                    val currentVersion = try {
                        context.packageManager.getPackageInfo(context.packageName, 0).versionName ?: "0.9.2"
                    } catch (_: Exception) {
                        "0.9.2"
                    }
                    val candidateVersion = if (apkVersion.isNotBlank()) apkVersion else tagName
                    val hasNewerApk = apkDownloadUrl.isNotBlank() && isNewerVersion(candidateVersion, currentVersion)

                    val result = UpdateResult(
                        hasUpdate = hasNewerApk,
                        newVersion = candidateVersion,
                        downloadUrl = if (apkDownloadUrl.isNotBlank()) apkDownloadUrl else json.optString("html_url", ""),
                        releaseNotes = bodyNotes
                    )

                    Handler(Looper.getMainLooper()).post {
                        onComplete?.invoke(result)
                        if (result.hasUpdate) {
                            showUpdateDialog(context, result)
                        } else if (!silent) {
                            Toast.makeText(
                                context,
                                context.getString(R.string.update_latest, currentVersion),
                                Toast.LENGTH_SHORT
                            ).show()
                        }
                    }
                } else {
                    Log.w(TAG, "GitHub release check returned HTTP $responseCode")
                    handleError(context, silent, onComplete)
                }
                conn.disconnect()
            } catch (e: Exception) {
                Log.w(TAG, "Failed to check for updates: ${e.message}")
                handleError(context, silent, onComplete)
            }
        }
    }

    private fun handleError(
        context: Context,
        silent: Boolean,
        onComplete: ((UpdateResult?) -> Unit)?
    ) {
        Handler(Looper.getMainLooper()).post {
            onComplete?.invoke(null)
            if (!silent) {
                Toast.makeText(
                    context,
                    context.getString(R.string.update_error),
                    Toast.LENGTH_SHORT
                ).show()
            }
        }
    }

    private fun showUpdateDialog(context: Context, update: UpdateResult) {
        AlertDialog.Builder(context)
            .setTitle(R.string.update_available_title)
            .setMessage(context.getString(R.string.update_available_msg, update.newVersion))
            .setPositiveButton(R.string.btn_download_update) { _, _ ->
                val intent = Intent(Intent.ACTION_VIEW, Uri.parse(update.downloadUrl)).apply {
                    flags = Intent.FLAG_ACTIVITY_NEW_TASK
                }
                try {
                    context.startActivity(intent)
                } catch (e: Exception) {
                    Log.e(TAG, "Could not open browser for download", e)
                }
            }
            .setNegativeButton(R.string.btn_later, null)
            .show()
    }

    fun isNewerVersion(candidate: String, current: String): Boolean {
        val candParts = parseVersion(candidate) ?: return false
        val currParts = parseVersion(current) ?: return false
        for (i in 0 until 3) {
            if (candParts[i] > currParts[i]) return true
            if (candParts[i] < currParts[i]) return false
        }
        return false
    }

    private fun parseVersion(v: String): IntArray? {
        val clean = v.trim().removePrefix("v").split("-", "+")[0]
        val parts = clean.split(".")
        if (parts.isEmpty()) return null
        return try {
            intArrayOf(
                parts.getOrNull(0)?.toInt() ?: 0,
                parts.getOrNull(1)?.toInt() ?: 0,
                parts.getOrNull(2)?.toInt() ?: 0
            )
        } catch (_: Exception) {
            null
        }
    }
}
