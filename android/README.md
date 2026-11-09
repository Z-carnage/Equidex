# EQUIDEX Android packaging

Standalone Android WebView wrapper for the existing mobile frontend and embedded Python backend. No desktop or hosted server is required. Internet is still needed for market data.

Build requirements: JDK 17, Android SDK platform 35 and build tools 35.0.0, Gradle 8.9, Python 3.12. Accept the Android SDK licences yourself using Android Studio or sdkmanager --licenses.

Set JAVA_HOME to JDK 17, ANDROID_HOME to the SDK, and EQUIDEX_BUILD_PYTHON to Python 3.12. From this folder run: gradle assembleDebug. Result: app/build/outputs/apk/debug/app-debug.apk. Copy it to ../dist/EQUIDEX.apk. This is a debug-signed personal-use APK; keep a stable release keystore for distribution and upgrades.

Source/assets are copied fresh by syncEquidex. Supports Android 7+ on ARM64 phones (x86_64 emulator support was dropped on 29 Oct 2026 to halve the APK; re-add 'x86_64' to abiFilters in app/build.gradle to test on an emulator). Android runtime, Back/drawer, external links, persistent storage, app resume and live APIs must be verified on a device/emulator before calling this validated. Built successfully on 6 October 2026: dist/EQUIDEX.apk. APK signature and bundled frontend/backend identity verified. No Android device/emulator connected; installation and on-device runtime remain unverified. Portable JDK/Gradle/SDK are in ../.android-tools. SDK licences were accepted by the user.
