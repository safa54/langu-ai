"""Langu-AI web app: chat with the AI partner, with the word panel, feedback notes and controls beside it.

Run:  python3 -m dilai.web          then open http://localhost:8787

The chat is driven by the Claude Code command-line program in headless mode, which uses your Claude
subscription login - no API key. The same panel also follows sessions held in Claude Desktop.
"""
import glob
import json
import os
import shutil
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import core, db, prompts

PORT = int(os.environ.get("DILAI_PORT", "8787"))
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
CHAT_CWD = os.path.join(db.ROOT, "data", "chat")


def find_claude():
    p = os.environ.get("DILAI_CLAUDE") or shutil.which("claude")
    if p:
        return p
    cands = glob.glob(os.path.expanduser(
        "~/Library/Application Support/Claude/claude-code/*/*/claude.app/Contents/MacOS/claude"))
    cands += glob.glob(os.path.expanduser("~/.claude/local/claude"))

    def version(path):
        try:
            return [int(x) for x in path.split("/claude-code/")[1].split("/")[0].split(".")]
        except (IndexError, ValueError):
            return [0]
    return sorted(cands, key=version)[-1] if cands else None


# ---------------------------------------------------------------- the AI process, one per session

class Chat(object):
    def __init__(self, session_id):
        self.sid = session_id
        self.proc = None
        self.lock = threading.Lock()
        self.busy = False
        self.partial = ""
        self.activity = ""
        self.error = None
        self.resumed = False      # this process was started with --resume
        self.replied = False      # this process has produced at least one reply
        self.last_text = None
        self.err_hint = None

    def _spawn(self, con):
        exe = find_claude()
        if not exe:
            raise RuntimeError("Claude Code was not found. Install it (https://claude.com/claude-code) or set "
                               "DILAI_CLAUDE to its path.")
        s = core.get_settings(con)
        srow = con.execute("SELECT * FROM session WHERE id=?", (self.sid,)).fetchone()
        guide = prompts.build_guide(s, srow["mode"], srow["scenario"], web=True, setup=core.get_setup(srow))
        mcp = {"mcpServers": {"dilai": {
            "command": sys.executable, "args": ["-m", "dilai.mcp_server"],
            "env": {"DILAI_SESSION": str(self.sid), "PYTHONPATH": db.ROOT, "DILAI_DB": db.DB_PATH}}}}
        cmd = [exe, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
               "--include-partial-messages", "--system-prompt", guide,
               "--mcp-config", json.dumps(mcp), "--strict-mcp-config",
               "--tools", "", "--allowedTools", "mcp__dilai", "--model", s.get("model") or "opus",
               "--effort", s.get("effort") or "medium"]
        if srow["cli_session"]:
            cmd += ["--resume", srow["cli_session"]]
        os.makedirs(CHAT_CWD, exist_ok=True)
        env = dict(os.environ)
        for k in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION"):
            env.pop(k, None)
        self.proc = subprocess.Popen(cmd, cwd=CHAT_CWD, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, bufsize=0)
        threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()
        threading.Thread(target=self._read_err, args=(self.proc,), daemon=True).start()
        self.resumed, self.replied = bool(srow["cli_session"]), False
        return self.resumed

    def send(self, con, text):
        """text goes to the model. Returns immediately; the reply arrives through the reader thread."""
        with self.lock:
            if self.busy:
                raise RuntimeError("Still writing the previous reply.")
            resumed = True
            if self.proc is None or self.proc.poll() is not None:
                resumed = self._spawn(con)
            parts = []
            if not resumed:
                parts.append(first_context(con, self.sid))
            self.last_text = text
            ev = core.pop_panel_events(con, self.sid)
            if ev:
                parts.append("<panel>\n%s\n</panel>" % "\n".join(ev))
            parts.append(text)
            msg = {"type": "user", "message": {"role": "user", "content": [
                {"type": "text", "text": "\n\n".join(parts)}]}}
            self.busy, self.partial, self.activity, self.error = True, "", "thinking", None
            try:
                self.proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
                self.proc.stdin.flush()
            except (IOError, OSError) as e:
                self.busy = False
                self.error = "Could not reach the AI process: %s" % e

    def _read_err(self, proc):
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            low = line.lower()
            if line and "connectors are disabled" not in low and ("error" in low or "log in" in low
                                                                  or "/login" in low):
                self.err_hint = line[:400]      # shown only if the process then fails

    def _read(self, proc):
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                if "/login" in line.lower() or "not logged in" in line.lower():
                    self._fail(line)
                continue
            t = ev.get("type")
            if t == "stream_event":
                e = ev.get("event") or {}
                if e.get("type") == "content_block_start":
                    block = e.get("content_block") or {}
                    if block.get("type") == "tool_use":
                        self.activity = "noting"
                    elif block.get("type") == "text":
                        self.activity = "writing"
                        if self.partial and not self.partial.endswith("\n"):
                            self.partial += "\n\n"
                elif e.get("type") == "content_block_delta":
                    d = e.get("delta") or {}
                    if d.get("type") == "text_delta":
                        self.partial += d.get("text", "")
            elif t == "system" and ev.get("subtype") == "init":
                self._save_cli_session(ev.get("session_id"))
            elif t == "result":
                text = self.partial.strip()
                if ev.get("is_error") and not text:
                    if proc is self.proc and self.resumed and not self.replied and self.last_text is not None:
                        self._restart_fresh()      # the saved conversation could not be reopened
                    elif proc is self.proc:
                        self._fail(str(ev.get("result") or ev.get("subtype") or "The AI returned an error."))
                    continue
                self.replied = True
                self._save_cli_session(ev.get("session_id"))
                if text:
                    con = db.connect()
                    con.execute("INSERT INTO message(session_id, ts, role, text) VALUES(?,?,?,?)",
                                (self.sid, db.now(), "assistant", text))
                    core.touch_session(con, self.sid)
                    con.close()
                self.partial, self.activity, self.busy = "", "", False
        if proc is not self.proc:
            return
        if self.busy and self.resumed and not self.replied and self.last_text is not None:
            self._restart_fresh()
        elif self.busy:
            self._fail(self.err_hint or "The AI process stopped unexpectedly.")

    def _restart_fresh(self):
        """Start a new AI conversation for this session and hand it the stored transcript and story notes."""
        old, text = self.proc, self.last_text
        con = db.connect()
        try:
            con.execute("UPDATE session SET cli_session=NULL WHERE id=?", (self.sid,))
            self.proc, self.busy, self.error = None, False, None
            if old and old.poll() is None:
                old.terminate()
            self.send(con, text)
        except Exception as e:
            self._fail("Could not restart the AI: %s" % e)
        finally:
            con.close()

    def _save_cli_session(self, cli_id):
        if cli_id:
            con = db.connect()
            con.execute("UPDATE session SET cli_session=? WHERE id=?", (cli_id, self.sid))
            con.close()

    def _fail(self, text):
        if "/login" in text.lower() or "not logged in" in text.lower():
            text = ("Claude Code is not logged in. Open a terminal, run:  claude   then type  /login  and sign in "
                    "with your Claude account. After that, send your message again.")
        self.error, self.busy, self.partial, self.activity = text, False, "", ""

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


