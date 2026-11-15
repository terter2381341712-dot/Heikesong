"""Automatic preparation must run on developer Macs and clean Windows alike."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import compute_config
import control
import managed_runtime as runtime


@pytest.mark.parametrize("component,relative", [
    ("P1", "P1/requirements.txt"),
    ("CUDA", "P1/requirements-gpu.txt"),
    ("P2", "P2/pyproject.toml"),
])
def test_dependency_lists_are_utf8_even_with_windows_gbk_default(tmp_path, monkeypatch, component, relative):
    expected = runtime.requirements(component)
    source = (runtime.ROOT / relative).read_text(encoding="utf-8")
    # Reproduce Windows checkout line endings, plus non-ASCII comments for P2.
    raw = ("# OCR 执行器（版本已在 profiles/ocr.default.v1.lock.json 冻结）\n" + source).replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8")
    with pytest.raises(UnicodeDecodeError):
        raw.decode("gbk")
    target = tmp_path / relative
    target.parent.mkdir(parents=True)
    target.write_bytes(raw)
    original_open = Path.open

    def gbk_default(path, mode="r", buffering=-1, encoding=None, errors=None, newline=None):
        if "b" not in mode and encoding is None:
            encoding = "gbk"
        return original_open(path, mode, buffering, encoding, errors, newline)

    monkeypatch.setattr(Path, "open", gbk_default)
    monkeypatch.setattr(runtime, "ROOT", tmp_path)
    assert runtime.requirements(component) == expected


def test_default_never_shortcuts_to_developer_python(tmp_path, monkeypatch):
    local = control.Runtime(tmp_path)
    monkeypatch.delenv("P4_P1_PYTHON", raising=False)
    monkeypatch.setattr(local, "candidates", lambda _: pytest.fail("must not discover developer environments"))
    calls = []
    def ensure(self, component, progress):
        calls.append(component)
        return "/application/private/python"
    monkeypatch.setattr(runtime.Manager, "ensure", ensure)
    for _ in range(2):
        assert local.ensure("P1", lambda _: None) == "/application/private/python"
    assert calls == ["P1", "P1"]


@pytest.mark.parametrize("component", ["P1", "P2", "CUDA", "web"])
def test_ready_private_environment_rechecked_without_download(tmp_path, monkeypatch, component):
    monkeypatch.setattr(runtime.platform, "system", lambda: "Windows")
    manager = runtime.Manager(tmp_path)
    checks = []
    monkeypatch.setattr(manager, "ready", lambda *args: checks.append(args) or True)
    monkeypatch.setattr(manager, "installer", lambda _: pytest.fail("no download for ready environment"))
    assert manager.ensure(component) == str(manager.executable(component))
    assert manager.ensure(component) == str(manager.executable(component))
    assert len(checks) == 2


def test_missing_cpu_installs_exact_profile_and_checks_before_return(tmp_path, monkeypatch):
    commands = []
    manager = runtime.Manager(tmp_path, runner=lambda argv, out: commands.append(argv))
    monkeypatch.setattr(manager, "ready", lambda *args: len(commands) == 4)
    monkeypatch.setattr(manager, "installer", lambda _: tmp_path / "manager")
    result = manager.ensure("P1")
    assert "python=3.9.6" in commands[0]
    assert "--no-rc" in commands[0] and "--override-channels" in commands[0]
    assert commands[1][-3:] == ["uninstall", "-y", "onnxruntime-gpu"]
    assert "onnxruntime==1.19.2" in commands[2]
    assert "--force-reinstall" in commands[2]
    assert commands[3][-2:] == ["pip", "check"]
    assert result == str(manager.executable("P1"))


@pytest.mark.parametrize("component,steps", [("P1", 4), ("P2", 3), ("CUDA", 4)])
def test_all_python_environments_install_exactly_396(tmp_path, monkeypatch, component, steps):
    commands = []
    manager = runtime.Manager(tmp_path, runner=lambda argv, output: commands.append(argv))
    monkeypatch.setattr(manager, "ready", lambda *args: len(commands) == steps)
    monkeypatch.setattr(manager, "installer", lambda _: tmp_path / "manager")
    manager.ensure(component, lambda _: None)
    assert "python=3.9.6" in commands[0]
    if component == "CUDA":
        assert "cuda-version=12.4" in commands[0]
        assert "onnxruntime-gpu==1.19.2" in commands[2]


def test_bootstrap_and_cuda_profiles_agree_on_python_version():
    for script in ("bootstrap.ps1", "bootstrap.sh"):
        text = (runtime.ROOT / "P4/scripts" / script).read_text(encoding="utf-8")
        assert "python=3.9.6" in text and "(3,9,6)" in text
        assert "3.11.9" not in text
    profile = json.loads((runtime.ROOT / "P1/profiles/heterogeneous/ocr.heterogeneous.v1.lock.json").read_bytes())
    assert profile["dependencies"]["python"] == "3.9.6"
    assert all(deps["python"] == "3.9.6" for deps in profile["backend_dependencies"].values())


def test_failed_install_is_not_ready_and_can_be_retried(tmp_path, monkeypatch):
    calls = []
    def failed(argv, out):
        calls.append(argv)
        raise subprocess.CalledProcessError(1, argv)
    manager = runtime.Manager(tmp_path, runner=failed)
    monkeypatch.setattr(manager, "ready", lambda *args: False)
    monkeypatch.setattr(manager, "installer", lambda _: tmp_path / "manager")
    for _ in range(2):
        with pytest.raises(ValueError, match="自动准备未完成"):
            manager.ensure("P1")
    assert len(calls) == 2


def test_broken_managed_python_is_reinstalled_not_just_version_checked(tmp_path, monkeypatch):
    commands = []
    manager = runtime.Manager(tmp_path, runner=lambda argv, out: commands.append(argv))
    history = tmp_path / "P2/conda-meta/history"
    history.parent.mkdir(parents=True)
    history.touch()
    monkeypatch.setattr(manager, "ready", lambda *args: len(commands) == 3)
    monkeypatch.setattr(manager, "installer", lambda _: tmp_path / "manager")
    manager.ensure("P2")
    assert "install" in commands[0] and "--force-reinstall" in commands[0]
    assert "--force-reinstall" in commands[1]


def test_successful_installer_without_working_imports_is_rejected(tmp_path, monkeypatch):
    manager = runtime.Manager(tmp_path, runner=lambda *args: None)
    monkeypatch.setattr(manager, "ready", lambda *args: False)
    monkeypatch.setattr(manager, "installer", lambda _: tmp_path / "manager")
    with pytest.raises(ValueError, match="自动准备未完成"):
        manager.ensure("P2")


def test_cuda_can_select_automatic_environment(tmp_path):
    path = tmp_path / "compute.json"
    path.write_text(json.dumps({"profile_set": "heterogeneous", "node": {"backend": "cuda"}}))
    assert compute_config.load(path)["node"]["python"] is None


def test_auto_cuda_does_not_replace_resource_interpreter(tmp_path, monkeypatch):
    path = tmp_path / "compute.json"
    path.write_text(json.dumps({"profile_set": "heterogeneous", "node": {"backend": "cuda"}}))
    panel = control.Panel(tmp_path / "panel", compute_path=path)
    panel.runtime.selected["P1"] = "/private/cpu/python"
    calls = []
    monkeypatch.setattr(runtime.Manager, "ensure", lambda self, component, progress: calls.append(component) or "/private/cuda/python")
    monkeypatch.setattr(compute_config, "validate_node_runtime", lambda *args: None)
    panel.ensure_node()
    assert calls == ["CUDA"]
    assert panel.runtime.selected == {"P1": "/private/cpu/python", "node": "/private/cuda/python"}


def test_child_environment_does_not_inherit_developer_python_paths(tmp_path):
    original = {"PATH": "/existing/bin", "PYTHONPATH": "/dev/packages", "PYTHONHOME": "/dev/python", "JOIN_TOKEN": "test-only"}
    env = runtime.process_environment(tmp_path / "cpu/bin/python", original)
    assert "PYTHONPATH" not in env and "PYTHONHOME" not in env
    assert env["PYTHONNOUSERSITE"] == "1"
    assert env["JOIN_TOKEN"] == "test-only"
    assert "PYTHONHOME" in original  # Parent process untouched.


def test_parallel_install_cannot_write_same_environment(tmp_path):
    with runtime.preparation_lock(tmp_path):
        with pytest.raises(ValueError, match="另一启动器"):
            with runtime.preparation_lock(tmp_path):
                pytest.fail("lock should reject concurrent preparation")


def test_mac_does_not_claim_cuda_support(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime.platform, "system", lambda: "Darwin")
    with pytest.raises(ValueError, match="不支持 NVIDIA"):
        runtime.Manager(tmp_path).ensure("CUDA")


def test_bootstrap_prepares_every_component_even_on_developer_machine(monkeypatch):
    monkeypatch.setattr(compute_config, "load", lambda _: {"node": {"backend": "cpu"}})
    calls = []
    monkeypatch.setattr(runtime.Manager, "ensure", lambda self, component, progress: calls.append(component))
    assert runtime.main(["--prepare-only"]) == 0
    assert calls == ["web", "P1", "P2"]
