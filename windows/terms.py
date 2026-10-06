"""
Z Mic Studio - Terms of Use, Privacy and Acceptance Storage

Keeps the terms text in one place so the first run wizard, the Help tab and the
About window all show exactly the same wording.

Created & Designed by Zameer
"""
import json
import os
import time

# Bump this whenever the wording changes. Anyone who accepted an older version is
# asked to read and accept again.
TERMS_VERSION = "0.9.0"

TERMS_SECTIONS = [
    (
        "What This App Does",
        "Z Mic Studio turns your Android phone into a microphone for your Windows "
        "computer over your local Wi-Fi network.\n\n"
        "The audio goes straight from your phone to your computer, using the "
        "local network only. No internet is used.\n\n"
        "Your computer needs a virtual audio cable installed. That is a free "
        "program called VB-Audio Virtual Cable."
    ),
    (
        "Acceptable Use",
        "Please do not use this app to:\n\n"
        "Pretend to be another person.\n"
        "Hide who you really are.\n"
        "Record other people talking without their permission.\n"
        "Lie to, bully, or cheat anyone.\n"
        "Get around the rules of another app.\n"
        "Break any law, including privacy and recording laws.\n\n"
        "Recording people without their permission is illegal in many countries. "
        "You are responsible for what you do with any audio you record.\n\n"
        "The person who made this app cannot be responsible for recordings you "
        "make with it."
    ),
    (
        "Your Privacy",
        "Your audio never leaves your devices. It travels only between your phone "
        "and your computer.\n\n"
        "This app has no user account. It has no advertising. It has no tracking "
        "and no analytics.\n\n"
        "Z Mic Studio does not connect to the internet. It never sends anything "
        "about you or your computer anywhere. It does not check for updates by "
        "itself.\n\n"
        "The only time it opens a web page is to take you to the VB-Audio website, "
        "and only when it cannot find the virtual audio cable you need.\n\n"
        "Your settings and log files stay on your computer only.\n\n"
        "If you want to send feedback or ask for help, you can email " +
        "zameer.sstr@gmail.com. You have to write that address yourself, " +
        "because this app cannot send anything by itself."
    ),
    (
        "Third Party Program",
        "This app needs VB-Audio Virtual Cable. That program is made by Vincent "
        "Burel and is not part of Z Mic Studio.\n\n"
        "It is free to download from www.vb-cable.com. You download and install it "
        "yourself.\n\n"
        "Z Mic Studio is not connected to, or approved by, VB-Audio."
    ),
    (
        "No Guarantee",
        "This app is provided as it is. There is no promise that it will work "
        "without problems.\n\n"
        "The person who made this app is not responsible for lost data, poor audio "
        "quality, or any damage caused by using this app."
    ),
]

TERMS_CHECKBOX_LABEL = "I have read and agree to the Terms and Privacy above."


def get_settings_path():
    """Absolute path of the local settings file that stores acceptance."""
    import zmic_studio_gui
    return os.path.join(zmic_studio_gui.LOG_DIR, 'settings.json')


def load_settings():
    """Reads the local settings file. Returns an empty dict on any problem."""
    try:
        path = get_settings_path()
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def save_settings(data):
    """Writes the local settings file. Returns True on success."""
    try:
        path = get_settings_path()
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, indent=2)
        return True
    except Exception:
        return False


def is_terms_accepted():
    """True when the user accepted the current wording of the terms."""
    return load_settings().get('accepted_terms_version') == TERMS_VERSION


def record_terms_accepted():
    """Stores acceptance of the current terms. Returns True on success."""
    settings = load_settings()
    settings['accepted_terms_version'] = TERMS_VERSION
    settings['accepted_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
    return save_settings(settings)


def clear_terms_acceptance():
    """Forgets acceptance so the wizard shows again. Used by tests and diagnostics."""
    settings = load_settings()
    settings.pop('accepted_terms_version', None)
    settings.pop('accepted_at', None)
    return save_settings(settings)


def get_terms_as_text():
    """The full terms as one plain-text block, for speech or a plain text view."""
    parts = []
    for heading, body in TERMS_SECTIONS:
        parts.append(heading)
        parts.append('')
        parts.append(body)
        parts.append('')
    return '\n'.join(parts).strip()