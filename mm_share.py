#!/usr/bin/env python3
"""Pack a channel archive into one clean .zip that others can unzip and browse.

Usage:
  python3 mm_share.py archive                  # -> <channel>-archive-<date>.zip
  python3 mm_share.py archive -o lab-chat.zip  # choose the zip name
  python3 mm_share.py archive --no-files       # messages only, without attachments
  python3 mm_share.py --code-only              # just the scripts, no chat data at all

The zip holds only what's needed to browse the archive:

  <name>/README.txt            how to open it
  <name>/mm_viewer.py          the viewer
  <name>/archive/archive.db    a fresh, compacted copy of the database
  <name>/archive/files/...     the attachments the database refers to

Nothing else from your folder goes in: no .venv, .DS_Store, .gitignore, git files,
half-downloaded files or anything else lying around. People's profile details
from Mattermost (email address etc.) are dropped from the copy; names stay.
"""
import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import urllib.parse
import zipfile
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
CODE_FILES = ["README.md", ".gitignore", "mm_archive.py", "mm_viewer.py", "mm_share.py"]
# Already-compressed formats: store as-is (compressing again only costs time).
STORED = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".mp4", ".mov", ".m4v",
          ".mp3", ".m4a", ".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar",
          ".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp"}

README = """{title} — Mattermost channel archive
{line}
Archived {exported} · {posts:,} messages · {files}

HOW TO OPEN IT
1. You need Python 3.9 or newer. Macs with the developer tools and most Linux
   systems have it; on Windows, install it from python.org or the Microsoft Store.
2. Unzip this file, open a terminal in the unzipped folder, and run:
       python3 mm_viewer.py archive
   (on Windows:  py mm_viewer.py archive)
3. Your web browser opens the archive: months on the left, threads expand in
   place, search with the box at the top (or press /), and "Files" lists every
   attachment. Stop the viewer with Ctrl-C in the terminal.

Everything stays on your computer: the viewer works offline, only answers your
own computer, and never changes the archive. archive/archive.db is an ordinary
SQLite database if you'd rather query it directly (start with the "messages" view).

PRIVACY
This is the private chat history of the channel's members. Please keep it to
the people it was shared with.
"""


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def safe(s):
    return re.sub(r"[^\w.-]+", "-", s or "").strip("-.") or "channel"


def finish_zip(tmp_zip, out):
    os.replace(tmp_zip, out)
    log(f"\nWrote {os.path.abspath(out)} ({human(os.path.getsize(out))})")


def pack_code(out):
    out = out or "mattermost-archive-tools.zip"
    tmp_zip = out + ".part"
    with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as z:
        for f in CODE_FILES:
            if os.path.isfile(os.path.join(HERE, f)):
                z.write(os.path.join(HERE, f), f"mattermost-archive/{f}")
                log(f"  {f}")
    finish_zip(tmp_zip, out)


