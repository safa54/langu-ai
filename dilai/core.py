"""Learning logic shared by the MCP server and the web app.

Every function takes an open sqlite connection. Nothing here talks to an LLM.
"""
import datetime
import json
import re

from .db import LEVELS, level_n, now, today, jl

KINDS = ["word", "phrase", "phrasal verb", "idiom"]
STATUSES = ["learning", "known", "unknown", "ignored"]

# Days until an item is due again after a counted correct use, by stage.
INTERVALS = {0: 0, 1: 1, 2: 3, 3: 7, 4: 16, 5: 35}
MAX_STAGE = 5

DEFAULTS = {
    "level": "B2",                    # the learner's own level
    "target_levels": ["B2", "C1"],    # levels new items are drawn from
    "assume_known_through": "A2",     # this level and below counts as known unless marked otherwise
    "variant": "us",                  # uk | us
    "kinds": list(KINDS),
    "topics": [],
    "new_per_session": 5,
    "review_per_session": 5,
    "intensity": 3,                   # 1 (barely noticeable) .. 5 (drill)
    "feedback": "important",          # off | important | all
    "grammar_per_session": 2,
    "known_stage": 4,                 # stage at which the system calls an item known
    "ai_level": "match",              # simpler | match | stretch
    "mode": "chat",                   # chat | roleplay | adventure | debate
    "scenario": "",
    "native_language": "",
    "hide_sensitive": False,
    "model": "opus",                  # opus | sonnet | haiku | fable
    "effort": "medium",               # how long the AI thinks before each reply: low | medium | high
}

MODES = ["chat", "roleplay", "adventure", "debate"]
MODELS = ["opus", "sonnet", "haiku", "fable"]
EFFORTS = ["low", "medium", "high"]

# What the learner can lay down for a session. Every field is optional free text.
SETUP_FIELDS = [
    ("you", "The learner plays"),
    ("partner", "You (the AI) play"),
    ("story", "Setting and story"),
    ("tone", "Tone and style"),
    ("rules", "Rules and wishes"),
]


# ---------------------------------------------------------------- settings

def get_settings(con):
    s = dict(DEFAULTS)
    for r in con.execute("SELECT key, value FROM settings"):
        if r["key"] in DEFAULTS:
            try:
                s[r["key"]] = json.loads(r["value"])
            except ValueError:
                pass
    return s


def _clean_setting(key, value):
    if key in ("level", "assume_known_through"):
        if value in ("", None) and key == "assume_known_through":
            return ""
        value = str(value).upper()
        if value not in LEVELS:
            raise ValueError("%s must be one of %s" % (key, ", ".join(LEVELS)))
        return value
    if key == "target_levels":
        vals = [str(v).upper() for v in (value if isinstance(value, list) else [value])]
        vals = [v for v in LEVELS if v in vals]
        if not vals:
            raise ValueError("target_levels needs at least one CEFR level")
        return vals
    if key == "kinds":
        vals = [v for v in KINDS if v in (value or [])]
        if not vals:
            raise ValueError("kinds needs at least one of %s" % ", ".join(KINDS))
        return vals
    if key == "topics":
        return [str(v) for v in (value or [])]
    if key in ("new_per_session", "review_per_session"):
        return max(0, min(30, int(value)))
    if key == "grammar_per_session":
        return max(0, min(6, int(value)))
    if key == "intensity":
        return max(1, min(5, int(value)))
    if key == "known_stage":
        return max(2, min(MAX_STAGE, int(value)))
    if key == "feedback":
        if value not in ("off", "important", "all"):
            raise ValueError("feedback must be off, important or all")
        return value
    if key == "ai_level":
        if value not in ("simpler", "match", "stretch"):
            raise ValueError("ai_level must be simpler, match or stretch")
        return value
    if key == "variant":
        if value not in ("uk", "us"):
            raise ValueError("variant must be uk or us")
        return value
    if key == "mode":
        if value not in MODES:
            raise ValueError("mode must be one of %s" % ", ".join(MODES))
        return value
    if key == "hide_sensitive":
        return bool(value)
    if key == "model":
        if value not in MODELS:
            raise ValueError("model must be one of %s" % ", ".join(MODELS))
        return value
    if key == "effort":
        if value not in EFFORTS:
            raise ValueError("effort must be one of %s" % ", ".join(EFFORTS))
        return value
    return str(value)[:2000]


