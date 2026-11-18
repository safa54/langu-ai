"""Load the saved English Profile data (data/raw/*.json) into the database.

Run:  python3 -m dilai.importer
Safe to re-run: reference rows are replaced in place, learner state is untouched.
"""
import collections
import json
import os
import re

from . import db

RAW = os.path.join(db.ROOT, "data", "raw")


def _load(name):
    p = os.path.join(RAW, name)
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _split_examples(text):
    if not text:
        return []
    parts = re.split(r";\s+(?=[\"'‘“\[(]?[A-Z0-9])", text.strip())
    return [p.strip() for p in parts if p.strip()]


PLACEHOLDERS = {"sb", "sth", "sb's", "sth's", "swh", "etc.", "etc", "sb/sth", "sth/sb", "sb's/sth's"}


def _cores(base_l):
    """'give up (sth) or give (sth) up' -> ['give up']; 'be on cloud nine' -> [..., 'on cloud nine']."""
    alts = re.split(r"\s+or\s+", base_l)
    if not all(len(a.split()) >= 2 for a in alts):
        alts = [base_l]
    out = []
    for a in alts:
        a = re.sub(r"\([^)]*\)", " ", a)
        toks = [t.strip(",") for t in a.split()]
        toks = [t for t in toks if t and t not in PLACEHOLDERS
                and not ("/" in t and all(p in PLACEHOLDERS for p in t.split("/")))]
        core = " ".join(toks).strip(" ,.")
        for c in (core, core[3:] if core.startswith("be ") and len(toks) > 2 else None):
            if c and c not in out:
                out.append(c)
    return out


def _forms(base, terms, kind):
    base_l = base.lower().strip()
    multi = " " in base_l
    out = [base_l]
    if multi:
        out += [c for c in _cores(base_l) if c not in out]
    for t in (terms or "").split(";"):
        t = t.strip().lower()
        if not t or t in out:
            continue
        if multi != (" " in t):
            continue          # a phrase is not matched by one of its words alone, nor a word by a phrase
        out.append(t)
    return out


def import_vocab(con, entries, variant, lookups):
    if not entries:
        return 0
    phrase_cats = collections.Counter(e.get("l_category_custom_evp_l_category") for e in entries
                                      if e.get("pos_text") == "phrase")
    idiom_cat = min(phrase_cats, key=phrase_cats.get) if len(phrase_cats) > 1 else None
    n = 0
    seen = set()
    con.execute("BEGIN")
    for e in sorted(entries, key=lambda x: x["id_text"]):
        # the same phrase is listed under each of its headwords; keep one copy
        dup = (e.get("base_text"), e.get("guideword_text"), e.get("pos_text"), e.get("definition_text"),
               e.get("cefr_text_text"))
        if dup in seen:
            continue
        seen.add(dup)
        pos = (e.get("pos_text") or "").strip()
        if pos == "phrasal verb":
            kind = "phrasal verb"
        elif pos == "phrase":
            kind = "idiom" if e.get("l_category_custom_evp_l_category") == idiom_cat else "phrase"
        else:
            kind = "word"
        base = (e.get("base_text") or "").strip()
        if not base:
            continue
        level = (e.get("cefr_text_text") or "").strip()
        grammar = ", ".join(lookups[g] for g in e.get("l_grammars_list_custom_evp_l_grammar") or [] if g in lookups)
        usage = lookups.get(e.get("l_usage_custom_evp_l_usage") or "")
        forms = _forms(base, e.get("searchterms_text"), kind)
        vals = (variant, e.get("refid_text"), base, e.get("hw_text"), e.get("guideword_text"), pos, kind,
                level, db.level_n(level), e.get("l_topic_text_text"), e.get("definition_text"),
                json.dumps(_split_examples(e.get("learnerexamples_text")), ensure_ascii=False),
                grammar or None, usage, e.get("ukpron_text") or e.get("uspron_text"),
                ";".join(forms), int(e.get("culture_number") or 0), e["id_text"])
        cur = con.execute(
            "UPDATE vocab SET variant=?, ref_id=?, base=?, headword=?, guideword=?, pos=?, kind=?, level=?, "
            "level_n=?, topics=?, definition=?, examples=?, grammar=?, usage=?, pron=?, forms=?, culture=? "
            "WHERE evp_id=?", vals)
        if cur.rowcount == 0:
            cur = con.execute(
                "INSERT INTO vocab(variant, ref_id, base, headword, guideword, pos, kind, level, level_n, topics, "
                "definition, examples, grammar, usage, pron, forms, culture, evp_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", vals)
            vid = cur.lastrowid
        else:
            vid = con.execute("SELECT id FROM vocab WHERE evp_id=?", (e["id_text"],)).fetchone()[0]
            con.execute("DELETE FROM vocab_form WHERE vocab_id=?", (vid,))
        con.executemany("INSERT OR IGNORE INTO vocab_form(form, vocab_id) VALUES(?, ?)", [(f, vid) for f in forms])
        n += 1
    con.execute("COMMIT")
    return n


def import_grammar(con, entries):
    n = 0
    con.execute("BEGIN")
    for e in entries:
        level = (e.get("level_text") or "").strip()
        gid = e.get("id_text") or e.get("_id")
        vals = (e.get("supercategory_text"), e.get("subcategory_text"), level, db.level_n(level),
                e.get("guideword_text"), e.get("can_dostatement_text"), e.get("example_text"),
                e.get("lexicalrange_text"), gid)
        cur = con.execute("UPDATE grammar SET supercategory=?, subcategory=?, level=?, level_n=?, guideword=?, "
                          "can_do=?, example=?, lexical_range=? WHERE egp_id=?", vals)
        if cur.rowcount == 0:
            con.execute("INSERT INTO grammar(supercategory, subcategory, level, level_n, guideword, can_do, "
                        "example, lexical_range, egp_id) VALUES(?,?,?,?,?,?,?,?,?)", vals)
        n += 1
    con.execute("COMMIT")
    return n


def main():
    con = db.connect()
    lookups = {}
    for l in _load("lookups.json"):
        name = l.get("name_text") or l.get("display_text")
        if name:
            lookups["1348695171700984260__LOOKUP__" + l["_id"]] = name
            lookups[l["_id"]] = name
    for name, variant in (("evp_uk.json", "uk"), ("evp_us.json", "us")):
        print("%s: %d senses" % (name, import_vocab(con, _load(name), variant, lookups)))
    print("egp.json: %d grammar points" % import_grammar(con, _load("egp.json")))
    for r in con.execute("SELECT variant, kind, COUNT(*) n FROM vocab GROUP BY 1, 2 ORDER BY 1, 2"):
        print("  ", r["variant"], r["kind"], r["n"])


if __name__ == "__main__":
    main()
