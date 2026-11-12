"""Use the existing fixed component inventory under one systemd service."""
import ipaddress


def configuration(home, state, run, web_address, web_port):
    ipaddress.ip_address(web_address)
    if not 1 <= web_port <= 65535:
        raise ValueError("Invalid Web port")
    address = f"[{web_address}]" if ":" in web_address else web_address
    py = f"{home}/venv/bin/python"
    text = f"""[unix_http_server]
file={run}/supervisor.sock
chmod=0600

[supervisord]
nodaemon=true
logfile={state}/logs/supervisord.log
logfile_maxbytes=2MB
logfile_backups=2
pidfile={run}/supervisor.pid
childlogdir={state}/logs

[rpcinterface:supervisor]
supervisor.rpcinterface_factory=supervisor.rpcinterface:make_main_rpcinterface

[supervisorctl]
serverurl=unix://{run}/supervisor.sock
"""
    programs = {"redis": 10, "unbound-raw": 20, "unbound-forward": 20,
                "dnscrypt": 20, "dnscrypt-socks": 20, "mosdns": 30,
                "watcher": 40, "cron": 40, "updater": 40}
    entries = [(name, f"{py} -m native.component {name}", False, name != "updater", priority,
                0 if name == "updater" else 1) for name, priority in programs.items()]
    entries += [("observer", f"{py} -m runtime.observer", True, True, 45, 1),
                ("manager", f"{py} -m runtime.manager", True, True, 46, 1),
                ("query-recorder", f"{py} -m runtime.query_recorder", True, True, 47, 1),
                ("web", f"{py} -m gunicorn --bind {address}:{web_port} --workers 2 --worker-class gthread --threads 4 --timeout 120 server:app", True, True, 50, 2),
                ("bootstrap", f"/bin/bash {home}/core/init.sh", True, False, 100, 0)]
    for name, command, start, restart, priority, seconds in entries:
        text += f"""
[program:{name}]
command={command}
directory={home}/app
autostart={str(start).lower()}
autorestart={str(restart).lower()}
startsecs={seconds}
startretries=3
priority={priority}
stopsignal=TERM
stopasgroup=true
killasgroup=true
stopwaitsecs={120 if name == 'redis' else 15}
stdout_logfile={state}/logs/{name}.log
stdout_logfile_maxbytes=2MB
stdout_logfile_backups=2
redirect_stderr=true
"""
    return text
