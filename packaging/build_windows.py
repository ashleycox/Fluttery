"""Package Fluttery for Windows from macOS or Linux using native target wheels."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECIPE = ROOT / "packaging"
PYTHON_VERSION = "3.13.18"
PYTHON_ABI = "3.13"
PYTHON_RELEASE = "20261110"
UV_VERSION = "0.13.9"
LLVM_RELEASE = "20261117"
GITHUB = "https://github.com"
SOURCE_DATE_EPOCH = "946684800"

# Pins are published release digests. The compiler hosts and CPython targets
# are independent. One Apple silicon Mac can build either native Windows app.
TARGETS = {
    "x64": ("x86_64", 0x8664, "2e0fed09d48989f64c9305da9ff2d37a41c0d13ab2aa6283405b2b620dfbf805"),
    "arm64": ("aarch64", 0xAA64, "0a8840071810ddb8565256cd406b466d4e9dc6dc523395a5477880dbc4f67eb3"),
}
MAC_COMPILER = ("macos-universal", "98d340bbb8d46b09ad6886ffba88bd9d5415e131c316de48e6bd7647377446aa")
COMPILER_HOSTS = {
    ("darwin", "arm64"): MAC_COMPILER,
    ("darwin", "x86_64"): MAC_COMPILER,
    ("linux", "aarch64"): ("ubuntu-22.04-aarch64", "6d9442ead9ace3b3e4c3389bfef69f93f873305e0705e6cc60978ac895320872"),
    ("linux", "x86_64"): ("ubuntu-22.04-x86_64", "e7a15a49a2ea95e9f3d8a9d0cfabe5052a4b10fb4777cc54de5a8c27b64bb701"),
}

# The full CP313 dependency closure for both Windows targets. Each entry
# accepts only the PyPI wheel for that platform, or one common pure-Python
# wheel. Hash enforcement prevents a source build or a substituted artifact.
WHEELS = {
    "certifi": ("2026.11.20", "7422df25243767b9877031a2248cb14d09ddac9beee846f4cef3d5c9191d488f"),
    "cffi": (
        "2.1.1",
        "1aa5645c30469b09530c4ebca77ebf8f17618293c58f8549cb1a543a50236e7d",
        "63bbfd5ded17c4840ac07cd8f1c21ba9d9708141f840b324f422f41b207e3973",
    ),
    "charset-normalizer": (
        "3.5.3",
        "cb18428af7399e0a6f7e3b45f110d57a1a47cf07fd4b3ce50e2366d99a38959a",
        "8b969f0309de905101bc3d2471c112e9ccbba711a7590b12d51f372b4163688a",
    ),
    "contourpy": (
        "1.4.0",
        "6507a016976a75a15aee47809a34605de6244ccc056e6164bf9fb14b27eecad4",
        "423bd5f4b3f11d54a8c597234e513382e3454bb106b8ec7ae32ba4ed63f8d99b",
    ),
    "cycler": ("0.12.1", "85cef7cff222d8644161529808465972e51340599459b8ac3ccbac5a854e0d30"),
    "fonttools": (
        "4.69.1",
        "a1dedfe496cf57d0ff5f3874aaa87f618a61062e39d9cb6a8b809b87a8fe2ee9",
        "6186fc88e6d2a899bf5dfc4a9f959beb4d19e6e8e1b0d64617909706851a03c9",
    ),
    "kiwisolver": (
        "1.5.1",
        "dc23390afe9f4ef9ac3bcc72a03a56eebbde03f4c571a32cb38f859cff9a6524",
        "186884a58486651e3c217b6acea0a53eaa9498fdd472057c46f2f0fb5c25aad5",
    ),
    "matplotlib": (
        "3.11.2",
        "bc067c462a86f0e57bf52fc6e058d90171a5420007dac00c46e90153050c69a7",
        "3e8576f7c47e02fd4f21f44171302d2d1d58d4471d46da3d71fe8899d19539d9",
    ),
    "numpy": (
        "2.5.5",
        "e0608d1585557b68dcb6a70a41654f7536567960afad79d91752d2863b242b68",
        "e0b87886bb45621d9279dad4ab9f2d154b5db0b019d378c107b8f71527b06a4a",
    ),
    "packaging": ("26.4", "81383611a09c1bc0b08b76e0fd5829d7c6824c65b27a6aee339da133f4233ea2"),
    "pillow": (
        "13.0.0",
        "9dda0788a9932776a75d8bcc020076edc308ce2508b51f6739a45e3158b3fc42",
        "40a266371ece581e5de13b6635ef78d7487824af80561b6ca27694a88c19bc0f",
    ),
    "platformdirs": ("4.14.8", "1e34af590de4d978631b751eb2202f2fee4497537a17c1720b12fcf57b331392"),
    "pycparser": ("3.12", "a076f3f489b39b43ed13563a1dafe42447445bec2e294154ec5c57fcdda4c85e"),
    "pyparsing": ("3.3.3", "ece8c00a69cf01b45d0b1dedabb469c90d8caf996d4fda40f147627a122849a4"),
    "python-dateutil": ("4.0.0", "a5e246f79eb80475e691bf93862cead98decd8a498cf53ba0516fcdc0f50dc1c"),
    "reportlab": ("4.5.1", "06fce8cb56c83307cfa4909cdf4e6a2ddbb44e5d6ef4d2edca896d7e9769f091"),
    "scipy": (
        "1.18.1",
        "559ed65f60c1af5a03f3912605a1b5114f522c7c32fb23c3376ae8f03219fe28",
        "cd479fc04dd9401e3b4f49e76518768ef99c4f517a98c284eb091fd725719adf",
    ),
    "sounddevice": (
        "0.5.6",
        "7f4162f514f007b0bf25a3ccfed3f1705bc2ec311888a90232729eec4f57a4f4",
        "c8ae19173e5f27f8c12d4b5eee2dbfe542cee125d591e663e0fb4dfb75246d45",
    ),
    "tzdata": ("2026.6", "0a10eec6e13ef16b627fabed9a5f2ba61a62b1c67f327305f4e7e412a70b4aec"),
    "wxpython": (
        "4.3.2",
        "9ef2cec75f570ac09fbcfe31cd668bbf122dc650a2c43a9016aba668c557c725",
        "100d532ddaf457e21421f69fbcaf63d6403dc10d70ca80c1726152aaf4967856",
    ),
}


def run(*command, **options):
    return subprocess.run([str(item) for item in command], check=True, **options)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def fetch(cache, repository, release, name, expected):
    cache.mkdir(parents=True, exist_ok=True)
    destination = cache / name
    if destination.is_file() and digest(destination) == expected:
        return destination
    with tempfile.TemporaryDirectory(dir=cache) as scratch:
        download = Path(scratch) / name
        url = f"{GITHUB}/{repository}/releases/download/{release}/{name}"
        run("curl", "--fail", "--silent", "--show-error", "--location", "--retry", "2", url, "-o", download)
        if digest(download) != expected:
            raise ValueError(f"SHA-256 mismatch for {name}.")
        download.replace(destination)
    return destination


def extract(archive, destination):
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as source:
        source.extractall(destination, filter="data")


def compiler_asset(host):
    name, checksum = COMPILER_HOSTS[host]
    return f"llvm-mingw-{LLVM_RELEASE}-ucrt-{name}", checksum


def python_asset(arch):
    cpu, _machine, checksum = TARGETS[arch]
    name = f"cpython-{PYTHON_VERSION}+{PYTHON_RELEASE}-{cpu}-pc-windows-msvc-install_only_stripped.tar.gz"
    return name, checksum


def project_metadata():
    with (ROOT / "pyproject.toml").open("rb") as stream:
        return tomllib.load(stream)["project"]


def target_platform(arch):
    return f"{TARGETS[arch][0]}-pc-windows-msvc"


def lock_file(path, arch):
    target_index = 1 if arch == "x64" else 2
    lines = []
    for name, (version, *hashes) in WHEELS.items():
        checksum = hashes[0 if len(hashes) == 1 else target_index - 1]
        lines.append(f"{name}=={version} --hash=sha256:{checksum}")
    path.write_text("\n".join(lines) + "\n", encoding="ascii")
    return path


def uv_command():
    uvx = shutil.which("uvx")
    if not uvx:
        raise OSError("Install Astral uv and uvx before preparing Windows dependencies.")
    return uvx, "--from", f"uv=={UV_VERSION}", "uv"


def package_wheels(arch, site_packages, work, *, dry_run=False):
    lock = lock_file(work / "windows-requirements.txt", arch)
    flags = ("--dry-run",) if dry_run else ()
    run(*uv_command(), "pip", "install", "--no-config", "--index", "https://pypi.org/simple",
        "--target", site_packages, "--python-version", PYTHON_ABI, "--python-platform", target_platform(arch),
        "--no-deps", "--only-binary", ":all:", "--require-hashes", "-r", lock, *flags)
    if not dry_run:
        # uv creates console scripts on the build OS. Fluttery starts only from
        # the Windows launcher and uses -m, so the generated scripts are unused.
        scripts = site_packages / "bin"
        if scripts.is_dir():
            shutil.rmtree(scripts)
        # Both sounddevice wheels carry PortAudio for three CPU architectures.
        # Keep the ASIO and non-ASIO variants for the same CPU as Fluttery.
        portaudio = site_packages / "_sounddevice_data" / "portaudio-binaries"
        target = {"x64": "libportaudio64bit", "arm64": "libportaudioarm64"}[arch]
        for stem in ("libportaudio32bit", "libportaudio64bit", "libportaudioarm64"):
            if stem != target:
                for suffix in (".dll", "-asio.dll"):
                    (portaudio / (stem + suffix)).unlink(missing_ok=True)


def check_dependency_closure(arch, work):
    lock = lock_file(work / "pinned-constraints.txt", arch)
    resolved = work / "resolved.txt"
    run(*uv_command(), "pip", "compile", "--no-config", "--index", "https://pypi.org/simple",
        ROOT / "pyproject.toml", "--constraint", lock, "--python-version", PYTHON_ABI,
        "--python-platform", target_platform(arch), "--only-binary", ":all:", "--output-file", resolved, "--quiet")
    expected = {f"{name}=={entry[0]}" for name, entry in WHEELS.items()}
    actual = {line for line in resolved.read_text(encoding="utf-8").splitlines() if line and line[0].isalnum()}
    if expected != actual:
        missing = ", ".join(sorted(actual - expected) or sorted(expected - actual))
        raise ValueError(f"Update the Windows wheel pins for {arch} before packaging. Changed requirements: {missing}")


def pe_type(path):
    """Return machine and subsystem without executing a Windows binary."""
    with path.open("rb") as stream:
        if stream.read(2) != b"MZ":
            raise ValueError(f"Invalid Windows binary: {path}")
        stream.seek(0x3C)
        file_header = int.from_bytes(stream.read(4), "little")
        stream.seek(file_header)
        header = stream.read(26)
        if header[:4] != b"PE\x00\x00" or len(header) != 26 or header[24:26] not in (b"\x0b\x01", b"\x0b\x02"):
            raise ValueError(f"Invalid PE header: {path}")
        stream.seek(file_header + 24 + 68)
        raw = stream.read(2)
        if len(raw) != 2:
            raise ValueError(f"Truncated PE optional header: {path}")
        return int.from_bytes(header[4:6], "little"), int.from_bytes(raw, "little")


def verify_pe(path, machine, subsystem=None):
    actual_machine, actual_subsystem = pe_type(path)
    if actual_machine != machine or (subsystem is not None and actual_subsystem != subsystem):
        raise ValueError(f"Unexpected Windows architecture or subsystem in {path.name}: "
                         f"{actual_machine:#06x}, {actual_subsystem}.")


def prepare_runtime(root, cache, work, arch, project):
    name, checksum = python_asset(arch)
    archive = fetch(cache, "astral-sh/python-build-standalone", PYTHON_RELEASE, name, checksum)
    extract(archive, root)
    runtime = root / "python"
    cpu, machine, _ = TARGETS[arch]
    (runtime / f"python{PYTHON_ABI.replace('.', '')}._pth").write_text(
        ".\nLib\nDLLs\nsite-packages\nimport site\n", encoding="ascii",
    )
    for obsolete in (runtime / "Lib" / "test", runtime / "Lib" / "site-packages", runtime / "Scripts"):
        if obsolete.is_dir():
            shutil.rmtree(obsolete)
    # This Astral archive includes an x64 exception-handling CRT beside the
    # ARM64 runtime. The ARM64 interpreter and target wheels do not import it.
    crt_companion = runtime / "vcruntime140_1.dll"
    if arch == "arm64" and crt_companion.is_file() and pe_type(crt_companion)[0] == TARGETS["x64"][1]:
        crt_companion.unlink()
    verify_pe(runtime / "python.exe", machine, 3)
    verify_pe(runtime / "pythonw.exe", machine, 2)
    site_packages = runtime / "site-packages"
    package_wheels(arch, site_packages, work)
    # The installed distribution metadata makes version discovery independent
    # of the source fallback in __init__.py and works with importlib.metadata.
    info = site_packages / f"fluttery-{project['version']}.dist-info"
    info.mkdir()
    lines = ["Metadata-Version: 2.4", f"Name: {project['name']}", f"Version: {project['version']}",
             f"Requires-Python: {project['requires-python']}"]
    lines.extend(f"Requires-Dist: {dependency}" for dependency in project["dependencies"])
    (info / "METADATA").write_text("\n".join(lines) + "\n", encoding="utf-8")
    source = ROOT / "fluttery"

    def ignore_nonwindows_files(directory, names):
        ignored = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")(directory, names)
        if Path(directory) == source / "manual" / "images":
            ignored.update(set(names) - {"windows", "shared"})
        return ignored

    shutil.copytree(source, site_packages / "fluttery", ignore=ignore_nonwindows_files)
    shutil.copy2(ROOT / "LICENSE", root)
    # Wrong-architecture native libraries can make the GUI fail before it can
    # display an error. Check every executable payload, including PortAudio,
    # wxWidgets and all of the scientific wheel library directories.
    libraries = [path for path in runtime.rglob("*") if path.suffix.lower() in {".pyd", ".dll", ".exe"}]
    if not any(path.name.startswith("_core") and path.parent.name == "wx" for path in libraries) or not any(
        "portaudio" in path.name.lower() for path in libraries
    ):
        raise ValueError(f"The {cpu} wheel set is missing wxPython or PortAudio.")
    for component in libraries:
        verify_pe(component, machine)


MANIFEST = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
  <assemblyIdentity version="1.0.0.0" processorArchitecture="*" name="AshleyCox.Fluttery" type="win32"/>
  <description>Fluttery</description>
  <dependency><dependentAssembly>
    <assemblyIdentity type="win32" name="Microsoft.Windows.Common-Controls" version="6.0.0.0"
      processorArchitecture="*" publicKeyToken="6595b64144ccf1df" language="*"/>
  </dependentAssembly></dependency>
  <trustInfo xmlns="urn:schemas-microsoft-com:asm.v3"><security><requestedPrivileges>
    <requestedExecutionLevel level="asInvoker" uiAccess="false"/>
  </requestedPrivileges></security></trustInfo>
  <compatibility xmlns="urn:schemas-microsoft-com:compatibility.v1"><application>
    <supportedOS Id="{8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a}"/>
  </application></compatibility>
  <application xmlns="urn:schemas-microsoft-com:asm.v3"><windowsSettings>
    <dpiAwareness xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">PerMonitorV2</dpiAwareness>
  </windowsSettings></application>
</assembly>
"""


