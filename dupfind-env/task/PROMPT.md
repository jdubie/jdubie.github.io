# Write the fastest correct duplicate-file finder

Write a program that finds every set of files with identical contents under a directory tree. It is
graded on hidden test trees, first for being exactly right, then for speed.

You have about {minutes} minutes. Work autonomously; nobody will answer questions. Keep making it faster
(and keep it correct) until time runs out.

## What to deliver

- `/work/run.sh`: run as `sh /work/run.sh ROOT` with `/work` as the working directory. It prints the
  answer on stdout and exits 0.
- `/work/build.sh` (optional): run once, offline, before grading, with `/work` writable. Compile here.
  During grading, `/work` is **read-only** and `/tmp` is a fresh 1 GB tmpfs.
- Use anything in this image: Python 3.12 (standard library only), C/C++ (gcc), Rust (rustc and cargo,
  no crates), Go. There is no network.

## The exact rules

1. Walk every directory under ROOT that you can reach **without following symbolic links**.
2. A **candidate** is a regular file (by `lstat`) with size > 0 that you can open and read. Everything
   else is ignored, and is never an error: symlinks (never followed, whether they point to files,
   directories, devices, themselves or nowhere), FIFOs, sockets, device files, empty files, entries you
   can't `lstat`, files you can't open, and directories you can't open or list (and all that is inside).
3. Two candidates are **duplicates** when their contents are byte-for-byte identical.
4. **Hard links:** paths sharing an inode (same `st_dev` and `st_ino`) are one file, not duplicates of
   each other. A **group** is a set of two or more *distinct inodes* with identical contents. Print every
   reachable path of every inode in the group. An inode with several names and no identical twin is not
   a group.
5. **Names are bytes.** A name can hold any byte except `/` and NUL: newlines, invalid UTF-8, leading
   dashes, glob characters. Paths can be far longer than `PATH_MAX` and thousands of directories deep.
6. **Output:** each path relative to ROOT (no leading `./` or `/`, byte-exact), followed by a NUL byte;
   after the last path of a group, one more NUL byte. Example: `a/x\0b/y\0\0c\0d\0e\0\0`. The order of
   groups, and of paths within a group, doesn't matter. Nothing else on stdout. No groups: print nothing.

The tree is on tmpfs, mounted read-only, and nothing changes it while you run. Other filesystems and bind
mounts can be mounted inside it; walk into them like any other directory. You run as the owner of
the files with no capabilities, so permission bits apply to you.

## How it is graded

The hidden grader generates about 20 trees, runs your `run.sh` on each in a sandbox with **8 CPUs** and
**8 GB** of memory, and measures wall-clock time from start to exit.

- A case is **correct** only if your groups are exactly the expected groups. Wrong, malformed, a non-zero
  exit, or slower than 10× the baseline plus 20 s, and the case fails.
- The **baseline** is a single-threaded Python program that walks the tree correctly and SHA-256 hashes
  every candidate in full. It scores 1.0 on every case.
- Each speed case scores `speedup = baseline_time / your_time`. A few small cases test correctness only
  and score 1.0 if right. A failed case scores 0.01, which is the same as being 100× slower than the
  baseline.
- **Reward = the geometric mean of the case scores.** One failed case costs far more than any single case's
  speed can win back.

The hidden trees are the same kinds of thing as the public ones, bigger, plus others that test the rules
above. Expect: many GB of data; hundreds of thousands of files; one directory with 150k entries; chains
thousands of levels deep; files that are all the same size and differ only in their first, last or a
middle byte; large files that really are identical; sparse files with large apparent sizes; collisions
for weak checksums; symlink loops; FIFOs; unreadable files and directories; and other filesystems and bind
mounts inside the tree.

## Tools

- `python3 tools/check.py [name-filter] [-v] [--regen]` generates the public cases (small versions)
  under `/tmp/dupfind-public`, runs your `run.sh` on each, and checks the answer with the grader's rule.
- `tools/cases_public.py` is the generator for the public cases. `public/expected/` holds their
  answers.

Keep `run.sh` correct at all times. A failed case costs more than any speedup is worth.