def update_settings(con, changes):
    out = {}
    for k, v in (changes or {}).items():
        if k not in DEFAULTS:
            raise ValueError("unknown setting: %s" % k)
        out[k] = _clean_setting(k, v)
    for k, v in out.items():
        con.execute("INSERT INTO settings(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, json.dumps(v)))
    return get_settings(con)


# ---------------------------------------------------------------- vocabulary helpers

def _assumed_n(settings):
    return level_n(settings.get("assume_known_through")) or 0


def effective_status(row, settings):
    """row is a vocab row joined with item_state columns (status may be None)."""
    if row["status"]:
        return row["status"]
    if (row["level_n"] or 9) <= _assumed_n(settings):
        return "assumed_known"
    return "new"


VOCAB_JOIN = ("SELECT v.*, s.status, s.status_source, s.stage, s.correct, s.incorrect, s.credits, "
              "s.exposures, s.lookups, s.last_used, s.last_correct, s.last_incorrect, s.due_at, "
              "s.first_seen, s.last_seen, x.synonyms, x.tip, x.translation "
              "FROM vocab v LEFT JOIN item_state s ON s.vocab_id = v.id "
              "LEFT JOIN vocab_extra x ON x.vocab_id = v.id ")


def brief(row, settings, full=False):
    """Compact description of a sense for the AI (full=True adds what the side panel shows)."""
    d = {"id": row["id"], "word": row["base"]}
    if row["guideword"]:
        d["sense"] = row["guideword"]
    d["pos"] = row["pos"]
    d["level"] = row["level"]
    d["definition"] = row["definition"]
    ex = jl(row["examples"])
    if ex:
        d["example"] = ex[0]
    st = effective_status(row, settings)
    if st == "learning" and not (row["correct"] or row["incorrect"] or row["exposures"]):
        st = "new"
    d["status"] = st
    if row["status"]:
        if row["correct"] or row["incorrect"]:
            d["used_correctly"] = row["correct"]
            d["used_incorrectly"] = row["incorrect"]
        if row["last_used"]:
            d["last_used"] = row["last_used"][:10]
    if full:
        d.update({
            "kind": row["kind"], "headword": row["headword"], "topics": row["topics"],
            "examples": ex, "grammar": row["grammar"], "usage": row["usage"], "pron": row["pron"],
            "forms": [f for f in (row["forms"] or "").split(";") if f],
            "stage": row["stage"] or 0, "credits": row["credits"] or 0,
            "exposures": row["exposures"] or 0, "status_source": row["status_source"],
            "used_correctly": row["correct"] or 0, "used_incorrectly": row["incorrect"] or 0,
            "due_at": row["due_at"], "synonyms": row["synonyms"], "tip": row["tip"],
            "translation": row["translation"], "variant": row["variant"],
        })
    return d


def get_vocab(con, vocab_id):
    return con.execute(VOCAB_JOIN + "WHERE v.id = ?", (vocab_id,)).fetchone()


def _norm(text):
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def find_senses(con, text, settings, limit=12):
    """All senses whose base form or an inflected form matches text."""
    t = _norm(text)
    if not t:
        return []
    variants = (settings["variant"], "custom")
    rows = con.execute(
        VOCAB_JOIN + "WHERE v.variant IN (?, ?) AND (v.base = ? COLLATE NOCASE OR v.id IN "
        "(SELECT vocab_id FROM vocab_form WHERE form = ?)) "
        "ORDER BY (v.base = ? COLLATE NOCASE) DESC, v.level_n, v.id LIMIT ?",
        (variants[0], variants[1], t, t, t, limit)).fetchall()
    if not rows:
        rows = con.execute(
            VOCAB_JOIN + "WHERE v.variant IN (?, ?) AND v.base LIKE ? ORDER BY length(v.base), v.level_n LIMIT ?",
            (variants[0], variants[1], "%" + t + "%", limit)).fetchall()
    return rows


def resolve(con, ref, settings, session_id=None):
    """Turn an id, a word, or {id|word, sense, pos} into one vocab row (or None)."""
    if isinstance(ref, dict):
        if ref.get("id") is not None:
            return get_vocab(con, int(ref["id"]))
        word, sense, pos = ref.get("word"), ref.get("sense"), ref.get("pos")
    elif isinstance(ref, int) or (isinstance(ref, str) and ref.strip().isdigit()):
        return get_vocab(con, int(ref))
    else:
        word, sense, pos = ref, None, None
    rows = find_senses(con, word, settings, limit=40)
    if not rows:
        return None
    exact = [r for r in rows if _norm(r["base"]) == _norm(word)] or rows
    if sense:
        m = [r for r in exact if _norm(r["guideword"]) == _norm(sense)]
        if m:
            return m[0]
    if pos:
        m = [r for r in exact if _norm(r["pos"]) == _norm(pos)]
        if m:
            exact = m
    if session_id:
        ids = set(r["vocab_id"] for r in con.execute(
            "SELECT vocab_id FROM session_item WHERE session_id = ?", (session_id,)))
        m = [r for r in exact if r["id"] in ids]
        if m:
            return m[0]
    m = [r for r in exact if r["status"]]
    return (m or exact)[0]


def add_custom(con, word, definition, level=None, kind=None, example=None):
    """An item the profile does not list (for instance an idiom the AI introduced)."""
    word = (word or "").strip()
    if not word or not definition:
        raise ValueError("a custom item needs a word and a definition")
    row = con.execute("SELECT id FROM vocab WHERE variant='custom' AND base = ? COLLATE NOCASE", (word,)).fetchone()
    if row:
        return row["id"]
    level = level if level in LEVELS else None
    if kind not in KINDS:
        kind = "word" if " " not in word else "phrase"
    n = con.execute("SELECT COUNT(*) FROM vocab WHERE variant='custom'").fetchone()[0] + 1
    cur = con.execute(
        "INSERT INTO vocab(evp_id, variant, base, headword, pos, kind, level, level_n, definition, examples, forms) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("custom:%d:%s" % (n, word), "custom", word, word, kind, kind, level, level_n(level),
         definition, json.dumps([example] if example else []), word.lower()))
    con.execute("INSERT OR IGNORE INTO vocab_form(form, vocab_id) VALUES(?, ?)", (word.lower(), cur.lastrowid))
    return cur.lastrowid


