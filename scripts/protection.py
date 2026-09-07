"""Read-only inventories of the reference tree, excluding access times."""
import concurrent.futures
import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path


def git(path, *args):
    result = subprocess.run(["git", "-C", str(path), *args], check=True,
                            capture_output=True, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    return result.stdout


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def project_record(root, rel):
    path = root / rel
    status = git(path, "status", "--porcelain=v1", "-z", "-uall")
    untracked = git(path, "ls-files", "--others", "--exclude-standard", "-z")
    files = {}
    for raw in untracked.split(b"\0"):
        if not raw:
            continue
        p = path / os.fsdecode(raw)
        if p.is_symlink():
            files[os.fsdecode(raw)] = {"symlink": os.readlink(p)}
        elif p.is_file():
            files[os.fsdecode(raw)] = {"size": p.stat().st_size, "sha256": sha256(p)}
    return {
        "path": rel,
        "head": git(path, "rev-parse", "HEAD").decode().strip(),
        "status": os.fsdecode(status),
        "diff_sha256": hashlib.sha256(git(path, "diff", "--binary", "HEAD", "--")).hexdigest(),
        "untracked": files,
    }


def source_inventory(root):
    root = Path(root)
    projects = (root / ".repo/project.list").read_text().splitlines()
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        return list(pool.map(lambda rel: project_record(root, rel), projects))


def output_inventory(root):
    root = Path(root)
    h = hashlib.sha256()
    count = 0
    total = 0
    for parent, dirs, files in os.walk(root, followlinks=False):
        dirs.sort()
        for name in sorted(dirs + files):
            path = Path(parent) / name
            s = path.lstat()
            # Directory timestamps can change due to external indexers; retain
            # file/symlink metadata and all names, which cover build artifacts.
            row = [str(path.relative_to(root)), stat.S_IFMT(s.st_mode)]
            if not stat.S_ISDIR(s.st_mode):
                row += [s.st_size, s.st_mtime_ns, stat.S_IMODE(s.st_mode)]
                if path.is_symlink():
                    row.append(os.readlink(path))
                else:
                    total += s.st_size
                count += 1
            h.update(json.dumps(row, ensure_ascii=True).encode() + b"\n")
    images = {}
    for product in (root / "target/product").glob("*"):
        for name in ("system.img", "super.img", "product.img", "vendor.img", "ramdisk.img", "kernel-ranchu"):
            p = product / name
            if p.is_file():
                images[str(p.relative_to(root))] = sha256(p)
    return {"files": count, "logical_bytes": total,
            "metadata_sha256": h.hexdigest(), "image_sha256": images}


def inventory(root):
    return {"projects": source_inventory(root), "out": output_inventory(Path(root) / "out")}
