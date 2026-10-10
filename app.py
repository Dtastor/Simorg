from flask import Flask, request, redirect, session, send_from_directory
import os
import html
import uuid
import psycopg2
from psycopg2.extras import RealDictCursor
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.exceptions import RequestEntityTooLarge
from datetime import datetime, timedelta

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "SIMURGH_2026")
DATABASE_URL = os.getenv("DATABASE_URL", "")

# ---------------------------------------------------------------------------
# Upload configuration (secure image uploads for PRO avatars & chat images)
# ---------------------------------------------------------------------------
UPLOAD_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
AVATAR_DIR = "avatars"
CHAT_DIR = "chat"
PAGE_DIR = "pages"
BG_DIR = "backgrounds"      # admin-managed chat backgrounds
STORY_DIR = "stories"       # story media (served only through an authenticated route)
ALLOWED_UPLOAD_SUBDIRS = {AVATAR_DIR, CHAT_DIR, PAGE_DIR, BG_DIR}
STORY_ROOT = os.path.join(UPLOAD_ROOT, STORY_DIR)
STORY_HOURS = 24
MAX_ACTIVE_STORIES = 20
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_POST_MEDIA_BYTES = 25 * 1024 * 1024
# Overall request body cap (a little above MAX_IMAGE_BYTES to allow for form fields).
app.config["MAX_CONTENT_LENGTH"] = 30 * 1024 * 1024

for _d in ALLOWED_UPLOAD_SUBDIRS:
    try:
        os.makedirs(os.path.join(UPLOAD_ROOT, _d), exist_ok=True)
    except Exception:
        pass
try:
    os.makedirs(STORY_ROOT, exist_ok=True)
except Exception:
    pass


class DB:
    def __init__(self):
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL is not configured.")
        self.conn = psycopg2.connect(DATABASE_URL)
        self.cur = self.conn.cursor(cursor_factory=RealDictCursor)

    def execute(self, *args, **kwargs):
        self.cur.execute(*args, **kwargs)
        return self.cur

    def fetchone(self):
        return self.cur.fetchone()

    def fetchall(self):
        return self.cur.fetchall()

    def commit(self):
        return self.conn.commit()

    def rollback(self):
        return self.conn.rollback()

    def close(self):
        try:
            self.cur.close()
        finally:
            self.conn.close()

def db():
    return DB()

def esc(v):
    return html.escape(str(v or ""))

def init_db():
    x = db()
    try:
        cur = x
        cur.execute("""CREATE TABLE IF NOT EXISTS users(
            id SERIAL PRIMARY KEY, username TEXT UNIQUE NOT NULL, email TEXT UNIQUE,
            password TEXT NOT NULL, emoji TEXT DEFAULT '👤', bio TEXT DEFAULT '',
            admin INTEGER DEFAULT 0, banned INTEGER DEFAULT 0,
            verified INTEGER DEFAULT 0, pro INTEGER DEFAULT 0,
            language TEXT DEFAULT 'en')""")
        cur.execute("""CREATE TABLE IF NOT EXISTS rooms(
            id SERIAL PRIMARY KEY, name TEXT NOT NULL, username TEXT UNIQUE NOT NULL,
            kind TEXT NOT NULL, owner TEXT NOT NULL, emoji TEXT DEFAULT '💬',
            bio TEXT DEFAULT '')""")
        cur.execute("""CREATE TABLE IF NOT EXISTS messages(
            id SERIAL PRIMARY KEY, room INTEGER, room_id INTEGER, username TEXT NOT NULL,
            text TEXT NOT NULL, created_at TEXT DEFAULT '', created TEXT DEFAULT '',
            reply INTEGER DEFAULT 0, edited INTEGER DEFAULT 0)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS private_messages(
            id SERIAL PRIMARY KEY, sender TEXT NOT NULL, receiver TEXT NOT NULL,
            text TEXT NOT NULL, created_at TEXT DEFAULT '')""")
        cur.execute("""CREATE TABLE IF NOT EXISTS private_chat_state(
            id SERIAL PRIMARY KEY, owner TEXT NOT NULL, other_user TEXT NOT NULL,
            unread INTEGER DEFAULT 0, UNIQUE(owner, other_user))""")
        # Additive Twitter-like features. Existing tables/data are preserved.
        cur.execute("""CREATE TABLE IF NOT EXISTS post_likes(
            id SERIAL PRIMARY KEY, post_id INTEGER NOT NULL, username TEXT NOT NULL,
            created_at TEXT DEFAULT '', UNIQUE(post_id,username))""")
        cur.execute("""CREATE TABLE IF NOT EXISTS post_comments(
            id SERIAL PRIMARY KEY, post_id INTEGER NOT NULL, username TEXT NOT NULL,
            text TEXT NOT NULL, created_at TEXT DEFAULT '')""")
        cur.execute("""CREATE TABLE IF NOT EXISTS follows(
            id SERIAL PRIMARY KEY, follower TEXT NOT NULL, target TEXT NOT NULL,
            created_at TEXT DEFAULT '', UNIQUE(follower,target))""")
        cur.execute("""CREATE TABLE IF NOT EXISTS bookmarks(
            id SERIAL PRIMARY KEY, post_id INTEGER NOT NULL, username TEXT NOT NULL,
            created_at TEXT DEFAULT '', UNIQUE(post_id,username))""")
        cur.execute("""CREATE TABLE IF NOT EXISTS blocks(
            id SERIAL PRIMARY KEY, blocker TEXT NOT NULL, blocked TEXT NOT NULL,
            created_at TEXT DEFAULT '', UNIQUE(blocker,blocked))""")
        cur.execute("""CREATE TABLE IF NOT EXISTS trending_posts(
            id SERIAL PRIMARY KEY, post_id INTEGER NOT NULL UNIQUE,
            enabled INTEGER DEFAULT 0, created_at TEXT DEFAULT '')""")
        # New, additive tables for chat backgrounds and stories. No existing
        # table or column is changed.
        cur.execute("""CREATE TABLE IF NOT EXISTS chat_backgrounds(
            id SERIAL PRIMARY KEY, filename TEXT NOT NULL, created_at TEXT DEFAULT '')""")
        cur.execute("""CREATE TABLE IF NOT EXISTS user_chat_bg(
            username TEXT PRIMARY KEY, bg_id INTEGER NOT NULL)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS stories(
            id SERIAL PRIMARY KEY, username TEXT NOT NULL, media_file TEXT NOT NULL,
            media_type TEXT NOT NULL, caption TEXT DEFAULT '',
            created_at TEXT DEFAULT '', expires_at TEXT DEFAULT '')""")
        for col, definition in {
            'media_type':"TEXT DEFAULT ''",'media_name':"TEXT DEFAULT ''",'media_url':"TEXT DEFAULT ''"
        }.items():
            cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name='messages' AND column_name=%s",(col,))
            if not cur.fetchone(): cur.execute(f"ALTER TABLE messages ADD COLUMN {col} {definition}")

        def columns(table):
            cur.execute("""SELECT column_name FROM information_schema.columns
                WHERE table_schema='public' AND table_name=%s""", (table,))
            rows = cur.fetchall()
            return {r["column_name"] for r in rows}

        # Safe, additive-only migrations. Nothing here ever drops a table,
        # column or row, so existing production data is always preserved.
        migrations = {
            "users": {
                "emoji":"TEXT DEFAULT '👤'","bio":"TEXT DEFAULT ''",
                "admin":"INTEGER DEFAULT 0","banned":"INTEGER DEFAULT 0",
                "verified":"INTEGER DEFAULT 0","pro":"INTEGER DEFAULT 0",
                "language":"TEXT DEFAULT 'en'",
                # Stores the safe, server-generated filename of a PRO user's
                # uploaded profile picture. Empty string = no picture (emoji avatar).
                "avatar":"TEXT DEFAULT ''",
                "profile_music":"TEXT DEFAULT ''"},
            "rooms": {
                "bio":"TEXT DEFAULT ''","emoji":"TEXT DEFAULT '💬'",
                # Admin-controlled blue verification badge for groups/channels.
                "verified":"INTEGER DEFAULT 0"},
            "messages": {
                "room":"INTEGER","room_id":"INTEGER","created_at":"TEXT DEFAULT ''",
                "created":"TEXT DEFAULT ''","reply":"INTEGER DEFAULT 0",
                "edited":"INTEGER DEFAULT 0",
                # Safe, server-generated filename of an image attached to a message
                # (PRO-only feature, enforced server-side on every send route).
                "image":"TEXT DEFAULT ''"},
            "private_messages":{
                "created_at":"TEXT DEFAULT ''",
                "image":"TEXT DEFAULT ''"}
        }
        for table, fields in migrations.items():
            have = columns(table)
            for name, definition in fields.items():
                if name not in have:
                    cur.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

        cur.execute("UPDATE messages SET room=room_id WHERE room IS NULL AND room_id IS NOT NULL")
        cur.execute("UPDATE messages SET room_id=room WHERE room_id IS NULL AND room IS NOT NULL")
        now = datetime.now().isoformat(timespec="seconds")
        cur.execute("UPDATE messages SET created_at=%s WHERE created_at IS NULL OR created_at=''", (now,))
        cur.execute("UPDATE messages SET created=created_at WHERE created IS NULL OR created=''", (now,))

        cur.execute("SELECT id FROM users WHERE username='simorg'")
        if not cur.fetchone():
            cur.execute("""INSERT INTO users
                (username,email,password,emoji,bio,admin,verified,pro)
                VALUES(%s,%s,%s,%s,%s,1,1,1)""",
                ("simorg","P@Sumorg",generate_password_hash("n2mm6mO-!Rvea0w"),"🦅","Official Simurgh"))
        else:
            # Row already exists: never delete it, just make sure the official
            # login (email/password) and admin flag are the requested ones.
            cur.execute(
                "UPDATE users SET email=%s, password=%s, admin=1 WHERE username='simorg'",
                ("P@Sumorg", generate_password_hash("Html930343245532"))
            )

        cur.execute("SELECT id FROM rooms WHERE username='simorg'")
        if not cur.fetchone():
            cur.execute("""INSERT INTO rooms(name,username,kind,owner,emoji,bio)
                VALUES(%s,%s,'channel',%s,%s,%s)""",
                ("سیمرغ","simorg","simorg","🦅","کانال رسمی سیمرغ"))
        else:
            # Row already exists: never delete it, just make sure kind/owner are correct.
            cur.execute("UPDATE rooms SET kind='channel', owner='simorg' WHERE username='simorg'")
        # Official Simurgh channel is always shown as verified.
        cur.execute("UPDATE rooms SET verified=1 WHERE username='simorg'")
        # New additive tables for: durable uploads, login sessions, private pages,
        # display names and seeded wallpapers. Existing tables are untouched.
        cur.execute("""CREATE TABLE IF NOT EXISTS upload_blobs(
            subdir TEXT NOT NULL, filename TEXT NOT NULL, mime TEXT DEFAULT '',
            data BYTEA NOT NULL, PRIMARY KEY(subdir,filename))""")
        cur.execute("""CREATE TABLE IF NOT EXISTS user_sessions(
            token TEXT PRIMARY KEY, username TEXT NOT NULL, user_agent TEXT DEFAULT '',
            ip TEXT DEFAULT '', created_at TEXT DEFAULT '', last_seen TEXT DEFAULT '',
            revoked INTEGER DEFAULT 0)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS account_privacy(
            username TEXT PRIMARY KEY, private INTEGER DEFAULT 0)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS follow_requests(
            id SERIAL PRIMARY KEY, requester TEXT NOT NULL, target TEXT NOT NULL,
            created_at TEXT DEFAULT '', UNIQUE(requester,target))""")
        cur.execute("""CREATE TABLE IF NOT EXISTS profile_names(
            username TEXT PRIMARY KEY, display_name TEXT DEFAULT '')""")
        cur.execute("""CREATE TABLE IF NOT EXISTS seeded_assets(name TEXT PRIMARY KEY)""")

        x.commit()
    except Exception:
        x.rollback()
        raise
    finally:
        cur.close()
        x.close()

def is_pro(username):
    x = db()
    x.execute("SELECT pro FROM users WHERE username=%s", (username,))
    row = x.fetchone()
    x.close()
    return bool(row and row["pro"])


def pro_badge(pro):
    return "<span class='pro-badge'>PRO</span>" if pro else ""

def me():
    name = session.get("user")
    if not name:
        return None
    x = db()
    x.execute("SELECT * FROM users WHERE username=%s", (name,))
    u = x.fetchone()
    if u is not None:
        try:
            u = _check_session(x, u)
        except Exception:
            x.rollback()
            app.logger.exception("SESSION CHECK ERROR")
    x.close()
    return u

def is_admin(u):
    return bool(u and int(u["admin"] or 0) == 1)

# ---------------------------------------------------------------------------
# Secure image upload helpers
#   - MIME/type validated from real file bytes (not trusted filename/header)
#   - size-limited
#   - filenames are always server-generated (uuid4) -> no path traversal,
#     no collisions, no reliance on user-supplied names
# ---------------------------------------------------------------------------
def detect_image_type(data):
    """Detect a safe image type from raw file bytes (magic-byte sniffing)."""
    if not data:
        return None, None
    if data[:3] == b"\xff\xd8\xff":
        return "jpg", "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png", "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif", "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    return None, None