def set_notes(con, vocab_id, synonyms=None, tip=None, translation=None):
    con.execute(
        "INSERT INTO vocab_extra(vocab_id, synonyms, tip, translation) VALUES(?,?,?,?) "
        "ON CONFLICT(vocab_id) DO UPDATE SET synonyms=COALESCE(excluded.synonyms, synonyms), "
        "tip=COALESCE(excluded.tip, tip), translation=COALESCE(excluded.translation, translation)",
        (vocab_id, synonyms, tip, translation))


# ---------------------------------------------------------------- learner state

def _ensure_state(con, vocab_id):
    con.execute("INSERT OR IGNORE INTO item_state(vocab_id, first_seen, last_seen) VALUES(?, ?, ?)",
                (vocab_id, now(), now()))
    return con.execute("SELECT * FROM item_state WHERE vocab_id = ?", (vocab_id,)).fetchone()


def _event(con, session_id, kind, vocab_id=None, grammar_id=None, sentence=None, note=None, spontaneous=0):
    con.execute("INSERT INTO usage_event(ts, session_id, vocab_id, grammar_id, kind, spontaneous, sentence, note) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (now(), session_id, vocab_id, grammar_id, kind, spontaneous, sentence, note))


def _due_after(stage):
    d = datetime.datetime.now() + datetime.timedelta(days=INTERVALS.get(stage, 35))
    return d.replace(microsecond=0).isoformat(sep=" ")


def add_session_item(con, session_id, vocab_id, role="target"):
    """Put an item in the session's panel. A target stays a target if it is later merely exposed."""
    if not session_id:
        return
    cur = con.execute("SELECT role FROM session_item WHERE session_id=? AND vocab_id=?",
                      (session_id, vocab_id)).fetchone()
    if cur is None:
        con.execute("INSERT INTO session_item(session_id, vocab_id, role, added_at) VALUES(?,?,?,?)",
                    (session_id, vocab_id, role, now()))
    elif role == "target" and cur["role"] != "target":
        con.execute("UPDATE session_item SET role='target' WHERE session_id=? AND vocab_id=?",
                    (session_id, vocab_id))
    _ensure_state(con, vocab_id)
    con.execute("UPDATE item_state SET last_seen=? WHERE vocab_id=?", (now(), vocab_id))


def record_usage(con, session_id, vocab_id, result, sentence=None, note=None, settings=None):
    """The learner used an item. result: correct | incorrect.

    Repetition inside one day earns one credit only, and a credit only raises the
    stage when the item was due - so drilling a word ten times in a row does not
    make it 'known'; coming back to it over days and weeks does.
    """
    settings = settings or get_settings(con)
    row = get_vocab(con, vocab_id)
    if row is None:
        raise ValueError("no vocabulary item with id %s" % vocab_id)
    had_state = row["status"] is not None
    in_session = session_id and con.execute(
        "SELECT 1 FROM session_item WHERE session_id=? AND vocab_id=?", (session_id, vocab_id)).fetchone()
    spontaneous = 0 if in_session else 1
    assumed = (not had_state) and (row["level_n"] or 9) <= _assumed_n(settings)
    st = _ensure_state(con, vocab_id)
    t, day = now(), today()
    stage, status, source = st["stage"], st["status"], st["status_source"]
    out = {"id": vocab_id, "word": row["base"], "result": result}

    if result == "correct":
        counted = st["last_credit_day"] != day
        if assumed:
            status, source, stage = "known", "system", max(stage, settings["known_stage"])
        elif counted:
            due = st["due_at"] is None or st["due_at"] <= t
            if spontaneous and not had_state:
                stage = max(stage, 2)       # used unprompted, first time we see it: real evidence
            elif due:
                stage = min(MAX_STAGE, stage + 1)
            if stage >= settings["known_stage"] and status in ("learning", "unknown"):
                status, source = "known", "system"
            elif status == "unknown":
                status, source = "learning", "system"
        con.execute(
            "UPDATE item_state SET correct=correct+1, credits=credits+?, stage=?, status=?, status_source=?, "
            "last_used=?, last_correct=?, last_seen=?, last_credit_day=?, due_at=? WHERE vocab_id=?",
            (1 if counted else 0, stage, status, source, t, t, t,
             day if counted else st["last_credit_day"],
             _due_after(stage) if (counted or st["due_at"] is None) else st["due_at"], vocab_id))
        out["counted"] = bool(counted)
        if not counted:
            out["note"] = "already credited today; repeats on the same day do not raise mastery"
    elif result == "incorrect":
        stage = max(0, stage - 2)
        if status in ("known", "ignored"):
            status, source = "learning", "system"
            out["note"] = "was marked known; moved back to practice"
        con.execute(
            "UPDATE item_state SET incorrect=incorrect+1, stage=?, status=?, status_source=?, last_used=?, "
            "last_incorrect=?, last_seen=?, due_at=? WHERE vocab_id=?",
            (stage, status, source, t, t, t, t, vocab_id))
    else:
        raise ValueError("result must be 'correct' or 'incorrect'")

    if session_id:
        state = "used_ok" if result == "correct" else "used_wrong"
        if in_session:
            con.execute("UPDATE session_item SET state=? WHERE session_id=? AND vocab_id=? "
                        "AND state NOT IN ('known')", (state, session_id, vocab_id))
        elif not assumed:
            con.execute("INSERT INTO session_item(session_id, vocab_id, role, state, added_at) VALUES(?,?,?,?,?)",
                        (session_id, vocab_id, "spontaneous", state, t))
    _event(con, session_id, result, vocab_id=vocab_id, sentence=sentence, note=note, spontaneous=spontaneous)
    out.update({"status": status, "stage": stage, "spontaneous": bool(spontaneous)})
    return out


def mark_exposed(con, session_id, vocab_id, target=False):
    """The AI used this item in its own message; show it in the panel."""
    add_session_item(con, session_id, vocab_id, "target" if target else "exposed")
    con.execute("UPDATE item_state SET exposures=exposures+1, last_seen=? WHERE vocab_id=?", (now(), vocab_id))
    if session_id:
        con.execute("UPDATE session_item SET ai_used=ai_used+1 WHERE session_id=? AND vocab_id=?",
                    (session_id, vocab_id))
    _event(con, session_id, "exposed", vocab_id=vocab_id)


def set_status(con, vocab_id, status, source="user", session_id=None):
    """known | unknown (don't know it - practise it) | learning | ignored | skip (not now, this session only)."""
    row = get_vocab(con, vocab_id)
    if row is None:
        raise ValueError("no vocabulary item with id %s" % vocab_id)
    if status == "skip":
        if session_id:
            con.execute("UPDATE session_item SET state='skipped' WHERE session_id=? AND vocab_id=?",
                        (session_id, vocab_id))
        _event(con, session_id, "mark", vocab_id=vocab_id, note="skip")
        return {"id": vocab_id, "word": row["base"], "status": "skipped for this session"}
    if status not in STATUSES:
        raise ValueError("status must be one of %s, or skip" % ", ".join(STATUSES))
    st = _ensure_state(con, vocab_id)
    stage, due = st["stage"], st["due_at"]
    if status == "unknown":
        stage, due = 0, now()
    elif status == "learning" and st["status"] in ("known", "ignored"):
        stage, due = min(stage, 2), now()
    con.execute("UPDATE item_state SET status=?, status_source=?, stage=?, due_at=?, last_seen=? WHERE vocab_id=?",
                (status, source, stage, due, now(), vocab_id))
    if session_id:
        s_state = {"known": "known", "ignored": "skipped", "unknown": "unknown", "learning": "proposed"}[status]
        con.execute("UPDATE session_item SET state=? WHERE session_id=? AND vocab_id=?",
                    (s_state, session_id, vocab_id))
    _event(con, session_id, "mark", vocab_id=vocab_id, note="%s by %s" % (status, source))
    return {"id": vocab_id, "word": row["base"], "status": status}


def note_lookup(con, session_id, vocab_id):
    _ensure_state(con, vocab_id)
    con.execute("UPDATE item_state SET lookups=lookups+1 WHERE vocab_id=?", (vocab_id,))


# ---------------------------------------------------------------- picking items

def _filters(settings, levels=None, kinds=None, topic=None, pos=None):
    where, args = ["v.variant IN (?, 'custom')"], [settings["variant"]]
    if settings.get("hide_sensitive"):
        where.append("v.culture = 0")
    if kinds:
        kinds = [k for k in kinds if k in KINDS]
    kinds = kinds or settings["kinds"]
    where.append("v.kind IN (%s)" % ",".join("?" * len(kinds)))
    args += kinds
    if pos:
        where.append("v.pos = ?")
        args.append(pos.lower())
    topics = [topic] if topic else settings.get("topics") or []
    if topics:
        where.append("(" + " OR ".join(["v.topics LIKE ?"] * len(topics)) + ")")
        args += ["%" + t + "%" for t in topics]
    return where, args, levels


def pick_words(con, session_id, count=None, levels=None, kinds=None, topic=None, pos=None,
               strategy="mixed", add=True, settings=None):
    """Choose items to practise.

    strategy: new (never met) | review (met, due again) | weak (misused or marked 'don't know')
              | mixed (review first, then new, in the proportions set in the settings)
    Items the learner already knows - marked, earned, or assumed from their level - are never returned.
    """
    settings = settings or get_settings(con)
    levels = [l for l in LEVELS if l in [str(x).upper() for x in (levels or [])]] or settings["target_levels"]
    lv = [level_n(l) for l in levels]
    where, args, _ = _filters(settings, levels, kinds, topic, pos)
    in_session = "v.id NOT IN (SELECT vocab_id FROM session_item WHERE session_id = ?)"
    sid = session_id or -1
    t = now()

    def new(n):
        if n <= 0:
            return []
        w = where + ["s.vocab_id IS NULL", "v.level_n IN (%s)" % ",".join("?" * len(lv)),
                     "v.level_n > ?", in_session]
        rows = con.execute(VOCAB_JOIN + "WHERE " + " AND ".join(w) + " ORDER BY RANDOM() LIMIT ?",
                           args + lv + [_assumed_n(settings), sid, n * 4]).fetchall()
        return rows

    def review(n, weak_only=False):
        if n <= 0:
            return []
        w = ["v.variant IN (?, 'custom')", "s.status IN ('learning', 'unknown')", in_session]
        a = [settings["variant"], sid]
        if weak_only:
            w.append("(s.status = 'unknown' OR s.incorrect > s.correct OR "
                     "(s.last_incorrect IS NOT NULL AND (s.last_correct IS NULL OR s.last_incorrect > s.last_correct)))")
        else:
            w.append("(s.due_at IS NULL OR s.due_at <= ?)")
            a.append(t)
        if topic:
            w.append("v.topics LIKE ?")
            a.append("%" + topic + "%")
        if kinds:
            ks = [k for k in kinds if k in KINDS]
            if ks:
                w.append("v.kind IN (%s)" % ",".join("?" * len(ks)))
                a += ks
        rows = con.execute(
            VOCAB_JOIN + "WHERE " + " AND ".join(w) +
            " ORDER BY (s.status = 'unknown') DESC, (s.last_incorrect IS NOT NULL AND "
            "(s.last_correct IS NULL OR s.last_incorrect > s.last_correct)) DESC, "
            "COALESCE(s.due_at, s.last_seen), RANDOM() LIMIT ?", a + [n * 3]).fetchall()
        return rows

    if strategy == "new":
        count = settings["new_per_session"] if count is None else count
        plan = [(new, count)]
    elif strategy == "review":
        count = settings["review_per_session"] if count is None else count
        plan = [(review, count)]
    elif strategy == "weak":
        count = settings["review_per_session"] if count is None else count
        plan = [(lambda n: review(n, True), count)]
    else:
        if count is None:
            n_rev, n_new = settings["review_per_session"], settings["new_per_session"]
        else:
            tot = (settings["review_per_session"] + settings["new_per_session"]) or 1
            n_rev = int(round(count * settings["review_per_session"] / float(tot)))
            n_new = count - n_rev
        plan = [(review, n_rev), (new, n_new)]

    chosen, seen_words = [], set()
    for fn, n in plan:
        got = 0
        for r in fn(n):
            keys = set([(r["ref_id"] or "").lower(), r["base"].lower()]) - set([""])
            if keys & seen_words or got >= n:
                continue
            seen_words |= keys
            chosen.append(r)
            got += 1

    out = []
    for r in chosen:
        if add and session_id:
            add_session_item(con, session_id, r["id"], "target")
        out.append(brief(r, settings))
    return out


# ---------------------------------------------------------------- grammar

def _gbrief(r):
    d = {"id": r["id"], "level": r["level"], "area": "%s: %s" % (r["supercategory"], r["subcategory"]),
         "point": r["guideword"], "can_do": r["can_do"]}
    if r["example"]:
        d["example"] = r["example"].split("\n")[0][:240]
    if r["g_status"]:
        d["used_correctly"], d["used_incorrectly"] = r["g_correct"], r["g_incorrect"]
    return d


GRAMMAR_JOIN = ("SELECT g.*, s.status AS g_status, s.correct AS g_correct, s.incorrect AS g_incorrect "
                "FROM grammar g LEFT JOIN grammar_state s ON s.grammar_id = g.id ")


def pick_grammar(con, session_id=None, count=3, levels=None, query=None, strategy="mixed", add=True,
                 settings=None):
    """Grammar points from the English Grammar Profile. strategy: weak | new | mixed. query searches text."""
    settings = settings or get_settings(con)
    levels = [l for l in LEVELS if l in [str(x).upper() for x in (levels or [])]]
    if not levels:
        n = level_n(settings["level"]) or 4
        levels = [LEVELS[i - 1] for i in (n, min(6, n + 1))]
    lv = [level_n(l) for l in levels]
    rows = []
    if query:
        q = "%" + query + "%"
        rows = con.execute(
            GRAMMAR_JOIN + "WHERE (g.supercategory LIKE ? OR g.subcategory LIKE ? OR g.guideword LIKE ? "
            "OR g.can_do LIKE ?) ORDER BY (g.level_n IN (%s)) DESC, g.level_n LIMIT ?" % ",".join("?" * len(lv)),
            [q, q, q, q] + lv + [count]).fetchall()
    else:
        if strategy in ("weak", "mixed"):
            rows += con.execute(
                GRAMMAR_JOIN + "WHERE s.incorrect > s.correct ORDER BY s.last_incorrect DESC LIMIT ?",
                (count,)).fetchall()
        if strategy in ("new", "mixed") and len(rows) < count:
            rows += con.execute(
                GRAMMAR_JOIN + "WHERE s.grammar_id IS NULL AND g.level_n IN (%s) ORDER BY RANDOM() LIMIT ?"
                % ",".join("?" * len(lv)), lv + [count - len(rows)]).fetchall()
    if add and session_id:
        for r in rows:
            con.execute("INSERT OR IGNORE INTO session_grammar(session_id, grammar_id, added_at) VALUES(?,?,?)",
                        (session_id, r["id"], now()))
    return [_gbrief(r) for r in rows]


def record_grammar(con, session_id, grammar_id, result, sentence=None, note=None):
    if not con.execute("SELECT 1 FROM grammar WHERE id=?", (grammar_id,)).fetchone():
        raise ValueError("no grammar point with id %s" % grammar_id)
    if result not in ("correct", "incorrect"):
        raise ValueError("result must be 'correct' or 'incorrect'")
    con.execute("INSERT OR IGNORE INTO grammar_state(grammar_id) VALUES(?)", (grammar_id,))
    col = "correct" if result == "correct" else "incorrect"
    extra = ", last_incorrect=?" if result == "incorrect" else ""
    args = [now()] + ([now()] if result == "incorrect" else []) + [grammar_id]
    con.execute("UPDATE grammar_state SET %s=%s+1, last_seen=?%s WHERE grammar_id=?" % (col, col, extra), args)
    _event(con, session_id, result, grammar_id=grammar_id, sentence=sentence, note=note)
    return {"grammar_id": grammar_id, "result": result}


# ---------------------------------------------------------------- feedback

def add_feedback(con, session_id, original, corrected, explanation, category="other", severity="minor",
                 vocab_id=None, grammar_id=None):
    msg = None
    if session_id:
        msg = con.execute("SELECT id FROM message WHERE session_id=? AND role='user' ORDER BY id DESC LIMIT 1",
                          (session_id,)).fetchone()
    cur = con.execute(
        "INSERT INTO feedback(ts, session_id, message_id, original, corrected, explanation, category, severity, "
        "vocab_id, grammar_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (now(), session_id, msg["id"] if msg else None, original, corrected, explanation, category, severity,
         vocab_id, grammar_id))
    return cur.lastrowid


