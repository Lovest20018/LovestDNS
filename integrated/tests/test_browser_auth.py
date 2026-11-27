from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import threading
import time

import pytest
import browser_auth
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


def test_session_reads_existing_admin_and_session_without_writes(client, monkeypatch, sqlite_trace):
    api, _, state = client
    reset_password(state, PASSWORD)
    path = state / "browser-auth.sqlite"
    chmod = Path.chmod

    def no_auth_chmod(file, *args, **kwargs):
        assert file != path
        return chmod(file, *args, **kwargs)

    monkeypatch.setattr(Path, "chmod", no_auth_chmod)
    with closing(sqlite3.connect(path)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        sqlite_trace.clear()
        assert api.get("/auth/session").get_json()["authenticated"] is False
        assert sqlite_trace == ["SELECT 1 FROM admin"]
        sqlite_trace.clear()
        assert api.get("/auth/session").status_code == 200
        assert sqlite_trace == []

    assert api.post("/auth/login", json={"password": PASSWORD}).status_code == 200
    with closing(sqlite3.connect(path)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        sqlite_trace.clear()
        assert api.get("/auth/session").get_json()["authenticated"] is True
        assert len(sqlite_trace) == 1 and sqlite_trace[0].startswith("SELECT sessions.*")


@pytest.mark.parametrize("replacement", [False, True])
def test_auth_database_rebuild_invalidates_schema_and_admin_caches(client, replacement):
    api, _, state = client
    reset_password(state, PASSWORD)
    assert api.get("/auth/session").get_json()["initial_password_file"] is None
    path = state / "browser-auth.sqlite"
    if replacement:
        empty = state / "replacement.sqlite"
        empty.touch()
        empty.replace(path)
    else:
        path.unlink()

    initial = state / "admin-password.initial"
    assert api.get("/auth/session").get_json()["initial_password_file"] == str(initial)
    assert api.post("/auth/login", json={"password": initial.read_text().strip()}).get_json()["must_change_password"]


def test_auth_database_rejects_symlink_even_after_schema_cache_is_warm(client, file_symlink):
    _, _, state = client
    path = state / "browser-auth.sqlite"
    target = state / "other.sqlite"
    path.rename(target)
    file_symlink(path, target)
    with pytest.raises(ValueError, match="symlink"):
        with database(state):
            pytest.fail("Authentication opened a symlink")


def test_login_throttling_groups_ipv6_addresses_by_network(client, monkeypatch):
    api, _, state = client
    reset_password(state, PASSWORD)
    for index in range(1, 6):
        response = api.post("/auth/login", json={"password": "incorrect"},
                            environ_overrides={"REMOTE_ADDR": f"2001:db8:1:2::{index}"})
        assert response.status_code == 401
    with database(state) as db:
        assert [tuple(row) for row in db.execute("SELECT address, count FROM attempts")] == [("2001:db8:1:2::/64", 5)]
    for address in ("2001:db8:1:3::1", "192.0.2.4"):
        assert api.post("/auth/login", json={"password": PASSWORD},
                        environ_overrides={"REMOTE_ADDR": address}).status_code == 200
    monkeypatch.setattr(browser_auth, "check_password_hash", lambda *_: pytest.fail("Rate limited login hashed a password"))
    assert api.post("/auth/login", json={"password": PASSWORD},
                    environ_overrides={"REMOTE_ADDR": "2001:0db8:0001:0002::a"}).status_code == 429


def test_failed_login_evicts_oldest_attempt_above_database_limit(client):
    api, _, state = client
    reset_password(state, PASSWORD)
    now = time.time()
    with database(state) as db:
        db.executemany("INSERT INTO attempts VALUES (?, ?, 1)",
                       ((f"client-{index}", now - 100 + index / 100) for index in range(4096)))
    assert api.post("/auth/login", json={"password": "incorrect"},
                    environ_overrides={"REMOTE_ADDR": "non-ip-peer"}).status_code == 401
    with database(state) as db:
        assert db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 4096
        assert db.execute("SELECT 1 FROM attempts WHERE address='client-0'").fetchone() is None
        for address in ("client-1", "non-ip-peer"):
            assert db.execute("SELECT count FROM attempts WHERE address=?", (address,)).fetchone()[0] == 1


def login_status(password):
    with server.app.test_client() as api:
        return api.post("/auth/login", json={"password": password}).status_code


@pytest.mark.parametrize("change,expected", [("attempt", 429), ("password", 401)])
def test_login_rechecks_state_after_unlocked_password_verification(client, monkeypatch, change, expected):
    _, _, state = client
    reset_password(state, PASSWORD)
    if change == "attempt":
        with database(state) as db:
            db.execute("INSERT INTO attempts VALUES ('127.0.0.1', ?, 4)", (time.time(),))

    started, release = threading.Event(), threading.Event()
    verify = browser_auth.check_password_hash

    def blocking_check(hash_value, password):
        if password == PASSWORD:
            started.set()
            assert release.wait(6)
        return verify(hash_value, password)

    monkeypatch.setattr(browser_auth, "check_password_hash", blocking_check)
    with ThreadPoolExecutor(2) as workers:
        pending = workers.submit(login_status, PASSWORD)
        try:
            assert started.wait(3)
            if change == "attempt":
                assert workers.submit(login_status, "incorrect").result(timeout=3) == 401
            else:
                workers.submit(reset_password, state, NEXT_PASSWORD).result(timeout=3)
        finally:
            release.set()
        assert pending.result(timeout=3) == expected
    with database(state) as db:
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    if change == "password":
        assert login_status(NEXT_PASSWORD) == 200


def test_concurrent_failed_logins_commit_at_most_five_attempts(client, monkeypatch):
    _, _, state = client
    reset_password(state, PASSWORD)
    verifying = threading.Barrier(6, timeout=5)

    def reject_together(*_):
        verifying.wait()
        return False

    monkeypatch.setattr(browser_auth, "check_password_hash", reject_together)
    with ThreadPoolExecutor(6) as workers:
        assert sorted(workers.map(login_status, ["incorrect"] * 6)) == [401] * 5 + [429]
    with database(state) as db:
        assert [tuple(row) for row in db.execute("SELECT address, count FROM attempts")] == [("127.0.0.1", 5)]


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
