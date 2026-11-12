"""Generate the fixed process inventory; component commands are not user input."""
from pathlib import Path
import sys

HEADER = """[unix_http_server]
file=/run/paopaodns-suite/supervisor.sock
chmod=0600

[supervisord]
nodaemon=true
logfile=/var/lib/paopaodns-suite/logs/supervisord.log
logfile_maxbytes=2MB
logfile_backups=2
pidfile=/run/paopaodns-suite/supervisor.pid
childlogdir=/var/lib/paopaodns-suite/logs
environment=PYTHONPATH="/opt/suite:/opt/suite/backend:/app",PYTHONUNBUFFERED="1"

[rpcinterface:supervisor]
supervisor.rpcinterface_factory=supervisor.rpcinterface:make_main_rpcinterface

[supervisorctl]
serverurl=unix:///run/paopaodns-suite/supervisor.sock
"""


def config():
    programs = {
        "redis": ("redis", 10), "unbound-raw": ("unbound-raw", 20),
        "unbound-forward": ("unbound-forward", 20), "dnscrypt": ("dnscrypt", 20),
        "dnscrypt-socks": ("dnscrypt-socks", 20), "mosdns": ("mosdns", 30),
        "watcher": ("watcher", 40), "cron": ("cron", 40), "updater": ("updater", 40),
    }
    entries = [(name, f"/opt/venv/bin/python -m runtime.component {action}", False,
                "false" if name == "updater" else "true", priority, 0 if name == "updater" else 1)
               for name, (action, priority) in programs.items()]
    entries += [
        ("observer", "/opt/venv/bin/python -m runtime.observer", True, "true", 45, 1),
        ("manager", "/opt/venv/bin/python -m runtime.manager", True, "true", 46, 1),
        ("query-recorder", "/opt/venv/bin/python -m runtime.query_recorder", True, "true", 47, 1),
        ("web", "/opt/venv/bin/gunicorn --bind 0.0.0.0:8080 --workers 2 --worker-class gthread --threads 4 --timeout 120 server:app",
         True, "true", 50, 2),
        ("bootstrap", "/bin/sh /usr/sbin/init.sh", True, "false", 100, 0),
    ]
    text = HEADER
    for name, command, start, restart, priority, seconds in entries:
        text += f"""
[program:{name}]
command={command}
directory=/app
autostart={str(start).lower()}
autorestart={restart}
startsecs={seconds}
startretries=3
priority={priority}
stopsignal=TERM
stopasgroup=true
killasgroup=true
stopwaitsecs={120 if name == 'redis' else 15}
stdout_logfile=/var/lib/paopaodns-suite/logs/{name}.log
stdout_logfile_maxbytes=2MB
stdout_logfile_backups=2
redirect_stderr=true
"""
    return text


if __name__ == "__main__":
    Path(sys.argv[1]).write_text(config(), encoding="utf-8", newline="\n")
