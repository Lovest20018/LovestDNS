import sqlite3
import time

import pytest
import server
from browser_auth import COOKIE, database, reset_password
from test_integrated_api import client, AUTH

PASSWORD = "local-test-password-123"
NEXT_PASSWORD = "changed-test-password-456"


def provision(client):
    api, _, state = client
    reset_password(state, PASSWORD)
    result = api.post("/auth/login", json={"password": PASSWORD, "remember": True})
    assert result.status_code == 200
    return result.get_json()["csrf_token"]


def test_initial_password_is_private_and_must_be_changed(client):
    api, _, state = client
    result = api.get("/auth/session")
    assert result.status_code == 200
    assert result.headers["Cache-Control"] == "no-store"
    assert result.get_json()["authenticated"] is False
    path = state / "admin-password.initial"
    initial = path.read_text().strip()
    assert len(initial) >= 24 and initial not in result.get_data(as_text=True)
    assert api.get("/auth/session").get_json()["initial_password_file"] == str(path)
    assert path.read_text().strip() == initial
    response = api.post("/auth/login", json={"password": initial, "remember": True})
    assert response.get_json()["must_change_password"]
    csrf = response.get_json()["csrf_token"]
    assert api.get("/api/v1/capabilities").status_code == 403
    assert api.get("/api/v1/capabilities", headers=AUTH).status_code == 200
    result = api.post("/auth/password", headers={"X-CSRF-Token": csrf},
                      json={"current_password": initial, "new_password": PASSWORD})
    assert result.status_code == 200 and not result.get_json()["must_change_password"]
    assert not path.exists()
    assert api.get("/api/v1/capabilities").status_code == 200


