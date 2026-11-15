"""Device-local runtimes: identical preparation on developer and end-user machines.

Only the per-user application data directory (see default_directory) is managed.
System interpreters and existing project venvs are never used as a readiness
shortcut or modified. No credentials belong here.
"""
import argparse
import contextlib
import csv
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYTHON_VERSION = "3.12.15"


def default_directory():
    """Prepared environments live outside the checkout.

    Keeping them in P4/var/runtime made every new worktree, branch switch or
    moved project directory re-download the whole runtime. P4_RUNTIME_DIR
    overrides the location for deployments and tests.
    """
    override = os.environ.get("P4_RUNTIME_DIR")
    if override:
        return Path(override)
    if platform.system() == "Darwin":
        return Path.home() / "Library/Application Support/EdgeFabric/runtime"
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "EdgeFabric/runtime"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "EdgeFabric/runtime"


DIRECTORY = default_directory()


def process_environment(executable, source=None):
    env = dict(os.environ if source is None else source)
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    env["PYTHONNOUSERSITE"] = "1"
    prefix = Path(executable).parent
    if prefix.name in {"bin", "Scripts"}:
        prefix = prefix.parent
    paths = [prefix, prefix / "Library/bin", prefix / "Scripts"] if os.name == "nt" else [prefix / "bin"]
    env["PATH"] = os.pathsep.join(map(str, paths)) + os.pathsep + env.get("PATH", "")
    if platform.system() == "Linux":
        env["LD_LIBRARY_PATH"] = str(prefix / "lib") + os.pathsep + env.get("LD_LIBRARY_PATH", "")
    return env


def platform_key():
    system, machine = platform.system(), platform.machine().lower()
    key = {("Darwin", "arm64"): "osx-arm64", ("Darwin", "x86_64"): "osx-64",
           ("Windows", "amd64"): "win-64", ("Linux", "x86_64"): "linux-64",
           ("Linux", "aarch64"): "linux-aarch64"}.get((system, machine))
    if key is None:
        raise ValueError(f"此系统架构尚无可用的应用运行包：{system}/{machine}")
    return key


@contextlib.contextmanager
def preparation_lock(directory):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".prepare.lock").open("a+b") as handle:
        handle.write(b"0")
        handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError("另一启动器正在准备环境，请等待完成后重试。") from exc
        yield


def requirements(component):
    """Return the pinned dependency set for a component.

    Every component installs from a committed lock export so both platforms
    resolve to identical transitive versions instead of re-resolving ranges.
    """
    name = {
        "P1": "requirements.lock.txt",
        "CUDA": "requirements-gpu.lock.txt",
    }.get(component)
    if name:
        path = ROOT / "P1" / name
    else:
        path = ROOT / "P2/requirements.lock.txt"
    pins = []
    for line in path.read_text(encoding="utf-8").splitlines():
        # Hash-pinned exports end each requirement with a line-continuation
        # backslash and list --hash= lines below it; keep only the pins.
        text = line.strip().rstrip("\\").strip()
        if text and not text.startswith(("#", "--")):
            pins.append(text)
    return pins