# ---------------------------------------------------------------- sessions

def clean_setup(setup):
    out = {}
    for key, _ in SETUP_FIELDS:
        v = str((setup or {}).get(key) or "").strip()[:4000]
        if v:
            out[key] = v
    return out


def new_session(con, source="web", mode=None, scenario=None, title=None, setup=None):
    s = get_settings(con)
    setup = clean_setup(setup)
    cur = con.execute("INSERT INTO session(started_at, last_active, title, mode, scenario, source, setup) "
                      "VALUES(?,?,?,?,?,?,?)",
                      (now(), now(), (title or "").strip()[:80] or None, mode or s["mode"],
                       scenario if scenario is not None else s["scenario"], source,
                       json.dumps(setup, ensure_ascii=False) if setup else None))
    return cur.lastrowid


def get_setup(srow):
    try:
        d = json.loads(srow["setup"]) if srow and srow["setup"] else {}
    except ValueError:
        d = {}
    return d if isinstance(d, dict) else {}


def set_setup(con, session_id, setup):
    setup = clean_setup(setup)
    con.execute("UPDATE session SET setup=? WHERE id=?",
                (json.dumps(setup, ensure_ascii=False) if setup else None, session_id))
    return setup


def setup_text(setup):
    return "\n".join("%s: %s" % (label, setup[key]) for key, label in SETUP_FIELDS if setup.get(key))


