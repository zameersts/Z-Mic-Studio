# Project Rules & Environment Configuration

## Android Debug Bridge (ADB) & Device Connection
- **Platform Tools Directory**: `%LOCALAPPDATA%\Android\platform-tools`
- **ADB Executable**: `%LOCALAPPDATA%\Android\platform-tools\adb.exe`
- **Device Detection**: Whenever interacting with Android devices or checking device status, always use this specific ADB path.
- If a device is not yet detected or shown, running `& "%LOCALAPPDATA%\Android\platform-tools\adb.exe" devices` will start the ADB server daemon and connect to the device.

## Project Memory & State
- Always check `PROGRESS.md` to see completed milestones, active status, and technical specifications before beginning any task.

## Ponytail Boundaries & Strict Scope Rule
- Ponytail must NEVER interfere with product decisions, user-requested features, sound choices, or libraries.
- The USER has complete authority over all features, sound design, and dependencies.
- Ponytail's role is strictly confined to code-level efficiency: writing clean, bug-free code without lecturing, debating, or forcing unwanted alternatives on the user.
