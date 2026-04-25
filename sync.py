#!/usr/bin/env python3
"""mmclaw incremental sync: local openclaw/ -> remote /root/mmclaw/openclaw/.

Strategy:
  1. Walk local openclaw/ tree (excluding heavy/build dirs).
  2. Ask remote for mtime+size of every file under /root/mmclaw/openclaw/.
  3. For each local file whose (mtime, size) does not match the remote,
     stage it into a tarball; stream that tarball over SSH and untar at
     the destination.  This is rsync-in-spirit but only needs the system
     ssh + tar that Windows OpenSSH already ships with.

Tweak SSH_KEY / REMOTE if the host changes.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

LOCAL_ROOT = Path(__file__).resolve().parent / "openclaw"
REMOTE_USER = "root"
REMOTE_HOST = "192.168.31.51"
REMOTE_ROOT = "/root/mmclaw/openclaw"
SSH_KEY = os.path.expanduser("~/.ssh/id_rsa")
# Fallback: Windows %USERPROFILE%\.ssh\id_rsa when HOME points elsewhere.
if not os.path.isfile(SSH_KEY):
    SSH_KEY = os.path.join(
        os.environ.get("USERPROFILE", os.path.expanduser("~")), ".ssh", "id_rsa"
    )

# Directory basenames to skip wherever they appear in the tree.
EXCLUDE_DIRS_ANY = {
    "node_modules",
    ".git",
    "dist",
    "dist-runtime",
    ".openclaw",
    ".pnpm-store",
    ".next",
    "__pycache__",
    "__openclaw_vitest__",
    ".gradle",
    ".cxx",
    ".kotlin",
    ".swiftpm",
    ".derivedData",
    ".DS_Store",
    ".stfolder",
}
# Directories matched only at the top level of LOCAL_ROOT (matches .gitignore's
# leading-slash entries — `bin/`, `vendor/`, `Core/` are top-level only).
EXCLUDE_DIRS_TOP = {
    "tmp",
    ".tmp",
    ".artifacts",
    "coverage",
    ".serena",
    ".local",
    ".agent",
    ".claude",
    ".worktrees",
    ".ant-colony",
    "bin",
    "Core",
    "vendor",
    ".idea",
    "memory",
    "local",
    "analysis",
    "Swabble",  # Xcode build dir at top level only
}
# File suffixes / exact names to skip.
EXCLUDE_SUFFIXES = (
    ".tsbuildinfo",
    ".pyc",
    ".test.ts.snap",
)
EXCLUDE_FILES = {
    ".DS_Store",
    ".runtime-postbuildstamp",
    ".dev-state",
}


def is_excluded_dir(rel: Path) -> bool:
    """Returns True if any segment of *rel* matches an EXCLUDE_DIRS_ANY entry,
    or the first segment matches an EXCLUDE_DIRS_TOP entry."""
    parts = rel.parts
    if not parts:
        return False
    if any(p in EXCLUDE_DIRS_ANY for p in parts):
        return True
    if parts[0] in EXCLUDE_DIRS_TOP:
        return True
    return False


def list_local() -> dict[str, tuple[int, int]]:
    out: dict[str, tuple[int, int]] = {}
    for dirpath, dirnames, filenames in os.walk(LOCAL_ROOT):
        d_rel = Path(dirpath).relative_to(LOCAL_ROOT)
        # Prune excluded subdirectories in-place: any-level matches always,
        # plus top-level matches when we are AT the root.
        if str(d_rel) == ".":
            dirnames[:] = [
                d
                for d in dirnames
                if d not in EXCLUDE_DIRS_ANY and d not in EXCLUDE_DIRS_TOP
            ]
        else:
            dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS_ANY]
        if is_excluded_dir(d_rel):
            dirnames[:] = []
            continue
        for fn in filenames:
            if fn in EXCLUDE_FILES or fn.endswith(EXCLUDE_SUFFIXES):
                continue
            full = Path(dirpath) / fn
            try:
                st = full.stat()
            except OSError:
                continue
            rel = (d_rel / fn).as_posix() if str(d_rel) != "." else fn
            # Use whole-second mtime to match remote `find -printf %T@`.
            out[rel] = (int(st.st_mtime), st.st_size)
    return out


def list_remote() -> dict[str, tuple[int, int]]:
    cmd = [
        "ssh",
        "-i",
        SSH_KEY,
        "-o",
        "IdentitiesOnly=yes",
        f"{REMOTE_USER}@{REMOTE_HOST}",
        # %T@ -> seconds.fraction, %s -> size, %P -> path relative to start
        f"cd {REMOTE_ROOT} && find . -type f -printf '%T@ %s %P\\n' 2>/dev/null",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    out: dict[str, tuple[int, int]] = {}
    for line in res.stdout.splitlines():
        if not line:
            continue
        try:
            mtime_s, size_s, path = line.split(" ", 2)
        except ValueError:
            continue
        out[path] = (int(float(mtime_s)), int(size_s))
    return out


def stream_tar(paths: list[str]) -> int:
    """Tar the given files (relative to LOCAL_ROOT) and untar on remote."""
    if not paths:
        return 0
    with tempfile.NamedTemporaryFile(
        prefix="mmclaw-sync-", suffix=".tar.gz", delete=False
    ) as tmp:
        tmp_path = tmp.name
    try:
        with tarfile.open(tmp_path, "w:gz", compresslevel=3) as tar:
            for rel in paths:
                tar.add(LOCAL_ROOT / rel, arcname=rel)
        size = os.path.getsize(tmp_path)
        # Stream the tar to remote.
        with open(tmp_path, "rb") as fh:
            proc = subprocess.run(
                [
                    "ssh",
                    "-i",
                    SSH_KEY,
                    "-o",
                    "IdentitiesOnly=yes",
                    f"{REMOTE_USER}@{REMOTE_HOST}",
                    f"mkdir -p {REMOTE_ROOT} && tar -xzf - -C {REMOTE_ROOT}",
                ],
                stdin=fh,
                check=True,
            )
        return size
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def delete_remote(paths: list[str]) -> None:
    if not paths:
        return
    # Build a NUL-delimited list and feed to xargs -0 rm.
    payload = "\0".join(paths).encode()
    subprocess.run(
        [
            "ssh",
            "-i",
            SSH_KEY,
            "-o",
            "IdentitiesOnly=yes",
            f"{REMOTE_USER}@{REMOTE_HOST}",
            f"cd {REMOTE_ROOT} && xargs -0 -r rm -f --",
        ],
        input=payload,
        check=True,
    )


def main() -> int:
    start = time.time()
    delete_enabled = "--delete" in sys.argv[1:]
    if not LOCAL_ROOT.is_dir():
        print(f"!! local root missing: {LOCAL_ROOT}", file=sys.stderr)
        return 2
    print(f"[sync] local : {LOCAL_ROOT}")
    print(f"[sync] remote: {REMOTE_USER}@{REMOTE_HOST}:{REMOTE_ROOT}")

    print("[sync] scanning local files...")
    local = list_local()
    print(f"[sync]   {len(local)} local files")

    print("[sync] querying remote files...")
    try:
        remote = list_remote()
    except subprocess.CalledProcessError as exc:
        print(f"!! remote scan failed: {exc.stderr or exc}", file=sys.stderr)
        return 1
    print(f"[sync]   {len(remote)} remote files")

    to_send: list[str] = []
    for rel, (lm, ls) in local.items():
        rm_size = remote.get(rel)
        if rm_size is None:
            to_send.append(rel)
            continue
        rm_mtime, rsz = rm_size
        # Compare size first; fall back to mtime drift > 2s (FAT vs ext4).
        if rsz != ls or abs(rm_mtime - lm) > 2:
            to_send.append(rel)

    to_delete = [rel for rel in remote if rel not in local]

    print(f"[sync] changed/new: {len(to_send)} | stale-on-remote: {len(to_delete)}")

    if to_send:
        sample = ", ".join(to_send[:5]) + ("..." if len(to_send) > 5 else "")
        print(f"[sync]   sending: {sample}")
        size = stream_tar(to_send)
        print(f"[sync]   tar payload: {size/1024:.1f} KiB")

    if to_delete:
        sample = ", ".join(to_delete[:5]) + ("..." if len(to_delete) > 5 else "")
        if delete_enabled:
            print(f"[sync]   deleting on remote: {sample}")
            delete_remote(to_delete)
        else:
            print(f"[sync]   (skipping {len(to_delete)} stale-on-remote; rerun with --delete to remove): {sample}")

    elapsed = time.time() - start
    print(f"[sync] done in {elapsed:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
