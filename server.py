import sqlite3, uuid, os, secrets, time, json
from flask import Flask, request, jsonify, g, send_from_directory

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "munjang.db")
TOKEN_PATH = os.path.join(BASE, "token.txt")
STATIC_DIR = os.path.join(BASE, "static")

# 처음 실행할 때 비밀 토큰을 자동 생성
if not os.path.exists(TOKEN_PATH):
    with open(TOKEN_PATH, "w") as f:
        f.write(secrets.token_urlsafe(24))
with open(TOKEN_PATH) as f:
    TOKEN = f.read().strip()

app = Flask(__name__)
app.json.ensure_ascii = False  # 한글이 깨지지 않게

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL DEFAULT 'writing',
    title TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    folder TEXT NOT NULL DEFAULT '',
    starred INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0,
    rev INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS note_tags (
    note_id TEXT NOT NULL,
    tag_id INTEGER NOT NULL,
    PRIMARY KEY (note_id, tag_id)
);
CREATE TABLE IF NOT EXISTS links (
    from_id TEXT NOT NULL,
    to_id TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'reference',
    PRIMARY KEY (from_id, to_id, kind)
);
CREATE TABLE IF NOT EXISTS folders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS applied_ops (
    op_id TEXT PRIMARY KEY,
    result TEXT NOT NULL,
    ts INTEGER NOT NULL
);
"""

# 글이 바뀔 때마다 전역 변경 번호(rev)를 하나 올려서 그 글에 붙인다.
# → 동기화 기준이 시계(updated_at)가 아니라 rev 라서 기기 시계가 틀려도, 별표만 바꿔도 놓치지 않는다.
TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS notes_rev_ins AFTER INSERT ON notes
BEGIN
    UPDATE meta SET value = value + 1 WHERE key = 'rev';
    UPDATE notes SET rev = (SELECT value FROM meta WHERE key = 'rev') WHERE id = NEW.id;
END;
CREATE TRIGGER IF NOT EXISTS notes_rev_upd
AFTER UPDATE OF type, title, body, folder, starred, deleted, updated_at ON notes
BEGIN
    UPDATE meta SET value = value + 1 WHERE key = 'rev';
    UPDATE notes SET rev = (SELECT value FROM meta WHERE key = 'rev') WHERE id = NEW.id;
END;
"""

OPEN_PATHS = ("/", "/ping", "/favicon.ico", "/sw.js")


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=10)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def now_ms():
    return int(time.time() * 1000)


# 플래그: starred 컬럼을 0~5 로 사용한다.
# 0=없음, 1=별표(예전 즐겨찾기), 2=깃발, 3=연필, 4=하트, 5=고민중
def flag_val(v):
    try:
        v = int(v)
    except (TypeError, ValueError):
        return 0
    return v if 0 <= v <= 5 else 0



def clean_tags(raw):
    names = []
    for n in raw or []:
        n = str(n).strip().lstrip("#").strip()
        if n and n not in names:
            names.append(n)
    return names[:6]  # 태그는 글당 최대 6개


def write_tags(db, nid, names, ts):
    if db.execute("SELECT 1 FROM notes WHERE id=?", (nid,)).fetchone() is None:
        raise ValueError("없는 글")
    db.execute("DELETE FROM note_tags WHERE note_id=?", (nid,))
    for n in names:
        db.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (n,))
        tid = db.execute("SELECT id FROM tags WHERE name=?", (n,)).fetchone()["id"]
        db.execute("INSERT OR IGNORE INTO note_tags (note_id, tag_id) VALUES (?,?)", (nid, tid))
    db.execute("UPDATE notes SET updated_at=? WHERE id=?", (ts, nid))


def purge_ids(db, ids, ts):
    """영구 삭제. 행을 지우지 않고 묘비(deleted=2)로 남겨야 다른 기기가 '지워졌다'는 걸 알 수 있다."""
    if not ids:
        return
    marks = ",".join("?" * len(ids))
    db.execute(f"DELETE FROM note_tags WHERE note_id IN ({marks})", ids)
    db.execute(f"DELETE FROM links WHERE from_id IN ({marks}) OR to_id IN ({marks})", ids + ids)
    db.execute(
        f"UPDATE notes SET deleted=2, title='', body='', folder='', updated_at=? WHERE id IN ({marks})",
        [ts, *ids],
    )