def make_launcher(root, work, compiler, arch, version):
    from PIL import Image

    cpu, machine, _ = TARGETS[arch]
    cross = compiler / "bin" / f"{cpu}-w64-mingw32"
    icon = work / "Fluttery.ico"
    with Image.open(ROOT / "fluttery" / "assets" / "icon.png") as image:
        image.save(icon, sizes=[(size, size) for size in (16, 24, 32, 48, 64, 128, 256)])
    version_parts = version.split(".")
    if len(version_parts) > 4 or not all(part.isdecimal() and int(part) < 65536 for part in version_parts):
        raise ValueError("Packaging requires a numeric Fluttery version with at most four components.")
    file_version = ", ".join([*version_parts, *(["0"] * (4 - len(version_parts)))])
    (work / "fluttery.manifest").write_text(MANIFEST, encoding="utf-8")
    resources = work / "fluttery.rc"
    resources.write_text(f'''1 ICON "Fluttery.ico"
1 24 "fluttery.manifest"
1 VERSIONINFO
 FILEVERSION {file_version}
 PRODUCTVERSION {file_version}
 FILEOS 0x40004
 FILETYPE 0x1
BEGIN
 BLOCK "StringFileInfo"
 BEGIN
  BLOCK "040904B0"
  BEGIN
   VALUE "FileDescription", "Fluttery"
   VALUE "FileVersion", "{version}"
   VALUE "OriginalFilename", "Fluttery.exe"
   VALUE "ProductName", "Fluttery"
   VALUE "ProductVersion", "{version}"
  END
 END
 BLOCK "VarFileInfo"
 BEGIN
  VALUE "Translation", 0x0409, 1200
 END
END
''', encoding="ascii")
    resource_object = work / "fluttery.res"
    run(f"{cross}-windres", "-i", resources.name, "-O", "coff", "-o", resource_object, cwd=work)
    launcher = root / "Fluttery.exe"
    run(f"{cross}-gcc", "-std=c17", "-O2", "-Wall", "-Wextra", "-Werror", "-municode", "-mwindows",
        "-Wl,--no-insert-timestamp", RECIPE / "windows_launcher.c", resource_object, "-o", launcher)
    verify_pe(launcher, machine, 2)
    return icon


