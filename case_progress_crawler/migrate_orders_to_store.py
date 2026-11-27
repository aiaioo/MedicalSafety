#!/usr/bin/env python3
"""Move the order files saved as case_details/consumer/<case key>/orders/<name> into the shared order store
(case_details/store/, one file per distinct content) and record them in the case_orders table.

Safe to run again, and the deploy runs it every time. By default the old files stay: each stored file is a hard link
to one of them, so this takes no extra disk space. With --remove-originals each old file is then deleted once the
stored file has been read back and matches it, and the database holds its reference; that is what frees the space
the copies took. Do that once the new code is what is running (the old code reads only the old files).

It takes the daily crawl's lock (.crawl.lock), and does nothing while a crawl is running.

Usage:  python migrate_orders_to_store.py [--remove-originals] [--dry-run]
"""

import argparse
import fcntl
import hashlib
import os
import sys

import progress_storage as storage
from judgements import ORDER_RE

BATCH = 1000  # references recorded per transaction, and old files deleted only after theirs are


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(1 << 20):
            h.update(block)
    return h.hexdigest()


def old_files():
    """(case key, order name, path) of every file in the old folders."""
    for folder in sorted(storage.CASE_DETAILS_DIR.glob("*/orders")):
        for path in sorted(folder.iterdir()):
            yield folder.parent.name, path.name, path


def put_in_store(path, sha256, name, verified):
    """Make sure the store holds this content, linking `path` into it if it is missing or damaged."""
    dest = storage.stored_order_path(sha256, name)
    if sha256 in verified:
        return
    if not dest.is_file() or digest(dest) != sha256:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f"{dest.name}.{os.getpid()}.part")
        tmp.unlink(missing_ok=True)
        os.link(path, tmp)
        os.replace(tmp, dest)
    verified.add(sha256)


def remove(paths):
    for path in paths:
        path.unlink()
    for folder in {p.parent for p in paths}:  # then the case's folders, if nothing else is in them
        for d in (folder, folder.parent):
            if not any(d.iterdir()):
                d.rmdir()


def migrate(remove_originals, dry_run):
    # not worth reading every reference when there is nothing left to move, as at most deploys
    refs = storage.load_order_refs() if next(old_files(), None) else {}
    counts = dict.fromkeys(("found", "recorded", "removed", "skipped"), 0)
    problems, verified, rows, done = [], set(), [], []

    def flush():
        if rows:
            storage.add_order_refs(rows)
            counts["recorded"] += len(rows)
            rows.clear()
        if done:
            remove(done)
            counts["removed"] += len(done)
            done.clear()

    for case_key, name, path in old_files():
        if not path.is_file() or not ORDER_RE.fullmatch(name):
            problems.append(f"{path}: not an order file, left alone")
            continue
        counts["found"] += 1
        size = path.stat().st_size
        ref = refs.get((case_key, name))
        if not remove_originals and ref and ref[1] == size and storage.stored_order_path(ref[0], name).is_file():
            counts["skipped"] += 1  # imported before; --remove-originals compares the contents
            continue
        sha256 = digest(path)
        if ref and ref[0] != sha256:
            problems.append(f"{path}: differs from the stored order of that name, left alone")
            continue
        if dry_run:
            continue
        put_in_store(path, sha256, name, verified)
        if not ref:
            rows.append((case_key, name, sha256, size))
        if remove_originals:
            done.append(path)
        if len(rows) >= BATCH or len(done) >= BATCH:
            flush()
    flush()
    return counts, problems


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--remove-originals", action="store_true", help="delete each old file once it is safely in the store")
    ap.add_argument("--dry-run", action="store_true", help="change nothing; count the files and report problems")
    args = ap.parse_args()
    with open(storage.BASE_DIR / ".crawl.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("a crawl is running; run this again when it has finished", file=sys.stderr)
            return 1
        counts, problems = migrate(args.remove_originals, args.dry_run)
    for p in problems:
        print(f"!! {p}", file=sys.stderr)
    print(f"order files: {counts['found']} in the old folders, {counts['recorded']} newly recorded, {counts['skipped']} already in the store, "
          f"{counts['removed']} old files removed, {len(problems)} left alone")
    return 0  # a file left alone doesn't stop a deploy; anything that goes wrong in the copying raises


if __name__ == "__main__":
    sys.exit(main())
