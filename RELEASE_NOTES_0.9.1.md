# Z Mic Studio 0.9.1

## What changed

### The voice no longer cuts

The phone was sending audio as fast as the computer could take it. A full buffer went out in
a few milliseconds, then the phone waited for the next one. That arrived as a hard cut every
time, and the gap after each burst was always a whole number of 5ms frames, which is the
fingerprint of a sender with frames to dump rather than a network losing packets.

The phone now sends one frame every 5ms on a steady clock, on its own thread, so the
microphone is never left waiting on the network.

### The phone stops disconnecting when you switch inputs

Choosing a USB microphone used to send the phone a stop command and close its socket. The
phone took that as the computer going away, shut its microphone down, and never came back.
Switching back to the phone then gave silence with nothing on screen to explain it. The
computer now keeps the phone's link open across the switch, only answering its heartbeat and
discarding the audio it cannot use.

### Moving the computer between Ethernet and Wi-Fi

Unplugging the cable does not remove the address on Windows, so the computer ends up holding
two addresses in the same network. It then answers from whichever one the router table
picks, and the phone threw the second one away as an intruder, declared the computer gone,
and stopped. This is the single most common way this app appeared broken, and it needed
nothing more than a change of cable.

The phone now accepts the same computer from either address and remembers which one is
answering. If the address it is streaming to dies while a stream is running, the phone follows
the computer to the new one instead of going quiet.

### The list and the sound agree

Connecting a phone while a USB microphone was selected used to force the sound onto the phone
while the list still said USB. The list now shows what is actually running, and a microphone
the user picked is never taken away from them by a phone arriving.

### The 80Hz rumble cut is actually running

It was nested inside the noise reduction switch, which is off by default, so it had never done
anything for most people while a checkbox claimed it was there. It now runs on its own and can
be switched off with Alt+B to hear the unfiltered voice.

### Installing is easier

An installer is on the way. Meanwhile the app asks Windows for permission to receive audio
itself, from the About tab, rather than leaving it to a firewall dialog whose defaults are
easy to get wrong. Without that permission the phone connects and no sound ever arrives, with
no error anywhere.

### Smaller and faster to build

scipy was in the project for one filter and cost 109MB and a 45 minute build. The filter is
now plain numpy and rejects more rumble, not less. The compiled program went from 80MB to
28MB and the build from 45 minutes to 9.

## Installing

1. Run `ZMicStudioSetup.exe`.
2. Windows will ask once whether Z Mic Studio may receive connections. Choose **Allow**.
   Without it the phone connects but no sound arrives, and nothing says why.
3. Open Z Mic Studio from the Start menu.

You need the VB-Audio Virtual Cable. If it is missing the app will offer the download.

## Before you start

Both devices must be on the same local network. Windows Firewall must allow the app to
receive connections, which step 2 handles.

No account, no advertising, no tracking, no analytics. Your audio never leaves your own
devices.

## Reporting a problem

Email zameer.sstr@gmail.com and say what you did, what you expected, and what happened
instead. Attach the Troubleshooting Log from the Help tab if you can, it usually answers the
question on its own.