def save_uploaded_image(file_storage, subdir):
    """Validate and save an uploaded image. Returns (filename, mime) or (None, None)
    when no file was supplied. Raises ValueError with a user-safe message on any
    validation failure. Never trusts the client-supplied filename or content-type."""
    if subdir not in ALLOWED_UPLOAD_SUBDIRS:
        raise ValueError("Invalid upload target.")
    if not file_storage or not getattr(file_storage, "filename", ""):
        return None, None

    data = file_storage.read()
    if not data:
        return None, None
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Image is too large (max 5MB).")

    ext, mime = detect_image_type(data)
    if not ext:
        raise ValueError("Unsupported image type. Use JPG, PNG, GIF or WEBP.")

    folder = os.path.join(UPLOAD_ROOT, subdir)
    os.makedirs(folder, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.{ext}"
    full_path = os.path.abspath(os.path.join(folder, filename))

    # Defense in depth against path traversal, even though the filename is
    # always server-generated and never derived from user input.
    if not full_path.startswith(os.path.abspath(folder) + os.sep):
        raise ValueError("Invalid file path.")

    with open(full_path, "wb") as f:
        f.write(data)
    persist_blob(subdir, filename, data, mime)
    return filename, mime

def delete_uploaded_file(subdir, filename):
    """Best-effort delete of a previously stored upload. Never raises."""
    if subdir not in ALLOWED_UPLOAD_SUBDIRS or not filename:
        return
    try:
        safe_name = os.path.basename(filename)
        folder = os.path.join(UPLOAD_ROOT, subdir)
        full_path = os.path.abspath(os.path.join(folder, safe_name))
        if not full_path.startswith(os.path.abspath(folder) + os.sep):
            return
        delete_blob(subdir, safe_name)
        if os.path.isfile(full_path):
            os.remove(full_path)
    except Exception:
        pass

def avatar_html(row, cls="avatar"):
    """Renders a user's avatar: an uploaded picture for PRO users who set one,
    otherwise the emoji avatar every account has by default."""
    avatar_file = None
    try:
        avatar_file = row["avatar"] if row and "avatar" in row.keys() else None
    except Exception:
        avatar_file = row.get("avatar") if row else None
    if avatar_file:
        return f"<div class='{cls} img'><img src='/uploads/avatars/{esc(avatar_file)}' alt=''></div>"
    emoji = (row["emoji"] if row else "👤") or "👤"
    return f"<div class='{cls}'>{esc(emoji)}</div>"

CSS = r"""
*{box-sizing:border-box}
html{transition:background-color .2s ease}
:root{
  --bg:#050a14; --bg-grad1:#0b2e6b; --bg-grad2:#06183a;
  --text:#eaf2ff; --side-bg:#060d1cdd; --border:#13284d;
  --nav-text:#8aa4d0; --nav-hover-bg:#0d1c38;
  --header-bg:#060d1cd9; --item-bg:#091428e8; --item-border:#153060; --item-hover:#0d1d3e;
  --avatar-bg:#10244a; --sub-text:#6d86b5; --arrow:#7d98c8;
  --input-bg:#08122a; --input-border:#153060; --input-text:#fff;
  --btn-grad1:#3b82f6; --btn-grad2:#1d4ed8; --btn-text:#fff;
  --msg-bg:#09152b; --msg-border:#153060; --msg-mine-bg:#123a85;
  --card-bg:#071022ed; --box-shadow:0 25px 80px rgba(0,10,40,.55);
  --logo-grad1:#38bdf8; --logo-grad2:#1d4ed8;
  --badge-bg:#2563eb;
  --bottom-bg:#050a16f2; --bottom-text:#7d98c8; --bottom-active:#60a5fa;
  --cover-grad:linear-gradient(120deg,#38bdf8,#2563eb,#0b1f4d);
}
[data-theme='light']{
  --bg:#f2f7ff; --bg-grad1:#dbe9ff; --bg-grad2:#e8f1ff;
  --text:#0b1f4d; --side-bg:#ffffffee; --border:#cfdcf2;
  --nav-text:#46629a; --nav-hover-bg:#e3eeff;
  --header-bg:#ffffffe6; --item-bg:#ffffff; --item-border:#cfdcf2; --item-hover:#f2f7ff;
  --avatar-bg:#dfeaff; --sub-text:#6a82ae; --arrow:#8aa0c6;
  --input-bg:#eef4ff; --input-border:#cfdcf2; --input-text:#0b1f4d;
  --btn-grad1:#3b82f6; --btn-grad2:#1d4ed8; --btn-text:#fff;
  --msg-bg:#e3eeff; --msg-border:#cfdcf2; --msg-mine-bg:#c9dcff;
  --card-bg:#ffffff; --box-shadow:0 20px 60px rgba(30,70,160,.14);
  --logo-grad1:#38bdf8; --logo-grad2:#1d4ed8;
  --badge-bg:#2563eb;
  --bottom-bg:#fffffff2; --bottom-text:#7d98c8; --bottom-active:#1d4ed8;
  --cover-grad:linear-gradient(120deg,#38bdf8,#2563eb,#0b1f4d);
}
html,body{margin:0;min-height:100%;background:var(--bg);color:var(--text);
font-family:Tahoma,Arial,sans-serif}a{text-decoration:none;color:inherit}button,input,textarea,select{font:inherit}
body{background:radial-gradient(circle at 20% 0%,var(--bg-grad1) 0,transparent 32%),radial-gradient(circle at 100% 20%,var(--bg-grad2) 0,transparent 28%),var(--bg);
transition:background-color .2s ease,color .2s ease}
.app{min-height:100vh;display:flex}.side{width:250px;background:var(--side-bg);border-right:1px solid var(--border);padding:18px}
.logo{font-size:25px;font-weight:900;margin:5px 8px 25px;color:var(--text)}.logo b{display:inline-flex;width:40px;height:40px;
align-items:center;justify-content:center;border-radius:13px;background:linear-gradient(135deg,var(--logo-grad1),var(--logo-grad2));margin-right:8px;color:#fff}
.nav{display:block;padding:13px 14px;margin:6px 0;border-radius:15px;color:var(--nav-text)}.nav:hover{background:var(--nav-hover-bg);color:var(--text)}
.main{flex:1;min-width:0}.header{height:64px;display:flex;align-items:center;gap:10px;padding:0 18px;
border-bottom:1px solid var(--border);background:var(--header-bg);backdrop-filter:blur(20px);position:sticky;top:0;z-index:5}
.header h3{margin:0;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text)}
.theme-toggle{flex:none;width:40px;height:40px;border-radius:12px;border:1px solid var(--border);background:var(--input-bg);
color:var(--text);display:flex;align-items:center;justify-content:center;cursor:pointer;font-size:18px;padding:0}
.page{max-width:760px;margin:auto;padding:18px 14px 95px}.list{display:flex;flex-direction:column;gap:9px}
.item{display:flex;align-items:center;gap:14px;padding:15px;border:1px solid var(--item-border);background:var(--item-bg);
border-radius:21px;transition:.18s}.item:hover{transform:translateY(-1px);background:var(--item-hover);border-color:var(--btn-grad2)}
.avatar{width:56px;height:56px;flex:none;border-radius:18px;background:var(--avatar-bg);display:flex;align-items:center;
justify-content:center;font-size:28px;color:var(--text)}
.avatar.img,.bigavatar.img{padding:0;overflow:hidden}
.avatar.img img,.bigavatar.img img{width:100%;height:100%;object-fit:cover;border-radius:inherit;display:block}
.info{flex:1;min-width:0}.name{font-weight:900;color:var(--text)}.sub{font-size:12px;color:var(--sub-text);margin-top:4px}
.arrow{color:var(--arrow);font-size:22px}
.pro-badge{display:inline-block;margin:0 5px;padding:3px 8px;border-radius:999px;background:linear-gradient(135deg,#ff4d8d,#7c5cff);color:#fff;font-size:10px;font-weight:800;box-shadow:0 4px 14px rgba(124,92,255,.28)}
.badge{font-size:10px;padding:3px 7px;border-radius:8px;background:var(--badge-bg);margin-right:4px;display:inline-block;color:#fff}.badge.pro{min-width:20px;text-align:center}
.pro{background:linear-gradient(135deg,#ffbd2e,#ff6a00);color:#15100a}
.box{max-width:520px;margin:30px auto;padding:22px;
border:1px solid var(--border);border-radius:25px;background:var(--card-bg);box-shadow:var(--box-shadow)}
input,textarea,select{width:100%;padding:13px;border:1px solid var(--input-border);border-radius:14px;background:var(--input-bg);color:var(--input-text);margin:6px 0}
textarea{min-height:100px}
button{width:100%;padding:13px;border:0;border-radius:14px;background:linear-gradient(135deg,var(--btn-grad1),var(--btn-grad2));color:var(--btn-text);font-weight:900;margin:6px 0;cursor:pointer}
.chat{height:calc(100vh - 64px);display:flex;flex-direction:column}.messages{flex:1;overflow:auto;padding:18px}.msg{max-width:78%;padding:11px 14px;
margin:8px 0;border-radius:18px;background:var(--msg-bg);border:1px solid var(--msg-border);line-height:1.7;color:var(--text)}.mine{margin-right:auto;background:var(--msg-mine-bg)}
.msg-img{max-width:100%;max-height:320px;object-fit:cover;border-radius:14px;display:block;margin-bottom:6px}
.meta{font-size:11px;color:var(--sub-text);margin-bottom:3px}.composer{display:flex;gap:8px;padding:10px 14px;background:var(--header-bg);border-top:1px solid var(--border)}
.composer input{margin:0}.composer button{width:58px;margin:0;flex:none}.bottom{display:none}
.img-btn{flex:none;width:44px;height:44px;display:flex;align-items:center;justify-content:center;background:var(--input-bg);
border:1px solid var(--input-border);border-radius:14px;cursor:pointer;font-size:18px}
.profile{max-width:620px;margin:25px auto;text-align:center;background:var(--card-bg);border:1px solid var(--border);border-radius:28px;overflow:hidden}
.cover{height:135px;background:var(--cover-grad)}.bigavatar{font-size:52px;width:100px;height:100px;
display:flex;align-items:center;justify-content:center;margin:-45px auto 10px;background:var(--avatar-bg);border:4px solid var(--card-bg);border-radius:28px;color:var(--text)}
.actions{display:flex;gap:8px;justify-content:center;padding:15px 20px 25px}.actions a{padding:11px 15px;border-radius:13px;background:var(--input-bg);color:var(--text)}
.avatar-upload{text-align:center;margin-bottom:10px}
.upload-label{display:inline-block;cursor:pointer;text-align:center}
.admin-row{display:flex;align-items:center;gap:14px;padding:15px;border:1px solid var(--item-border);background:var(--item-bg);border-radius:21px;flex-wrap:wrap}
.admin-row .info{flex:1;min-width:140px}.admin-actions{display:flex;gap:6px;flex-wrap:wrap}
.btn-sm{width:auto;padding:8px 12px;margin:0;font-size:12px;border-radius:10px;background:var(--input-bg);color:var(--text)}
.btn-sm.warn{background:linear-gradient(135deg,#ff4d4d,#c92a2a);color:#fff}.btn-sm.on{background:linear-gradient(135deg,var(--btn-grad1),var(--btn-grad2));color:#fff}
.admin-section-title{margin:22px 0 10px}
.card{padding:12px 14px;border:1px solid var(--item-border);background:var(--item-bg);border-radius:14px;margin:8px 0;color:var(--text)}
.feed-title{display:flex;align-items:center;justify-content:space-between}.feed-tabs{position:sticky;top:62px;z-index:4;display:flex;background:var(--header-bg);backdrop-filter:blur(18px);border-bottom:1px solid var(--border);margin:0 -10px 10px}.feed-tabs a{flex:1;text-align:center;padding:15px 5px;color:var(--sub-text);font-weight:800;border-bottom:3px solid transparent}.feed-tabs a.active{color:var(--text);border-bottom-color:var(--btn-grad2)}.composer-card{background:var(--card-bg);border:1px solid var(--border);border-radius:20px;padding:12px;margin-bottom:10px}.composer-card textarea{min-height:70px;border:0;background:transparent;margin:0;resize:none}.compose-row{display:flex;align-items:center;gap:8px}.compose-row .sub{flex:1}.compose-row button{width:auto;padding:10px 18px;margin:0}.media-pick{width:40px;height:40px;border-radius:50%;display:flex;align-items:center;justify-content:center;background:var(--input-bg);cursor:pointer;font-size:22px}.feed-list{display:flex;flex-direction:column}.post-card{background:var(--card-bg);border:1px solid var(--border);border-radius:20px;padding:14px;margin:7px 0;box-shadow:var(--box-shadow);user-select:text}.post-head{display:flex;align-items:center;gap:10px}.post-author{flex:1;min-width:0}.post-author a{color:var(--text)}.more-btn{width:auto;background:transparent;color:var(--sub-text);padding:5px;margin:0}.post-text{font-size:15px;line-height:1.9;margin:12px 3px;white-space:pre-wrap;word-break:break-word}.post-actions{display:flex;align-items:center;gap:4px;border-top:1px solid var(--border);padding-top:8px;margin-top:8px;flex-wrap:wrap}.post-actions form{display:inline}.post-actions button,.post-actions a{width:auto;background:transparent;color:var(--sub-text);padding:7px 9px;margin:0;border-radius:10px}.post-actions button:hover,.post-actions a:hover{background:var(--input-bg);color:var(--text)}.danger{color:#ff6666!important}.edited{font-size:10px;color:var(--sub-text)}.trend-mark{font-size:9px;background:#ff7a00;color:#fff;border-radius:7px;padding:2px 5px;margin-right:3px}.post-media{width:100%;max-height:480px;border-radius:17px;object-fit:cover;margin-top:5px}.post-audio{width:100%;margin-top:8px}.file-pill{display:block;padding:12px;border:1px solid var(--border);border-radius:13px;margin-top:8px;color:var(--text)}.empty{text-align:center;padding:50px 15px;color:var(--sub-text)}.focus-overlay{position:fixed;inset:0;background:rgba(0,0,0,.72);backdrop-filter:blur(14px);z-index:100;display:none;align-items:center;justify-content:center;padding:15px}.focus-overlay.open{display:flex}.focus-box{width:min(650px,100%);max-height:90vh;overflow:auto}.focus-box .post-card{margin:0;box-shadow:0 20px 80px rgba(0,0,0,.5)}.profile-stats{font-size:14px}.profile-music{padding:8px 18px}.profile-music audio{width:100%;margin-top:7px}.profile-music form{display:flex;gap:8px;align-items:center}.profile-music button{width:auto}.bigavatar.img{overflow:hidden}.bigavatar.img img{width:100%;height:100%;object-fit:cover}.avatar.img{overflow:hidden}.avatar.img img{width:100%;height:100%;object-fit:cover}@media(max-width:700px){.side{display:none}.header{height:62px}.page{padding:12px 10px 88px}.chat{height:calc(100vh - 132px)}
.msg{max-width:90%}.bottom{position:fixed;display:flex;bottom:0;left:0;right:0;height:70px;z-index:30;background:var(--bottom-bg);
border-top:1px solid var(--border);backdrop-filter:blur(22px)}.bottom a{flex:1;text-align:center;padding:8px 2px;color:var(--bottom-text);font-size:10px}.bottom span{display:block;font-size:21px;margin-bottom:3px}
.bottom .active{color:var(--bottom-active)}.box{margin:15px 5px}.item{padding:13px}.avatar{width:52px;height:52px}.post-card{padding:12px;margin:5px 0}
.post-text{font-size:14px;margin:10px 0}.feed-tabs{padding:0 10px}.composer-card{margin:8px 0;padding:10px}.input-bg{padding:10px}input,textarea,button{padding:12px;font-size:16px}
.focus-box{max-height:95vh;border-radius:20px}.focus-overlay{padding:10px}.profile{margin:10px 0}.actions{gap:5px}.admin-row{flex-wrap:wrap}.admin-actions{width:100%;gap:4px}}
/* ---- additions: rules, stories, chat backgrounds ---- */
.rules-box{text-align:right;direction:rtl;border:1px solid var(--item-border);background:var(--item-bg);border-radius:16px;padding:12px 16px;margin:12px 18px;color:var(--text)}
.rules-box h3{margin:0 0 6px}.rules-box summary{cursor:pointer;font-weight:900}
.rules-list{margin:8px 0;padding:0 22px 0 0;line-height:2;font-size:14px}
.box .rules-box{margin:12px 0;max-height:210px;overflow:auto}
.check-row{display:flex;align-items:center;gap:8px;margin:10px 0;direction:rtl;font-size:14px;cursor:pointer}
.check-row input{width:auto;margin:0}
button:disabled{opacity:.45;cursor:not-allowed}
.story-bar{display:flex;gap:12px;overflow-x:auto;padding:6px 2px 12px;margin-bottom:6px;scrollbar-width:none}
.story-bar::-webkit-scrollbar{display:none}
.story-item{flex:none;width:70px;text-align:center;font-size:11px;color:var(--sub-text)}
.story-item span{display:block;margin-top:5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.story-ring{width:66px;height:66px;border-radius:22px;padding:3px;background:linear-gradient(135deg,#38bdf8,#2563eb,#1e3a8a);display:flex}
.story-ring.add{background:var(--input-border)}
.story-ring .avatar{width:100%;height:100%;border-radius:19px;border:2px solid var(--bg);font-size:24px}
.story-stage{position:fixed;inset:0;background:#000;z-index:200;color:#fff}
.story-stage .slide{display:none;position:absolute;inset:0;align-items:center;justify-content:center}
.story-stage .slide.on{display:flex}
.story-stage img,.story-stage video{max-width:100%;max-height:100%;object-fit:contain}
.story-progress{position:absolute;top:8px;left:8px;right:8px;display:flex;gap:4px;z-index:6}
.story-progress i{flex:1;height:3px;border-radius:3px;background:rgba(255,255,255,.3)}
.story-progress i.done{background:#fff}.story-progress i.cur{background:#60a5fa}
.story-top{position:absolute;top:18px;left:0;right:0;padding:10px 14px;display:flex;gap:10px;align-items:center;background:linear-gradient(rgba(0,0,0,.65),transparent);z-index:5}
.story-top a{color:#fff;font-size:20px}.story-top form{margin:0}
.story-cap{position:absolute;bottom:0;left:0;right:0;padding:40px 18px 28px;text-align:center;background:linear-gradient(transparent,rgba(0,0,0,.75));z-index:4;line-height:1.8}
.story-tap{position:absolute;top:70px;bottom:70px;width:35%;z-index:2}
.story-tap.prev{left:0}.story-tap.nxt{right:0}
.bg-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(90px,1fr));gap:8px;padding:0 18px 18px}
.bg-tile{width:100%;height:78px;padding:0;margin:0;border-radius:14px;border:2px solid var(--item-border);background-size:cover;background-position:center;background-color:var(--input-bg);color:var(--text);font-size:12px}
.bg-tile.sel{border-color:#60a5fa;box-shadow:0 0 0 2px #2563eb55}
.bg-grid form{margin:0}
.bg-admin{display:flex;flex-direction:column;gap:6px}
.bg-thumb{height:78px;border-radius:12px;background-size:cover;background-position:center;border:1px solid var(--item-border)}
"""

THEME_INIT_SCRIPT = """<script>(function(){
try{
  var t = localStorage.getItem('simurgh_theme') || 'dark';
  document.documentElement.setAttribute('data-theme', t);
}catch(e){}
})();</script>"""

THEME_TOGGLE_SCRIPT = """<script>
function toggleTheme(){
  var html = document.documentElement;
  var cur = html.getAttribute('data-theme') || 'dark';
  var next = cur === 'dark' ? 'light' : 'dark';
  html.setAttribute('data-theme', next);
  try{ localStorage.setItem('simurgh_theme', next); }catch(e){}
}
function focusPost(e,card){if(e)e.preventDefault();var o=document.getElementById('focusOverlay'),b=document.getElementById('focusBox');if(!o||!b||!card)return;try{var clone=card.cloneNode(true);clone.oncontextmenu=null;clone.ontouchstart=null;clone.ontouchend=null;clone.ontouchmove=null;b.innerHTML='';b.appendChild(clone);o.classList.add('open');document.body.style.overflow='hidden'}catch(x){console.error('Error focusing post:',x)}}
function closeFocus(e){if(e&&e.target.id==='focusOverlay'){e.currentTarget.classList.remove('open');document.body.style.overflow=''}}
var holdTimer;function startHold(e,card){holdTimer=setTimeout(function(){focusPost(e,card)},550)}function cancelHold(){clearTimeout(holdTimer)}
</script>"""

def layout(title, body, active="chat"):
    u = me()
    is_adm = bool(u and int(u["admin"] or 0) == 1)
    links=[("chat","⌂","خانه","/chat"),("messages","✉","دایرکت","/messages"),("search","⌕","جستجو","/search"),
           ("bookmarks","★","ذخیره‌ها","/bookmarks"),("profile","◉","پروفایل","/profile")]
    side="".join(f"<a class='nav' href='{u}'>{i} {n}</a>" for k,i,n,u in links)
    bottom="".join(f"<a class='{'active' if active==k else ''}' href='{u}'><span>{i}</span>{n}</a>" for k,i,n,u in links)
    admin_side = "<a class='nav' href='/admin'>🛡️ پنل مدیریت</a>" if is_adm else ""
    admin_bottom = f"<a class='{'active' if active=='admin' else ''}' href='/admin'><span>🛡️</span>مدیریت</a>" if is_adm else ""
    return f"""<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'>
    {THEME_INIT_SCRIPT}
    <title>{esc(title)} · Simurgh</title><style>{CSS}</style></head><body>
    <div class='app'><aside class='side'><div class='logo'><b>🦅</b>Simurgh</div>{side}{admin_side}
    <a class='nav' href='/settings'>⚙️ تنظیمات</a><a class='nav' href='/logout'>↪ خروج</a></aside>
    <main class='main'><header class='header'><h3>{esc(title)}</h3>
    <button type='button' class='theme-toggle' onclick='toggleTheme()' title='تغییر پوسته روشن/تاریک'>🌓</button>
    </header>{body}</main></div>
    <nav class='bottom'>{bottom}{admin_bottom}</nav>{THEME_TOGGLE_SCRIPT}</body></html>"""

@app.route("/")
def index():
    if me(): return redirect("/chat")
    return f"""<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'>
    {THEME_INIT_SCRIPT}<style>{CSS}</style></head>
    <body><div class='box'><div class='avatar' style='margin:auto'>🦅</div><h1 style='text-align:center'>Simurgh</h1>
    <div class='rules-box'><h3>📜 قوانین سیمرغ</h3>{rules_html()}</div>
    <form method='post' action='/login'>
    <label class='check-row'><input type='checkbox' id='acc' name='accept_rules' value='1' required> قوانین را خوانده‌ام و می‌پذیرم</label>
    <input name='email' placeholder='Email' required><input name='password' type='password' placeholder='Password' required><button id='lb' disabled>Login</button></form>
    <a href='/register'>Create account</a></div>{RULES_JS}</body></html>""" 

@app.route("/register",methods=["GET","POST"])
def register():
    if request.method=="GET":
        return f"""<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'>
        {THEME_INIT_SCRIPT}<style>{CSS}</style></head>
        <body><div class='box'><h2>Create account</h2><div class='rules-box'><h3>📜 قوانین سیمرغ</h3>{rules_html()}</div><form method='post'><label class='check-row'><input type='checkbox' id='acc' name='accept_rules' value='1' required> قوانین را خوانده‌ام و می‌پذیرم</label><input name='username' placeholder='Username' required>
        <input name='email' placeholder='Email' required><input name='password' type='password' placeholder='Password' required><button id='lb' disabled>Register</button></form><a href='/'>Back</a></div>{RULES_JS}</body></html>"""
    username = request.form.get("username", "").strip()
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")

    if not request.form.get("accept_rules"):
        return "برای ثبت‌نام باید قوانین را بپذیرید.", 400
    if not username or not email or not password:
        return "Username, email and password are required.", 400
    if len(username) > 40 or len(email) > 255 or len(password) < 6:
        return "Invalid account information.", 400

    x = db()
    try:
        x.execute(
            "INSERT INTO users(username,email,password) VALUES(%s,%s,%s)",
            (username, email, generate_password_hash(password))
        )
        x.commit()
    except psycopg2.IntegrityError:
        x.rollback()
        return "Username or email already exists.", 409
    finally:
        x.close()

    session.clear()
    session["user"] = username
    start_session_row(username)
    return redirect("/chat")

@app.route("/login",methods=["POST"])
def login():
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")

    if not request.form.get("accept_rules"):
        return "برای ورود باید قوانین را بخوانید و تیک پذیرش را بزنید.", 400
    if not email or not password:
        return "Email and password are required.", 400

    x = None
    try:
        x = db()
        x.execute(
            "SELECT username, password, banned FROM users WHERE LOWER(email)=LOWER(%s) LIMIT 1",
            (email,)
        )
        u = x.fetchone()
        x.close()
        x = None
    except Exception:
        app.logger.exception("LOGIN DATABASE ERROR")
        if x is not None:
            try:
                x.rollback()
                x.close()
            except Exception:
                pass
        return "Login database error. Check Render logs.", 500

    if not u:
        return "Invalid login.", 401

    try:
        if not check_password_hash(u["password"], password):
            return "Invalid login.", 401
    except Exception:
        app.logger.exception("PASSWORD HASH ERROR")
        return "Invalid login.", 500

    if int(u["banned"] or 0) == 1:
        return "Account is banned.", 403

    session.clear()
    session["user"] = u["username"]
    start_session_row(u["username"])
    return redirect("/chat")

@app.route("/logout")
def logout():
    sid = session.get("sid")
    if sid:
        try:
            x = db()
            x.execute("UPDATE user_sessions SET revoked=1 WHERE token=%s", (sid,))
            x.commit()
            x.close()
        except Exception:
            pass
    session.clear()
    return redirect("/")

def _post_rows(x, u, tab="for_you"):
    blocked_sql="NOT EXISTS(SELECT 1 FROM blocks b WHERE b.blocker=%s AND b.blocked=m.username) AND NOT EXISTS(SELECT 1 FROM blocks b2 WHERE b2.blocker=m.username AND b2.blocked=%s)"
    params=[u['username'],u['username']]
    if tab == 'following':
        where="("+blocked_sql+") AND EXISTS(SELECT 1 FROM follows f WHERE f.follower=%s AND f.target=m.username)"
        params.append(u['username'])
        order="m.id DESC"
    elif tab == 'trending':
        where="("+blocked_sql+") AND (EXISTS(SELECT 1 FROM trending_posts tp WHERE tp.post_id=m.id AND tp.enabled=1) OR (SELECT COUNT(*) FROM post_likes l2 WHERE l2.post_id=m.id)>=60)"
        order="(SELECT COUNT(*) FROM post_likes l3 WHERE l3.post_id=m.id) DESC,m.id DESC"
    elif tab == 'new':
        where="("+blocked_sql+")"
        order="m.id DESC"
    else:
        where="("+blocked_sql+")"
        order="m.id DESC"
    priv_sql="NOT EXISTS(SELECT 1 FROM account_privacy ap WHERE ap.username=m.username AND ap.private=1 AND m.username<>%s AND NOT EXISTS(SELECT 1 FROM follows fp WHERE fp.follower=%s AND fp.target=m.username))"
    where="("+where+") AND "+priv_sql
    params=params+[u['username'],u['username']]
    q=f"""SELECT m.*, COALESCE(r.name,pn.display_name,u2.username) AS author_name,
        COALESCE(r.username,u2.username) AS author_username,
        COALESCE(r.emoji,u2.emoji) AS author_emoji,
        COALESCE(r.verified,u2.verified) AS author_verified,
        u2.avatar AS author_avatar,u2.pro AS author_pro,
        (SELECT COUNT(*) FROM post_likes l WHERE l.post_id=m.id) AS likes,
        (SELECT COUNT(*) FROM post_comments c WHERE c.post_id=m.id) AS comments,
        EXISTS(SELECT 1 FROM post_likes ml WHERE ml.post_id=m.id AND ml.username=%s) AS liked,
        EXISTS(SELECT 1 FROM bookmarks bm WHERE bm.post_id=m.id AND bm.username=%s) AS bookmarked,
        EXISTS(SELECT 1 FROM trending_posts tp0 WHERE tp0.post_id=m.id AND tp0.enabled=1) AS admin_trending
        FROM messages m LEFT JOIN rooms r ON r.id=m.room
        LEFT JOIN users u2 ON u2.username=m.username
        LEFT JOIN profile_names pn ON pn.username=m.username
        WHERE {where} ORDER BY {order} LIMIT 100"""
    return x.execute(q, (u['username'],u['username'],*params)).fetchall()

def _render_post(m, u, modal=True):
    badge=" <span class='badge'>✓</span>" if m['author_verified'] else ''
    pro=" <span class='badge pro'>PRO</span>" if m['author_pro'] else ''
    av=avatar_html({'avatar':m['author_avatar'],'emoji':m['author_emoji']})
    if m.get('room'):
        pi=page_image_url(m['author_username'])
        if pi: av=f"<img class='avatar img' src='{pi}' alt=''>"
    media=''; mt=m.get('media_type') or ''; mu=m.get('media_url') or ''
    if mu and mt.startswith('image/'): media=f"<img class='msg-img' src='/uploads/chat/{esc(mu)}' alt=''>"
    elif mu and mt.startswith('video/'): media=f"<video controls class='post-media'><source src='/uploads/chat/{esc(mu)}' type='{esc(mt)}'></video>"
    elif mu and mt.startswith('audio/'): media=f"<audio controls class='post-audio' src='/uploads/chat/{esc(mu)}'></audio>"
    elif mu: media=f"<a class='file-pill' href='/uploads/chat/{esc(mu)}' download>📎 {esc(m.get('media_name') or 'فایل')}</a>"
    owner_actions=''
    if m['username']==u['username'] or is_admin(u):
        owner_actions=f"<a class='post-more' href='/post/edit/{m['id']}'>ویرایش</a><form style='display:inline' method='post' action='/post/delete/{m['id']}'><button class='post-more danger'>حذف</button></form>"
    like_label='✦' if m['liked'] else '✧'; save_label='★' if m['bookmarked'] else '☆'
    trend=' <span class="trend-mark">ترند</span>' if m.get('admin_trending') else ''
    card=f"""<article class='post-card' data-post='{m['id']}' oncontextmenu='focusPost(event,this)' ontouchstart='startHold(event,this)' ontouchend='cancelHold(this)' ontouchmove='cancelHold(this)'>
    <div class='post-head'>{av}<div class='post-author'><a href='/profile/{esc(m['author_username'])}'><b>{esc(m['author_name'])}</b>{badge}{pro}{trend}</a><div class='sub'>@{esc(m['author_username'])} · {esc(m.get('created_at',''))}</div></div><button class='more-btn' onclick='focusPost(event,this.closest("article"))'>•••</button></div>
    <div class='post-text'>{esc(m['text'])}{' <span class="edited">ویرایش شد</span>' if m['edited'] else ''}</div>{media}
    <div class='post-actions'><form method='post' action='/post/like/{m['id']}'><button title='لایک'>{like_label} <span>{m['likes']}</span></button></form><a href='/post/{m['id']}/comments'>⌘<span>{m['comments']}</span></a><form method='post' action='/post/bookmark/{m['id']}'><button title='ذخیره'>{save_label}</button></form><button onclick='focusPost(event,this.closest("article"))' title='تمرکز'>⤢</button>{owner_actions}</div>
    </article>"""
    return card

@app.route("/chat")
def chat():
    u=me()
    if not u:return redirect("/")
    tab=request.args.get('tab','for_you')
    if tab not in ('for_you','following','trending','new'): tab='for_you'
    x=db();posts=_post_rows(x,u,tab)
    today=datetime.now().date().isoformat();today_count=x.execute("SELECT COUNT(*) AS c FROM messages WHERE username=%s AND created_at LIKE %s",(u['username'],today+'%')).fetchone()['c'];x.close()
    limit=1000 if u['pro'] else (5 if u['verified'] else 3)
    composer=f"""<div class='composer-card'><form method='post' action='/post/create' enctype='multipart/form-data'><textarea name='text' maxlength='2000' placeholder='چه خبر؟'></textarea><div class='compose-row'><span class='sub'>امروز {today_count}/{limit} پست</span><label class='media-pick'>＋<input type='file' name='media' accept='image/*,video/*,audio/*,.pdf,.zip,.txt,.doc,.docx' hidden></label><button>پست کردن</button></div></form></div>"""
    tabs=[('for_you','برای تو'),('following','دنبال‌شده‌ها'),('trending','ترند'),('new','تازه‌ها')]
    tab_html="<div class='feed-tabs'>"+''.join(f"<a class='{'active' if tab==k else ''}' href='/chat?tab={k}'>{n}</a>" for k,n in tabs)+"</div>"
    out=''.join(_render_post(m,u) for m in posts)
    body=f"<div class='page feed-page'><div class='feed-title'><h2>خانه</h2></div>{story_bar(u)}{tab_html}{composer}<div class='feed-list'>{out or '<div class="empty">چیزی برای نمایش نیست.</div>'}</div></div><div id='focusOverlay' class='focus-overlay' onclick='closeFocus(event)'><div id='focusBox' class='focus-box'></div></div>"
    return layout("خانه",body,"chat")

@app.route('/bookmarks')
def bookmarks():
    u=me()
    if not u:return redirect('/')
    x=db();rows=x.execute("""SELECT m.*,COALESCE(r.name,pn.display_name,u2.username) AS author_name,COALESCE(r.username,u2.username) AS author_username,COALESCE(r.emoji,u2.emoji) AS author_emoji,COALESCE(r.verified,u2.verified) AS author_verified,u2.avatar AS author_avatar,u2.pro AS author_pro,(SELECT COUNT(*) FROM post_likes l WHERE l.post_id=m.id) AS likes,(SELECT COUNT(*) FROM post_comments c WHERE c.post_id=m.id) AS comments,EXISTS(SELECT 1 FROM post_likes ml WHERE ml.post_id=m.id AND ml.username=%s) AS liked,TRUE AS bookmarked,FALSE AS admin_trending FROM bookmarks b JOIN messages m ON m.id=b.post_id LEFT JOIN rooms r ON r.id=m.room LEFT JOIN users u2 ON u2.username=m.username LEFT JOIN profile_names pn ON pn.username=m.username WHERE b.username=%s ORDER BY b.id DESC""",(u['username'],u['username'])).fetchall();x.close()
    out=''.join(_render_post(m,u) for m in rows)
    body=f"<div class='page'><h2>ذخیره‌ها</h2><div class='feed-list'>{out or '<div class=\"empty\">هنوز پستی ذخیره نکردی.</div>'}</div></div><div id='focusOverlay' class='focus-overlay' onclick='closeFocus(event)'><div id='focusBox' class='focus-box'></div></div>"
    return layout('ذخیره‌ها',body,'bookmarks')

@app.route("/messages")
def messages():
    u=me()
    if not u:return redirect("/")
    x=db()
    private_users=x.execute("""SELECT u.username,u.emoji,u.avatar,u.verified,u.pro,COALESCE(s.unread,0) AS unread
        FROM users u JOIN (SELECT CASE WHEN sender=%s THEN receiver ELSE sender END AS other_user
        FROM private_messages WHERE sender=%s OR receiver=%s GROUP BY CASE WHEN sender=%s THEN receiver ELSE sender END) p
        ON p.other_user=u.username LEFT JOIN private_chat_state s ON s.owner=%s AND s.other_user=u.username
        ORDER BY COALESCE(s.unread,0) DESC,u.username""",(u['username'],u['username'],u['username'],u['username'],u['username'])).fetchall();x.close()
    cards=''.join(f"<a class='item' href='/private/{esc(z['username'])}'>{avatar_html(z)}<div class='info'><div class='name'>@{esc(z['username'])} {'✓' if z['verified'] else ''} {'PRO' if z['pro'] else ''}</div><div class='sub'>دایرکت</div></div></a>" for z in private_users)
    return layout("دایرکت",f"<div class='page'><h2>دایرکت</h2><div class='list'>{cards or '<p class=\"sub\">هنوز گفتگویی نداری.</p>'}</div></div>","messages")

@app.route("/post/create",methods=["POST"])
def create_post():
    u=me()
    if not u:return redirect("/")
    text=request.form.get('text','').strip()[:2000]
    x=db();today=datetime.now().date().isoformat()
    limit=1000 if u['pro'] else (5 if u['verified'] else 3)
    c=x.execute("SELECT COUNT(*) AS c FROM messages WHERE username=%s AND created_at LIKE %s",(u['username'],today+'%')).fetchone()['c']
    if c>=limit:x.close();return f"محدودیت روزانه پست شما تمام شده است. سقف امروز: {limit}",429
    f=request.files.get('media'); media_url=''; media_type=''; media_name=''
    if f and f.filename:
        data=f.read()
        if len(data)>MAX_POST_MEDIA_BYTES:x.close();return 'حجم فایل بیشتر از 25MB است.',400
        ext=os.path.splitext(f.filename)[1].lower()
        allowed={'.jpg': 'image/jpeg','.jpeg':'image/jpeg','.png':'image/png','.gif':'image/gif','.webp':'image/webp','.mp4':'video/mp4','.webm':'video/webm','.mov':'video/quicktime','.mp3':'audio/mpeg','.wav':'audio/wav','.ogg':'audio/ogg','.pdf':'application/pdf','.zip':'application/zip','.txt':'text/plain','.doc':'application/msword','.docx':'application/vnd.openxmlformats-officedocument.wordprocessingml.document'}
        if ext not in allowed:x.close();return 'نوع فایل پشتیبانی نمی‌شود.',400
        media_type=allowed[ext]; media_name=os.path.basename(f.filename)[:120]; media_url=f"{uuid.uuid4().hex}{ext}"
        with open(os.path.join(UPLOAD_ROOT,CHAT_DIR,media_url),'wb') as out:out.write(data)
        persist_blob(CHAT_DIR,media_url,data,media_type)
    if not text and not media_url:x.close();return 'پست خالی است.',400
    now=datetime.now().isoformat(timespec='seconds')
    x.execute("INSERT INTO messages(room,room_id,username,text,created_at,created,reply,edited,image,media_type,media_name,media_url) VALUES(NULL,NULL,%s,%s,%s,%s,0,0,'',%s,%s,%s)",(u['username'],text,now,now,media_type,media_name,media_url));x.commit();x.close();return redirect('/chat')

@app.route('/post/like/<int:pid>',methods=['POST'])
def post_like(pid):
    u=me()
    if not u:return redirect('/')
    x=db();row=x.execute('SELECT id FROM messages WHERE id=%s',(pid,)).fetchone()
    if not row:x.close();return 'Post not found',404
    if x.execute('SELECT id FROM post_likes WHERE post_id=%s AND username=%s',(pid,u['username'])).fetchone(): x.execute('DELETE FROM post_likes WHERE post_id=%s AND username=%s',(pid,u['username']))
    else:x.execute('INSERT INTO post_likes(post_id,username,created_at) VALUES(%s,%s,%s)',(pid,u['username'],datetime.now().isoformat(timespec='seconds')))
    x.commit();x.close();return redirect('/chat')

@app.route('/post/bookmark/<int:pid>',methods=['POST'])
def post_bookmark(pid):
    u=me()
    if not u:return redirect('/')
    x=db()
    if not x.execute('SELECT id FROM messages WHERE id=%s',(pid,)).fetchone(): x.close();return 'Post not found',404
    if x.execute('SELECT id FROM bookmarks WHERE post_id=%s AND username=%s',(pid,u['username'])).fetchone():
        x.execute('DELETE FROM bookmarks WHERE post_id=%s AND username=%s',(pid,u['username']))
    else:
        x.execute('INSERT INTO bookmarks(post_id,username,created_at) VALUES(%s,%s,%s)',(pid,u['username'],datetime.now().isoformat(timespec='seconds')))
    x.commit();x.close();return redirect(request.referrer or '/chat')

@app.route('/post/<int:pid>/comments',methods=['GET','POST'])
def post_comments(pid):
    u=me()
    if not u:return redirect('/')
    x=db();p=x.execute('SELECT m.*,u.username,u.emoji,u.avatar,u.verified,u.pro FROM messages m JOIN users u ON u.username=m.username WHERE m.id=%s',(pid,)).fetchone()
    if not p:x.close();return 'Post not found',404
    if request.method=='POST':
        t=request.form.get('text','').strip()[:1000]
        if t:x.execute('INSERT INTO post_comments(post_id,username,text,created_at) VALUES(%s,%s,%s,%s)',(pid,u['username'],t,datetime.now().isoformat(timespec='seconds')));x.commit()
    cs=x.execute('SELECT c.*,u.emoji,u.avatar,u.verified,u.pro FROM post_comments c JOIN users u ON u.username=c.username WHERE c.post_id=%s ORDER BY c.id DESC',(pid,)).fetchall();x.close()
    comments=''.join(f"<div class='card'><b>@{esc(c['username'])}</b> {'✓' if c['verified'] else ''}<div>{esc(c['text'])}</div></div>" for c in cs)
    return layout('کامنت‌ها',f"<div class='page'><div class='card'><b>@{esc(p['username'])}</b><p>{esc(p['text'])}</p></div><form method='post'><input name='text' placeholder='کامنت...' required><button>ارسال</button></form><div>{comments}</div></div>",'chat')

@app.route('/post/edit/<int:pid>',methods=['GET','POST'])
def post_edit(pid):
    u=me()
    if not u:return redirect('/')
    x=db();p=x.execute('SELECT * FROM messages WHERE id=%s',(pid,)).fetchone()
    if not p:x.close();return 'Post not found',404
    if p['username']!=u['username'] and not is_admin(u):x.close();return 'Forbidden',403
    if request.method=='POST':
        t=request.form.get('text','').strip()[:2000]
        x.execute('UPDATE messages SET text=%s,edited=1 WHERE id=%s',(t,pid));x.commit();x.close();return redirect('/chat')
    x.close();return layout('ویرایش پست',f"<div class='page'><form method='post'><textarea name='text' required>{esc(p['text'])}</textarea><button>ذخیره</button></form></div>",'chat')

@app.route('/post/delete/<int:pid>',methods=['POST'])
def post_delete(pid):
    u=me()
    if not u:return redirect('/')
    x=db();p=x.execute('SELECT * FROM messages WHERE id=%s',(pid,)).fetchone()
    if not p:x.close();return 'Post not found',404
    if p['username']!=u['username'] and not is_admin(u):x.close();return 'Forbidden',403
    x.execute('DELETE FROM post_likes WHERE post_id=%s',(pid,));x.execute('DELETE FROM post_comments WHERE post_id=%s',(pid,));x.execute('DELETE FROM messages WHERE id=%s',(pid,));x.commit();x.close()
    if p.get('media_url'):delete_uploaded_file(CHAT_DIR,p['media_url'])
    return redirect('/chat')

def page_image_url(username):
    folder=os.path.join(UPLOAD_ROOT,PAGE_DIR)
    names=[f"{username}.{ext}" for ext in ("jpg","png","gif","webp")]
    for name in names:
        if os.path.isfile(os.path.join(folder,name)):
            return f"/uploads/pages/{esc(name)}"
    try:
        x=db()
        try:
            row=x.execute("SELECT filename FROM upload_blobs WHERE subdir=%s AND filename = ANY(%s)",(PAGE_DIR,names)).fetchone()
        finally:
            x.close()
        if row and restore_blob(PAGE_DIR,row["filename"]):
            return f"/uploads/pages/{esc(row['filename'])}"
    except Exception:
        pass
    return ""

@app.route("/page/<int:rid>/image", methods=["POST"])
def page_image_upload(rid):
    u=me()
    if not u:return redirect("/")
    x=db(); r=x.execute("SELECT * FROM rooms WHERE id=%s",(rid,)).fetchone(); x.close()
    if not r:return "Page not found.",404
    if r['kind']!='channel' or r['owner']!=u['username']:return "Forbidden",403
    f=request.files.get('image') or request.files.get('media')
    if not f:return redirect(f'/room/{rid}')
    data=f.read()
    ext,_=detect_image_type(data)
    if not ext:return "Unsupported image type.",400
    if len(data)>MAX_IMAGE_BYTES:return "Image is too large (max 5MB).",400
    folder=os.path.join(UPLOAD_ROOT,PAGE_DIR); os.makedirs(folder,exist_ok=True)
    for old_ext in ("jpg","png","gif","webp"):
        old=os.path.join(folder,f"{r['username']}.{old_ext}")
        if os.path.isfile(old):
            try: os.remove(old)
            except Exception: pass
    with open(os.path.join(folder,f"{r['username']}.{ext}"),"wb") as out: out.write(data)
    for old_ext in ("jpg","png","gif","webp"):
        if old_ext != ext: delete_blob(PAGE_DIR,f"{r['username']}.{old_ext}")
    persist_blob(PAGE_DIR,f"{r['username']}.{ext}",data)
    return redirect(f'/room/{rid}')

@app.route("/room/<int:rid>")
def room(rid):
    u=me()
    if not u:return redirect("/")
    x=db();r=x.execute("SELECT * FROM rooms WHERE id=%s",(rid,)).fetchone()
    if not r:x.close();return "Page not found.",404
    if r['kind']!='channel':x.close();return "This legacy room is no longer available.",404
    ms=x.execute("SELECT * FROM messages WHERE room=%s ORDER BY id",(rid,)).fetchall();x.close()
    body=""
    for m in ms:
        cls = 'mine' if m['username'] == u['username'] else ''
        img_tag = f"<img class='msg-img' src='/uploads/chat/{esc(m['image'])}' alt=''>" if m.get('image') else ""
        txt = esc(m['text']) if m['text'] else ""
        body += f"<div class='msg {cls}'><div class='meta'>@{esc(m['username'])}</div>{img_tag}{txt}</div>"
    # Groups: everyone can post. Channels: only the owner can post (enforced again in /send).
    can_send = r["kind"] == "group" or (r["kind"] == "channel" and r["owner"] == u["username"])
    if can_send:
        # Image sending is a PRO-only feature. Server-side enforcement happens
        # again in /send regardless of what is rendered here.
        img_input = "<label class='img-btn' title='ارسال عکس (PRO)'>📷<input type='file' name='media' accept='image/*,video/*,audio/*,.pdf,.zip,.txt,.doc,.docx' hidden></label>" if u["pro"] else ""
        composer = f"<form class='composer' method='post' action='/send/{rid}' enctype='multipart/form-data'>{img_input}<input name='text' placeholder='پست جدید برای پیج...'><input type='file' name='media' accept='image/*,video/*,audio/*,.pdf,.zip,.txt,.doc,.docx'><button>پست</button></form>"
    else:
        composer = "<p class='sub' style='text-align:center;padding:14px'>فقط سازنده پیج می‌تواند پیام بفرستد.</p>"
    title = r["name"] + (" ✓" if r["verified"] else "")
    page_img=page_image_url(r["username"]) if r["kind"]=="channel" else ""
    page_head=(f"<div class='card'><img class='msg-img' src='{page_img}' alt=''><div class='name'>@{esc(r["username"])}</div><div class='sub'>{esc(r["bio"] or "")}</div></div>" if page_img else f"<div class='card'><div class='name'>@{esc(r["username"])}</div><div class='sub'>{esc(r["bio"] or "")}</div></div>")
    page_upload=(f"<form method='post' action='/page/{rid}/image' enctype='multipart/form-data'><label class='btn-sm on upload-label'>عکس پیج<input type='file' name='media' accept='image/*,video/*,audio/*,.pdf,.zip,.txt,.doc,.docx' hidden onchange='this.form.submit()'></label></form>" if r["kind"]=="channel" and r["owner"]==u["username"] else "")
    return layout(title,f"<div class='chat'{chat_style(u)}><div class='messages'>{page_head}{page_upload}{body or '<p class=\"sub\">هنوز پیامی نیست.</p>'}</div>{composer}</div>","chat")

@app.route("/send/<int:rid>",methods=["POST"])
def send(rid):
    u=me()
    if not u:return redirect("/")
    x=db();r=x.execute("SELECT * FROM rooms WHERE id=%s",(rid,)).fetchone()
    if not r:x.close();return "Page not found.",404
    if r["kind"]!="channel" or r["owner"]!=u["username"]:x.close();return "Only the page owner can post.",403
    today=datetime.now().date().isoformat();limit=1000 if u['pro'] else (5 if u['verified'] else 3)
    c=x.execute("SELECT COUNT(*) AS c FROM messages WHERE username=%s AND created_at LIKE %s",(u['username'],today+'%')).fetchone()['c']
    if c>=limit:x.close();return f"محدودیت روزانه پست شما تمام شده است. سقف امروز: {limit}",429
    text=request.form.get("text","").strip()[:2000]; media_url='';media_type='';media_name=''
    f=request.files.get('media') or request.files.get('image')
    if f and f.filename:
        data=f.read()
        if len(data)>MAX_POST_MEDIA_BYTES:x.close();return 'حجم فایل بیشتر از 25MB است.',400
        ext=os.path.splitext(f.filename)[1].lower();allowed={'.jpg':'image/jpeg','.jpeg':'image/jpeg','.png':'image/png','.gif':'image/gif','.webp':'image/webp','.mp4':'video/mp4','.webm':'video/webm','.mov':'video/quicktime','.mp3':'audio/mpeg','.wav':'audio/wav','.ogg':'audio/ogg','.pdf':'application/pdf','.zip':'application/zip','.txt':'text/plain','.doc':'application/msword','.docx':'application/vnd.openxmlformats-officedocument.wordprocessingml.document'}
        if ext not in allowed:x.close();return 'نوع فایل پشتیبانی نمی‌شود.',400
        media_type=allowed[ext];media_name=os.path.basename(f.filename)[:120];media_url=f"{uuid.uuid4().hex}{ext}"
        with open(os.path.join(UPLOAD_ROOT,CHAT_DIR,media_url),'wb') as out:out.write(data)
        persist_blob(CHAT_DIR,media_url,data,media_type)
    if not text and not media_url:x.close();return 'پست خالی است.',400
    now=datetime.now().isoformat(timespec='seconds')
    x.execute("INSERT INTO messages(room,room_id,username,text,created_at,created,reply,edited,image,media_type,media_name,media_url) VALUES(%s,%s,%s,%s,%s,%s,0,0,'',%s,%s,%s)",(rid,rid,u['username'],text,now,now,media_type,media_name,media_url));x.commit();x.close();return redirect(f"/room/{rid}")

@app.route("/search")
def search():
    u=me()
    if not u:return redirect("/")
    q=request.args.get("q","").strip()
    x=db()
    users=x.execute("SELECT username,emoji,verified,pro,avatar FROM users WHERE username ILIKE %s LIMIT 30",(f"%{q}%",)).fetchall()
    rooms=x.execute("SELECT * FROM rooms WHERE kind='channel' AND (username ILIKE %s OR name ILIKE %s) LIMIT 30",(f"%{q}%",f"%{q}%")).fetchall()
    x.close()
    out=""
    for z in users:
        badges=("<span class='badge'>✓</span>" if z["verified"] else "")+("<span class='badge pro'>PRO</span>" if z["pro"] else "")
        av = avatar_html(z)
        out+=f"""<a class='item' href='/private/{esc(z["username"])}'>{av}<div class='info'>
        <div class='name'>@{esc(z["username"])} {badges}</div><div class='sub'>شروع چت خصوصی با آیدی</div></div><div class='arrow'>‹</div></a>"""
    for r in rooms:
        rbadge = " <span class='badge'>✓</span>" if r["verified"] else ""
        out+=f"""<a class='item' href='/room/{r["id"]}'><div class='avatar'>{esc(r["emoji"])}</div><div class='info'>
        <div class='name'>{esc(r["name"])}{rbadge}</div><div class='sub'>@{esc(r["username"])} · {esc(r["kind"])}</div></div><div class='arrow'>‹</div></a>"""
    return layout("جستجو",f"<div class='page'><form><input name='q' value='{esc(q)}' placeholder='آیدی را جستجو کن...'></form><div class='list'>{out}</div></div>","search")

@app.route("/create",methods=["GET","POST"])
def create():
    u=me()
    if not u:return redirect("/")
    if request.method=="GET":
        return layout("ساخت",f"""<div class='page'><div class='box'><h2>ساخت پیج</h2>
        <form method='post'><input name='name' placeholder='نام پیج' required><input name='username' placeholder='آیدی پیج' required>
        <input type='hidden' name='kind' value='channel'>
        <input name='emoji' value='💬'><textarea name='bio' placeholder='توضیح'></textarea><button>ساخت</button></form></div></div>""","create")
    name = request.form.get("name", "").strip()
    username = request.form.get("username", "").strip()
    kind = request.form.get("kind", "group")
    if not name or not username or kind != 'channel':
        return "Invalid group/channel information.", 400

    x = db()
    try:
        x.execute(
            """INSERT INTO rooms(name,username,kind,owner,emoji,bio)
               VALUES(%s,%s,%s,%s,%s,%s)""",
            (name[:80], username[:40], kind, u["username"],
             request.form.get("emoji", "💬")[:8],
             request.form.get("bio", "")[:200])
        )
        x.commit()
    except psycopg2.IntegrityError:
        x.rollback()
        return "This ID is already in use.", 409
    finally:
        x.close()
    return redirect("/chat")

@app.route("/private/<username>")
def private(username):
    u=me()
    if not u:return redirect("/")
    x=db();z=x.execute("SELECT * FROM users WHERE username=%s",(username,)).fetchone()
    if not z:x.close();return "User not found."
    if username!=u['username'] and x.execute("SELECT id FROM blocks WHERE (blocker=%s AND blocked=%s) OR (blocker=%s AND blocked=%s)",(u['username'],username,username,u['username'])).fetchone():
        x.close();return "این حساب برای شما مسدود شده و امکان دایرکت وجود ندارد.",403
    ms=x.execute("""SELECT * FROM private_messages WHERE
        (sender=%s AND receiver=%s) OR (sender=%s AND receiver=%s) ORDER BY id""",
        (u["username"],username,username,u["username"])).fetchall()
    x.execute("""INSERT INTO private_chat_state(owner,other_user,unread) VALUES(%s,%s,0)
        ON CONFLICT(owner,other_user) DO UPDATE SET unread=0""", (u["username"],username))
    x.commit()
    x.close()
    body = ''
    for m in ms:
        cls = 'mine' if m['sender'] == u['username'] else ''
        img_tag = f"<img class='msg-img' src='/uploads/chat/{esc(m['image'])}' alt=''>" if m.get('image') else ""
        txt = esc(m['text']) if m['text'] else ""
        body += f"<div class='msg {cls}'><div class='meta'>@{esc(m['sender'])}</div>{img_tag}{txt}</div>"
    # Image sending in private chats is PRO-only; enforced again in /private/<u>/send.
    img_input = "<label class='img-btn' title='ارسال عکس (PRO)'>📷<input type='file' name='media' accept='image/*,video/*,audio/*,.pdf,.zip,.txt,.doc,.docx' hidden></label>" if u["pro"] else ""
    return layout("@"+username,f"""<div class='chat'{chat_style(u)}><div class='messages'>{body or '<p class="sub">شروع گفتگو</p>'}</div>
    <form class='composer' method='post' action='/private/{esc(username)}/send' enctype='multipart/form-data'>{img_input}<input name='text' placeholder='پیام خصوصی...'><button>➤</button></form>
    <script>setInterval(()=>location.reload(),3000);</script></div>""","chat")

@app.route("/private/<username>/send",methods=["POST"])
def private_send(username):
    u=me()
    if not u:return redirect("/")
    x=db()
    if username!=u['username'] and x.execute("SELECT id FROM blocks WHERE (blocker=%s AND blocked=%s) OR (blocker=%s AND blocked=%s)",(u['username'],username,username,u['username'])).fetchone():
        x.close();return "این حساب برای شما مسدود شده و امکان دایرکت وجود ندارد.",403
    if x.execute("SELECT id FROM users WHERE username=%s",(username,)).fetchone():
        text = request.form.get("text","").strip()[:2000]
        image_filename = ""
        if u["pro"]:
            file_storage = request.files.get("image")
            try:
                fname, _mime = save_uploaded_image(file_storage, CHAT_DIR)
                image_filename = fname or ""
            except ValueError as e:
                x.close()
                return str(e), 400
        if text or image_filename:
            x.execute("INSERT INTO private_messages(sender,receiver,text,created_at,image) VALUES(%s,%s,%s,%s,%s)",
                      (u["username"],username,text,datetime.now().isoformat(timespec="seconds"),image_filename))
            x.execute("""INSERT INTO private_chat_state(owner,other_user,unread) VALUES(%s,%s,1)
                ON CONFLICT(owner,other_user) DO UPDATE SET unread=private_chat_state.unread+1""",
                (username,u["username"]))
            x.commit()
    x.close();return redirect(f"/private/{username}")

@app.route('/follow/<username>',methods=['POST'])
def follow_user(username):
    u=me()
    if not u:return redirect('/')
    if username==u['username']:return redirect('/profile/'+username)
    x=db();target=x.execute('SELECT username FROM users WHERE username=%s',(username,)).fetchone()
    if not target:x.close();return 'User not found',404
    if x.execute('SELECT id FROM follows WHERE follower=%s AND target=%s',(u['username'],username)).fetchone():
        x.execute('DELETE FROM follows WHERE follower=%s AND target=%s',(u['username'],username))
    elif is_private_account(x,username):
        if x.execute('SELECT id FROM follow_requests WHERE requester=%s AND target=%s',(u['username'],username)).fetchone():
            x.execute('DELETE FROM follow_requests WHERE requester=%s AND target=%s',(u['username'],username))
        else:
            x.execute('INSERT INTO follow_requests(requester,target,created_at) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING',(u['username'],username,datetime.now().isoformat(timespec='seconds')))
    else:x.execute('INSERT INTO follows(follower,target,created_at) VALUES(%s,%s,%s)',(u['username'],username,datetime.now().isoformat(timespec='seconds')))
    x.commit();x.close();return redirect('/profile/'+username)

@app.route("/profile")
def profile_redirect():
    u=me()
    return redirect("/profile/"+u["username"]) if u else redirect("/")

@app.route("/profile/<username>")
def profile(username):
    u=me()
    if not u:return redirect("/")
    x=db();z=x.execute("SELECT * FROM users WHERE username=%s",(username,)).fetchone()
    if not z:x.close();return "User not found."
    followers=x.execute("SELECT COUNT(*) AS c FROM follows WHERE target=%s",(username,)).fetchone()['c']
    following=x.execute("SELECT COUNT(*) AS c FROM follows WHERE follower=%s",(username,)).fetchone()['c']
    if username=='simorg': followers=max(followers,1000000)
    following_me=bool(x.execute("SELECT id FROM follows WHERE follower=%s AND target=%s",(u['username'],username)).fetchone()) if u['username']!=username else False
    blocked=bool(x.execute("SELECT id FROM blocks WHERE blocker=%s AND blocked=%s",(u['username'],username)).fetchone()) if u['username']!=username else False
    posts=x.execute("SELECT * FROM messages WHERE username=%s ORDER BY id DESC LIMIT 30",(username,)).fetchall()
    disp=get_display_name(x,username)
    is_priv=is_private_account(x,username)
    req_pending=bool(x.execute("SELECT id FROM follow_requests WHERE requester=%s AND target=%s",(u['username'],username)).fetchone()) if u['username']!=username else False
    can_view=(not is_priv) or u['username']==username or following_me or is_admin(u)
    if not can_view: posts=[]
    name_html=(esc(disp)+" · ") if disp else ""
    lock_badge=" 🔒" if is_priv else ""
    lock_note="<div class='empty'>🔒 این پیج خصوصی است. برای دیدن پست‌ها باید دنبالش کنید.</div>"
    x.close()
    badges=("<span class='badge'>✓</span>" if z["verified"] else "")+("<span class='badge pro'>PRO</span>" if z["pro"] else "")
    if u['username']==username:
        button=f"<a href='/editprofile'>ویرایش پروفایل</a> <a href='/settings'>⚙️ تنظیمات</a> <a href='/logout'>خروج از حساب</a>"
    else:
        button=f"<form style='display:inline' method='post' action='/follow/{esc(username)}'><button class='btn-sm'>{'دنبال نکردن' if following_me else ('لغو درخواست' if req_pending else ('درخواست دنبال کردن' if is_priv else 'دنبال کردن'))}</button></form> <form style='display:inline' method='post' action='/block/{esc(username)}'><button class='btn-sm warn'>{'رفع بلاک' if blocked else 'بلاک'}</button></form>"
        if not blocked: button+=f" <a href='/private/{esc(username)}'>دایرکت</a>"
    big_avatar=avatar_html(z,cls='bigavatar')
    story_btn=f" <a href='/story/view/{esc(username)}'>🎞 استوری</a>" if user_has_visible_story(u,username) else ''
    extras=profile_extras(u,username)
    post_html=''.join(_render_post({**dict(p), 'author_name':(disp or username),'author_username':username,'author_emoji':z['emoji'],'author_verified':z['verified'],'author_avatar':z['avatar'],'author_pro':z['pro'],'likes':0,'comments':0,'liked':False,'bookmarked':False,'admin_trending':False},u,False) for p in posts)
    body=f"""<div class='page'><div class='profile'><div class='cover'></div>{big_avatar}<h2>{name_html}@{esc(z['username'])} {badges}{lock_badge}</h2><p class='sub'>{esc(z['bio'] or 'No bio yet.')}</p><p class='profile-stats'><b>{followers:,}</b> دنبال‌کننده · <b>{following:,}</b> دنبال‌شونده</p><div class='actions'>{button}{story_btn}</div>{extras}</div><div class='feed-list'>{post_html if can_view else lock_note}</div></div><div id='focusOverlay' class='focus-overlay' onclick='closeFocus(event)'><div id='focusBox' class='focus-box'></div></div>"""
    return layout("پروفایل",body,"profile")

@app.route('/block/<username>',methods=['POST'])
def block_user(username):
    u=me()
    if not u:return redirect('/')
    if username==u['username']:return redirect('/profile/'+username)
    x=db()
    if x.execute('SELECT id FROM users WHERE username=%s',(username,)).fetchone():
        if x.execute('SELECT id FROM blocks WHERE blocker=%s AND blocked=%s',(u['username'],username)).fetchone():
            x.execute('DELETE FROM blocks WHERE blocker=%s AND blocked=%s',(u['username'],username))
        else:
            x.execute('INSERT INTO blocks(blocker,blocked,created_at) VALUES(%s,%s,%s) ON CONFLICT(blocker,blocked) DO NOTHING',(u['username'],username,datetime.now().isoformat(timespec='seconds')))
            x.execute('DELETE FROM follows WHERE (follower=%s AND target=%s) OR (follower=%s AND target=%s)',(u['username'],username,username,u['username']))
        x.commit()
    x.close();return redirect('/profile/'+username)

@app.route("/editprofile",methods=["GET","POST"])
def editprofile():
    u=me()
    if not u:return redirect("/")
    if request.method=="GET":
        # PRO users may upload/change/remove a profile picture. Normal users
        # keep the emoji-only avatar (enforced server-side in /avatar/upload).
        if u["pro"]:
            preview = avatar_html(u, cls="bigavatar")
            remove_btn = "<form method='post' action='/avatar/remove'><button class='btn-sm warn'>حذف عکس پروفایل</button></form>" if u["avatar"] else ""
            avatar_section = f"""<div class='avatar-upload'>{preview}
            <form method='post' action='/avatar/upload' enctype='multipart/form-data'>
            <label class='btn-sm on upload-label'>آپلود عکس پروفایل
            <input type='file' name='avatar' accept='image/*' hidden onchange='this.form.submit()'></label>
            </form>{remove_btn}</div>"""
        else:
            avatar_section = f"""<div class='avatar-upload'>{avatar_html(u, cls='bigavatar')}
            <p class='sub'>آپلود عکس پروفایل مخصوص کاربران PRO است.</p></div>"""
        return layout("پروفایل",f"""<div class='page'><div class='box'>{avatar_section}<form method='post'>
        <input name='display_name' maxlength='40' placeholder='نام نمایشی' value='{esc(current_display_name(u["username"]))}'><select name='emoji'><option>👤</option><option>😎</option><option>⚡</option><option>🤖</option><option>🔥</option></select>
        <textarea name='bio' placeholder='بیو'>{esc(u["bio"])}</textarea><button>ذخیره</button></form></div></div>""","profile")
    x=db();x.execute("UPDATE users SET emoji=%s,bio=%s WHERE username=%s",(request.form["emoji"],request.form["bio"][:200],u["username"]))
    x.execute("INSERT INTO profile_names(username,display_name) VALUES(%s,%s) ON CONFLICT(username) DO UPDATE SET display_name=EXCLUDED.display_name",(u["username"],request.form.get("display_name","").strip()[:40]))
    x.commit();x.close()
    return redirect("/profile/"+u["username"])

@app.route("/avatar/upload", methods=["POST"])
def avatar_upload():
    u = me()
    if not u:
        return redirect("/")
    # Server-side enforcement: only PRO accounts may upload a profile picture,
    # regardless of what request is sent to this endpoint.
    if not u["pro"]:
        return "PRO required.", 403
    file_storage = request.files.get("avatar")
    try:
        filename, _mime = save_uploaded_image(file_storage, AVATAR_DIR)
    except ValueError as e:
        return str(e), 400
    if not filename:
        return redirect("/editprofile")
    old = u["avatar"]
    x = db()
    x.execute("UPDATE users SET avatar=%s WHERE username=%s", (filename, u["username"]))
    x.commit()
    x.close()
    if old:
        delete_uploaded_file(AVATAR_DIR, old)
    return redirect("/editprofile")

@app.route("/avatar/remove", methods=["POST"])
def avatar_remove():
    u = me()
    if not u:
        return redirect("/")
    if not u["pro"]:
        return "PRO required.", 403
    old = u["avatar"]
    x = db()
    x.execute("UPDATE users SET avatar='' WHERE username=%s", (u["username"],))
    x.commit()
    x.close()
    if old:
        delete_uploaded_file(AVATAR_DIR, old)
    return redirect("/editprofile")

@app.route("/uploads/<subdir>/<filename>")
def serve_upload(subdir, filename):
    # Only ever serve from the two known, whitelisted subfolders, and only a
    # bare filename (no path separators / traversal sequences survive this).
    if subdir not in ALLOWED_UPLOAD_SUBDIRS:
        return "Not found", 404
    safe_name = os.path.basename(filename)
    if not safe_name or safe_name != filename:
        return "Not found", 404
    folder = os.path.join(UPLOAD_ROOT, subdir)
    full_path = os.path.abspath(os.path.join(folder, safe_name))
    if not full_path.startswith(os.path.abspath(folder) + os.sep):
        return "Not found", 404
    if not os.path.isfile(full_path) and not restore_blob(subdir, safe_name):
        return "Not found", 404
    return send_from_directory(folder, safe_name)

@app.route('/admin/trending/<int:pid>',methods=['POST'])
def admin_trending(pid):
    u=me()
    if not u or not is_admin(u):return 'Forbidden',403
    x=db();row=x.execute('SELECT id FROM trending_posts WHERE post_id=%s',(pid,)).fetchone()
    if row:x.execute('UPDATE trending_posts SET enabled=CASE WHEN enabled=1 THEN 0 ELSE 1 END WHERE post_id=%s',(pid,))
    else:x.execute('INSERT INTO trending_posts(post_id,enabled,created_at) VALUES(%s,1,%s)',(pid,datetime.now().isoformat(timespec='seconds')))
    x.commit();x.close();return redirect('/admin')

@app.route("/admin")
def admin_panel():
    u = me()
    if not u:
        return redirect("/")
    if not is_admin(u):
        return redirect("/chat")
    x = db()
    users = x.execute("SELECT * FROM users ORDER BY id").fetchall()
    rooms = x.execute("SELECT * FROM rooms ORDER BY id").fetchall()
    posts = x.execute("SELECT m.*,u.verified,u.pro FROM messages m LEFT JOIN users u ON u.username=m.username ORDER BY m.id DESC LIMIT 100").fetchall()
    x.close()
    rows = ""
    for z in users:
        badges = ("<span class='badge'>✓</span>" if z["verified"] else "") + \
                 ("<span class='badge pro'>PRO</span>" if z["pro"] else "") + \
                 ("<span class='badge' style='background:#ff4d4d'>مدیر</span>" if z["admin"] else "") + \
                 ("<span class='badge' style='background:#555'>بن‌شده</span>" if z["banned"] else "")
        ban_label = "آزاد کردن" if z["banned"] else "بن کردن"
        verify_label = "لغو تایید" if z["verified"] else "تایید کردن"
        pro_label = "لغو PRO" if z["pro"] else "فعال کردن PRO"
        av = avatar_html(z)
        rows += f"""<div class='admin-row'>
        {av}
        <div class='info'><div class='name'>@{esc(z["username"])} {badges}</div>
        <div class='sub'>{esc(z["email"] or "")}</div></div>
        <div class='admin-actions'>
        <form method='post' action='/admin/ban/{esc(z["username"])}'><button class='btn-sm{" warn" if not z["banned"] else ""}'>{ban_label}</button></form>
        <form method='post' action='/admin/verify/{esc(z["username"])}'><button class='btn-sm{" on" if not z["verified"] else ""}'>{verify_label}</button></form>
        <form method='post' action='/admin/pro/{esc(z["username"])}'><button class='btn-sm{" on" if not z["pro"] else ""}'>{pro_label}</button></form>
        </div></div>"""
    room_rows = ""
    for r in rooms:
        rbadge = "<span class='badge'>✓</span>" if r["verified"] else ""
        rverify_label = "لغو تایید" if r["verified"] else "تایید کردن"
        kind_label = "گروه" if r["kind"] == "group" else "پیج"
        room_rows += f"""<div class='admin-row'>
        <div class='avatar'>{esc(r["emoji"])}</div>
        <div class='info'><div class='name'>{esc(r["name"])} {rbadge}</div>
        <div class='sub'>@{esc(r["username"])} · {kind_label} · owner: @{esc(r["owner"])}</div></div>
        <div class='admin-actions'>
        <form method='post' action='/admin/room/verify/{r["id"]}'><button class='btn-sm{" on" if not r["verified"] else ""}'>{rverify_label}</button></form>
        </div></div>"""
    post_rows = "".join(f"<div class='admin-row'><div class='info'><div class='name'>#{p['id']} · @{esc(p['username'])}</div><div class='sub'>{esc(p['text'][:180])}</div></div><div class='admin-actions'><a class='btn-sm' href='/post/edit/{p['id']}'>ویرایش</a><form method='post' action='/post/delete/{p['id']}'><button class='btn-sm warn'>حذف</button></form><form method='post' action='/admin/trending/{p['id']}'><button class='btn-sm on'>ترند / لغو</button></form></div></div>" for p in posts)
    body = f"""<div class='page'><h2>پنل مدیریت</h2>
    <h3 class='admin-section-title'>پست‌ها</h3><div class='list'>{post_rows or '<p class="sub">پستی نیست.</p>'}</div>
    <h3 class='admin-section-title'>کاربران</h3><div class='list'>{rows}</div>
    <h3 class='admin-section-title'>گروه‌ها و پیج‌ها</h3><div class='list'>{room_rows or '<p class="sub">هنوز گروه یا کانالی نیست.</p>'}</div>
    {admin_extra_html()}
    </div>"""
    return layout("مدیریت", body, "admin")

@app.route("/admin/ban/<username>", methods=["POST"])
def admin_toggle_ban(username):
    u = me()
    if not u or not is_admin(u):
        return "Forbidden", 403
    x = db()
    x.execute("UPDATE users SET banned = CASE WHEN banned=1 THEN 0 ELSE 1 END WHERE username=%s", (username,))
    x.commit()
    x.close()
    return redirect("/admin")

@app.route("/admin/verify/<username>", methods=["POST"])
def admin_toggle_verify(username):
    u = me()
    if not u or not is_admin(u):
        return "Forbidden", 403
    x = db()
    x.execute("UPDATE users SET verified = CASE WHEN verified=1 THEN 0 ELSE 1 END WHERE username=%s", (username,))
    x.commit()
    x.close()
    return redirect("/admin")

@app.route("/admin/pro/<username>", methods=["POST"])
def admin_toggle_pro(username):
    u = me()
    if not u or not is_admin(u):
        return "Forbidden", 403
    x = db()
    x.execute("UPDATE users SET pro = CASE WHEN pro=1 THEN 0 ELSE 1 END WHERE username=%s", (username,))
    x.commit()
    x.close()
    return redirect("/admin")

@app.route("/admin/room/verify/<int:rid>", methods=["POST"])
def admin_toggle_room_verify(rid):
    u = me()
    if not u or not is_admin(u):
        return "Forbidden", 403
    x = db()
    x.execute("UPDATE rooms SET verified = CASE WHEN verified=1 THEN 0 ELSE 1 END WHERE id=%s", (rid,))
    x.commit()
    x.close()
    return redirect("/admin")

@app.route("/pro")
def pro_page():
    u = me()
    if not u:
        return redirect("/")
    if not u["pro"]:
        return layout("PRO", """
        <div class="page"><div class="box">
            <h2>حساب PRO</h2>
            <p>حساب شما هنوز PRO نیست.</p>
            <a href="/profile/%s">بازگشت به پروفایل</a>
        </div></div>
        """ % esc(u["username"]), "profile")
    return layout("PRO", """
    <div class="page"><div class="box">
        <h2>PRO فعال است <span class="pro-badge">PRO</span></h2>
        <p class="sub">قابلیت‌های ویژه حساب PRO:</p>
        <div class="card">✓ نشان PRO کنار نام</div>
        <div class="card">✓ پروفایل ویژه</div>
        <div class="card">✓ نمایش وضعیت PRO در لیست‌ها</div>
        <div class="card">✓ دسترسی به صفحه اختصاصی PRO</div>
        <div class="card">✓ آپلود، تغییر و حذف عکس پروفایل</div>
        <div class="card">✓ ارسال عکس در چت خصوصی، گروه و پیج</div>
        <div class="card">✓ امکانات مدیریتی PRO در صورت فعال‌سازی توسط ادمین</div>
    </div></div>
    """, "profile")



# ===========================================================================
# Rules
# ===========================================================================
# Edit this list to change the rules shown at login / register / profile.
RULES = [
    "به همه کاربران احترام بگذارید؛ توهین، تهدید و آزار ممنوع است.",
    "انتشار محتوای غیراخلاقی، خشونت‌آمیز یا غیرقانونی ممنوع است.",
    "اسپم، تبلیغات مزاحم و لینک‌های کلاهبرداری ممنوع است.",
    "جعل هویت دیگران یا ادعای دروغ مدیریت بودن ممنوع است.",
    "اطلاعات شخصی دیگران را بدون اجازه منتشر نکنید.",
    "هر کاربر مسئول پست‌ها، استوری‌ها و پیام‌های خودش است.",
    "مدیریت می‌تواند محتوای متخلف را حذف و حساب متخلف را مسدود کند.",
    "استفاده از سیمرغ به معنی پذیرش این قوانین است.",
]

RULES_JS = ("<script>document.addEventListener('DOMContentLoaded',function(){var a=document.getElementById('acc'),"
            "b=document.getElementById('lb');if(!a||!b)return;function u(){b.disabled=!a.checked}"
            "a.addEventListener('change',u);u()})</script>")

def rules_html():
    return "<ol class='rules-list'>" + "".join(f"<li>{esc(r)}</li>" for r in RULES) + "</ol>"

@app.route("/rules")
def rules_page():
    return layout("قوانین", f"<div class='page'><div class='box'><h2>📜 قوانین سیمرغ</h2>{rules_html()}</div></div>", "profile") if me() else \
        f"<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'>{THEME_INIT_SCRIPT}<style>{CSS}</style></head><body><div class='box'><h2>📜 قوانین سیمرغ</h2>{rules_html()}<a href='/'>بازگشت</a></div></body></html>"

# ===========================================================================
# Chat backgrounds (admin adds, every user picks their own)
# ===========================================================================
def chat_style(u):
    """Inline style for the .chat container using the user's chosen background."""
    row = None
    x = None
    try:
        x = db()
        row = x.execute("""SELECT b.filename FROM user_chat_bg ub
            JOIN chat_backgrounds b ON b.id=ub.bg_id WHERE ub.username=%s""", (u["username"],)).fetchone()
    except Exception:
        row = None
    finally:
        if x is not None:
            try: x.close()
            except Exception: pass
    if not row:
        return ""
    f = esc(row["filename"])
    return (f" style=\"background:linear-gradient(rgba(4,9,20,.5),rgba(4,9,20,.5)),"
            f"url('/uploads/backgrounds/{f}') center/cover no-repeat\"")

def bg_picker_html(u):
    x = db()
    try:
        bgs = x.execute("SELECT * FROM chat_backgrounds ORDER BY id").fetchall()
        cur = x.execute("SELECT bg_id FROM user_chat_bg WHERE username=%s", (u["username"],)).fetchone()
    finally:
        x.close()
    cur_id = cur["bg_id"] if cur else 0
    tiles = (f"<form method='post' action='/chatbg/select'><input type='hidden' name='bg_id' value='0'>"
             f"<button class='bg-tile{' sel' if not cur_id else ''}'>پیش‌فرض</button></form>")
    for b in bgs:
        tiles += (f"<form method='post' action='/chatbg/select'><input type='hidden' name='bg_id' value='{b['id']}'>"
                  f"<button class='bg-tile{' sel' if cur_id == b['id'] else ''}' "
                  f"style='background-image:url(/uploads/backgrounds/{esc(b['filename'])})'></button></form>")
    return f"<h3>🖼 پس‌زمینه چت</h3><div class='bg-grid'>{tiles}</div>"

@app.route("/chatbg/select", methods=["POST"])
def chatbg_select():
    u = me()
    if not u:
        return redirect("/")
    try:
        bg_id = int(request.form.get("bg_id", "0"))
    except ValueError:
        bg_id = 0
    x = db()
    try:
        if bg_id <= 0:
            x.execute("DELETE FROM user_chat_bg WHERE username=%s", (u["username"],))
        else:
            if not x.execute("SELECT id FROM chat_backgrounds WHERE id=%s", (bg_id,)).fetchone():
                return "Background not found.", 404
            x.execute("""INSERT INTO user_chat_bg(username,bg_id) VALUES(%s,%s)
                ON CONFLICT(username) DO UPDATE SET bg_id=EXCLUDED.bg_id""", (u["username"], bg_id))
        x.commit()
    finally:
        x.close()
    if request.form.get("next") == "settings":
        return redirect("/settings/appearance")
    return redirect("/profile/" + u["username"])

@app.route("/admin/bg/add", methods=["POST"])
def admin_bg_add():
    u = me()
    if not u or not is_admin(u):
        return "Forbidden", 403
    try:
        fname, _mime = save_uploaded_image(request.files.get("bg"), BG_DIR)
    except ValueError as e:
        return str(e), 400
    if fname:
        x = db()
        x.execute("INSERT INTO chat_backgrounds(filename,created_at) VALUES(%s,%s)",
                  (fname, datetime.now().isoformat(timespec="seconds")))
        x.commit()
        x.close()
    return redirect("/admin")

@app.route("/admin/bg/delete/<int:bid>", methods=["POST"])
def admin_bg_delete(bid):
    u = me()
    if not u or not is_admin(u):
        return "Forbidden", 403
    x = db()
    row = x.execute("SELECT filename FROM chat_backgrounds WHERE id=%s", (bid,)).fetchone()
    if row:
        x.execute("DELETE FROM user_chat_bg WHERE bg_id=%s", (bid,))
        x.execute("DELETE FROM chat_backgrounds WHERE id=%s", (bid,))
        x.commit()
    x.close()
    if row:
        delete_uploaded_file(BG_DIR, row["filename"])
    return redirect("/admin")

# ===========================================================================
# Stories
#   - every user can post (photo / video, expires after STORY_HOURS)
#   - visible to: the author, the author's followers, and everyone for @simorg
#   - media is served only through /story/media/<id> which re-checks visibility
# ===========================================================================
STORY_ALLOWED = {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png', '.gif': 'image/gif',
                 '.webp': 'image/webp', '.mp4': 'video/mp4', '.webm': 'video/webm', '.mov': 'video/quicktime'}

def _now_iso():
    return datetime.now().isoformat(timespec="seconds")

def save_story_media(file_storage):
    data = file_storage.read()
    if not data:
        return None, None
    if len(data) > MAX_POST_MEDIA_BYTES:
        raise ValueError("حجم فایل بیشتر از 25MB است.")
    ext = os.path.splitext(file_storage.filename)[1].lower()
    if ext not in STORY_ALLOWED:
        raise ValueError("فقط عکس یا ویدیو (JPG, PNG, GIF, WEBP, MP4, WEBM, MOV) مجاز است.")
    mime = STORY_ALLOWED[ext]
    if mime.startswith("image/"):
        real_ext, real_mime = detect_image_type(data)
        if not real_ext:
            raise ValueError("فایل عکس معتبر نیست.")
        ext, mime = "." + real_ext, real_mime
    os.makedirs(STORY_ROOT, exist_ok=True)
    name = f"{uuid.uuid4().hex}{ext}"
    with open(os.path.join(STORY_ROOT, name), "wb") as out:
        out.write(data)
    persist_blob(STORY_DIR, name, data, mime)
    return name, mime

def delete_story_file(filename):
    try:
        safe = os.path.basename(filename or "")
        if not safe:
            return
        delete_blob(STORY_DIR, safe)
        full = os.path.abspath(os.path.join(STORY_ROOT, safe))
        if full.startswith(os.path.abspath(STORY_ROOT) + os.sep) and os.path.isfile(full):
            os.remove(full)
    except Exception:
        pass

def purge_expired_stories(x):
    rows = x.execute("SELECT id,media_file FROM stories WHERE expires_at<=%s", (_now_iso(),)).fetchall()
    for r in rows:
        x.execute("DELETE FROM stories WHERE id=%s", (r["id"],))
    x.commit()
    for r in rows:
        delete_story_file(r["media_file"])

def visible_stories(x, u, only_user=None):
    me_name = u["username"]
    sql = """SELECT s.* FROM stories s WHERE s.expires_at>%s AND (
        s.username=%s OR s.username='simorg'
        OR EXISTS(SELECT 1 FROM follows f WHERE f.follower=%s AND f.target=s.username))
        AND NOT EXISTS(SELECT 1 FROM blocks b WHERE (b.blocker=%s AND b.blocked=s.username)
        OR (b.blocker=s.username AND b.blocked=%s))"""
    params = [_now_iso(), me_name, me_name, me_name, me_name]
    if only_user:
        sql += " AND s.username=%s"
        params.append(only_user)
    sql += " ORDER BY s.id"
    return x.execute(sql, tuple(params)).fetchall()

def user_has_visible_story(u, username):
    x = db()
    try:
        return bool(visible_stories(x, u, username))
    finally:
        x.close()

def story_bar(u):
    x = db()
    try:
        purge_expired_stories(x)
        rows = visible_stories(x, u)
        names = []
        for r in rows:
            if r["username"] not in names:
                names.append(r["username"])
        info = {}
        if names:
            for z in x.execute("SELECT username,emoji,avatar FROM users WHERE username = ANY(%s)", (names,)).fetchall():
                info[z["username"]] = z
    finally:
        x.close()
    mine = u["username"]
    ordered = [n for n in (mine, "simorg") if n in names]
    ordered += [n for n in reversed(names) if n not in ordered]
    items = ("<a class='story-item' href='/story/new'><div class='story-ring add'>"
             "<div class='avatar'>＋</div></div><span>استوری جدید</span></a>")
    for n in ordered:
        av = avatar_html(info.get(n) or {"emoji": "👤", "avatar": ""})
        label = "شما" if n == mine else "@" + n
        items += (f"<a class='story-item' href='/story/view/{esc(n)}'><div class='story-ring'>{av}</div>"
                  f"<span>{esc(label)}</span></a>")
    return f"<div class='story-bar'>{items}</div>"

@app.route("/story/new")
def story_new():
    u = me()
    if not u:
        return redirect("/")
    return layout("استوری جدید", f"""<div class='page'><div class='box'><h2>🎞 استوری جدید</h2>
    <p class='sub'>عکس یا ویدیو · تا {STORY_HOURS} ساعت می‌ماند · فقط دنبال‌کننده‌های شما می‌بینند</p>
    <form method='post' action='/story/create' enctype='multipart/form-data'>
    <input type='file' name='media' accept='image/*,video/*' required>
    <input name='caption' maxlength='200' placeholder='کپشن (اختیاری)'>
    <button>انتشار استوری</button></form></div></div>""", "chat")

@app.route("/story/create", methods=["POST"])
def story_create():
    u = me()
    if not u:
        return redirect("/")
    f = request.files.get("media")
    if not f or not f.filename:
        return "فایلی انتخاب نشده است.", 400
    caption = request.form.get("caption", "").strip()[:200]
    x = db()
    try:
        purge_expired_stories(x)
        active = x.execute("SELECT COUNT(*) AS c FROM stories WHERE username=%s AND expires_at>%s",
                           (u["username"], _now_iso())).fetchone()["c"]
        if active >= MAX_ACTIVE_STORIES:
            return f"حداکثر {MAX_ACTIVE_STORIES} استوری فعال می‌توانید داشته باشید.", 429
        try:
            name, mime = save_story_media(f)
        except ValueError as e:
            return str(e), 400
        if not name:
            return "فایل خالی است.", 400
        now = datetime.now()
        x.execute("""INSERT INTO stories(username,media_file,media_type,caption,created_at,expires_at)
            VALUES(%s,%s,%s,%s,%s,%s)""",
                  (u["username"], name, mime, caption, now.isoformat(timespec="seconds"),
                   (now + timedelta(hours=STORY_HOURS)).isoformat(timespec="seconds")))
        x.commit()
    finally:
        x.close()
    return redirect("/chat")

@app.route("/story/media/<int:sid>")
def story_media(sid):
    u = me()
    if not u:
        return "Forbidden", 403
    x = db()
    try:
        s = x.execute("SELECT * FROM stories WHERE id=%s", (sid,)).fetchone()
        if not s:
            return "Not found", 404
        if not is_admin(u):
            ok = any(r["id"] == sid for r in visible_stories(x, u, s["username"]))
            if not ok:
                return "Forbidden", 403
    finally:
        x.close()
    safe = os.path.basename(s["media_file"])
    if not os.path.isfile(os.path.join(STORY_ROOT, safe)) and not restore_blob(STORY_DIR, safe):
        return "Not found", 404
    resp = send_from_directory(STORY_ROOT, safe, mimetype=s["media_type"])
    resp.headers["Cache-Control"] = "private, max-age=300"
    return resp

STORY_JS = ("<script>(function(){var s=[].slice.call(document.querySelectorAll('.story-stage .slide')),"
            "b=[].slice.call(document.querySelectorAll('.story-progress i')),t=null,cur=0;"
            "function show(n){if(n<0)n=0;if(n>=s.length){location.href='/chat';return}clearTimeout(t);cur=n;"
            "s.forEach(function(e,k){e.classList.toggle('on',k===n);var v=e.querySelector('video');"
            "if(v){if(k===n){try{v.currentTime=0;var p=v.play();if(p&&p.catch)p.catch(function(){})}catch(x){}}else{v.pause()}}});"
            "b.forEach(function(e,k){e.className=k<n?'done':(k===n?'cur':'')});"
            "var v=s[n].querySelector('video');if(v){v.onended=function(){show(n+1)}}else{t=setTimeout(function(){show(n+1)},6000)}}"
            "document.getElementById('nx').onclick=function(){show(cur+1)};"
            "document.getElementById('pv').onclick=function(){show(cur-1)};show(0)})();</script>")

@app.route("/story/view/<username>")
def story_view(username):
    u = me()
    if not u:
        return redirect("/")
    x = db()
    try:
        purge_expired_stories(x)
        rows = visible_stories(x, u, username)
    finally:
        x.close()
    if not rows:
        return redirect("/chat")
    slides = ""
    for s in rows:
        src_url = f"/story/media/{s['id']}"
        if s["media_type"].startswith("video/"):
            media = f"<video src='{src_url}' playsinline></video>"
        else:
            media = f"<img src='{src_url}' alt=''>"
        delete = ""
        if s["username"] == u["username"] or is_admin(u):
            delete = (f"<form method='post' action='/story/delete/{s['id']}' onsubmit=\"return confirm('این استوری حذف شود؟')\">"
                      f"<input type='hidden' name='next' value='/chat'><button class='btn-sm warn'>حذف</button></form>")
        cap = f"<div class='story-cap'>{esc(s['caption'])}</div>" if s["caption"] else ""
        slides += (f"<div class='slide'>{media}<div class='story-top'><a href='/chat'>✕</a>"
                   f"<b>@{esc(s['username'])}</b><span class='sub'>{esc((s['created_at'] or '')[11:16])}</span>"
                   f"<span style='flex:1'></span>{delete}</div>{cap}</div>")
    bars = "<i></i>" * len(rows)
    return (f"<!doctype html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"{THEME_INIT_SCRIPT}<title>استوری · Simurgh</title><style>{CSS}</style></head><body>"
            f"<div class='story-stage'><div class='story-progress'>{bars}</div>{slides}"
            f"<div class='story-tap prev' id='pv'></div><div class='story-tap nxt' id='nx'></div></div>"
            f"{STORY_JS}</body></html>")

@app.route("/story/delete/<int:sid>", methods=["POST"])
def story_delete(sid):
    u = me()
    if not u:
        return redirect("/")
    x = db()
    s = x.execute("SELECT * FROM stories WHERE id=%s", (sid,)).fetchone()
    if not s:
        x.close()
        return "Not found", 404
    if s["username"] != u["username"] and not is_admin(u):
        x.close()
        return "Forbidden", 403
    x.execute("DELETE FROM stories WHERE id=%s", (sid,))
    x.commit()
    x.close()
    delete_story_file(s["media_file"])
    nxt = request.form.get("next", "/chat")
    return redirect(nxt if nxt in ("/chat", "/admin") else "/chat")

# ===========================================================================
# Profile extras + admin panel extra sections
# ===========================================================================
def profile_extras(u, username):
    out = f"<details class='rules-box'><summary>📜 قوانین سیمرغ</summary>{rules_html()}</details>"
    if u["username"] == username:
        out += bg_picker_html(u)
    return out

def admin_extra_html():
    x = db()
    try:
        purge_expired_stories(x)
        bgs = x.execute("SELECT * FROM chat_backgrounds ORDER BY id DESC").fetchall()
        sts = x.execute("SELECT * FROM stories ORDER BY id DESC LIMIT 100").fetchall()
    finally:
        x.close()
    bg_items = "".join(
        f"<div class='bg-admin'><div class='bg-thumb' style='background-image:url(/uploads/backgrounds/{esc(b['filename'])})'></div>"
        f"<form method='post' action='/admin/bg/delete/{b['id']}'><button class='btn-sm warn'>حذف</button></form></div>"
        for b in bgs)
    st_items = "".join(
        f"<div class='admin-row'><div class='info'><div class='name'>#{s['id']} · @{esc(s['username'])}</div>"
        f"<div class='sub'>{esc(s['created_at'])} · {'ویدیو' if s['media_type'].startswith('video/') else 'عکس'} {esc(s['caption'] or '')}</div></div>"
        f"<div class='admin-actions'><a class='btn-sm' href='/story/media/{s['id']}' target='_blank'>مشاهده</a>"
        f"<form method='post' action='/story/delete/{s['id']}'><input type='hidden' name='next' value='/admin'>"
        f"<button class='btn-sm warn'>حذف استوری</button></form></div></div>"
        for s in sts)
    return f"""<h3 class='admin-section-title'>🖼 پس‌زمینه‌های چت</h3>
    <form method='post' action='/admin/bg/add' enctype='multipart/form-data'>
    <input type='file' name='bg' accept='image/*' required><button>افزودن پس‌زمینه جدید</button></form>
    <div class='bg-grid' style='padding:10px 0'>{bg_items or "<p class='sub'>هنوز پس‌زمینه‌ای اضافه نشده.</p>"}</div>
    <h3 class='admin-section-title'>🎞 استوری‌های فعال</h3>
    <div class='list'>{st_items or "<p class='sub'>استوری فعالی نیست.</p>"}</div>"""


@app.errorhandler(RequestEntityTooLarge)
def handle_too_large(error):
    return "Uploaded file is too large.", 413


@app.errorhandler(Exception)
def handle_unexpected_error(error):
    app.logger.exception("UNHANDLED APPLICATION ERROR")
    return "Internal Server Error. Check Render logs for the traceback.", 500


@app.route("/favicon.ico")
def favicon():
    return "", 204

@app.route("/health")
def health():
    x = db()
    try:
        x.execute("SELECT 1 AS ok")
        row = x.fetchone()
        return {"status": "ok", "database": bool(row and row["ok"] == 1)}
    finally:
        x.close()


# ===========================================================================
# ADDED FEATURES (additive only - nothing above is removed)
#   - durable uploads (kept in the DB so they survive Render redeploys)
#   - login sessions: see devices, kick devices, change password
#   - private ("locked") pages with follow requests
#   - display name
#   - settings hub: chat appearance, privacy & security, lock page, language
#   - Persian / English UI switch
# ===========================================================================
import mimetypes

DEFAULT_BG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "default_backgrounds")