# ---------------------------------------------------------------- story notes (continuity)

MAX_NOTES = 60


def story_notes(con, session_id):
    rows = con.execute("SELECT id, text, source FROM story_note WHERE session_id=? AND active=1 ORDER BY id",
                       (session_id,)).fetchall()
    return [dict(r) for r in rows][-MAX_NOTES:]


def add_story_note(con, session_id, text, source="ai"):
    text = re.sub(r"\s+", " ", str(text or "")).strip()[:400]
    if not text or not session_id:
        return None
    if con.execute("SELECT 1 FROM story_note WHERE session_id=? AND active=1 AND text=? COLLATE NOCASE",
                   (session_id, text)).fetchone():
        return None
    return con.execute("INSERT INTO story_note(session_id, ts, text, source) VALUES(?,?,?,?)",
                       (session_id, now(), text, source)).lastrowid


def drop_story_note(con, session_id, note_id):
    con.execute("UPDATE story_note SET active=0 WHERE id=? AND session_id=?", (int(note_id), session_id))


def transcript(con, session_id, limit=80):
    rows = con.execute("SELECT role, text FROM message WHERE session_id=? ORDER BY id DESC LIMIT ?",
                       (session_id, limit)).fetchall()
    return [{"who": "learner" if r["role"] == "user" else "you", "text": r["text"]} for r in reversed(rows)]


