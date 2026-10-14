# ZMic Studio - Security & Compilation Hardening Plan
**Target: Google Play & Future Hardened Releases**

---

## Target Objective
Make the Android app decompilation-resistant, protect intellectual property & protocol logic, prevent reverse engineering with tools like JADX/Apktool, and secure audio transmission.

---

## Step 1: Aggressive R8 Obfuscation (`proguard-rules.pro`)

1. **Eliminate Blanket Keep Rules:**
   - Remove any blanket `-keep class com.phonemic.app.** { *; }` rule.
   - Only preserve class names required by `AndroidManifest.xml` (Activity, Service).
   - Allow R8 to rename all internal methods, fields, helper classes, and audio/network components into short identifiers (`a`, `b`, `c`).

2. **Flatten Package Hierarchy:**
   - Add rule: `-repackageclasses ''`
   - Merges all internal packages (`com.phonemic.app.audio`, `com.phonemic.app.network`) into a single flat root package, destroying architectural clues.

3. **Strip Debugging and Log Statements:**
   - Add rule:
     ```proguard
     -assumenosideeffects class android.util.Log {
         public static *** d(...);
         public static *** v(...);
         public static *** i(...);
     }
     ```
   - Strips all verbose/debug logs during compilation so internal flows are not logged.

4. **Optimize & Repackage:**
   - Enable optimizations:
     ```proguard
     -optimizationpasses 5
     -allowaccessmodification
     ```

---

## Step 2: Compile-Time String Encryption (StringFog / Bytecode Transform)

1. **Problem:**
   - By default, R8 does not encrypt literal strings (ports 8088/8089, URLs, packet magic headers, action intents). Anyone searching strings in JADX can read them.

2. **Implementation:**
   - Integrate StringFog or an equivalent compile-time Gradle plugin.
   - All strings in classes are encrypted into XOR/AES byte arrays at build time.
   - Strings are dynamically decrypted in-memory only when needed at runtime.

---

## Step 3: Runtime Anti-Tamper & Self-Integrity Checks

1. **Signature Verification:**
   - At application launch, read the signing certificate hash (SHA-256) of the running APK.
   - Compare against the official release certificate hash (from `zmic-release.jks`).
   - If a third party decompiles the APK, modifies code, and re-signs with a custom/debug key, the application detects the signature mismatch and safely terminates immediately.

2. **Anti-Debugging Check:**
   - Check if a debugger or Frida/Xposed inspection hook is attached:
     ```kotlin
     if (android.os.Debug.isDebuggerConnected() || android.os.Debug.waitingForDebugger()) {
         System.exit(0);
     }
     ```

---

## Step 4: Network & Audio Stream Encryption (AES-256-GCM)

1. **Reference Architecture:**
   - In accordance with `ENCRYPTION_DESIGN.md`.

2. **Packet Security:**
   - Audio UDP packets on port 8088 encrypted with AES-256-GCM using session keys.
   - Prevents local Wi-Fi sniffing (e.g. Wireshark eavesdropping).
   - Guarantees packet integrity and prevents packet tampering or replay attacks.

---

## Step 5: Secure Build Execution & Mapping Archive

1. **Release Build Command:**
   ```powershell
   cd android
   .\gradlew assembleRelease
   ```

2. **Mapping File Archival:**
   - Location: `app/build/outputs/mapping/release/mapping.txt`
   - **CRITICAL:** Archive `mapping.txt` in a secure, private backup location.
   - Do **NOT** distribute `mapping.txt` publicly.
   - Used privately by the developer to de-obfuscate genuine crash stack traces.

3. **Final Verification:**
   - Run the generated release APK through JADX GUI to verify:
     - [ ] Class and method names are obfuscated (`a`, `b`, `c`).
     - [ ] No readable plain-text protocol strings or URLs exist.
     - [ ] Package structures are flattened.
     - [ ] Decompilation produces unreadable spaghetti logic.
