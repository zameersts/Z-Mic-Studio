# PhoneMic Proguard Rules
#
# R8 is switched on for release builds. These rules keep the parts Android needs to
# find by name at runtime, which shrinking cannot see for itself.

# --- Components declared in AndroidManifest.xml -----------------------------
# R8 reads the manifest and keeps Activities, Services and Receivers automatically,
# but the manifest values are read at runtime by the system, so state it explicitly.
-keep class com.phonemic.app.MainActivity { *; }
-keep class com.phonemic.app.WelcomeActivity { *; }
-keep class com.phonemic.app.AudioStreamService { *; }

# --- Entry point ------------------------------------------------------------
# onCreate is reached by name through the manifest, so keep it explicitly too.
-keepclassmembers class * extends android.app.Activity {
    public void onCreate(android.os.Bundle);
}
-keepclassmembers class * extends android.app.Service {
    public void onStartCommand(android.content.Intent, int, int);
}

# --- Broadcast and service intents ------------------------------------------
# The Activity talks to the Service through action strings, and the Service sends
# broadcast intents back. The action string constants themselves must survive.
-keep class com.phonemic.app.AudioStreamService$Companion { *; }

# --- Kotlin -----------------------------------------------------------------
# Keeps the useful stack traces and metadata so a release crash can still be read.
-keepattributes SourceFile,LineNumberTable
-renamesourcefileattribute SourceFile

# --- Data classes used as message payloads ---------------------------------
# These carry protocol fields and are read by name in a few places.
-keep class com.phonemic.app.** { *; }