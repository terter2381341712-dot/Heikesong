import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from node_agent import compute
from tests.resource.conftest import TASK
from tests.resource.test_executions import setup_ready, make_execution_request


def long_compute(pipe, server, name, identity, kind, payload, prepared):
    path = Path(payload["marker"])
    for i in range(1000):
        path.write_text(str(i))
        time.sleep(0.02)
    path.with_suffix(".result").write_text("must not appear")


@pytest.mark.parametrize("kind", ["preparation", "execution"])
def test_model_process_really_stops(tmp_path, monkeypatch, kind):
    monkeypatch.setattr(compute, "compute", long_compute)
    marker = tmp_path / "counter"
    ack = []
    agent = SimpleNamespace(
        server="",
        name="",
        worker_id="w",
        session_id="s",
        node_secret="n",
        _prepared=None,
        _sharing_disabled=False,
        _session_stale=False,
        _cancel_ids=set(),
        _ack_cancel=ack.append,
    )

    def disable():
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        agent._sharing_disabled = True

    thread = threading.Thread(target=disable)
    thread.start()
    compute.supervise(agent, kind, {"execution_id": "e", "marker": str(marker)})
    thread.join()
    before = marker.read_text()
    time.sleep(0.15)
    assert marker.read_text() == before
    assert not marker.with_suffix(".result").exists()
    assert ack == (["e"] if kind == "execution" else [])


def test_shutdown_retains_success_and_revokes_running(client_with_profile):
    c, digest, node, artifact = setup_ready(client_with_profile)
    prefix = f"/api/resource/internal/v1/nodes/{node['worker_id']}"
    auth = {k: node[k] for k in ("session_id", "node_secret")}
    result = dict(
        schema_version="1.0",
        task_type="ocr.image.v1",
        input_artifact_id=artifact["artifact_id"],
        image_width=64,
        image_height=32,
        text="saved",
        blocks=[],
    )
    ids = []
    for i in range(2):
        req = make_execution_request(node, artifact["artifact_id"], digest)
        assert c.post("/api/resource/v1/executions", headers=TASK, json=req).status_code == 201
        c.post(prefix + "/claim", json=auth)
        ids.append(req["execution_id"])
        if i == 0:
            saved = c.post(
                prefix + f"/executions/{ids[0]}/complete",
                json={**auth, "outcome": "SUCCEEDED", "result": result},
            ).json()
    for _ in range(2):
        assert c.post(prefix + "/sharing", json={**auth, "enabled": False}).status_code == 200
    assert c.post(prefix + "/sharing", json={**auth, "enabled": True}).status_code == 409
    assert c.post(prefix + "/confirm_idle", json=auth).status_code == 409
    assert (
        c.post(
            prefix + f"/executions/{ids[1]}/complete",
            json={**auth, "outcome": "SUCCEEDED", "result": result},
        ).status_code
        == 409
    )
    assert c.post(prefix + "/claim", json=auth).json()["stop_ack_required"] == [ids[1]]
    cancelled = c.post(prefix + f"/executions/{ids[1]}/cancel_ack", json=auth).json()
    assert cancelled["status"] == "CANCELLED"
    assert cancelled["error"]["code"] == "NODE_UNAVAILABLE"
    assert cancelled["error"]["details"]["execution_id"] == ids[1]
    assert c.get("/api/resource/v1/executions/" + ids[0], headers=TASK).json() == saved
    assert c.post(prefix + "/confirm_idle", json=auth).status_code == 200
    assert c.post(prefix + "/sharing", json={**auth, "enabled": True}).status_code == 200
    assert (
        c.post(
            prefix + "/sharing", json={**auth, "node_secret": "wrong-secret", "enabled": False}
        ).status_code
        == 409
    )


def test_preparation_shutdown_cannot_publish_ready(client_with_profile):
    import uuid
    from tests.resource.conftest import register_node

    c, digest = client_with_profile
    node = register_node(c)
    auth = {k: node[k] for k in ('session_id', 'node_secret')}
    prefix = f"/api/resource/internal/v1/nodes/{node['worker_id']}"
    pid = str(uuid.uuid4())
    request = dict(preparation_id=pid, worker_id=node['worker_id'],
                   worker_session_id=node['session_id'], task_type='ocr.image.v1',
                   profile_id='ocr.default.v1', profile_digest=digest)
    assert c.post('/api/resource/v1/preparations',headers=TASK,json=request).status_code==202
    assert c.post(prefix+'/claim',json=auth).json()['preparation']['preparation_id']==pid
    assert c.post(prefix+'/sharing',json={**auth,'enabled':False}).status_code==200
    assert c.post(prefix+f'/preparations/{pid}/status',json={**auth,'status':'READY'}).status_code==409
    record=c.get('/api/resource/v1/preparations/'+pid,headers=TASK).json()
    assert record['status']=='FAILED' and record['error']['code']=='NODE_UNAVAILABLE'
    assert c.post(prefix+'/sharing',json={**auth,'enabled':True}).status_code==409
    assert c.post(prefix+'/confirm_idle',json=auth).status_code==200
    assert c.post(prefix+'/sharing',json={**auth,'enabled':False}).json()['cleanup_pending'] is False
    assert c.post(prefix+'/sharing',json={**auth,'enabled':True}).status_code==200
