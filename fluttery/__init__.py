"""Fluttery desktop audio measurement tools."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("fluttery")
except PackageNotFoundError:
    # Running directly from a checkout is supported for local development.
    __version__ = "1.0.0"
