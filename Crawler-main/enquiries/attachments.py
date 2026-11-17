"""Checks for files attached to an enquiry: allowed types and a size limit."""
from __future__ import annotations

import os
from typing import Optional

MAX_BYTES = 10 * 1024 * 1024
ALLOWED_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".xls", ".xlsx", ".csv",
                      ".doc", ".docx", ".txt", ".eml", ".msg"}


def check_upload(uploaded) -> Optional[str]:
    """None when the file may be attached, otherwise what is wrong with it."""
    name = os.path.basename(getattr(uploaded, "name", "") or "")
    ext = os.path.splitext(name)[1].lower()
    if not name:
        return "The file has no name."
    if ext not in ALLOWED_EXTENSIONS:
        return f"{name}: this kind of file cannot be attached. Allowed: {', '.join(sorted(e[1:] for e in ALLOWED_EXTENSIONS))}."
    if getattr(uploaded, "size", 0) > MAX_BYTES:
        return f"{name} is larger than {MAX_BYTES // (1024 * 1024)} MB."
    if getattr(uploaded, "size", 0) == 0:
        return f"{name} is empty."
    return None