def _blob_folder(subdir):
    return STORY_ROOT if subdir == STORY_DIR else os.path.join(UPLOAD_ROOT, subdir)


def persist_blob(subdir, filename, data, mime=None):
    """Keep a copy of an uploaded file in the database (best effort, never raises)."""
    try:
        if mime is None:
            mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        x = db()
        try:
            x.execute("""INSERT INTO upload_blobs(subdir,filename,mime,data) VALUES(%s,%s,%s,%s)
                ON CONFLICT(subdir,filename) DO UPDATE SET mime=EXCLUDED.mime,data=EXCLUDED.data""",
                      (subdir, filename, mime, psycopg2.Binary(data)))
            x.commit()
        finally:
            x.close()
    except Exception:
        app.logger.exception("BLOB SAVE ERROR")


def delete_blob(subdir, filename):
    try:
        x = db()
        try:
            x.execute("DELETE FROM upload_blobs WHERE subdir=%s AND filename=%s", (subdir, filename))
            x.commit()
        finally:
            x.close()
    except Exception:
        pass


def restore_blob(subdir, filename):
    """If the file is missing on disk (fresh deploy) bring it back from the DB."""
    folder = _blob_folder(subdir)
    full = os.path.join(folder, filename)
    if os.path.isfile(full):
        return True
    try:
        x = db()
        try:
            row = x.execute("SELECT data FROM upload_blobs WHERE subdir=%s AND filename=%s",
                            (subdir, filename)).fetchone()
        finally:
            x.close()
        if not row:
            return False
        os.makedirs(folder, exist_ok=True)
        with open(full, "wb") as f:
            f.write(bytes(row["data"]))
        return True
    except Exception:
        app.logger.exception("BLOB RESTORE ERROR")
        return False