def folder_add(db, name, ts):
    db.execute("INSERT OR IGNORE INTO folders (name, created_at) VALUES (?,?)", (name, ts))


def folder_rename(db, old, new, ts, merge=False):
    if old == new:
        return
    if db.execute("SELECT 1 FROM folders WHERE name=?", (new,)).fetchone():
        if not merge:
            raise ValueError("이미 있는 폴더 이름이에요")
        db.execute("DELETE FROM folders WHERE name=?", (old,))  # 합치기: 옛 폴더만 없애고 글을 옮긴다
    else:
        cur = db.execute("UPDATE folders SET name=? WHERE name=?", (new, old))
        if cur.rowcount == 0:
            folder_add(db, new, ts)
    db.execute("UPDATE notes SET folder=?, updated_at=? WHERE folder=? AND deleted<2", (new, ts, old))


def folder_delete(db, name, ts):
    db.execute("UPDATE notes SET folder='', updated_at=? WHERE folder=? AND deleted<2", (ts, name))
    db.execute("DELETE FROM folders WHERE name=?", (name,))


def link_add(db, f, t, kind):
    if not f or not t or f == t or kind not in ("reference", "source"):
        raise ValueError("잘못된 링크")
    ok = db.execute(
        "SELECT COUNT(*) FROM notes WHERE id IN (?,?) AND deleted<2", (f, t)
    ).fetchone()[0]
    if ok != 2:
        raise ValueError("연결할 글이 없어요")
    db.execute("INSERT OR IGNORE INTO links (from_id, to_id, kind) VALUES (?,?,?)", (f, t, kind))


def link_del(db, f, t, kind):
    db.execute("DELETE FROM links WHERE from_id=? AND to_id=? AND kind=?", (f, t, kind))


def tags_map(db):
    m = {}
    rows = db.execute(
        "SELECT nt.note_id AS nid, t.name AS name "
        "FROM note_tags nt JOIN tags t ON t.id = nt.tag_id ORDER BY t.name"
    ).fetchall()
    for r in rows:
        m.setdefault(r["nid"], []).append(r["name"])
    return m


