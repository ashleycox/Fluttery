"""Update discovery and verified replacement of a writable Linux AppImage."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import platform
import re
import ssl
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

import certifi
from packaging.version import InvalidVersion, Version

from .staging import atomic_output

REPOSITORY = 'https://github.com/ashleycox/Fluttery'
RELEASES_PAGE = f'{REPOSITORY}/releases/latest'
RELEASES_API = 'https://api.github.com/repos/ashleycox/Fluttery/releases/latest'
MAX_ASSET_BYTES = 1024 * 1024 * 1024
APPIMAGE_HEADER_BYTES = 20
APPIMAGE_MACHINES = {62, 183}  # ELF x86-64 and AArch64.


@dataclass(frozen=True, slots=True)
class Asset:
    url: str
    size: int
    digest: str


@dataclass(frozen=True, slots=True)
class Release:
    version: str
    page: str
    asset: Asset | None


class _HttpsRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlsplit(newurl)
        if target.scheme != 'https' or target.username or target.password:
            raise OSError('The update server tried to redirect to an insecure address.')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url, *, api=False):
    headers = {'User-Agent': 'Fluttery-updater'}
    if api:
        headers['Accept'] = 'application/vnd.github+json'
    context = ssl.create_default_context(cafile=certifi.where())
    opener = build_opener(_HttpsRedirect, HTTPSHandler(context=context))
    return opener.open(Request(url, headers=headers), timeout=12)


def _linux_asset_name(machine):
    match machine.lower():
        case 'x86_64' | 'amd64':
            return 'fluttery-linux-x64.AppImage'
        case 'aarch64' | 'arm64':
            return 'fluttery-linux-arm64.AppImage'
        case _:
            return ''


def parse_release(payload, current_version, *, machine=None):
    tag = payload.get('tag_name', '')
    if payload.get('draft') or payload.get('prerelease') or not isinstance(tag, str):
        return None
    try:
        latest = Version(tag)
        if latest <= Version(current_version) or latest.is_prerelease or latest.is_devrelease:
            return None
    except InvalidVersion:
        return None
    escaped_tag = quote(tag, safe='')
    asset_name = _linux_asset_name(machine or platform.machine())
    expected_url = f'{REPOSITORY}/releases/download/{escaped_tag}/{asset_name}'
    asset = None
    assets = payload.get('assets')
    for entry in assets if isinstance(assets, list) else ():
        if not isinstance(entry, dict):
            continue
        digest = entry.get('digest', '')
        size = entry.get('size')
        if (asset_name and entry.get('name') == asset_name
                and entry.get('browser_download_url') == expected_url
                and isinstance(digest, str) and re.fullmatch(r'sha256:[0-9a-f]{64}', digest)
                and isinstance(size, int) and APPIMAGE_HEADER_BYTES <= size <= MAX_ASSET_BYTES):
            asset = Asset(expected_url, size, digest.removeprefix('sha256:'))
            break
    return Release(str(latest), f'{REPOSITORY}/releases/tag/{escaped_tag}', asset)


def latest_release(current_version):
    try:
        with _open(RELEASES_API, api=True) as response:
            body = response.read(2_000_001)
    except HTTPError as error:
        if error.code == 404:
            return None  # A new repository can have no published releases yet.
        raise
    if len(body) > 2_000_000:
        raise ValueError('The release information is too large to read.')
    metadata = json.loads(body)
    return parse_release(metadata, current_version) if isinstance(metadata, dict) else None


def current_appimage():
    if platform.system() != 'Linux' or not getattr(sys, 'frozen', False):
        return None
    path = Path(os.environ.get('APPIMAGE', ''))
    mount = Path(os.environ.get('APPDIR', ''))
    if not path.is_absolute() or not mount.is_absolute() or not os.access(path.parent, os.W_OK):
        return None
    try:
        # Confirm the running executable is the one AppRun starts inside the image.
        # A source or Debian install can inherit these variables from another app.
        executable = mount / 'usr/lib/fluttery/Fluttery'
        if mount.resolve() == Path('/') or executable.resolve() != Path(sys.executable).resolve():
            return None
        _inspect_appimage(path)
    except (OSError, RuntimeError):
        return None
    return path


def _appimage_machine(header):
    # The AppImage marker occupies padding in the little-endian ELF64 header.
    if len(header) >= APPIMAGE_HEADER_BYTES and header[:6] == b'\x7fELF\x02\x01' and header[8:11] == b'AI\x02':
        return int.from_bytes(header[18:20], 'little')
    return None


def _inspect_appimage(path):
    # Inspect the opened file so its identity and header describe the same data.
    # Reject symlinks without following them and avoid blocking on a replaced FIFO.
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    with os.fdopen(os.open(path, flags), 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or not info.st_mode & 0o111:
            raise OSError('The current AppImage is not an executable file.')
        machine = _appimage_machine(stream.read(APPIMAGE_HEADER_BYTES))
    if machine not in APPIMAGE_MACHINES:
        raise OSError('The current file is not a supported type 2 AppImage.')
    return info, machine


def install_appimage(asset, destination):
    """Replace the old AppImage only after verifying the entire new file."""
    destination = Path(destination)
    original, machine = _inspect_appimage(destination)
    with atomic_output(destination) as output:
        with _open(asset.url) as response:
            digest = hashlib.sha256()
            length = 0
            header = bytearray()
            while chunk := response.read(1024 * 1024):
                if len(header) < APPIMAGE_HEADER_BYTES:
                    header.extend(chunk[:APPIMAGE_HEADER_BYTES - len(header)])
                    if len(header) == APPIMAGE_HEADER_BYTES:
                        downloaded_machine = _appimage_machine(header)
                        if downloaded_machine is None:
                            raise ValueError('The downloaded file is not a type 2 AppImage.')
                        if downloaded_machine != machine:
                            raise ValueError('The downloaded AppImage uses a different processor architecture.')
                length += len(chunk)
                if length > asset.size:
                    raise ValueError('The downloaded file is larger than expected.')
                digest.update(chunk)
                output.write(chunk)
            if len(header) < APPIMAGE_HEADER_BYTES:
                raise ValueError('The downloaded file is not a type 2 AppImage.')
            if length != asset.size or not hmac.compare_digest(digest.hexdigest(), asset.digest):
                raise ValueError('The download failed its SHA-256 integrity check.')
            os.fchmod(output.fileno(), stat.S_IMODE(original.st_mode) & 0o777)
            output.flush()
            os.fsync(output.fileno())
        try:
            current, current_machine = _inspect_appimage(destination)
        except OSError as error:
            raise OSError('The current AppImage changed while the update was downloading.') from error
        if (not os.path.samestat(original, current) or current_machine != machine
                or current.st_mtime_ns != original.st_mtime_ns or current.st_ctime_ns != original.st_ctime_ns):
            raise OSError('The current AppImage changed while the update was downloading.')