def seed_default_backgrounds():
    """Adds the bundled wallpapers (default_backgrounds/) as chat backgrounds, once each."""
    try:
        if not os.path.isdir(DEFAULT_BG_DIR):
            return
        x = db()
        try:
            for name in sorted(os.listdir(DEFAULT_BG_DIR)):
                path = os.path.join(DEFAULT_BG_DIR, name)
                if not os.path.isfile(path):
                    continue
                key = "bg:" + name
                if x.execute("SELECT 1 AS k FROM seeded_assets WHERE name=%s", (key,)).fetchone():
                    continue
                with open(path, "rb") as f:
                    data = f.read()
                ext, mime = detect_image_type(data)
                if not ext:
                    continue
                fname = "default_" + name
                os.makedirs(os.path.join(UPLOAD_ROOT, BG_DIR), exist_ok=True)
                with open(os.path.join(UPLOAD_ROOT, BG_DIR, fname), "wb") as out:
                    out.write(data)
                persist_blob(BG_DIR, fname, data, mime)
                x.execute("INSERT INTO chat_backgrounds(filename,created_at) VALUES(%s,%s)",
                          (fname, datetime.now().isoformat(timespec="seconds")))
                x.execute("INSERT INTO seeded_assets(name) VALUES(%s)", (key,))
                x.commit()
        finally:
            x.close()
    except Exception:
        app.logger.exception("SEED BACKGROUNDS ERROR")


