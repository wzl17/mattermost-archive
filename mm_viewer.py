#!/usr/bin/env python3
"""Browse a channel archive made by mm_archive.py in your web browser.

Usage:
  python3 mm_viewer.py archive              # opens your browser
  python3 mm_viewer.py archive --port 9000 --no-browser

The viewer only answers requests from this computer and opens the archive
read-only. Stop it with Ctrl-C.
"""
import argparse
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import sys
import threading
import urllib.parse
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ALLOWED_HOSTS = ("127.0.0.1", "localhost")
INLINE_UNSAFE = re.compile(r"html|xml|svg|javascript", re.I)


class BadRequest(Exception):
    pass


class Archive:
    def __init__(self, root):
        self.root = os.path.realpath(root)
        self.db_path = os.path.join(self.root, "archive.db")
        self.files_root = os.path.join(self.root, "files")
        if not os.path.exists(self.db_path):
            raise SystemExit(f"No archive.db in {self.root}. Run mm_archive.py first.")

    def conn(self):
        uri = "file:" + urllib.parse.quote(self.db_path) + "?mode=ro"
        c = sqlite3.connect(uri, uri=True)
        c.row_factory = sqlite3.Row
        return c

    # ---------- helpers ----------
    @staticmethod
    def chunks(seq, n=500):
        for i in range(0, len(seq), n):
            yield seq[i:i + n]

    def decorate(self, c, rows, with_replies=False):
        """Turn post rows into dicts with their files, reactions and bot attachments."""
        posts = []
        for r in rows:
            p = {k: r[k] for k in ("id", "root_id", "user_id", "create_at", "edit_at",
                                   "type", "message", "is_pinned", "override_username")}
            props = (json.loads(r["raw"]).get("props") or {}) if r["raw"] else {}
            atts = []
            for a in props.get("attachments") or []:
                if isinstance(a, dict):
                    atts.append({k: a.get(k) for k in (
                        "pretext", "title", "title_link", "text", "color", "author_name")}
                        | {"fields": [{"title": f.get("title"), "value": f.get("value")}
                                      for f in a.get("fields") or [] if isinstance(f, dict)]})
            p["attachments"] = atts
            p["files"], p["reactions"] = [], []
            if with_replies:
                p["replies"], p["last_reply"] = r["replies"], r["last_reply"]
            posts.append(p)
        by_id = {p["id"]: p for p in posts}
        ids = list(by_id)
        for part in self.chunks(ids):
            marks = ",".join("?" * len(part))
            for f in c.execute(f"""SELECT id, post_id, name, mime_type, size, width, height,
                                   path FROM files WHERE post_id IN ({marks})
                                   ORDER BY create_at, id""", part):
                by_id[f["post_id"]]["files"].append(dict(f))
            for x in c.execute(f"""SELECT post_id, emoji_name, GROUP_CONCAT(user_id) AS users
                                   FROM reactions WHERE post_id IN ({marks})
                                   GROUP BY post_id, emoji_name
                                   ORDER BY MIN(COALESCE(create_at, 0))""", part):
                by_id[x["post_id"]]["reactions"].append(
                    {"emoji": x["emoji_name"], "users": x["users"].split(",")})
        return posts

    @staticmethod
    def month_bounds(m):
        if not re.fullmatch(r"\d{4}-\d{2}", m or ""):
            raise BadRequest("month must look like 2023-04")
        y, mo = map(int, m.split("-"))
        start = datetime(y, mo, 1)
        end = datetime(y + (mo == 12), mo % 12 + 1, 1)
        return int(start.timestamp() * 1000), int(end.timestamp() * 1000)

    @staticmethod
    def day_ms(d, end=False):
        try:
            t = datetime.strptime(d, "%Y-%m-%d")
        except ValueError:
            raise BadRequest("dates must look like 2023-04-30")
        return int(((t + timedelta(days=1)) if end else t).timestamp() * 1000)

    @staticmethod
    def fts_query(text):
        text = text.strip()
        if len(text) > 2 and text[0] == text[-1] == '"':
            return '"' + text[1:-1].replace('"', '""') + '"'
        words = re.findall(r"\w+", text)
        return " ".join(f'"{w}"*' for w in words) or None

    # ---------- API ----------
    def info(self, _):
        with self.conn() as c:
            meta = {r["key"]: json.loads(r["value"]) for r in c.execute("SELECT * FROM meta")}
            users = [dict(r) for r in c.execute("""
                SELECT u.id, u.username, u.first_name, u.last_name, u.nickname,
                       (SELECT COUNT(*) FROM posts p WHERE p.user_id = u.id) AS posts
                FROM users u ORDER BY posts DESC, username""")]
            months = [dict(r) for r in c.execute("""
                SELECT strftime('%Y-%m', COALESCE(r.create_at, p.create_at) / 1000,
                                'unixepoch', 'localtime') AS month, COUNT(*) AS posts
                FROM posts p LEFT JOIN posts r ON r.id = p.root_id
                GROUP BY month ORDER BY month""")]
            totals = dict(c.execute("""
                SELECT (SELECT COUNT(*) FROM posts) AS posts,
                       (SELECT COUNT(*) FROM posts WHERE root_id = '') AS threads,
                       (SELECT COUNT(*) FROM files) AS files,
                       (SELECT COUNT(path) FROM files) AS files_saved,
                       (SELECT MIN(create_at) FROM posts) AS first,
                       (SELECT MAX(create_at) FROM posts) AS last""").fetchone())
        ch = meta.get("channel") or {}
        return {"channel": {k: ch.get(k) for k in ("id", "name", "display_name",
                                                    "purpose", "header", "type")},
                "server_url": meta.get("server_url"), "exported_at": meta.get("exported_at"),
                "exported_by": meta.get("exported_by"),
                "users": users, "months": months, "totals": totals}

    def month(self, qs):
        start, end = self.month_bounds(qs.get("m"))
        sys_filter = "" if qs.get("system") else "AND p.type NOT LIKE 'system%'"
        with self.conn() as c:
            rows = c.execute(f"""
                SELECT p.*,
                  (SELECT COUNT(*) FROM posts x WHERE x.root_id = p.id) AS replies,
                  (SELECT MAX(create_at) FROM posts x WHERE x.root_id = p.id) AS last_reply
                FROM posts p LEFT JOIN posts r ON r.id = p.root_id AND p.root_id != ''
                WHERE (p.root_id = '' OR r.id IS NULL)
                  AND p.create_at >= ? AND p.create_at < ? {sys_filter}
                ORDER BY p.create_at, p.id""", (start, end)).fetchall()
            return {"month": qs["m"], "posts": self.decorate(c, rows, with_replies=True)}

    def thread(self, qs):
        rid = qs.get("id") or ""
        sys_filter = "" if qs.get("system") else "AND type NOT LIKE 'system%'"
        with self.conn() as c:
            rows = c.execute(f"""SELECT * FROM posts WHERE root_id = ? {sys_filter}
                                 ORDER BY create_at, id""", (rid,)).fetchall()
            return {"posts": self.decorate(c, rows)}

    def locate(self, qs):
        with self.conn() as c:
            r = c.execute("""
                SELECT p.id, CASE WHEN r.id IS NULL THEN p.id ELSE r.id END AS root,
                       COALESCE(r.create_at, p.create_at) AS thread_at
                FROM posts p LEFT JOIN posts r ON r.id = p.root_id AND p.root_id != ''
                WHERE p.id = ?""", (qs.get("id") or "",)).fetchone()
        if not r:
            raise BadRequest("no such post in this archive")
        return dict(r)

    def search(self, qs):
        text = (qs.get("q") or "").strip()
        where, params = [], []
        if text:
            fts = self.fts_query(text)
            if not fts:
                return {"total": 0, "results": []}
            src = "posts_fts JOIN posts p ON p.rowid = posts_fts.rowid"
            where.append("posts_fts MATCH ?")
            params.append(fts)
            snip = "snippet(posts_fts, 0, char(2), char(3), '…', 30)"
        else:
            src, snip = "posts p", "substr(p.message, 1, 400)"
        if qs.get("user"):
            where.append("p.user_id = ?")
            params.append(qs["user"])
        if qs.get("from"):
            where.append("p.create_at >= ?")
            params.append(self.day_ms(qs["from"]))
        if qs.get("to"):
            where.append("p.create_at < ?")
            params.append(self.day_ms(qs["to"], end=True))
        if not qs.get("system"):
            where.append("p.type NOT LIKE 'system%'")
        cond = " AND ".join(where) or "1"
        order = {"old": "p.create_at ASC",
                 "best": "posts_fts.rank" if text else "p.create_at DESC"
                 }.get(qs.get("sort"), "p.create_at DESC")
        offset = max(0, int(qs.get("offset") or 0))
        with self.conn() as c:
            try:
                total = c.execute(f"SELECT COUNT(*) FROM {src} WHERE {cond}", params).fetchone()[0]
                rows = c.execute(f"""
                    SELECT p.id, p.root_id, p.user_id, p.create_at, p.override_username,
                           p.type, {snip} AS snippet,
                           COALESCE((SELECT r.create_at FROM posts r WHERE r.id = p.root_id),
                                    p.create_at) AS thread_at,
                           (SELECT COUNT(*) FROM files f WHERE f.post_id = p.id) AS files
                    FROM {src} WHERE {cond} ORDER BY {order} LIMIT 50 OFFSET ?""",
                    params + [offset]).fetchall()
            except sqlite3.OperationalError as e:
                raise BadRequest(f"search not understood: {e}")
        return {"total": total, "results": [dict(r) for r in rows]}

    def files(self, qs):
        where, params = [], []
        if qs.get("q"):
            where.append("f.name LIKE ? ESCAPE '\\'")
            params.append("%" + re.sub(r"([%_\\])", r"\\\1", qs["q"].strip()) + "%")
        kind = qs.get("kind")
        if kind == "images":
            where.append("f.mime_type LIKE 'image/%'")
        elif kind == "other":
            where.append("COALESCE(f.mime_type, '') NOT LIKE 'image/%'")
        if qs.get("user"):
            where.append("p.user_id = ?")
            params.append(qs["user"])
        cond = " AND ".join(where) or "1"
        offset = max(0, int(qs.get("offset") or 0))
        with self.conn() as c:
            total = c.execute(f"""SELECT COUNT(*) FROM files f JOIN posts p
                                  ON p.id = f.post_id WHERE {cond}""", params).fetchone()[0]
            rows = c.execute(f"""
                SELECT f.id, f.name, f.mime_type, f.size, f.width, f.height, f.path,
                       f.create_at, p.id AS post_id, p.user_id, p.override_username,
                       COALESCE((SELECT r.create_at FROM posts r WHERE r.id = p.root_id),
                                p.create_at) AS thread_at
                FROM files f JOIN posts p ON p.id = f.post_id WHERE {cond}
                ORDER BY f.create_at DESC LIMIT 100 OFFSET ?""",
                params + [offset]).fetchall()
        return {"total": total, "results": [dict(r) for r in rows]}