CHATS = {}
CHATS_LOCK = threading.Lock()


def chat_for(session_id):
    with CHATS_LOCK:
        if session_id not in CHATS:
            CHATS[session_id] = Chat(session_id)
        return CHATS[session_id]


def first_context(con, session_id):
    b = core.session_brief(con, session_id)
    for it in b["target_items"]:
        r = con.execute("SELECT synonyms, tip FROM vocab_extra WHERE vocab_id=?", (it["id"],)).fetchone()
        if not r or not (r["synonyms"] or r["tip"]):
            it["needs_notes"] = True
    past = core.transcript(con, session_id)
    if past and past[-1]["who"] == "learner":
        past = past[:-1]          # the newest learner message is sent as the message itself
    if past:
        b["earlier_conversation"] = past
    return "<session>\n%s\n</session>" % json.dumps(b, ensure_ascii=False)


# ---------------------------------------------------------------- what the page shows

def session_title(r):
    label = {"chat": "Chat", "roleplay": "Role-play", "adventure": "Adventure", "debate": "Debate"}.get(
        r["mode"], r["mode"] or "Session")
    sc = (r["scenario"] or "").strip()
    if not sc:
        setup = core.get_setup(r)
        sc = (setup.get("story") or setup.get("partner") or setup.get("you") or "").strip()
    sc = " ".join(sc.split())
    return r["title"] or (label + (": " + sc[:48] if sc else ""))


