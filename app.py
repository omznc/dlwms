# /// script
# requires-python = ">=3.11,<3.13"
# dependencies = ["fastembed==0.9.1", "sqlite-vec==0.1.9", "numpy", "starlette==0.46.2", "uvicorn==0.34.3", "jinja2==3.1.6"]
# ///
"""Hybrid search (FTS5 bm25 + vector KNN, merged with RRF) over index.db."""
import os
import re
import threading
import unicodedata
from contextlib import asynccontextmanager
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.responses import FileResponse, HTMLResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from common import connect, load_model

HERE = Path(__file__).parent
INDEX_DB = os.environ.get("INDEX_DB", str(HERE / "index.db"))
FILES_DIR = Path(os.environ.get("FILES_DIR", HERE / "files")).resolve()
PAGE, CANDIDATES, RRF_K = 20, 100, 60
POST_URL = "https://www.fit.ba/student/obavijesti/opsirnije.aspx?sqs="
DOC_FIELDS = ("filename", "path", "size", "uploaded_at", "kind", "school_year")

env = Environment(loader=FileSystemLoader(HERE / "templates"), autoescape=select_autoescape())
local = threading.local()
state = {}


def db():
    if not hasattr(local, "db"):
        local.db = connect(INDEX_DB, readonly=True)
    return local.db


# ---- text helpers ----
def fold(s):
    """Lowercase and strip diacritics; keeps the string length."""
    out = []
    for c in s:
        d = unicodedata.normalize("NFD", c)[0].lower() if c not in "đĐ" else "d"
        out.append(d if len(d) == 1 else c)
    return "".join(out)


def terms_of(q):
    return [t for t in re.findall(r"\w+", fold(q)) if len(t) > 1][:12]


def fts_query(terms):
    return " OR ".join(f'"{t}"*' if len(t) >= 4 else f'"{t}"' for t in terms)


def mark_hits(body, hits, start, end):
    """Escape body[start:end] and wrap the matches in <mark>."""
    out, pos = [], start
    for m in hits:
        if m.start() >= end:
            break
        if m.end() <= start:
            continue
        out += [escape(body[pos:m.start()]), Markup("<mark>"), escape(body[m.start():m.end()]), Markup("</mark>")]
        pos = m.end()
    out.append(escape(body[pos:end]))
    return "".join(out)


def snippet(text, terms, width=260):
    """Return an HTML snippet around the first match, with <mark> on the matches."""
    body = text.split("\n", 1)[-1]
    long_terms = [t for t in terms if len(t) >= 3]
    hits = []
    if long_terms:
        rx = re.compile("|".join(re.escape(t) + r"\w*" for t in long_terms))
        hits = list(rx.finditer(fold(body)))
    start = max(0, hits[0].start() - 70) if hits else 0
    end = min(len(body), start + width)
    return Markup(("… " if start else "") + mark_hits(body, hits, start, end) + (" …" if end < len(body) else ""))


# ---- search ----
def filters(p):
    sql, args = "", []
    if p["kind"] in ("post", "document"):
        sql += " AND i.kind=?"
        args.append(p["kind"])
    if p["subject"]:
        sql += " AND i.subject=?"
        args.append(p["subject"])
    if p["from"]:
        sql += " AND i.year>=?"
        args.append(p["from"])
    if p["to"]:
        sql += " AND i.year<=?"
        args.append(p["to"])
    return sql, args


def fts_hits(terms, p):
    if not terms:
        return []
    where, args = filters(p)
    sql = ("SELECT c.id, c.item_id FROM chunks_fts f JOIN chunks c ON c.id=f.rowid "
           "JOIN items i ON i.id=c.item_id WHERE chunks_fts MATCH ?" + where +
           " ORDER BY bm25(chunks_fts) LIMIT ?")
    return db().execute(sql, [fts_query(terms), *args, CANDIDATES]).fetchall()


def vec_hits(q, p):
    vec = next(iter(state["model"].embed(["query: " + q]))).astype("float32").tobytes()
    k = CANDIDATES * 3 if (p["subject"] or p["from"] or p["to"] or p["kind"]) else CANDIDATES
    rows = db().execute("SELECT rowid, distance FROM vec_chunks WHERE embedding MATCH ? AND k=? "
                        "ORDER BY distance", (vec, k)).fetchall()
    ids = [r[0] for r in rows]
    where, args = filters(p)
    marks = ",".join("?" * len(ids))
    ok = db().execute("SELECT c.id, c.item_id FROM chunks c JOIN items i ON i.id=c.item_id "
                      f"WHERE c.id IN ({marks})" + where, [*ids, *args]).fetchall()
    by_id = {r[0]: r for r in ok}
    return [by_id[i] for i in ids if i in by_id][:CANDIDATES]


