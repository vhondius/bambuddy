plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.vhondius.bambuddyhandy"
    compileSdk = 36

    defaultConfig {
        applicationId = "com.vhondius.bambuddyhandy"
        minSdk = 26
        targetSdk = 36
        versionCode = 1
        versionName = "1.0"
    }

    // A fixed local key (app/handy.keystore, kept out of git): every build is
    // signed the same, so a new APK installs over the old one and keeps cookies
    // and settings. Without it, Gradle's own debug key signs the build.
    val handyKey = file("handy.keystore")
    signingConfigs {
        create("handy") {
            storeFile = handyKey
            storePassword = "android"
            keyAlias = "handy"
            keyPassword = "android"
        }
    }
    buildTypes {
        debug { if (handyKey.exists()) signingConfig = signingConfigs.getByName("handy") }
    }
}

kotlin { jvmToolchain(17) }

dependencies {
    implementation("androidx.appcompat:appcompat:1.7.1")
    implementation("com.google.android.material:material:1.12.0")
    implementation("androidx.swiperefreshlayout:swiperefreshlayout:1.1.0")
}
