from types import SimpleNamespace

from core.http_api_client.harbor_client import HarborClient


def _response(status_code: int, text: str = "") -> SimpleNamespace:
    return SimpleNamespace(status_code=status_code, text=text)


def test_create_project_skips_creation_when_project_exists(monkeypatch):
    get_calls = []

    def fake_get(url, **kwargs):
        get_calls.append((url, kwargs))
        return _response(200)

    def unexpected_post(*args, **kwargs):
        raise AssertionError("existing projects must not be created again")

    monkeypatch.setattr("requests.get", fake_get)
    monkeypatch.setattr("requests.post", unexpected_post)

    client = HarborClient(base_url="https://harbor.example.test")
    assert client.create_project("docker.io") is True
    assert get_calls[0][0].endswith("/api/v2.0/projects/docker.io")
    assert get_calls[0][1]["headers"] == {"X-Is-Resource-Name": "true"}


def test_create_project_creates_missing_project(monkeypatch):
    post_calls = []

    monkeypatch.setattr("requests.get", lambda *args, **kwargs: _response(404))

    def fake_post(url, **kwargs):
        post_calls.append((url, kwargs))
        return _response(201)

    monkeypatch.setattr("requests.post", fake_post)

    client = HarborClient(base_url="https://harbor.example.test")
    assert client.create_project("docker.io", public=True) is True
    assert post_calls[0][1]["json"] == {
        "project_name": "docker.io",
        "public": True,
        "storage_limit": -1,
    }


def test_create_project_treats_concurrent_creation_as_success(monkeypatch):
    monkeypatch.setattr("requests.get", lambda *args, **kwargs: _response(404))
    monkeypatch.setattr(
        "requests.post", lambda *args, **kwargs: _response(409, "conflict")
    )

    client = HarborClient(base_url="https://harbor.example.test")
    assert client.create_project("docker.io") is True


def test_create_project_falls_back_to_creation_when_lookup_fails(monkeypatch):
    monkeypatch.setattr("requests.get", lambda *args, **kwargs: _response(403))
    monkeypatch.setattr(
        "requests.post", lambda *args, **kwargs: _response(201)
    )

    client = HarborClient(base_url="https://harbor.example.test")
    assert client.create_project("docker.io") is True
