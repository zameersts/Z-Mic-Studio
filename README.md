# Z Mic Studio

Turns an Android phone into a microphone for a Windows PC, over your own local network.

Built for people who use a screen reader and who want a proper microphone for voice
applications without holding a headset, where the noise of a room ends up in the recording.

The phone does the recording. The PC does the processing. Nothing passes through the internet.

## Get it

There is no public download. This repository and its releases are private, and the project is not
being distributed publicly. The build is kept on the machine that made it.

If you have a copy, unpack it and run `ZMicStudio.exe`. Windows will ask once whether the app may
receive connections, and you should say **Allow**. That step matters more than it looks:

> Without it the phone connects, the app says Connected, and no sound ever arrives. Nothing on
> screen tells you why.

You also need the [VB-Audio Virtual Cable](https://vb-audio.com/Cable/), which is what lets
other Windows applications treat your phone as an ordinary microphone. If it is missing, the app
opens the download page for you.

## Using it

1. Put the phone and the PC on the same network.
2. Open the app on both.
3. Press **Connect** on the PC.
4. Choose your input, phone or a USB microphone, and press **Start**.

### Keyboard

| Key | Does |
|---|---|
| `Alt+R` | Start or stop the stream |
| `Alt+I` | Move to the microphone list |
| `Alt+M` | Mute |
| `Alt+N` | Noise reduction |
| `Alt+B` | 80Hz rumble cut |
| `Alt+H` | Headphone monitoring |
| `Ctrl+Tab` | Next tab |

Arrow keys move a slider, and every control carries a name a screen reader will read out.

## If you cannot hear anything

The Troubleshooting Log on the Help tab usually answers this by itself. In order of likelihood:

- **Windows Firewall is blocking the app.** Open the About tab and use the Windows Firewall
  button. This is the most common cause and produces no error message anywhere.
- **The virtual cable is not installed.** The app checks for it at launch and offers the
  download if it is missing.
- **The phone stopped streaming.** It will say so on its own screen.
- **The stream is muted.** `Alt+M`.

If your phone appears but keeps dropping, the PC is probably holding two network addresses and
answering from the wrong one. Unplugging a cable does not remove its address on Windows. Either
unplug the cable properly, or turn Wi-Fi off in Windows when using Ethernet. The app now
tolerates this, but it is worth removing anyway.

## Privacy

No account. No advertising. No tracking, no analytics. Your audio never leaves your own devices.

This copy is shipped with no update address, so it makes no internet connection at all. If a
build is ever given one, it would only read a version number. It never downloads or installs
anything.

**The audio is not encrypted.** It travels as plain PCM between the two devices. On a home
network you control that is fine. On a network you do not, anyone able to see the traffic can
listen. This is written down here rather than hidden, and an encryption design exists in
[ENCRYPTION_DESIGN.md](ENCRYPTION_DESIGN.md) but is not implemented. If that matters to you,
keep this on your own network.

## Building it

See [WINDOWS_BUILD.md](WINDOWS_BUILD.md). You will need Python 3.14, a JDK 17, and the Android
SDK.

Two version numbers have to match: `APP_VERSION` in `windows/zmic_studio_gui.py` and
`versionName` in `android/app/build.gradle.kts`.

### The release keystore

Not in this repository, and it must never be. Losing it means no future release can update an
already installed copy, which is why it lives outside the tree entirely.

## Tests

```
cd windows
python test_phase1.py
python test_phase2.py
python test_phase3.py
python test_phase4.py
```

All four must pass before a release.

## Licence

MIT. See [LICENSE](LICENSE), which also states plainly what is not finished yet.

## Contact

zameer.sstr@gmail.com