def installer_compiler(requested):
    command = shutil.which(requested or "makensis")
    if not command:
        raise OSError("Install NSIS with makensis on PATH or provide --makensis.")
    output = run(command, "-VERSION", capture_output=True, text=True).stdout.strip()
    if not output.startswith("v3.") and not output.startswith("3."):
        raise ValueError("Fluttery requires an NSIS 3.x installer compiler.")
    return command


def verify_destinations(output, targets, package_format):
    for arch in targets:
        base = output / f"fluttery-windows-{arch}"
        paths = [base.with_suffix(".exe")] if package_format == "installer" else [base]
        if package_format == "both":
            paths.append(base.with_suffix(".exe"))
        for path in paths:
            if path.exists() or path.is_symlink():
                raise FileExistsError(f"Output already exists: {path}")


def build_target(arch, output, work, cache, compiler, makensis, project, package_format):
    from windows_setup import installer_defines

    root = work / f"fluttery-windows-{arch}"
    work.mkdir()
    prepare_runtime(root, cache, work, arch, project)
    icon = make_launcher(root, work, compiler, arch, project["version"])
    # Stage beside the output so a completed artifact can be renamed into place
    # atomically, even when the temporary build and output use different volumes.
    with tempfile.TemporaryDirectory(dir=output, prefix=".fluttery-windows-") as scratch:
        staging = Path(scratch)
        if package_format in ("installer", "both"):
            installer = staging / f"{root.name}.exe"
            run(makensis, "-V2", *installer_defines(root, installer, project["version"], arch, work, icon),
                RECIPE / "installer.nsi",
                env={**os.environ, "SOURCE_DATE_EPOCH": SOURCE_DATE_EPOCH})
            # NSIS has one x86 bootstrap format even when the installed payload
            # is the target's native x64 or ARM64 application.
            verify_pe(installer, 0x014C, 2)
        if package_format in ("portable", "both"):
            shutil.copytree(root, staging / root.name)
        verify_destinations(output, [arch], package_format)
        for item in sorted(staging.iterdir()):
            destination = output / item.name
            item.rename(destination)
            print(f"{destination}  SHA-256 {digest(destination)}" if destination.is_file() else destination)


