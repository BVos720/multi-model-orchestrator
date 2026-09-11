from __future__ import annotations

from datetime import datetime
from pathlib import Path

ATTACH_DIR = Path(".orchestrator") / "attachments"


def save_clipboard_image() -> Path | None:
    """Grab whatever image is currently on the OS clipboard and save it.

    This is NOT a literal Ctrl+V inside the task prompt - a terminal text
    input can only ever receive text, never image bytes, no matter what key
    you press; that's a limitation of terminals, not something this code
    can work around. Screenshot normally (Win+Shift+S / PrtScn), THEN pick
    this menu option to pull that clipboard image in as a file.

    Returns the saved path, or None if the clipboard doesn't currently hold
    an image (e.g. you copied text instead, or copied nothing).
    """
    from PIL import ImageGrab  # imported lazily - only needed if you use this

    img = ImageGrab.grabclipboard()
    if img is None:
        return None

    ATTACH_DIR.mkdir(parents=True, exist_ok=True)
    path = ATTACH_DIR / f"clipboard-{datetime.now():%Y%m%d-%H%M%S}.png"
    img.save(path)
    return path
