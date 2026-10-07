# Building and releasing Z Mic Studio

Two programs are built here: a Windows one, written in Python and compiled to a standalone
executable, and an Android one, written in Kotlin and packaged as an APK. They have to agree
on the version number, because the Windows app compares its own version against a published
one to decide whether an update exists.

## Version numbers

`windows/zmic_studio_gui.py` holds `APP_VERSION`.
`android/app/build.gradle.kts` holds `versionName`.

Both must be the same string. Change both, or the app will offer itself an update that does not
exist, or refuse the one that does.

## Windows

Requires Python 3.14 with the packages in `windows/requirements.txt`, plus Nuitka.

```
pip install -r windows/requirements.txt
pip install nuitka
```

Build from inside `windows/`, because the data paths in the command are relative:

```
cd windows
nuitka --standalone ^
  --output-dir="<somewhere>\Z Mic Studio" ^
  --include-data-dir=sounds=sounds ^
  --windows-console-mode=disable ^
  --output-filename=ZMicStudio.exe ^
  --assume-yes-for-downloads ^
  --remove-output ^
  --company-name=Zameer --product-name="Z Mic Studio" ^
  --file-version=0.9.1 --product-version=0.9.1 ^
  zmic_studio_gui.py
```

Then copy the Visual C++ runtime DLLs next to the executable. Without them the program will not
start on a machine that lacks the redistributable, which is most machines that have never run
anything built this way:

```
copy %SystemRoot%\System32\msvcp140.dll       <dist folder>
copy %SystemRoot%\System32\msvcp140_1.dll     <dist folder>
copy %SystemRoot%\System32\msvcp140_2.dll     <dist folder>
copy %SystemRoot%\System32\vcruntime140.dll   <dist folder>
copy %SystemRoot%\System32\vcruntime140_1.dll <dist folder>
```

Use `--standalone`, never `--onefile`. A one file build extracts itself into a fresh temporary
folder on every run, so the Windows Firewall rule, which is written against a specific path,
would stop matching the next time the app started. The audio would then be blocked with nothing
on screen to explain it.

### Update address files

Two files sit beside the executable:

- `update_source.txt` — one line, an address that answers with a version number.
- `update_page.txt` — one line, the page to open when a newer version exists.

Both are optional. With no `update_source.txt` the app never contacts the network at all. Leave
out `update_page.txt` and the app will announce a new version without offering to open a page,
rather than guessing at an address.

The check downloads and installs nothing. It only reads.

## Android

Requires a JDK 17 and the Android SDK.

### Signing

Release signing reads `android/keystore.properties`, which is deliberately not in this
repository. Point the build at it either way:

```
set ZMIC_KEYSTORE_PROPERTIES=C:/secure/place/keystore.properties
```

or drop the file at `android/keystore.properties`. Both are gitignored. Without it the build
falls back to debug signing, which is fine for testing and produces an APK that can never be
updated by a later release.

Never commit the keystore, its passwords, or the R8 mapping file. The mapping file turns
obfuscated names back into real ones, so it is as sensitive as the key itself.

### Build

```
cd android
gradlew assembleRelease
```

The APK lands in `android/app/build/outputs/apk/release/`.

## Tests

```
cd windows
python test_phase1.py
python test_phase2.py
python test_phase3.py
python test_phase4.py
```

They must all pass before a release. Phase 2 covers the audio pipeline and the protocol, phase 4
covers the interface, the notification pipeline, and the connection behaviour.

## Publishing

1. Make both version numbers match, and above the last published one.
2. Tag the commit, `git tag 0.9.1` and push the tag.
3. Build both programs.
4. Put the Windows files in a release on the repository page.
5. Update `update_source.txt` to the new tag's version address and rebuild the Windows app, so
   an existing copy offers the new version.

That last step is why the address is a file rather than a constant in the source: changing
where the app looks costs one text file, not a rebuild.