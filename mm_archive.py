#!/usr/bin/env python3
"""Archive one Mattermost channel into a SQLite database, as a regular user.

No admin rights needed: it reads through the REST API (v4) with your own login,
so you must be able to see the channel (be a member, for private channels).

Auth: the script asks for a token when it starts (typing is hidden, and it is
never saved or printed). Any of these works:
  * the MMAUTHTOKEN cookie from your logged-in browser (works with SSO);
  * a personal access token (Profile > Security), if your admin enabled them.
For unattended runs you can set the MM_TOKEN environment variable instead.

Usage:
  python3 mm_archive.py --url https://chat.example.org \
      --team myteam --channel my-channel --out archive
  python3 mm_archive.py --url https://chat.example.org \
      --channel-id 4xp9fdt9jbyb5x3w8k1d1zdo3e --out archive

Then browse it with:  python3 mm_viewer.py archive

Output (in --out):
  archive.db   SQLite database: posts, users, files, reactions, full-text index
               (every post's complete API data is kept in posts.raw)
  files/       attachments, as files/<year>/<month>/<file_id>_<name>

Interrupted? Run the same command again: it continues where it stopped.
Running it again after a finished export refreshes everything (picks up edits,
new messages and reactions) and only downloads attachments it doesn't have yet.
"""
import argparse
import getpass
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from http.client import HTTPException

PER_PAGE = 200  # API maximum
SCHEMA_VERSION = "1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY, username TEXT, first_name TEXT, last_name TEXT,
    nickname TEXT, raw TEXT);
CREATE TABLE IF NOT EXISTS posts (
    id TEXT PRIMARY KEY,
    root_id TEXT NOT NULL DEFAULT '',       -- '' for thread starters
    user_id TEXT,
    create_at INTEGER NOT NULL,             -- milliseconds since 1970 (UTC)
    edit_at INTEGER NOT NULL DEFAULT 0,
    type TEXT NOT NULL DEFAULT '',          -- 'system_...' for join/leave etc.
    message TEXT NOT NULL DEFAULT '',
    is_pinned INTEGER NOT NULL DEFAULT 0,
    reply_count INTEGER NOT NULL DEFAULT 0,
    override_username TEXT,                 -- name shown by bots/webhooks
    raw TEXT NOT NULL);                     -- complete post JSON from the API
CREATE INDEX IF NOT EXISTS posts_by_time ON posts(create_at);
CREATE INDEX IF NOT EXISTS posts_by_root ON posts(root_id, create_at);
CREATE INDEX IF NOT EXISTS posts_by_user ON posts(user_id, create_at);
CREATE TABLE IF NOT EXISTS files (
    id TEXT PRIMARY KEY, post_id TEXT NOT NULL, create_at INTEGER,
    name TEXT, extension TEXT, mime_type TEXT, size INTEGER,
    width INTEGER, height INTEGER,
    path TEXT,      -- relative to the archive folder; NULL until downloaded
    error TEXT);    -- why the download failed, if it did
CREATE INDEX IF NOT EXISTS files_by_post ON files(post_id);
CREATE TABLE IF NOT EXISTS reactions (
    post_id TEXT NOT NULL, user_id TEXT NOT NULL, emoji_name TEXT NOT NULL,
    create_at INTEGER, PRIMARY KEY (post_id, user_id, emoji_name));
