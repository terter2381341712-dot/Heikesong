"""Verified portable Windows runtimes; no Conda solver or system installation."""

import argparse
import csv
import hashlib
import os
import shutil
import subprocess
import time
import urllib.request
import uuid
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]
PYTHON_VERSION = "3.9.6"


def archive(directory, component):
    with (ROOT / "P4/runtime/portable-windows.tsv").open(encoding="utf-8") as source:
        item = next(row for row in csv.DictReader(source, delimiter="\t") if row["component"] == component)
    target = directory / "downloads" / item["url"].rsplit("/", 1)[1]
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == item["sha256"]:
        return target
    temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".download")
    try:
        with urllib.request.urlopen(item["url"], timeout=60) as response, temporary.open("wb") as output:
            shutil.copyfileobj(response, output)
        if hashlib.sha256(temporary.read_bytes()).hexdigest() != item["sha256"]:
            raise ValueError(f"{component} download checksum mismatch")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def extract(source, destination, strip=""):
    with zipfile.ZipFile(source) as zipped:
        for member in zipped.infolist():
            name = PurePosixPath(member.filename.replace("\\", "/"))
            if name.is_absolute() or ".." in name.parts or ":" in str(name):
                raise ValueError("Unsafe runtime archive path")
            if strip:
                name = name.relative_to(strip)
            target = destination.joinpath(*name.parts)
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with zipped.open(member) as incoming, target.open("wb") as outgoing:
                    shutil.copyfileobj(incoming, outgoing)


def ready(prefix, component):
    if component == "web":
        command = [str(prefix / "node.exe"), "--version"]
    else:
        command = [str(prefix / "python.exe"), "-I", "-c",
                   "import platform,ssl,pip; assert platform.python_version()=='3.9.6'; assert pip.__version__=='25.3'"]
    env = dict(os.environ)
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    env["PATH"] = str(prefix) + os.pathsep + env.get("PATH", "")
    try:
        result = subprocess.run(command, capture_output=True, timeout=20, env=env)
        return result.returncode == 0 and (component != "web" or result.stdout.strip() == b"v22.14.0")
    except (OSError, subprocess.SubprocessError):
        return False


def rename_runtime(source, destination):
    # Windows scanners may briefly hold a just-validated interpreter open.
    for attempt in range(8):
        try:
            source.rename(destination)
            return
        except PermissionError:
            if attempt == 7:
                raise
            time.sleep(min(0.1 * 2 ** attempt, 1))


def install(directory, component):
    if component not in {"bootstrap", "P1", "P2", "web"}:
        raise ValueError("Unknown portable runtime")
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    prefix = directory / component
    if ready(prefix, component):
        return prefix
    stage = directory / (component + ".prepare-" + uuid.uuid4().hex)
    stage.mkdir()
    if component == "web":
        extract(archive(directory, "web"), stage, "node-v22.14.0-win-x64")
    else:
        extract(archive(directory, "python"), stage)
        extract(archive(directory, "pip"), stage / "Lib/site-packages")
        (stage / "Library/bin").mkdir(parents=True)
        (stage / "python39._pth").write_text("python39.zip\n.\nLib/site-packages\nimport site\n", encoding="ascii")
    if not ready(stage, component):
        raise ValueError(f"Portable {component} runtime validation failed; files kept at {stage}")
    if prefix.resolve().parent != directory or stage.resolve().parent != directory:
        raise ValueError("Runtime path escaped the application directory")
    backup = directory / (component + ".backup-" + uuid.uuid4().hex)
    if prefix.exists():
        rename_runtime(prefix, backup)
    try:
        try:
            rename_runtime(stage, prefix)
        except PermissionError:
            # Some Windows filesystems/scanners deny moving a directory after
            # its executable was probed, while reading/copying remains allowed.
            # Never overwrite an existing runtime or delete the validated stage.
            if prefix.exists():
                raise
            shutil.copytree(stage, prefix)
            if not ready(prefix, component):
                raise ValueError(f"Copied runtime validation failed: {prefix}")
    except OSError:
        if backup.exists() and not prefix.exists():
            rename_runtime(backup, prefix)
        raise
    return prefix


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--component", choices=["P1", "P2", "web"], required=True)
    parser.add_argument("--python-version", choices=[PYTHON_VERSION], default=PYTHON_VERSION)
    args = parser.parse_args()
    install(args.directory, args.component)