def one(db, nid):
    row = db.execute("SELECT * FROM notes WHERE id=?", (nid,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["tags"] = [
        r["name"]
        for r in db.execute(
            "SELECT t.name FROM note_tags nt JOIN tags t ON t.id = nt.tag_id "
            "WHERE nt.note_id=? ORDER BY t.name",
            (nid,),
        )
    ]
    return d


@app.before_request
def check_token():
    if request.method == "OPTIONS" or request.path in OPEN_PATHS:  # OPTIONS = 브라우저의 사전 확인(CORS), 토큰 없이 통과
        return None
    auth = request.headers.get("Authorization", "")
    if not secrets.compare_digest(auth, "Bearer " + TOKEN):
        return jsonify(error="인증 실패"), 401


@app.after_request
def add_cors(resp):
    # APK / PC 앱처럼 화면이 다른 출처에서 열려도 이 서버를 부를 수 있게 한다.
    # 인증은 Authorization 토큰이 하므로 출처는 제한하지 않는다 (쿠키를 쓰지 않아 안전).
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    resp.headers["Access-Control-Allow-Private-Network"] = "true"
    resp.headers["Access-Control-Max-Age"] = "600"
    return resp


@app.get("/")
def index():
    resp = send_from_directory(STATIC_DIR, "index.html")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/ping")
def ping():
    return jsonify(ok=True, name="문장 서버")


# ---------- 글 ----------

@app.get("/notes")
def list_notes():
    q = "SELECT * FROM notes"
    cond, args = [], []
    since = request.args.get("since")
    if since:  # 동기화용: 이 시각 이후 바뀐 것 (삭제된 것 포함)
        cond.append("updated_at > ?")
        args.append(int(since))
    elif request.args.get("deleted") == "1":  # 휴지통
        cond.append("deleted = 1")
    else:
        cond.append("deleted = 0")
    t = request.args.get("type")
    if t:
        cond.append("type = ?")
        args.append(t)
    q += " WHERE " + " AND ".join(cond) + " ORDER BY updated_at DESC"
    db = get_db()
    rows = db.execute(q, args).fetchall()
    tmap = tags_map(db)
    out = []
    for r in rows:
        d = dict(r)
        d["tags"] = tmap.get(d["id"], [])
        out.append(d)
    return jsonify(out)


@app.post("/notes")
def create_note():
    data = request.get_json(force=True)
    t = data.get("type", "writing")
    if t not in ("idea", "writing"):
        return jsonify(error="type은 idea 또는 writing"), 400
    nid = data.get("id") or str(uuid.uuid4())
    ts = now_ms()
    folder = (data.get("folder") or "").strip()
    db = get_db()
    body = data.get("body", "")
    if not isinstance(body, str):
        return jsonify(error="body는 문자열이어야 합니다"), 400
    # 서식 HTML을 제거하지 않고 body TEXT 컬럼에 그대로 저장한다.
    db.execute(
        "INSERT INTO notes (id,type,title,body,folder,starred,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (nid, t, data.get("title", ""), body,
         folder, flag_val(data.get("starred", 0)), ts, ts),
    )
    if folder:
        db.execute("INSERT OR IGNORE INTO folders (name, created_at) VALUES (?,?)", (folder, ts))
    db.commit()
    return jsonify(one(db, nid)), 201


@app.get("/notes/<nid>")
def get_note(nid):
    d = one(get_db(), nid)
    if d is None:
        return jsonify(error="없는 글"), 404
    return jsonify(d)


@app.put("/notes/<nid>")
def update_note(nid):
    data = request.get_json(force=True)
    db = get_db()
    if db.execute("SELECT 1 FROM notes WHERE id=?", (nid,)).fetchone() is None:
        return jsonify(error="없는 글"), 404
    # body는 일반 텍스트뿐 아니라 서식 HTML(RICH 마커 + HTML)을 그대로 보존한다.
    # 글자 크기는 HTML span의 font-size(pt)로 저장되므로 변환하거나 제거하지 않는다.
    if "body" in data and not isinstance(data["body"], str):
        return jsonify(error="body는 문자열이어야 합니다"), 400
    for field in ("type", "title", "body", "folder", "starred", "deleted"):
        if field in data:
            val = flag_val(data[field]) if field == "starred" else data[field]
            db.execute(f"UPDATE notes SET {field}=? WHERE id=?", (val, nid))
    # 플래그만 바꾼 경우에는 '수정한 시각'을 그대로 둬서 목록 순서가 바뀌지 않게 한다.
    if set(data.keys()) - {"starred"}:
        db.execute("UPDATE notes SET updated_at=? WHERE id=?", (now_ms(), nid))
    db.commit()
    return jsonify(one(db, nid))


@app.delete("/notes/<nid>")
def delete_note(nid):  # 완전 삭제가 아니라 휴지통으로 이동
    db = get_db()
    db.execute("UPDATE notes SET deleted=1, updated_at=? WHERE id=?", (now_ms(), nid))
    db.commit()
    return jsonify(ok=True)


@app.post("/notes/bulk")
def bulk_notes():
    """여러 글을 한 번에: trash(휴지통) / restore(복원) / move(폴더 이동) / purge(영구 삭제)"""
    data = request.get_json(force=True)
    ids = [str(i) for i in (data.get("ids") or [])][:500]
    action = data.get("action")
    if not ids or action not in ("trash", "restore", "move", "purge"):
        return jsonify(error="잘못된 요청"), 400
    ts = now_ms()
    db = get_db()
    marks = ",".join("?" * len(ids))
    if action == "trash":
        db.execute(f"UPDATE notes SET deleted=1, updated_at=? WHERE id IN ({marks})", [ts, *ids])
    elif action == "restore":
        db.execute(f"UPDATE notes SET deleted=0, updated_at=? WHERE id IN ({marks})", [ts, *ids])
    elif action == "move":
        folder = (data.get("folder") or "").strip()
        db.execute(f"UPDATE notes SET folder=?, updated_at=? WHERE id IN ({marks})", [folder, ts, *ids])
        if folder:
            db.execute("INSERT OR IGNORE INTO folders (name, created_at) VALUES (?,?)", (folder, ts))
    else:  # purge (묘비로 남김)
        purge_ids(db, ids, ts)
    db.commit()
    return jsonify(ok=True, updated_at=ts)


# ---------- 폴더 ----------

@app.get("/folders")
def list_folders():
    rows = get_db().execute("SELECT name FROM folders ORDER BY created_at, id").fetchall()
    return jsonify([r["name"] for r in rows])


@app.post("/folders")
def add_folder():
    name = (request.get_json(force=True).get("name") or "").strip()
    if not name:
        return jsonify(error="폴더 이름이 비어 있어요"), 400
    db = get_db()
    folder_add(db, name, now_ms())
    db.commit()
    return jsonify(ok=True, name=name), 201


@app.put("/folders")
def rename_folder_route():
    data = request.get_json(force=True)
    old = (data.get("old") or "").strip()
    new = (data.get("new") or "").strip()
    if not old or not new:
        return jsonify(error="이름이 비어 있어요"), 400
    db = get_db()
    try:
        folder_rename(db, old, new, now_ms())
    except ValueError as e:
        return jsonify(error=str(e)), 409
    db.commit()
    return jsonify(ok=True)


@app.delete("/folders")
def delete_folder():  # 폴더만 지우고, 안에 있던 글은 '폴더 없음'으로 나옴
    name = (request.get_json(force=True).get("name") or "").strip()
    if not name:
        return jsonify(error="이름이 비어 있어요"), 400
    db = get_db()
    folder_delete(db, name, now_ms())
    db.commit()
    return jsonify(ok=True)


# ---------- 태그 ----------

@app.put("/notes/<nid>/tags")
def set_tags(nid):
    data = request.get_json(force=True)
    db = get_db()
    try:
        write_tags(db, nid, clean_tags(data.get("tags", [])), now_ms())
    except ValueError as e:
        return jsonify(error=str(e)), 404
    db.commit()
    return jsonify(one(db, nid))


# ---------- 링크 ----------

@app.get("/links")
def list_links():
    rows = get_db().execute("SELECT from_id, to_id, kind FROM links").fetchall()
    return jsonify([dict(r) for r in rows])


@app.post("/links")
def add_link():
    data = request.get_json(force=True)
    db = get_db()
    try:
        link_add(db, data.get("from_id"), data.get("to_id"), data.get("kind", "reference"))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    db.commit()
    return jsonify(ok=True), 201


@app.delete("/links")
def remove_link():
    data = request.get_json(force=True)
    db = get_db()
    link_del(db, data.get("from_id"), data.get("to_id"), data.get("kind", "reference"))
    db.commit()
    return jsonify(ok=True)


# ---------- 동기화 ----------
# GET  /sync?since=<rev>  : rev 이후 바뀐 글(휴지통·묘비 포함) + 링크/폴더 전체 스냅샷
# POST /sync {ops:[...]}  : 기기에서 쌓아 둔 변경 목록을 순서대로 적용 (op_id 로 중복 적용 방지)

def cur_rev(db):
    return db.execute("SELECT value FROM meta WHERE key='rev'").fetchone()[0]


@app.get("/sync")
def sync_pull():
    db = get_db()
    since = request.args.get("since", default=0, type=int)
    limit = 300
    cur = cur_rev(db)
    rows = db.execute(
        "SELECT * FROM notes WHERE rev > ? AND rev <= ? ORDER BY rev LIMIT ?",
        (since, cur, limit + 1),
    ).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    tmap = tags_map(db)
    notes = []
    for r in rows:
        d = dict(r)
        d["tags"] = tmap.get(d["id"], [])
        notes.append(d)
    return jsonify(
        rev=rows[-1]["rev"] if more else cur,
        has_more=more,
        notes=notes,
        links=[dict(r) for r in db.execute("SELECT from_id, to_id, kind FROM links")],
        folders=[r["name"] for r in db.execute("SELECT name FROM folders ORDER BY created_at, id")],
        server_time=now_ms(),
    )


def op_note_upsert(db, op, ts):
    nid = str(op.get("id") or "")
    if not nid:
        raise ValueError("id 없음")
    f = op.get("fields") or {}
    c = {}
    if f.get("type") in ("idea", "writing"):
        c["type"] = f["type"]
    if "title" in f:
        c["title"] = str(f["title"] or "")
    if isinstance(f.get("body"), str):
        c["body"] = f["body"]
    if "folder" in f:
        c["folder"] = str(f["folder"] or "").strip()
    if "starred" in f:
        c["starred"] = flag_val(f["starred"])
    if "deleted" in f:
        c["deleted"] = 1 if f["deleted"] else 0

    cid = str(op.get("client_id") or "")[:64]  # 이 변경을 보낸 기기
    row = db.execute("SELECT * FROM notes WHERE id=?", (nid,)).fetchone()
    res = {"id": nid}
    content_changed = False
    if row is None:  # 이 기기에서 새로 만든 글
        created = op.get("created_at")
        created = int(created) if isinstance(created, (int, float)) else ts
        db.execute(
            "INSERT INTO notes (id,type,title,body,folder,starred,created_at,updated_at,deleted) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (nid, c.get("type", "writing"), c.get("title", ""), c.get("body", ""),
             c.get("folder", ""), c.get("starred", 0), created, ts, c.get("deleted", 0)),
        )
        content_changed = True
    elif row["deleted"] == 2:  # 이미 영구 삭제된 글 → 무시 (다음 pull 에서 기기에서도 사라짐)
        return {"id": nid, "purged": True}
    else:
        base = op.get("base_rev")
        # 충돌 = '다른 기기'가 제목/본문을 내가 본 버전(base_rev) 이후에 고쳤고, 내 내용과 다를 때만.
        #  · 같은 기기가 직접 올린 이전 저장(자동저장)은 충돌이 아니다.
        #  · 다른 기기가 태그/플래그/폴더만 바꿔서 rev 가 올라간 것도 충돌이 아니다. (cc_rev = 제목/본문이 마지막으로 바뀐 rev)
        same_writer = bool(cid) and row["cc_client"] == cid
        if (base is not None and not same_writer and row["cc_rev"] > int(base)
                and any(k in c and c[k] != row[k] for k in ("title", "body"))):
            copy_id = str(uuid.uuid4())
            title = (c.get("title", row["title"]) or "제목 없음") + " (충돌 사본)"
            db.execute(
                "INSERT INTO notes (id,type,title,body,folder,starred,created_at,updated_at,deleted) "
                "VALUES (?,?,?,?,?,?,?,?,0)",
                (copy_id, row["type"], title, c.get("body", row["body"]), row["folder"], 0, ts, ts),
            )
            db.execute("UPDATE notes SET cc_rev=rev, cc_client=? WHERE id=?", (cid, copy_id))
            res["conflict"] = copy_id
            c.pop("title", None)
            c.pop("body", None)
        content_changed = any(k in c and c[k] != row[k] for k in ("title", "body"))
        if c:
            sets, vals = ", ".join(f"{k}=?" for k in c), list(c.values())
            if set(c) != {"starred"}:  # 플래그만 바꾼 건 목록 순서를 안 바꾼다 (기존 규칙)
                sets += ", updated_at=?"
                vals.append(ts)
            db.execute(f"UPDATE notes SET {sets} WHERE id=?", [*vals, nid])
    if content_changed:  # 제목/본문을 바꾼 기기와 그때의 rev 를 기록 (트리거는 이 컬럼엔 반응하지 않는다)
        db.execute("UPDATE notes SET cc_rev=rev, cc_client=? WHERE id=?", (cid, nid))
    if c.get("folder"):
        folder_add(db, c["folder"], ts)
    res["rev"] = db.execute("SELECT rev FROM notes WHERE id=?", (nid,)).fetchone()[0]
    return res