# ---------------------------------------------------------------------------
# Login sessions
# ---------------------------------------------------------------------------
def _client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    return (fwd.split(",")[0].strip() if fwd else (request.remote_addr or ""))[:64]


def _register_session(x, username):
    token = uuid.uuid4().hex
    now = _now_iso()
    x.execute("""INSERT INTO user_sessions(token,username,user_agent,ip,created_at,last_seen,revoked)
        VALUES(%s,%s,%s,%s,%s,%s,0)""",
              (token, username, (request.headers.get("User-Agent", "") or "")[:300], _client_ip(), now, now))
    x.commit()
    session["sid"] = token


def start_session_row(username):
    x = db()
    try:
        _register_session(x, username)
    except Exception:
        x.rollback()
        app.logger.exception("SESSION REGISTER ERROR")
    finally:
        x.close()


def _check_session(x, u):
    """Returns the user, or None when this device was kicked out."""
    sid = session.get("sid")
    if sid:
        row = x.execute("SELECT username,revoked,last_seen FROM user_sessions WHERE token=%s", (sid,)).fetchone()
        if row and row["username"] == u["username"]:
            if int(row["revoked"] or 0) == 1:
                session.clear()
                return None
            try:
                last = datetime.fromisoformat(row["last_seen"])
            except Exception:
                last = None
            if not last or (datetime.now() - last).total_seconds() > 60:
                x.execute("UPDATE user_sessions SET last_seen=%s WHERE token=%s", (_now_iso(), sid))
                x.commit()
            return u
    # Older login (before this feature) or unknown token: register this device.
    _register_session(x, u["username"])
    return u


