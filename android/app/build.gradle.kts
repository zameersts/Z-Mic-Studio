import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// Release signing credentials live outside the repository. A build machine without
// that file still works: it falls back to debug signing, which is useful for testing
// but produces an APK that can never be updated by a real release.
val signingPropertiesFile = file("C:/Users/Zameer/ZMicStudio-Keys/keystore.properties")
val signingProperties = Properties().apply {
    if (signingPropertiesFile.exists()) {
        signingPropertiesFile.inputStream().use { stream -> load(stream) }
    }
}
val hasReleaseSigning = signingProperties.getProperty("storeFile") != null

android {
    namespace = "com.phonemic.app"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.phonemic.app"
        minSdk = 21
        targetSdk = 35
        versionCode = 1
        // Must match APP_VERSION in windows/zmic_studio_gui.py
        versionName = "0.9.0-beta"
    }

    signingConfigs {
        if (hasReleaseSigning) {
            create("release") {
                storeFile = file(signingProperties.getProperty("storeFile"))
                storePassword = signingProperties.getProperty("storePassword")
                keyAlias = signingProperties.getProperty("keyAlias")
                keyPassword = signingProperties.getProperty("keyPassword")
            }
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    buildTypes {
        release {
            // Every release APK must use the same key. Otherwise Android refuses to
            // install it over the version a user already has, and their saved pairing
            // with the PC is lost with the uninstall.
            signingConfig = if (hasReleaseSigning) {
                signingConfigs.getByName("release")
            } else {
                signingConfigs.getByName("debug")
            }

            // R8. Obfuscated class and method names make a decompiled APK unreadable
            // and drop unused code. It cannot hide everything, but it raises the effort
            // far above casual decompilation.
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
    }
}

dependencies {
    // Pure Android SDK APIs (AudioRecord, Service, Notification, DatagramSocket).
    // No external or heavy dependencies required for Stage 1.
}