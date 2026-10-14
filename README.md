# Z Mic Studio

Turns an Android phone into a high-quality microphone for a Windows PC over your local Wi-Fi network.

The phone does the recording, and the PC handles real-time audio processing and effects. Audio streams directly between your devices without passing through the internet.

## Downloads & Installation

Pre-compiled production binaries are available on the official releases page:

- **Windows Setup Installer (`.exe`)**: [Download from Releases](https://github.com/zameersts/Z-Mic-Studio-Releases/releases/latest)
- **Android App (`.apk`)**: [Download from Releases](https://github.com/zameersts/Z-Mic-Studio-Releases/releases/latest)

When launching for the first time, Windows may prompt you once to allow network access through the firewall. Be sure to select **Allow** (or use the built-in firewall button on the About tab).

> Without firewall permission, the phone connects, the status says Connected, but no audio will arrive.

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
| `Ctrl+Alt+R` | Start or stop the stream |
| `Ctrl+Alt+M` | Mute or unmute microphone |
| `Ctrl+Alt+I` | Move to the microphone list |
| `Ctrl+Alt+N` | Noise reduction |
| `Ctrl+Alt+B` | 80Hz rumble cut |
| `Ctrl+Alt+E` | Studio echo |
| `Ctrl+Alt+P` | Ping-pong delay |
| `Ctrl+Alt+H` | Headphone monitoring |
| `Ctrl+Alt+A` | About window |
| `Ctrl+Tab` | Next tab |
| `Ctrl+Shift+Tab` | Previous tab |
| `Ctrl+1` .. `5` | Direct jump to tabs 1 through 5 |

Arrow keys move sliders, and standard keyboard navigation works across all tabs and controls.

## If you cannot hear anything

The Troubleshooting Log on the Help tab usually answers this by itself. In order of likelihood:

- **Windows Firewall is blocking the app.** Open the About tab and use the Windows Firewall
  button. This is the most common cause and produces no error message anywhere.
- **The virtual cable is not installed.** The app checks for it at launch and offers the
  download if it is missing.
- **The phone stopped streaming.** It will say so on its own screen.
- **The stream is muted.** `Ctrl+Alt+M`.

If your phone appears but keeps dropping, the PC is probably holding two network addresses and
answering from the wrong one. Unplugging a cable does not remove its address on Windows. Either
unplug the cable properly, or turn Wi-Fi off in Windows when using Ethernet. The app now
tolerates this, but it is worth removing anyway.

## Privacy & Updates

No account. No advertising. No tracking, no analytics. Your audio never leaves your own devices.

Z Mic Studio only contacts the internet when checking for updates via GitHub's public API (`https://api.github.com/repos/zameersts/Z-Mic-Studio-Releases/releases/latest`) to inform you if a newer version is available. It never transmits audio, user identifiers, telemetry, or private data.

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

## Support & Feedback

For questions, feature requests, or bug reports, please open an issue on GitHub:
[GitHub Issues](https://github.com/zameersts/Z-Mic-Studio/issues)