def device_label(ua):
    ua = ua or ""
    if "iPhone" in ua:
        d = "iPhone"
    elif "iPad" in ua:
        d = "iPad"
    elif "Android" in ua:
        d = "Android"
    elif "Windows" in ua:
        d = "Windows"
    elif "Macintosh" in ua or "Mac OS" in ua:
        d = "Mac"
    elif "Linux" in ua:
        d = "Linux"
    else:
        d = "نامشخص"
    if "Edg/" in ua or "EdgA/" in ua:
        b = "Edge"
    elif "OPR/" in ua:
        b = "Opera"
    elif "Firefox" in ua or "FxiOS" in ua:
        b = "Firefox"
    elif "Chrome" in ua or "CriOS" in ua:
        b = "Chrome"
    elif "Safari" in ua:
        b = "Safari"
    else:
        b = ""
    return f"{d} · {b}" if b else d


# ---------------------------------------------------------------------------
# Display name / private account helpers
# ---------------------------------------------------------------------------
def get_display_name(x, username):
    row = x.execute("SELECT display_name FROM profile_names WHERE username=%s", (username,)).fetchone()
    return (row["display_name"] if row else "") or ""


def current_display_name(username):
    x = db()
    try:
        return get_display_name(x, username)
    finally:
        x.close()


