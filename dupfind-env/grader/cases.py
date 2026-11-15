"""Fixture generators for the find-duplicates benchmark. Every case is deterministic in (seed, scale).

A case builds a directory tree under `root` through `Tree`, which creates everything relative to directory
fds so paths longer than PATH_MAX work. Expected answers are computed afterwards by the reference finder,
never from the generator's intent, so a generator bug can't make the expected answer disagree with the spec.
"""

from __future__ import annotations

import os
import random
import resource
import socket
import stat
import zlib

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


# ---------------------------------------------------------------- public cases (also run hidden, bigger)

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

# ---------------------------------------------------------------- hidden-only cases


def first_byte(t: Tree, rng: random.Random, s: float) -> None:
    """Large files, all the same size, identical except the very first byte. One real pair."""
    size, n = n_(300 * MB, s, MB), 16
    block = rb(rng, 4 * MB)
    heads = [bytes([i]) for i in range(n)]
    heads[5] = heads[4]
    for i in range(n):
        t.big(b"%02d" % i, block, size, {0: heads[i]})


def middle_byte(t: Tree, rng: random.Random, s: float) -> None:
    """Same size, same head and tail, one byte flipped somewhere in the middle."""
    size, n = n_(300 * MB, s, MB), 12
    block = rb(rng, 4 * MB + 17)
    for i in range(n):
        patch = {} if i in (0, 1, 6, 7) else {rng.randrange(size // 8, size - size // 8): bytes([rng.randrange(256)])}
        t.big(b"m/%02d" % i, block, size, patch)


def big_true_dups(t: Tree, rng: random.Random, s: float) -> None:
    """Large files that really are duplicates: every byte must be read."""
    size = n_(300 * MB, s, MB)
    for c in range(3):
        block = rb(rng, 8 * MB + c)
        for k in range(4):
            t.big(b"c%d/copy%d" % (c, k), block, size, {-7: b"content%d" % c})


def many_tiny(t: Tree, rng: random.Random, s: float) -> None:
    """Hundreds of thousands of 1-64 byte files over 1000 directories; syscall-bound."""
    pool = [rb(rng, rng.randint(1, 64)) for _ in range(n_(20000, s, 50))]
    for i in range(n_(300000, s, 200)):
        t.file(b"d%03d/%d" % (i % 1000, i), rng.choice(pool) if rng.random() < 0.6 else rb(rng, rng.randint(1, 64)))


def huge_flat_dir(t: Tree, rng: random.Random, s: float) -> None:
    """One directory, 150k entries, all exactly 1 KiB."""
    pool = [rb(rng, 1024) for _ in range(n_(30000, s, 20))]
    for i in range(n_(150000, s, 100)):
        t.file(b"%07d" % i, rng.choice(pool) if rng.random() < 0.3 else rb(rng, 1024))


def deep_pathmax(t: Tree, rng: random.Random, s: float) -> None:
    """A 2500-level chain of 100-byte names: paths far beyond PATH_MAX, recursion-limit deep."""
    depth = n_(2500, s, 60)
    a, b = rb(rng, 3000), rb(rng, 3000)
    parts: list[bytes] = []
    for lvl in range(depth):
        parts.append(b"%04d" % lvl + b"d" * 96)
        if lvl % 50 == 49:
            t.file(b"/".join(parts + [b"x"]), a if lvl % 100 == 49 else b)
            t.file(b"/".join(parts + [b"u"]), rb(rng, 3000))
    t.file(b"top_a", a)


def wide_tree(t: Tree, rng: random.Random, s: float) -> None:
    """A full binary tree of directories, two files per leaf."""
    depth = 15 if s >= 1 else 6
    pool = [rb(rng, rng.randint(1, 9000)) for _ in range(3000)]
    for leaf in range(1 << depth):
        path = b"/".join(b"LR"[(leaf >> k) & 1:((leaf >> k) & 1) + 1] for k in range(depth))
        t.file(path + b"/p", rng.choice(pool))
        t.file(path + b"/q", rb(rng, rng.randint(1, 9000)))


def special_files(t: Tree, rng: random.Random, s: float) -> None:
    """FIFOs (opening one blocks forever), sockets, and links to devices, between real duplicates."""
    d = rb(rng, 5000)
    for i in range(n_(50, s, 3)):
        t.fifo(b"q/fifo%02d" % i)
        t.sock(b"q/sock%02d" % i)
        t.file(b"q/real%02d" % i, d if i % 2 else rb(rng, 5000))
    t.symlink(b"/dev/zero", b"dev/zero")
    t.symlink(b"/dev/urandom", b"dev/urandom")
    t.symlink(b"/proc/self/fd/0", b"dev/stdin")
    t.symlink(b"../q/fifo00", b"dev/fifo_link")


def permissions(t: Tree, rng: random.Random, s: float) -> None:
    """Unreadable files, unlistable directories, list-only directories. You run as the owner, without caps."""
    a, b = rb(rng, 4096), rb(rng, 4096)
    t.file(b"ok/a1", a)
    t.file(b"ok/a2", a)
    t.file(b"ok/b1", b)
    t.file(b"noread/b2", b)
    t.chmod_later(b"noread/b2", 0o000)  # its only copy elsewhere is b1: no group
    t.file(b"wronly/a3", a)
    t.chmod_later(b"wronly/a3", 0o200)
    for i in range(n_(200, s, 5)):
        t.file(b"locked/%03d" % i, a)  # dir mode 000: invisible
        t.file(b"xonly/%03d" % i, a)  # dir mode 0111: can't list, so invisible
        t.file(b"ronly/%03d" % i, a)  # dir mode 0444: names listable but nothing can be stat'ed or opened
    t.file(b"locked/sub/deeper", b)
    t.chmod_later(b"locked", 0o000)
    t.chmod_later(b"xonly", 0o111)
    t.chmod_later(b"ronly", 0o444)
    c = rb(rng, 999)
    t.file(b"ok/c1", c)
    t.link(b"ok/c1", b"locked/c1_hardlink")  # a name you can't reach doesn't count
    t.file(b"ok/c2", c)


def sparse_giant(t: Tree, rng: random.Random, s: float) -> None:
    """3 GiB apparent size, mostly holes. A hole equals written zeros: dense_copy duplicates sparse_a."""
    size = n_(3 << 30, s, 8 * MB)
    ext = {o: rb(rng, MB) for o in sorted(rng.sample(range(0, size - MB, MB), 6))}
    last = size - 4096
    base = ext | {last: rb(rng, 4096)}
    t.sparse(b"sparse_a", size, base)
    t.sparse(b"sparse_b", size, base)
    t.sparse(b"sparse_c_lastbyte", size, base | {size - 1: b"\x01"})
    mid = sorted(ext)[3]
    t.sparse(b"sparse_d_mid", size, base | {mid + 500: bytes([ext[mid][500] ^ 0xFF])})
    zeros = bytes(4 * MB)
    t.big(b"dense_copy", zeros, size, base)


def crc_bait(t: Tree, rng: random.Random, s: float) -> None:
    """Same size, same first and last 64 KiB, same CRC32, different bytes in the middle."""
    for i in range(n_(40, s, 4)):
        size = rng.choice([200_000, 1_000_000, 5_000_000])
        a = bytearray(rb(rng, size))
        b = crc32_twin(a, rng.randrange(70_000, size - 70_000))
        assert zlib.crc32(a) == zlib.crc32(b) and a != b
        t.file(b"pair%02d/a" % i, bytes(a))
        t.file(b"pair%02d/b" % i, bytes(b))
        if i % 4 == 0:
            t.file(b"pair%02d/a_copy" % i, bytes(a))


def crc32_twin(a: bytearray, off: int) -> bytes:
    """A copy of a with 8 bytes at off changed so its CRC32 is unchanged (CRC is affine over GF(2))."""
    n = len(a)
    z = zlib.crc32(bytes(n))

    def contrib(bit: int) -> int:
        d = bytearray(n)
        d[off + bit // 8] = 1 << (bit % 8)
        return zlib.crc32(bytes(d)) ^ z

    rows = [(contrib(k), 1 << k) for k in range(64)]
    basis: list[tuple[int, int]] = []
    for v, m in rows:
        for bv, bm in basis:
            if v ^ bv < v:
                v, m = v ^ bv, m ^ bm
        if v == 0:
            out = bytearray(a)
            for k in range(64):
                if m >> k & 1:
                    out[off + k // 8] ^= 1 << (k % 8)
            return bytes(out)
        basis.append((v, m))
        basis.sort(reverse=True)
    raise AssertionError("no CRC32 null vector")


def one_giant_group(t: Tree, rng: random.Random, s: float) -> None:
    """One 300-byte content copied 50k times, plus near-misses that differ in one byte."""
    d = rb(rng, 300)
    for i in range(n_(50000, s, 50)):
        if i % 997 == 0:
            t.file(b"g/%02d/%06d" % (i % 50, i), d[:150] + bytes([d[150] ^ 1]) + d[151:])
        else:
            t.file(b"g/%02d/%06d" % (i % 50, i), d)


def trivial(t: Tree, rng: random.Random, s: float) -> None:
    """No duplicates at all: a lone file, an empty directory, a symlink to that file."""
    t.file(b"only", rb(rng, 10))
    t.mkdir(b"empty_dir/nested/deeper")
    t.symlink(b"../../only", b"empty_dir/nested/link")


def size_classes(t: Tree, rng: random.Random, s: float) -> None:
    """Thousands of files in three sizes, random content, some duplicates: sizes alone don't help."""
    for i in range(n_(12000, s, 60)):
        size = rng.choice([4096, 65536, 262144])
        data = rb(rng, size)
        t.file(b"s%d/%05d" % (size, i), data)
        if rng.random() < 0.05:
            t.file(b"dup/%05d" % i, data)


def _sudo(*argv: str) -> None:
    import subprocess
    subprocess.run(["sudo", "-n", *argv], check=True, capture_output=True)


def _mount_points(t: Tree, rels: list[bytes], how: str, src: list[bytes] | None = None) -> list[str]:
    """Mount a fresh tmpfs (how='tmpfs') or a bind of src[i] (how='bind') at each rel; returns host paths."""
    while t.stack:  # cached dir fds would point under the mount; reopen through it from now on
        os.close(t.stack.pop()[1])
    out = []
    for i, rel in enumerate(rels):
        t.mkdir(rel)
        while t.stack:
            os.close(t.stack.pop()[1])
        dst = os.fsdecode(os.path.join(t.root, rel))
        if how == "tmpfs":
            _sudo("mount", "-t", "tmpfs", "-o", f"size=512m,mode=755,uid={os.getuid()},gid={os.getgid()}", "dupfind", dst)
        else:
            _sudo("mount", "--bind", os.fsdecode(os.path.join(t.root, src[i])), dst)
        out.append(dst)
    return out


def _remount_ro(paths: list[str], bind: bool) -> None:
    for p in paths:
        _sudo("mount", "-o", "remount,bind,ro" if bind else "remount,ro", p)


def cross_device(t: Tree, rng: random.Random, s: float) -> None:
    """Two more tmpfs filesystems mounted inside the tree, filled in the same order, so their inode numbers
    collide. Same st_ino on a different st_dev is a different file: keying inodes on st_ino alone merges them."""
    n = n_(600, s, 12)
    a = [rb(rng, rng.randint(100, 4000)) for _ in range(n)]
    b = [a[(i + 1) % n] if i % 2 == 0 else rb(rng, len(a[i])) for i in range(n)]  # same sizes as A, half are A's
    mounts = _mount_points(t, [b"fsA", b"mnt/fsB"], "tmpfs")
    for side, data in ((b"fsA", a), (b"mnt/fsB", b)):
        for i, d in enumerate(data):
            t.file(side + b"/d%d/f%05d" % (i % 4, i), d)
        for i in range(0, n, 9):
            t.link(side + b"/d%d/f%05d" % (i % 4, i), side + b"/links/l%05d" % i)
    while t.stack:
        os.close(t.stack.pop()[1])
    ia = os.stat(os.path.join(t.root, b"fsA/d0/f00000")).st_ino
    ib = os.stat(os.path.join(t.root, b"mnt/fsB/d0/f00000")).st_ino
    assert ia == ib, (ia, ib)
    for i in range(3, n, 11):
        t.file(b"top/%05d" % i, a[i])  # a third device: the tree's own tmpfs
    _remount_ro(mounts, bind=False)


def bind_alias(t: Tree, rng: random.Random, s: float) -> None:
    """A directory bind-mounted at two more places: every file is reachable by several paths with the same
    st_dev and st_ino (one inode, every path printed). Skipping directories already seen by inode drops paths."""
    n = n_(600, s, 12)
    data = [rb(rng, rng.randint(50, 5000)) for _ in range(n)]
    for i, d in enumerate(data):
        t.file(b"real/s%d/%05d" % (i % 5, i), d)
    for i in range(0, n, 3):
        t.file(b"copies/%05d" % i, data[i])  # a distinct inode: real/, alias/ and copies/ paths form a group
    for i in range(1, n, 13):
        t.file(b"real/s0/twin%05d" % i, data[i])
    mounts = _mount_points(t, [b"alias", b"z/deep/s1_again"], "bind", [b"real", b"real/s1"])
    _remount_ro(mounts, bind=True)


HIDDEN = PUBLIC | {"first_byte": first_byte, "middle_byte": middle_byte, "big_true_dups": big_true_dups,
                   "many_tiny": many_tiny, "huge_flat_dir": huge_flat_dir, "deep_pathmax": deep_pathmax,
                   "wide_tree": wide_tree, "special_files": special_files, "permissions": permissions,
                   "sparse_giant": sparse_giant, "crc_bait": crc_bait, "one_giant_group": one_giant_group,
                   "trivial": trivial, "size_classes": size_classes,
                   "cross_device": cross_device, "bind_alias": bind_alias}

# Correctness traps too small to time meaningfully: they score 1.0 if right, the failure floor if wrong.
GATES = {"trivial", "special_files", "permissions", "symlinks", "weird_names", "crc_bait", "deep_pathmax",
         "cross_device", "bind_alias"}
PUBLIC_SCALE, PUBLIC_SEED, HIDDEN_SEED = 0.05, 1, 7919


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