# ---------------------------------------------------------------- my words

def my_words(con, settings=None):
    """Every item the learner has a record for, with the numbers behind it."""
    settings = settings or get_settings(con)
    rows = con.execute(VOCAB_JOIN + "WHERE s.vocab_id IS NOT NULL AND v.variant IN (?, 'custom') "
                       "ORDER BY s.last_seen DESC", (settings["variant"],)).fetchall()
    t, out = now(), []
    for r in rows:
        d = brief(r, settings, full=True)
        d["status"] = r["status"]
        d["due"] = r["status"] in ("learning", "unknown") and (r["due_at"] is None or r["due_at"] <= t)
        d["last_used"] = r["last_used"]
        d["first_seen"], d["last_seen"] = r["first_seen"], r["last_seen"]
        out.append(d)
    return out


def touch_session(con, session_id):
    con.execute("UPDATE session SET last_active=? WHERE id=?", (now(), session_id))


def latest_session(con, source=None, max_age_hours=None):
    q, a = "SELECT * FROM session", []
    if source:
        q += " WHERE source=?"
        a.append(source)
    row = con.execute(q + " ORDER BY last_active DESC LIMIT 1", a).fetchone()
    if row and max_age_hours:
        cut = (datetime.datetime.now() - datetime.timedelta(hours=max_age_hours)).isoformat(sep=" ")
        if (row["last_active"] or "") < cut:
            return None
    return row