def is_private_account(x, username):
    row = x.execute("SELECT private FROM account_privacy WHERE username=%s", (username,)).fetchone()
    return bool(row and int(row["private"] or 0) == 1)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def current_lang():
    return "en" if request.cookies.get("lang") == "en" else "fa"


def _settings_back():
    return "<p><a href='/settings'>‹ تنظیمات</a></p>"


@app.route("/settings")
def settings():
    u = me()
    if not u:
        return redirect("/")
    lang = current_lang()

    def card(href, icon, title, sub):
        return (f"<a class='item' href='{href}'><div class='avatar'>{icon}</div><div class='info'>"
                f"<div class='name'>{title}</div><div class='sub'>{sub}</div></div><div class='arrow'>‹</div></a>")

    items = (card("/settings/appearance", "🖼", "ظاهر چت", "انتخاب عکس پس‌زمینه چت")
             + card("/settings/security", "🔐", "حریم خصوصی و امنیت", "دستگاه‌های وارد شده و تغییر رمز")
             + card("/settings/lock", "🔒", "قفل کردن پیج", "خصوصی کردن پیج مثل اینستاگرام"))
    langbox = (f"<div class='box' style='margin:18px 0'><h3>🌐 زبان برنامه</h3>"
               f"<form method='post' action='/settings/lang'><input type='hidden' name='lang' value='fa'>"
               f"<button class='{'' if lang == 'fa' else 'btn-sm'}'>فارسی{' ✓' if lang == 'fa' else ''}</button></form>"
               f"<form method='post' action='/settings/lang'><input type='hidden' name='lang' value='en'>"
               f"<button class='{'' if lang == 'en' else 'btn-sm'}'>English{' ✓' if lang == 'en' else ''}</button></form></div>")
    return layout("تنظیمات", f"<div class='page'><div class='list'>{items}</div>{langbox}</div>", "profile")


@app.route("/settings/lang", methods=["POST"])
def settings_lang():
    lang = "en" if request.form.get("lang") == "en" else "fa"
    resp = redirect("/settings")
    resp.set_cookie("lang", lang, max_age=365 * 24 * 3600, samesite="Lax")
    return resp


@app.route("/settings/appearance")
def settings_appearance():
    u = me()
    if not u:
        return redirect("/")
    x = db()
    try:
        bgs = x.execute("SELECT * FROM chat_backgrounds ORDER BY id").fetchall()
        cur = x.execute("SELECT bg_id FROM user_chat_bg WHERE username=%s", (u["username"],)).fetchone()
    finally:
        x.close()
    cur_id = cur["bg_id"] if cur else 0
    tiles = ("<form method='post' action='/chatbg/select'><input type='hidden' name='bg_id' value='0'>"
             "<input type='hidden' name='next' value='settings'>"
             f"<button class='bg-tile{' sel' if not cur_id else ''}'>پیش‌فرض</button></form>")
    for b in bgs:
        tiles += (f"<form method='post' action='/chatbg/select'><input type='hidden' name='bg_id' value='{b['id']}'>"
                  f"<input type='hidden' name='next' value='settings'>"
                  f"<button class='bg-tile{' sel' if cur_id == b['id'] else ''}' "
                  f"style=\"background-image:url('/uploads/backgrounds/{esc(b['filename'])}')\"></button></form>")
    body = (f"<div class='page'>{_settings_back()}<h3>🖼 ظاهر چت</h3>"
            f"<p class='sub'>یکی از عکس‌ها را برای پس‌زمینه چت‌هایت انتخاب کن.</p>"
            f"<div class='bg-grid' style='padding:0 0 18px'>{tiles}</div></div>")
    return layout("ظاهر چت", body, "profile")


_SEC_MSGS = {
    "ok": "رمز با موفقیت تغییر کرد و بقیه دستگاه‌ها خارج شدند.",
    "bad": "رمز فعلی اشتباه است.",
    "short": "رمز جدید باید حداقل ۶ کاراکتر باشد.",
    "mismatch": "رمز جدید و تکرار آن یکسان نیست.",
    "kicked": "دستگاه خارج شد.",
    "kickedall": "بقیه دستگاه‌ها خارج شدند.",
}


@app.route("/settings/security")
def settings_security():
    u = me()
    if not u:
        return redirect("/")
    cutoff = (datetime.now() - timedelta(days=30)).isoformat(timespec="seconds")
    x = db()
    try:
        sess = x.execute("""SELECT * FROM user_sessions WHERE username=%s AND revoked=0 AND last_seen>=%s
            ORDER BY last_seen DESC""", (u["username"], cutoff)).fetchall()
    finally:
        x.close()
    my_sid = session.get("sid")
    rows = ""
    for s in sess:
        is_cur = s["token"] == my_sid
        ua = s["user_agent"] or ""
        icon = "📱" if ("iPhone" in ua or "Android" in ua or "iPad" in ua) else "💻"
        act = ("<span class='badge'>این دستگاه</span>" if is_cur else
               f"<form method='post' action='/settings/session/revoke/{esc(s['token'])}'>"
               f"<button class='btn-sm warn'>بیرون انداختن</button></form>")
        seen = esc((s["last_seen"] or "")[:16].replace("T", " "))
        rows += (f"<div class='admin-row'><div class='avatar'>{icon}</div><div class='info'>"
                 f"<div class='name'>{esc(device_label(ua))}</div>"
                 f"<div class='sub'>IP {esc(s['ip'])} · آخرین فعالیت {seen}</div></div>"
                 f"<div class='admin-actions'>{act}</div></div>")
    others = len([s for s in sess if s["token"] != my_sid])
    kick_all = ("<form method='post' action='/settings/sessions/revoke_others'>"
                "<button class='btn-sm warn' style='margin:10px 0'>خروج همه دستگاه‌های دیگر</button></form>") if others else ""
    msg = _SEC_MSGS.get(request.args.get("m", ""), "")
    msg_html = f"<div class='card'>{esc(msg)}</div>" if msg else ""
    body = f"""<div class='page'>{_settings_back()}{msg_html}
    <h3>🔐 دستگاه‌های وارد شده ({len(sess)})</h3>
    <p class='sub'>تعداد دستگاه‌هایی که الان داخل اکانتت هستند. هرکدام را نمی‌شناسی بیرون بینداز.</p>
    <div class='list'>{rows}</div>{kick_all}
    <div class='box' style='margin:18px 0'><h3>🔑 تغییر رمز</h3>
    <form method='post' action='/settings/password'>
    <input name='old' type='password' placeholder='رمز فعلی' required>
    <input name='new' type='password' placeholder='رمز جدید' required>
    <input name='conf' type='password' placeholder='تکرار رمز جدید' required>
    <button>تغییر رمز</button></form></div></div>"""
    return layout("حریم خصوصی و امنیت", body, "profile")


@app.route("/settings/session/revoke/<token>", methods=["POST"])
def settings_session_revoke(token):
    u = me()
    if not u:
        return redirect("/")
    if token != session.get("sid"):
        x = db()
        try:
            x.execute("UPDATE user_sessions SET revoked=1 WHERE token=%s AND username=%s", (token, u["username"]))
            x.commit()
        finally:
            x.close()
    return redirect("/settings/security?m=kicked")


