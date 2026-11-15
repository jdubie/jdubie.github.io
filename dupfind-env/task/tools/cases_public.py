"""Generators for the public cases, at public scale (the hidden ones are the same kind of thing, bigger,
plus other cases). Usage: python3 tools/cases_public.py CASE OUT_DIR"""

from __future__ import annotations


import os


import random


import resource


import socket


import stat




O_DIR = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


MB = 1 << 20


class Tree:
    """Builds files under root. Paths are bytes, '/'-separated, relative to root."""

    def __init__(self, root: bytes) -> None:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
        os.makedirs(root, exist_ok=True)
        self.root = root
        self.stack: list[tuple[bytes, int]] = []  # open dir fds for the current path prefix
        self.rootfd = os.open(root, O_DIR)
        self.deferred: list[tuple[bytes, int]] = []

    def _dir(self, parts: list[bytes]) -> int:
        common = 0
        while common < min(len(parts), len(self.stack)) and self.stack[common][0] == parts[common]:
            common += 1
        while len(self.stack) > common:
            os.close(self.stack.pop()[1])
        fd = self.stack[-1][1] if self.stack else self.rootfd
        for p in parts[common:]:
            try:
                os.mkdir(p, 0o755, dir_fd=fd)
            except FileExistsError:
                pass
            fd = os.open(p, O_DIR, dir_fd=fd)
            self.stack.append((p, fd))
        return fd

    def _split(self, rel: bytes) -> tuple[int, bytes]:
        parts = rel.split(b"/")
        return self._dir(parts[:-1]), parts[-1]

    def mkdir(self, rel: bytes) -> None:
        self._dir(rel.split(b"/"))

    def file(self, rel: bytes, data: bytes) -> None:
        d, name = self._split(rel)
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644, dir_fd=d)
        try:
            mv = memoryview(data)
            while mv:
                mv = mv[os.write(fd, mv):]
        finally:
            os.close(fd)

    def big(self, rel: bytes, block: bytes, size: int, patches: dict[int, bytes] | None = None) -> None:
        """size bytes of `block` repeated, then patches written at their offsets (negative = from the end)."""
        d, name = self._split(rel)
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644, dir_fd=d)
        try:
            left = size
            while left:
                left -= os.write(fd, memoryview(block)[:min(left, len(block))])
            for off, data in (patches or {}).items():
                os.pwrite(fd, data, off if off >= 0 else size + off)
        finally:
            os.close(fd)

    def sparse(self, rel: bytes, size: int, extents: dict[int, bytes]) -> None:
        d, name = self._split(rel)
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644, dir_fd=d)
        try:
            os.ftruncate(fd, size)
            for off, data in extents.items():
                os.pwrite(fd, data, off)
        finally:
            os.close(fd)

    def link(self, src: bytes, dst: bytes) -> None:
        sd, sname = self._split(src)
        sfd = os.dup(sd)
        try:
            dd, dname = self._split(dst)
            os.link(sname, dname, src_dir_fd=sfd, dst_dir_fd=dd)
        finally:
            os.close(sfd)

    def symlink(self, target: bytes, rel: bytes) -> None:
        d, name = self._split(rel)
        os.symlink(target, name, dir_fd=d)

    def fifo(self, rel: bytes) -> None:
        d, name = self._split(rel)
        os.mkfifo(name, 0o644, dir_fd=d)

    def sock(self, rel: bytes) -> None:
        d, name = self._split(rel)
        cwd = os.open(".", O_DIR)
        try:
            os.fchdir(d)
            s = socket.socket(socket.AF_UNIX)
            s.bind(name)
            s.close()
        finally:
            os.fchdir(cwd)
            os.close(cwd)

    def chmod_later(self, rel: bytes, mode: int) -> None:
        """Applied by close(), deepest first, so the generator can still write inside first."""
        self.deferred.append((rel, mode))

    def close(self) -> None:
        for rel, mode in sorted(self.deferred, key=lambda x: -x[0].count(b"/")):
            d, name = self._split(rel)
            os.chmod(name, mode, dir_fd=d)
        while self.stack:
            os.close(self.stack.pop()[1])
        os.close(self.rootfd)


def rb(rng: random.Random, n: int) -> bytes:
    return rng.randbytes(n)


def n_(x: float, s: float, lo: int = 1) -> int:
    return max(lo, int(x * s))


