import pytest

from cv_maker.app import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("CV_TAILOR_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("CV_TAILOR_ALLOW_REMOTE", raising=False)
    return create_app(data_dir=tmp_path, sync_jobs=True).test_client()


SETTINGS = {"LLM_PROVIDER": "ollama", "LLM_MODEL": "llama3", "OLLAMA_HOST": "https://attacker.example.com"}


def test_cross_site_post_cannot_change_settings(client, tmp_path):
    res = client.post("/settings", data=SETTINGS, headers={"Sec-Fetch-Site": "cross-site", "Origin": "https://evil.example.com"})
    assert res.status_code == 403
    assert b"another website" in res.data
    assert not (tmp_path / "settings.json").exists()


def test_other_localhost_port_is_also_cross_origin(client):
    assert client.post("/settings", data=SETTINGS, headers={"Sec-Fetch-Site": "same-site"}).status_code == 403
    assert client.post("/settings", data=SETTINGS, headers={"Origin": "http://localhost:8888"}).status_code == 403
    assert client.post("/settings", data=SETTINGS, headers={"Origin": "null"}).status_code == 403


def test_same_origin_requests_work(client):
    assert client.post("/settings", data=SETTINGS, headers={"Sec-Fetch-Site": "same-origin"}).status_code == 302
    assert client.post("/settings", data=SETTINGS, headers={"Origin": "http://localhost"}).status_code == 302
    assert client.post("/settings", data=SETTINGS).status_code == 302  # no browser metadata (curl, old browsers)


def test_cross_site_navigation_is_still_allowed(client):
    # Following a link from elsewhere is fine; only state-changing requests are blocked.
    assert client.get("/profile", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200


def test_dns_rebinding_host_is_refused(client):
    res = client.get("/profile", headers={"Host": "evil.example.com:5000"})
    assert res.status_code == 403 and b"only answers on localhost" in res.data
    for host in ("localhost:5000", "127.0.0.1:5000", "[::1]:5000", "app.localhost"):
        assert client.get("/profile", headers={"Host": host}).status_code == 200, host


def test_non_loopback_connections_are_refused(client):
    res = client.get("/profile", environ_base={"REMOTE_ADDR": "192.168.1.20"})
    assert res.status_code == 403 and b"this computer" in res.data
    assert client.get("/profile", environ_base={"REMOTE_ADDR": "::ffff:127.0.0.1"}).status_code == 200


def test_escape_hatches_are_opt_in(tmp_path, monkeypatch):
    monkeypatch.setenv("CV_TAILOR_ALLOWED_HOSTS", "cv.internal")
    monkeypatch.setenv("CV_TAILOR_ALLOW_REMOTE", "1")
    client = create_app(data_dir=tmp_path).test_client()
    assert client.get("/profile", headers={"Host": "cv.internal"}, environ_base={"REMOTE_ADDR": "10.0.0.5"}).status_code == 200


def test_privacy_headers(client):
    res = client.get("/settings")
    assert res.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in res.headers["Content-Security-Policy"]
    assert res.headers["Cache-Control"] == "no-store"
    assert res.headers["Referrer-Policy"] == "same-origin"


def test_settings_page_explains_where_data_lives(client, tmp_path):
    page = client.get("/settings").data.decode()
    assert str(tmp_path.resolve()) in page
    assert "Leaves this computer" in page and "Ollama" in page