def session_items(con, session_id, settings, full=False):
    rows = con.execute(
        "SELECT v.*, s.status, s.status_source, s.stage, s.correct, s.incorrect, s.credits, s.exposures, "
        "s.lookups, s.last_used, s.last_correct, s.last_incorrect, s.due_at, x.synonyms, x.tip, x.translation, "
        "si.role AS si_role, si.state AS si_state, si.ai_used AS si_ai_used "
        "FROM session_item si JOIN vocab v ON v.id = si.vocab_id "
        "LEFT JOIN item_state s ON s.vocab_id = v.id LEFT JOIN vocab_extra x ON x.vocab_id = v.id "
        "WHERE si.session_id = ? ORDER BY si.added_at, v.id", (session_id,)).fetchall()
    out = []
    for r in rows:
        d = brief(r, settings, full=full)
        d["role"], d["session_state"], d["ai_used"] = r["si_role"], r["si_state"], r["si_ai_used"]
        out.append(d)
    return out


def session_grammar(con, session_id):
    rows = con.execute(GRAMMAR_JOIN + "JOIN session_grammar sg ON sg.grammar_id = g.id "
                       "WHERE sg.session_id = ? ORDER BY sg.added_at", (session_id,)).fetchall()
    return [_gbrief(r) for r in rows]