def apply_op(db, op, ts):
    k = op.get("kind")
    if k == "note_upsert":
        return op_note_upsert(db, op, ts)
    if k == "note_tags":
        nid = str(op.get("id") or "")
        write_tags(db, nid, clean_tags(op.get("tags")), ts)
        return {"id": nid, "rev": db.execute("SELECT rev FROM notes WHERE id=?", (nid,)).fetchone()[0]}
    if k == "note_purge":
        purge_ids(db, [str(i) for i in (op.get("ids") or [])][:500], ts)
        return {}
    if k == "link_add":
        link_add(db, op.get("from_id"), op.get("to_id"), op.get("link_kind", "reference"))
        return {}
    if k == "link_del":
        link_del(db, op.get("from_id"), op.get("to_id"), op.get("link_kind", "reference"))
        return {}
    if k == "folder_add":
        name = (op.get("name") or "").strip()
        if not name:
            raise ValueError("폴더 이름이 비어 있어요")
        folder_add(db, name, ts)
        return {}
    if k == "folder_rename":
        old, new = (op.get("old") or "").strip(), (op.get("new") or "").strip()
        if not old or not new:
            raise ValueError("이름이 비어 있어요")
        folder_rename(db, old, new, ts, merge=True)
        return {}
    if k == "folder_del":
        name = (op.get("name") or "").strip()
        if not name:
            raise ValueError("이름이 비어 있어요")
        folder_delete(db, name, ts)
        return {}
    raise ValueError("알 수 없는 작업: %s" % k)


