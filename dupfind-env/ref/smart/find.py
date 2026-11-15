"""Reference duplicate finder: the spec in code. Also the naive baseline that defines speedup 1.0.

  python3 reffind.py ROOT [--mode naive|smart|sloppy]

naive   walk, SHA-256 every candidate in full, single thread. Correct; this is the baseline.
smart   size filter, inode dedup, head/tail prefilter, threaded full hash. Correct; a mid calibration point.
sloppy  size + first 64 KiB only, os.walk on string paths. Wrong on purpose: proves the grader catches it.
Output: per group, each path + NUL, then an extra NUL.
"""

from __future__ import annotations

import hashlib
import os
import stat
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

O_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
O_FILE = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY
CHUNK = 1 << 20


def walk(root: bytes):
    """Yield (relpath, st, dir_fd, name) for every regular non-empty file reachable without symlinks.

    dir_fd is only valid during the yield."""
    rootfd = os.open(root, O_DIR)
    stack: list[tuple[int, bytes]] = [(rootfd, b"")]
    while stack:
        fd, rel = stack.pop()
        try:
            try:
                names = os.listdir(fd)
            except OSError:
                continue
            for s in names:
                name = os.fsencode(s)
                try:
                    st = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except OSError:
                    continue
                path = rel + b"/" + name if rel else name
                if stat.S_ISDIR(st.st_mode):
                    try:
                        stack.append((os.open(name, O_DIR, dir_fd=fd), path))
                    except OSError:
                        pass
                elif stat.S_ISREG(st.st_mode) and st.st_size > 0:
                    yield path, st, fd, name
        finally:
            os.close(fd)


def sha_fd(fd: int) -> bytes | None:
    h = hashlib.sha256()
    try:
        while True:
            b = os.read(fd, CHUNK)
            if not b:
                return h.digest()
            h.update(b)
    except OSError:
        return None


def naive(root: bytes) -> list[list[bytes]]:
    by_inode: dict[tuple[int, int], list[bytes]] = defaultdict(list)
    digest: dict[tuple[int, int], bytes] = {}
    for path, st, dfd, name in walk(root):
        key = (st.st_dev, st.st_ino)
        if key not in digest:
            try:
                fd = os.open(name, O_FILE, dir_fd=dfd)
            except OSError:
                continue
            try:
                d = sha_fd(fd)
            finally:
                os.close(fd)
            if d is None:
                continue
            digest[key] = d
        by_inode[key].append(path)
    groups: dict[bytes, list[tuple[int, int]]] = defaultdict(list)
    for key, d in digest.items():
        groups[d].append(key)
    return [sum((by_inode[k] for k in keys), []) for keys in groups.values() if len(keys) > 1]


def open_rel(root: bytes, rel: bytes) -> int:
    full = root + b"/" + rel
    if len(full) < 4000:
        return os.open(full, O_FILE)
    parts = rel.split(b"/")
    fd = os.open(root, O_DIR)
    try:
        for p in parts[:-1]:
            nfd = os.open(p, O_DIR, dir_fd=fd)
            os.close(fd)
            fd = nfd
        return os.open(parts[-1], O_FILE, dir_fd=fd)
    finally:
        os.close(fd)


def smart(root: bytes) -> list[list[bytes]]:
    by_inode: dict[tuple[int, int], list[bytes]] = defaultdict(list)
    by_size: dict[int, list[tuple[int, int]]] = defaultdict(list)
    for path, st, _, _ in walk(root):
        key = (st.st_dev, st.st_ino)
        if key not in by_inode:
            by_size[st.st_size].append(key)
        by_inode[key].append(path)

    def probe(key_size):
        key, size = key_size
        try:
            fd = open_rel(root, by_inode[key][0])
        except OSError:
            return key, None
        try:
            return key, os.pread(fd, 4096, 0) + os.pread(fd, 4096, max(0, size - 4096))
        except OSError:
            return key, None
        finally:
            os.close(fd)

    def full(key):
        try:
            fd = open_rel(root, by_inode[key][0])
        except OSError:
            return key, None
        try:
            return key, sha_fd(fd)
        finally:
            os.close(fd)

    out = []
    with ThreadPoolExecutor(8) as ex:
        for size, keys in by_size.items():
            if len(keys) < 2:
                continue
            buckets: dict[bytes, list[tuple[int, int]]] = defaultdict(list)
            for key, edge in ex.map(probe, [(k, size) for k in keys]):
                if edge is not None:
                    buckets[edge].append(key)
            todo = [k for members in buckets.values() if len(members) > 1 for k in members]
            groups: dict[bytes, list[tuple[int, int]]] = defaultdict(list)
            for key, d in ex.map(full, todo):
                if d is not None:
                    groups[d].append(key)
            out += [sum((by_inode[k] for k in g), []) for g in groups.values() if len(g) > 1]
    return out


def sloppy(root: bytes) -> list[list[bytes]]:
    groups: dict[tuple[int, bytes], list[bytes]] = defaultdict(list)
    for dirpath, _, files in os.walk(root):
        for f in files:
            p = os.path.join(dirpath, f)
            if not os.path.isfile(p) or os.path.getsize(p) == 0:
                continue
            try:
                with open(p, "rb") as fh:
                    groups[(os.path.getsize(p), fh.read(65536))].append(os.path.relpath(p, root))
            except OSError:
                pass
    return [g for g in groups.values() if len(g) > 1]


def main() -> None:
    import resource
    resource.setrlimit(resource.RLIMIT_NOFILE, (resource.getrlimit(resource.RLIMIT_NOFILE)[1],) * 2)
    root = os.fsencode(sys.argv[1]).rstrip(b"/") or b"/"
    mode = sys.argv[sys.argv.index("--mode") + 1] if "--mode" in sys.argv else "naive"
    groups = {"naive": naive, "smart": smart, "sloppy": sloppy}[mode](root)
    out = sys.stdout.buffer
    for g in groups:
        for p in g:
            out.write(p + b"\0")
        out.write(b"\0")
    out.flush()


if __name__ == "__main__":
    main()