class Manager:
    def __init__(self, directory=None, runner=None):
        self.directory = Path(directory or DIRECTORY)
        self.runner = runner

    def command(self, argv, output):
        if self.runner:
            self.runner(list(map(str, argv)), output)
        else:
            subprocess.run(list(map(str, argv)), check=True, stdout=output, stderr=output,
                           env=process_environment(argv[0]))

    def installer(self, progress):
        with (ROOT / "P4/runtime/installers.tsv").open(encoding="utf-8") as handle:
            item = next(row for row in csv.DictReader(handle, delimiter="\t") if row["platform"] == platform_key())
        target = self.directory / ("micromamba.exe" if os.name == "nt" else "micromamba")
        if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == item["sha256"]:
            return target
        progress("正在下载并校验应用环境管理器…")
        temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".download")
        try:
            with urllib.request.urlopen(item["url"], timeout=60) as response, temporary.open("wb") as out:
                while chunk := response.read(1024 * 1024):
                    out.write(chunk)
            if hashlib.sha256(temporary.read_bytes()).hexdigest() != item["sha256"]:
                raise ValueError("环境管理器下载校验失败，请重新启动。")
            temporary.chmod(0o700)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    def executable(self, component):
        prefix = self.directory / component
        if component == "web":
            return prefix / ("node.exe" if os.name == "nt" else "bin/node")
        return prefix / ("python.exe" if os.name == "nt" else "bin/python")

    def ready(self, component, executable):
        if component == "web":
            argv = [str(executable), "-e", "if(+process.versions.node.split('.')[0]!==22)process.exit(1)"]
        else:
            version = PYTHON_VERSION
            # Import the runtime, not just its metadata: missing DLLs must fail here.
            code = """import importlib,importlib.metadata as m,json,platform,sys
from pip._vendor.packaging.requirements import Requirement
assert platform.python_version()==sys.argv[1]
for text in json.loads(sys.argv[2]):
    req=Requirement(text)
    if req.marker is None or req.marker.evaluate(): assert m.version(req.name) in req.specifier
for name in json.loads(sys.argv[3]): importlib.import_module(name)
if sys.argv[4]:
    try: m.version(sys.argv[4])
    except m.PackageNotFoundError: pass
    else: raise RuntimeError('CPU/GPU distributions must not be mixed')
"""
            modules = ["ssl", "fastapi", "uvicorn", "httpx"]
            modules += ["rapidocr", "onnxruntime", "PIL", "psutil", "multipart"] if component != "P2" else ["jsonschema", "yaml"]
            opposite = "onnxruntime" if component == "CUDA" else "onnxruntime-gpu" if component == "P1" else ""
            argv = [str(executable), "-I", "-c", code, version, json.dumps(requirements(component)), json.dumps(modules), opposite]
        try:
            result = subprocess.run(argv, capture_output=True, timeout=45, env=process_environment(executable))
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def ensure(self, component, progress=print):
        if component not in {"P1", "P2", "CUDA", "web"}:
            raise ValueError("未知运行环境")
        if component == "CUDA" and platform.system() == "Darwin":
            raise ValueError("本机不支持 NVIDIA CUDA，请选择 CPU 计算配置。")
        with preparation_lock(self.directory):
            executable = self.executable(component)
            progress(f"检查应用专用 {component} 环境…")
            if self.ready(component, executable):
                progress(f"{component} 环境检查通过。")
                return str(executable)
            installer = self.installer(progress)
            prefix = executable.parent if os.name == "nt" else executable.parent.parent
            progress(f"正在自动准备 {component} 环境，首次下载可能需要几分钟…")
            action = "install" if (prefix / "conda-meta/history").exists() else "create"
            repair = ["--force-reinstall"] if action == "install" else []
            packages = ["nodejs=22"] if component == "web" else ["python=" + PYTHON_VERSION, "pip"]
            # Conda puts CUDA runtime DLLs in this private environment's Library/bin.
            if component == "CUDA":
                packages += ["cuda-version=12.4", "cudnn=9.10.2.21", "libcublas", "libcufft", "cuda-cudart"]
            log = self.directory / "setup.log"
            try:
                with log.open("ab", buffering=0) as output:
                    self.command([installer, "--no-rc", "-r", self.directory / "cache", action,
                                  *repair, "-y", "-p", prefix, "--override-channels", "-c", "conda-forge", *packages], output)
                    if component != "web":
                        if os.name == "nt":
                            # Python 3.8+ extension DLL lookup does not reliably use PATH.
                            # Retain the directory handle for spawned Python workers too.
                            site = prefix / "Lib/site-packages"
                            site.mkdir(parents=True, exist_ok=True)
                            (site / "_edge_private_runtime.pth").write_text(
                                "import os,sys; os._edge_dll_handles = [os.add_dll_directory(os.path.join(sys.prefix,'Library','bin'))]\n",
                                encoding="utf-8")
                        if component in {"P1", "CUDA"}:
                            opposite = "onnxruntime" if component == "CUDA" else "onnxruntime-gpu"
                            self.command([executable, "-I", "-m", "pip", "--isolated", "uninstall", "-y", opposite], output)
                        self.command([executable, "-I", "-m", "pip", "--isolated", "install", "--force-reinstall", "--disable-pip-version-check",
                                      "--cache-dir", self.directory / "pip-cache", *requirements(component)], output)
                        self.command([executable, "-I", "-m", "pip", "check"], output)
                if not self.ready(component, executable):
                    raise ValueError("安装后运行核验未通过")
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                raise ValueError(f"{component} 自动准备未完成，可重新启动重试。日志：{log}") from exc
            progress(f"{component} 环境准备完成。")
            return str(executable)


def main(argv=None):
    # Unknown arguments belong to control.py; no user data is touched by --prepare-only.
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--compute-config")
    args, rest = parser.parse_known_args(argv)
    sys.path.insert(0, str(Path(__file__).parent))
    import compute_config
    config = compute_config.load(args.compute_config)
    manager = Manager()
    try:
        for component in ["web", "P1", "P2"] + (["CUDA"] if config["node"]["backend"] == "cuda" else []):
            manager.ensure(component, lambda message: print(message, flush=True))
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.prepare_only:
        return 0
    if args.compute_config:
        rest += ["--compute-config", args.compute_config]
    # Keep the bootstrap Python (stdlib only). Child services use prepared runtimes.
    try:
        return subprocess.call([sys.executable, str(ROOT / "P4/scripts/control.py"), *rest],
                               env=process_environment(sys.executable))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
