"""Device-local runtimes: identical preparation on developer and end-user machines.

Only P4/var/runtime is managed. System interpreters and existing project venvs are
never used as a readiness shortcut or modified. No credentials belong here.
"""
import argparse
import ast
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
DIRECTORY = ROOT / "P4/var/runtime"
PYTHON_VERSION = "3.9.6"


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
    if component in {"P1", "CUDA", "DML", "COREML"}:
        name = "requirements-gpu.txt" if component == "CUDA" else "requirements.txt"
        result = [line.strip() for line in (ROOT / "P1" / name).read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.lstrip().startswith("#")]
        if component in {"DML", "COREML"}:
            package = "onnxruntime-directml==1.19.2" if component == "DML" else "onnxruntime==1.19.2"
            result = [package if line.startswith("onnxruntime==") else line for line in result]
        return result
    text = (ROOT / "P2/pyproject.toml").read_text(encoding="utf-8")
    return ast.literal_eval(re.search(r"dependencies\s*=\s*(\[[\s\S]*?\])", text).group(1))


class Manager:
    def __init__(self, directory=None, runner=None):
        self.directory = Path(directory or DIRECTORY)
        self.runner = runner
        self.last_probe_error = ""

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
assert platform.python_version()==sys.argv[1], 'Python '+platform.python_version()+' != '+sys.argv[1]
for text in json.loads(sys.argv[2]):
    req=Requirement(text)
    if req.marker is None or req.marker.evaluate(): assert m.version(req.name) in req.specifier, str(req)+' installed='+m.version(req.name)
for name in json.loads(sys.argv[3]): importlib.import_module(name)
if sys.argv[4]:
    try: m.version(sys.argv[4])
    except m.PackageNotFoundError: pass
    else: raise RuntimeError('CPU/GPU distributions must not be mixed')
"""
            modules = ["ssl", "fastapi", "uvicorn", "httpx"]
            modules += ["rapidocr", "onnxruntime", "PIL", "psutil", "multipart"] if component != "P2" else ["jsonschema", "yaml"]
            opposite = "onnxruntime" if component in {"CUDA", "DML"} else "onnxruntime-gpu" if component in {"P1", "COREML"} else ""
            argv = [str(executable), "-I", "-c", code, version, json.dumps(requirements(component)), json.dumps(modules), opposite]
        try:
            result = subprocess.run(argv, capture_output=True, timeout=45, env=process_environment(executable))
            self.last_probe_error = "" if result.returncode == 0 else (result.stderr.decode("utf-8", errors="replace")[-3000:] or f"runtime exited with code {result.returncode}")
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError) as exc:
            self.last_probe_error = str(exc)
            return False

    def ensure(self, component, progress=print):
        if component not in {"P1", "P2", "CUDA", "DML", "COREML", "web"}:
            raise ValueError("未知运行环境")
        if component == "CUDA" and platform.system() == "Darwin":
            raise ValueError("本机不支持 NVIDIA CUDA，请选择 CPU 计算配置。")
        if component == "DML" and platform.system() != "Windows":
            raise ValueError("DirectML 需要 Windows。")
        if component == "COREML" and platform.system() != "Darwin":
            raise ValueError("CoreML 需要 macOS。")
        with preparation_lock(self.directory):
            executable = self.executable(component)
            progress(f"检查应用专用 {component} 环境…")
            if self.ready(component, executable):
                progress(f"{component} 环境检查通过。")
                return str(executable)
            portable = os.name == "nt" and component != "CUDA"
            installer = None if portable else self.installer(progress)
            prefix = executable.parent if os.name == "nt" else executable.parent.parent
            progress(f"正在自动准备 {component} 环境，首次下载可能需要几分钟…")
            action = "install" if (prefix / "conda-meta/history").exists() else "create"
            repair = ["--force-reinstall"] if action == "install" else []
            packages = ["nodejs=22"] if component == "web" else ["python=" + PYTHON_VERSION, "pip"]
            # Conda puts CUDA runtime DLLs in this private environment's Library/bin.
            if component == "CUDA":
                packages += ["cuda-version=12.4", "cudnn=9.10.2.21", "libcublas", "libcufft", "cuda-cudart"]
            log = self.directory / "setup.log"
            attempt_start = log.stat().st_size if log.exists() else 0
            try:
                with log.open("ab", buffering=0) as output:
                    output.write((f"\n=== Preparing {component}; Python {PYTHON_VERSION} ===\n").encode("utf-8"))
                    if portable:
                        self.command([sys.executable, "-I", ROOT / "P4/scripts/windows_runtime.py",
                                      "--directory", self.directory, "--component", component,
                                      "--python-version", PYTHON_VERSION], output)
                    else:
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
                        if component in {"P1", "CUDA", "DML", "COREML"}:
                            opposite = "onnxruntime" if component in {"CUDA", "DML"} else "onnxruntime-gpu"
                            self.command([executable, "-I", "-m", "pip", "--isolated", "uninstall", "-y", opposite], output)
                        if portable:
                            # Embedded Python ignores pip's temporary PYTHONPATH build
                            # environment. Seed build tools in this private prefix instead.
                            self.command([executable, "-I", "-m", "pip", "--isolated", "install",
                                          "--retries", "3", "--timeout", "60",
                                          "--only-binary=:all:", "--cache-dir", self.directory / "pip-cache",
                                          "setuptools==80.9.0", "wheel==0.45.1"], output)
                        self.command([executable, "-I", "-m", "pip", "--isolated", "install", "--force-reinstall", "--disable-pip-version-check",
                                      "--retries", "3", "--timeout", "60",
                                      *(["--no-build-isolation"] if portable else []),
                                      "--cache-dir", self.directory / "pip-cache", *requirements(component)], output)
                        self.command([executable, "-I", "-m", "pip", "check"], output)
                if not self.ready(component, executable):
                    with log.open("ab") as output:
                        output.write(("\nRuntime import check failed:\n" + self.last_probe_error + "\n").encode("utf-8"))
                    raise ValueError("安装后运行核验未通过：" + (self.last_probe_error or "模块导入或依赖核验失败").splitlines()[-1])
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                if log.exists():
                    with log.open("rb") as source:
                        source.seek(max(attempt_start, log.stat().st_size - 6000))
                        lines = source.read().decode("utf-8", errors="replace").splitlines()
                else:
                    lines = []
                relevant = [line.strip() for line in lines if re.search(r"ERROR:|Error:|Cannot import|exited with code", line)]
                detail = relevant[-1] if relevant else str(exc)
                detail = re.sub(r"(https?://)[^/\s@]+@", r"\1***@", detail)[:350]
                raise ValueError(f"{component} 自动准备未完成。原因：{detail}。日志：{log}") from exc
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
