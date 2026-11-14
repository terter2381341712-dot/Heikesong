"""Interpreter symlinks identify venvs, even when their targets are the same Python."""

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import cluster  # noqa: E402
import control  # noqa: E402


@pytest.fixture
def linked_python(tmp_path, monkeypatch):
    entry = tmp_path / "P1/.venv/bin/python"
    base = tmp_path / "system/bin/python"
    for path in (entry, base):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("test interpreter placeholder", encoding="utf-8")
    aliases = {entry}
    original = Path.resolve

    def follow_link(path, *args, **kwargs):
        return base if path in aliases else original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", follow_link)
    return SimpleNamespace(entry=entry, base=base, aliases=aliases, root=tmp_path)


def test_selected_ready_venv_does_not_turn_into_system_python(linked_python, monkeypatch):
    runtime = control.Runtime(linked_python.root / "runtime")
    monkeypatch.setattr(runtime, "candidates", lambda component: [linked_python.entry])
    monkeypatch.setattr(runtime, "probe", lambda *args: {"ready": True, "compatible": True})
    assert linked_python.entry.resolve() == linked_python.base  # Reproduces the original trap.
    assert runtime.ensure("P1", lambda msg: None) == str(linked_python.entry)


def test_p2_installs_and_checks_in_the_selected_venv(linked_python, monkeypatch):
    runtime = control.Runtime(linked_python.root / "runtime")
    monkeypatch.setattr(runtime, "candidates", lambda component: [linked_python.entry])
    installed = []

    def probe(executable, component):
        return {
            "compatible": True,
            "ready": executable == linked_python.entry and (component == "P1" or bool(installed)),
        }

    monkeypatch.setattr(runtime, "probe", probe)
    monkeypatch.setattr(
        control.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0)
    )
    monkeypatch.setattr(runtime, "setup_command", lambda command, output: installed.append(command))
    runtime.ensure("P1", lambda msg: None)
    runtime.ensure("P2", lambda msg: None)
    assert installed[0][:3] == [str(linked_python.entry), "-m", "pip"]
    assert runtime.selected == {"P1": str(linked_python.entry), "P2": str(linked_python.entry)}


def test_newly_prepared_venv_path_is_preserved(linked_python, monkeypatch):
    runtime = control.Runtime(linked_python.root / "runtime")
    suffix = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    expected = runtime.directory / "P1" / suffix
    linked_python.aliases.add(expected)
    commands = []
    monkeypatch.setattr(runtime, "candidates", lambda component: [linked_python.entry])
    monkeypatch.setattr(
        runtime,
        "probe",
        lambda path, component: {
            "compatible": True,
            "ready": path == expected and len(commands) == 2,
        },
    )
    monkeypatch.setattr(runtime, "setup_command", lambda command, output: commands.append(command))
    assert runtime.ensure("P1", lambda msg: None) == str(expected)
    assert commands[1][0] == str(expected)


def test_machine_config_and_plan_keep_the_venv_entry(linked_python, monkeypatch, capsys):
    monkeypatch.setattr(cluster, "ROOT", linked_python.root)
    config = linked_python.root / "node.json"
    config.write_text(
        json.dumps({"resource_url": "http://center:8001", "tokens": {"JOIN_TOKEN": "join"}}),
        encoding="utf-8",
    )
    machine = linked_python.root / "machine.json"
    machine.write_text(
        json.dumps(
            {
                "roles": ["node"],
                "config": "node.json",
                "data_dir": "data",
                "p1_python": "P1/.venv/bin/python",
                "node_name": "Mac-01",
            }
        ),
        encoding="utf-8",
    )
    args = SimpleNamespace(
        machine=str(machine),
        role=None,
        roles=None,
        config=None,
        data_dir=None,
        p1_python=None,
        p2_python=None,
        with_node=False,
        plan=True,
    )
    cluster.configure_run(args)
    assert args.p1_python == str(linked_python.entry)
    cluster.run(args)
    assert json.loads(capsys.readouterr().out)["roles"][0]["python"] == str(linked_python.entry)


def test_cluster_launch_does_not_resolve_override(linked_python, monkeypatch):
    config = linked_python.root / "task.json"
    config.write_text(
        json.dumps(
            {
                "resource_url": "http://center:8001",
                "tokens": {"DEMO_TOKEN": "demo", "TASK_TOKEN": "task"},
            }
        ),
        encoding="utf-8",
    )
    captured = []

    def popen(argv, **kwargs):
        captured.append(argv)
        raise RuntimeError("captured without starting services")

    monkeypatch.setattr(cluster.subprocess, "Popen", popen)
    args = SimpleNamespace(
        config=str(config),
        data_dir=str(linked_python.root / "run"),
        role="task",
        roles=None,
        with_node=False,
        node_name="Mac-01",
        p1_python=None,
        p2_python=str(linked_python.entry),
        plan=False,
        bind="127.0.0.1",
        resource_port=0,
        task_port=0,
        interactive=False,
    )
    with pytest.raises(RuntimeError, match="captured"):
        cluster.run(args)
    assert captured[0][0] == str(linked_python.entry)


@pytest.mark.skipif(os.name == "nt", reason="Actual POSIX venv symlinks require a POSIX host")
def test_real_posix_venv_prefix_and_import_are_preserved(tmp_path, monkeypatch):
    import venv

    env = tmp_path / "venv"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(env)
    executable = env / "bin/python"
    assert executable.is_symlink()
    selected = cluster.absolute_interpreter(executable)
    site = Path(
        subprocess.check_output(
            [str(selected), "-c", "import site;print(site.getsitepackages()[0])"], text=True
        ).strip()
    )
    (site / "p4_venv_marker.py").write_text("VALUE = 'only-in-this-venv'\n", encoding="utf-8")
    module = tmp_path / "P2/probe_venv.py"
    module.parent.mkdir()
    module.write_text(
        "import sys,json,p4_venv_marker;print(json.dumps({'prefix':sys.prefix,'value':p4_venv_marker.VALUE}))",
        encoding="utf-8",
    )
    monkeypatch.setattr(cluster, "ROOT", tmp_path)
    body = json.loads(
        subprocess.check_output(cluster.module_command(selected, "P2", "probe_venv"), text=True)
    )
    assert Path(body["prefix"]).resolve() == env.resolve()
    assert body["value"] == "only-in-this-venv"
