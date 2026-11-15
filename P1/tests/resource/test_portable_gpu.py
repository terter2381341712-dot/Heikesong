import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from node_agent import accelerators
from node_agent.windows_gpu import aggregate
from resource_service.backends import PROVIDERS
from resource_service.models import CapabilityModel
from runners.rapidocr_runner import RapidOCRRunner
from tests.resource.test_gpu_resident import install_fake_rapidocr


def test_windows_all_gpu_types_and_cuda_ordinals(monkeypatch):
    adapters = [{"backend": "directml", "device_id": 0, "name": "Intel", "vendor_id": 0x8086},
                {"backend": "directml", "device_id": 2, "name": "NVIDIA RTX", "vendor_id": 0x10de}]
    monkeypatch.setattr(accelerators.platform, "system", lambda: "Windows")
    monkeypatch.setattr(accelerators, "windows_adapters", lambda: adapters)
    monkeypatch.setattr(accelerators.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="0, NVIDIA RTX\n"))
    found = accelerators.discover()
    assert found[0]["backend"] == "directml"
    assert found[1]["backend"] == "cuda" and found[1]["device_id"] == 0


def test_pdh_sums_processes_per_engine_without_summing_overlapping_engines():
    prefix = "luid_0x00000000_0x00000012_phys_0_eng_"
    values = [("pid_1_" + prefix + "0_engtype_3D", 20),
              ("pid_2_" + prefix + "0_engtype_3D", 30),
              ("pid_1_" + prefix + "1_engtype_Copy", 40),
              ("pid_3_luid_0x0_0x99_phys_0_eng_0_engtype_3D", 99)]
    assert aggregate(values, "00000000_00000012") == 50
    assert aggregate(values, "00000000_00000022") is None


@pytest.mark.parametrize("backend", ["directml", "coreml"])
def test_new_gpu_provider_must_have_executed_operators(tmp_path, backend):
    runner = RapidOCRRunner(backend=backend)
    traces = []
    for index in range(3):
        path = tmp_path / f"trace{index}.json"
        path.write_text(json.dumps([{"cat": "Node", "args": {"provider": "CPUExecutionProvider"}}]))
        traces.append(SimpleNamespace(session=SimpleNamespace(session=SimpleNamespace(end_profiling=lambda path=path: str(path)))))
    runner._engine = SimpleNamespace(text_det=traces[0], text_cls=traces[1], text_rec=traces[2])
    with pytest.raises(ValueError):
        runner.verify_acceleration()
    path = tmp_path / "trace0.json"
    path.write_text(json.dumps([{"cat": "Node", "args": {"provider": PROVIDERS[backend]}}]))
    runner.verify_acceleration()
    assert runner._acceleration_checked


@pytest.mark.parametrize("backend", ["directml", "coreml"])
def test_each_model_uses_explicit_gpu_session_and_directml_uses_selected_adapter(tmp_path, monkeypatch, backend):
    import onnxruntime as ort
    provider = PROVIDERS[backend]
    seen = install_fake_rapidocr(monkeypatch, [provider], actual=provider)
    captured = []
    class Session:
        def get_providers(self): return [provider, "CPUExecutionProvider"]
        def disable_fallback(self): pass
        def get_provider_options(self): return {provider: {"device_id": "2"}}
    def session(path, sess_options, providers):
        captured.append((sess_options, providers))
        return Session()
    monkeypatch.setattr(ort, "InferenceSession", session)
    monkeypatch.setenv("NODE_AGENT_DATA_DIR", str(tmp_path))
    runner = RapidOCRRunner(backend=backend, device_id=2)
    assert runner.runtime_provider() == provider and len(captured) == 3
    assert not seen["params"]["EngineConfig.onnxruntime.use_cuda"]
    if backend == "directml":
        assert all(not options.enable_mem_pattern and options.execution_mode == ort.ExecutionMode.ORT_SEQUENTIAL for options, _ in captured)
        assert all(providers[0][1]["device_id"] == 2 for _, providers in captured)
    else:
        assert all(providers[0][1]["flags"] == "COREML_FLAG_CREATE_MLPROGRAM" for _, providers in captured)