class Handler(BaseHTTPRequestHandler):
    archive = None  # set in main()
    routes = {"/api/info": "info", "/api/month": "month", "/api/thread": "thread",
              "/api/locate": "locate", "/api/search": "search", "/api/files": "files"}

    def log_message(self, *args):
        pass

    def send_body(self, code, body, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, obj, code=200):
        self.send_body(code, json.dumps(obj, ensure_ascii=False).encode(),
                       "application/json; charset=utf-8", {"Cache-Control": "no-store"})

    def do_GET(self):
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        if host not in ALLOWED_HOSTS:
            return self.send_body(403, b"forbidden", "text/plain")
        url = urllib.parse.urlsplit(self.path)
        qs = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query).items()}
        try:
            if url.path in ("/", "/index.html"):
                return self.send_body(200, PAGE.encode(), "text/html; charset=utf-8",
                                      {"Cache-Control": "no-store"})
            if url.path in self.routes:
                return self.send_json(getattr(self.archive, self.routes[url.path])(qs))
            if url.path.startswith("/files/"):
                return self.send_file(urllib.parse.unquote(url.path), "download" in qs)
            self.send_json({"error": "not found"}, 404)
        except BadRequest as e:
            self.send_json({"error": str(e)}, 400)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # keep the viewer running whatever happens
            self.send_json({"error": f"{type(e).__name__}: {e}"}, 500)

    def send_file(self, rel, download):
        a = self.archive
        path = os.path.realpath(os.path.join(a.root, rel.lstrip("/")))
        if not path.startswith(a.files_root + os.sep) or not os.path.isfile(path):
            return self.send_json({"error": "file not found"}, 404)
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        name = os.path.basename(path).split("_", 1)[-1]
        disp = "attachment" if download or INLINE_UNSAFE.search(ctype) else "inline"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(os.path.getsize(path)))
        self.send_header("Content-Disposition",
                         f"{disp}; filename*=UTF-8''{urllib.parse.quote(name)}")
        self.send_header("Content-Security-Policy", "sandbox")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "private, max-age=86400")
        self.end_headers()
        with open(path, "rb") as f:
            shutil.copyfileobj(f, self.wfile)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("archive", nargs="?", default="archive",
                    help="archive folder made by mm_archive.py (default: archive)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true", help="don't open a browser")
    args = ap.parse_args()

    Handler.archive = Archive(args.archive)
    server = None
    for port in range(args.port, args.port + 20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if not server:
        raise SystemExit(f"No free port between {args.port} and {args.port + 19}.")
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"Viewing {Handler.archive.root}\nOpen {url}  (Ctrl-C to stop)", flush=True)
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, (url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Channel Archive</title>
<style>
:root {
  --bg: #ffffff; --panel: #f6f7f9; --line: #e3e5e8; --text: #1d2125; --muted: #6b7178;
  --accent: #1c58d9; --accent-soft: #e8effc; --mark: #ffe58a; --code: #f1f2f4;
  --flash: #fff6cc; --sys: #8a9097;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #17191c; --panel: #1f2226; --line: #2f3338; --text: #e3e5e8; --muted: #9aa0a6;
    --accent: #6c9cff; --accent-soft: #23314d; --mark: #6b5a12; --code: #262a2f;
    --flash: #3a3417; --sys: #7d838a;
  }
}
* { box-sizing: border-box; }
html, body { margin: 0; height: 100%; }
body { background: var(--bg); color: var(--text);
  font: 15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  display: grid; grid-template-rows: auto 1fr; }
