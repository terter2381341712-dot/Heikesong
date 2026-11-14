"""Local P4 control panel: system detection, explicit sharing, LAN discovery and pairing.

Only the pairing endpoint is exposed to LAN clients. Product services keep their APIs.
"""

import argparse
import ast
import hashlib
import ipaddress
import json
import os
import platform
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cluster  # noqa: E402

ROOT = cluster.ROOT
DISCOVERY_PORT = 37043
PROTOCOL = "P4-PAIR-2"


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


def fingerprint():
    digest = hashlib.sha256()
    for folder in ("node_agent", "resource_service", "runners", "profiles"):
        for path in sorted((ROOT / "P1" / folder).rglob("*"), key=lambda p: p.as_posix()):
            if path.suffix in {".py", ".json"}:
                digest.update(path.relative_to(ROOT / "P1").as_posix().encode())
                # Runtime profile copies use the same LF bytes on every system.
                raw = path.read_bytes()
                digest.update(raw.replace(b"\r\n", b"\n"))
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
            ROOT / "P4/var/mac-p1" / suffix,
            ROOT / "P4/var/python39/python.exe",
            ROOT / "P4/.venv" / suffix,
            Path(sys.executable),
        ]
        for name in ("python3.9", "python3.11", "python3.12", "python3.13", "python3", "python"):
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
        return list(dict.fromkeys(p for p in values if p.is_file()))

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
            result["compatible"] = (
                version == (3, 9, 6) if component == "P1" else version >= (3, 9, 6)
            )
            return result
        except (OSError, ValueError, subprocess.SubprocessError):
            return {"compatible": False, "ready": False}

    def ensure(self, component, progress):
        if component == "P2" and "P1" in self.selected:
            # One exact Python 3.9.6 interpreter for the normal center workflow.
            executable = Path(self.selected["P1"])
            if not self.probe(executable, "P2")["ready"]:
                progress("正在为 Python 3.9.6 补齐P2依赖，P1/P2将共用本机环境…")
                self.directory.mkdir(parents=True, exist_ok=True)
                probe = subprocess.run(
                    [str(executable), "-c", "import pip"], capture_output=True, timeout=10
                )
                if probe.returncode == 0:
                    command = [str(executable), "-m", "pip"]
                else:
                    # The optional Windows embedded interpreter has no bundled pip.
                    helper = None
                    for candidate in self.candidates("P2"):
                        if candidate == executable:
                            continue
                        try:
                            help_result = subprocess.run(
                                [str(candidate), "-m", "pip", "--help"],
                                capture_output=True,
                                timeout=10,
                            )
                            if help_result.returncode == 0 and b"--python" in help_result.stdout:
                                helper = candidate
                                break
                        except (OSError, subprocess.SubprocessError):
                            continue
                    if helper is None:
                        raise ValueError("当前Python缺少pip，请为Python安装pip后重试。")
                    command = [str(helper), "-m", "pip", "--python", str(executable)]
                text = (ROOT / "P2/pyproject.toml").read_text(encoding="utf-8")
                dependencies = ast.literal_eval(
                    re.search(r"dependencies\s*=\s*(\[[\s\S]*?\])", text).group(1)
                )
                command += [
                    "install",
                    "--disable-pip-version-check",
                    "--cache-dir",
                    str(self.directory / "pip-cache"),
                    *dependencies,
                ]
                with self.log.open("ab") as output:
                    self.setup_command(command, output)
            if (
                not self.probe(executable, "P1")["ready"]
                or not self.probe(executable, "P2")["ready"]
            ):
                raise ValueError("统一环境未通过P1/P2联合依赖检查，请查看准备日志。")
            self.selected["P2"] = str(executable.resolve())
            return self.selected["P2"]
        compatible = []
        progress(f"检测 {component} 的本机解释器和依赖…")
        for executable in self.candidates(component):
            result = self.probe(executable, component)
            if result["compatible"]:
                if result["ready"]:
                    self.selected[component] = str(executable.resolve())
                    return self.selected[component]
                compatible.append(executable)
        if not compatible:
            required = "Python 3.9.6（完整版本号）" if component == "P1" else "Python 3.9.6 或以上"
            raise ValueError(
                f"本机没有找到 {required}。请先安装该解释器，再点按钮重试；无需填写路径。"
            )
        target = self.directory / component
        suffix = "Scripts/python.exe" if os.name == "nt" else "bin/python"
        executable = target / suffix
        self.directory.mkdir(parents=True, exist_ok=True)
        progress(f"首次准备 {component} 环境，正在安装依赖，可能需要几分钟…")
        with self.log.open("ab") as output:
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
            if component == "P1":
                command += ["-r", str(ROOT / "P1/requirements.txt")]
            else:
                text = (ROOT / "P2/pyproject.toml").read_text(encoding="utf-8")
                command += ast.literal_eval(
                    re.search(r"dependencies\s*=\s*(\[[\s\S]*?\])", text).group(1)
                )
            self.setup_command(command, output)
        if not self.probe(executable, component)["ready"]:
            raise ValueError(f"{component} 依赖核验未通过，查看 {self.log}")
        self.selected[component] = str(executable.resolve())
        return self.selected[component]


