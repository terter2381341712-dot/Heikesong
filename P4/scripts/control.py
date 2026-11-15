"""Local P4 control panel: system detection, explicit sharing, LAN discovery and pairing.

Only the pairing endpoint is exposed to LAN clients. Product services keep their APIs.
"""

import argparse
import base64
import binascii
import hashlib
import ipaddress
import json
import os
import platform
import re
import secrets
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cluster  # noqa: E402
import compute_config
import managed_runtime

ROOT = cluster.ROOT
DISCOVERY_PORT = 37043
PROTOCOL = "P4-PAIR-2"
CONTROL_ASSETS = {
    "/assets/anthropic-sans-normal.woff2": "font/woff2",
    "/assets/anthropic-mono-normal.woff2": "font/woff2",
    "/assets/noto-sans-sc-home.woff2": "font/woff2",
    "/styles.css": "text/css; charset=utf-8",
}


def encode_connection_code(address, port, code):
    payload = struct.pack("!4sHI", ipaddress.IPv4Address(address).packed, port, int(code))
    raw = payload + hashlib.sha256(payload).digest()[:4]
    return "P4-1-" + base64.b32encode(raw).decode("ascii").rstrip("=")


def decode_connection_code(value):
    try:
        value = value.strip().upper()
        if not value.startswith("P4-1-") or len(value) != 28:
            raise ValueError()
        raw = base64.b32decode(value[5:] + "=")
        payload, checksum = raw[:10], raw[10:]
        if not secrets.compare_digest(checksum, hashlib.sha256(payload).digest()[:4]):
            raise ValueError()
        packed, port, code = struct.unpack("!4sHI", payload)
        address = ipaddress.IPv4Address(packed)
        if not port or code > 999999 or address.is_unspecified or address.is_multicast:
            raise ValueError()
        return f"{address}:{port}", f"{code:06d}"
    except (ValueError, TypeError, AttributeError, binascii.Error, struct.error):
        raise ValueError("连接码无效或不完整，请从中心重新复制完整连接码。") from None


class ControlHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("w", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False, indent=2)
    if os.name != "nt":
        temporary.chmod(0o600)
    temporary.replace(path)


def fingerprint(compute=None):
    digest = hashlib.sha256()
    for folder in ("node_agent", "resource_service", "runners", "profiles"):
        for path in sorted((ROOT / "P1" / folder).rglob("*"), key=lambda p: p.as_posix()):
            if path.suffix in {".py", ".json"}:
                digest.update(path.relative_to(ROOT / "P1").as_posix().encode())
                # Runtime profile copies use the same LF bytes on every system.
                raw = path.read_bytes()
                digest.update(raw.replace(b"\r\n", b"\n"))
    if compute and compute["profile_set"] != "default":
        digest.update(("\0profile_set=" + compute["profile_set"]).encode())
    return digest.hexdigest()


def materialize_profiles(source, destination):
    destination.mkdir(parents=True, exist_ok=True)
    manifest = {"transform": "CRLF_TO_LF_ONLY", "files": {}}
    for path in sorted(source.glob("*.json"), key=lambda p: p.name):
        raw = path.read_bytes()
        canonical = raw.replace(b"\r\n", b"\n")
        if json.loads(raw) != json.loads(canonical):
            raise ValueError("运行profile归一化改变了JSON内容")
        target = destination / path.name
        if not target.exists() or target.read_bytes() != canonical:
            temporary = target.with_name(target.name + "." + uuid.uuid4().hex + ".tmp")
            temporary.write_bytes(canonical)
            temporary.replace(target)
        manifest["files"][path.name] = {
            "source_sha256": hashlib.sha256(raw).hexdigest(),
            "runtime_sha256": hashlib.sha256(canonical).hexdigest(),
        }
    save(destination / "manifest.json", manifest)
    return manifest


