"""Multi-interface discovery must advertise the service address, not the reply route."""

import json
import socket
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import control  # noqa: E402


def reply(**changes):
    return dict(
        protocol=control.PROTOCOL,
        nonce="request-nonce",
        name="center",
        center_id="remote-center",
        address="10.4.142.66",
        port=8003,
        **changes,
    )


def test_advertised_address_overrides_virtual_adapter_reply():
    assert control.discovered_center(reply(), ("169.254.69.129", 37043), "request-nonce") == {
        "address": "10.4.142.66:8003",
        "name": "center",
    }
    legacy = reply()
    del legacy["address"]
    assert control.discovered_center(legacy, ("169.254.69.129", 37043), "request-nonce") is None
    assert control.discovered_center(legacy, ("10.4.142.66", 37043), "request-nonce")


@pytest.mark.parametrize(
    "changes",
    [
        {"address": "169.254.69.129"},
        {"address": "127.0.0.1"},
        {"address": "0.0.0.0"},
        {"address": "bad-address"},
        {"address": None},
        {"protocol": "wrong"},
        {"nonce": "stale-request"},
        {"port": 0},
        {"port": 65536},
        {"port": None},
    ],
)
def test_invalid_or_unusable_reply_rejected(changes):
    body = reply()
    body.update(changes)
    assert control.discovered_center(body, ("10.4.142.66", 37043), "request-nonce") is None


def test_self_exclusion_uses_identity_with_legacy_name_fallback():
    peer = ("10.4.142.66", 37043)
    assert control.discovered_center(reply(), peer, "request-nonce", "remote-center") is None
    # Two devices can have the same name; a different identity is still discoverable.
    assert control.discovered_center(reply(), peer, "request-nonce", "local-center", "center")
    legacy = reply()
    del legacy["center_id"]
    assert (
        control.discovered_center(legacy, peer, "request-nonce", "local-center", "center") is None
    )


def test_each_interface_probed_and_duplicate_replies_merged(monkeypatch):
    clients = []

    class Client:
        def __init__(self, *args):
            self.pending = []
            self.closed = False
            clients.append(self)

        def setsockopt(self, *args):
            pass

        def bind(self, address):
            self.bound = address

        def sendto(self, message, destination):
            self.destination = destination
            body = reply()
            body["nonce"] = json.loads(message)["nonce"]
            self.pending.append((json.dumps(body).encode(), ("169.254.69.129", 37043)))

        def recvfrom(self, size):
            return self.pending.pop()

        def close(self):
            self.closed = True

    def ready(readers, writers, errors, timeout):
        available = [client for client in readers if client.pending]
        if not available:
            time.sleep(timeout)
        return available, [], []

    monkeypatch.setattr(
        control, "lan_addresses", lambda: ["10.1.0.8", "192.168.4.5", "169.254.1.1", "10.1.0.8"]
    )
    monkeypatch.setattr(control.socket, "socket", Client)
    monkeypatch.setattr(control.select, "select", ready)
    assert control.discover(timeout=0.01) == [{"address": "10.4.142.66:8003", "name": "center"}]
    assert [client.bound for client in clients] == [
        ("10.1.0.8", 0),
        ("192.168.4.5", 0),
        ("127.0.0.1", 0),
    ]
    assert [client.destination[0] for client in clients] == [
        "255.255.255.255",
        "255.255.255.255",
        "127.0.0.1",
    ]
    assert all(client.closed for client in clients)


def test_real_udp_server_advertises_selected_address_and_excludes_self(monkeypatch):
    # An ephemeral port avoids touching a user's running center on port 37043.
    ready = threading.Event()
    socket_type = socket.socket

    class ReadySocket(socket_type):
        def bind(self, address):
            super().bind(address)
            if address == ("0.0.0.0", 0):
                control.DISCOVERY_PORT = self.getsockname()[1]
                ready.set()

    monkeypatch.setattr(control, "DISCOVERY_PORT", 0)
    monkeypatch.setattr(control.socket, "socket", ReadySocket)
    monkeypatch.setattr(control, "lan_addresses", lambda: [])
    panel = SimpleNamespace(
        mode="center",
        address="10.4.142.66",
        identity="this-center",
        name="test-center",
        port=18003,
        closed=threading.Event(),
    )
    thread = threading.Thread(target=control.discovery_server, args=(panel,), daemon=True)
    thread.start()
    try:
        assert ready.wait(3)
        assert control.discover(timeout=0.1) == [
            {"address": "10.4.142.66:18003", "name": "test-center"}
        ]
        assert control.discover(timeout=0.1, exclude_id=panel.identity) == []
    finally:
        panel.closed.set()
        thread.join(3)
    assert not thread.is_alive()