header { border-bottom: 1px solid var(--line); padding: 10px 16px; display: flex;
  flex-wrap: wrap; gap: 8px 16px; align-items: center; background: var(--panel); }
header h1 { font-size: 17px; margin: 0; white-space: nowrap; }
header .sub { color: var(--muted); font-size: 13px; }
.controls { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin-left: auto; }
input, select, button { font: inherit; font-size: 14px; color: var(--text); background: var(--bg);
  border: 1px solid var(--line); border-radius: 6px; padding: 5px 8px; }
button { cursor: pointer; }
button:hover { border-color: var(--accent); }
button.on { background: var(--accent-soft); border-color: var(--accent); }
#q { width: 260px; }
label.chk { font-size: 13px; color: var(--muted); display: flex; gap: 4px; align-items: center; }
.layout { display: grid; grid-template-columns: 190px 1fr; min-height: 0; }
aside { border-right: 1px solid var(--line); overflow-y: auto; padding: 8px 0; background: var(--panel); }
aside .year { font-weight: 600; padding: 6px 14px 2px; cursor: pointer; user-select: none; }
aside .year span { color: var(--muted); font-weight: 400; font-size: 12px; }
aside a { display: flex; justify-content: space-between; padding: 3px 14px 3px 24px;
  color: var(--text); text-decoration: none; font-size: 14px; }
aside a span { color: var(--muted); font-size: 12px; }
aside a:hover { background: var(--accent-soft); }
aside a.active { background: var(--accent-soft); color: var(--accent); font-weight: 600; }
main { overflow-y: auto; padding: 0 24px 40px; }
.inner { max-width: 900px; margin: 0 auto; }
.viewhead { position: sticky; top: 0; background: var(--bg); padding: 14px 0 10px; z-index: 2;
  display: flex; align-items: baseline; gap: 12px; border-bottom: 1px solid var(--line); }