def lan_addresses():
    candidates = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
            route.connect(("8.8.8.8", 80))  # Route lookup only, no packet is sent.
            candidates.append(route.getsockname()[0])
    except OSError:
        pass
    try:
        candidates.extend(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    command = (
        ["ipconfig"]
        if os.name == "nt"
        else (["ifconfig"] if sys.platform == "darwin" else ["hostname", "-I"])
    )
    try:
        text = subprocess.check_output(command, timeout=5, stderr=subprocess.DEVNULL).decode(
            errors="replace"
        )
        pattern = (
            r"IPv4[^\n:]*:\s*(\d+\.\d+\.\d+\.\d+)"
            if os.name == "nt"
            else r"\b(?:\d{1,3}\.){3}\d{1,3}\b"
        )
        candidates.extend(re.findall(pattern, text))
    except (OSError, subprocess.SubprocessError):
        pass
    networks = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
    result = []
    for value in candidates:
        try:
            if any(ipaddress.ip_address(value) in n for n in networks) and value not in result:
                result.append(value)
        except ValueError:
            pass
    return result


def request_json(url, payload=None, token=None, timeout=4):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        return json.load(response)


class Runtime:
    def __init__(self, directory):
        self.directory = directory
        self.log = directory / "setup.log"
        self.selected = {}
        self.cancelled = threading.Event()
        self.active = None

    def setup_command(self, command, output):
        self.active = subprocess.Popen(
            command,
            env=managed_runtime.process_environment(command[0]),
            stdout=output,
            stderr=output,
            start_new_session=os.name != "nt",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        try:
            while self.active.poll() is None:
                if self.cancelled.wait(0.15):
                    if os.name == "nt":
                        subprocess.run(
                            ["taskkill", "/PID", str(self.active.pid), "/T", "/F"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                    else:
                        os.killpg(self.active.pid, signal.SIGKILL)
                    self.active.wait(timeout=8)
                    raise ValueError("控制台已关闭，环境准备已中断。")
            if self.active.returncode:
                raise subprocess.CalledProcessError(self.active.returncode, command)
        finally:
            self.active = None

    def candidates(self, component):
        windows = os.name == "nt"
        suffix = "Scripts/python.exe" if windows else "bin/python"
        values = [
            ROOT / component / ".venv" / suffix,
            self.directory / component / suffix,
            ROOT / "P4/.venv" / suffix,
            Path(sys.executable),
        ]
        if component == "P1":
            values += [ROOT / "P4/var/mac-p1" / suffix]
        for name in (
            "python3.12",
            "python3.13",
            "python3.14",
            "python3",
            "python",
        ):
            found = shutil.which(name)
            if found:
                values.append(Path(found))
        values.extend(
            Path(p)
            for p in ("/usr/bin/python3", "/opt/homebrew/bin/python3", "/usr/local/bin/python3")
        )
        values.extend(Path.home().glob(".pyenv/versions/*/bin/python*"))
        values.extend(Path("/Library/Frameworks/Python.framework/Versions").glob("*/bin/python3"))
        if windows and shutil.which("py"):
            try:
                listing = subprocess.check_output(
                    ["py", "-0p"], timeout=5, stderr=subprocess.DEVNULL
                ).decode(errors="replace")
                values.extend(
                    Path(p.strip()) for p in re.findall(r"([A-Za-z]:\\[^\r\n]*python.exe)", listing)
                )
            except subprocess.SubprocessError:
                pass
        return list(dict.fromkeys(cluster.absolute_interpreter(p) for p in values if p.is_file()))

    def probe(self, executable, component):
        modules = (
            ["fastapi", "uvicorn", "httpx", "psutil", "multipart"]
            if component == "P1"
            else ["fastapi", "uvicorn", "httpx", "jsonschema", "yaml"]
        )
        pinned = (
            {"rapidocr": "3.10.0", "onnxruntime": "1.19.2", "pillow": "11.3.0"}
            if component == "P1"
            else {}
        )
        code = (
            "import sys,json,platform,importlib.util; from importlib.metadata import version; "
            "print(json.dumps({'version':platform.python_version(),'ready':"
            f"all(importlib.util.find_spec(x) is not None for x in {modules!r}) and "
            f"all(version(k)==v for k,v in {pinned!r}.items())" + "}))"
        )
        try:
            process = subprocess.run([str(executable), "-c", code], capture_output=True, timeout=12)
            if process.returncode:
                # An absent distribution is not an invalid interpreter.
                version = (
                    subprocess.check_output(
                        [str(executable), "-c", "import platform;print(platform.python_version())"],
                        timeout=5,
                    )
                    .decode()
                    .strip()
                )
                result = {"version": version, "ready": False}
            else:
                result = json.loads(process.stdout)
            version = tuple(int(p) for p in result["version"].split("."))
            result["compatible"] = version == tuple(
                int(p) for p in managed_runtime.PYTHON_VERSION.split(".")
            )
            return result
        except (OSError, ValueError, subprocess.SubprocessError):
            return {"compatible": False, "ready": False}

    def ensure(self, component, progress):
        override = os.environ.get(f"P4_{component}_PYTHON")
        if not override:
            executable = managed_runtime.Manager(runner=self.setup_command).ensure(component, progress)
            self.selected[component] = executable
            return executable
        compatible = []
        progress(f"检测 {component} 的本机解释器和依赖…")
        candidates = [Path(override)] if override else self.candidates(component)
        for executable in candidates:
            executable = cluster.absolute_interpreter(executable)
            result = self.probe(executable, component)
            if result["compatible"]:
                if result["ready"]:
                    self.selected[component] = str(executable)
                    return self.selected[component]
                compatible.append(executable)
        if not compatible:
            required = "Python 3.12.15（完整版本号）"
            hint = (
                f"请检查 P4_{component}_PYTHON 的设置。"
                if override
                else "请先安装该解释器，再重试。"
            )
            raise ValueError(f"{component} 需要 {required}。{hint}")
        target = self.directory / component
        suffix = "Scripts/python.exe" if os.name == "nt" else "bin/python"
        executable = cluster.absolute_interpreter(target / suffix)
        self.directory.mkdir(parents=True, exist_ok=True)
        progress(f"首次准备 {component} 环境，正在安装依赖，可能需要几分钟…")
        with self.log.open("ab") as output:
            if executable not in compatible:
                self.setup_command([str(compatible[0]), "-m", "venv", str(target)], output)
            command = [
                str(executable),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--cache-dir",
                str(self.directory / "pip-cache"),
            ]
            # Install the committed lock export so a repaired environment matches
            # the versions the managed runtime would have produced.
            lock = (ROOT / "P1/requirements.lock.txt" if component == "P1"
                    else ROOT / "P2/requirements.lock.txt")
            command += ["-r", str(lock)]
            self.setup_command(command, output)
        result = self.probe(executable, component)
        if not (result["compatible"] and result["ready"]):
            raise ValueError(f"{component} 依赖核验未通过，查看 {self.log}")
        self.selected[component] = str(executable)
        return self.selected[component]


class Panel:
    def __init__(self, directory, port=8003, web_port=5173, compute_path=None):
        self.compute = compute_config.load(compute_path)
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        identity_file = self.directory / "identity.json"
        marker = hashlib.sha256(
            (platform.system() + socket.gethostname() + str(uuid.getnode())).encode()
        ).hexdigest()
        if not identity_file.exists() or read_json(identity_file).get("machine_marker") != marker:
            save(identity_file, {"id": uuid.uuid4().hex, "machine_marker": marker})
        self.identity = read_json(identity_file)["id"]
        hostname = re.sub(r"[^A-Za-z0-9_-]", "-", socket.gethostname()).strip("-_")[:50]
        self.name = f"{hostname or 'node'}-{self.identity[:6]}"
        self.port, self.session = port, secrets.token_urlsafe(32)
        self.web_port = web_port
        self.node_executable = None
        self.runtime = Runtime(self.directory / "env")
        self.addresses = [*lan_addresses(), "127.0.0.1"]
        self.address = self.addresses[0]
        self.lock = threading.RLock()
        self.processes, self.logs = {}, {}
        self.mode, self.message, self.busy = "idle", "选择本机角色开始。", False
        self.code, self.code_until, self.bad_codes = "", 0, 0
        self.config = None
        self.sharing_requested = False
        self.closed = threading.Event()
        self.worker = None
        self.last_result = None
        self.test_run = None
        self.last_result_title = ""
        self.profile_dir = self.directory / "profiles" / self.compute["profile_set"]
        self.operation_started = None
        self.events = []
        self.discovery_error = ""

    def progress(self, message):
        with self.lock:
            self.message = message
            self.events.append(f"{time.strftime('%H:%M:%S')} [{self.name}] {message}")
            self.events = self.events[-200:]

    def spawn(self, role):
        if self.closed.is_set():
            raise ValueError("控制台已关闭。")
        if role == "web":
            env = dict(os.environ)
            env.update(
                WEB_HOST="0.0.0.0",
                WEB_PORT=str(self.web_port),
                P4_CONTROL_PORT=str(self.port),
                P4_MANAGED_WORKBENCH="1",
                DEMO_TOKEN="",  # Prevent fallback authentication, including from web/.env.
            )
            for key in ("TASK_TOKEN", "JOIN_TOKEN"):
                env.pop(key, None)
            self.launch(role, [self.node_executable, "server.js"], ROOT / "web",
                        managed_runtime.process_environment(self.node_executable, env))
            return
        component = "P2" if role == "task" else "P1"
        executable = self.runtime.selected.get("node", self.runtime.selected.get(component)) if role == "node" else self.runtime.selected[component]
        module = {"resource": "resource_service", "task": "task_service", "node": "node_agent"}[
            role
        ]
        argv = cluster.module_command(executable, component, module)
        if role == "node":
            (self.directory / "node-status.json").unlink(missing_ok=True)
            save(
                self.directory / "node-control.json",
                {"enabled": False, "revision": uuid.uuid4().hex},
            )
            argv = [
                executable,
                str(ROOT / "P4/scripts/managed_node.py"),
                "--server",
                self.config["resource_url"],
                f"--name={self.name}",
                "--control-file",
                str(self.directory / "node-control.json"),
            ]
            argv += compute_config.node_arguments(self.compute)
        env = cluster.environment(
            self.config, role, self.directory / "data", self.address, 8001, 8002, self.name
        )
        env["PYTHONUNBUFFERED"] = "1"
        env.update(compute_config.environment(self.compute, self.profile_dir))
        if os.name == "nt" and role in {"resource", "task"}:
            env["UVICORN_LOOP"] = "asyncio:SelectorEventLoop"
        if role in {"resource", "node"}:
            materialize_profiles(compute_config.profiles(self.compute), self.profile_dir)
            env["RESOURCE_PROFILES_DIR"] = str(self.profile_dir)
            env["NODE_PROFILES_DIR"] = str(self.profile_dir)
        self.launch(role, argv, ROOT / component, managed_runtime.process_environment(executable, env))

    def launch(self, role, argv, cwd, env):
        self.logs[role] = (self.directory / f"{role}.log").open("ab")
        self.processes[role] = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=self.logs[role],
            stderr=subprocess.STDOUT,
            start_new_session=os.name != "nt",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )

    def wait_health(self, role):
        url = (
            f"http://127.0.0.1:{self.web_port}/config"
            if role == "web"
            else self.config[f"{role}_url"] + f"/api/{role}/v1/healthz"
        )
        deadline = time.monotonic() + 18
        while time.monotonic() < deadline:
            if self.processes[role].poll() is not None:
                raise ValueError(f"{role} 启动失败，查看本页日志。")
            try:
                data = request_json(url, timeout=1)
                ready = (
                    data.get("managed") is True
                    if role == "web"
                    else data.get("contract_version") == compute_config.CONTRACT_VERSION
                )
                if ready:
                    return
            except (OSError, ValueError, KeyError):
                pass
            time.sleep(0.3)
        raise ValueError(f"{role} 未就绪，请检查网卡、端口占用和本页日志。")

    def new_code(self):
        with self.lock:
            if self.mode != "center" or self.address == "127.0.0.1":
                raise ValueError("请使用局域网地址启动中心后再配对。")
            candidate = f"{secrets.randbelow(1000000):06d}"
            self.code = (
                candidate if candidate != self.code else f"{(int(candidate) + 1) % 1000000:06d}"
            )
            self.code_until, self.bad_codes = time.monotonic() + 600, 0

    def start_center(self, local_node=True, address=None):
        if self.mode != "idle":
            raise ValueError("请先停止当前角色，再切换。")
        selected_address = address or self.address
        if selected_address not in self.addresses:
            raise ValueError("请选择列表中的本机地址。")
        path = self.directory / "center.json"
        config = (
            read_json(path)
            if path.exists()
            else {
                "tokens": {
                    key: secrets.token_urlsafe(32)
                    for key in ("DEMO_TOKEN", "TASK_TOKEN", "JOIN_TOKEN")
                }
            }
        )
        config.update(
            resource_url=f"http://{selected_address}:8001",
            task_url=f"http://{selected_address}:8002",
        )
        for port in (8001, 8002):
            with socket.socket() as probe:
                if os.name == "nt":
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                try:
                    probe.bind((selected_address, port))
                except OSError as exc:
                    raise ValueError(
                        f"中心启动失败：端口 {port} 无法使用（可能已被旧服务占用）。"
                        "请先在旧控制台点击“停止本机服务”，或在旧启动终端按 Ctrl+C，"
                        "再重试。直接结束控制台进程可能留下服务；"
                        "程序不会自动结束无法确认归属的进程。"
                    ) from exc
        self.ensure_workbench()
        self.runtime.ensure("P1", self.progress)
        self.runtime.ensure("P2", self.progress)
        if local_node:
            self.ensure_node()
        save(path, config)
        self.config, self.address = config, selected_address
        try:
            self.spawn("resource")
            self.wait_health("resource")
            self.spawn("task")
            self.wait_health("task")
            if local_node:
                self.spawn("node")
                self.sharing(True)
            self.mode = "center"
            if selected_address != "127.0.0.1":
                self.new_code()
            self.progress(
                "中心和工作台已就绪。另一台电脑可粘贴连接码加入。"
                if selected_address != "127.0.0.1"
                else "中心和工作台已就绪，当前地址仅供本机使用。"
            )
        except Exception:
            self.stop()
            raise

    def ensure_workbench(self):
        process = self.processes.get("web")
        if process and process.poll() is None:
            return
        if process:
            self.processes.pop("web")
        log = self.logs.pop("web", None)
        if log:
            log.close()
        self.node_executable = managed_runtime.Manager(runner=self.runtime.setup_command).ensure("web", self.progress)
        try:
            self.spawn("web")
            self.wait_health("web")
        except (OSError, ValueError):
            if "web" in self.processes:
                self.stop(close_workbench=True)
            elif log := self.logs.pop("web", None):
                log.close()
            raise

    def pair(self, body):
        with self.lock:
            if self.mode != "center" or time.monotonic() > self.code_until or self.bad_codes >= 5:
                raise ValueError("配对未开启或已失效，请在中心刷新配对码。")
            if not secrets.compare_digest(str(body.get("code", "")), self.code):
                self.bad_codes += 1
                raise ValueError("配对码不正确。")
            if body.get("protocol") != PROTOCOL or body.get("fingerprint") != fingerprint(self.compute):
                raise ValueError("两端P1代码或模型锁文件不同，请同步最新源码后重试。")
            return {
                "protocol": PROTOCOL,
                "resource_url": self.config["resource_url"],
                "task_url": self.config["task_url"],
                "tokens": {"JOIN_TOKEN": self.config["tokens"]["JOIN_TOKEN"]},
            }

    def join(self, address, code):
        if self.mode != "idle":
            raise ValueError("请先停止当前角色，再切换。")
        parsed = urlsplit(address if "://" in address else "http://" + address)
        if (
            parsed.scheme != "http"
            or not parsed.hostname
            or parsed.path not in {"", "/"}
            or parsed.username
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("中心地址应为局域网IP，或 http://IP:控制台端口。")
        host = parsed.hostname
        port = parsed.port or 8003
        if host in {self.address, "127.0.0.1", "localhost"} and port == self.port:
            raise ValueError("请选择另一台电脑的中心控制台。")
        config = request_json(
            f"http://{host}:{port}/api/pair",
            {"protocol": PROTOCOL, "code": code, "fingerprint": fingerprint(self.compute)},
            timeout=10,
        )
        if config.get("protocol") != PROTOCOL or set(config.get("tokens", {})) != {"JOIN_TOKEN"}:
            raise ValueError("配对响应不符合协议。")
        cluster.base_url(config["resource_url"])
        if config.get("task_url"):
            cluster.base_url(config["task_url"])
        self.ensure_node()
        config["local_fingerprint"] = fingerprint(self.compute)
        config["paired_device_id"] = self.identity
        self.config = config
        save(self.directory / "node.json", config)
        save(self.directory / "last-center.json", {"address": address})
        self.start_node()
        self.progress("已发送共享开启请求。注册、准备与计算状态请查看节点日志和中心资源列表。")

    def join_connection(self, connection_code):
        address, code = decode_connection_code(connection_code)
        self.join(address, code)

    def start_node(self):
        try:
            self.spawn("node")
            self.sharing(True)
            self.mode = "node"
        except Exception:
            self.stop()
            raise

    def ensure_node(self):
        """The CUDA node must not replace the resource service's CPU environment."""
        node = self.compute["node"]
        if node["python"]:
            executable = compute_config.node_interpreter(self.compute, node["python"])
            compute_config.validate_node_runtime(self.compute, executable)
            self.runtime.selected["node"] = str(executable)
        elif node["backend"] == "cuda":
            executable = managed_runtime.Manager(runner=self.runtime.setup_command).ensure("CUDA", self.progress)
            compute_config.validate_node_runtime(self.compute, executable)
            self.runtime.selected["node"] = executable
        else:
            executable = self.runtime.ensure("P1", self.progress)
            if executable:
                self.runtime.selected["node"] = executable

    def reconnect(self):
        if self.mode != "idle":
            raise ValueError("请先停止当前角色。")
        path = self.directory / "node.json"
        if not path.exists():
            raise ValueError("本机尚未配对，请输入中心配对码。")
        config = read_json(path)
        if config.get("paired_device_id") != self.identity:
            raise ValueError("这是另一台设备的配对记录，请在本机重新输入中心配对码。")
        if config.get("local_fingerprint") != fingerprint(self.compute):
            raise ValueError("本机源码或模型锁已更新，请重新配对核对版本。")
        self.ensure_node()
        health = request_json(config["resource_url"] + "/api/resource/v1/healthz")
        if health.get("contract_version") != compute_config.CONTRACT_VERSION:
            raise ValueError("中心契约版本不一致，请同步升级中心与节点")
        self.config = config
        self.start_node()
        self.progress("已使用本机保存的配对信息重连，并请求开启共享。")

    def set_computing(self, enabled):
        process = self.processes.get("node")
        if (
            self.mode in {"center", "node"}
            and enabled
            and (not process or process.poll() is not None)
        ):
            self.progress("正在启动这台电脑的计算节点…")
            self.ensure_node()
            previous_log = self.logs.pop("node", None)
            if previous_log:
                previous_log.close()
            self.spawn("node")
        self.sharing(enabled)

    def sharing(self, enabled):
        process = self.processes.get("node")
        if not process or process.poll() is not None:
            raise ValueError("本机计算节点尚未启动。")
        save(
            self.directory / "node-control.json", {"enabled": enabled, "revision": uuid.uuid4().hex}
        )
        self.sharing_requested = enabled
        self.progress("已请求开启共享。" if enabled else "已请求关闭共享，等待节点确认停止。")

    def stop(self, close_workbench=False):
        self.code_until = 0
        if "node" in self.processes and self.processes["node"].poll() is None:
            try:
                self.sharing(False)
            except OSError:
                pass
        roles = [role for role in reversed(self.processes) if role != "web"]
        if close_workbench and "web" in self.processes:
            roles.append("web")
        for role in roles:
            process = self.processes[role]
            self.progress(
                f"正在停止 {dict(node='本机计算', task='任务分配服务', resource='设备管理服务', web='工作台').get(role, role)}…"
            )
            if process.poll() is None:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=10,
                    )
                else:
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    if os.name == "nt":
                        process.kill()
                    else:
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
            if process.stdin:
                process.stdin.close()
            self.processes.pop(role)
            log = self.logs.pop(role, None)
            if log:
                log.close()
        self.mode, self.sharing_requested = "idle", False
        self.progress("本机计算与后端服务已停止，配置与任务数据保留。")

    def test_job(self, images=None):
        if self.mode != "center":
            raise ValueError("请先启动中心。")
        prepared = []
        if images is not None:
            if not isinstance(images, list) or not 1 <= len(images) <= 20:
                raise ValueError("请选择 1–20 张 PNG/JPG 图片。")
            hashes = set()
            total = 0
            for index, image in enumerate(images):
                try:
                    name = str(image["name"]).replace("\\", "/").split("/")[-1]
                    suffix = Path(name).suffix.lower()
                    raw = base64.b64decode(image["data"], validate=True)
                except (KeyError, TypeError, ValueError):
                    raise ValueError("图片数据无效，请重新选择。") from None
                total += len(raw)
                if suffix not in {".png", ".jpg", ".jpeg"} or not raw or total > 20 * 1024 * 1024:
                    raise ValueError("只支持 PNG/JPG，图片总大小不能超过 20 MB。")
                if not (
                    raw.startswith(b"\x89PNG\r\n\x1a\n")
                    if suffix == ".png"
                    else raw.startswith(b"\xff\xd8\xff")
                ):
                    raise ValueError("图片内容与 PNG/JPG 格式不符。")
                digest = hashlib.sha256(raw).hexdigest()
                if digest in hashes:
                    raise ValueError("选择了内容重复的图片，请移除重复项。")
                hashes.add(digest)
                prepared.append((f"{index + 1:03d}-" + re.sub(r"[^\w.\-]", "_", name)[:120], raw))
        moment = datetime.now(timezone(timedelta(hours=8)))
        title = moment.strftime("%Y-%m-%d %H:%M:%S") + "（北京时间）"
        output = self.directory / (
            "test-" + moment.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
        )
        self.last_result = None
        self.last_result_title = ""
        self.test_run = (output, "running")
        try:
            self.verify_test_job(output, title, moment, prepared)
        except Exception:
            self.test_run = (output, "failed")
            raise
        self.test_run = (output, "passed")

    def verify_test_job(self, output, title, moment, prepared):
        image_dir = output / "inputs"
        image_dir.mkdir(parents=True)
        save(
            output / "test-info.json",
            {
                "title": title,
                "started_at": moment.isoformat(),
                "source": "upload" if prepared else "sample",
                "images": [i[0] for i in prepared],
            },
        )
        generator = "from PIL import Image,ImageDraw,ImageFont; from pathlib import Path; import sys\nfor i,text in enumerate(('HELLO OCR 123','APPROVED 456')):\n im=Image.new('RGB',(640,220),'white'); ImageDraw.Draw(im).text((30,70),text,font=ImageFont.load_default(size=44),fill='black'); im.save(Path(sys.argv[1])/f'input-{i}.png')"
        if not prepared:
            subprocess.run(
                [self.runtime.selected["P1"], "-c", generator, str(image_dir)],
                env=managed_runtime.process_environment(self.runtime.selected["P1"]),
                check=True,
                timeout=15,
            )
        else:
            for name, raw in prepared:
                (image_dir / name).write_bytes(raw)
        self.progress(f"测试 {title}：正在提交 {len(prepared) or 2} 张图片，等待 OCR 与结果核验…")
        verification = output / "verification"
        with (self.directory / "test.log").open("wb") as log:
            result = subprocess.run(
                [
                    self.runtime.selected["P2"],
                    str(ROOT / "P4/scripts/verify.py"),
                    "--config",
                    str(self.directory / "center.json"),
                    "--inputs",
                    str(image_dir),
                    "--out",
                    str(verification),
                    "--timeout",
                    "180",
                    "--profiles-dir",
                    str(self.profile_dir),
                ],
                stdout=log,
                stderr=log,
                env=managed_runtime.process_environment(self.runtime.selected["P2"]),
                timeout=210,
            )
        if result.returncode:
            raise ValueError(f"测试未通过，证据保留于 {output}；查看测试日志。")
        report = read_json(verification / "report.json")
        resources = request_json(
            self.config["resource_url"] + "/api/resource/v1/resources",
            token=self.config["tokens"]["DEMO_TOKEN"],
            timeout=3,
        )["resources"]
        names = {n["worker_id"]: n.get("name", n["worker_id"]) for n in resources}
        for execution in read_json(verification / "executions.json"):
            worker = execution["worker_id"]
            self.progress(
                f"设备 {names.get(worker, worker)}：执行 {execution['execution_id']}，输入 {execution.get('input_sha256', '')[:12]}，结果 {execution['status']}"
            )
        shutil.copyfile(verification / "results.zip", output / "results.zip")
        self.last_result = output / "results.zip"
        self.last_result_title = title
        self.progress(
            f"测试 {title}：真实OCR/结果/ZIP验证通过；贡献节点数 {len(report.get('successful_workers', []))}。证据：{output}"
        )

    def test_status(self):
        run = self.test_run
        if run is None:
            return None
        output, outcome = run
        reference = {}
        try:
            reference = read_json(output / "verification" / "job-reference.json")
        except (OSError, ValueError):
            # The verifier may still be uploading inputs or publishing the reference.
            pass
        return {"state": outcome, "job_id": reference.get("job_id")}

    def node_status(self):
        try:
            node = read_json(self.directory / "node-status.json")
            if time.time() - node["received_at"] <= 12:
                return {
                    "connection": "ONLINE",
                    "mode": node["mode"],
                    "worker_id": node["worker_id"],
                }
            return {"connection": "STALE", "mode": None, "worker_id": node["worker_id"]}
        except (OSError, ValueError, KeyError):
            return {"connection": "PENDING", "mode": None, "worker_id": None}

    def background(self, function):
        with self.lock:
            if self.busy:
                raise ValueError("当前操作尚未完成，请稍候。")
            self.busy = True
            self.operation_started = time.monotonic()
            self.message = "正在处理操作，请稍候；下方日志会显示进度。"

        def work():
            try:
                function()
            except Exception as exc:
                if isinstance(exc, urllib.error.HTTPError):
                    try:
                        message = json.load(exc).get("error", "中心拒绝配对")
                    except ValueError:
                        message = "中心拒绝请求"
                elif isinstance(exc, subprocess.CalledProcessError):
                    message = "环境安装或子程序执行失败，请查看下方日志。"
                else:
                    message = str(exc)
                self.progress("未完成：" + message)
            finally:
                with self.lock:
                    self.busy = False
                    self.operation_started = None

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def status(self):
        with self.lock:
            state = {
                "system": platform.system() + " / " + platform.machine(),
                "mode": self.mode,
                "message": self.message,
                "busy": self.busy,
                "operation_seconds": int(time.monotonic() - self.operation_started)
                if self.operation_started is not None
                else 0,
                "addresses": self.addresses,
                "address": self.address,
                "control_port": self.port,
                "discovery_error": self.discovery_error,
                "node_name": self.name,
                "sharing_requested": self.sharing_requested,
                "code": self.code
                if self.mode == "center"
                and time.monotonic() < self.code_until
                and self.bad_codes < 5
                else "",
                "processes": {k: p.poll() is None for k, p in self.processes.items()},
                "python_paths": dict(self.runtime.selected),
                "compute": self.compute,
            }
        if state["processes"].get("web"):
            state["workbench"] = {
                "local_url": f"http://127.0.0.1:{self.web_port}/",
                "lan_url": f"http://{state['address']}:{self.web_port}/"
                if state["mode"] == "center" and state["address"] != "127.0.0.1"
                else "",
            }
        state["connection_code"] = (
            encode_connection_code(state["address"], state["control_port"], state["code"])
            if state["code"]
            else ""
        )
        state["workbench_connected"] = self.mode in {"center", "node"} and bool(
            self.config and self.config.get("task_url")
        )
        state["logs"] = {"操作记录": "\n".join(self.events)}
        for role in ("resource", "task", "node", "web", "test", "env/setup"):
            path = self.directory / (role + ".log")
            if path.exists():
                with path.open("rb") as stream:
                    stream.seek(max(0, path.stat().st_size - 5000))
                    state["logs"][role] = stream.read().decode("utf-8", errors="replace")
        saved = self.directory / "last-center.json"
        state["last_center"] = read_json(saved)["address"] if saved.exists() else ""
        state["saved_pair"] = (self.directory / "node.json").exists()
        if state["saved_pair"]:
            state["saved_pair"] = (
                read_json(self.directory / "node.json").get("paired_device_id") == self.identity
            )
        state["has_result"] = self.last_result is not None and self.last_result.exists()
        state["ocr_test"] = self.test_status() if state["mode"] == "center" else None
        state["local_node"] = self.node_status() if state["processes"].get("node") else None
        state["result_title"] = self.last_result_title
        state["result_filename"] = self.last_result.parent.name + ".zip" if self.last_result else ""
        if any(not running for running in state["processes"].values()):
            state["message"] = (
                "有服务已退出，请查看下方日志。点击“停止本机服务”后可以重试。\n" + state["message"]
            )
        if state["mode"] == "node" and not state["processes"].get("node"):
            state["message"] = (
                "未完成：本机计算节点已退出，请查看“本机计算”日志；修复后可点击“开启计算”重试。"
            )
        if self.mode == "center" and self.config:
            try:
                state["nodes"] = request_json(
                    self.config["resource_url"] + "/api/resource/v1/resources",
                    token=self.config["tokens"]["DEMO_TOKEN"],
                    timeout=1,
                )["resources"]
            except (OSError, ValueError, KeyError):
                state["nodes"] = []
        return state


def usable_lan_ip(value):
    try:
        address = ipaddress.ip_address(value)
        return any(
            address in ipaddress.ip_network(n)
            for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
        )
    except (ValueError, TypeError):
        return False


def discovered_center(body, peer, nonce, exclude_id=None, exclude_name=None):
    if not isinstance(body, dict) or body.get("protocol") != PROTOCOL or body.get("nonce") != nonce:
        return None
    if (exclude_id and body.get("center_id") == exclude_id) or (
        not body.get("center_id") and exclude_name and body.get("name") == exclude_name
    ):
        return None
    # A reply can arrive through VMware/WSL. Use the center's chosen LAN address.
    address = body.get("address", peer[0])
    if not usable_lan_ip(address):
        return None
    try:
        port = int(body["port"])
        if not 1 <= port <= 65535:
            return None
    except (TypeError, ValueError, KeyError):
        return None
    return {"address": f"{address}:{port}", "name": str(body.get("name", "中心"))[:100]}


def discover(timeout=1.2, exclude_id=None, exclude_name=None):
    found = {}
    nonce = secrets.token_hex(12)
    clients = []
    message = json.dumps({"protocol": PROTOCOL, "nonce": nonce}).encode()
    try:
        # Bind one request socket to each LAN interface instead of trusting the OS
        # route for a single global broadcast (which can select a virtual adapter).
        for local in dict.fromkeys(
            [a for a in lan_addresses() if usable_lan_ip(a)] + ["127.0.0.1"]
        ):
            client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                client.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                client.bind((local, 0))
                client.sendto(
                    message,
                    ("127.0.0.1" if local == "127.0.0.1" else "255.255.255.255", DISCOVERY_PORT),
                )
                clients.append(client)
            except OSError:
                client.close()
        until = time.monotonic() + timeout
        while clients and time.monotonic() < until:
            readable, _, _ = select.select(clients, [], [], max(0, until - time.monotonic()))
            for client in readable:
                try:
                    raw, peer = client.recvfrom(4096)
                    center = discovered_center(
                        json.loads(raw), peer, nonce, exclude_id, exclude_name
                    )
                    if center:
                        found[center["address"]] = center
                except (OSError, ValueError):
                    pass
    finally:
        for client in clients:
            client.close()
    return list(found.values())


def discovery_server(panel):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server:
        try:
            server.bind(("0.0.0.0", DISCOVERY_PORT))
        except OSError:
            panel.discovery_error = f"无法监听 UDP {DISCOVERY_PORT}，其他电脑可用中心地址手动配对。"
            return
        server.settimeout(0.5)
        while not panel.closed.is_set():
            try:
                raw, peer = server.recvfrom(4096)
                body = json.loads(raw)
                if not isinstance(body, dict):
                    continue
                if (
                    panel.mode == "center"
                    and panel.address != "127.0.0.1"
                    and body.get("protocol") == PROTOCOL
                    and isinstance(body.get("nonce"), str)
                ):
                    server.sendto(
                        json.dumps(
                            {
                                "protocol": PROTOCOL,
                                "nonce": body["nonce"][:64],
                                "port": panel.port,
                                "name": panel.name,
                                "address": panel.address,
                                "center_id": panel.identity,
                            }
                        ).encode(),
                        peer,
                    )
            except (OSError, ValueError):
                pass


def handler_for(panel):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Never log pairing codes or payloads.

        def send(
            self, status, body, content_type="application/json; charset=utf-8", cache="no-store"
        ):
            raw = (
                body
                if isinstance(body, bytes)
                else (
                    body.encode()
                    if isinstance(body, str)
                    else json.dumps(body, ensure_ascii=False).encode()
                )
            )
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-P4-Control", PROTOCOL)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            )
            self.end_headers()
            self.wfile.write(raw)

        def local(self):
            host = self.headers.get("Host", "")
            return ipaddress.ip_address(self.client_address[0]).is_loopback and host in {
                f"127.0.0.1:{panel.port}",
                f"localhost:{panel.port}",
            }

        def do_GET(self):
            if not self.local():
                return self.send(403, {"error": "控制页面仅允许本机访问"})
            if self.path in CONTROL_ASSETS:
                asset = ROOT / "web/public" / self.path.lstrip("/")
                return self.send(200, asset.read_bytes(), CONTROL_ASSETS[self.path], "no-cache")
            if self.path == "/api/bridge-session" and self.headers.get("X-P4-Bridge") == "1":
                return self.send(200, {"session": panel.session})
            if self.path == "/api/bridge-upstreams" and self.headers.get("X-P4-Bridge") == "1":
                with panel.lock:
                    if panel.mode in {"center", "node"} and panel.config:
                        return self.send(
                            200,
                            {
                                "resource": panel.config["resource_url"],
                                "task": panel.config.get("task_url"),
                                "lan": panel.mode == "center" and panel.address != "127.0.0.1",
                            },
                        )
                return self.send(503, {"error": "中心服务未连接"})
            if self.path == "/":
                html = (
                    (ROOT / "P4/control.html")
                    .read_text(encoding="utf-8")
                    .replace("__SESSION__", json.dumps(panel.session))
                )
                return self.send(200, html, "text/html; charset=utf-8")
            if self.path == "/api/status" and secrets.compare_digest(
                self.headers.get("X-P4-Session", ""), panel.session
            ):
                return self.send(200, panel.status())
            if self.path == "/api/result.zip" and secrets.compare_digest(
                self.headers.get("X-P4-Session", ""), panel.session
            ):
                if panel.last_result and panel.last_result.exists():
                    return self.send(200, panel.last_result.read_bytes(), "application/zip")
            return self.send(404, {"error": "NOT_FOUND"})

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                upload = self.path == "/api/test-images"
                if upload and (
                    not self.local()
                    or not secrets.compare_digest(
                        self.headers.get("X-P4-Session", ""), panel.session
                    )
                ):
                    return self.send(403, {"error": "本机会话无效"})
                if not 0 < length <= (29 * 1024 * 1024 if upload else 8192):
                    return self.send(413, {"error": "请求大小不正确"})
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError("请求应为对象")
                if self.path == "/api/pair":
                    return self.send(200, panel.pair(body))
                if not self.local() or not secrets.compare_digest(
                    self.headers.get("X-P4-Session", ""), panel.session
                ):
                    return self.send(403, {"error": "本机会话无效"})
                origin = self.headers.get("Origin")
                if origin and origin != "http://" + self.headers.get("Host", ""):
                    return self.send(403, {"error": "跨站请求被拒绝"})
                if self.path == "/api/workbench-token":
                    with panel.lock:
                        if panel.mode != "center":
                            return self.send(409, {"error": "请先启动中心。"})
                        return self.send(200, {"token": panel.config["tokens"]["DEMO_TOKEN"]})
                if self.path == "/api/discover":
                    return self.send(
                        200,
                        {"centers": discover(exclude_id=panel.identity, exclude_name=panel.name)},
                    )
                if self.path in {"/api/on", "/api/off"}:
                    panel.background(lambda: panel.set_computing(self.path == "/api/on"))
                    return self.send(202, {"requested": True})
                actions = {
                    "/api/center": lambda: panel.start_center(
                        bool(body.get("local_node", True)), body.get("address")
                    ),
                    "/api/connect": lambda: panel.join_connection(body.get("connection_code")),
                    "/api/join": lambda: panel.join(
                        str(body.get("address", "")), str(body.get("code", ""))
                    ),
                    "/api/reconnect": panel.reconnect,
                    "/api/stop": panel.stop,
                    "/api/test": panel.test_job,
                    "/api/test-images": lambda: panel.test_job(body.get("images", [])),
                    "/api/code": panel.new_code,
                }
                if self.path not in actions:
                    return self.send(404, {"error": "NOT_FOUND"})
                panel.background(actions[self.path])
                return self.send(202, {"accepted": True})
            except (ValueError, KeyError) as exc:
                return self.send(400, {"error": str(exc)})

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8003)
    parser.add_argument("--web-port", type=int, default=5173)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "P4/var/control")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--compute-config", help="计算配置 JSON；不包含连接凭证")
    parser.add_argument(
        "--control-only",
        action="store_true",
        help="Open the fallback P4 panel for a node-only device.",
    )
    args = parser.parse_args()
    if not 1 <= args.web_port <= 65535 or args.web_port in {8001, 8002, args.port}:
        parser.error("--web-port must be a valid port distinct from P1, P2 and the control panel")
    panel = Panel(args.data_dir, args.port, args.web_port, args.compute_config)
    try:
        server = ControlHTTPServer(("0.0.0.0", args.port), handler_for(panel))
        panel.port = server.server_port
    except OSError:
        url = f"http://127.0.0.1:{args.port}"
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(url, timeout=2) as response:
                existing = response.headers.get("X-P4-Control") == PROTOCOL
            if existing:
                if args.control_only:
                    print("控制台已在运行：" + url)
                    if not args.no_browser:
                        webbrowser.open(url)
                    return 0
                bridge_request = urllib.request.Request(
                    url + "/api/bridge-session", headers={"X-P4-Bridge": "1"}
                )
                with opener.open(bridge_request, timeout=2) as response:
                    session = json.load(response)["session"]
                status_request = urllib.request.Request(
                    url + "/api/status", headers={"X-P4-Session": session}
                )
                with opener.open(status_request, timeout=4) as response:
                    workbench = json.load(response).get("workbench")
                if workbench:
                    frontend = workbench["local_url"]
                    print("工作台已在运行：" + frontend)
                    if not args.no_browser:
                        webbrowser.open(frontend)
                    return 0
                print("现有控制台未启动工作台。请退出旧实例，再运行启动脚本。", file=sys.stderr)
                return 1
        except (OSError, ValueError, KeyError):
            pass
        print(
            f"控制台端口 {args.port} 已占用，无法确认现有工作台。请退出旧控制台再重试。",
            file=sys.stderr,
        )
        return 1
    url = f"http://127.0.0.1:{panel.port}"
    print("控制台：" + url, flush=True)
    open_url = url
    if not args.control_only:
        try:
            panel.ensure_workbench()
            open_url = f"http://127.0.0.1:{panel.web_port}/"
            print("工作台：" + open_url, flush=True)
        except (ValueError, OSError) as exc:
            print(f"无法打开前端：{exc} 日志：{panel.directory / 'web.log'}", file=sys.stderr)
            server.server_close()
            return 1
    threading.Thread(target=discovery_server, args=(panel,), daemon=True).start()
    if not args.no_browser:
        webbrowser.open(open_url)
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    finally:
        panel.closed.set()
        panel.runtime.cancelled.set()
        if panel.worker:
            panel.worker.join(timeout=12)
        panel.stop(close_workbench=True)
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
