"""Identify local SQLite state files for per-process caches."""


def database_inode(path):
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return info.st_dev, info.st_ino


def create_database_inode(path):
    # Capture the inode before SQLite opens it, even on first use.
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.touch(mode=0o600, exist_ok=False)
    except FileExistsError:
        pass
    return database_inode(path)
