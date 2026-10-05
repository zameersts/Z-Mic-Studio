plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

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

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
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