.viewhead h2 { margin: 0; font-size: 20px; }
.viewhead .muted { flex: 1; }
.muted { color: var(--muted); font-size: 13px; }
.day { display: flex; align-items: center; gap: 10px; color: var(--muted); font-size: 13px;
  font-weight: 600; margin: 22px 0 6px; }
.day::before, .day::after { content: ""; flex: 1; border-top: 1px solid var(--line); }
.post { display: grid; grid-template-columns: 36px 1fr; gap: 10px; padding: 8px 8px;
  border-radius: 8px; }
.post:hover { background: var(--panel); }
.post.flash { background: var(--flash); transition: background 2s; }
.av { width: 36px; height: 36px; border-radius: 50%; color: #fff; font-weight: 600;
  font-size: 14px; display: grid; place-items: center; }
.meta { display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; }
.name { font-weight: 600; }
.time { color: var(--muted); font-size: 12px; }
.tag { font-size: 11px; color: var(--muted); border: 1px solid var(--line); border-radius: 4px;
  padding: 0 4px; }
.msg { white-space: pre-wrap; overflow-wrap: anywhere; }
.msg pre { white-space: pre; overflow-x: auto; background: var(--code); padding: 8px 10px;
  border-radius: 6px; margin: 4px 0; font-size: 13px; }
.msg code { background: var(--code); padding: 1px 4px; border-radius: 4px; font-size: 13px; }
.msg pre code { padding: 0; background: none; }
.msg .q { display: inline-block; border-left: 3px solid var(--line); padding-left: 8px;
  color: var(--muted); }
.msg .h { font-size: 16px; }
.mention { color: var(--accent); }
a { color: var(--accent); }
.post.sys { display: block; color: var(--sys); font-size: 13px; padding: 2px 8px 2px 54px; }
.files { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 6px; }
.files img { max-width: 360px; max-height: 260px; border-radius: 6px; border: 1px solid var(--line);
  display: block; background: var(--panel); }
.file { display: inline-flex; gap: 6px; align-items: center; border: 1px solid var(--line);
  border-radius: 6px; padding: 6px 10px; text-decoration: none; color: var(--text); font-size: 14px; }
.file span { color: var(--muted); font-size: 12px; }
.file.missing { opacity: .6; }
.rxs { display: flex; gap: 4px; flex-wrap: wrap; margin-top: 4px; }
.rx { border: 1px solid var(--line); border-radius: 10px; padding: 0 7px; font-size: 13px; }
.att { border-left: 4px solid var(--line); padding: 4px 10px; margin-top: 6px; }
.att .title { font-weight: 600; }
.att .field b { display: block; font-size: 12px; color: var(--muted); }
button.replies { margin-top: 6px; border: none; padding: 2px 0; background: none;
  color: var(--accent); font-weight: 600; font-size: 13px; }
.thread { border-left: 2px solid var(--line); margin: 4px 0 4px 8px; padding-left: 8px; }
.result { padding: 10px 8px; border-bottom: 1px solid var(--line); cursor: pointer;
  display: grid; grid-template-columns: 36px 1fr; gap: 10px; }
.result:hover { background: var(--panel); }
.result .snip { white-space: pre-wrap; overflow-wrap: anywhere; }
mark { background: var(--mark); color: inherit; border-radius: 2px; }
.more { display: block; margin: 16px auto; }
.nav { display: flex; justify-content: space-between; margin-top: 24px; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(170px, 1fr)); gap: 10px;
  margin-top: 12px; }
.tile { border: 1px solid var(--line); border-radius: 8px; overflow: hidden; cursor: pointer;
  font-size: 13px; background: var(--panel); }
.tile .thumb { height: 120px; display: grid; place-items: center; font-size: 36px;
  background: var(--bg); overflow: hidden; }
.tile .thumb img { width: 100%; height: 100%; object-fit: cover; }
.tile .cap { padding: 6px 8px; overflow-wrap: anywhere; }
.empty { color: var(--muted); padding: 40px 0; text-align: center; }
.err { color: #c0392b; padding: 20px 0; }
@media (max-width: 760px) {
  .layout { grid-template-columns: 1fr; }
  aside { display: none; }
  #q { width: 100%; }
  main { padding: 0 12px 40px; }
}
</style>
</head>
<body>
<header>
  <div>
    <h1 id="title">Channel archive</h1>
    <div class="sub" id="sub"></div>
  </div>
  <div class="controls">
    <input id="q" type="search" placeholder='Search messages (press /)' autocomplete="off">
    <select id="user" title="Only messages from"><option value="">Everyone</option></select>
    <input id="from" type="date" title="From date">
    <input id="to" type="date" title="To date">
    <select id="sort" title="Order of results">
      <option value="new">Newest first</option><option value="old">Oldest first</option>
      <option value="best">Best match</option>
    </select>
    <label class="chk"><input id="system" type="checkbox"> join/leave notices</label>
    <button id="filesbtn" title="Browse all attachments">Files</button>
  </div>
</header>
<div class="layout">
  <aside id="months"></aside>
  <main id="main"><div class="inner" id="view"><div class="empty">Loading…</div></div></main>
</div>
<script>
"use strict";
const S = { info: null, users: {}, months: [], month: null, mode: "month" };
const $ = (s) => document.querySelector(s);
const view = $("#view");

const EMOJI = {"+1":"👍","-1":"👎","thumbsup":"👍","thumbsdown":"👎","thumbs_up":"👍","smile":"😄",
"smiley":"😃","grinning":"😀","grin":"😁","laughing":"😆","joy":"😂","rofl":"🤣","wink":"😉","blush":"😊",
"slightly_smiling_face":"🙂","upside_down_face":"🙃","heart":"❤️","heart_eyes":"😍","thinking_face":"🤔",
"thinking":"🤔","tada":"🎉","clap":"👏","pray":"🙏","ok_hand":"👌","muscle":"💪","fire":"🔥","rocket":"🚀",
"eyes":"👀","100":"💯","white_check_mark":"✅","heavy_check_mark":"✔️","x":"❌","warning":"⚠️",
"question":"❓","exclamation":"❗","sweat_smile":"😅","cry":"😢","sob":"😭","scream":"😱",
"sunglasses":"😎","confused":"😕","neutral_face":"😐","raised_hands":"🙌","wave":"👋","coffee":"☕",
"beers":"🍻","beer":"🍺","star":"⭐","bulb":"💡","memo":"📝","calendar":"📅","point_up":"☝️",
"point_right":"👉","see_no_evil":"🙈","facepalm":"🤦","shrug":"🤷","slightly_frowning_face":"🙁",
"grimacing":"😬","hugs":"🤗","handshake":"🤝","smirk":"😏","relieved":"😌","stuck_out_tongue":"😛",
"zap":"⚡","bug":"🐛","chart_with_upwards_trend":"📈","books":"📚","microscope":"🔬","telescope":"🔭",
"bangbang":"‼️","checkered_flag":"🏁","trophy":"🏆","cake":"🍰","birthday":"🎂","gift":"🎁",
"sparkles":"✨","boom":"💥","sleeping":"😴","robot":"🤖","partying_face":"🥳","raised_hand":"✋",
"ok":"🆗","heavy_plus_sign":"➕","sweat":"😓","innocent":"😇","nerd_face":"🤓","exploding_head":"🤯"};

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c =>
    ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}
