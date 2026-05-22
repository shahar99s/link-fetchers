import os

import httpx
from httporchestrator import Flow, Response

from link_fetchers.base_fetcher import BaseFetcher
from link_fetchers.utils import Mode


class DummyFetcher(BaseFetcher):
    NAME = "Dummy"
    BASE_URL = ""
    steps = []

    def build_marker(self):
        return "marker-value"


def test_runner_preserves_fetcher_methods_across_run():
    fetcher = DummyFetcher()

    assert fetcher.build_marker() == "marker-value"
    run = fetcher.run()

    assert fetcher.build_marker() == "marker-value"
    assert run.summary.name == "Dummy"


def test_fetcher_instances_keep_independent_variable_state():
    first = DummyFetcher().variables({"shared_var": "first"}).export(["shared_var"])
    second = DummyFetcher()

    first_run = first.run()
    second_run = second.run()

    assert first_run.exported["shared_var"] == "first"
    assert "shared_var" not in second_run.session_variables


def test_fetcher_base_uses_composition_not_workflow_inheritance():
    assert not issubclass(BaseFetcher, Flow)


def test_fetcher_base_consumes_shared_constructor_kwargs(tmp_path):
    fetcher = DummyFetcher(
        headers={"User-Agent": "test-agent"},
        mode=Mode.INFO,
        log_details=True,
        save_path=tmp_path,
    )

    assert fetcher.headers == {"User-Agent": "test-agent"}
    assert fetcher.mode is Mode.INFO
    assert fetcher.flow.log_details is True
    assert fetcher.save_path == os.path.abspath(tmp_path)


def test_fetcher_base_can_enable_impersonation(monkeypatch):
    created_clients = []

    class FakeCurlClient:
        def __init__(self, *, impersonate):
            self.impersonate = impersonate
            self.closed = False
            created_clients.append(self)

        def close(self):
            self.closed = True

    monkeypatch.setattr(
        "link_fetchers.base_fetcher.CurlImpersonatingClient", FakeCurlClient
    )

    fetcher = DummyFetcher(impersonate="firefox120")
    run = fetcher.run()

    assert run.summary.name == "Dummy"
    assert len(created_clients) == 1
    assert created_clients[0].impersonate == "firefox120"
    assert created_clients[0].closed is True


def test_fetcher_base_impersonate_kwarg_drives_single_client(monkeypatch):
    created_clients = []

    class FakeCurlClient:
        def __init__(self, *, impersonate):
            self.impersonate = impersonate
            created_clients.append(self)

        def close(self):
            pass

    monkeypatch.setattr(
        "link_fetchers.base_fetcher.CurlImpersonatingClient", FakeCurlClient
    )

    fetcher = DummyFetcher(impersonate="chrome124", headers={"X-Custom": "yes"})
    fetcher.run()

    assert fetcher.impersonate == "chrome124"
    assert fetcher.headers["X-Custom"] == "yes"
    assert len(created_clients) == 1
    assert created_clients[0].impersonate == "chrome124"


def test_fetcher_save_file_uses_configured_save_path(tmp_path):
    save_path = tmp_path / "downloads"
    fetcher = DummyFetcher(save_path=save_path)
    response = Response(
        httpx.Response(
            200,
            content=b"hello",
            headers={"Content-Disposition": 'attachment; filename="../report.txt"'},
            request=httpx.Request("GET", "https://example.com/report"),
        )
    )

    state = fetcher.save_file(response, "fallback.bin")

    expected_path = os.path.join(os.path.abspath(save_path), "report.txt")
    assert state == {"local_file_path": expected_path}
    assert (save_path / "report.txt").read_bytes() == b"hello"
