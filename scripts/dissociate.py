"""Copy existing Git objects out of alternates, preserving partial-clone packs.

Git 2.34 repack -a -d conflicts with Repo's preciousObjects protection and
repacking partial history tries to read absent promised blobs. Copy immutable
objects instead; Repo removes the alternates file only after this succeeds.
"""
import os
import re
import shutil
import sys
from pathlib import Path


def copy_objects(destination):
    destination = Path(destination).resolve()
    visited = {destination}

    def visit(objects):
        alternate = objects / "info/alternates"
        if alternate.exists():
            for line in alternate.read_text().splitlines():
                source = Path(line)
                if not source.is_absolute():
                    source = objects / source
                source = source.resolve(strict=True)
                if source in visited:
                    continue
                visited.add(source)
                visit(source)
                for directory in source.iterdir():
                    if directory.name != "pack" and not re.fullmatch(r"[0-9a-f]{2}", directory.name):
                        continue
                    for file in directory.iterdir():
                        if not file.is_file() or file.name.startswith("tmp_"):
                            continue
                        target = destination / directory.name / file.name
                        if target.exists():
                            if target.stat().st_size != file.stat().st_size:
                                raise RuntimeError(f"Object collision: {target}")
                            continue
                        target.parent.mkdir(exist_ok=True)
                        temp = target.with_name("tmp_research_" + file.name)
                        shutil.copyfile(file, temp)
                        os.replace(temp, target)

    visit(destination)


if __name__ == "__main__":
    copy_objects(sys.argv[1])
