"""
Windows Firewall rules for Z Mic Studio, created by the app on the user's behalf.

Why this exists. The app receives UDP audio on ports 8088 and 8089. Windows blocks
unsolicited inbound traffic by default, so a program that has never asked for permission
silently receives nothing. The symptom is the worst kind: the window opens, the phone is
discovered, the status reads Connected and Streaming Active, and no sound ever arrives. There
is no error anywhere, because from the PC's point of view nothing failed.

Windows does normally prompt the first time a program binds a listening port, and clicking
Allow does create the rule. That is not enough. The user can click Cancel, which leaves the
app silently mute. The prompt offers a Private/Public checkbox pair and both networks have to
be covered, or the rule will not apply. On managed machines the prompt sometimes never
appears at all. And the rule is written against one exact executable path, so installing to
Program Files after having run from a folder silently invalidates it.

So the app asks once, in its own words, and then adds the rules itself. Still one consent
from the user, because administrator rights genuinely are required, but nothing is left to a
dialog whose defaults are easy to get wrong.

Running from source needs nothing. The interpreter already has an inbound rule, and the
permission is not the app's to ask for, so the whole prompt is skipped in that mode.

netsh is used rather than the Win32 firewall API because netsh works on any Windows the app
supports without an extra dependency, and it reads the same rule store the GUI does.
"""

import os
import sys
import tempfile
import time

# Ports the app listens on: 8088 receives audio, 8089 receives discovery and pairing.
PORTS = (8088, 8089)

# Only UDP is opened. Nothing here ever accepts a TCP connection.
RULE_PREFIX = "Z Mic Studio audio (UDP "

# Statuses returned by ensure_rules and read by the UI.
GRANTED = "granted"
NEEDS_GRANT = "needs_grant"
NOT_APPLICABLE = "not_applicable"
CANCELLED = "cancelled"
FAILED = "failed"


def rule_name(port):
    return f"{RULE_PREFIX}{port})"


def is_compiled():
    """True when running as a built executable, False when running from source.

    Nuitka marks compiled modules with __compiled__, and also sets sys.frozen. Either is
    enough, and both are checked so a build change does not silently disable the prompt.
    """
    if "__compiled__" in globals():
        return True
    return bool(getattr(sys, "frozen", False))


def target_executable():
    """The program the firewall must name in its rules, or None when there is nothing to grant.

    In source mode the interpreter is the program holding the sockets, and it already carries
    its own broad rule, so the app does not claim it.
    """
    if not is_compiled():
        return None
    return os.path.abspath(sys.executable)


def granted_path(settings):
    """The executable path the user has already granted, recorded in the settings file."""
    return (settings.get("firewall_granted_for") or "").strip()


def needs_grant(settings):
    """True when this executable has never been granted, or was granted under another path.

    Keying on the path means reinstalling to Program Files re-asks, which is exactly when the
    old rule stops matching and the app would otherwise go mute for no visible reason.
    """
    target = target_executable()
    if target is None:
        return False
    return granted_path(settings).lower() != target.lower()


def _work_dir():
    return os.path.join(tempfile.gettempdir(), "ZMicStudioFirewall")


def _paths(tag):
    base = os.path.join(_work_dir(), tag)
    return base + ".bat", base + ".result"


def _netsh_add(port, exe):
    # Double the quotes because the whole thing lives inside a batch file. profile=any is
    # deliberate: both of this PC's networks were Public, and a Private-only rule would have
    # looked granted while letting nothing through.
    prog = exe.replace('"', '""')
    return (
        f'netsh advfirewall firewall delete rule name="{rule_name(port)}" >nul 2>&1\r\n'
        f'netsh advfirewall firewall add rule name="{rule_name(port)}" '
        f'dir=in action=allow protocol=UDP localport={port} '
        f'program="{prog}" enable=yes profile=any\r\n'
    )


def _netsh_check(port):
    return (
        f'chcp 65001 >nul 2>&1\r\n'
        f'netsh advfirewall firewall show rule name="{rule_name(port)}" 2>nul '
        f'| findstr /i /c:"{RULE_PREFIX}" >nul\r\n'
        f'if errorlevel 1 (echo {port} MISSING) else (echo {port} PRESENT)\r\n'
    )


