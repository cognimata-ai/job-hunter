import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from jobhunter import cli
from jobhunter.cli import main


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path.startswith("/moved"):  # /moved-to/healthz redirects to /healthz
            self.send_response(302)
            self.send_header("Location", self.path.removeprefix("/moved-to"))
        else:
            self.send_response(204 if self.path == "/healthz" else 503)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:  # quiet test output
        pass


@pytest.fixture
def server_url() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_exit_status_follows_the_http_status(server_url, capsys) -> None:
    assert main(["http-check", f"{server_url}/healthz"]) == 0
    assert capsys.readouterr().err == ""
    assert main(["http-check", f"{server_url}/readyz"]) == 1
    assert capsys.readouterr().err == "http-check: HTTPError\n"


def test_redirects_are_followed_to_the_final_status(server_url, capsys) -> None:
    assert main(["http-check", f"{server_url}/moved-to/healthz"]) == 0
    assert main(["http-check", f"{server_url}/moved-to/readyz"]) == 1
    assert capsys.readouterr().err == "http-check: HTTPError\n"


def test_an_unreachable_server_names_the_error_class_only(capsys) -> None:
    with socket.socket() as probe:  # a port with no listener
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    assert main(["http-check", f"http://127.0.0.1:{port}/healthz"]) == 1
    assert capsys.readouterr().err == "http-check: URLError\n"


def test_a_silent_server_times_out(monkeypatch, capsys) -> None:
    assert cli.HTTP_CHECK_TIMEOUT_S == 3
    monkeypatch.setattr(cli, "HTTP_CHECK_TIMEOUT_S", 0.2)
    with socket.socket() as silent:  # accepts connections (the kernel's backlog) but never answers
        silent.bind(("127.0.0.1", 0))
        silent.listen(1)
        assert main(["http-check", f"http://127.0.0.1:{silent.getsockname()[1]}/healthz"]) == 1
    assert capsys.readouterr().err == "http-check: TimeoutError\n"


def test_proxy_variables_are_ignored(server_url, monkeypatch) -> None:
    for name in ("http_proxy", "HTTP_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")  # the discard port: a proxied probe would fail
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    assert main(["http-check", f"{server_url}/healthz"]) == 0


def test_only_http_urls_are_probed(tmp_path, capsys) -> None:
    local = tmp_path / "healthz"
    local.write_text("ok")
    assert main(["http-check", local.as_uri()]) == 1
    assert capsys.readouterr().err == "http-check: ValueError\n"


def test_the_probe_loads_neither_the_api_nor_the_database_stack() -> None:
    code = (
        "import sys\n"
        "from jobhunter.cli import main\n"
        "assert main(['http-check', 'http://127.0.0.1:1/healthz']) == 1\n"
        # neither LangSmith nor langchain either: `main` leaves the probe out of `disable_tracing` for that
        # (`UNTRACEABLE_PROBES`, M5 Task 6, Ruling T6-9)
        "heavy = ('fastapi', 'sqlalchemy', 'jobhunter.api', 'procrastinate', 'langsmith', 'langchain_core')\n"
        "loaded = [m for m in heavy if m in sys.modules]\n"
        "assert not loaded, loaded\n"
    )
    done = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    assert done.stderr == "http-check: URLError\n"
