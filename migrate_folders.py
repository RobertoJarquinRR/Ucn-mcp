"""One-off migration: consolidate course folders to their short names and
rewrite the paths stored in the downloads table.

    python migrate_folders.py [--apply]

Without --apply it only reports what it would do (dry run).
"""
import shutil
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, ".")
from core.library import DOCS_ROOT, short_course_name
from storage.db import DB


def plan() -> dict:
    """Return {day: {target_folder_name: [source_folder_paths]}}."""
    out: dict[str, dict[str, list[Path]]] = defaultdict(lambda: defaultdict(list))
    if not DOCS_ROOT.exists():
        return out
    for day in sorted(DOCS_ROOT.iterdir()):
        if not day.is_dir():
            continue
        for course_dir in sorted(day.iterdir()):
            if not course_dir.is_dir():
                continue
            short = short_course_name(course_dir.name)
            out[day.name][short].append(course_dir)
    return out


def main(apply: bool = False) -> None:
    p = plan()
    db = DB()
    moves = 0

    for day, groups in p.items():
        for short, sources in groups.items():
            target = DOCS_ROOT / day / short
            dups = [s for s in sources if s.name != short]
            print(f"\n[{day}] {short}")
            for s in sources:
                mark = "(mantener)" if s.name == short else "→ fusionar"
                print(f"   {s.name}  {mark}")

            if not dups:
                continue
            if not apply:
                moves += len(dups)
                continue

            target.mkdir(parents=True, exist_ok=True)
            for s in dups:
                for item in s.rglob("*"):
                    if item.is_dir():
                        continue
                    rel = item.relative_to(s)
                    dest = target / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if dest.exists():
                        dest = dest.with_name(f"{dest.stem}_dup{dest.suffix}")
                    old_path = str(item)
                    shutil.move(old_path, str(dest))
                    # update the DB path for this exact file
                    db.conn.execute("UPDATE downloads SET path=? WHERE path=?",
                                    (str(dest), old_path))
                    moves += 1
                # drop the now-empty source tree
                try:
                    shutil.rmtree(s)
                except OSError:
                    pass
            db.conn.commit()

    print(f"\n{'MOVED' if apply else 'WOULD MOVE'}: {moves} archivos")
    if not apply:
        print("(dry run — vuelve a correr con --apply para aplicar)")


if __name__ == "__main__":
    main(apply="--apply" in sys.argv)
