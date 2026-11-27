"""Structural validation of captured screenshots -- ONE implementation.

Imported by BOTH the task worker (staged into the container alongside the
app's runtime) and the test suite, deliberately not copied into each. The
worker's copy and the tested copy drifting apart is exactly how a validator
stops validating what you think it validates, and a screenshot is evidence.

WHAT THIS CHECKS, AND WHAT IT DELIBERATELY DOES NOT
---------------------------------------------------
Structure only: the right dashboards are present, each file exists, decodes as
a real PNG, and is at least MIN_WIDTH wide.

It does NOT judge CONTENT. No pixel-variance, entropy, file-size or
minimum-height test. Sheet 22 in the Superstore workbook is a genuinely empty
placeholder sheet -- confirmed twice over, against the IR (`non_data: True`,
no dims, no measures) and against Tableau's OWN screenshot of the same sheet,
which is equally blank -- and it captures as a real, full-width, ~66px-tall
PNG. A blankness check would fail every single validation run on a dashboard
that can never look any different, and would burn all three capture attempts
doing it.

Width IS enforced, because every real dashboard renders at the app's full
viewport width regardless of content, so a narrow image means corruption
rather than an empty dashboard. A full-sized blank screenshot is valid
evidence; a 1x1 or truncated PNG is not.
"""

from __future__ import annotations

import io
import os

MIN_WIDTH = 640

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def validate_capture(files: dict, out_dir: str, expected_titles,
                     min_width: int = MIN_WIDTH):
    """(ok, reason). `files` is {title: {"file": name, ...}} as the capture
    manifest records it; `expected_titles` is what the workbook declared.

    Returns the FIRST problem found, named specifically enough to act on --
    "9 of 10 captured" is useless without knowing which one and why.
    """
    expected = set(expected_titles)
    got = set(files)
    if got != expected:
        missing = sorted(expected - got)
        unexpected = sorted(got - expected)
        return False, (f"title mismatch -- missing {missing or 'none'}, "
                       f"unexpected {unexpected or 'none'}")

    for title, info in files.items():
        name = info.get("file") if isinstance(info, dict) else None
        if not name:
            return False, f"{title!r}: manifest entry has no file name"
        path = os.path.join(out_dir, name)
        if not os.path.exists(path):
            return False, f"{title!r}: listed in the manifest but not on disk"
        with open(path, "rb") as fh:
            data = fh.read()
        if not data.startswith(PNG_MAGIC):
            return False, f"{title!r}: not a valid PNG signature"
        try:
            from PIL import Image
            Image.open(io.BytesIO(data)).verify()
            with Image.open(io.BytesIO(data)) as img:
                img.load()
                width, height = img.size
        except Exception as exc:                                # noqa: BLE001
            return False, (f"{title!r}: PIL could not open/verify/load: "
                           f"{type(exc).__name__}: {exc}")
        if width <= 0 or height <= 0:
            return False, f"{title!r}: zero-sized image ({width}x{height})"
        if width < min_width:
            # Height is intentionally unchecked -- see the module docstring.
            return False, (f"{title!r}: below minimum width ({width}x{height}, "
                           f"need >= {min_width} wide)")
    return True, None


__all__ = ["validate_capture", "MIN_WIDTH", "PNG_MAGIC"]
