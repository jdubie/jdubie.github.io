"""Check a data directory obeys the empty-files rule. Prints JSON {"ok": bool, "violations": [...], "entries": n}.

Allowed: directories, and regular files with st_size == 0 and no allocated blocks. Nothing may carry
extended attributes. Everything else (symlinks, fifos, sockets, devices, non-empty files) is a violation.
With a cap (second argument), more than `cap` entries (every name counts, hard links included, plus the
root itself) is a violation too.

  python3 scan.py [data_dir] [cap]
"""

import json
import os
import stat
import sys


def scan(root: str, cap: int = 0, limit: int = 20) -> dict:
    bad, n = [], 0
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = os.path.join(dirpath, name)
            n += 1
            try:
                st = os.lstat(p)
                if stat.S_ISREG(st.st_mode):
                    if st.st_size or st.st_blocks:
                        bad.append(f"non-empty file {p} size={st.st_size} blocks={st.st_blocks}")
                elif not stat.S_ISDIR(st.st_mode):
                    bad.append(f"forbidden file type {p} mode={oct(st.st_mode)}")
                if os.listxattr(p, follow_symlinks=False):
                    bad.append(f"xattrs on {p}")
            except FileNotFoundError:
                continue  # removed while we walked: the server is running
            if len(bad) >= limit:
                return {"ok": False, "violations": bad, "entries": n}
    try:
        if os.listxattr(root):
            bad.append(f"xattrs on {root}")
    except OSError:
        pass
    if cap and n + 1 > cap:
        bad.append(f"{n + 1} inodes/links in the data directory, over the cap of {cap}")
    return {"ok": not bad, "violations": bad, "entries": n}


if __name__ == "__main__":
    print(json.dumps(scan(sys.argv[1] if len(sys.argv) > 1 else "/data", int(sys.argv[2]) if len(sys.argv) > 2 else 0)))