def describe(host, targets, project, package_format):
    compiler, checksum = compiler_asset(host) if host in COMPILER_HOSTS else ("unsupported", "")
    return {
        "fluttery": project["version"],
        "python": PYTHON_VERSION,
        "uv": UV_VERSION,
        "compiler_archive": f"{compiler}.tar.xz",
        "compiler_sha256": checksum,
        "nsis": "makensis 3.x" if package_format != "portable" else None,
        "targets": [{
            "architecture": arch,
            "python_platform": target_platform(arch),
            "python_archive": python_asset(arch)[0],
            "python_sha256": python_asset(arch)[1],
            "wheel_count": len(WHEELS),
            "installer": f"fluttery-windows-{arch}.exe" if package_format != "portable" else None,
            "portable": f"fluttery-windows-{arch}" if package_format != "installer" else None,
        } for arch in targets],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arch", choices=(*TARGETS, "both"), default="both")
    parser.add_argument("--format", choices=("installer", "portable", "both"), default="installer")
    parser.add_argument("--output", type=Path, help="Destination for completed artifacts")
    parser.add_argument("--cache", type=Path, default=Path(tempfile.gettempdir()) / "fluttery-windows-cache")
    parser.add_argument("--makensis", help="NSIS installer compiler executable, default makensis on PATH")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--plan", action="store_true", help="Describe targets and pins without downloads or outputs")
    modes.add_argument("--check-dependencies", action="store_true", help="Check pinned target resolution without building")
    args = parser.parse_args()
    host = sys.platform, platform.machine().lower()
    targets = tuple(TARGETS) if args.arch == "both" else (args.arch,)
    project = project_metadata()
    if args.plan:
        print(json.dumps(describe(host, targets, project, args.format), indent=2))
        return
    try:
        if args.check_dependencies:
            for arch in targets:
                with tempfile.TemporaryDirectory(prefix="fluttery-win-deps-") as scratch:
                    work = Path(scratch)
                    check_dependency_closure(arch, work)
                    package_wheels(arch, work / "target", work, dry_run=True)
                print(f"Verified {arch} dependency resolution for Python {PYTHON_ABI} ({len(WHEELS)} pins).")
            return
        if host not in COMPILER_HOSTS:
            parser.error("Build from an x64 or ARM64 Mac, Ubuntu Linux, or Ubuntu in WSL.")
        if args.output is None:
            parser.error("--output is required when building installers or portable folders.")
        output = args.output.expanduser().resolve()
        verify_destinations(output, targets, args.format)
        makensis = installer_compiler(args.makensis) if args.format != "portable" else None
        cache = args.cache.expanduser().resolve()
        compiler_name, compiler_checksum = compiler_asset(host)
        with tempfile.TemporaryDirectory(prefix="fluttery-win-build-") as scratch:
            work = Path(scratch)
            archive = fetch(cache, "mstorsjo/llvm-mingw", LLVM_RELEASE, f"{compiler_name}.tar.xz", compiler_checksum)
            extract(archive, work)
            output.mkdir(parents=True, exist_ok=True)
            for arch in targets:
                build_target(arch, output, work / arch, cache, work / compiler_name, makensis, project, args.format)
    except (OSError, ValueError, subprocess.CalledProcessError, tarfile.TarError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