def _netsh_remove(port):
    return f'netsh advfirewall firewall delete rule name="{rule_name(port)}" >nul 2>&1\r\n'


def _bat_header(tag):
    folder = _work_dir()
    _, result = _paths(tag)
    return (
        "@echo off\r\n"
        f'mkdir "{folder}" >nul 2>&1\r\n'
        f'chcp 65001 >nul 2>&1\r\n'
        f'echo START > "{result}"\r\n'
    )


def _bat_footer(tag):
    _, result = _paths(tag)
    return f'echo DONE >> "{result}"\r\n'


def _write_bat(tag, body):
    bat, result = _paths(tag)
    os.makedirs(_work_dir(), exist_ok=True)
    with open(bat, "w", encoding="ascii", newline="") as fh:
        fh.write(_bat_header(tag) + body + _bat_footer(tag))
    for stale in (result,):
        try:
            os.remove(stale)
        except OSError:
            pass
    return bat, result


def _elevate(bat, wait_seconds=45):
    """Runs the batch file as administrator. Returns False if the user declined the prompt."""
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return False

    SW_HIDE = 0

    ShellExecuteW = ctypes.windll.shell32.ShellExecuteW
    ShellExecuteW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
                              wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int]
    ShellExecuteW.restype = wintypes.HINSTANCE

    rc = ShellExecuteW(None, "runas", "cmd.exe", f'/c ""{bat}""', None, SW_HIDE)
    # 32 is the classic "cancelled by the user" code for the elevated launch.
    return rc != 32


def _read_result(path, wait_seconds=45):
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                    text = fh.read()
                if "DONE" in text:
                    return text
            except OSError:
                pass
        time.sleep(0.4)
    return ""


def ensure_rules(exe_path):
    """Adds the inbound UDP rules for exe_path. Returns (status, detail).

    Grants are idempotent: the existing rules are deleted first, so re-running after an
    upgrade repairs a half-written rule rather than adding a duplicate.
    """
    if not exe_path:
        return NOT_APPLICABLE, "Running from source. The interpreter already has permission."

    tag = f"grant_{int(time.time() * 1000)}"
    body = "".join(_netsh_add(port, exe_path) for port in PORTS)
    bat, result = _write_bat(tag, body)

    if not _elevate(bat):
        return CANCELLED, "Windows permission was declined, so the phone will not be heard."

    text = _read_result(result)
    if "DONE" not in text:
        return FAILED, "The permission step did not finish. It may have been blocked by policy."

    try:
        os.remove(bat)
    except OSError:
        pass
    return GRANTED, f"Windows Firewall now lets Z Mic Studio receive audio on ports " \
                    f"{' and '.join(str(p) for p in PORTS)}."


def rules_present(exe_path):
    """True when the firewall reports a rule for exe_path. Needs elevation, so it prompts."""
    if not exe_path:
        return False
    tag = f"check_{int(time.time() * 1000)}"
    body = "".join(_netsh_check(port) for port in PORTS)
    bat, result = _write_bat(tag, body)

    if not _elevate(bat):
        return False
    text = _read_result(result)
    try:
        os.remove(bat)
    except OSError:
        pass

    if "DONE" not in text:
        return False
    return all(f"{port} PRESENT" in text for port in PORTS)


def revoke():
    """Removes the rules this app added. Used by the About tab so the change is reversible."""
    tag = f"revoke_{int(time.time() * 1000)}"
    body = "".join(_netsh_remove(port) for port in PORTS)
    bat, result = _write_bat(tag, body)
    if not _elevate(bat):
        return False
    text = _read_result(result)
    try:
        os.remove(bat)
    except OSError:
        pass
    return "DONE" in text


def describe(settings):
    """A short line for the About tab, plus whether the repair button should be offered."""
    target = target_executable()
    if target is None:
        return "Not needed. Running from source; Python already has permission."
    if granted_path(settings):
        return "Windows permission granted for this copy of Z Mic Studio."
    return "Not granted yet. Without it the phone connects but no sound arrives."
