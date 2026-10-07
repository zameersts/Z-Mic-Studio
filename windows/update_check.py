"""
Looks up whether a newer Z Mic Studio has been published.

Kept separate from the GUI because the address is a setting rather than a constant. The app
was built before any public home for it existed, so the place to look is entered in the About
tab. Hardcoding a URL would mean rebuilding every time the home changes, which is exactly the
kind of trap that turns a small project into a chore.

Two shapes are understood, both a plain text response:

  a bare version   "0.9.1" or "0.9.1\\n"
  a one line JSON  {"version": "0.9.1"}

Only the version is compared. No file is downloaded and nothing is installed by this module.
Asking whether a newer version exists and acting on the answer are separate decisions, and
keeping them apart is what makes it possible to add the second one later without touching
this.

There is no timeout and no retry loop by design. A slow or unreachable address must not delay
the app, and the user asked for this by pressing a button, so there is nothing to hide.
"""

import json
import re
import urllib.error
import urllib.request

TIMEOUT_SECONDS = 8

# Accepts 0.9, 0.9.1, 0.9.1-beta, v0.9.1. Anything else is not treated as a version, because
# guessing at an address's contents could report a wrong answer with total confidence.
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)(?:\.(\d+))?(?:[-+](.+))?$")


def parse_version(text):
    """Returns (major, minor, patch, suffix) for a version string, or None if it is not one."""
    if not text:
        return None
    match = _VERSION_RE.match(text.strip())
    if not match:
        return None
    major = int(match.group(1))
    minor = int(match.group(2))
    patch = int(match.group(3)) if match.group(3) else 0
    return major, minor, patch, (match.group(4) or "").lower()


def is_newer(candidate, current):
    """True when candidate is a strictly newer version than current.

    A pre-release sorts below its own release, so 0.9.1-beta does not replace 0.9.1. Getting
    this wrong would either hide an update from a beta user or push a release onto someone who
    chose the beta channel on purpose.
    """
    a = parse_version(candidate)
    b = parse_version(current)
    if a is None or b is None:
        return False
    for i in range(3):
        if a[i] != b[i]:
            return a[i] > b[i]
    # Same numbers on both sides, so the suffix decides. A release outranks a pre-release of
    # the same version, and two pre-releases are ordered by their suffix. When the suffixes
    # are equal there is nothing to offer.
    # Equal numbers means equal versions for deciding whether to offer a download. A release
    # and its own pre-release differ only in suffix, and someone already on the beta has no
    # reason to be pushed onto the release. The suffix is kept for reporting, not for deciding.
    return False


def extract_version(body):
    """Pulls a version out of a plain text or single line JSON response."""
    text = (body or "").strip()
    if not text:
        return None
    if text[0] in "{[":
        try:
            data = json.loads(text)
        except ValueError:
            return None
        if isinstance(data, dict):
            for key in ("version", "latest", "tag", "tag_name", "current_version"):
                if key in data:
                    found = parse_version(str(data[key]))
                    if found:
                        return str(data[key]).strip().lstrip("vV")
        return None
    first = text.splitlines()[0].strip()
    if parse_version(first):
        return first.lstrip("vV")
    return None


def check_for_update(source, current_version):
    """Returns {'ok', 'latest', 'error'}. Never raises, never installs anything."""
    source = (source or "").strip()
    if not source:
        return {"ok": False, "error": "empty"}

    url = source
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "ZMicStudio-UpdateCheck",
            # GitHub's API rejects requests without one.
            "Accept": "application/vnd.github+json, text/plain, */*",
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            body = response.read(64 * 1024).decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": f"http_{exc.code}"}
    except Exception:
        return {"ok": False, "error": "unreachable"}

    latest = extract_version(body)
    if not latest:
        return {"ok": False, "error": "no_version"}

    return {
        "ok": True,
        "latest": latest,
        "newer": is_newer(latest, current_version),
    }
