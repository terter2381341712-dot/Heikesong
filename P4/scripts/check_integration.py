"""Run local checks and collect explicit G0–G7 evidence; never infer physical acceptance."""

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import compute_config

ROOT = compute_config.ROOT
GATES = {
    "G0": "自动回归与契约", "G1": "启动停止及 Windows", "G2": "双机配对与身份",
    "G3": "权限与两端工作台路由", "G4": "真实浏览器 OCR 和双机贡献",
    "G5": "共享取消迁移", "G6": "断线重启幂等", "G7": "原有演示与交互",
}
STAGES = {
    "S0": "main 历史基线、两人双机、Windows、20 张人工参考冻结及三轮质量/吞吐",
    "S1": "默认 CPU 单槽常驻复用，停止/取消/共享关闭后无遗留进程",
    "S2": "异构 CPU/CUDA 同 Job、真实 provider、错误环境拒绝、GPU 回收",
    "S3": "CPU 多槽重叠、线程预算、单槽取消、重启与旧结果隔离",
    "S4": "校准冷启动、历史恢复、探索公平性、不可用节点拒绝与决策追溯",
    "S5": "真实预取命中、TTL/session/profile 失效、预算与重复结果检查",
    "S6": "Windows CUDA N=1/2/4、显存拒绝、取消/崩溃回收及共享锁串行证据",
    "S7": "main 数据副本同步升级、幂等迁移、重启与代码/环境/数据配套回滚",
}


def source_digest():
    digest = hashlib.sha256()
    for folder in ("P1", "P2", "P4/scripts", "P4/tests", "web"):
        # WindowsPath sorts case-insensitively; use the same POSIX relative key
        # on both teammates' computers before hashing LF-normalized bytes.
        for path in sorted((ROOT / folder).rglob("*"), key=lambda p: p.relative_to(ROOT).as_posix()):
            if any(p.startswith(".") or p in {"var", "node_modules", "__pycache__"} for p in path.relative_to(ROOT).parts):
                continue
            if path.is_file() and path.suffix in {".py", ".json", ".yaml", ".js", ".cjs", ".html", ".toml"}:
                digest.update(path.relative_to(ROOT).as_posix().encode())
                digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return digest.hexdigest()