@app.post("/sync")
def sync_push():
    data = request.get_json(force=True)
    ops = (data.get("ops") or [])[:200]
    db = get_db()
    ts = now_ms()
    # 먼저 쓰기 트랜잭션을 열어 둔다 (이래야 아래 SAVEPOINT 가 바깥 트랜잭션 안에서 동작)
    db.execute("UPDATE meta SET value = value WHERE key = 'rev'")
    results = []
    for op in ops:
        oid = str(op.get("op_id") or "")
        if oid:
            prev = db.execute("SELECT result FROM applied_ops WHERE op_id=?", (oid,)).fetchone()
            if prev:  # 응답을 못 받고 재전송한 경우: 저장해 둔 결과를 그대로 돌려줌
                results.append(json.loads(prev["result"]))
                continue
        db.execute("SAVEPOINT op")
        try:
            res = apply_op(db, op, ts) or {}
            db.execute("RELEASE op")
        except Exception as e:  # 이상한 작업 하나가 전체 큐를 막지 않게
            db.execute("ROLLBACK TO op")
            db.execute("RELEASE op")
            res = {"error": str(e)}
        res["op_id"] = oid
        if oid:
            db.execute(
                "INSERT OR REPLACE INTO applied_ops (op_id, result, ts) VALUES (?,?,?)",
                (oid, json.dumps(res, ensure_ascii=False), ts),
            )
        results.append(res)
    db.execute("DELETE FROM applied_ops WHERE ts < ?", (ts - 30 * 24 * 3600 * 1000,))
    db.commit()
    return jsonify(results=results, rev=cur_rev(db))


