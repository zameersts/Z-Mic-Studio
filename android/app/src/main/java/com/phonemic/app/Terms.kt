package com.phonemic.app

import android.content.Context

/**
 * Z Mic Studio - Terms of Use, Privacy and Acceptance Storage
 *
 * The wording here must stay identical to terms.py on the Windows companion, so both apps
 * show the user the same agreement.
 *
 * Created & Designed by Zameer
 */
object Terms {

    /** Bump this whenever the wording changes. Older acceptances are then asked again. */
    const val VERSION = "0.9.0"

    const val PREFS_NAME = "ZMicPrefs"
    const val KEY_ACCEPTED_VERSION = "accepted_terms_version"
    const val KEY_ACCEPTED_AT = "accepted_terms_at"

    const val CHECKBOX_LABEL = "I have read and agree to the Terms and Privacy above."

    val sections: List<Pair<String, String>> = listOf(
        "What This App Does" to (
            "Z Mic Studio turns your Android phone into a microphone for your Windows " +
                "computer over your local Wi-Fi network.\n\n" +
                "The audio goes straight from your phone to your computer, using the " +
                "local network only. No internet is used.\n\n" +
                "Your computer needs a virtual audio cable installed. That is a free " +
                "program called VB-Audio Virtual Cable."
            ),
        "Acceptable Use" to (
            "Please do not use this app to:\n\n" +
                "Pretend to be another person.\n" +
                "Hide who you really are.\n" +
                "Record other people talking without their permission.\n" +
                "Lie to, bully, or cheat anyone.\n" +
                "Get around the rules of another app.\n" +
                "Break any law, including privacy and recording laws.\n\n" +
                "Recording people without their permission is illegal in many countries. " +
                "You are responsible for what you do with any audio you record.\n\n" +
                "The person who made this app cannot be responsible for recordings you " +
                "make with it."
            ),
        "Your Privacy" to (
            "Your audio never leaves your devices. It travels only between your phone " +
                "and your computer.\n\n" +
                "This app has no user account. It has no advertising. It has no tracking " +
                "and no analytics.\n\n" +
                "Z Mic Studio does not connect to the internet. It never sends anything " +
                "about you or your computer anywhere. It does not check for updates by " +
                "itself.\n\n" +
                "The only time it opens a web page is to take you to the VB-Audio website, " +
                "and only when it cannot find the virtual audio cable you need.\n\n" +
                "Your settings and log files stay on your computer only.\n\n" +
                "If you want to send feedback or ask for help, you can open an issue on GitHub at " +
                "https://github.com/zameersts/Z-Mic-Studio/issues."
            ),
        "Third Party Program" to (
            "This app needs VB-Audio Virtual Cable. That program is made by Vincent " +
                "Burel and is not part of Z Mic Studio.\n\n" +
                "It is free to download from www.vb-cable.com. You download and install it " +
                "yourself.\n\n" +
                "Z Mic Studio is not connected to, or approved by, VB-Audio."
            ),
        "No Guarantee" to (
            "This app is provided as it is. There is no promise that it will work " +
                "without problems.\n\n" +
                "The person who made this app is not responsible for lost data, poor audio " +
                "quality, or any damage caused by using this app."
            )
    )

    /** The full terms as one plain text block. */
    fun asText(): String = sections.joinToString("\n\n") { (heading, body) ->
        "$heading\n\n$body"
    }

    private fun prefs(context: Context) =
        context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    fun isAccepted(context: Context): Boolean =
        prefs(context).getString(KEY_ACCEPTED_VERSION, null) == VERSION

    fun recordAccepted(context: Context) {
        prefs(context).edit()
            .putString(KEY_ACCEPTED_VERSION, VERSION)
            .putLong(KEY_ACCEPTED_AT, System.currentTimeMillis())
            .apply()
    }
}