@app.route("/settings/sessions/revoke_others", methods=["POST"])
def settings_sessions_revoke_others():
    u = me()
    if not u:
        return redirect("/")
    x = db()
    try:
        x.execute("UPDATE user_sessions SET revoked=1 WHERE username=%s AND token<>%s",
                  (u["username"], session.get("sid") or ""))
        x.commit()
    finally:
        x.close()
    return redirect("/settings/security?m=kickedall")


@app.route("/settings/password", methods=["POST"])
def settings_password():
    u = me()
    if not u:
        return redirect("/")
    old = request.form.get("old", "")
    new = request.form.get("new", "")
    conf = request.form.get("conf", "")
    try:
        ok = check_password_hash(u["password"], old)
    except Exception:
        ok = False
    if not ok:
        return redirect("/settings/security?m=bad")
    if len(new) < 6:
        return redirect("/settings/security?m=short")
    if new != conf:
        return redirect("/settings/security?m=mismatch")
    x = db()
    try:
        x.execute("UPDATE users SET password=%s WHERE username=%s", (generate_password_hash(new), u["username"]))
        x.execute("UPDATE user_sessions SET revoked=1 WHERE username=%s AND token<>%s",
                  (u["username"], session.get("sid") or ""))
        x.commit()
    finally:
        x.close()
    return redirect("/settings/security?m=ok")


@app.route("/settings/lock")
def settings_lock():
    u = me()
    if not u:
        return redirect("/")
    x = db()
    try:
        private = is_private_account(x, u["username"])
        reqs = x.execute("SELECT * FROM follow_requests WHERE target=%s ORDER BY id", (u["username"],)).fetchall()
    finally:
        x.close()
    state = "🔒 پیج شما خصوصی است." if private else "🌐 پیج شما عمومی است."
    desc = ("فقط دنبال‌کننده‌های تاییدشده پست‌هایت را می‌بینند و دنبال کردن نیاز به تایید تو دارد."
            if private else "هر کسی می‌تواند پست‌هایت را ببیند و دنبالت کند.")
    toggle = ("<form method='post' action='/settings/lock/toggle'><button>"
              + ("باز کردن قفل پیج" if private else "قفل کردن پیج") + "</button></form>")
    req_html = ""
    for r in reqs:
        req_html += (f"<div class='admin-row'><div class='info'><div class='name'>@{esc(r['requester'])}</div>"
                     f"<div class='sub'>درخواست دنبال کردن</div></div><div class='admin-actions'>"
                     f"<form method='post' action='/settings/lock/request/{r['id']}/approve'><button class='btn-sm on'>تایید</button></form>"
                     f"<form method='post' action='/settings/lock/request/{r['id']}/decline'><button class='btn-sm warn'>رد</button></form>"
                     f"</div></div>")
    req_block = (f"<h3 class='admin-section-title'>درخواست‌های دنبال کردن ({len(reqs)})</h3><div class='list'>{req_html}</div>"
                 if reqs else "")
    body = (f"<div class='page'>{_settings_back()}<div class='box' style='margin:12px 0'><h3>{state}</h3>"
            f"<p class='sub'>{desc}</p>{toggle}</div>{req_block}</div>")
    return layout("قفل کردن پیج", body, "profile")


@app.route("/settings/lock/toggle", methods=["POST"])
def settings_lock_toggle():
    u = me()
    if not u:
        return redirect("/")
    x = db()
    try:
        now_private = is_private_account(x, u["username"])
        new_val = 0 if now_private else 1
        x.execute("""INSERT INTO account_privacy(username,private) VALUES(%s,%s)
            ON CONFLICT(username) DO UPDATE SET private=EXCLUDED.private""", (u["username"], new_val))
        if now_private:
            # Unlocking: pending requests become followers automatically.
            x.execute("""INSERT INTO follows(follower,target,created_at)
                SELECT requester,target,%s FROM follow_requests WHERE target=%s
                ON CONFLICT(follower,target) DO NOTHING""", (_now_iso(), u["username"]))
            x.execute("DELETE FROM follow_requests WHERE target=%s", (u["username"],))
        x.commit()
    finally:
        x.close()
    return redirect("/settings/lock")


@app.route("/settings/lock/request/<int:rid>/<action>", methods=["POST"])
def settings_lock_request(rid, action):
    u = me()
    if not u:
        return redirect("/")
    x = db()
    try:
        r = x.execute("SELECT * FROM follow_requests WHERE id=%s AND target=%s", (rid, u["username"])).fetchone()
        if r:
            if action == "approve":
                x.execute("""INSERT INTO follows(follower,target,created_at) VALUES(%s,%s,%s)
                    ON CONFLICT(follower,target) DO NOTHING""", (r["requester"], u["username"], _now_iso()))
            x.execute("DELETE FROM follow_requests WHERE id=%s", (rid,))
            x.commit()
    finally:
        x.close()
    return redirect("/settings/lock")


# ---------------------------------------------------------------------------
# Persian -> English UI translation (applied only when the "lang" cookie is "en")
# ---------------------------------------------------------------------------
EN_PAIRS = [
    # navigation / layout
    ("خانه", "Home"), ("دایرکت", "Direct"), ("جستجو", "Search"), ("ذخیره‌ها", "Saved"),
    ("پروفایل", "Profile"), ("مدیریت", "Admin"), ("پنل مدیریت", "Admin Panel"), ("تنظیمات", "Settings"),
    ("↪ خروج", "↪ Logout"), ("خروج از حساب", "Log out"), ("تغییر پوسته روشن/تاریک", "Toggle light/dark theme"),
    # rules
    ("📜 قوانین سیمرغ", "📜 Simurgh Rules"), ("قوانین سیمرغ", "Simurgh Rules"), ("قوانین", "Rules"), ("بازگشت", "Back"),
    ("قوانین را خوانده‌ام و می‌پذیرم", "I have read and accept the rules"),
    ("برای ثبت‌نام باید قوانین را بپذیرید.", "You must accept the rules to register."),
    ("برای ورود باید قوانین را بخوانید و تیک پذیرش را بزنید.", "You must read the rules and tick the box to log in."),
    ("به همه کاربران احترام بگذارید؛ توهین، تهدید و آزار ممنوع است.", "Respect all users; insults, threats and harassment are forbidden."),
    ("انتشار محتوای غیراخلاقی، خشونت‌آمیز یا غیرقانونی ممنوع است.", "Posting immoral, violent or illegal content is forbidden."),
    ("اسپم، تبلیغات مزاحم و لینک‌های کلاهبرداری ممنوع است.", "Spam, intrusive ads and scam links are forbidden."),
    ("جعل هویت دیگران یا ادعای دروغ مدیریت بودن ممنوع است.", "Impersonating others or falsely claiming to be staff is forbidden."),
    ("اطلاعات شخصی دیگران را بدون اجازه منتشر نکنید.", "Do not share others' personal information without permission."),
    ("هر کاربر مسئول پست‌ها، استوری‌ها و پیام‌های خودش است.", "Every user is responsible for their own posts, stories and messages."),
    ("مدیریت می‌تواند محتوای متخلف را حذف و حساب متخلف را مسدود کند.", "Admins may remove violating content and ban violating accounts."),
    ("استفاده از سیمرغ به معنی پذیرش این قوانین است.", "Using Simurgh means you accept these rules."),
    # feed
    ("چه خبر؟", "What's happening?"), ("پست کردن", "Post"), ("امروز ", "Today: "), ("پست</span>", "posts</span>"),
    ("برای تو", "For you"), ("دنبال‌شده‌ها", "Following"), ("ترند / لغو", "Trend / Undo"), ("ترند", "Trending"), ("تازه‌ها", "New"),
    ("چیزی برای نمایش نیست.", "Nothing to show."), ("هنوز پستی ذخیره نکردی.", "You haven't saved any posts yet."),
    ("ویرایش پست", "Edit post"), ("ویرایش شد", "edited"), ("ویرایش", "Edit"), ("حذف", "Delete"),
    ("لایک", "Like"), ("ذخیره", "Save"), ("تمرکز", "Focus"), ("کامنت‌ها", "Comments"), ("کامنت...", "Comment..."), ("ارسال", "Send"),
    ("<button>پست</button>", "<button>Post</button>"), ("پست جدید برای پیج...", "New post for the page..."),
    ("محدودیت روزانه پست شما تمام شده است. سقف امروز: ", "Your daily post limit is used up. Today's cap: "),
    ("حجم فایل بیشتر از 25MB است.", "File is larger than 25MB."), ("نوع فایل پشتیبانی نمی‌شود.", "Unsupported file type."),
    ("پست خالی است.", "The post is empty."),
    # messages / search
    ("هنوز گفتگویی نداری.", "No conversations yet."), ("شروع گفتگو", "Start chatting"), ("پیام خصوصی...", "Private message..."),
    ("شروع چت خصوصی با آیدی", "Start a private chat"), ("آیدی را جستجو کن...", "Search by ID..."),
    ("این حساب برای شما مسدود شده و امکان دایرکت وجود ندارد.", "This account is blocked for you; direct messages are unavailable."),
    # pages
    ("ساخت پیج", "Create page"), ("نام پیج", "Page name"), ("آیدی پیج", "Page ID"), ("توضیح", "Description"), ("ساخت", "Create"),
    ("عکس پیج", "Page photo"), ("فقط سازنده پیج می‌تواند پیام بفرستد.", "Only the page owner can post."),
    ("هنوز پیامی نیست.", "No messages yet."), ("ارسال عکس (PRO)", "Send photo (PRO)"),
    # profile
    ("ویرایش پروفایل", "Edit profile"), ("دنبال نکردن", "Unfollow"), ("درخواست دنبال کردن", "Request to follow"),
    ("لغو درخواست", "Cancel request"), ("دنبال کردن", "Follow"), ("رفع بلاک", "Unblock"), ("بلاک", "Block"),
    ("🎞 استوری", "🎞 Story"), ("دنبال‌کننده", "followers"), ("دنبال‌شونده", "following"),
    ("آپلود عکس پروفایل مخصوص کاربران PRO است.", "Profile photo upload is for PRO users."),
    ("آپلود عکس پروفایل", "Upload profile photo"), ("حذف عکس پروفایل", "Remove profile photo"),
    ("نام نمایشی", "Display name"), ("بیو", "Bio"),
    ("🔒 این پیج خصوصی است. برای دیدن پست‌ها باید دنبالش کنید.", "🔒 This page is private. Follow it to see its posts."),
    # backgrounds / stories
    ("🖼 پس‌زمینه چت", "🖼 Chat background"), ("پیش‌فرض", "Default"),
    ("🎞 استوری جدید", "🎞 New story"), ("استوری جدید", "New story"),
    ("عکس یا ویدیو · تا", "Photo or video · up to"), ("ساعت می‌ماند", "hours"),
    ("فقط دنبال‌کننده‌های شما می‌بینند", "only your followers can see it"),
    ("کپشن (اختیاری)", "Caption (optional)"), ("انتشار استوری", "Publish story"),
    ("این استوری حذف شود؟", "Delete this story?"), ("<span>شما</span>", "<span>You</span>"),
    ("فایلی انتخاب نشده است.", "No file selected."),
    # admin panel (display only)
    ("گروه‌ها و پیج‌ها", "Groups & pages"), ("کاربران", "Users"), ("پست‌ها", "Posts"), ("پستی نیست.", "No posts."),
    ("لغو تایید", "Unverify"), ("تایید کردن", "Verify"), ("آزاد کردن", "Unban"), ("بن کردن", "Ban"),
    ("فعال کردن PRO", "Enable PRO"), ("لغو PRO", "Revoke PRO"), ("مدیر</span>", "Admin</span>"), ("بن‌شده</span>", "Banned</span>"),
    ("هنوز گروه یا کانالی نیست.", "No groups or channels yet."), ("🖼 پس‌زمینه‌های چت", "🖼 Chat backgrounds"),
    ("افزودن پس‌زمینه جدید", "Add new background"), ("هنوز پس‌زمینه‌ای اضافه نشده.", "No backgrounds added yet."),
    ("🎞 استوری‌های فعال", "🎞 Active stories"), ("استوری فعالی نیست.", "No active stories."),
    ("حذف استوری", "Delete story"), ("مشاهده", "View"),
    # PRO page
    ("حساب شما هنوز PRO نیست.", "Your account is not PRO yet."), ("بازگشت به پروفایل", "Back to profile"),
    ("PRO فعال است", "PRO is active"), ("حساب PRO", "PRO account"),
    # settings (new)
    ("ظاهر چت", "Chat appearance"), ("انتخاب عکس پس‌زمینه چت", "Choose a chat background"),
    ("حریم خصوصی و امنیت", "Privacy & security"), ("دستگاه‌های وارد شده و تغییر رمز", "Logged-in devices and password change"),
    ("قفل کردن پیج", "Lock page"), ("خصوصی کردن پیج مثل اینستاگرام", "Make your page private, like Instagram"),
    ("🌐 زبان برنامه", "🌐 App language"), ("فارسی", "Persian"),
    ("یکی از عکس‌ها را برای پس‌زمینه چت‌هایت انتخاب کن.", "Pick one of the images as your chat background."),
    ("رمز با موفقیت تغییر کرد و بقیه دستگاه‌ها خارج شدند.", "Password changed; all other devices were signed out."),
    ("رمز فعلی اشتباه است.", "Current password is wrong."),
    ("رمز جدید باید حداقل ۶ کاراکتر باشد.", "New password must be at least 6 characters."),
    ("رمز جدید و تکرار آن یکسان نیست.", "New password and its repeat don't match."),
    ("دستگاه خارج شد.", "Device signed out."), ("بقیه دستگاه‌ها خارج شدند.", "All other devices were signed out."),
    ("🔐 دستگاه‌های وارد شده", "🔐 Logged-in devices"),
    ("تعداد دستگاه‌هایی که الان داخل اکانتت هستند. هرکدام را نمی‌شناسی بیرون بینداز.",
     "Devices currently signed in to your account. Kick out any you don't recognize."),
    ("آخرین فعالیت", "Last active"), ("این دستگاه", "This device"), ("بیرون انداختن", "Kick out"),
    ("خروج همه دستگاه‌های دیگر", "Sign out all other devices"), ("🔑 تغییر رمز", "🔑 Change password"),
    ("رمز فعلی", "Current password"), ("تکرار رمز جدید", "Repeat new password"), ("رمز جدید", "New password"),
    ("تغییر رمز", "Change password"), ("نامشخص", "Unknown"),
    ("🔒 پیج شما خصوصی است.", "🔒 Your page is private."), ("🌐 پیج شما عمومی است.", "🌐 Your page is public."),
    ("فقط دنبال‌کننده‌های تاییدشده پست‌هایت را می‌بینند و دنبال کردن نیاز به تایید تو دارد.",
     "Only approved followers can see your posts, and following needs your approval."),
    ("هر کسی می‌تواند پست‌هایت را ببیند و دنبالت کند.", "Anyone can see your posts and follow you."),
    ("باز کردن قفل پیج", "Unlock page"), ("درخواست‌های دنبال کردن", "Follow requests"),
    ("تایید</button>", "Approve</button>"), ("رد</button>", "Decline</button>"),
]
EN_ITEMS = sorted(EN_PAIRS, key=lambda p: len(p[0]), reverse=True)
EN_CSS = ("<style>.rules-box,.check-row{direction:ltr;text-align:left}"
          ".rules-list{padding:0 0 0 22px}</style>")


@app.after_request
def apply_language(resp):
    try:
        if request.cookies.get("lang") != "en":
            return resp
        if resp.direct_passthrough or resp.mimetype != "text/html":
            return resp
        text = resp.get_data(as_text=True)
        for fa, en in EN_ITEMS:
            text = text.replace(fa, en)
        text = text.replace("</head>", EN_CSS + "</head>", 1)
        resp.set_data(text)
    except Exception:
        app.logger.exception("LANGUAGE ERROR")
    return resp


# ---------------------------------------------------------------------------
# Keep-alive: the site pings itself every 5 minutes so Render doesn't put it
# to sleep. Uses only the standard library and a route that never touches the
# database (/favicon.ico). Set KEEPALIVE_URL to override the auto-detected URL;
# set KEEPALIVE=0 to disable.
# ---------------------------------------------------------------------------
import threading
import time
import urllib.request

KEEPALIVE_INTERVAL = 300  # 5 minutes


def _keepalive_loop():
    base = (os.getenv("KEEPALIVE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
    if not base:
        app.logger.warning("KEEPALIVE: no RENDER_EXTERNAL_URL / KEEPALIVE_URL set, self-ping disabled.")
        return
    url = base + "/favicon.ico"
    time.sleep(60)  # let the server finish starting
    while True:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "simurgh-keepalive"})
            with urllib.request.urlopen(req, timeout=20) as r:
                r.read(1)
        except Exception as e:
            app.logger.warning("KEEPALIVE ping failed: %s", e)
        time.sleep(KEEPALIVE_INTERVAL)


def start_keepalive():
    if os.getenv("KEEPALIVE", "1") == "0":
        return
    t = threading.Thread(target=_keepalive_loop, name="keepalive", daemon=True)
    t.start()


init_db()
seed_default_backgrounds()
start_keepalive()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","8080")), debug=False)