def fuse(*rank_lists):
    """Reciprocal rank fusion. Returns [(item_id, best_chunk_id)] best first."""
    score, best = {}, {}
    for hits in rank_lists:
        for rank, (cid, item) in enumerate(hits):
            s = 1 / (RRF_K + rank + 1)
            score[item] = score.get(item, 0) + s
            if s > best.get(item, (0, 0))[0]:
                best[item] = (s, cid)
    return [(i, best[i][1]) for i in sorted(score, key=score.get, reverse=True)]


def browse(p):
    where, args = filters(p)
    rows = db().execute("SELECT i.id, (SELECT id FROM chunks WHERE item_id=i.id ORDER BY n LIMIT 1) "
                        f"FROM items i WHERE 1=1{where} ORDER BY i.date DESC", args).fetchall()
    return [(r[0], r[1]) for r in rows]


def search(q, p):
    terms = terms_of(q)
    if not terms:
        return browse(p), []
    return fuse(fts_hits(terms, p), vec_hits(q, p)), terms


def load_results(pairs, terms):
    out = []
    for item_id, cid in pairs:
        it = db().execute("SELECT id,kind,title,subject,date,year FROM items WHERE id=?", (item_id,)).fetchone()
        text = db().execute("SELECT text FROM chunks WHERE id=?", (cid,)).fetchone()[0]
        out.append({**dict(it), "snippet": snippet(text, terms)})
    return out


# ---- views ----
def int_arg(req, name):
    v = req.query_params.get(name, "")
    return int(v) if v.isdigit() else None


def render(req, name, status=200, **ctx):
    ctx["user"] = req.headers.get("Cf-Access-Authenticated-User-Email")
    return HTMLResponse(env.get_template(name).render(**ctx), status_code=status)


def home(req):
    q = req.query_params.get("q", "").strip()[:300]
    p = {"kind": req.query_params.get("type", ""), "subject": req.query_params.get("subject", ""),
         "from": int_arg(req, "from"), "to": int_arg(req, "to")}
    page = max(1, int_arg(req, "page") or 1)
    pairs, terms = search(q, p)
    results = load_results(pairs[(page - 1) * PAGE: page * PAGE], terms)
    pages = max(1, -(-len(pairs) // PAGE))
    args = {k: v for k, v in (("q", q), ("type", p["kind"]), ("subject", p["subject"]),
                              ("from", p["from"]), ("to", p["to"])) if v}
    return render(req, "search.html", q=q, p=p, results=results, total=len(pairs), page=page,
                  pages=pages, args=args, subjects=state["subjects"])


def item_or_404(req):
    row = db().execute("SELECT * FROM items WHERE id=?", (req.path_params["id"],)).fetchone()
    if not row:
        raise HTTPException(404)
    return row


def detail(req):
    it = item_or_404(req)
    extra = {}
    if it["kind"] == "post":
        extra["source_url"] = POST_URL + it["meta"]
    else:
        extra["doc"] = dict(zip(DOC_FIELDS, it["meta"].split("\x1f")))
    return render(req, "detail.html", it=it, **extra)


def download(req):
    it = item_or_404(req)
    if it["kind"] != "document":
        raise HTTPException(404)
    doc = dict(zip(DOC_FIELDS, it["meta"].split("\x1f")))
    path = (FILES_DIR / doc["path"]).resolve()
    if not path.is_relative_to(FILES_DIR) or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, filename=doc["filename"] or path.name)


def not_found(req, exc):
    return render(req, "error.html", status=exc.status_code, message="Stranica nije pronađena.")


def health(req):
    return HTMLResponse("ok")


@asynccontextmanager
async def lifespan(app):
    state["model"] = load_model()
    next(iter(state["model"].embed(["query: warmup"])))
    rows = db().execute("SELECT DISTINCT subject FROM items WHERE subject != '' ORDER BY subject").fetchall()
    state["subjects"] = [r[0] for r in rows]
    yield


app = Starlette(
    routes=[Route("/", home), Route("/healthz", health), Route("/item/{id:int}", detail),
            Route("/item/{id:int}/file", download),
            Mount("/static", StaticFiles(directory=HERE / "static"), name="static")],
    exception_handlers={404: not_found},
    lifespan=lifespan,
)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")))
