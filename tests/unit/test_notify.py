"""Unit tests for notify.py (design §8): publish posts title + deep link only to
the configured topic, runs off-loop with the configured timeout, and raises
NotifyError on HTTP error / timeout / unreachable server — exercised against a
local fake HTTP server and a monkeypatched urlopen, never the real ntfy.sh.

tests/conftest.py is frozen (design §9): all extra fixtures live here.
"""

from __future__ import annotations

import ast
import base64
import socket
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

import sf_factory.notify
from sf_factory.config import FactoryConfig
from sf_factory.models import NotifyError
from sf_factory.notify import NtfyPublisher, dashboard_link

# --------------------------------------------------------------- local fixtures


class _RecordingHandler(BaseHTTPRequestHandler):
    """Records every request on the server; behavior driven by server attrs."""

    def do_POST(self) -> None:  # noqa: N802 — http.server contract
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        server: Any = self.server
        server.requests.append(
            {
                "path": self.path,
                "headers": {k: v for k, v in self.headers.items()},
                "body": body,
            }
        )
        if server.sleep_s:
            time.sleep(server.sleep_s)
        try:
            if server.status == 200:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")
            else:
                self.send_error(server.status, "scripted failure")
        except OSError:
            pass  # client gave up (timeout test) — the server must not crash

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """Silence request logging."""


@pytest.fixture()
def ntfy_server():
    """Factory for local fake ntfy servers: make(status=..., sleep_s=...)."""
    servers: list[ThreadingHTTPServer] = []

    def make(*, status: int = 200, sleep_s: float = 0.0) -> ThreadingHTTPServer:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _RecordingHandler)
        server.requests = []  # type: ignore[attr-defined]
        server.status = status  # type: ignore[attr-defined]
        server.sleep_s = sleep_s  # type: ignore[attr-defined]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.shutdown()
        server.server_close()


def _config_for(
    config_dict: dict[str, Any], server: ThreadingHTTPServer, *, timeout_s: float = 2.0
) -> FactoryConfig:
    """FactoryConfig pointing founder_channel.ntfy at the fake server."""
    port = server.server_address[1]
    config_dict["founder_channel"]["ntfy"]["server"] = f"http://127.0.0.1:{port}"
    config_dict["founder_channel"]["ntfy"]["timeout_s"] = timeout_s
    return FactoryConfig.model_validate(config_dict)


class _FakeResponse:
    """Minimal context-manager stand-in for urlopen's response."""

    def read(self) -> bytes:
        return b"{}"

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


# ------------------------------------------------------------------ happy path


async def test_publish_posts_title_and_link_to_topic(config_dict, ntfy_server) -> None:
    server = ntfy_server()
    cfg = _config_for(config_dict, server)
    link = "http://server-e9:8377/#stage/foo"

    await NtfyPublisher(cfg).publish("Decision needed", link=link, priority="high")

    assert len(server.requests) == 1
    request = server.requests[0]
    assert request["path"] == "/sf-f5-test-topic"  # topic from config, URL-encoded
    assert request["headers"]["Title"] == "Decision needed"
    assert request["headers"]["Priority"] == "high"
    assert request["headers"]["Click"] == link
    # D-0004: payload is title + deep link only — the body is exactly the link.
    assert request["body"] == link.encode()


async def test_publish_without_link_sends_no_click_and_empty_body(
    config_dict, ntfy_server
) -> None:
    server = ntfy_server()
    cfg = _config_for(config_dict, server)

    await NtfyPublisher(cfg).publish("title only")

    request = server.requests[0]
    assert "Click" not in request["headers"]
    assert request["body"] == b""
    assert request["headers"]["Priority"] == "default"


async def test_publish_encodes_romanian_title_as_rfc2047(config_dict, ntfy_server) -> None:
    server = ntfy_server()
    cfg = _config_for(config_dict, server)
    title = "Decizie: fundația e gata"  # founder-facing Romanian (protocol §6)

    await NtfyPublisher(cfg).publish(title, link="http://h:1/#d/1")

    header = server.requests[0]["headers"]["Title"]
    assert header.startswith("=?UTF-8?B?") and header.endswith("?=")
    decoded = base64.b64decode(header[len("=?UTF-8?B?") : -len("?=")]).decode("utf-8")
    assert decoded == title


