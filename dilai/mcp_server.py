"""Langu-AI MCP server (stdio, standard library only).

Run:  python3 -m dilai.mcp_server
Env:  DILAI_SESSION=<id>   pin every call to one session (the web chat sets this)
      DILAI_DB=<path>      use another database file
"""
import json
import os
import sys
import traceback

from . import core, db, prompts

PROTOCOL = "2024-11-05"
PINNED = os.environ.get("DILAI_SESSION")
_state = {"session": int(PINNED) if PINNED else None}

ITEM_REF = {
    "type": "object",
    "description": "A vocabulary item: give its id when you have it, otherwise the word (and the sense label "
                   "when the word has several senses).",
    "properties": {
        "id": {"type": "integer"},
        "word": {"type": "string", "description": "Base form as listed, e.g. 'give up', 'reluctant'."},
        "sense": {"type": "string", "description": "Sense label (guideword) from the profile, e.g. 'LEAVE'."},
    },
}

LEVEL_ARRAY = {"type": "array", "items": {"type": "string", "enum": db.LEVELS}}

TOOLS = [
    {
        "name": "start_session",
        "description": "Begin (or rejoin) a practice session. Returns the guide for how to run the conversation, "
                       "the target vocabulary chosen for this session, the grammar points in focus and the "
                       "learner's settings. Call this first when the learner wants to practise English.",
        "inputSchema": {"type": "object", "properties": {
            "mode": {"type": "string", "enum": core.MODES,
                     "description": "Kind of conversation. Omit to use the learner's default."},
            "scenario": {"type": "string", "description": "Scene or topic the learner asked for, in a sentence."},
            "setup": {"type": "object", "description": "Details the learner gave for a role-play or story.",
                      "properties": dict((k, {"type": "string", "description": label})
                                         for k, label in core.SETUP_FIELDS)},
            "new": {"type": "boolean", "description": "Force a fresh session even if one is still active."},
        }},
    },
    {
        "name": "log_turn",
        "description": "Record one turn of the conversation. Call once per turn, before writing your reply. "
                       "Saves which items the learner used well or badly, shows corrections beside the "
                       "learner's message, and puts the notable items from your reply in the side panel. "
                       "Returns anything the learner changed in the panel and which targets are still unused.",
        "inputSchema": {"type": "object", "properties": {
            "learner_used": {"type": "array", "description": "Items the learner used in their last message.",
                             "items": {"type": "object", "properties": {
                                 "id": {"type": "integer"}, "word": {"type": "string"},
                                 "sense": {"type": "string"},
                                 "result": {"type": "string", "enum": ["correct", "incorrect"]},
                                 "sentence": {"type": "string",
                                              "description": "The learner's sentence containing it."},
                                 "note": {"type": "string", "description": "What was wrong, if incorrect."},
                             }, "required": ["result"]}},
            "i_used": {"type": "array",
                       "description": "Target items and other notable items your reply uses. For something the "
                                      "profile does not list (an idiom, say), give word and definition.",
                       "items": {"type": "object", "properties": {
                           "id": {"type": "integer"}, "word": {"type": "string"}, "sense": {"type": "string"},
                           "definition": {"type": "string",
                                          "description": "Only for items not in the profile."},
                           "level": {"type": "string", "enum": db.LEVELS},
                       }}},
            "feedback": {"type": "array", "description": "Corrections for the learner's last message.",
                         "items": {"type": "object", "properties": {
                             "original": {"type": "string", "description": "The learner's exact wording."},
                             "corrected": {"type": "string"},
                             "explanation": {"type": "string", "description": "One short plain sentence."},
                             "category": {"type": "string",
                                          "enum": ["grammar", "word choice", "collocation", "preposition",
                                                   "spelling", "register", "word form", "other"]},
                             "severity": {"type": "string", "enum": ["minor", "important"]},
                             "grammar_id": {"type": "integer",
                                            "description": "Grammar Profile point involved, if known."},
                         }, "required": ["original", "corrected", "explanation"]}},
            "grammar": {"type": "array", "description": "Grammar points in focus the learner got right or wrong.",
                        "items": {"type": "object", "properties": {
                            "id": {"type": "integer"},
                            "result": {"type": "string", "enum": ["correct", "incorrect"]},
                            "sentence": {"type": "string"},
                        }, "required": ["id", "result"]}},
            "story_notes": {"type": "array", "items": {"type": "string"},
                            "description": "New facts that must stay true later, one short sentence each: who "
                                           "is who, what a character knows or does not know, who holds what, "
                                           "promises, lies, decisions, where everyone is."},
            "drop_story_notes": {"type": "array", "items": {"type": "integer"},
                                 "description": "Ids of earlier story notes that are no longer true."},
        }},
    },
    {
        "name": "get_words",
        "description": "Draw vocabulary items from the Cambridge English Vocabulary Profile at random, filtered by "
                       "level, kind, topic and the learner's history, and add them to the session as targets. "
                       "Items the learner already knows are never returned. Each sense of a word is a separate "
                       "item with its own level.",
        "inputSchema": {"type": "object", "properties": {
            "count": {"type": "integer", "minimum": 1, "maximum": 30},
            "strategy": {"type": "string", "enum": ["mixed", "new", "review", "weak"],
                         "description": "new: never met. review: met before and due again. weak: misused or "
                                        "marked 'don't know'. mixed (default): review first, then new."},
            "levels": LEVEL_ARRAY,
            "kinds": {"type": "array", "items": {"type": "string", "enum": core.KINDS}},
            "pos": {"type": "string", "description": "Part of speech, e.g. noun, verb, adjective, adverb."},
            "topic": {"type": "string", "description": "Profile topic, e.g. 'food and drink', 'work', 'crime'."},
            "add_to_session": {"type": "boolean", "description": "Default true. False just previews."},
        }},
    },
    {
        "name": "lookup_word",
        "description": "Look up a word or phrase in the profile: every sense with its level, definition, example "
                       "and what the learner's record says about it.",
        "inputSchema": {"type": "object", "properties": {
            "word": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 30},
        }, "required": ["word"]},
    },
    {
        "name": "set_word_status",
        "description": "Change what the system believes about an item, when the learner says so in the chat. "
                       "known: knows it well, stop proposing it. unknown: does not know it, practise it. "
                       "learning: put it back in practice. ignored: never propose it. skip: not now, this "
                       "session only.",
        "inputSchema": {"type": "object", "properties": {
            "item": ITEM_REF,
            "status": {"type": "string", "enum": ["known", "unknown", "learning", "ignored", "skip"]},
        }, "required": ["item", "status"]},
    },
    {
        "name": "add_word_notes",
        "description": "Save extra notes for items (synonyms, a usage tip, a translation). Shown on the item's "
                       "card in the side panel and reused in later sessions.",
        "inputSchema": {"type": "object", "properties": {
            "items": {"type": "array", "items": {"type": "object", "properties": {
                "id": {"type": "integer"},
                "synonyms": {"type": "string", "description": "Comma-separated."},
                "tip": {"type": "string"},
                "translation": {"type": "string"},
            }, "required": ["id"]}},
        }, "required": ["items"]},
    },
    {
        "name": "get_grammar",
        "description": "Find grammar points in the Cambridge English Grammar Profile (can-do statements by CEFR "
                       "level), by level and free-text search, or pick ones the learner is weak at or has not "
                       "met. Adds them to the session's grammar focus.",
        "inputSchema": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Search text, e.g. 'conditional', 'passive', 'modality'."},
            "levels": LEVEL_ARRAY,
            "count": {"type": "integer", "minimum": 1, "maximum": 15},
            "strategy": {"type": "string", "enum": ["mixed", "weak", "new"]},
            "add_to_session": {"type": "boolean"},
        }},
    },
    {
        "name": "settings",
        "description": "Read the learner's settings, or change some when the learner asks in the chat (for "
                       "example 'give me more new words' or 'be stricter'). Call with no arguments to read.",
        "inputSchema": {"type": "object", "properties": {
            "changes": {"type": "object", "description":
                        "Any of: level, target_levels, assume_known_through, variant (uk|us), kinds, topics, "
                        "new_per_session, review_per_session, intensity (1-5), feedback (off|important|all), "
                        "grammar_per_session, known_stage (2-5), ai_level (simpler|match|stretch), mode, "
                        "scenario, native_language."},
        }},
    },
    {
        "name": "progress",
        "description": "Summary of the learner's progress: items tracked by level and status, how many are due "
                       "for review, weakest items and grammar points.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def session_id(con, create=True):
    if _state["session"]:
        return _state["session"]
    row = core.latest_session(con, source="desktop", max_age_hours=6)
    if row:
        _state["session"] = row["id"]
    elif create:
        _state["session"] = core.new_session(con, source="desktop")
        core.begin_session(con, _state["session"])
    return _state["session"]


def _with_updates(con, sid, result):
    ev = core.pop_panel_events(con, sid) if sid else []
    if ev:
        result["panel_updates"] = ev
    return result


def _needs_notes(con, items):
    for it in items:
        r = con.execute("SELECT synonyms, tip FROM vocab_extra WHERE vocab_id=?", (it["id"],)).fetchone()
        if not r or not (r["synonyms"] or r["tip"]):
            it["needs_notes"] = True
    return items


def call_tool(con, name, a):
    s = core.get_settings(con)
    if name == "start_session":
        if not PINNED:
            row = None if a.get("new") else core.latest_session(con, source="desktop", max_age_hours=6)
            if row:
                _state["session"] = row["id"]
                if a.get("mode") or a.get("scenario"):
                    con.execute("UPDATE session SET mode=COALESCE(?, mode), scenario=COALESCE(?, scenario) "
                                "WHERE id=?", (a.get("mode"), a.get("scenario"), row["id"]))
            else:
                _state["session"] = core.new_session(con, source="desktop", mode=a.get("mode"),
                                                     scenario=a.get("scenario"), setup=a.get("setup"))
        sid = _state["session"]
        if a.get("setup") and not PINNED:
            core.set_setup(con, sid, a["setup"])
        core.touch_session(con, sid)
        b = core.begin_session(con, sid, s)
        _needs_notes(con, b["target_items"])
        b["guide"] = prompts.build_guide(s, b["mode"], b["scenario"], web=bool(PINNED), setup=b.get("setup"))
        if not PINNED:
            b["panel_url"] = "http://localhost:%s/" % os.environ.get("DILAI_PORT", "8787")
        return _with_updates(con, sid, b)

    sid = session_id(con)
    core.touch_session(con, sid)

    if name == "log_turn":
        out = {}
        used, unresolved = [], []
        for u in a.get("learner_used") or []:
            row = core.resolve(con, u, s, sid)
            if row is None:
                unresolved.append(u.get("word") or u.get("id"))
                continue
            used.append(core.record_usage(con, sid, row["id"], u.get("result"), u.get("sentence"),
                                          u.get("note"), s))
        if used:
            out["recorded"] = used
        shown = []
        for u in a.get("i_used") or []:
            row = core.resolve(con, u, s, sid)
            if row is None and u.get("word") and u.get("definition"):
                row = core.get_vocab(con, core.add_custom(con, u["word"], u["definition"], u.get("level")))
            if row is None:
                unresolved.append(u.get("word") or u.get("id"))
                continue
            if core.effective_status(row, s) in ("assumed_known",):
                continue
            core.mark_exposed(con, sid, row["id"])
            shown.append(row["base"])
        if shown:
            out["shown_in_panel"] = shown
        n = 0
        for f in a.get("feedback") or []:
            if s["feedback"] == "off":
                break
            core.add_feedback(con, sid, f.get("original", ""), f.get("corrected", ""), f.get("explanation", ""),
                              f.get("category", "other"), f.get("severity", "minor"),
                              grammar_id=f.get("grammar_id"))
            n += 1
        if n:
            out["feedback_shown"] = n
        elif a.get("feedback") and s["feedback"] == "off":
            out["feedback_shown"] = "none: the learner has feedback switched off"
        for g in a.get("grammar") or []:
            try:
                core.record_grammar(con, sid, int(g["id"]), g.get("result"), g.get("sentence"))
            except (ValueError, KeyError) as e:
                unresolved.append("grammar: %s" % e)
        if unresolved:
            out["not_found"] = unresolved
            out["hint"] = ("Use lookup_word to find an item's id, or pass a definition in i_used to add an item "
                           "the profile does not list.")
        for nid in a.get("drop_story_notes") or []:
            try:
                core.drop_story_note(con, sid, nid)
            except (ValueError, TypeError):
                pass
        added = [core.add_story_note(con, sid, t) for t in (a.get("story_notes") or [])[:8]]
        notes = core.story_notes(con, sid)
        turns = con.execute("SELECT COUNT(*) FROM usage_event WHERE session_id=? AND kind='turn'",
                            (sid,)).fetchone()[0]
        con.execute("INSERT INTO usage_event(ts, session_id, kind) VALUES(?,?,'turn')", (db.now(), sid))
        if any(added) or a.get("drop_story_notes"):
            out["story_notes_saved"] = len([x for x in added if x])
        if notes and turns % 6 == 5:
            # every few turns, hand the whole list back so the facts stay in front of the model
            out["story_notes_so_far"] = ["%d: %s" % (n["id"], n["text"]) for n in notes]
        items = core.session_items(con, sid, s)
        left = [i["word"] for i in items if i["role"] == "target" and i["session_state"] in ("proposed", "unknown",
                                                                                          "used_wrong")]
        out["targets_still_unused_by_learner"] = left
        if not left:
            out["hint_more"] = "All targets are done or dropped. Call get_words for more if the learner is up for it."
        return _with_updates(con, sid, out)

    if name == "get_words":
        items = core.pick_words(con, sid, count=a.get("count"), levels=a.get("levels"), kinds=a.get("kinds"),
                                topic=a.get("topic"), pos=a.get("pos"), strategy=a.get("strategy") or "mixed",
                                add=a.get("add_to_session", True), settings=s)
        res = {"items": _needs_notes(con, items)}
        if not items:
            res["note"] = "Nothing matches. Widen the levels, kinds or topic, or try another strategy."
        return _with_updates(con, sid, res)

    if name == "lookup_word":
        rows = core.find_senses(con, a.get("word", ""), s, limit=int(a.get("limit") or 12))
        return {"senses": [core.brief(r, s) for r in rows]} if rows else \
            {"senses": [], "note": "Not in the profile. You can still use it; report it in log_turn i_used "
                                   "with a definition to add it."}

    if name == "set_word_status":
        row = core.resolve(con, a.get("item"), s, sid)
        if row is None:
            raise ValueError("item not found; use lookup_word to find its id")
        return _with_updates(con, sid, core.set_status(con, row["id"], a.get("status"), "user", sid))

    if name == "add_word_notes":
        n = 0
        for it in a.get("items") or []:
            if core.get_vocab(con, int(it["id"])) is None:
                continue
            core.set_notes(con, int(it["id"]), it.get("synonyms"), it.get("tip"), it.get("translation"))
            n += 1
        return {"saved": n}

    if name == "get_grammar":
        pts = core.pick_grammar(con, sid, count=int(a.get("count") or 3), levels=a.get("levels"),
                                query=a.get("query"), strategy=a.get("strategy") or "mixed",
                                add=a.get("add_to_session", True), settings=s)
        return {"points": pts}

    if name == "settings":
        if a.get("changes"):
            s = core.update_settings(con, a["changes"])
        return {"settings": s}

    if name == "progress":
        return core.stats(con, s)

    raise ValueError("unknown tool: %s" % name)


def handle(con, msg):
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": (msg.get("params") or {}).get("protocolVersion") or PROTOCOL,
            "capabilities": {"tools": {}, "prompts": {}},
            "serverInfo": {"name": "dilai", "version": "0.1.0"},
            "instructions": prompts.SERVER_INSTRUCTIONS}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"prompts": [
            {"name": "practice", "description": "Start an English practice session with Langu-AI",
             "arguments": [{"name": "scenario", "description": "What you feel like doing (optional)",
                            "required": False}]}]}}
    if method == "prompts/get":
        sc = ((msg.get("params") or {}).get("arguments") or {}).get("scenario")
        text = "Let's practise English with Langu-AI. Start a session and begin."
        if sc:
            text += " What I feel like: %s" % sc
        return {"jsonrpc": "2.0", "id": mid, "result": {"messages": [
            {"role": "user", "content": {"type": "text", "text": text}}]}}
    if method in ("resources/list", "resources/templates/list"):
        key = "resources" if method == "resources/list" else "resourceTemplates"
        return {"jsonrpc": "2.0", "id": mid, "result": {key: []}}
    if method == "tools/call":
        p = msg.get("params") or {}
        try:
            res = call_tool(con, p.get("name"), p.get("arguments") or {})
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False)}]}}
        except Exception as e:  # reported to the model so it can correct the call
            sys.stderr.write(traceback.format_exc())
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": "Error: %s" % e}], "isError": True}}
    if mid is None:
        return None  # a notification
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Method not found: %s" % method}}


def main():
    con = db.connect()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        resp = handle(con, msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
