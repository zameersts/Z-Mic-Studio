"""
ZMic Studio - Backward Compatibility Forwarder

The Windows companion was renamed from `phone_mic_gui.py` to `zmic_studio_gui.py`.
This module keeps the old entry point and the old public names working, so existing
launchers, documentation and third-party scripts continue to function.

Created & Designed by Zameer
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import zmic_studio_gui
from zmic_studio_gui import main  # noqa: F401  (re-exported for `python phone_mic_gui.py`)


def __getattr__(name):
    """Re-export any public name from the real application module."""
    try:
        return getattr(zmic_studio_gui, name)
    except AttributeError:
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
        ) from None


def __dir__():
    return sorted(set(dir(zmic_studio_gui)) | {"main"})


if __name__ == "__main__":
    sys.exit(main())