CREATE VIRTUAL TABLE IF NOT EXISTS posts_fts USING fts5(
    message, content='posts', content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 2');
CREATE VIEW IF NOT EXISTS messages AS
    SELECT p.id,
           datetime(p.create_at / 1000, 'unixepoch', 'localtime') AS time,
           COALESCE(p.override_username, u.username, p.user_id) AS user,
           p.message, NULLIF(p.root_id, '') AS thread, p.type
    FROM posts p LEFT JOIN users u ON u.id = p.user_id;
"""

POST_UPSERT = """
INSERT INTO posts (id, root_id, user_id, create_at, edit_at, type, message,
                   is_pinned, reply_count, override_username, raw)
VALUES (?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(id) DO UPDATE SET
    root_id=excluded.root_id, user_id=excluded.user_id,
    create_at=excluded.create_at, edit_at=excluded.edit_at, type=excluded.type,
    message=excluded.message, is_pinned=excluded.is_pinned,
    reply_count=excluded.reply_count,
    override_username=excluded.override_username, raw=excluded.raw
"""

FILE_UPSERT = """
INSERT INTO files (id, post_id, create_at, name, extension, mime_type, size,
                   width, height)
VALUES (?,?,?,?,?,?,?,?,?)
ON CONFLICT(id) DO UPDATE SET
    post_id=excluded.post_id, create_at=excluded.create_at,
    name=COALESCE(excluded.name, files.name),
    extension=COALESCE(excluded.extension, files.extension),
    mime_type=COALESCE(excluded.mime_type, files.mime_type),
    size=COALESCE(excluded.size, files.size),
    width=COALESCE(excluded.width, files.width),
    height=COALESCE(excluded.height, files.height)
"""


def log(msg):
    print(msg, file=sys.stderr, flush=True)


class ApiError(Exception):
    def __init__(self, code, method, path, detail):
        super().__init__(f"{method} {path} -> HTTP {code}: {detail[:300]}")
        self.code = code


class Client:
    def __init__(self, base_url, token):
        self.base = base_url.rstrip("/") + "/api/v4"
        self.token = token

    def _open(self, method, path, body=None):
        """Send a request, waiting out rate limits and retrying network errors."""
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(10):
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("Authorization", f"Bearer {self.token}")
            req.add_header("X-Requested-With", "XMLHttpRequest")
            if data is not None:
                req.add_header("Content-Type", "application/json")
            try:
                return urllib.request.urlopen(req, timeout=120)
            except urllib.error.HTTPError as e:
                if e.code == 429 or e.code >= 500:
                    reset = e.headers.get("X-RateLimit-Reset")
                    wait = int(reset) if reset and reset.isdigit() else 2 ** attempt
                    time.sleep(min(max(wait, 1), 60))
                    continue
                raise ApiError(e.code, method, path, e.read().decode(errors="replace"))
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                log(f"  network problem ({e}); retrying...")
                time.sleep(min(2 ** attempt, 60))
        raise SystemExit(f"{method} {path}: giving up after repeated failures")

    def get(self, path):
        with self._open("GET", path) as r:
            return json.loads(r.read())

    def post(self, path, body):
        with self._open("POST", path, body) as r:
            return json.loads(r.read())

    def download(self, path, dest):
        """Stream a file to disk; retries if the connection drops midway."""
        tmp = dest + ".part"
        for attempt in range(5):
            try:
                with self._open("GET", path) as r, open(tmp, "wb") as f:
                    while chunk := r.read(1 << 20):
                        f.write(chunk)
                os.replace(tmp, dest)
                return
            except (OSError, HTTPException) as e:
                if attempt == 4:
                    raise ApiError(0, "GET", path, f"download failed: {e}")
                time.sleep(2 ** attempt)


def q(s):
    return urllib.parse.quote(s, safe="")


def safe_name(name):
    name = re.sub(r"[^\w.\- ]+", "_", name or "").strip(" .") or "file"
    return name[-120:]


def get_meta(db, key, default=None):
    row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def set_meta(db, key, value):
    db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
               (key, json.dumps(value, ensure_ascii=False)))


def store_posts(db, channel_id, posts):
    n = 0
    for p in posts:
        if p.get("channel_id") != channel_id or p.get("delete_at"):
            continue
        props = p.get("props") or {}
        md = p.get("metadata") or {}
        db.execute(POST_UPSERT, (
            p["id"], p.get("root_id") or "", p.get("user_id"), p["create_at"],
            p.get("edit_at") or 0, p.get("type") or "", p.get("message") or "",
            1 if p.get("is_pinned") else 0, p.get("reply_count") or 0,
            props.get("override_username") or None,
            json.dumps(p, ensure_ascii=False)))
        infos = {f["id"]: f for f in md.get("files") or []}
        for fid in dict.fromkeys(list(p.get("file_ids") or []) + list(infos)):
            f = infos.get(fid, {})
            db.execute(FILE_UPSERT, (
                fid, p["id"], f.get("create_at") or p["create_at"], f.get("name"),
                f.get("extension"), f.get("mime_type"), f.get("size"),
                f.get("width"), f.get("height")))
        db.execute("DELETE FROM reactions WHERE post_id = ?", (p["id"],))
        for r in md.get("reactions") or []:
            db.execute("INSERT OR IGNORE INTO reactions VALUES (?,?,?,?)",
                       (p["id"], r["user_id"], r["emoji_name"], r.get("create_at")))
        n += 1
    return n


def fetch_posts(c, db, channel_id):
    """Walk backwards through the channel, saving after every page."""
    state = get_meta(db, "fetch_state") or {}
    cursor = None
    if state.get("cursor") and not state.get("done"):
        cursor = state["cursor"]
        log("  resuming where the last run stopped")
    pages = 0
    while True:
        path = f"/channels/{channel_id}/posts?per_page={PER_PAGE}"
        if cursor:
            path += f"&before={cursor}"
        try:
            r = c.get(path)
        except ApiError as e:
            if cursor and e.code in (400, 403, 404):
                log("  resume point no longer exists; starting from the newest post")
                cursor = None
                continue
            raise
        order = r.get("order") or []
        if not order or order[-1] == cursor:
            break
        store_posts(db, channel_id, (r.get("posts") or {}).values())
        cursor = order[-1]
        set_meta(db, "fetch_state", {"cursor": cursor, "done": False})
        db.commit()
        pages += 1
        oldest = db.execute("SELECT create_at FROM posts WHERE id = ?", (cursor,)).fetchone()
        when = (datetime.fromtimestamp(oldest[0] / 1000).strftime("%Y-%m-%d")
                if oldest else "?")
        total = db.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        log(f"  page {pages}: back to {when}, {total} posts saved")
    set_meta(db, "fetch_state", {"cursor": None, "done": True})
    db.commit()

    # Safety net: fetch whole threads whose replies look incomplete.
    roots = [r[0] for r in db.execute("""
        SELECT p.id FROM posts p WHERE p.root_id = '' AND p.reply_count >
            (SELECT COUNT(*) FROM posts x WHERE x.root_id = p.id)""")]
    for i, rid in enumerate(roots, 1):
        try:
            t = c.get(f"/posts/{rid}/thread")
        except ApiError as e:
            log(f"  could not load thread {rid}: {e}")
            continue
        store_posts(db, channel_id, (t.get("posts") or {}).values())
        if i % 50 == 0:
            db.commit()
    if roots:
        log(f"  checked {len(roots)} threads for missing replies")
    db.commit()


def fetch_users(c, db):
    ids = [r[0] for r in db.execute(
        "SELECT user_id FROM posts WHERE user_id IS NOT NULL "
        "UNION SELECT user_id FROM reactions")]
    for i in range(0, len(ids), 100):
        for u in c.post("/users/ids", ids[i:i + 100]):
            db.execute("INSERT OR REPLACE INTO users VALUES (?,?,?,?,?,?)", (
                u["id"], u.get("username"), u.get("first_name"),
                u.get("last_name"), u.get("nickname"),
                json.dumps(u, ensure_ascii=False)))
    db.commit()
    return len(ids)


def fetch_file_infos(c, db):
    missing = [r[0] for r in db.execute("SELECT id FROM files WHERE name IS NULL")]
    for fid in missing:
        try:
            f = c.get(f"/files/{fid}/info")
            db.execute("""UPDATE files SET name=?, extension=?, mime_type=?, size=?,
                          width=?, height=? WHERE id=?""",
                       (f.get("name"), f.get("extension"), f.get("mime_type"),
                        f.get("size"), f.get("width"), f.get("height"), fid))
        except ApiError as e:
            db.execute("UPDATE files SET error=? WHERE id=?", (str(e), fid))
    db.commit()


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def download_files(c, db, out, workers):
    rows = db.execute("SELECT id, name, create_at, size FROM files "
                      "WHERE path IS NULL ORDER BY create_at").fetchall()
    if not rows:
        log("  all attachments already downloaded")
        return
    need = sum(r[3] or 0 for r in rows)
    free = shutil.disk_usage(out).free
    log(f"  {len(rows)} to download, about {human(need)} ({human(free)} free on disk)")
    if need > free:
        raise SystemExit("Not enough disk space for the attachments. Free some space, "
                         "or run with --no-files to export only the messages.")

    jobs = {}
    for fid, name, create_at, _ in rows:
        d = datetime.fromtimestamp((create_at or 0) / 1000, tz=timezone.utc)
        rel = f"files/{d:%Y}/{d:%m}/{fid}_{safe_name(name or fid)}"
        dest = os.path.join(out, rel)
        if os.path.exists(dest):
            db.execute("UPDATE files SET path=?, error=NULL WHERE id=?", (rel, fid))
            continue
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        jobs[fid] = (rel, dest)
    db.commit()

    done = failed = 0
    ex = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {ex.submit(c.download, f"/files/{fid}", dest): (fid, rel)
                   for fid, (rel, dest) in jobs.items()}
        for fut in as_completed(futures):
            fid, rel = futures[fut]
            try:
                fut.result()
                db.execute("UPDATE files SET path=?, error=NULL WHERE id=?", (rel, fid))
                done += 1
            except ApiError as e:
                db.execute("UPDATE files SET error=? WHERE id=?", (str(e), fid))
                failed += 1
                log(f"  could not download {rel}: {e}")
            if (done + failed) % 25 == 0:
                db.commit()
                log(f"  {done + failed}/{len(jobs)} attachments")
    finally:
        ex.shutdown(wait=True, cancel_futures=True)
        db.commit()
    log(f"  downloaded {done}" + (f", {failed} failed (run again to retry)" if failed else ""))


def keep_out_of_git(out):
    """Put a .gitignore that ignores everything into the archive folder, so the
    private chat data is never committed by accident, whatever the folder is called."""
    out = os.path.realpath(out)
    if out in (os.path.realpath(os.getcwd()),
               os.path.realpath(os.path.dirname(os.path.abspath(__file__)))):
        return  # never hide the project itself
    path = os.path.join(out, ".gitignore")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write("# Private Mattermost archive: keep everything here out of git.\n*\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="server URL, e.g. https://chat.example.org")
    ap.add_argument("--team", help="team name (from the URL, not the display name)")
    ap.add_argument("--channel", help="channel name (from the URL)")
    ap.add_argument("--channel-id", help="channel id, instead of --team/--channel")
    ap.add_argument("--out", default="archive", help="output folder (default: archive)")
    ap.add_argument("--no-files", action="store_true", help="skip downloading attachments")
    ap.add_argument("--workers", type=int, default=4,
                    help="parallel attachment downloads (default: 4)")
    args = ap.parse_args()

    if not args.channel_id and not (args.team and args.channel):
        ap.error("give --channel-id, or both --team and --channel")
    token = os.environ.get("MM_TOKEN", "").strip()
    if not token:
        try:
            token = getpass.getpass("Mattermost token (typing is hidden): ").strip()
        except (EOFError, KeyboardInterrupt):
            token = ""
        if not token:
            raise SystemExit("No token given.")

    c = Client(args.url, token)
    try:
        me = c.get("/users/me")
    except ApiError as e:
        if e.code == 401:
            raise SystemExit("Login failed: the token is wrong or has expired.")
        raise SystemExit(str(e))
    log(f"Logged in as @{me['username']}")

    try:
        if args.channel_id:
            channel = c.get(f"/channels/{q(args.channel_id)}")
        else:
            team = c.get(f"/teams/name/{q(args.team)}")
            channel = c.get(f"/teams/{team['id']}/channels/name/{q(args.channel)}")
    except ApiError as e:
        raise SystemExit(f"Could not open the channel (check the names, and that you "
                         f"are a member): {e}")
    log(f"Channel: {channel.get('display_name')} ({channel['id']})")

    os.makedirs(args.out, exist_ok=True)
    keep_out_of_git(args.out)
    db = sqlite3.connect(os.path.join(args.out, "archive.db"))
    db.executescript(SCHEMA)
    known = get_meta(db, "channel")
    if known and known.get("id") != channel["id"]:
        raise SystemExit(f"{args.out} already holds an archive of a different channel "
                         f"({known.get('display_name')}). Use another --out folder.")
    set_meta(db, "schema_version", SCHEMA_VERSION)
    set_meta(db, "channel", channel)
    set_meta(db, "server_url", args.url.rstrip("/"))
    set_meta(db, "exported_by", me["username"])
    db.commit()

    try:
        log("Fetching posts...")
        fetch_posts(c, db, channel["id"])
        log("Fetching users...")
        fetch_users(c, db)
        if not args.no_files:
            log("Downloading attachments...")
            fetch_file_infos(c, db)
            download_files(c, db, args.out, max(1, args.workers))
        log("Building search index...")
        db.execute("INSERT INTO posts_fts (posts_fts) VALUES ('rebuild')")
        set_meta(db, "exported_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        db.commit()
    except ApiError as e:
        db.commit()
        if e.code == 401:
            raise SystemExit("The login token expired during the export. Get a new one "
                             "and run the same command again to continue.")
        raise SystemExit(f"{e}\nRun the same command again to continue.")
    except KeyboardInterrupt:
        db.commit()
        raise SystemExit("\nStopped. Run the same command again to continue.")

    posts, threads = db.execute(
        "SELECT COUNT(*), SUM(root_id = '') FROM posts").fetchone()
    files, got, size = db.execute(
        "SELECT COUNT(*), COUNT(path), COALESCE(SUM(CASE WHEN path IS NOT NULL "
        "THEN size END), 0) FROM files").fetchone()
    log(f"\nDone: {posts} posts in {threads} threads, "
        f"{got}/{files} attachments ({human(size)}).")
    if channel.get("total_msg_count"):
        log(f"(The server counts {channel['total_msg_count']} messages in this channel; "
            f"the figures can differ a little because of deleted posts.)")
    log(f"Browse it with:  python3 {os.path.join(os.path.dirname(sys.argv[0]) or '.', 'mm_viewer.py')} {args.out}")


if __name__ == "__main__":
    main()