def flat_sizes(t: Tree, rng: random.Random, s: float) -> None:
    """Every file has a different size except a few planted pairs: a size index answers almost everything."""
    n = n_(20000, s, 50)
    sizes = rng.sample(range(1, n * 10), n)  # mean ~5n bytes: ~2 GB at n=20000
    for i, size in enumerate(sizes):
        t.file(b"f%06d" % i, rb(rng, size))
    for i in rng.sample(range(n), max(3, n // 1000)):
        t.file(b"copy_of_%06d" % i, open(os.path.join(t.root, b"f%06d" % i), "rb").read())


def last_byte(t: Tree, rng: random.Random, s: float) -> None:
    """Large files, all the same size, identical except the very last byte. Two real pairs."""
    size, n = n_(400 * MB, s, MB), 20
    block = rb(rng, 4 * MB)
    tails = [bytes([i]) for i in range(n)]
    tails[1], tails[3] = tails[0], tails[2]
    for i in range(n):
        t.big(b"big/%02d.bin" % i, block, size, {-1: tails[i]})


def nested_mix(t: Tree, rng: random.Random, s: float) -> None:
    """Source-tree-like: vendored copies of the same small files across many directories."""
    lib = [(b"lib/%s.py" % rb(rng, 4).hex().encode(), rb(rng, rng.randint(10, 20000))) for _ in range(n_(300, s, 20))]
    for p in range(n_(1000, s, 4)):
        base = b"pkg%03d/node_modules/dep%02d" % (p, p % 17)
        for rel, data in rng.sample(lib, k=len(lib) // 3):
            t.file(base + b"/" + rel, data)
        for k in range(n_(40, s, 3)):
            t.file(b"pkg%03d/src/%02d/own%03d.txt" % (p, k % 7, k), rb(rng, rng.randint(1, 3000)))


def hardlinks(t: Tree, rng: random.Random, s: float) -> None:
    """Inodes with many names across directories. Names of one inode are the same file, not duplicates."""
    n = n_(40000, s, 20)
    contents = [rb(rng, rng.randint(100, 5000)) for _ in range(n)]
    for i in range(n):
        t.file(b"a/%05d" % i, contents[i])
        for k in range(rng.randint(0, 12)):
            t.link(b"a/%05d" % i, b"links/%d/%05d_%d" % (rng.randint(0, 99), i, k))
    for i in rng.sample(range(n), n // 5):  # a separate inode with the same bytes: now it is a group
        t.file(b"b/%05d" % i, contents[i])
        if rng.random() < 0.5:
            t.link(b"b/%05d" % i, b"links/b%05d" % i)


def symlinks(t: Tree, rng: random.Random, s: float) -> None:
    """Loops, links to ancestors, dangling links, links to files and to devices. None may be followed."""
    datas = [rb(rng, rng.randint(50, 4000)) for _ in range(n_(200, s, 10))]
    for i, d in enumerate(datas):
        t.file(b"real/%03d" % i, d)
        t.symlink(b"../real/%03d" % i, b"ln/to_%03d" % i)  # a link to a file is not a copy of it
    for i in range(0, len(datas), 7):
        t.file(b"copies/%03d" % i, datas[i])
    t.symlink(b"self", b"loop/self")
    t.symlink(b"b", b"loop/a")
    t.symlink(b"a", b"loop/b")
    t.symlink(b"..", b"loop/up")
    t.symlink(b"../..", b"real/deeper/up2")
    t.symlink(b"/", b"loop/fsroot")
    t.symlink(b"nowhere/at/all", b"loop/dangling")


def weird_names(t: Tree, rng: random.Random, s: float) -> None:
    """Names are bytes: newlines, invalid UTF-8, leading dashes, globs, backslashes, 255-byte names."""
    names = [b"new\nline", b"tab\there", b" lead space", b"trail space ", b"-n", b"--help", b"*", b"?", b"[a]",
             b"back\\slash", b"\xff\xfe invalid utf8", b"caf\xc3\xa9", b"cafe\xcc\x81", b"..dots", b"...", b"'q'",
             b'"dq"', b"$HOME", b"a" * 255, b"\x01\x02ctl", b"\x1b[31mred", b"\r", b" ", b"#", b"~", b"%s%n"]
    payload = [rb(rng, 777), rb(rng, 777), rb(rng, 778)]
    for k, nm in enumerate(names):
        t.file(b"w/" + nm, payload[k % 3])
        t.file(nm + b"/" + nm, payload[(k + 1) % 3])


def empty_files(t: Tree, rng: random.Random, s: float) -> None:
    """Many empty files (never reported) beside a few real duplicates."""
    for i in range(n_(300000, s, 100)):
        t.file(b"e/%03d/%06d" % (i % 300, i), b"")
    one = rb(rng, 1)
    for i in range(5):
        t.file(b"one_byte_%d" % i, one)


PUBLIC = {"flat_sizes": flat_sizes, "last_byte": last_byte, "nested_mix": nested_mix, "hardlinks": hardlinks,
          "symlinks": symlinks, "weird_names": weird_names, "empty_files": empty_files}


PUBLIC_SCALE, PUBLIC_SEED = 0.05, 1
HIDDEN = PUBLIC


def build(name: str, root: bytes, seed: int, scale: float) -> None:
    t = Tree(root)
    try:
        HIDDEN[name](t, random.Random(f"{name}:{seed}"), scale)
    finally:
        t.close()


def _unmount_under(root: bytes) -> None:
    """Unmount anything mounted at or below root, deepest first."""
    top = os.path.realpath(os.fsdecode(root))
    with open("/proc/self/mountinfo") as f:
        points = [ln.split()[4].encode().decode("unicode_escape") for ln in f]
    for p in sorted({p for p in points if p.startswith(top + "/")}, key=len, reverse=True):
        import subprocess
        subprocess.run(["sudo", "-n", "umount", p], check=True, capture_output=True)


def remove(root: bytes) -> None:
    """rm -rf that also handles mode-000 directories and paths longer than PATH_MAX."""
    if not os.path.lexists(root):
        return
    _unmount_under(root)

    def rm_in(fd: int) -> None:
        for name in os.listdir(fd):
            st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(st.st_mode):
                os.chmod(name, 0o700, dir_fd=fd)
                sub = os.open(name, O_DIR, dir_fd=fd)
                try:
                    rm_in(sub)
                finally:
                    os.close(sub)
                os.rmdir(name, dir_fd=fd)
            else:
                os.unlink(name, dir_fd=fd)

    import sys
    sys.setrecursionlimit(max(sys.getrecursionlimit(), 20000))
    os.chmod(root, 0o700)
    fd = os.open(root, O_DIR)
    try:
        rm_in(fd)
    finally:
        os.close(fd)
    os.rmdir(root)


if __name__ == "__main__":
    import sys
    build(sys.argv[1], os.fsencode(sys.argv[2]), PUBLIC_SEED, PUBLIC_SCALE)