def pack_archive(src, out, no_files, force):
    src = os.path.realpath(src)
    db_path = os.path.join(src, "archive.db")
    if not os.path.isfile(db_path):
        raise SystemExit(f"No archive.db in {src}. Give the folder made by mm_archive.py.")
    uri = "file:" + urllib.parse.quote(db_path) + "?mode=ro"

    con = sqlite3.connect(uri, uri=True)
    meta = {k: json.loads(v) for k, v in con.execute("SELECT key, value FROM meta")}
    if not meta.get("exported_at") or not (meta.get("fetch_state") or {}).get("done"):
        msg = "The export in this folder hasn't finished; run mm_archive.py again first"
        if not force:
            raise SystemExit(msg + " (or add --force to pack it as it is).")
        log(f"Warning: {msg}.")
    posts = con.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    files = [] if no_files else con.execute(
        "SELECT path, COALESCE(size, 0) FROM files WHERE path IS NOT NULL ORDER BY path").fetchall()
    not_downloaded = con.execute("SELECT COUNT(*) FROM files WHERE path IS NULL").fetchone()[0]
    con.close()

    channel = meta.get("channel") or {}
    stamp = (meta.get("exported_at") or "")[:10] or date.today().isoformat()
    out = out or f"{safe(channel.get('name'))}-archive-{stamp}.zip"
    if not out.lower().endswith(".zip"):
        out += ".zip"
    top = safe(os.path.splitext(os.path.basename(out))[0])

    need = os.path.getsize(db_path) + sum(s for _, s in files)
    free = shutil.disk_usage(os.path.dirname(os.path.abspath(out))).free
    if need > free:
        raise SystemExit(f"Not enough disk space: the zip needs up to {human(need)}, "
                         f"{human(free)} free.")

    tmp_zip = out + ".part"
    try:
        with tempfile.TemporaryDirectory() as tmp:
            # Fresh copy of the database, so the original is never touched.
            clean_db = os.path.join(tmp, "archive.db")
            con = sqlite3.connect(uri, uri=True)
            con.execute("VACUUM INTO ?", (clean_db,))
            con.close()
            con = sqlite3.connect(clean_db)
            con.execute("UPDATE users SET raw = NULL")  # drop emails etc., keep names
            con.execute("DELETE FROM meta WHERE key = 'fetch_state'")
            if no_files:
                con.execute("UPDATE files SET path = NULL")
            con.commit()
            con.execute("VACUUM")  # really remove the dropped data from the file
            con.close()

            log(f"Packing {posts:,} messages"
                + ("" if no_files else f" and {len(files):,} attachments") + "...")
            n_files, missing = 0, 0
            with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
                n_label = ("attachments not included" if no_files
                           else f"{len(files):,} attachments")
                z.writestr(f"{top}/README.txt", README.format(
                    title=channel.get("display_name") or channel.get("name") or "Channel",
                    line="=" * 60, exported=stamp, posts=posts, files=n_label))
                z.write(os.path.join(HERE, "mm_viewer.py"), f"{top}/mm_viewer.py")
                z.write(clean_db, f"{top}/archive/archive.db")
                for rel, _ in files:
                    path = os.path.realpath(os.path.join(src, rel))
                    if not path.startswith(src + os.sep) or not os.path.isfile(path):
                        missing += 1
                        continue
                    kind = (zipfile.ZIP_STORED if os.path.splitext(path)[1].lower() in STORED
                            else zipfile.ZIP_DEFLATED)
                    z.write(path, f"{top}/archive/{rel}", compress_type=kind)
                    n_files += 1
                    if n_files % 200 == 0:
                        log(f"  {n_files:,}/{len(files):,} attachments")
        finish_zip(tmp_zip, out)
    except BaseException:
        if os.path.exists(tmp_zip):
            os.remove(tmp_zip)
        raise

    if missing:
        log(f"Note: {missing} attachment(s) listed in the database are missing from the "
            f"folder and were left out.")
    if not_downloaded and not no_files:
        log(f"Note: {not_downloaded} attachment(s) were never downloaded; "
            f"run mm_archive.py again to fetch them.")
    log("This zip contains private chat history: share it only with the people it's meant for.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("archive", nargs="?", help="archive folder made by mm_archive.py")
    ap.add_argument("-o", "--output", help="zip file to write (default: named after the channel)")
    ap.add_argument("--no-files", action="store_true", help="leave out the attachments")
    ap.add_argument("--code-only", action="store_true",
                    help="pack only the scripts and README, without any chat data")
    ap.add_argument("--force", action="store_true", help="pack an unfinished export anyway")
    args = ap.parse_args()

    try:
        if args.code_only:
            pack_code(args.output)
        elif args.archive:
            pack_archive(args.archive, args.output, args.no_files, args.force)
        else:
            ap.error("give the archive folder (e.g. archive), or --code-only")
    except KeyboardInterrupt:
        raise SystemExit("\nStopped; no zip was written.")


if __name__ == "__main__":
    main()
