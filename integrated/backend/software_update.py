"""Browser-only update controls; the installer runs in a separate host unit."""
import os
from pathlib import Path
import sqlite3

from flask import jsonify, request
from werkzeug.exceptions import ServiceUnavailable

from browser_auth import current_session, csrf_valid, WRITE_METHODS
from runtime import software_update as protocol
from runtime import update_network


def update_root():
    raw = os.environ.get('LOVESTDNS_UPDATE_DIR', '')
    return Path(raw) if os.environ.get('LOVESTDNS_SYSTEMD') == '1' and raw and Path(raw).is_absolute() else None


def update_in_progress():
    root = update_root()
    if root is None or not protocol.supported(root):
        return False
    value = protocol.read_json(root / 'status.json', {})
    return value.get('action') == 'apply' and value.get('phase') in protocol.BUSY


def register(server):
    @server.app.route('/auth/software-update/network', methods=['GET', 'PUT'])
    def software_update_network():
        session = current_session(server.STATE_DIR)
        if not session:
            return jsonify({'error': '请先登录'}), 401
        if session['initial']:
            return jsonify({'error': '请先修改初始管理员密码'}), 403
        if request.method == 'PUT' and not csrf_valid(session):
            return jsonify({'error': '登录校验已失效，请刷新页面'}), 403
        try:
            root = update_root()
            if root is None or not protocol.supported(root):
                return jsonify({'error': '当前安装不支持更新网络设置，请先升级主机更新助手'}), 409
            value = (update_network.save(root, request.get_json(silent=True), server.VERSION)
                     if request.method == 'PUT' else update_network.public(update_network.load(root)))
            return jsonify(value)
        except protocol.UpdateBusy as exc:
            return jsonify({'error': str(exc)}), 409
        except protocol.UpdateError as exc:
            return jsonify({'error': str(exc)}), 400
        except (OSError, ValueError, sqlite3.Error):
            return jsonify({'error': '更新网络设置暂不可用，请核对主机更新助手状态'}), 503

    @server.app.route('/auth/software-update', methods=['GET', 'POST'])
    def software_update():
        session = current_session(server.STATE_DIR)
        if not session:
            return jsonify({'error': '请先登录'}), 401
        if session['initial']:
            return jsonify({'error': '请先修改初始管理员密码'}), 403
        if request.method == 'POST' and not csrf_valid(session):
            return jsonify({'error': '登录校验已失效，请刷新页面'}), 403
        try:
            root = update_root()
            if root is None or not protocol.supported(root):
                if request.method == 'POST':
                    return jsonify({'error': '当前部署不支持网页更新，请先通过主机安装包升级'}), 409
                return jsonify({'schema_version': 1, 'supported': False, 'current_version': server.VERSION,
                    'reason': '网页更新适用于已安装更新助手的 Debian 原生版本。Docker 部署请使用主机 Compose。'})
            if request.method == 'POST':
                return jsonify(protocol.queue(root, request.get_json(silent=True), server.VERSION)), 202
            return jsonify(protocol.status(root, server.VERSION))
        except protocol.UpdateBusy as exc:
            return jsonify({'error': str(exc)}), 409
        except protocol.UpdateError as exc:
            return jsonify({'error': str(exc)}), 400
        except (OSError, ValueError, sqlite3.Error):
            return jsonify({'error': '更新记录暂不可用，请核对主机更新助手状态'}), 503

    @server.app.before_request
    def protect_update_snapshot():
        # Keep configuration, auth settings and core maintenance stable across
        # the installer backup. DNS queries and status remain available.
        if (request.method in WRITE_METHODS
                and ((request.path.startswith('/api/')
                      and not request.path.endswith(('/dns-test', '/dns-compare')))
                     or request.path in {'/auth/password', '/auth/external-api'})):
            try:
                busy = update_in_progress()
            except (OSError, ValueError, sqlite3.Error):
                if request.path.startswith('/api/v1/'):
                    raise ServiceUnavailable('更新状态暂不可确认，请稍后重试')
                return jsonify({'error': '更新状态暂不可确认，请稍后重试'}), 503
            if busy:
                if request.path.startswith('/api/v1/'):
                    raise ServiceUnavailable('软件更新进行中，请完成后再修改配置或执行维护任务')
                return jsonify({'error': '软件更新进行中，请完成后再修改配置或执行维护任务'}), 503
