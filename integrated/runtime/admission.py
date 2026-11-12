"""Non-waiting process-wide slots; kernel locks release on process termination."""
from contextlib import contextmanager
import errno
import os
from pathlib import Path


class CapacityBusy(RuntimeError):
    pass


@contextmanager
def slots(directory, capacity):
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = None
    for index in range(capacity):
        candidate = os.open(root / str(index), os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            if os.name == 'nt':
                import msvcrt
                if os.fstat(candidate).st_size == 0:
                    os.write(candidate, b'0')
                os.lseek(candidate, 0, os.SEEK_SET)
                msvcrt.locking(candidate, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(candidate, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(candidate)
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                continue
            raise
        descriptor = candidate
        break
    if descriptor is None:
        raise CapacityBusy('诊断并发已满，请等待当前查询结束后重试')
    try:
        yield
    finally:
        if os.name == 'nt':
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        os.close(descriptor)
