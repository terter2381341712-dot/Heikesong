import hashlib
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import windows_runtime as portable  # noqa: E402


@pytest.mark.parametrize("entry", ["../escape.txt", "/absolute.txt", "C:/escape.txt", "..\\escape.txt"])
def test_archive_rejects_escaping_paths(tmp_path, entry):
    source = tmp_path / "archive.zip"
    with zipfile.ZipFile(source, "w") as zipped:
        zipped.writestr(entry, "unsafe")
    with pytest.raises(ValueError, match="Unsafe"):
        portable.extract(source, tmp_path / "destination")


def test_download_checks_hash_and_rejects_changed_bytes(tmp_path, monkeypatch):
    source = tmp_path / "source.zip"
    source.write_bytes(b"official archive fixture")
    manifest = tmp_path / "repo/P4/runtime/portable-windows.tsv"
    manifest.parent.mkdir(parents=True)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest.write_text(f"component\turl\tsha256\npython\t{source.as_uri()}\t{digest}\n", encoding="utf-8")
    monkeypatch.setattr(portable, "ROOT", tmp_path / "repo")
    cache = tmp_path / "runtime"
    assert portable.archive(cache, "python").read_bytes() == b"official archive fixture"
    # A corrupted cache and changed upstream are both rejected, never executed.
    (cache / "downloads/source.zip").write_bytes(b"bad cache")
    source.write_bytes(b"changed source")
    with pytest.raises(ValueError, match="checksum"):
        portable.archive(cache, "python")


@pytest.mark.parametrize("blocked_rename", [False, True])
def test_portable_repair_preserves_previous_runtime_and_data(tmp_path, monkeypatch, blocked_rename):
    directory = tmp_path / "中文 project/runtime"
    old = directory / "P1"
    old.mkdir(parents=True)
    (old / "old-version.txt").write_text("previous runtime")
    business = directory.parent / "node.json"
    business.write_text("business data")
    archives = {}
    for component, files in {
        "python": {"python.exe": "fixture", "python39._pth": "original"},
        "pip": {"pip/__init__.py": "fixture"},
    }.items():
        source = tmp_path / (component + ".zip")
        with zipfile.ZipFile(source, "w") as zipped:
            for name, content in files.items():
                zipped.writestr(name, content)
        archives[component] = source
    monkeypatch.setattr(portable, "archive", lambda root, component: archives[component])
    monkeypatch.setattr(portable, "ready", lambda prefix, component: (prefix / "Lib/site-packages/pip/__init__.py").exists())
    if blocked_rename:
        def rename(source, destination):
            if ".prepare-" in source.name:
                raise PermissionError("validated executable is temporarily held open")
            source.rename(destination)
        monkeypatch.setattr(portable, "rename_runtime", rename)
    result = portable.install(directory, "P1")
    assert result == old.resolve()
    assert (result / "python39._pth").read_text().splitlines() == ["python39.zip", ".", "Lib/site-packages", "import site"]
    assert len(list(directory.glob("P1.backup-*/old-version.txt"))) == 1
    assert business.read_text() == "business data"
    monkeypatch.setattr(portable, "archive", lambda *args: pytest.fail("ready runtime must reuse files"))
    assert portable.install(directory, "P1") == result