def validate_attestation(path, stage, digest, config=None):
    body = json.loads(path.read_text(encoding="utf-8"))
    if body.get("stage") != stage or body.get("source_digest") != digest:
        raise ValueError("人工证据阶段或源码摘要不一致，必须对当前版本重新验收")
    if not body.get("operators") or not body.get("physical_devices"):
        raise ValueError("人工证据缺少操作人和物理设备记录")
    if config is not None and body.get("compute") != config:
        raise ValueError("人工证据的解析后计算配置不一致")
    verified = {}
    values = dict(body.get("gates", {}))
    for key, value in body.get("stage_checks", {}).items():
        if key not in list(STAGES)[:int(stage[1:]) + 1]:
            raise ValueError("阶段证据超出本阶段范围")
        values[key] = value
    for gate, value in values.items():
        if gate not in GATES and gate not in STAGES or gate == "G0":
            raise ValueError("人工证据不能替代 G0 自动检查")
        if value.get("status") not in {"PASS", "FAIL", "NOT_RUN"}:
            raise ValueError("人工验收状态无效")
        artifacts = value.get("artifacts", [])
        if value["status"] == "PASS":
            if value.get("operator_attested") is not True or not artifacts:
                raise ValueError("PASS 必须有操作人确认和证据文件")
            for item in artifacts:
                target = (path.parent / item["path"]).resolve()
                target.relative_to(path.parent.resolve())
                if hashlib.sha256(target.read_bytes()).hexdigest() != item["sha256"]:
                    raise ValueError("人工证据文件 SHA 不匹配")
            if gate in {"G2", "G4"} and (len(set(body["physical_devices"])) < 2 or len(set(body["operators"])) < 2):
                raise ValueError("双机验收须确认至少两个不同物理设备")
        verified[gate] = {"status": value["status"], "source": "OPERATOR_ATTESTATION", "artifacts": artifacts}
    return verified


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=[f"S{i}" for i in range(8)], required=True)
    parser.add_argument("--compute-config")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--p1-python", default=str(ROOT / "P1/.venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")))
    parser.add_argument("--p2-python", default=str(ROOT / "P2/.venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")))
    parser.add_argument("--p4-python", default=sys.executable)
    parser.add_argument("--real-http", action="store_true", help="额外运行隔离的本机 CPU/OCR 检查；不代表浏览器或双机验收")
    parser.add_argument("--attestation", type=Path)
    parser.add_argument("--default-report", type=Path, help="启用优化时提供同阶段、同源码的默认配置完整 PASS 报告")
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    report = {"stage": args.stage, "status": "NOT_RUN", "contract_version": compute_config.CONTRACT_VERSION,
              "started_at": datetime.now(timezone.utc).isoformat(), "platform": platform.platform(),
              "gates": {k: {"name": v, "status": "NOT_RUN"} for k, v in GATES.items()}, "checks": []}
    report["stage_checks"] = {k: {"name": v, "status": "NOT_RUN"} for k, v in STAGES.items() if int(k[1:]) <= int(args.stage[1:])}
    try:
        report["compute"] = compute_config.load(args.compute_config)
        report["source_digest"] = source_digest()
        report["commit"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        report["working_tree_changed"] = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT))
        report["profile_digest"] = {p.name: hashlib.sha256(p.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
                                    for p in compute_config.profiles(report["compute"]).glob("*.lock.json")}
        report["interpreters"] = {}
        version_code = "import json,platform,importlib.metadata as m; print(json.dumps({'python':platform.python_version(),'packages':{d.metadata['Name']:d.version for d in m.distributions()}}))"
        for role, executable in (("P1", args.p1_python), ("P2", args.p2_python), ("P4", args.p4_python)):
            report["interpreters"][role] = json.loads(subprocess.check_output([executable, "-c", version_code], text=True, timeout=20))
        jobs = [("P1", ROOT / "P1", [args.p1_python, "-m", "pytest", "-q", "tests/resource"]),
                ("P2", ROOT / "P2", [args.p2_python, "-m", "pytest", "-q", "tests/tasks"]),
                ("P4", ROOT, [args.p4_python, "-m", "pytest", "-q", "P4/tests"]),
                ("launcher", ROOT, [args.p2_python, "-m", "pytest", "-q", "qa/p1-p2/test_run_local.py"]),
                ("control-js", ROOT, ["node", "--test", "P4/tests/control_ui.test.cjs"]),
                ("contract", ROOT, [args.p4_python, "P4/scripts/verify.py", "--static", "--out", str(out / "static")])]
        for name, cwd, argv in jobs:
            if name in {"P1", "P2", "P4"}:
                argv.append("--junitxml=" + str(out / (name + ".xml")))
            with (out / (name + ".log")).open("w") as log:
                result = subprocess.run(argv, cwd=cwd, stdout=log, stderr=subprocess.STDOUT,
                                        env={**os.environ, "PYTHONPATH": str(cwd)}, timeout=300)
            report["checks"].append({"name": name, "exit_code": result.returncode, "log": name + ".log"})
        report["gates"]["G0"]["status"] = "PASS" if all(c["exit_code"] == 0 for c in report["checks"]) else "FAIL"
        if args.real_http:
            # The legacy test helper selects ROOT/P1 and ROOT/P2 venvs itself.
            # Keep all private credentials under ignored var, not in shared evidence.
            private = ROOT / "P4/var/integration" / out.name
            private.mkdir(parents=True, exist_ok=False)
            report["local_http_configuration"] = compute_config.DEFAULTS
            with (out / "real-http.log").open("w") as log:
                result = subprocess.run([args.p2_python, "qa/p1-p2/run_local.py", "check", "--data-dir", str(private)],
                                        cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, timeout=300)
            report["local_http"] = "PASS" if result.returncode == 0 else "FAIL"
            if (private / "report.json").exists():
                (out / "real-http-report.json").write_bytes((private / "report.json").read_bytes())
        if args.attestation:
            for key, value in validate_attestation(args.attestation, args.stage, report["source_digest"], report["compute"]).items():
                report["gates" if key in GATES else "stage_checks"][key].update(value)
        statuses = [g["status"] for g in (*report["gates"].values(), *report["stage_checks"].values())]
        if report["compute"] != compute_config.DEFAULTS:
            baseline = json.loads(args.default_report.read_text()) if args.default_report else {}
            passed = all(baseline.get(k) == v for k,v in {"stage": args.stage, "source_digest": report["source_digest"], "compute": compute_config.DEFAULTS, "status": "PASS"}.items())
            report["default_configuration_regression"] = "PASS" if passed else "NOT_RUN"
            statuses.append(report["default_configuration_regression"])
        report["status"] = "FAIL" if "FAIL" in statuses or report.get("local_http") == "FAIL" else (
            "PASS" if all(s == "PASS" for s in statuses) else "NOT_RUN")
    except Exception as exc:
        report.update(status="FAIL", error_type=type(exc).__name__)
        # Do not serialize arbitrary exception bodies that might contain credentials.
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(report["status"], out / "report.json")
    return {"PASS": 0, "FAIL": 1, "NOT_RUN": 2}[report["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
