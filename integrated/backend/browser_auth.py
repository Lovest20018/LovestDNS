"""Password-based browser sessions, separate from the integration API tokens."""
from contextlib import closing, contextmanager
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import sqlite3
import time

from flask import jsonify, request
from werkzeug.security import check_password_hash, generate_password_hash

COOKIE = "lovestdns_session"
SESSION_SECONDS = 7 * 86400
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@contextmanager
def database(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / "browser-auth.sqlite"
    # No credentials or sessions live in the shared DNS data directory.
    if path.is_symlink():
        raise ValueError("Browser authentication database must not be a symlink")
    with closing(sqlite3.connect(path, timeout=10)) as db, db:
        path.chmod(0o600)
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE IF NOT EXISTS admin (id INTEGER PRIMARY KEY CHECK(id=1), password_hash TEXT NOT NULL, initial INTEGER NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires REAL NOT NULL, remembered INTEGER NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS attempts (address TEXT PRIMARY KEY, started REAL NOT NULL, count INTEGER NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
        yield db


def external_api_enabled(root):
    with database(root) as db:
        row = db.execute("SELECT value FROM settings WHERE name='external_api'").fetchone()
        return bool(row and row['value'] == 'enabled')


def set_external_api(root, enabled):
    with database(root) as db:
        db.execute("INSERT OR REPLACE INTO settings VALUES('external_api', ?)",
                   ('enabled' if enabled else 'disabled',))


def ensure_admin(root):
    """Create an unguessable initial password once; only the host can read it."""
    with database(root) as db:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT 1 FROM admin").fetchone():
            password = secrets.token_urlsafe(18)
            path = Path(root) / "admin-password.initial"
            # Exclusive creation also refuses symbolic links and stale files.
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as output:
                output.write(password + "\n")
                output.flush()
                os.fsync(output.fileno())
            db.execute("INSERT INTO admin VALUES(1, ?, 1)", (generate_password_hash(password),))


def session_digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def current_session(root):
    cookie = request.cookies.get(COOKIE, "")
    if not cookie or len(cookie) > 128:
        return None
    with database(root) as db:
        row = db.execute("SELECT sessions.*, admin.initial FROM sessions JOIN admin ON admin.id=1 WHERE sessions.id=? AND expires>?",
                         (session_digest(cookie), time.time())).fetchone()
        return dict(row) if row else None


def same_origin():
    origin = request.headers.get("Origin")
    return request.headers.get("Sec-Fetch-Site") != "cross-site" and (not origin or origin == request.host_url.rstrip("/"))


def csrf_valid(session):
    supplied = request.headers.get("X-CSRF-Token", "")
    return same_origin() and bool(supplied) and hmac.compare_digest(supplied.encode(), session["csrf"].encode())


def reset_password(root, password):
    if not isinstance(password, str) or not 8 <= len(password) <= 256:
        raise ValueError("管理员密码需为 8–256 个字符")
    encoded = generate_password_hash(password)
    with database(root) as db:
        db.execute("INSERT OR REPLACE INTO admin VALUES(1, ?, 0)", (encoded,))
        db.execute("DELETE FROM sessions")
        db.execute("DELETE FROM attempts")
    (Path(root) / "admin-password.initial").unlink(missing_ok=True)


def create_session(db, remembered):
    value, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    now = time.time()
    db.execute("DELETE FROM sessions WHERE expires<=?", (now,))
    # Keep a bounded set across repeated logins and workers.
    db.execute("DELETE FROM sessions WHERE id IN (SELECT id FROM sessions ORDER BY expires DESC LIMIT -1 OFFSET 19)")
    db.execute("INSERT INTO sessions VALUES(?, ?, ?, ?)",
               (session_digest(value), csrf, now + (SESSION_SECONDS if remembered else 12 * 3600), int(remembered)))
    return value, csrf


def set_cookie(response, value, remembered):
    response.set_cookie(COOKIE, value, max_age=SESSION_SECONDS if remembered else None,
                        httponly=True, samesite="Strict", secure=request.is_secure, path="/")
    return response


def register(server):
    app = server.app

    def failure(message, status):
        return jsonify({"error": message}), status

    @app.route("/auth/external-api", methods=["GET", "PUT"])
    def external_api_setting():
        session = current_session(server.STATE_DIR)
        if not session:
            return failure("请先登录", 401)
        if session['initial']:
            return failure("请先修改初始管理员密码", 403)
        if request.method == 'PUT':
            if not csrf_valid(session):
                return failure("登录校验已失效，请刷新页面", 403)
            body = request.get_json(silent=True)
            if not isinstance(body, dict) or type(body.get('enabled')) is not bool:
                return failure("enabled 需为布尔值", 400)
            set_external_api(server.STATE_DIR, body['enabled'])
        return jsonify({'enabled': external_api_enabled(server.STATE_DIR)})

    @app.before_request
    def browser_request_guard():
        if request.path.startswith("/auth/") and request.method in WRITE_METHODS:
            if not same_origin():
                return failure("请从当前站点发起登录操作", 403)
            if not request.is_json:
                return failure("请求需使用 JSON", 415)

    @app.after_request
    def browser_response_headers(response):
        if request.path.startswith("/auth/"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.get("/auth/session")
    def session_status():
        ensure_admin(server.STATE_DIR)
        session = current_session(server.STATE_DIR)
        if session:
            return jsonify({"authenticated": True, "csrf_token": session["csrf"],
                            "must_change_password": bool(session["initial"])})
        initial = server.STATE_DIR / "admin-password.initial"
        return jsonify({"authenticated": False,
                        "initial_password_file": str(initial) if initial.is_file() else None})

    @app.post("/auth/login")
    def login():
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not isinstance(body.get("password"), str) or len(body["password"]) > 256:
            return failure("请输入管理员密码", 400)
        ensure_admin(server.STATE_DIR)
        address, now = request.remote_addr or "unknown", time.time()
        with database(server.STATE_DIR) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM attempts WHERE started<?", (now - 300,))
            attempt = db.execute("SELECT count FROM attempts WHERE address=?", (address,)).fetchone()
            if attempt and attempt["count"] >= 5:
                return failure("尝试次数过多，请在 5 分钟后重试", 429)
            admin = db.execute("SELECT * FROM admin WHERE id=1").fetchone()
            if not check_password_hash(admin["password_hash"], body["password"]):
                db.execute("INSERT INTO attempts VALUES(?, ?, 1) ON CONFLICT(address) DO UPDATE SET count=count+1", (address, now))
                return failure("管理员密码不正确", 401)
            db.execute("DELETE FROM attempts WHERE address=?", (address,))
            previous = request.cookies.get(COOKIE, "")
            if previous:
                db.execute("DELETE FROM sessions WHERE id=?", (session_digest(previous),))
            remembered = body.get("remember") is True
            value, csrf = create_session(db, remembered)
            return set_cookie(jsonify({"authenticated": True, "csrf_token": csrf,
                                       "must_change_password": bool(admin["initial"])}), value, remembered)

    @app.post("/auth/logout")
    def logout():
        session = current_session(server.STATE_DIR)
        if session and not csrf_valid(session):
            return failure("登录校验已失效，请刷新页面", 403)
        if session:
            with database(server.STATE_DIR) as db:
                db.execute("DELETE FROM sessions WHERE id=?", (session["id"],))
        response = jsonify({"authenticated": False})
        response.delete_cookie(COOKIE, path="/", httponly=True, samesite="Strict", secure=request.is_secure)
        return response

    @app.post("/auth/password")
    def password():
        session = current_session(server.STATE_DIR)
        if not session:
            return failure("请先登录", 401)
        if not csrf_valid(session):
            return failure("登录校验已失效，请刷新页面", 403)
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return failure("密码格式无效", 400)
        current, next_password = body.get("current_password"), body.get("new_password")
        if not isinstance(current, str) or len(current) > 256:
            return failure("请输入当前密码", 400)
        if not isinstance(next_password, str) or not 8 <= len(next_password) <= 256:
            return failure("管理员密码需为 8–256 个字符", 400)
        with database(server.STATE_DIR) as db:
            db.execute("BEGIN IMMEDIATE")
            admin = db.execute("SELECT password_hash FROM admin WHERE id=1").fetchone()
            if not check_password_hash(admin["password_hash"], current):
                return failure("当前密码不正确", 401)
            if hmac.compare_digest(current.encode(), next_password.encode()):
                return failure("新密码需与当前密码不同", 400)
            db.execute("UPDATE admin SET password_hash=?, initial=0 WHERE id=1", (generate_password_hash(next_password),))
            db.execute("DELETE FROM sessions")
            db.execute("DELETE FROM attempts")
            value, csrf = create_session(db, bool(session["remembered"]))
        (server.STATE_DIR / "admin-password.initial").unlink(missing_ok=True)
        return set_cookie(jsonify({"authenticated": True, "csrf_token": csrf, "must_change_password": False}),
                          value, bool(session["remembered"]))


if __name__ == "__main__":
    import argparse
    import getpass
    parser = argparse.ArgumentParser(description="Reset the local administrator password and revoke browser sessions")
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--external-api", choices=['on', 'off', 'status'])
    args = parser.parse_args()
    if args.external_api:
        if args.external_api != 'status':
            set_external_api(args.state_dir, args.external_api == 'on')
        print('External API: ' + ('enabled' if external_api_enabled(args.state_dir) else 'disabled'))
        raise SystemExit(0)
    password = getpass.getpass("New administrator password (8–256 characters): ")
    if password != getpass.getpass("Repeat password: "):
        parser.error("Passwords do not match")
    try:
        reset_password(args.state_dir, password)
    except ValueError as exc:
        parser.error(str(exc))
    print("Administrator password updated; previous browser sessions revoked.")
