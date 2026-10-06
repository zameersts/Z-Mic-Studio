# Encryption Design (not yet implemented)

Status: approved as the plan, deliberately postponed to a future update.
Reason: an unfinished encryption gives a false sense of safety, which is worse than
having none. Shipping it complete in one go is the point.

Nothing in the app reads this file. It is a design note only.

## Threat model

What this will protect:
- Anyone on the same Wi-Fi who is sniffing traffic cannot hear or record the audio.
- Captured packets cannot be played back later to recover the audio.

What this will NOT protect, stated plainly:
- Someone with physical access to the phone or the computer.
- Someone who has malware on the computer, because they can read the audio before
  it is encrypted.
- The Wi-Fi router owner. On the user's own router that is the user themselves; on
  public Wi-Fi the operator is a third party. This must be worded honestly in the
  Terms rather than left vague.

## 1. The shared secret

Shown on the computer as 12 characters, for example: K7QM-4RTX-92BN

The user types it into the phone once, at first pairing. It is never asked again.

A short 6-digit code was rejected. It has only one million combinations, which can
be brute-forced by anyone on the network. Twelve characters has no practical brute
force, and the user only types it once.

Derivation, per pairing:
1. Turn the typed characters into a long number with a key derivation function.
2. Mix in a fresh random salt that is used for that pairing only.
3. The output is a 256-bit session key.

The random salt means the same typed code produces a different key every pairing, so
an old key can never be recovered by retyping the code.

The code is valid for one pairing only and expires after that.

## 2. Each audio packet

On the phone, before sending:

    microphone  ->  [16000, -16000, 8000, ...]
                ->  encrypted with the session key and the packet number
                ->  50 4D 02 01 ... A3 7F 1C E0 5B 9D 44 02 B1 68 3A 9F

On the computer, on arrival:

    garbage    ->  decrypt with the session key
               ->  [16000, -16000, 8000, ...]

AES-GCM, because it both encrypts and detects tampering. If someone alters a packet
in transit, the computer detects it and drops the packet instead of playing altered
audio.

Library situation, already verified on this machine:
- Android: AES-GCM is built into the platform via javax.crypto. No dependency.
- Windows: the `cryptography` wheel downloads fine here, 3.7 MB, pip works.

Nonce handling: the nonce must never repeat within a session or GCM breaks. Use a
random 8-byte prefix created once per session plus a 4-byte counter, so a nonce can
never repeat and no random call is needed per packet.

## 3. Replay protection

The protocol already carries a sequence number, used today for packet loss
detection. Reuse it for security: the computer tracks the highest number it has
accepted and silently drops anything lower, so a captured packet replayed later
produces no audio.

## 4. Which packets are encrypted

| Packet | Encrypted | Why |
|---|---|---|
| Audio | Yes | This is the recording itself |
| Heartbeat and ACK | Yes | Otherwise an observer learns a session is live |
| Discovery beacon | No | It carries only a device name and IP address. Encrypting it would make the phone hard to find for no gain |

## 5. What the user is told when something goes wrong

No error may be silent. Every case gets a plain sentence, because the user reads
the screen rather than the display.

| Situation | Message |
|---|---|
| Wrong code typed | "The code did not match. Ask your computer to show it again." |
| Computer pairing is stale | "The computer needs to be paired again. On your computer press Forget Phone, then connect again." |
| Someone else sending packets | "Someone else is trying to connect. Rejected." |
| Encryption cannot start | "Secure audio could not start. Install the required library, or run without encryption." |

The last row matters most. If encryption fails, the app must say so plainly and must
not quietly carry on sending audio in the clear.

## 6. Effort

Roughly one day, in small steps that are shown and checked one at a time:

1. Install the library on Windows. About 1 minute.
2. Phone side code. One to two hours.
3. Windows side code. Two to three hours.
4. Tests for each step.
5. A run on the real phone with the real computer.

## 7. Open questions to settle when work starts

- Whether a wrong-code attempt should be counted and eventually blocked, so that a
  12-character code cannot be slowly guessed on the network.
- Whether the Terms wording changes now or only when the feature ships. If it ships
  later, the wording must not describe it as already present.