def test_session_reads_and_csrf_protected_writes_share_api_handlers(client):
    api, data, _ = client
    csrf = provision(client)
    assert api.get("/api/v1/capabilities").get_json()["data"]["access_role"] == "admin"
    content = {"content": 'CNAUTO="yes"\n'}
    assert api.put("/api/v1/file/custom_env.ini", json=content).status_code == 403
    assert api.put("/api/v1/file/custom_env.ini", json=content, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert not (data / "custom_env.ini").exists()
    result = api.put("/api/v1/file/custom_env.ini", json=content, headers={"X-CSRF-Token": csrf})
    assert result.status_code == 200
    assert (data / "custom_env.ini").read_text() == content["content"]
    assert len(api.get("/api/v1/history/custom_env.ini").get_json()["data"]["revisions"]) == 1
    # Existing credentials keep their explicit role, even with an admin cookie.
    assert api.get("/api/v1/capabilities", headers={"Authorization": "Bearer invalid"}).status_code == 401


def test_observer_and_admin_tokens_stay_independent_of_browser_sessions(client, monkeypatch):
    monkeypatch.setenv("SUITE_READ_TOKEN", "independent-observer")
    api = client[0]
    provision(client)
    observer = {"Authorization": "Bearer independent-observer"}
    assert api.get("/api/v1/capabilities", headers=observer).get_json()["data"]["access_role"] == "observer"
    assert api.get("/api/v1/env", headers=observer).status_code == 403
    api.delete_cookie(COOKIE)
    assert api.get("/api/v1/capabilities", headers=AUTH).status_code == 200
    assert api.get("/api/v1/capabilities", headers=observer).status_code == 200
    assert api.get("/api/v1/capabilities").status_code == 401


def test_cookie_is_http_only_remembered_and_revoked_on_logout(client):
    api = client[0]
    csrf = provision(client)
    cookie = api.get_cookie(COOKIE)
    assert cookie.http_only and cookie.same_site == "Strict" and cookie.max_age == 7 * 86400
    value = cookie.value
    assert api.post("/auth/logout", json={}).status_code == 403
    assert api.post("/auth/logout", json={}, headers={"X-CSRF-Token": csrf}).status_code == 200
    assert api.get_cookie(COOKIE) is None
    api.set_cookie(COOKIE, value)
    assert api.get("/api/v1/capabilities").status_code == 401


def test_browser_session_persists_for_another_client_and_expires(client):
    api, _, state = client
    provision(client)
    cookie = api.get_cookie(COOKIE).value
    with server.app.test_client() as other:
        other.set_cookie(COOKIE, cookie)
        assert other.get("/auth/session").get_json()["authenticated"]
        assert other.get("/api/v1/capabilities").status_code == 200
    with database(state) as db:
        db.execute("UPDATE sessions SET expires=?", (time.time() - 1,))
    assert not api.get("/auth/session").get_json()["authenticated"]
    assert api.get("/api/v1/capabilities").status_code == 401


def test_password_change_rotates_cookie_and_revokes_other_sessions(client):
    api, _, state = client
    csrf = provision(client)
    original = api.get_cookie(COOKIE).value
    result = api.post("/auth/password", json={"current_password": PASSWORD, "new_password": NEXT_PASSWORD},
                      headers={"X-CSRF-Token": csrf})
    assert result.status_code == 200
    assert api.get_cookie(COOKIE).value != original
    with server.app.test_client() as other:
        other.set_cookie(COOKIE, original)
        assert other.get("/api/v1/capabilities").status_code == 401
        assert other.post("/auth/login", json={"password": PASSWORD}).status_code == 401
        assert other.post("/auth/login", json={"password": NEXT_PASSWORD}).status_code == 200
        assert other.get_cookie(COOKIE).max_age is None
    with sqlite3.connect(state / "browser-auth.sqlite") as db:
        assert PASSWORD not in db.execute("SELECT password_hash FROM admin").fetchone()[0]


@pytest.mark.parametrize("headers", [{"Origin": "https://foreign.example"}, {"Sec-Fetch-Site": "cross-site"}])
def test_login_and_authenticated_writes_reject_other_origins(client, headers):
    api = client[0]
    csrf = provision(client)
    assert api.post("/auth/login", headers=headers, json={"password": PASSWORD}).status_code == 403
    assert api.post("/auth/password", headers={**headers, "X-CSRF-Token": csrf},
                    json={"current_password": PASSWORD, "new_password": NEXT_PASSWORD}).status_code == 403
    assert api.put("/api/v1/file/custom_env.ini", headers={**headers, "X-CSRF-Token": csrf},
                   json={"content": 'CNAUTO="no"\n'}).status_code == 403
    assert api.post("/auth/login", data={"password": PASSWORD}).status_code == 415


def test_login_throttling_is_shared_and_expires(client, monkeypatch):
    api, _, state = client
    reset_password(state, PASSWORD)
    for _ in range(5):
        assert api.post("/auth/login", json={"password": "incorrect"}).status_code == 401
    assert api.post("/auth/login", json={"password": PASSWORD}).status_code == 429
    with server.app.test_client() as other:
        assert other.post("/auth/login", json={"password": PASSWORD}).status_code == 429
    with database(state) as db:
        db.execute("UPDATE attempts SET started=?", (time.time() - 301,))
    assert api.post("/auth/login", json={"password": PASSWORD}).status_code == 200


def test_reset_command_supports_unicode_and_revokes_sessions(client):
    api, _, state = client
    provision(client)
    unicode_password = "管理员独立密码-2026-测试"
    reset_password(state, unicode_password)
    assert api.get("/api/v1/capabilities").status_code == 401
    response = api.post("/auth/login", json={"password": unicode_password})
    assert response.status_code == 200
    assert api.post("/auth/password", headers={"X-CSRF-Token": response.get_json()["csrf_token"]},
                    json={"current_password": unicode_password, "new_password": NEXT_PASSWORD}).status_code == 200


def test_https_cookie_is_secure_and_session_response_never_exposes_password(client):
    api, _, state = client
    reset_password(state, PASSWORD)
    response = api.post("/auth/login", base_url="https://localhost", json={"password": PASSWORD})
    assert "Secure;" in response.headers["Set-Cookie"]
    assert "HttpOnly;" in response.headers["Set-Cookie"]
    assert PASSWORD not in response.get_data(as_text=True)


@pytest.mark.parametrize("candidate,accepted", [("abcdefg", False), ("abcdefgh", True)])
def test_reset_and_browser_password_change_accept_exactly_eight_characters(client, candidate, accepted):
    api, _, state = client
    csrf = provision(client)
    cookie = api.get_cookie(COOKIE).value
    response = api.post("/auth/password", headers={"X-CSRF-Token": csrf},
                        json={"current_password": PASSWORD, "new_password": candidate})
    assert response.status_code == (200 if accepted else 400)
    if accepted:
        assert api.post("/auth/login", json={"password": candidate}).status_code == 200
    else:
        assert "8–256" in response.get_json()["error"]
        assert api.get_cookie(COOKIE).value == cookie
        assert api.get("/api/v1/capabilities").status_code == 200
    if accepted:
        reset_password(state, candidate)
        assert api.post("/auth/login", json={"password": candidate}).status_code == 200
    else:
        with pytest.raises(ValueError, match="8–256"):
            reset_password(state, candidate)
        assert api.get("/api/v1/capabilities").status_code == 200
