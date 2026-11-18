"""SQLite storage for Langu-AI: reference data (EVP vocabulary, EGP grammar) and learner state."""
import json
import os
import sqlite3
import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("DILAI_DB", os.path.join(ROOT, "data", "dilai.db"))

LEVELS = ["A1", "A2", "B1", "B2", "C1", "C2"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS vocab (
  id INTEGER PRIMARY KEY,
  evp_id TEXT UNIQUE,            -- e.g. ID_00000002_01_UK, or custom:<n>
  variant TEXT NOT NULL,         -- uk | us | custom
  ref_id TEXT,                   -- groups all senses of one headword
  base TEXT NOT NULL,            -- the word / phrase / phrasal verb as listed
  headword TEXT,                 -- dictionary headword it belongs to
  guideword TEXT,                -- sense label, e.g. LEAVE
  pos TEXT,                      -- noun, verb, phrase, phrasal verb ...
  kind TEXT,                     -- word | phrase | phrasal verb | idiom
  level TEXT, level_n INTEGER,   -- CEFR level, 1..6
  topics TEXT,
  definition TEXT,
  examples TEXT,                 -- JSON list
  grammar TEXT, usage TEXT, pron TEXT,
  forms TEXT,                    -- ';'-separated inflected forms
  culture INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS vocab_base ON vocab(base COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS vocab_sel ON vocab(variant, level_n, kind);
CREATE INDEX IF NOT EXISTS vocab_ref ON vocab(ref_id);

CREATE TABLE IF NOT EXISTS vocab_form (
  form TEXT NOT NULL COLLATE NOCASE, vocab_id INTEGER NOT NULL,
  PRIMARY KEY (form, vocab_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS grammar (
  id INTEGER PRIMARY KEY,
  egp_id TEXT UNIQUE,
  supercategory TEXT, subcategory TEXT,
  level TEXT, level_n INTEGER,
  guideword TEXT, can_do TEXT, example TEXT, lexical_range TEXT
);
CREATE INDEX IF NOT EXISTS grammar_sel ON grammar(level_n, supercategory);

-- notes the AI adds once and that are then reused (a cache)
CREATE TABLE IF NOT EXISTS vocab_extra (
  vocab_id INTEGER PRIMARY KEY, synonyms TEXT, tip TEXT, translation TEXT
);

-- what the learner knows about each vocabulary sense
CREATE TABLE IF NOT EXISTS item_state (
  vocab_id INTEGER PRIMARY KEY,
  status TEXT NOT NULL DEFAULT 'learning',   -- learning | known | unknown | ignored
  status_source TEXT NOT NULL DEFAULT 'system', -- user | system
  stage INTEGER NOT NULL DEFAULT 0,          -- spaced-practice stage 0..5
  correct INTEGER NOT NULL DEFAULT 0,        -- raw count of correct uses
  incorrect INTEGER NOT NULL DEFAULT 0,
  credits INTEGER NOT NULL DEFAULT 0,        -- correct uses that counted (max one per day)
  exposures INTEGER NOT NULL DEFAULT 0,      -- times the AI used it in front of the learner
  lookups INTEGER NOT NULL DEFAULT 0,        -- times the learner opened its card
  first_seen TEXT, last_seen TEXT, last_used TEXT,
  last_correct TEXT, last_incorrect TEXT, last_credit_day TEXT,
  due_at TEXT,
  note TEXT
);
CREATE INDEX IF NOT EXISTS item_state_sel ON item_state(status, due_at);

CREATE TABLE IF NOT EXISTS grammar_state (
  grammar_id INTEGER PRIMARY KEY,
  status TEXT NOT NULL DEFAULT 'learning',
  correct INTEGER NOT NULL DEFAULT 0,
  incorrect INTEGER NOT NULL DEFAULT 0,
  last_seen TEXT, last_incorrect TEXT
);

CREATE TABLE IF NOT EXISTS session (
  id INTEGER PRIMARY KEY,
  started_at TEXT, last_active TEXT,
  title TEXT, mode TEXT, scenario TEXT,
  source TEXT,                   -- web | desktop
  cli_session TEXT               -- Claude Code session id, for resuming a web chat
);

CREATE TABLE IF NOT EXISTS session_item (
  session_id INTEGER NOT NULL, vocab_id INTEGER NOT NULL,
  role TEXT NOT NULL,            -- target (picked for practice) | exposed (AI used it in passing)
  state TEXT NOT NULL DEFAULT 'proposed', -- proposed | used_ok | used_wrong | skipped | known | unknown
  ai_used INTEGER NOT NULL DEFAULT 0,
  added_at TEXT,
  PRIMARY KEY (session_id, vocab_id)
);

CREATE TABLE IF NOT EXISTS session_grammar (
  session_id INTEGER NOT NULL, grammar_id INTEGER NOT NULL, added_at TEXT,
  PRIMARY KEY (session_id, grammar_id)
);

CREATE TABLE IF NOT EXISTS message (
  id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL, ts TEXT,
  role TEXT, text TEXT
);
CREATE INDEX IF NOT EXISTS message_s ON message(session_id, id);

CREATE TABLE IF NOT EXISTS usage_event (
  id INTEGER PRIMARY KEY, ts TEXT, session_id INTEGER,
  vocab_id INTEGER, grammar_id INTEGER,
  kind TEXT,                     -- correct | incorrect | exposed | lookup | mark
  spontaneous INTEGER DEFAULT 0,
  sentence TEXT, note TEXT
);
CREATE INDEX IF NOT EXISTS usage_v ON usage_event(vocab_id, ts);

CREATE TABLE IF NOT EXISTS feedback (
  id INTEGER PRIMARY KEY, ts TEXT, session_id INTEGER, message_id INTEGER,
  original TEXT, corrected TEXT, explanation TEXT,
  category TEXT, severity TEXT,
  vocab_id INTEGER, grammar_id INTEGER
);
CREATE INDEX IF NOT EXISTS feedback_s ON feedback(session_id, id);

-- things the learner did in the side panel that the AI has not been told about yet
CREATE TABLE IF NOT EXISTS panel_event (
  id INTEGER PRIMARY KEY, ts TEXT, session_id INTEGER, text TEXT, delivered INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);

-- facts the story has established (who is who, who knows what, who holds what); kept true by the AI
CREATE TABLE IF NOT EXISTS story_note (
  id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL, ts TEXT,
  text TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'ai',   -- ai | user
  active INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS story_note_s ON story_note(session_id, id);
"""

# columns added after the first version: (table, column, definition)
MIGRATIONS = [("session", "setup", "TEXT")]   # JSON: the learner's own roles / story / rules for the session


def now():
    return datetime.datetime.now().replace(microsecond=0).isoformat(sep=" ")


def today():
    return datetime.date.today().isoformat()


_ready = set()


def connect(path=None):
    path = path or DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path, timeout=15, isolation_level=None, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=15000")
    if path not in _ready:
        con.executescript(SCHEMA)
        for table, col, definition in MIGRATIONS:
            if col not in [r["name"] for r in con.execute("PRAGMA table_info(%s)" % table)]:
                con.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, definition))
        _ready.add(path)
    return con


def level_n(level):
    try:
        return LEVELS.index(level) + 1
    except ValueError:
        return None


def jl(text):
    try:
        return json.loads(text) if text else []
    except ValueError:
        return []