class Panel:
    def __init__(self, directory, port=8003):
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        identity_file = self.directory / "identity.json"
        marker = hashlib.sha256(
            (platform.system() + socket.gethostname() + str(uuid.getnode())).encode()
        ).hexdigest()
        if not identity_file.exists() or read_json(identity_file).get("machine_marker") != marker:
            save(identity_file, {"id": uuid.uuid4().hex, "machine_marker": marker})
        self.identity = read_json(identity_file)["id"]
        self.name = (
            re.sub(r"[^A-Za-z0-9_-]", "-", socket.gethostname())[:50] + "-" + self.identity[:6]
        )
        self.port, self.session = port, secrets.token_urlsafe(32)
        self.runtime = Runtime(self.directory / "env")
        self.addresses = lan_addresses()
        self.address = self.addresses[0] if self.addresses else ""
        self.lock = threading.RLock()
        self.processes, self.logs = {}, {}
        self.mode, self.message, self.busy = "idle", "选择本机角色开始。", False
        self.code, self.code_until, self.bad_codes = "", 0, 0
        self.config = None
        self.sharing_requested = False
        self.closed = threading.Event()
        self.worker = None
        self.last_result = None
        self.profile_dir = self.directory / "profiles"

    def progress(self, message):
        with self.lock:
            self.message = message

    def spawn(self, role):
        if self.closed.is_set():
            raise ValueError("控制台已关闭。")
        component = "P2" if role == "task" else "P1"
        executable = self.runtime.selected[component]
        module = {"resource": "resource_service", "task": "task_service", "node": "node_agent"}[
            role
        ]
        argv = cluster.module_command(executable, component, module)
        if role == "node":
            save(
                self.directory / "node-control.json",
                {"enabled": False, "revision": uuid.uuid4().hex},
            )
            argv = [
                executable,
                str(ROOT / "P4/scripts/managed_node.py"),
                "--server",
                self.config["resource_url"],
                "--name",
                self.name,
                "--control-file",
                str(self.directory / "node-control.json"),
            ]
        env = cluster.environment(
            self.config, role, self.directory / "data", "0.0.0.0", 8001, 8002, self.name
        )
        if role in {"resource", "node"}:
            materialize_profiles(ROOT / "P1/profiles", self.profile_dir)
            env["RESOURCE_PROFILES_DIR"] = str(self.profile_dir)
            env["NODE_PROFILES_DIR"] = str(self.profile_dir)
        self.logs[role] = (self.directory / f"{role}.log").open("ab")
        self.processes[role] = subprocess.Popen(
            argv,
            cwd=ROOT / component,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=self.logs[role],
            stderr=subprocess.STDOUT,
            start_new_session=os.name != "nt",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )

    def wait_health(self, role):
        url = (
            self.config["resource_url"] + "/api/resource/v1/healthz"
            if role == "resource"
            else self.config["task_url"] + "/api/task/v1/healthz"
        )
        for _ in range(60):
            if self.processes[role].poll() is not None:
                raise ValueError(f"{role} 启动失败，查看本页日志。")
            try:
                body = request_json(url, timeout=1)
                if body["contract_version"] == "0.2.0":
                    return
            except (OSError, ValueError, KeyError):
                pass
            time.sleep(0.3)
        raise ValueError(f"{role} 未就绪，请检查网卡、端口占用和本页日志。")

    def new_code(self):
        with self.lock:
            if self.mode != "center":
                raise ValueError("请先启动中心。")
            self.code = f"{secrets.randbelow(1000000):06d}"
            self.code_until, self.bad_codes = time.monotonic() + 600, 0

    def start_center(self, local_node=True, address=None):
        if self.mode != "idle":
            raise ValueError("请先停止当前角色，再切换。")
        selected_address = address or self.address
        if selected_address not in self.addresses:
            raise ValueError("没有找到可用局域网网卡，请连接Wi-Fi/以太网后重新打开控制台。")
        self.runtime.ensure("P1", self.progress)
        self.runtime.ensure("P2", self.progress)
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
        # Refuse occupied ports before launching either service; never stop another owner's service.
        for port in (8001, 8002):
            with socket.socket() as probe:
                if os.name == "nt":
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                probe.bind(("0.0.0.0", port))
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
            self.new_code()
            self.progress("中心已就绪。另一台电脑选择加入节点，输入配对码即可。")
        except Exception:
            self.stop()
            raise

    def pair(self, body):
        with self.lock:
            if self.mode != "center" or time.monotonic() > self.code_until or self.bad_codes >= 5:
                raise ValueError("配对未开启或已失效，请在中心刷新配对码。")
            if not secrets.compare_digest(str(body.get("code", "")), self.code):
                self.bad_codes += 1
                raise ValueError("配对码不正确。")
            if body.get("protocol") != PROTOCOL or body.get("fingerprint") != fingerprint():
                raise ValueError("两端P1代码或模型锁文件不同，请同步最新源码后重试。")
            return {
                "protocol": PROTOCOL,
                "resource_url": self.config["resource_url"],
                "tokens": {"JOIN_TOKEN": self.config["tokens"]["JOIN_TOKEN"]},
            }

    def join(self, address, code):
        if self.mode != "idle":
            raise ValueError("请先停止当前角色，再切换。")
        self.runtime.ensure("P1", self.progress)
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
        config = request_json(
            f"http://{host}:{port}/api/pair",
            {"protocol": PROTOCOL, "code": code, "fingerprint": fingerprint()},
            timeout=10,
        )
        if config.get("protocol") != PROTOCOL or set(config.get("tokens", {})) != {"JOIN_TOKEN"}:
            raise ValueError("配对响应不符合协议。")
        cluster.base_url(config["resource_url"])
        config["local_fingerprint"] = fingerprint()
        config["paired_device_id"] = self.identity
        self.config = config
        save(self.directory / "node.json", config)
        save(self.directory / "last-center.json", {"address": address})
        self.start_node()
        self.progress("已发送共享开启请求。注册、准备与计算状态请查看节点日志和中心资源列表。")

    def start_node(self):
        try:
            self.spawn("node")
            self.sharing(True)
            self.mode = "node"
        except Exception:
            self.stop()
            raise

    def reconnect(self):
        if self.mode != "idle":
            raise ValueError("请先停止当前角色。")
        path = self.directory / "node.json"
        if not path.exists():
            raise ValueError("本机尚未配对，请输入中心配对码。")
        config = read_json(path)
        if config.get("paired_device_id") != self.identity:
            raise ValueError("这是另一台设备的配对记录，请在本机重新输入中心配对码。")
        if config.get("local_fingerprint") != fingerprint():
            raise ValueError("本机源码或模型锁已更新，请重新配对核对版本。")
        self.runtime.ensure("P1", self.progress)
        request_json(config["resource_url"] + "/api/resource/v1/healthz")
        self.config = config
        self.start_node()
        self.progress("已使用本机保存的配对信息重连，并请求开启共享。")

    def sharing(self, enabled):
        process = self.processes.get("node")
        if not process or process.poll() is not None:
            raise ValueError("本机计算节点尚未启动。")
        save(
            self.directory / "node-control.json", {"enabled": enabled, "revision": uuid.uuid4().hex}
        )
        self.sharing_requested = enabled
        self.progress("已请求开启共享。" if enabled else "已请求关闭共享，等待节点确认停止。")

    def stop(self):
        self.code_until = 0
        if "node" in self.processes and self.processes["node"].poll() is None:
            try:
                self.sharing(False)
            except OSError:
                pass
        for process in reversed(list(self.processes.values())):
            if process.poll() is None:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
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
                    process.wait()
            if process.stdin:
                process.stdin.close()
        self.processes.clear()
        for log in self.logs.values():
            log.close()
        self.logs.clear()
        self.mode, self.sharing_requested = "idle", False
        self.progress("本次启动的服务已停止，配置与任务数据保留。")

    def test_job(self):
        if self.mode != "center":
            raise ValueError("请先启动中心。")
        image_dir = self.directory / "test-inputs"
        image_dir.mkdir(exist_ok=True)
        generator = "from PIL import Image,ImageDraw,ImageFont; from pathlib import Path; import sys\nfor i,text in enumerate(('HELLO OCR 123','APPROVED 456')):\n im=Image.new('RGB',(640,220),'white'); ImageDraw.Draw(im).text((30,70),text,font=ImageFont.load_default(size=44),fill='black'); im.save(Path(sys.argv[1])/f'input-{i}.png')"
        subprocess.run(
            [self.runtime.selected["P1"], "-c", generator, str(image_dir)], check=True, timeout=15
        )
        output = self.directory / ("test-" + uuid.uuid4().hex[:8])
        self.progress("正在提交两张新测试图，等待真实OCR与结果下载核验…")
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
                    str(output),
                    "--timeout",
                    "180",
                    "--profiles-dir",
                    str(self.profile_dir),
                ],
                stdout=log,
                stderr=log,
            )
        if result.returncode:
            raise ValueError(f"测试未通过，证据保留于 {output}；查看测试日志。")
        report = read_json(output / "report.json")
        self.last_result = output / "results.zip"
        self.progress(
            f"真实OCR/结果/ZIP验证通过；贡献节点数 {len(report.get('successful_workers', []))}。证据：{output}"
        )

    def background(self, function):
        with self.lock:
            if self.busy:
                raise ValueError("当前操作尚未完成，请稍候。")
            self.busy = True

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

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def status(self):
        with self.lock:
            state = {
                "system": platform.system() + " / " + platform.machine(),
                "mode": self.mode,
                "message": self.message,
                "busy": self.busy,
                "addresses": self.addresses,
                "address": self.address,
                "node_name": self.name,
                "sharing_requested": self.sharing_requested,
                "code": self.code
                if self.mode == "center"
                and time.monotonic() < self.code_until
                and self.bad_codes < 5
                else "",
                "processes": {k: p.poll() is None for k, p in self.processes.items()},
                "python_paths": dict(self.runtime.selected),
            }
        state["logs"] = {}
        for role in ("resource", "task", "node", "test", "env/setup"):
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
        if any(not running for running in state["processes"].values()):
            state["message"] = (
                "有子进程已退出，请查看下方日志。停止本机角色后可以重试。\n" + state["message"]
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


def discover(timeout=1.2):
    found = {}
    nonce = secrets.token_hex(12)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
        client.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        client.settimeout(0.2)
        message = json.dumps({"protocol": PROTOCOL, "nonce": nonce}).encode()
        for target in ("255.255.255.255", "127.0.0.1"):
            client.sendto(message, (target, DISCOVERY_PORT))
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            try:
                raw, peer = client.recvfrom(4096)
                body = json.loads(raw)
                if not isinstance(body, dict):
                    continue
                if body.get("protocol") == PROTOCOL and body.get("nonce") == nonce:
                    key = peer[0] + ":" + str(int(body["port"]))
                    found[key] = {"address": key, "name": str(body.get("name", "中心"))[:100]}
            except (OSError, ValueError, KeyError):
                pass
    return list(found.values())


def discovery_server(panel):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server:
        try:
            server.bind(("0.0.0.0", DISCOVERY_PORT))
        except OSError:
            return  # Pairing by an explicit address remains available.
        server.settimeout(0.5)
        while not panel.closed.is_set():
            try:
                raw, peer = server.recvfrom(4096)
                body = json.loads(raw)
                if not isinstance(body, dict):
                    continue
                if (
                    panel.mode == "center"
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

        def send(self, status, body, content_type="application/json; charset=utf-8"):
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
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-P4-Control", PROTOCOL)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'",
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
                if not 0 < length <= 8192:
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
                if self.path == "/api/discover":
                    return self.send(200, {"centers": discover()})
                if self.path in {"/api/on", "/api/off"}:
                    panel.sharing(self.path == "/api/on")
                    return self.send(200, {"requested": True})
                actions = {
                    "/api/center": lambda: panel.start_center(
                        bool(body.get("local_node", True)), body.get("address")
                    ),
                    "/api/join": lambda: panel.join(
                        str(body.get("address", "")), str(body.get("code", ""))
                    ),
                    "/api/reconnect": panel.reconnect,
                    "/api/stop": panel.stop,
                    "/api/on": lambda: panel.sharing(True),
                    "/api/off": lambda: panel.sharing(False),
                    "/api/test": panel.test_job,
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
    parser.add_argument("--data-dir", type=Path, default=ROOT / "P4/var/control")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    panel = Panel(args.data_dir, args.port)
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
                print("控制台已在运行：" + url)
                if not args.no_browser:
                    webbrowser.open(url)
                return 0
        except OSError:
            pass
        print(
            f"控制台端口 {args.port} 已占用；先访问 http://127.0.0.1:{args.port} 或停止已有控制台。",
            file=sys.stderr,
        )
        return 1
    threading.Thread(target=discovery_server, args=(panel,), daemon=True).start()
    url = f"http://127.0.0.1:{panel.port}"
    print("控制台：" + url, flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    finally:
        panel.closed.set()
        panel.runtime.cancelled.set()
        if panel.worker:
            panel.worker.join(timeout=12)
        panel.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