def state(con, session_id):
    s = core.get_settings(con)
    out = {"settings": s, "levels": db.LEVELS, "kinds": core.KINDS, "modes": core.MODES,
           "models": core.MODELS, "efforts": core.EFFORTS, "setup_fields": [k for k, _ in core.SETUP_FIELDS]}
    out["sessions"] = [{"id": r["id"], "title": session_title(r), "source": r["source"], "mode": r["mode"],
                        "scenario": r["scenario"], "setup": core.get_setup(r),
                        "started": r["started_at"], "active": r["last_active"]}
                       for r in con.execute("SELECT * FROM session ORDER BY last_active DESC LIMIT 20")]
    srow = con.execute("SELECT * FROM session WHERE id=?", (session_id,)).fetchone() if session_id else None
    if srow is None:
        out["session"] = None
        return out
    fb = {}
    loose = []
    for f in con.execute("SELECT * FROM feedback WHERE session_id=? ORDER BY id", (session_id,)):
        d = {k: f[k] for k in ("id", "original", "corrected", "explanation", "category", "severity", "ts")}
        if f["message_id"]:
            fb.setdefault(f["message_id"], []).append(d)
        else:
            loose.append(d)
    msgs = [{"id": m["id"], "role": m["role"], "text": m["text"], "feedback": fb.get(m["id"], [])}
            for m in con.execute("SELECT * FROM message WHERE session_id=? ORDER BY id", (session_id,))]
    chat = CHATS.get(session_id)
    out["session"] = {
        "id": session_id, "title": session_title(srow), "mode": srow["mode"], "scenario": srow["scenario"],
        "source": srow["source"], "messages": msgs, "loose_feedback": loose,
        "setup": core.get_setup(srow), "notes": core.story_notes(con, session_id),
        "items": core.session_items(con, session_id, s, full=True),
        "grammar": core.session_grammar(con, session_id),
        "busy": bool(chat and chat.busy), "partial": chat.partial if chat else "",
        "activity": chat.activity if chat else "", "error": chat.error if chat else None,
    }
    return out


SETTING_LABEL = {
    "intensity": lambda v: "how hard to push target items is now %s/5: %s" % (v, prompts.INTENSITY_NOTES[v]),
    "feedback": lambda v: "feedback is now '%s': %s" % (v, prompts.FEEDBACK_NOTES[v]),
    "ai_level": lambda v: "your own language level is now '%s': %s" % (v, prompts.AI_LEVEL_NOTES[v]),
    "level": lambda v: "the learner's level is now %s" % v,
    "variant": lambda v: "use %s English" % ("American" if v == "us" else "British"),
    "native_language": lambda v: "the learner's first language is %s" % (v or "not given"),
    "target_levels": lambda v: "new items now come from levels %s" % ", ".join(v),
}


