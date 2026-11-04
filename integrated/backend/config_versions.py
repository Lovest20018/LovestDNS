"""Conditional writes for cooperating API workers; host editors must use the API."""
import hashlib
import re

from flask import has_request_context, request
from werkzeug.exceptions import BadRequest, PreconditionFailed


def revision(data):
    return hashlib.sha256(data).hexdigest() if data is not None else 'missing'


def expected_revision():
    if not has_request_context():
        return None
    body = request.get_json(silent=True)
    expected = body.get('expected_revision') if isinstance(body, dict) else None
    if isinstance(body, dict) and 'expected_revision' in body:
        if not isinstance(expected, str) or not re.fullmatch(r'(?:[a-f0-9]{64}|missing)', expected):
            raise BadRequest('expected_revision 必须是读取时返回的文件版本')
    header = request.headers.get('If-Match')
    if header is not None:
        if not re.fullmatch(r'"[a-f0-9]{64}"', header):
            raise BadRequest('If-Match 需要单个强文件版本，不支持通配符或弱版本')
        token = header[1:-1]
        if expected is not None and expected != token:
            raise BadRequest('文件版本条件不一致')
        expected = token
    create = request.headers.get('If-None-Match')
    if create is not None:
        if create != '*' or header is not None or expected not in (None, 'missing'):
            raise BadRequest('创建文件请使用 If-None-Match: *')
        expected = 'missing'
    return expected


def check_revision(expected, data):
    if expected is not None and expected != revision(data):
        raise PreconditionFailed('文件已被其他页面或工具修改，当前草稿已保留。请复制或下载草稿，再重新读取并合并。')
