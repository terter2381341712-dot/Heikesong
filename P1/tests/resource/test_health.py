"""healthz、鉴权与静态目录的基本行为。"""
from tests.resource.conftest import DEMO, TASK, png_bytes, register_node, upload_png


def test_healthz(client):
    resp = client.get("/api/resource/v1/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["service"] == "p1_resource"
    assert body["contract_version"] == "0.3.0"
    assert body["status"] == "ok"
    assert body["server_time"].endswith("Z")


def test_resources_requires_token(client):
    assert client.get("/api/resource/v1/resources").status_code == 401
    assert client.get("/api/resource/v1/resources", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_resources_with_demo_token(client):
    register_node(client)
    resp = client.get("/api/resource/v1/resources", headers=DEMO)
    assert resp.status_code == 200
    body = resp.json()
    assert body["contract_version"] == "0.3.0"
    assert len(body["resources"]) == 1
    node = body["resources"][0]
    assert node["connection"] == "ONLINE"
    assert node["mode"] == "ACTIVE"
    assert node["max_slots"] == 1
    assert node["available_slots"] == 1
    assert node["session_id"]
    assert node["hardware"]["cpu_name"] == "Apple M4"


def test_task_types_unconfigured(client):
    for headers in (DEMO, TASK):
        resp = client.get("/api/resource/v1/task-types", headers=headers)
        assert resp.status_code == 200
        types = resp.json()["task_types"]
        assert len(types) == 2
        assert all(t["configuration_state"] == "UNCONFIGURED" for t in types)
        assert all(t["profile_digest"] is None for t in types)