async def test_publish_collapses_newlines_in_title(config_dict, ntfy_server) -> None:
    """CR/LF in a title must never reach the header line (header injection)."""
    server = ntfy_server()
    cfg = _config_for(config_dict, server)

    await NtfyPublisher(cfg).publish("stage done\r\nX-Injected: yes")

    assert server.requests[0]["headers"]["Title"] == "stage done X-Injected: yes"


async def test_publish_percent_encodes_non_ascii_link(config_dict, ntfy_server) -> None:
    """The Click header must stay a literal ASCII URL, not an RFC 2047 token."""
    server = ntfy_server()
    cfg = _config_for(config_dict, server)

    await NtfyPublisher(cfg).publish("t", link="http://h:1/#decizie/așteptare")

    click = server.requests[0]["headers"]["Click"]
    assert click.isascii()
    assert click == "http://h:1/#decizie/a%C8%99teptare"


# ---------------------------------------------------------------- failure paths


async def test_publish_raises_notify_error_on_http_error(config_dict, ntfy_server) -> None:
    server = ntfy_server(status=500)
    cfg = _config_for(config_dict, server)

    with pytest.raises(NotifyError, match="500"):
        await NtfyPublisher(cfg).publish("t", link="http://h:1/#x")


async def test_publish_raises_notify_error_on_timeout(config_dict, ntfy_server) -> None:
    server = ntfy_server(sleep_s=1.0)  # responds well past the client timeout
    cfg = _config_for(config_dict, server, timeout_s=0.2)

    with pytest.raises(NotifyError):
        await NtfyPublisher(cfg).publish("t", link="http://h:1/#x")


async def test_publish_raises_notify_error_when_unreachable(factory_config) -> None:
    # conftest config points ntfy at http://127.0.0.1:1 — connection refused.
    with pytest.raises(NotifyError):
        await NtfyPublisher(factory_config).publish("t", link="http://h:1/#x")


# ------------------------------------------------- off-loop + timeout plumbing


async def test_publish_runs_blocking_call_off_the_loop_thread(
    factory_config, monkeypatch
) -> None:
    """§7: the urlopen call must run via asyncio.to_thread, never on the loop."""
    seen: dict[str, object] = {}

    def fake_urlopen(request: urllib.request.Request, timeout: float | None = None):
        seen["thread"] = threading.get_ident()
        seen["timeout"] = timeout
        seen["url"] = request.full_url
        seen["method"] = request.get_method()
        return _FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    loop_thread = threading.get_ident()

    await NtfyPublisher(factory_config).publish("t", link="http://h:1/#x")

    assert seen["thread"] != loop_thread
    assert seen["method"] == "POST"
    assert seen["url"] == "http://127.0.0.1:1/sf-f5-test-topic"
    # founder_channel.ntfy.timeout_s travels into urlopen by name, not hardcoded.
    assert seen["timeout"] == pytest.approx(
        factory_config.founder_channel.ntfy.timeout_s
    )


# -------------------------------------------------------------- dashboard_link


def test_dashboard_link_uses_hostname_and_config_port(factory_config) -> None:
    link = dashboard_link(factory_config, "stage/foo")
    port = factory_config.founder_channel.dashboard.port
    assert link == f"http://{socket.gethostname()}:{port}/#stage/foo"


def test_dashboard_link_quotes_fragment_to_ascii(factory_config) -> None:
    link = dashboard_link(factory_config, "decizie nr 1/ășt")
    assert link.isascii()
    assert link.endswith("/#decizie%20nr%201/%C4%83%C8%99t")


# ------------------------------------------------------------------- structure


def test_notify_imports_only_models_and_config() -> None:
    """Design §1 import DAG: notify = models + config, never db/statemachine/..."""
    tree = ast.parse(Path(sf_factory.notify.__file__).read_text(encoding="utf-8"))
    sf_imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module is not None
        and node.module.startswith("sf_factory")
    }
    assert sf_imports <= {"sf_factory.models", "sf_factory.config"}


def test_publisher_binds_config_priorities(factory_config) -> None:
    publisher = NtfyPublisher(factory_config)
    assert publisher.priority_decision == factory_config.founder_channel.ntfy.priority_decision
    assert publisher.priority_alert == factory_config.founder_channel.ntfy.priority_alert