def item_label(row):
    return '"%s"%s (id %d)' % (row["base"], " [%s]" % row["guideword"] if row["guideword"] else "", row["id"])


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}

    def _local(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        origin = self.headers.get("Origin")
        ok_origin = origin is None or urllib.parse.urlparse(origin).hostname in ("localhost", "127.0.0.1")
        return host in ("localhost", "127.0.0.1") and ok_origin

    def do_GET(self):
        if not self._local():
            return self._send(403, {"error": "local use only"})
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        if u.path in ("/", "/index.html"):
            with open(os.path.join(STATIC, "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        con = db.connect()
        try:
            if u.path == "/api/state":
                sid = int(q["session"]) if q.get("session") else None
                if sid is None:
                    row = core.latest_session(con)
                    sid = row["id"] if row else None
                return self._send(200, state(con, sid))
            if u.path == "/api/mywords":
                return self._send(200, {"items": core.my_words(con)})
            if u.path == "/api/stats":
                return self._send(200, core.stats(con))
            if u.path == "/api/vocab":
                return self._send(200, self.vocab(con, q))
            if u.path == "/api/grammar":
                pts = core.pick_grammar(con, None, count=40, levels=[q["level"]] if q.get("level") else None,
                                        query=q.get("q") or None, strategy="new", add=False)
                return self._send(200, {"points": pts})
            return self._send(404, {"error": "not found"})
        except Exception as e:
            return self._send(500, {"error": str(e)})
        finally:
            con.close()

    def vocab(self, con, q):
        s = core.get_settings(con)
        where, args = ["v.variant IN (?, 'custom')"], [s["variant"]]
        if q.get("q"):
            where.append("(v.base LIKE ? OR v.id IN (SELECT vocab_id FROM vocab_form WHERE form = ?))")
            args += [q["q"].strip() + "%", q["q"].strip().lower()]
        if q.get("level"):
            where.append("v.level = ?")
            args.append(q["level"])
        if q.get("kind"):
            where.append("v.kind = ?")
            args.append(q["kind"])
        st = q.get("status")
        if st == "new":
            where.append("s.vocab_id IS NULL AND v.level_n > ?")
            args.append(core._assumed_n(s))
        elif st == "due":
            where.append("s.status IN ('learning','unknown') AND (s.due_at IS NULL OR s.due_at <= ?)")
            args.append(db.now())
        elif st:
            where.append("s.status = ?")
            args.append(st)
        order = "s.last_seen DESC, v.base" if st and st != "new" else "v.base COLLATE NOCASE, v.level_n"
        rows = con.execute(core.VOCAB_JOIN + "WHERE " + " AND ".join(where) + " ORDER BY " + order + " LIMIT 80",
                           args).fetchall()
        return {"items": [core.brief(r, s, full=True) for r in rows]}

    def do_POST(self):
        if not self._local():
            return self._send(403, {"error": "local use only"})
        path = urllib.parse.urlparse(self.path).path
        con = db.connect()
        try:
            b = self._body()
            sid = int(b["session"]) if b.get("session") else None
            if path == "/api/session":
                changes = {k: b[k] for k in ("mode", "scenario") if k in b}
                if changes:
                    core.update_settings(con, changes)
                sid = core.new_session(con, source="web", mode=b.get("mode"), scenario=b.get("scenario"),
                                       title=b.get("title"), setup=b.get("setup"))
                core.begin_session(con, sid)
                core.pop_panel_events(con, sid)
                chat_for(sid).send(con, "(The learner has just opened this session. Open the conversation: set "
                                        "the scene or say hello, in keeping with the mode, the scenario and the "
                                        "learner's setup. Record the starting facts as story_notes.)")
                return self._send(200, {"session": sid})
            if path == "/api/setup":
                srow = con.execute("SELECT * FROM session WHERE id=?", (sid,)).fetchone()
                if srow is None:
                    return self._send(404, {"error": "no such session"})
                before = core.get_setup(srow)
                after = core.set_setup(con, sid, b.get("setup"))
                if after != before:
                    core.push_panel_event(con, sid, "The learner changed the setup. From now on it is:\n%s\n"
                                          "Keep what has already happened; apply the changes going forward."
                                          % (core.setup_text(after) or "(empty: no fixed setup)"))
                return self._send(200, {"setup": after})
            if path == "/api/note":
                if b.get("delete"):
                    row = con.execute("SELECT text FROM story_note WHERE id=? AND session_id=?",
                                      (int(b["delete"]), sid)).fetchone()
                    if row:
                        core.drop_story_note(con, sid, b["delete"])
                        core.push_panel_event(con, sid, "The learner removed this story note; it is not true or "
                                              "no longer applies: %s" % row["text"])
                elif (b.get("text") or "").strip():
                    nid = core.add_story_note(con, sid, b["text"], "user")
                    if nid:
                        core.push_panel_event(con, sid, "The learner added a story note that must hold from now "
                                              "on (note %d): %s" % (nid, b["text"].strip()))
                return self._send(200, {"ok": True})
            if path == "/api/send":
                text = (b.get("text") or "").strip()
                if not text or not sid:
                    return self._send(400, {"error": "nothing to send"})
                chat = chat_for(sid)
                if chat.busy:
                    return self._send(409, {"error": "Still writing the previous reply."})
                con.execute("INSERT INTO message(session_id, ts, role, text) VALUES(?,?,?,?)",
                            (sid, db.now(), "user", text))
                core.touch_session(con, sid)
                if b.get("spoken"):
                    core.push_panel_event(con, sid, prompts.SPOKEN_NOTE)
                chat.send(con, text)
                return self._send(200, {"ok": True})
            if path == "/api/item":
                row = core.get_vocab(con, int(b["id"]))
                if row is None:
                    return self._send(404, {"error": "no such item"})
                action = b.get("action")
                if action == "lookup":
                    core.note_lookup(con, sid, row["id"])
                elif action == "target":
                    core.add_session_item(con, sid, row["id"], "target")
                    if sid:
                        con.execute("UPDATE session_item SET state='proposed' WHERE session_id=? AND vocab_id=?",
                                    (sid, row["id"]))
                        core.push_panel_event(con, sid, "The learner added %s to this session's targets: %s"
                                              % (item_label(row), row["definition"]))
                else:
                    core.set_status(con, row["id"], action, "user", sid)
                    said = {"known": "already knows %s well - stop targeting it",
                            "unknown": "does not know %s - let them meet it in a clear context soon",
                            "skip": "does not want %s in this session - drop it",
                            "ignored": "never wants to practise %s - drop it",
                            "learning": "wants %s back in practice"}[action]
                    if sid:
                        core.push_panel_event(con, sid, "The learner " + said % item_label(row))
                return self._send(200, {"ok": True})
            if path == "/api/more":
                items = core.pick_words(con, sid, count=int(b.get("count") or 3),
                                        strategy=b.get("strategy") or "mixed")
                if items and sid:
                    core.push_panel_event(con, sid, "The learner asked for more items. New targets: %s"
                                          % json.dumps(items, ensure_ascii=False))
                return self._send(200, {"added": len(items)})
            if path == "/api/settings":
                before = core.get_settings(con)
                after = core.update_settings(con, b.get("changes") or {})
                notes = [SETTING_LABEL[k](after[k]) for k in SETTING_LABEL if before[k] != after[k]]
                if notes:
                    for r in con.execute("SELECT id FROM session WHERE last_active >= datetime('now','localtime',"
                                         "'-6 hours')"):
                        core.push_panel_event(con, r["id"], "Settings changed: " + "; ".join(notes))
                if before["model"] != after["model"] or before["effort"] != after["effort"]:
                    for c in list(CHATS.values()):     # idle chats reopen with the new model, same conversation
                        if not c.busy:
                            c.stop()
                return self._send(200, {"settings": after})
            if path == "/api/stop":
                chat = CHATS.get(sid)
                if chat:
                    chat.stop()
                return self._send(200, {"ok": True})
            return self._send(404, {"error": "not found"})
        except (ValueError, KeyError) as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:
            return self._send(500, {"error": str(e)})
        finally:
            con.close()


def main():
    db.connect().close()
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        sys.exit("Langu-AI is already running at http://localhost:%d (or another program uses that port).\n"
                 "Open that address, or stop the other copy with:  pkill -f run.py" % PORT)
    srv.daemon_threads = True
    print("Langu-AI is running: http://localhost:%d" % PORT)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for c in CHATS.values():
            c.stop()


if __name__ == "__main__":
    main()