async function api(path, params = {}) {
  const u = new URL(path, location.origin);
  for (const [k, v] of Object.entries(params)) if (v !== "" && v != null && v !== false) u.searchParams.set(k, v);
  const r = await fetch(u);
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || r.status);
  return j;
}
function displayName(id, override) {
  if (override) return override;
  const u = S.users[id];
  if (!u) return "unknown user";
  return [u.first_name, u.last_name].filter(Boolean).join(" ") || u.nickname || u.username;
}
function initials(n) { return (n.match(/\b\w/g) || ["?"]).slice(0, 2).join("").toUpperCase(); }
function color(id) {
  let h = 0; for (const c of String(id)) h = (h * 31 + c.charCodeAt(0)) % 360;
  return `hsl(${h} 45% 45%)`;
}
const fmt = (ms, o) => new Date(ms).toLocaleString(undefined, o);
const timeOf = ms => fmt(ms, {hour: "2-digit", minute: "2-digit"});
const fullDate = ms => fmt(ms, {year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"});
const dayOf = ms => fmt(ms, {weekday: "long", year: "numeric", month: "long", day: "numeric"});
const monthKey = ms => { const d = new Date(ms); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`; };
const monthName = m => { const [y, mo] = m.split("-"); return new Date(+y, +mo - 1, 1).toLocaleString(undefined, {month: "long", year: "numeric"}); };
function size(n) {
  if (n == null) return "";
  const u = ["B", "KB", "MB", "GB"]; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i ? n.toFixed(1) : n) + " " + u[i];
}
const fileUrl = p => "/" + p.split("/").map(encodeURIComponent).join("/");
const userLabel = id => { const u = S.users[id]; return u ? "@" + u.username : ""; };

// --- Mattermost-flavoured Markdown, rendered safely (everything is escaped first) ---
function md(src) {
  return String(src || "").split("```").map((seg, i) => {
    if (i % 2) {
      const nl = seg.indexOf("\n");
      const body = nl >= 0 && /^[\w+#.-]*$/.test(seg.slice(0, nl).trim()) ? seg.slice(nl + 1) : seg;
      return `<pre><code>${esc(body.replace(/\n$/, ""))}</code></pre>`;
    }
    return inline(seg);
  }).join("");
}
function inline(t) {
  const keep = [];
  const K = h => { keep.push(h); return `\u0000${keep.length - 1}\u0000`; };
  const link = (u, label) => K(`<a href="${esc(u)}" target="_blank" rel="noopener noreferrer">${esc(label)}</a>`);
  t = t.replace(/`([^`\n]+)`/g, (_, c) => K(`<code>${esc(c)}</code>`));
  t = t.replace(/\[([^\]\n]+)\]\(((?:https?:\/\/|mailto:)[^\s)]+)\)/g, (_, l, u) => link(u, l));
  t = t.replace(/https?:\/\/[^\s<>"'\u0000]+/g, u => {
    const tail = (u.match(/[).,;:!?]+$/) || [""])[0];
    if (tail) u = u.slice(0, -tail.length);
    return link(u, u) + tail;
  });
  t = esc(t);
  t = t.split("\n").map(line => {
    let m;
    if ((m = line.match(/^&gt; ?(.*)$/))) return `<span class="q">${m[1]}</span>`;
    if ((m = line.match(/^#{1,6} (.*)$/))) return `<strong class="h">${m[1]}</strong>`;
    if ((m = line.match(/^(\s*)[-*] (.*)$/))) return `${m[1]}• ${m[2]}`;
    return line;
  }).join("\n");
  t = t.replace(/\*\*(?=\S)([^\n]*?\S)\*\*/g, "<strong>$1</strong>")
       .replace(/~~(?=\S)([^\n]*?\S)~~/g, "<del>$1</del>")
       .replace(/(^|[\s(])_(?=\S)([^\n_]*?\S)_(?=$|[\s).,!?:;])/gm, "$1<em>$2</em>")
       .replace(/(^|[\s(])\*(?=\S)([^\n*]*?\S)\*(?=$|[\s).,!?:;])/gm, "$1<em>$2</em>")
       .replace(/(^|[^\w@])@([a-z0-9][a-z0-9._-]*)/gi, '$1<span class="mention">@$2</span>')
       .replace(/:([a-z0-9_+-]+):/g, (m, n) => EMOJI[n] || m);
  return t.replace(/\u0000(\d+)\u0000/g, (_, i) => keep[+i]);
}

function filesHTML(files) {
  if (!files.length) return "";
  return `<div class="files">` + files.map(f => {
    if (!f.path) return `<span class="file missing" title="Not downloaded">📄 ${esc(f.name || f.id)} <span>not downloaded</span></span>`;
    const url = fileUrl(f.path);
    if ((f.mime_type || "").startsWith("image/") && !/svg/.test(f.mime_type))
      return `<a href="${url}" target="_blank" title="${esc(f.name)}"><img loading="lazy" src="${url}" alt="${esc(f.name)}"></a>`;
    return `<a class="file" href="${url}" target="_blank" title="Open">📄 ${esc(f.name)} <span>${size(f.size)}</span></a>`;
  }).join("") + `</div>`;
}
const ATT_COLORS = {good: "#2fa36b", warning: "#e0a020", danger: "#d24b4e"};
function attColor(c) { c = ATT_COLORS[c] || c; return /^#[0-9a-f]{3,8}$/i.test(c || "") ? c : "var(--line)"; }
function attachmentsHTML(atts) {
  return atts.map(a => {
    const title = a.title ? (/^https?:\/\//.test(a.title_link || "")
      ? `<a class="title" href="${esc(a.title_link)}" target="_blank" rel="noopener noreferrer">${esc(a.title)}</a>`
      : `<div class="title">${esc(a.title)}</div>`) : "";
    const fields = (a.fields || []).map(f => `<div class="field"><b>${esc(f.title)}</b>${md(String(f.value ?? ""))}</div>`).join("");
    return `${a.pretext ? `<div class="msg">${md(a.pretext)}</div>` : ""}<div class="att" style="border-left-color:${attColor(a.color)}">
      ${a.author_name ? `<div class="muted">${esc(a.author_name)}</div>` : ""}${title}
      ${a.text ? `<div class="msg">${md(a.text)}</div>` : ""}${fields}</div>`;
  }).join("");
}
function reactionsHTML(rx) {
  if (!rx.length) return "";
  return `<div class="rxs">` + rx.map(r =>
    `<span class="rx" title="${esc(r.users.map(u => displayName(u)).join(", "))}">${esc(EMOJI[r.emoji] || ":" + r.emoji + ":")} ${r.users.length}</span>`
  ).join("") + `</div>`;
}
function postHTML(p, inThread) {
  if ((p.type || "").startsWith("system"))
    return `<div class="post sys" id="p-${esc(p.id)}"><span class="time">${timeOf(p.create_at)}</span> · ${esc(p.message)}</div>`;
  const name = displayName(p.user_id, p.override_username);
  let h = `<div class="post" id="p-${esc(p.id)}">
    <div class="av" style="background:${color(p.override_username || p.user_id)}">${esc(initials(name))}</div><div>
    <div class="meta"><span class="name" title="${esc(userLabel(p.user_id))}">${esc(name)}</span>
      <span class="time" title="${esc(fullDate(p.create_at))}">${inThread ? fullDate(p.create_at) : timeOf(p.create_at)}</span>
      ${p.override_username ? '<span class="tag">bot</span>' : ""}
      ${p.edit_at ? '<span class="tag" title="Edited ' + esc(fullDate(p.edit_at)) + '">edited</span>' : ""}
      ${p.is_pinned ? '<span class="tag">📌 pinned</span>' : ""}</div>
    <div class="msg">${md(p.message)}</div>${attachmentsHTML(p.attachments)}${filesHTML(p.files)}${reactionsHTML(p.reactions)}`;
  if (p.replies)
    h += `<button class="replies" data-root="${esc(p.id)}">${p.replies} ${p.replies === 1 ? "reply" : "replies"} · last ${esc(fullDate(p.last_reply))} ▸</button>
          <div class="thread" id="t-${esc(p.id)}" hidden></div>`;
  return h + `</div></div>`;
}

// --- views ---
function setHash(h) { history.replaceState(null, "", "#" + h); }
function head(title, sub, extra = "") {
  return `<div class="viewhead"><h2>${title}</h2><span class="muted">${sub}</span>${extra}</div>`;
}
function markMonth(m) {
  document.querySelectorAll("aside a").forEach(a => a.classList.toggle("active", a.dataset.m === m));
  const a = document.querySelector(`aside a[data-m="${m}"]`);
  if (a) a.scrollIntoView({block: "nearest"});
}
async function showMonth(m, focus) {
  S.mode = "month"; S.month = m; $("#filesbtn").classList.remove("on");
  setHash(focus ? "p=" + focus.id : "m=" + m);
  markMonth(m);
  view.innerHTML = `<div class="empty">Loading…</div>`;
  const d = await api("/api/month", {m, system: $("#system").checked ? 1 : ""});
  const i = S.months.indexOf(m), prev = S.months[i - 1], next = S.months[i + 1];
  const navBtns = `${prev ? `<button data-go="${prev}">◂ ${esc(monthName(prev))}</button>` : "<span></span>"}
                   ${next ? `<button data-go="${next}">${esc(monthName(next))} ▸</button>` : "<span></span>"}`;
  let h = head(esc(monthName(m)), `${d.posts.length} conversation${d.posts.length === 1 ? "" : "s"} started`,
               `<span style="display:flex;gap:6px">${navBtns}</span>`);
  let lastDay = "";
  for (const p of d.posts) {
    const day = dayOf(p.create_at);
    if (day !== lastDay) { h += `<div class="day">${esc(day)}</div>`; lastDay = day; }
    h += postHTML(p);
  }
  if (!d.posts.length) h += `<div class="empty">Nothing this month${$("#system").checked ? "" : " (apart from join/leave notices)"}.</div>`;
  h += `<div class="nav">${navBtns}</div>`;
  view.innerHTML = h;
  if (focus) {
    if (focus.root !== focus.id) await openThread(focus.root, true);
    const el = document.getElementById("p-" + focus.id);
    if (el) { el.scrollIntoView({block: "center"}); el.classList.add("flash"); setTimeout(() => el.classList.remove("flash"), 2500); }
  } else $("#main").scrollTop = 0;
}
async function openThread(root, forceOpen) {
  const box = document.getElementById("t-" + root);
  if (!box) return;
  const btn = document.querySelector(`button.replies[data-root="${CSS.escape(root)}"]`);
  if (!box.dataset.loaded) {
    const d = await api("/api/thread", {id: root, system: $("#system").checked ? 1 : ""});
    box.innerHTML = d.posts.map(p => postHTML(p, true)).join("");
    box.dataset.loaded = "1";
  }
  box.hidden = forceOpen ? false : !box.hidden;
  if (btn) btn.textContent = btn.textContent.replace(/[▸▾]$/, box.hidden ? "▸" : "▾");
}
async function goTo(id) {
  const loc = await api("/api/locate", {id});
  await showMonth(monthKey(loc.thread_at), {id: loc.id, root: loc.root});
}

let searchOffset = 0, searchSeq = 0;
function filtersActive() { return $("#q").value.trim() || $("#user").value || $("#from").value || $("#to").value; }
async function runSearch(more) {
  if (!filtersActive()) { if (S.mode === "search") showMonth(S.month || S.months.at(-1)); return; }
  S.mode = "search"; $("#filesbtn").classList.remove("on");
  searchOffset = more ? searchOffset + 50 : 0;
  const seq = ++searchSeq;
  let d;
  try {
    d = await api("/api/search", {q: $("#q").value.trim(), user: $("#user").value, from: $("#from").value,
                                  to: $("#to").value, sort: $("#sort").value, offset: searchOffset,
                                  system: $("#system").checked ? 1 : ""});
  } catch (e) { view.innerHTML = `<div class="err">${esc(e.message)}</div>`; return; }
  if (seq !== searchSeq) return;
  const rows = d.results.map(r => {
    const name = displayName(r.user_id, r.override_username);
    const snip = esc(r.snippet).replace(/\u0002/g, "<mark>").replace(/\u0003/g, "</mark>")
                              .replace(/:([a-z0-9_+-]+):/g, (m, n) => EMOJI[n] || m);
    return `<div class="result" data-id="${esc(r.id)}">
      <div class="av" style="background:${color(r.override_username || r.user_id)}">${esc(initials(name))}</div>
      <div><div class="meta"><span class="name">${esc(name)}</span><span class="time">${esc(fullDate(r.create_at))}</span>
      ${r.root_id ? '<span class="tag">reply in thread</span>' : ""}${r.files ? `<span class="tag">📎 ${r.files}</span>` : ""}</div>
      <div class="snip">${snip || '<span class="muted">(no text)</span>'}</div></div></div>`;
  }).join("");
  const moreBtn = searchOffset + 50 < d.total ? `<button class="more" id="more">Show more</button>` : "";
  if (more) { $("#more")?.remove(); view.insertAdjacentHTML("beforeend", rows + moreBtn); return; }
  $("#main").scrollTop = 0;
  view.innerHTML = head("Search", `${d.total.toLocaleString()} message${d.total === 1 ? "" : "s"} · click one to see it in its conversation`,
                        `<button id="clear">Clear</button>`)
                 + (rows || `<div class="empty">No messages found.</div>`) + moreBtn;
}

let filesOffset = 0;
async function showFiles(more) {
  S.mode = "files"; $("#filesbtn").classList.add("on"); setHash("files");
  markMonth(null);
  filesOffset = more ? filesOffset + 100 : 0;
  const params = {q: $("#fq")?.value || "", kind: $("#fkind")?.value || "", user: $("#user").value, offset: filesOffset};
  const d = await api("/api/files", params);
  const tiles = d.results.map(f => {
    const img = f.path && (f.mime_type || "").startsWith("image/") && !/svg/.test(f.mime_type);
    const thumb = img ? `<img loading="lazy" src="${fileUrl(f.path)}" alt="">` : "📄";
    return `<div class="tile" data-id="${esc(f.post_id)}" title="Show the message">
      <div class="thumb">${thumb}</div>
      <div class="cap">${f.path ? `<a href="${fileUrl(f.path)}?download" title="Download">${esc(f.name || f.id)}</a>` : esc(f.name || f.id) + " (not downloaded)"}
      <div class="muted">${esc(displayName(f.user_id, f.override_username))} · ${esc(fmt(f.create_at, {year: "numeric", month: "short", day: "numeric"}))} · ${size(f.size)}</div></div></div>`;
  }).join("");
  const moreBtn = filesOffset + 100 < d.total ? `<button class="more" id="fmore">Show more</button>` : "";
  if (more) { $("#fmore")?.remove(); $(".grid").insertAdjacentHTML("beforeend", tiles); if (moreBtn) view.insertAdjacentHTML("beforeend", moreBtn); return; }
  $("#main").scrollTop = 0;
  const keepQ = $("#fq")?.value || "", keepKind = $("#fkind")?.value || "";
  view.innerHTML = head("Files", `${d.total.toLocaleString()} attachment${d.total === 1 ? "" : "s"} · click a tile to see its message`,
    `<input id="fq" type="search" placeholder="Filter by file name" value="${esc(keepQ)}">
     <select id="fkind"><option value="">All</option><option value="images">Images</option><option value="other">Documents & other</option></select>`)
    + `<div class="grid">${tiles}</div>` + (d.total ? "" : `<div class="empty">No attachments.</div>`) + moreBtn;
  $("#fkind").value = keepKind;
  const fq = $("#fq"); fq.focus(); fq.setSelectionRange(keepQ.length, keepQ.length);
}

// --- events ---
let timer;
$("#q").addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => runSearch(), 300); });
$("#q").addEventListener("keydown", e => {
  if (e.key === "Enter") { clearTimeout(timer); runSearch(); }
  if (e.key === "Escape") { $("#q").value = ""; runSearch(); }
});
for (const id of ["#user", "#from", "#to", "#sort"])
  $(id).addEventListener("change", () => S.mode === "files" ? showFiles() : runSearch());
$("#system").addEventListener("change", () => S.mode === "search" ? runSearch() : S.mode === "month" ? showMonth(S.month) : null);
$("#filesbtn").addEventListener("click", () => S.mode === "files" ? showMonth(S.month || S.months.at(-1)) : showFiles());
document.addEventListener("keydown", e => {
  if (e.key === "/" && !/INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName)) { e.preventDefault(); $("#q").focus(); }
});
view.addEventListener("input", e => {
  if (e.target.id === "fq") { clearTimeout(timer); timer = setTimeout(() => showFiles(), 300); }
});
view.addEventListener("change", e => { if (e.target.id === "fkind") showFiles(); });
view.addEventListener("click", e => {
  const t = e.target;
  if (t.closest("a")) return;
  const go = t.closest("[data-go]"); if (go) return showMonth(go.dataset.go);
  const rep = t.closest("button.replies"); if (rep) return openThread(rep.dataset.root);
  if (t.id === "more") return runSearch(true);
  if (t.id === "fmore") return showFiles(true);
  if (t.id === "clear") { $("#q").value = $("#from").value = $("#to").value = $("#user").value = ""; return runSearch(); }
  const res = t.closest(".result, .tile"); if (res) return goTo(res.dataset.id);
});
$("#months").addEventListener("click", e => {
  const a = e.target.closest("a[data-m]");
  if (a) { e.preventDefault(); $("#q").value = ""; return showMonth(a.dataset.m); }
  const y = e.target.closest(".year");
  if (y) { const box = y.nextElementSibling; box.hidden = !box.hidden; }
});

// --- start ---
(async function init() {
  try {
    const info = await api("/api/info");
    S.info = info;
    for (const u of info.users) S.users[u.id] = u;
    S.months = info.months.map(m => m.month);
    const ch = info.channel;
    document.title = (ch.display_name || ch.name || "Channel") + " · archive";
    $("#title").textContent = "~ " + (ch.display_name || ch.name || "Channel");
    const t = info.totals;
    $("#sub").textContent = [ch.purpose,
      `${t.posts.toLocaleString()} messages · ${t.files.toLocaleString()} files`,
      t.first ? `${fmt(t.first, {year: "numeric", month: "short"})} – ${fmt(t.last, {year: "numeric", month: "short"})}` : "",
      info.exported_at ? `archived ${new Date(info.exported_at).toLocaleDateString()}` : "export not finished"
    ].filter(Boolean).join(" · ");
    $("#user").insertAdjacentHTML("beforeend", info.users.map(u =>
      `<option value="${esc(u.id)}">${esc(displayName(u.id))} (@${esc(u.username)}) · ${u.posts}</option>`).join(""));
    const years = {};
    for (const m of info.months) (years[m.month.slice(0, 4)] ||= []).push(m);
    $("#months").innerHTML = Object.entries(years).reverse().map(([y, ms]) =>
      `<div class="year">${y} <span>${ms.reduce((s, m) => s + m.posts, 0).toLocaleString()}</span></div><div>` +
      ms.slice().reverse().map(m => `<a href="#m=${m.month}" data-m="${m.month}">${esc(new Date(+y, +m.month.slice(5) - 1, 1).toLocaleString(undefined, {month: "long"}))}<span>${m.posts.toLocaleString()}</span></a>`).join("") +
      `</div>`).join("");
    if (!S.months.length) { view.innerHTML = `<div class="empty">This archive has no messages yet.</div>`; return; }
    await route();
    window.addEventListener("hashchange", () => route().catch(e => view.innerHTML = `<div class="err">${esc(e.message)}</div>`));
  } catch (e) {
    view.innerHTML = `<div class="err">Could not load the archive: ${esc(e.message)}</div>`;
  }
})();
async function route() {
  const h = new URLSearchParams(location.hash.slice(1));
  if (h.get("p")) await goTo(h.get("p"));
  else if (location.hash === "#files") await showFiles();
  else await showMonth(S.months.includes(h.get("m")) ? h.get("m") : S.months.at(-1));
}
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
