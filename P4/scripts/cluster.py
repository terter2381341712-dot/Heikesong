"""P4 deployment helper. Standard library only; never changes product modules."""

import argparse
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]


def base_url(value):
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username
    ):
        raise ValueError("地址必须为 http(s)://主机:端口，不带路径或尾斜杠")
    if parsed.hostname in {"0.0.0.0", "::"}:
        raise ValueError("连接地址不能是监听地址 0.0.0.0/::")
    return value


def write_private(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Do not overwrite existing deployment identities or tokens.
    with path.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    if os.name != "nt":
        path.chmod(0o600)


def init(args):
    directory = Path(args.out).resolve()
    resource = base_url(args.resource_url)
    task = base_url(args.task_url)
    tokens = {key: secrets.token_urlsafe(32) for key in ("DEMO_TOKEN", "TASK_TOKEN", "JOIN_TOKEN")}
    shared = {"resource_url": resource, "task_url": task}
    names = ("center.json", "node.json", "task.json", "client.json")
    if any((directory / name).exists() for name in names):
        raise ValueError("配置已存在；重启请复用原文件，需要新部署请指定新 --out")
    write_private(directory / "center.json", {**shared, "tokens": tokens})
    write_private(
        directory / "node.json",
        {"resource_url": resource, "tokens": {"JOIN_TOKEN": tokens["JOIN_TOKEN"]}},
    )
    write_private(
        directory / "task.json",
        {**shared, "tokens": {key: tokens[key] for key in ("DEMO_TOKEN", "TASK_TOKEN")}},
    )
    write_private(
        directory / "client.json",
        {**shared, "tokens": {key: tokens[key] for key in ("DEMO_TOKEN", "TASK_TOKEN")}},
    )
    print(f"配置已生成：{directory}（包含凭证，请勿提交）")


def environment(config, role, directory, bind, resource_port, task_port, node_name):
    env = dict(os.environ)
    for key in (
        "DEMO_TOKEN",
        "TASK_TOKEN",
        "JOIN_TOKEN",
        "PYTHONPATH",
        "PYTHONHOME",
        "P2_ENABLE_MOCK_P1",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "NO_PROXY",
        "no_proxy",
    ):
        env.pop(key, None)
    needed = {
        "resource": ("DEMO_TOKEN", "TASK_TOKEN", "JOIN_TOKEN"),
        "task": ("DEMO_TOKEN", "TASK_TOKEN"),
        "node": ("JOIN_TOKEN",),
    }[role]
    for key in needed:
        env[key] = config["tokens"][key]
        if not env[key]:
            raise ValueError(f"缺少 {key}")
    env.update(
        {
            "RESOURCE_BASE_URL": base_url(config["resource_url"]),
            "RESOURCE_BIND_HOST": bind,
            "RESOURCE_PORT": str(resource_port),
            "TASK_BIND_HOST": bind,
            "TASK_PORT": str(task_port),
            "RESOURCE_DATA_DIR": str(directory / "resource"),
            "TASK_DATA_DIR": str(directory / "task"),
            "NODE_AGENT_DATA_DIR": str(directory / "nodes" / node_name),
            "RESOURCE_PROFILES_DIR": str(ROOT / "P1/profiles"),
            "NODE_PROFILES_DIR": str(ROOT / "P1/profiles"),
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
            "NO_PROXY": "*",
            "no_proxy": "*",
        }
    )
    return env


def select_roles(role=None, roles=None, with_node=False):
    selected = (
        list(roles) if roles is not None else (["resource", "task"] if role == "center" else [role])
    )
    if not selected or any(r not in {"resource", "task", "node"} for r in selected):
        raise ValueError("请选择 resource（P1中心）、task（P2）、node（P1计算），可组合")
    if len(set(selected)) != len(selected):
        raise ValueError("同一角色不能重复启动")
    if with_node and "node" not in selected:
        selected.append("node")
    return [r for r in ("resource", "task", "node") if r in selected]


def configure_run(args):
    if args.machine:
        machine = json.loads(Path(args.machine).read_text(encoding="utf-8"))
        allowed = {
            "roles",
            "config",
            "data_dir",
            "bind",
            "resource_port",
            "task_port",
            "node_name",
            "p1_python",
            "p2_python",
            "interactive",
        }
        if set(machine) - allowed:
            raise ValueError(f"机器配置含未知设置：{sorted(set(machine) - allowed)}")
        if "roles" in machine and not isinstance(machine["roles"], list):
            raise ValueError("机器配置 roles 必须是数组")
        for key, value in machine.items():
            if key == "roles" and args.role:
                continue
            if getattr(args, key, None) is None:
                if key in {"config", "data_dir", "p1_python", "p2_python"} and value:
                    value = str((ROOT / value).resolve())
                setattr(args, key, value)
    defaults = {
        "bind": "0.0.0.0",
        "resource_port": 8001,
        "task_port": 8002,
        "node_name": "node-" + socket.gethostname(),
        "interactive": False,
    }
    for key, value in defaults.items():
        if getattr(args, key, None) is None:
            setattr(args, key, value)
    if not args.config or not args.data_dir:
        raise ValueError("需要 --config 和 --data-dir，或在 --machine 配置中填写")
    select_roles(args.role, args.roles, args.with_node)
    return args


def interpreter_for(role, p1_python=None, p2_python=None, windows=None):
    if windows is None:
        windows = os.name == "nt"
    component = "P2" if role == "task" else "P1"
    override = p2_python if role == "task" else p1_python
    return (
        Path(override)
        if override
        else (ROOT / component / ".venv" / ("Scripts/python.exe" if windows else "bin/python"))
    )


def run(args):
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    directory = Path(args.data_dir).resolve()
    roles = select_roles(args.role, args.roles, args.with_node)
    if not args.node_name or any(
        c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
        for c in args.node_name
    ):
        raise ValueError("节点名仅可含 ASCII 字母、数字、下划线、短横线")
    # Validate every role before starting anything. --plan never opens a port or starts a service.
    plan = []
    for role in roles:
        executable = interpreter_for(role, args.p1_python, args.p2_python)
        if not executable.is_file():
            raise ValueError(f"解释器不存在：{executable}；先按 README 安装本角色环境")
        environment(
            config, role, directory, args.bind, args.resource_port, args.task_port, args.node_name
        )
        plan.append(
            {
                "role": role,
                "python": str(executable.resolve()),
                "resource_url": config["resource_url"],
                "listen_port": {"resource": args.resource_port, "task": args.task_port}.get(role),
                "node_name": args.node_name if role == "node" else None,
            }
        )
    if args.plan:
        print(json.dumps({"roles": plan, "data_dir": str(directory)}, ensure_ascii=False, indent=2))
        return
    processes = []
    logs = []
    locks = []
    directory.mkdir(parents=True, exist_ok=True)
    try:
        for role in roles:
            lock = (directory / f".{role}.lock").open("a+b")
            locks.append(lock)
            if os.name == "nt":
                import msvcrt

                if lock.tell() == 0:
                    lock.write(b"0")
                    lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            component = "P2" if role == "task" else "P1"
            interpreter = interpreter_for(role, args.p1_python, args.p2_python)
            # Bind check never kills an existing service.
            if role != "node":
                with socket.socket() as probe:
                    probe.bind(
                        (args.bind, args.task_port if role == "task" else args.resource_port)
                    )
            module = {"resource": "resource_service", "task": "task_service", "node": "node_agent"}[
                role
            ]
            argv = [str(interpreter.resolve()), "-m", module]
            if role == "node":
                argv += ["--server", config["resource_url"], "--name", args.node_name]
                if args.interactive:
                    argv += ["--interactive"]
            log = (directory / f"{role}-{args.node_name}.log").open("a", encoding="utf-8")
            logs.append(log)
            processes.append(
                subprocess.Popen(
                    argv,
                    cwd=ROOT / component,
                    env=environment(
                        config,
                        role,
                        directory,
                        args.bind,
                        args.resource_port,
                        args.task_port,
                        args.node_name,
                    ),
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=(os.name != "nt"),
                )
            )
            print(f"{role} PID={processes[-1].pid} 日志={log.name}", flush=True)
        print("已启动；Ctrl+C 停止本次启动的进程。交互节点输入 on/off 控制共享。", flush=True)
        while True:
            for process in processes:
                if process.poll() is not None:
                    raise RuntimeError(f"子进程 {process.pid} 退出 {process.returncode}，查看日志")
            time.sleep(0.5)
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                if os.name == "nt":
                    # Compute workers are grandchildren. Stop only our own process tree.
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                else:
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    if os.name == "nt":
                        process.kill()
                    else:
                        os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
        for log in logs:
            log.close()
        for lock in locks:
            lock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("init")
    create.add_argument("--resource-url", required=True)
    create.add_argument("--task-url", required=True)
    create.add_argument("--out", default=str(ROOT / "P4/var/config"))
    start = commands.add_parser("run")
    selection = start.add_mutually_exclusive_group()
    selection.add_argument("--role", choices=["center", "resource", "task", "node"])
    selection.add_argument("--roles", nargs="+", choices=["resource", "task", "node"])
    start.add_argument("--machine", help="本机角色JSON，路径字段相对项目根目录")
    start.add_argument("--plan", action="store_true", help="只核对配置并打印计划，不启动服务")
    start.add_argument("--config")
    start.add_argument("--data-dir")
    start.add_argument("--bind")
    start.add_argument("--resource-port", type=int)
    start.add_argument("--task-port", type=int)
    start.add_argument("--with-node", action="store_true")
    start.add_argument("--interactive", action="store_true", default=None)
    start.add_argument("--node-name")
    start.add_argument("--p1-python")
    start.add_argument("--p2-python")
    args = parser.parse_args()
    try:
        if args.command == "run":
            configure_run(args)
        (init if args.command == "init" else run)(args)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
