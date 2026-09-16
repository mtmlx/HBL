"""Filesystem boundaries shared by HTTP generation and package registration."""
import os
from pathlib import Path
import re


def confined_path(path, root):
    if root is None:
        raise ValueError("An explicitly authorized output root is required.")
    base = os.path.realpath(root)
    resolved = os.path.realpath(path)
    if resolved != base and not resolved.startswith(base.rstrip(os.sep) + os.sep):
        raise ValueError("Output path is outside the authorized directory.")
    return Path(resolved)


def filename_component(value):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
        raise ValueError("Document identifiers must not contain path separators or unsafe filename characters.")
    return value
