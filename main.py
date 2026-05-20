import os
import json
import sqlite3
import logging
from datetime import datetime
from contextlib import contextmanager
from typing import Optional

import anthropic
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from twilio.request_validator import RequestValidator

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = FastAPI(title="FSFN Inventory")
app.mount("/static", StaticFiles(directory="static"), name="static")

DB_PATH = os.getenv("DB_PATH", "inventory.db")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
VALIDATE_TWILIO = os.getenv("VALIDATE_TWILIO", "true").lower() == "true"

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def column_exists(conn, table: str, column: str) -> bool:
    return any(r["name"] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sms_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                received_at TEXT    NOT NULL,
                from_number TEXT    NOT NULL,
                raw_body    TEXT    NOT NULL,
                parsed_json TEXT,
                parse_error TEXT
            );

            CREATE TABLE IF NOT EXISTS inventory (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                item        TEXT    NOT NULL,
                quantity    INTEGER NOT NULL DEFAULT 0,
                unit        TEXT    NOT NULL DEFAULT 'units',
                updated_at  TEXT    NOT NULL,
                UNIQUE(item)
            );

            CREATE TABLE IF NOT EXISTS sales_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                recorded_at TEXT    NOT NULL,
                amount_usd  REAL    NOT NULL,
                note        TEXT,
                sms_event_id INTEGER REFERENCES sms_events(id),
                site_id     INTEGER REFERENCES sites(id)
            );

            CREATE TABLE IF NOT EXISTS item_registry (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                canonical TEXT    NOT NULL UNIQUE,
                unit      TEXT    NOT NULL DEFAULT 'units',
                aliases   TEXT    NOT NULL DEFAULT '[]',
                weight_per_unit REAL
            );

            CREATE TABLE IF NOT EXISTS unknown_items (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                seen_at     TEXT NOT NULL,
                raw_name    TEXT NOT NULL,
                sms_event_id INTEGER REFERENCES sms_events(id),
                resolved_to TEXT,
                UNIQUE(raw_name)
            );

            CREATE TABLE IF NOT EXISTS sites (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                canonical TEXT    NOT NULL UNIQUE,
                aliases   TEXT    NOT NULL DEFAULT '[]'
            );

            CREATE TABLE IF NOT EXISTS inventory_batches (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                item         TEXT    NOT NULL,
                site_id      INTEGER REFERENCES sites(id),
                quantity     INTEGER NOT NULL,
                unit         TEXT    NOT NULL DEFAULT 'units',
                weight_lbs   REAL,
                quality_score INTEGER,
                received_at  TEXT    NOT NULL,
                sms_event_id INTEGER REFERENCES sms_events(id),
                note         TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_batches_item ON inventory_batches(item);
            CREATE INDEX IF NOT EXISTS idx_batches_received_at ON inventory_batches(received_at);
            CREATE INDEX IF NOT EXISTS idx_sales_recorded_at ON sales_log(recorded_at);
        """)

        # Idempotent migrations for older DBs
        if not column_exists(conn, "item_registry", "weight_per_unit"):
            conn.execute("ALTER TABLE item_registry ADD COLUMN weight_per_unit REAL")
        if not column_exists(conn, "sales_log", "site_id"):
            conn.execute("ALTER TABLE sales_log ADD COLUMN site_id INTEGER REFERENCES sites(id)")

    log.info("Database initialised at %s", DB_PATH)


init_db()


# ---------------------------------------------------------------------------
# Item registry helpers
# ---------------------------------------------------------------------------

# (canonical, unit, aliases, approx weight per unit in lbs)
DEFAULT_REGISTRY = [
    ("lemon boxes",    "boxes",  ["lemons", "lmons", "lemon", "lemon box", "citrus boxes"], 30.0),
    ("apple boxes",    "boxes",  ["apples", "apple", "apple box"], 40.0),
    ("tomato flats",   "flats",  ["tomatoes", "tomato", "toms", "tomato flat"], 25.0),
    ("bread loaves",   "loaves", ["bread", "loaf", "loaves", "bread loaf"], 1.0),
    ("eggplant",       "units",  ["aubergine", "aubergines", "eggplants", "egg plant"], 1.0),
    ("potato bags",    "bags",   ["potatoes", "potato", "spuds", "potato bag"], 10.0),
    ("onion bags",     "bags",   ["onions", "onion", "onion bag"], 10.0),
    ("mixed greens",   "bags",   ["greens", "salad", "salad bags", "mixed salad"], 1.0),
    ("canned goods",   "cans",   ["cans", "tinned goods", "tins", "canned food"], 1.0),
    ("dairy boxes",    "boxes",  ["dairy", "milk", "cheese", "dairy box"], 20.0),
]

DEFAULT_SITES = [
    ("downtown",       ["downtown", "main", "main site", "hq"]),
    ("east side",      ["east", "east side", "eastside"]),
    ("warehouse",      ["warehouse", "wh", "depot"]),
]


def seed_registry_if_empty():
    with get_db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM item_registry").fetchone()[0]
        if count == 0:
            conn.executemany(
                "INSERT OR IGNORE INTO item_registry (canonical, unit, aliases, weight_per_unit) VALUES (?, ?, ?, ?)",
                [(c, u, json.dumps(a), w) for c, u, a, w in DEFAULT_REGISTRY],
            )
            log.info("Seeded item registry with %d items", len(DEFAULT_REGISTRY))

        site_count = conn.execute("SELECT COUNT(*) FROM sites").fetchone()[0]
        if site_count == 0:
            conn.executemany(
                "INSERT OR IGNORE INTO sites (canonical, aliases) VALUES (?, ?)",
                [(c, json.dumps(a)) for c, a in DEFAULT_SITES],
            )
            log.info("Seeded sites with %d entries", len(DEFAULT_SITES))


seed_registry_if_empty()


def backfill_batches_from_inventory():
    """One-time: if there are inventory rows but no batches, create a synthetic
    batch per item so the splash page has something to show. Idempotent."""
    with get_db() as conn:
        n_batches = conn.execute("SELECT COUNT(*) FROM inventory_batches").fetchone()[0]
        if n_batches > 0:
            return
        rows = conn.execute("SELECT item, quantity, unit, updated_at FROM inventory").fetchall()
        if not rows:
            return
        for r in rows:
            conn.execute(
                """
                INSERT INTO inventory_batches
                  (item, site_id, quantity, unit, weight_lbs, quality_score, received_at, note)
                VALUES (?, NULL, ?, ?, NULL, NULL, ?, ?)
                """,
                (r["item"], r["quantity"], r["unit"], r["updated_at"], "backfilled from inventory snapshot"),
            )
        log.info("Backfilled %d synthetic batches from inventory snapshot", len(rows))


backfill_batches_from_inventory()


def get_registry() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT canonical, unit, aliases, weight_per_unit FROM item_registry ORDER BY canonical"
        ).fetchall()
    return [
        {
            "canonical": r["canonical"],
            "unit": r["unit"],
            "aliases": json.loads(r["aliases"]),
            "weight_per_unit": r["weight_per_unit"],
        }
        for r in rows
    ]


def get_sites() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute("SELECT id, canonical, aliases FROM sites ORDER BY canonical").fetchall()
    return [
        {"id": r["id"], "canonical": r["canonical"], "aliases": json.loads(r["aliases"])}
        for r in rows
    ]


def resolve_site(name: Optional[str]) -> Optional[int]:
    """Resolve a site name (possibly an alias) to a site row id."""
    if not name:
        return None
    n = name.lower().strip()
    with get_db() as conn:
        row = conn.execute("SELECT id, aliases FROM sites WHERE canonical=?", (n,)).fetchone()
        if row:
            return row["id"]
        for r in conn.execute("SELECT id, aliases FROM sites").fetchall():
            if n in {a.lower() for a in json.loads(r["aliases"])}:
                return r["id"]
    return None


def build_parse_system() -> str:
    registry = get_registry()
    sites = get_sites()
    registry_lines = "\n".join(
        f'  - "{r["canonical"]}" ({r["unit"]}) — also accept: {", ".join(repr(a) for a in r["aliases"])}'
        for r in registry
    )
    site_lines = "\n".join(
        f'  - "{s["canonical"]}" — also accept: {", ".join(repr(a) for a in s["aliases"])}'
        for s in sites
    ) or "  (no sites registered yet)"
    return f"""
You parse text messages from food bank volunteers into structured JSON.
Messages may report:
  - sales/revenue (e.g. "sold $150", "sales 200")
  - inventory restocks (e.g. "3 lemon boxes", "got 4 apple boxes in")
  - inventory removals or usage (e.g. "used 2 bread loaves", "gave out 5 bags")

Each message comes from one site. The volunteer may say the site at the start
("at downtown: 3 lemon boxes"), at the end ("3 lemon boxes — eastside"), or
omit it entirely. Resolve site names using the registry below.

Volunteers may report weight ("got 45 lbs lemons") and/or count ("3 lemon
boxes"). When weight is mentioned, capture it as weight_lbs. When omitted,
leave weight_lbs null — the server will estimate from registry weight-per-unit.

Volunteers may include a quality cue ("A grade", "great condition", "kinda
bruised", "expired"). Map to 1–5:
  5 = excellent / A grade / fresh
  4 = good
  3 = average / OK
  2 = poor / bruised / nearing expiry
  1 = bad / expired / unusable
If no quality cue, leave quality_score null.

CANONICAL ITEM REGISTRY — you MUST map any item mention to one of these exact canonical names.
Use fuzzy matching: handle typos, abbreviations, regional names (e.g. aubergine→eggplant), plurals.
{registry_lines}

SITE REGISTRY — map any site mention to one of these exact canonical names.
{site_lines}

If an item cannot be confidently matched to the registry, use the raw text as the item name
and set "unrecognized": true on that change entry so staff can review it.

Return ONLY valid JSON, no other text, matching this schema:
{{
  "site": "<canonical site name from registry, or null>",
  "sale_usd": <number or null>,
  "changes": [
    {{
      "item": "<canonical name from registry, or raw text if unrecognized>",
      "delta": <positive=add, negative=remove>,
      "unit": "<unit>",
      "weight_lbs": <number or null>,
      "quality_score": <integer 1-5 or null>,
      "unrecognized": <true if not in registry, omit or false otherwise>
    }}
  ],
  "note": "<brief human-readable summary>"
}}

If a message is completely unrelated to inventory/sales, return:
{{"site": null, "sale_usd": null, "changes": [], "note": "unrecognized message"}}
"""


# ---------------------------------------------------------------------------
# SMS parsing via Claude
# ---------------------------------------------------------------------------

def parse_sms(body: str) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=512,
        system=build_parse_system(),
        messages=[{"role": "user", "content": body}],
    )
    text = msg.content[0].text.strip()
    return json.loads(text)


# ---------------------------------------------------------------------------
# Apply parsed update
# ---------------------------------------------------------------------------

def estimate_weight(conn, item: str, quantity: int) -> Optional[float]:
    row = conn.execute(
        "SELECT weight_per_unit FROM item_registry WHERE canonical=?", (item,)
    ).fetchone()
    if row and row["weight_per_unit"] is not None:
        return round(row["weight_per_unit"] * quantity, 2)
    return None


def apply_update(conn: sqlite3.Connection, parsed: dict, event_id: int):
    now = datetime.utcnow().isoformat()
    site_id = resolve_site(parsed.get("site"))
    note = parsed.get("note")

    if parsed.get("sale_usd"):
        conn.execute(
            "INSERT INTO sales_log (recorded_at, amount_usd, note, sms_event_id, site_id) VALUES (?, ?, ?, ?, ?)",
            (now, parsed["sale_usd"], note, event_id, site_id),
        )

    for change in parsed.get("changes", []):
        item = change["item"].lower().strip()
        delta = int(change["delta"])
        unit = change.get("unit", "units")
        weight_lbs = change.get("weight_lbs")
        quality = change.get("quality_score")

        if change.get("unrecognized"):
            conn.execute(
                "INSERT OR IGNORE INTO unknown_items (seen_at, raw_name, sms_event_id) VALUES (?, ?, ?)",
                (now, item, event_id),
            )
            log.warning("Unrecognized item in SMS %d: %r", event_id, item)
            continue

        # Restock → record a batch row
        if delta > 0:
            est_weight = weight_lbs if weight_lbs is not None else estimate_weight(conn, item, delta)
            conn.execute(
                """
                INSERT INTO inventory_batches
                  (item, site_id, quantity, unit, weight_lbs, quality_score, received_at, sms_event_id, note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (item, site_id, delta, unit, est_weight, quality, now, event_id, note),
            )

        # Always update the rollup table
        conn.execute(
            """
            INSERT INTO inventory (item, quantity, unit, updated_at)
            VALUES (?, MAX(0, ?), ?, ?)
            ON CONFLICT(item) DO UPDATE SET
                quantity   = MAX(0, inventory.quantity + excluded.quantity),
                unit       = excluded.unit,
                updated_at = excluded.updated_at
            """,
            (item, delta, unit, now),
        )


# ---------------------------------------------------------------------------
# Twilio webhook
# ---------------------------------------------------------------------------

@app.post("/sms")
async def receive_sms(
    request: Request,
    From: str = Form(...),
    Body: str = Form(...),
):
    if VALIDATE_TWILIO and TWILIO_AUTH_TOKEN:
        validator = RequestValidator(TWILIO_AUTH_TOKEN)
        url = str(request.url)
        form_data = dict(await request.form())
        sig = request.headers.get("X-Twilio-Signature", "")
        if not validator.validate(url, form_data, sig):
            raise HTTPException(status_code=403, detail="Invalid Twilio signature")

    now = datetime.utcnow().isoformat()
    log.info("SMS from %s: %s", From, Body)

    parsed = None
    parse_error = None

    try:
        parsed = parse_sms(Body)
    except Exception as exc:
        parse_error = str(exc)
        log.error("Parse error: %s", exc)

    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO sms_events (received_at, from_number, raw_body, parsed_json, parse_error) VALUES (?, ?, ?, ?, ?)",
            (now, From, Body, json.dumps(parsed) if parsed else None, parse_error),
        )
        event_id = cur.lastrowid

        if parsed and not parse_error:
            try:
                apply_update(conn, parsed, event_id)
            except Exception as exc:
                log.error("DB update error: %s", exc)

    return HTMLResponse(
        content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
        media_type="application/xml",
    )


# ---------------------------------------------------------------------------
# Inventory + feed + sales APIs
# ---------------------------------------------------------------------------

@app.get("/api/inventory")
def api_inventory():
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT i.item, i.quantity, i.unit, i.updated_at,
                   r.weight_per_unit,
                   COALESCE(
                     (SELECT SUM(b.weight_lbs) FROM inventory_batches b WHERE b.item = i.item),
                     0
                   ) AS total_weight_lbs
            FROM inventory i
            LEFT JOIN item_registry r ON r.canonical = i.item
            ORDER BY i.item
            """
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/feed")
def api_feed(limit: int = 20):
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, received_at, from_number, raw_body, parsed_json, parse_error
            FROM sms_events
            ORDER BY received_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        d["parsed"] = json.loads(d["parsed_json"]) if d["parsed_json"] else None
        items.append(d)
    return items


@app.get("/api/sales")
def api_sales():
    with get_db() as conn:
        total = conn.execute("SELECT COALESCE(SUM(amount_usd),0) as total FROM sales_log").fetchone()["total"]
        today = conn.execute(
            "SELECT COALESCE(SUM(amount_usd),0) as total FROM sales_log WHERE DATE(recorded_at)=DATE('now')"
        ).fetchone()["total"]
        count = conn.execute("SELECT COUNT(*) as n FROM sales_log").fetchone()["n"]
    return {"total_usd": total, "today_usd": today, "transaction_count": count}


# ---------------------------------------------------------------------------
# Cashflow + totals (charts)
# ---------------------------------------------------------------------------

@app.get("/api/cashflow")
def api_cashflow(bucket: str = "day", since_days: int = 30, site: Optional[str] = None):
    """
    Returns sales aggregated by time bucket, optionally segmented by site.
    bucket: 'hour' | 'day' | 'week'
    since_days: window size in days
    site: optional canonical name filter
    """
    fmt = {
        "hour": "%Y-%m-%d %H:00",
        "day":  "%Y-%m-%d",
        "week": "%Y-W%W",
    }.get(bucket, "%Y-%m-%d")

    params = [fmt, f"-{int(since_days)} days"]
    where = ["recorded_at >= datetime('now', ?)"]
    if site:
        where.append("s.canonical = ?")
        params.append(site)
    where_sql = " AND ".join(where)

    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT strftime(?, recorded_at) AS bucket,
                   COALESCE(s.canonical, 'unassigned') AS site,
                   SUM(l.amount_usd) AS total
            FROM sales_log l
            LEFT JOIN sites s ON s.id = l.site_id
            WHERE {where_sql}
            GROUP BY bucket, site
            ORDER BY bucket ASC
            """,
            params,
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/totals")
def api_totals(metric: str = "quantity", segment: str = "item"):
    """
    Aggregates current state.
    metric:  'quantity' | 'weight'
    segment: 'item' | 'site'
    """
    if segment == "site":
        with get_db() as conn:
            rows = conn.execute(
                """
                SELECT COALESCE(s.canonical, 'unassigned') AS label,
                       SUM(b.quantity) AS quantity,
                       COALESCE(SUM(b.weight_lbs), 0) AS weight
                FROM inventory_batches b
                LEFT JOIN sites s ON s.id = b.site_id
                GROUP BY label
                ORDER BY label
                """
            ).fetchall()
    else:
        with get_db() as conn:
            rows = conn.execute(
                """
                SELECT i.item AS label,
                       i.quantity AS quantity,
                       COALESCE(
                         (SELECT SUM(b.weight_lbs) FROM inventory_batches b WHERE b.item = i.item),
                         0
                       ) AS weight
                FROM inventory i
                ORDER BY i.item
                """
            ).fetchall()

    key = "weight" if metric == "weight" else "quantity"
    return [{"label": r["label"], "value": r[key] or 0} for r in rows]


# ---------------------------------------------------------------------------
# Item detail (splash page)
# ---------------------------------------------------------------------------

@app.get("/api/item/{canonical:path}")
def api_item_detail(canonical: str):
    canonical = canonical.lower().strip()
    with get_db() as conn:
        reg = conn.execute(
            "SELECT canonical, unit, aliases, weight_per_unit FROM item_registry WHERE canonical=?",
            (canonical,),
        ).fetchone()
        inv = conn.execute(
            "SELECT quantity, unit, updated_at FROM inventory WHERE item=?",
            (canonical,),
        ).fetchone()
        batches = conn.execute(
            """
            SELECT b.id, b.quantity, b.unit, b.weight_lbs, b.quality_score,
                   b.received_at, b.note,
                   COALESCE(s.canonical, 'unassigned') AS site
            FROM inventory_batches b
            LEFT JOIN sites s ON s.id = b.site_id
            WHERE b.item = ?
            ORDER BY b.received_at DESC
            """,
            (canonical,),
        ).fetchall()

    if not reg and not inv and not batches:
        raise HTTPException(404, f"item '{canonical}' not found")

    batches_list = [dict(b) for b in batches]
    quality_vals = [b["quality_score"] for b in batches_list if b["quality_score"] is not None]
    avg_quality = round(sum(quality_vals) / len(quality_vals), 2) if quality_vals else None
    total_weight = round(sum((b["weight_lbs"] or 0) for b in batches_list), 2)

    return {
        "canonical": canonical,
        "registry": dict(reg) | {"aliases": json.loads(reg["aliases"])} if reg else None,
        "current": dict(inv) if inv else None,
        "batches": batches_list,
        "avg_quality": avg_quality,
        "total_weight_lbs": total_weight,
    }


# ---------------------------------------------------------------------------
# Registry + sites + unknowns management
# ---------------------------------------------------------------------------

@app.get("/api/registry")
def api_registry():
    return get_registry()


@app.post("/api/registry")
def api_registry_add(payload: dict):
    canonical = payload.get("canonical", "").lower().strip()
    unit = payload.get("unit", "units")
    aliases = payload.get("aliases", [])
    weight_per_unit = payload.get("weight_per_unit")
    if not canonical:
        raise HTTPException(400, "canonical required")
    with get_db() as conn:
        conn.execute(
            "INSERT INTO item_registry (canonical, unit, aliases, weight_per_unit) VALUES (?, ?, ?, ?)",
            (canonical, unit, json.dumps(aliases), weight_per_unit),
        )
    return {"ok": True, "canonical": canonical}


@app.patch("/api/registry/{canonical:path}")
def api_registry_update(canonical: str, payload: dict):
    """Update an existing item: aliases (append), unit, weight_per_unit."""
    new_aliases = payload.get("aliases")
    unit = payload.get("unit")
    weight_per_unit = payload.get("weight_per_unit")
    with get_db() as conn:
        row = conn.execute(
            "SELECT aliases, unit, weight_per_unit FROM item_registry WHERE canonical=?", (canonical,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "item not found")
        merged = json.loads(row["aliases"])
        if new_aliases:
            merged = list(dict.fromkeys(merged + new_aliases))
        conn.execute(
            "UPDATE item_registry SET aliases=?, unit=COALESCE(?, unit), weight_per_unit=COALESCE(?, weight_per_unit) WHERE canonical=?",
            (json.dumps(merged), unit, weight_per_unit, canonical),
        )
    return {"ok": True, "canonical": canonical, "aliases": merged}


@app.delete("/api/registry/{canonical:path}")
def api_registry_delete(canonical: str):
    with get_db() as conn:
        conn.execute("DELETE FROM item_registry WHERE canonical=?", (canonical,))
    return {"ok": True}


@app.get("/api/sites")
def api_sites():
    return get_sites()


@app.post("/api/sites")
def api_sites_add(payload: dict):
    canonical = payload.get("canonical", "").lower().strip()
    aliases = payload.get("aliases", [])
    if not canonical:
        raise HTTPException(400, "canonical required")
    with get_db() as conn:
        conn.execute(
            "INSERT INTO sites (canonical, aliases) VALUES (?, ?)",
            (canonical, json.dumps(aliases)),
        )
    return {"ok": True, "canonical": canonical}


@app.patch("/api/sites/{canonical:path}")
def api_sites_update(canonical: str, payload: dict):
    new_aliases = payload.get("aliases", [])
    with get_db() as conn:
        row = conn.execute("SELECT aliases FROM sites WHERE canonical=?", (canonical,)).fetchone()
        if not row:
            raise HTTPException(404, "site not found")
        merged = list(dict.fromkeys(json.loads(row["aliases"]) + new_aliases))
        conn.execute("UPDATE sites SET aliases=? WHERE canonical=?", (json.dumps(merged), canonical))
    return {"ok": True, "canonical": canonical, "aliases": merged}


@app.delete("/api/sites/{canonical:path}")
def api_sites_delete(canonical: str):
    with get_db() as conn:
        conn.execute("DELETE FROM sites WHERE canonical=?", (canonical,))
    return {"ok": True}


@app.get("/api/unknowns")
def api_unknowns():
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT u.id, u.seen_at, u.raw_name, u.resolved_to, u.sms_event_id, e.raw_body
            FROM unknown_items u
            LEFT JOIN sms_events e ON e.id = u.sms_event_id
            WHERE u.resolved_to IS NULL
            ORDER BY u.seen_at DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/unknowns/{unknown_id}/resolve")
def api_resolve_unknown(unknown_id: int, payload: dict):
    action = payload.get("action")
    canonical = payload.get("canonical", "").lower().strip()
    if not canonical:
        raise HTTPException(400, "canonical required")

    with get_db() as conn:
        row = conn.execute(
            "SELECT raw_name FROM unknown_items WHERE id=?", (unknown_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "unknown item not found")
        raw_name = row["raw_name"]

        if action == "add":
            unit = payload.get("unit", "units")
            weight_per_unit = payload.get("weight_per_unit")
            conn.execute(
                "INSERT OR IGNORE INTO item_registry (canonical, unit, aliases, weight_per_unit) VALUES (?, ?, ?, ?)",
                (canonical, unit, json.dumps([raw_name]), weight_per_unit),
            )
        elif action == "map":
            alias_row = conn.execute(
                "SELECT aliases FROM item_registry WHERE canonical=?", (canonical,)
            ).fetchone()
            if not alias_row:
                raise HTTPException(404, f"canonical '{canonical}' not in registry")
            existing = json.loads(alias_row["aliases"])
            if raw_name not in existing:
                existing.append(raw_name)
                conn.execute(
                    "UPDATE item_registry SET aliases=? WHERE canonical=?",
                    (json.dumps(existing), canonical),
                )
        else:
            raise HTTPException(400, "action must be 'map' or 'add'")

        conn.execute(
            "UPDATE unknown_items SET resolved_to=? WHERE id=?", (canonical, unknown_id)
        )

    return {"ok": True, "raw_name": raw_name, "resolved_to": canonical}


# ---------------------------------------------------------------------------
# HTML pages
# ---------------------------------------------------------------------------

def _render(path: str) -> str:
    with open(path) as f:
        return f.read()


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return _render("templates/index.html")


@app.get("/admin", response_class=HTMLResponse)
def admin_page():
    return _render("templates/admin.html")


@app.get("/item/{canonical:path}", response_class=HTMLResponse)
def item_page(canonical: str):
    return _render("templates/item.html")
