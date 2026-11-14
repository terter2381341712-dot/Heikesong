"""Automatic preparation must run on developer Macs and clean Windows alike."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import managed_runtime as runtime
import compute_config
import control


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
