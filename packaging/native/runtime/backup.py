"""Bounded streaming backup of private runtime files, committed before shutdown."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import uuid

from runtime.maintenance import atomic_file

LIMIT = 128 * 1024 * 1024
FILE_LIMIT = 32 * 1024 * 1024
CHUNK = 64 * 1024


def digest(path):
    checksum, size = hashlib.sha256(), 0
    with path.open('rb') as source:
        while block := source.read(CHUNK):
            size += len(block)
            if size > FILE_LIMIT:
                raise ValueError('运行配置超过单文件备份限制')
            checksum.update(block)
    return size, checksum.hexdigest()


class RuntimeBackup:
    def __init__(self, core, directory, manifest):
        self.groups = {'temporary': core.temporary, 'run': core.run, 'resolver': core.data / 'dnscrypt-resolvers'}
        self.directory, self.manifest = directory, manifest
        self.paths = {self.target(item) for item in manifest['files']}

    def target(self, item):
        name, group = item['name'], item['group']
        if not isinstance(name, str) or not name or name in {'.', '..'} or '/' in name or '\\' in name or ':' in name or group not in self.groups:
            raise ValueError('运行备份路径无效')
        if group == 'run' and name not in {'core-env.json', 'mosdns-env.json'}:
            raise ValueError('运行备份文件不在允许清单中')
        if group == 'resolver' and name not in {'dnscrypt.toml', 'dnscrypt_socks.toml'}:
            raise ValueError('解析器备份文件不在允许清单中')
        target = self.groups[group] / name
        if target.is_symlink() or self.groups[group].is_symlink():
            raise ValueError('运行配置路径包含链接')
        return target

    def verify(self):
        total = 0
        for item in self.manifest['files']:
            self.target(item)
            if not re.fullmatch(r'[0-9]{4}', item['blob']):
                raise ValueError('运行备份清单无效')
            source = self.directory / item['blob']
            if source.is_symlink() or not source.is_file():
                raise ValueError('运行备份文件无效')
            size, checksum = digest(source)
            total += size
            if total > LIMIT or size != item['size'] or checksum != item['sha256']:
                raise ValueError('运行备份校验失败')

    def restore(self):
        self.verify()  # Verify every file before modifying the first destination.
        for item in self.manifest['files']:
            target = self.target(item)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + '.suite-restore')
            try:
                with (self.directory / item['blob']).open('rb') as source, temporary.open('wb') as output:
                    shutil.copyfileobj(source, output, CHUNK)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)


def create(core):
    root = core.state / 'runtime-backups'
    if root.is_symlink():
        raise ValueError('备份目录不允许使用链接')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    paths = [('temporary', p) for p in core.temporary.iterdir() if p.is_file() and not p.is_symlink()]
    paths += [('run', core.run / name) for name in ('core-env.json', 'mosdns-env.json')]
    paths += [('resolver', core.data / 'dnscrypt-resolvers' / name) for name in ('dnscrypt.toml', 'dnscrypt_socks.toml')]
    paths = [(group, path) for group, path in paths if path.exists()]
    total = sum(path.stat().st_size for _, path in paths)
    if len(paths) > 9999 or total > LIMIT or shutil.disk_usage(root).free < total + 8 * 1024 * 1024:
        raise ValueError('运行配置备份空间不足或超过限制，请通过主机维护命令重启')
    directory = root / uuid.uuid4().hex
    directory.mkdir(mode=0o700)
    manifest = {'schema_version': 1, 'directory': directory.name, 'files': []}
    try:
        for i, (group, path) in enumerate(paths):
            if path.is_symlink() or not path.is_file() or path.stat().st_size > FILE_LIMIT:
                raise ValueError('运行配置过大或包含链接')
            before = path.stat()
            blob = f'{i:04d}'
            with path.open('rb') as source, (directory / blob).open('xb') as target:
                copied = 0
                while block := source.read(CHUNK):
                    copied += len(block)
                    if copied > FILE_LIMIT:
                        raise ValueError('运行配置在备份时超过限制')
                    target.write(block)
                target.flush()
                os.fsync(target.fileno())
            after = path.stat()
            if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                raise ValueError('运行配置在备份时发生变化，请稍后重试')
            size, checksum = digest(directory / blob)
            manifest['files'].append({'group': group, 'name': path.name, 'blob': blob, 'size': size, 'sha256': checksum})
        backup = RuntimeBackup(core, directory, manifest)
        backup.verify()
        payload = json.dumps(manifest, ensure_ascii=False).encode('utf-8')
        atomic_file(directory / 'manifest.json', payload)
        atomic_file(core.state / 'last-runtime-before-restart.json', payload)
    except Exception:
        if directory.resolve().parent == root.resolve():
            shutil.rmtree(directory)
        raise
    # Keep the current and previous verified generations. Never remove foreign
    # directories or follow symlinks while cleaning old backup generations.
    generations = sorted((p for p in root.iterdir() if re.fullmatch(r'[a-f0-9]{32}', p.name) and p.is_dir() and not p.is_symlink()), key=lambda p: p.stat().st_mtime_ns, reverse=True)
    for old in generations[2:]:
        if old != directory and old.resolve().parent == root.resolve():
            shutil.rmtree(old)
    return backup