@app.get("/sw.js")
def service_worker():  # 서버가 꺼져 있어도 앱 화면이 열리도록 (localhost 에서만 동작)
    resp = send_from_directory(STATIC_DIR, "sw.js", mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-store"
    return resp


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL")  # 읽는 동안에도 쓰기 가능 → 동기화와 화면 요청이 안 막힘
        conn.executescript(SCHEMA)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(notes)")]
        if "rev" not in cols:  # 예전 DB 이전: rev 컬럼 추가 후 기존 글에 순번 부여
            conn.execute("ALTER TABLE notes ADD COLUMN rev INTEGER NOT NULL DEFAULT 0")
            conn.execute("UPDATE notes SET rev = rowid")
        if "cc_rev" not in cols:  # 제목/본문이 마지막으로 바뀐 rev 와 기기 (충돌 판정용)
            conn.execute("ALTER TABLE notes ADD COLUMN cc_rev INTEGER NOT NULL DEFAULT 0")
        if "cc_client" not in cols:
            conn.execute("ALTER TABLE notes ADD COLUMN cc_client TEXT NOT NULL DEFAULT ''")
        conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('rev', 0)")
        conn.execute(
            "UPDATE meta SET value = MAX(value, (SELECT COALESCE(MAX(rev), 0) FROM notes)) WHERE key='rev'"
        )
        conn.executescript(TRIGGERS)
        # 예전에 글에만 적혀 있던 폴더 이름을 폴더 목록으로 옮겨 둠
        conn.execute(
            "INSERT OR IGNORE INTO folders (name, created_at) "
            "SELECT DISTINCT folder, ? FROM notes WHERE folder != ''",
            (now_ms(),),
        )


init_db()


if __name__ == "__main__":
    print("\n=== 내 토큰 ===")
    print(TOKEN)
    print("===============\n")
    print("브라우저에서 http://localhost:8000 을 열어 보세요.\n")
    app.run(host="0.0.0.0", port=8000)