def begin_session(con, session_id, settings=None):
    """Fill a fresh session with targets and grammar points; return what the AI needs to start."""
    settings = settings or get_settings(con)
    if not con.execute("SELECT 1 FROM session_item WHERE session_id=? LIMIT 1", (session_id,)).fetchone():
        pick_words(con, session_id, strategy="mixed", settings=settings)
    if settings["grammar_per_session"] and not con.execute(
            "SELECT 1 FROM session_grammar WHERE session_id=? LIMIT 1", (session_id,)).fetchone():
        pick_grammar(con, session_id, count=settings["grammar_per_session"], settings=settings)
    return session_brief(con, session_id, settings)


def session_brief(con, session_id, settings=None):
    settings = settings or get_settings(con)
    srow = con.execute("SELECT * FROM session WHERE id=?", (session_id,)).fetchone()
    items = session_items(con, session_id, settings)
    active = [i for i in items if i["session_state"] not in ("skipped", "known") and i["role"] != "spontaneous"]
    dropped = [{"word": i["word"], "why": i["session_state"]} for i in items
               if i["session_state"] in ("skipped", "known")]
    for i in active:
        st = i.pop("session_state")
        i["in_session"] = {"proposed": "not used by learner yet", "used_ok": "learner used it correctly",
                           "used_wrong": "learner misused it - give another chance",
                           "unknown": "learner says they do not know it - teach it in context"}.get(st, st)
        if not i.pop("ai_used"):
            i["you_have_used_it"] = False
    recent = con.execute(
        "SELECT original, corrected, category FROM feedback ORDER BY id DESC LIMIT 5").fetchall()
    return {
        "session_id": session_id,
        "mode": srow["mode"] if srow else settings["mode"],
        "scenario": (srow["scenario"] if srow else settings["scenario"]) or None,
        "setup": get_setup(srow) or None,
        "story_notes": ["%d: %s" % (n["id"], n["text"]) for n in story_notes(con, session_id)] or None,
        "learner": {k: settings[k] for k in ("level", "target_levels", "intensity", "feedback", "ai_level",
                                             "native_language", "variant")},
        "target_items": active,
        "dropped_by_learner": dropped,
        "grammar_focus": session_grammar(con, session_id),
        "recent_mistakes": [dict(r) for r in recent],
    }


# ---------------------------------------------------------------- panel -> AI

def push_panel_event(con, session_id, text):
    con.execute("INSERT INTO panel_event(ts, session_id, text) VALUES(?,?,?)", (now(), session_id, text))


def pop_panel_events(con, session_id):
    rows = con.execute("SELECT id, text FROM panel_event WHERE delivered=0 AND (session_id=? OR session_id IS NULL) "
                       "ORDER BY id", (session_id,)).fetchall()
    if rows:
        con.execute("UPDATE panel_event SET delivered=1 WHERE id IN (%s)" % ",".join("?" * len(rows)),
                    [r["id"] for r in rows])
    return [r["text"] for r in rows]


# ---------------------------------------------------------------- overview

def stats(con, settings=None):
    settings = settings or get_settings(con)
    by = {}
    for r in con.execute("SELECT v.level, s.status, COUNT(*) n FROM item_state s JOIN vocab v ON v.id=s.vocab_id "
                         "GROUP BY v.level, s.status"):
        by.setdefault(r["level"] or "-", {})[r["status"]] = r["n"]
    totals = {r["level"]: r["n"] for r in con.execute(
        "SELECT level, COUNT(*) n FROM vocab WHERE variant=? GROUP BY level", (settings["variant"],))}
    due = con.execute("SELECT COUNT(*) FROM item_state WHERE status IN ('learning','unknown') "
                      "AND (due_at IS NULL OR due_at <= ?)", (now(),)).fetchone()[0]
    weak = con.execute(
        VOCAB_JOIN + "WHERE s.incorrect > 0 AND s.status != 'known' ORDER BY s.last_incorrect DESC LIMIT 8").fetchall()
    gram = con.execute(GRAMMAR_JOIN + "WHERE s.incorrect > 0 ORDER BY s.incorrect - s.correct DESC, "
                       "s.last_incorrect DESC LIMIT 6").fetchall()
    return {
        "profile_size_by_level": totals,
        "tracked_by_level": by,
        "assumed_known_through": settings["assume_known_through"] or None,
        "due_for_review": due,
        "weak_items": [brief(r, settings) for r in weak],
        "weak_grammar": [_gbrief(r) for r in gram],
        "sessions": con.execute("SELECT COUNT(*) FROM session").fetchone()[0],
    }
