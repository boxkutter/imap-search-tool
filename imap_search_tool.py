#!/usr/bin/env python3
"""Local IMAP search tool with iterative refinement. Standard library only.

Workflow
  1. Run a broad search on the mail server (e.g. Gmail query "971").
     The matching emails are downloaded into memory (read-only).
  2. Refine that candidate set as many times as you like with local filters
     (must contain all / any / none, whole word, match case, near / not near,
     sender and subject rules, regex, number starts/ends/contains ...).
     Each step works on the result of the previous one and needs no login.
  3. Undo a step, or start over, at any time.

The app password is kept in server memory for the browser session so repeat searches
don't need it again (use "Forget password" to drop it; it is also dropped after 2 idle
hours or when the app stops). Nothing is written to disk.
"""

from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote
from email import message_from_bytes
from email.header import decode_header, make_header
from email.policy import default
from email.utils import parsedate_to_datetime
from html import escape
from html.parser import HTMLParser
import imaplib
import re
import secrets
import socket
import sys
import threading
import time
from datetime import date

HOST = "127.0.0.1"
PORT = 8765
DEFAULT_IMAP_HOST = "imap.gmail.com"
DEFAULT_IMAP_PORT = 993
DEFAULT_FOLDER = "[Gmail]/All Mail"
DEFAULT_LIMIT = 1500
MAX_LIMIT = 5000
FETCH_BATCH = 20
MAX_BODY_CHARS = 500_000          # per message, to bound memory use
MAX_DISPLAY = 200                 # results rendered per page
SESSION_TTL = 2 * 60 * 60         # idle seconds before a session is dropped

# A "number" for the number-aware filters: digits separated by spaces, dots,
# dashes, slashes or brackets, with an optional leading +.
NUMBER_RE = re.compile(r"\+?\d(?:[ \t ()./\-]{0,3}\d)+")

MODES = {
    "contains":     "Text contains",
    "regex":        "Regular expression",
    "num_starts":   "Number starts with",
    "num_ends":     "Number ends with",
    "num_contains": "Number contains",
}
SCOPES = {
    "all":     "Subject, sender and body",
    "subject": "Subject only",
    "sender":  "Sender only",
    "body":    "Body only",
}


# ---------------------------------------------------------------- parsing --

class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip = 0
    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1
    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)
    def text(self):
        return " ".join(self.parts)


def decode_header_value(value):
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def part_text(part):
    try:
        payload = part.get_content()
    except Exception:
        raw = part.get_payload(decode=True) or b""
        payload = raw.decode(part.get_content_charset() or "utf-8", "replace")
    if not isinstance(payload, str):
        return ""
    if part.get_content_subtype() == "html":
        parser = TextExtractor()
        parser.feed(payload)
        payload = parser.text()
    return payload


def get_message_text(msg):
    chunks = []
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        if part.get_content_maintype() != "text":
            continue
        if part.get_content_disposition() == "attachment":
            continue
        chunks.append(part_text(part))
    return "\n".join(chunks)[:MAX_BODY_CHARS]


def format_date(value):
    if not value:
        return ""
    try:
        return parsedate_to_datetime(value).astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return value


def parse_message(uid, raw_bytes):
    msg = message_from_bytes(raw_bytes, policy=default)
    message_id = str(msg.get("Message-ID", "")).strip().strip("<>")
    return {
        "uid": uid,
        "subject": decode_header_value(str(msg.get("Subject", ""))) or "",
        "sender": decode_header_value(str(msg.get("From", ""))),
        "date": format_date(str(msg.get("Date", ""))),
        "body": get_message_text(msg),
        "url": ("https://mail.google.com/mail/u/0/#search/"
                + quote("rfc822msgid:" + message_id, safe="")) if message_id else "",
    }


# ------------------------------------------------------------------- IMAP --

def connect(host, port):
    return imaplib.IMAP4_SSL(host, port, timeout=120)


def imap_quote(value):
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def imap_date(d):
    return f"{d.day}-{MONTHS[d.month - 1]}-{d.year}"      # locale-independent


def parse_date(text, label):
    text = text.strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{label} must be a date like 2025-01-31.")


def gmail_raw(query, exclude, from_, subject):
    """Combine the structured fields into one Gmail search string."""
    parts = [query.strip()] if query.strip() else []
    parts += ['-"' + t.replace('"', "") + '"' for t in exclude]
    if from_:
        parts.append("from:(" + from_.replace("(", "").replace(")", "") + ")")
    if subject:
        parts.append("subject:(" + subject.replace("(", "").replace(")", "") + ")")
    return " ".join(parts)


def server_search(conn, syntax, query, since=None, before=None, exclude=(), from_="", subject=""):
    """Return a list of UIDs (as bytes) matching the search and date range.
    `since` is inclusive, `before` is exclusive (IMAP SINCE / BEFORE semantics)."""
    query = query.strip()
    args = []
    if since:
        args += ["SINCE", imap_date(since)]
    if before:
        args += ["BEFORE", imap_date(before)]
    if syntax == "gmail":
        raw = gmail_raw(query, exclude, from_, subject)
        tail = ("X-GM-RAW", raw) if raw else None
    else:
        extras = [("NOT TEXT", t) for t in exclude] + ([("FROM", from_)] if from_ else []) \
                 + ([("SUBJECT", subject)] if subject else [])
        if any(not v.isascii() for _, v in extras):
            raise ValueError("With plain IMAP search, the exclude / from / subject fields must be ASCII. "
                             "Use Gmail syntax, or apply them as filters after downloading.")
        for key, val in extras:
            args += key.split() + [imap_quote(val)]
        tail = ("TEXT", query) if query else None
    charset = []
    if tail:
        key, val = tail
        if val.isascii():
            args += [key, imap_quote(val)]
        else:
            # Non-ASCII search strings must be sent as a UTF-8 literal. The literal
            # attaches to the last argument, so the text key must come last.
            conn.literal = val.encode("utf-8")
            args += [key]
            charset = ["CHARSET", "UTF-8"]
    typ, data = conn.uid("SEARCH", *charset, *(args or ["ALL"]))
    if typ != "OK":
        raise RuntimeError("The server rejected the search: " + " ".join(
            d.decode(errors="replace") if isinstance(d, bytes) else str(d) for d in (data or [])))
    return data[0].split() if data and data[0] else []


def fetch_batch(conn, uids):
    """Yield (uid, raw_bytes) for each UID in the batch."""
    typ, data = conn.uid("FETCH", b",".join(uids), "(UID BODY.PEEK[])")
    if typ != "OK":
        return
    pending = None
    for item in data:
        if isinstance(item, tuple):
            m = re.search(rb"UID (\d+)", item[0])
            if m:
                yield m.group(1), item[1]
                pending = None
            else:
                pending = item[1]       # UID follows the literal
        elif isinstance(item, bytes) and pending is not None:
            m = re.search(rb"UID (\d+)", item)
            if m:
                yield m.group(1), pending
            pending = None


def decode_folder(name):
    """Decode IMAP modified UTF-7 for display."""
    def repl(m):
        if not m.group(1):
            return "&"
        return ("+" + m.group(1).replace(",", "/") + "-").encode("ascii").decode("utf-7")
    try:
        return re.sub(r"&([^-]*)-", repl, name)
    except Exception:
        return name


def list_folders(conn):
    """Return [(flags, raw_name)] for every selectable folder."""
    typ, data = conn.list()
    folders = []
    for line in (data or []):
        if not isinstance(line, bytes):
            continue
        m = re.match(rb'\((.*?)\) (?:"(?:[^"\\]|\\.)*"|NIL) (.*)$', line)
        if not m:
            continue
        flags = m.group(1).decode(errors="replace")
        name = m.group(2).decode(errors="replace").strip()
        if name.startswith('"') and name.endswith('"'):
            name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        if "\\Noselect" not in flags:
            folders.append((flags, name))
    return folders


def count_from(select_data):
    try:
        return int(select_data[0])
    except (TypeError, ValueError, IndexError):
        return None


def open_folder(conn, folder):
    """Select `folder` read-only. If the Gmail default isn't found, fall back to the
    account's \\All folder (its name is localised), else explain what is available."""
    status, data = conn.select(imap_quote(folder), readonly=True)
    if status == "OK":
        return folder, count_from(data)
    folders = list_folders(conn)
    if folder == DEFAULT_FOLDER:
        for flags, name in folders:
            if "\\All" in flags:
                status, data = conn.select(imap_quote(name), readonly=True)
                if status == "OK":
                    return name, count_from(data)
    if folders:
        names = ", ".join(f"{decode_folder(n)}" + (f" [type: {n}]" if decode_folder(n) != n else "") for _, n in folders)
        raise RuntimeError(f"Could not open folder “{folder}”. Folders available on this account: {names}. "
                           "Enter one of these under Server settings → Folder.")
    raise RuntimeError(f"Could not open folder “{folder}”, and the server listed no folders. "
                       "Check that IMAP is enabled in Gmail (Settings → Forwarding and POP/IMAP).")


def test_connection(host, port, email_address, password, folder):
    """Log in and open the folder without downloading anything. Returns a summary."""
    conn = connect(host, port)
    try:
        conn.login(email_address, password)
        name, count = open_folder(conn, folder)
        found = f"{count} email(s)" if count is not None else "folder opened"
        used = "" if name == folder else f" (using “{decode_folder(name)}” instead of “{folder}”)"
        return f"Connected to {host} as {email_address}. Folder “{decode_folder(name)}”: {found}{used}."
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def download(host, port, email_address, password, folder, syntax, query, limit, since=None, before=None,
             exclude=(), from_="", subject=""):
    """Log in, run the server-side search and download the newest `limit` hits."""
    conn = connect(host, port)
    try:
        conn.login(email_address, password)
        open_folder(conn, folder)
        uids = server_search(conn, syntax, query, since, before, exclude, from_, subject)
        total = len(uids)
        uids = sorted(uids, key=int, reverse=True)[:limit]
        msgs = {}
        for i in range(0, len(uids), FETCH_BATCH):
            for uid, raw in fetch_batch(conn, uids[i:i + FETCH_BATCH]):
                try:
                    msgs[uid] = parse_message(uid, raw)
                except Exception:
                    continue
        order = [u for u in uids if u in msgs]
        return msgs, order, total
    finally:
        try:
            conn.logout()
        except Exception:
            pass


# -------------------------------------------------------------- filtering --

def collapse(text):
    return re.sub(r"\s+", " ", text)


def summarize(text, spans, numbers=False, ctx=60, limit=5):
    """Turn match spans into highlighted context snippets.
    Matches close together share one snippet; returns count, snippets, number hidden."""
    seen, uniq = set(), []
    for s, e in sorted(set(spans)):
        if numbers:
            key = re.sub(r"\D", "", text[s:e])
            if key in seen:
                continue
            seen.add(key)
        uniq.append((s, e))
    clusters = []
    for s, e in uniq:
        if clusters and s - clusters[-1][-1][1] <= 2 * ctx:
            clusters[-1].append((s, e))
        else:
            clusters.append([(s, e)])
    snips = []
    for cluster in clusters[:limit]:
        a, b = max(0, cluster[0][0] - ctx), min(len(text), cluster[-1][1] + ctx)
        out, pos = ["…" if a > 0 else ""], a
        for s, e in cluster:
            s = max(s, pos)
            if e <= s:
                continue
            mid = text[s:e]
            out.append(escape(collapse(text[pos:s])))
            out.append("<mark>" + escape(collapse(mid[:120])) + ("…" if len(mid) > 120 else "") + "</mark>")
            pos = e
        out.append(escape(collapse(text[pos:b])))
        out.append("…" if b < len(text) else "")
        snips.append("".join(out))
    return {"count": len(uniq), "snips": snips, "more": max(0, len(clusters) - limit)}


def build_matcher(mode, text, ignore_case, whole_word, min_digits, max_digits):
    """Return f(string) -> list of (start, end) matches for one search line."""
    flags = re.IGNORECASE if ignore_case else 0
    if mode in ("contains", "regex"):
        pattern = re.escape(text) if mode == "contains" else text
        if whole_word:
            pattern = r"(?<!\w)(?:" + pattern + r")(?!\w)"
        try:
            rx = re.compile(pattern, flags)
        except re.error as e:
            raise ValueError(f"Invalid regular expression “{text}”: {e}")
        return lambda s: [(m.start(), m.end()) for m in rx.finditer(s) if m.end() > m.start()]

    query_digits = re.sub(r"\D", "", text)
    if not query_digits:
        raise ValueError(f"“{text}” has no digits. Number modes need digits (a leading + is allowed).")
    need_plus = text.strip().startswith("+") and mode == "num_starts"
    test = {
        "num_starts": str.startswith,
        "num_ends": str.endswith,
        "num_contains": lambda d, q: q in d,
    }[mode]

    def match(s):
        found = []
        for m in NUMBER_RE.finditer(s):
            raw = m.group(0)
            digits = re.sub(r"\D", "", raw)
            if not min_digits <= len(digits) <= max_digits:
                continue
            if need_plus and not raw.startswith("+"):
                continue
            if test(digits, query_digits):
                found.append((m.start(), m.end()))
        return found
    return match


def message_text(m, scope):
    if scope == "subject":
        return m["subject"]
    if scope == "sender":
        return m["sender"]
    if scope == "body":
        return m["body"]
    return "\n".join([m["subject"], m["sender"], m["body"]])


REFINE_DEFAULTS = {
    "mode": "contains", "scope": "all",
    "all_terms": "", "any_terms": "", "not_terms": "",
    "near_terms": "", "notnear_terms": "", "distance": 60,
    "whole_word": False, "match_case": False, "min_count": 1,
    "from_in": "", "from_out": "", "subj_in": "", "subj_out": "",
    "min_digits": 7, "max_digits": 15,
}
LIST_FIELDS = ("all_terms", "any_terms", "not_terms", "near_terms", "notnear_terms",
               "from_in", "from_out", "subj_in", "subj_out")


def split_lines(raw):
    return [line.strip() for line in raw.splitlines() if line.strip()]


def compile_rules(p):
    """Compile a rule set. `p` holds raw text for list fields (one entry per line)."""
    ic = not p["match_case"]
    def matchers(raw, mode=p["mode"]):
        return [build_matcher(mode, t, ic, p["whole_word"], p["min_digits"], p["max_digits"]) for t in split_lines(raw)]
    near_mode = p["mode"] if p["mode"] in ("contains", "regex") else "contains"
    low = lambda raw: [t.lower() for t in split_lines(raw)]
    return {
        "scope": p["scope"], "numbers": p["mode"].startswith("num_"),
        "all": matchers(p["all_terms"]), "any": matchers(p["any_terms"]), "not": matchers(p["not_terms"]),
        "near": matchers(p["near_terms"], near_mode), "notnear": matchers(p["notnear_terms"], near_mode),
        "dist": p["distance"], "min_count": p["min_count"],
        "from_in": low(p["from_in"]), "from_out": low(p["from_out"]),
        "subj_in": low(p["subj_in"]), "subj_out": low(p["subj_out"]),
    }


def run_rules(r, m):
    """Apply compiled rules to one message. Returns (spans, text) or None if it is rejected."""
    sender, subject = m["sender"].lower(), m["subject"].lower()
    if r["from_in"] and not any(t in sender for t in r["from_in"]):
        return None
    if any(t in sender for t in r["from_out"]):
        return None
    if r["subj_in"] and not any(t in subject for t in r["subj_in"]):
        return None
    if any(t in subject for t in r["subj_out"]):
        return None
    text = message_text(m, r["scope"])
    if any(f(text) for f in r["not"]):
        return None

    def prune(spans):
        if not (r["near"] or r["notnear"]):
            return spans
        keep = []
        for s, e in spans:
            around = (text[max(0, s - r["dist"]):s], text[e:e + r["dist"]])
            near = lambda ms: any(f(w) for f in ms for w in around)
            if r["near"] and not near(r["near"]):
                continue
            if r["notnear"] and near(r["notnear"]):
                continue
            keep.append((s, e))
        return keep

    hits = []
    for f in r["all"]:
        found = prune(f(text))
        if not found:
            return None
        hits += found
    if r["any"]:
        found = [h for f in r["any"] for h in prune(f(text))]
        if not found:
            return None
        hits += found
    if (r["all"] or r["any"]) and len(set(hits)) < r["min_count"]:
        return None
    return hits, text


def describe(p):
    quote = lambda raw: ", ".join(f"“{t}”" for t in split_lines(raw))
    parts = []
    for key, name in (("all_terms", "ALL of"), ("any_terms", "ANY of"), ("not_terms", "NOT")):
        if split_lines(p[key]):
            parts.append(f"{name} {quote(p[key])}")
    if split_lines(p["near_terms"]):
        parts.append(f"near {quote(p['near_terms'])} (±{p['distance']} chars)")
    if split_lines(p["notnear_terms"]):
        parts.append(f"not near {quote(p['notnear_terms'])} (±{p['distance']} chars)")
    for key, name in (("from_in", "sender has"), ("from_out", "sender lacks"),
                      ("subj_in", "subject has"), ("subj_out", "subject lacks")):
        if split_lines(p[key]):
            parts.append(f"{name} {quote(p[key])}")
    opts = [o for o, on in (("whole word", p["whole_word"]), ("match case", p["match_case"]),
                            (f"≥{p['min_count']} times", p["min_count"] > 1)) if on]
    return (f"{SCOPES[p['scope']]} · {MODES[p['mode']].lower()}: " + " · ".join(parts)
            + (f" [{', '.join(opts)}]" if opts else ""))


def apply_filter(session, p):
    rules = compile_rules(p)
    if not any(split_lines(p[k]) for k in LIST_FIELDS if k not in ("near_terms", "notnear_terms")):
        raise ValueError("Enter at least one condition (must contain, must not contain, sender or subject).")
    kept, hits = [], {}
    for uid in session["steps"][-1]["uids"]:
        res = run_rules(rules, session["msgs"][uid])
        if res is None:
            continue
        kept.append(uid)
        spans, text = res
        if spans:
            hits[uid] = summarize(text, spans, rules["numbers"])
    session["steps"].append({"label": describe(p), "uids": kept, "hits": hits,
                             "expects_hits": bool(rules["all"] or rules["any"])})


def highlight_terms(query, syntax):
    """Words/phrases from the server search that should be highlighted in the results."""
    query = query.strip()
    if not query:
        return []
    if syntax != "gmail":
        return [query]
    terms = []
    for tok in re.findall(r'[^\s"]*"[^"]*"[^\s"]*|\S+', query):
        if tok.startswith("-"):
            continue
        tok = tok.strip("(){}")
        if not tok or tok.upper() in ("OR", "AND", "AROUND") or re.match(r"^\w+:", tok):
            continue
        tok = tok.strip('"').strip()
        if tok and tok.lower() not in [t.lower() for t in terms]:
            terms.append(tok)
    return terms


def initial_hits(msgs, order, query, syntax):
    """Find the uses of the search words in the downloaded emails (case-insensitive,
    whole-word for Gmail searches, like the server does). Returns (hits, terms)."""
    terms = highlight_terms(query, syntax)
    if not terms:
        return {}, []
    p = {**REFINE_DEFAULTS, "any_terms": "\n".join(terms), "whole_word": syntax == "gmail"}
    rules = compile_rules(p)
    hits = {}
    for uid in order:
        res = run_rules(rules, msgs[uid])
        if res:
            hits[uid] = summarize(res[1], res[0])
    return hits, terms


# --------------------------------------------------------------- sessions --

SESSIONS = {}
SESSIONS_LOCK = threading.Lock()


def get_session(sid, create=False):
    now = time.time()
    with SESSIONS_LOCK:
        for k in [k for k, v in SESSIONS.items() if now - v["touched"] > SESSION_TTL]:
            del SESSIONS[k]
        s = SESSIONS.get(sid)
        if s is None and create:
            sid = secrets.token_urlsafe(24)
            s = SESSIONS[sid] = {"sid": sid, "touched": now, "lock": threading.Lock(),
                                 "msgs": {}, "steps": [], "email": "", "password": None}
        if s:
            s["touched"] = now
        return s


# ------------------------------------------------------------------- HTML --

CSS = """
:root { color-scheme: light dark; font-family: system-ui, sans-serif; }
body { max-width: 940px; margin: 36px auto; padding: 0 18px; line-height: 1.5; }
h1 { margin-bottom: .2rem; }
h2 { margin: 0 0 .6rem; font-size: 1.15rem; }
.note { color: #777; font-size: .94rem; }
.panel { border: 1px solid #8886; border-radius: 12px; padding: 16px 20px; margin: 20px 0; }
label { display:block; font-weight:600; margin: 12px 0 4px; font-size:.93rem; }
input[type=text], input[type=email], input[type=password], input[type=number], select {
  box-sizing:border-box; width:100%; max-width:460px; padding:9px; border:1px solid #8888;
  border-radius:7px; font:inherit; }
input[type=number] { max-width:110px; }
input[type=date] { box-sizing:border-box; padding:8px; border:1px solid #8888; border-radius:7px; font:inherit; }
.row { display:flex; gap:20px; flex-wrap:wrap; }
.check { display:inline-block; font-weight:400; margin-right:18px; }
button { margin-top:16px; padding:9px 16px; border:0; border-radius:7px; font:inherit; font-weight:650; cursor:pointer; }
button.secondary { background:#8883; }
.inline { display:inline; }
.inline button { margin:0 8px 0 0; }
.warning { background:#b8860b18; border-left:4px solid #b8860b; padding:10px 14px; border-radius:4px; font-size:.93rem; }
.error { color:#c33; font-weight:600; }
.ok { color:#2a8a3e; font-weight:600; }
form button + button { margin-left:8px; }
ol.steps { margin:.3rem 0 .8rem; padding-left:1.4rem; }
ol.steps li.last { font-weight:700; }
.result { border:1px solid #8886; border-radius:10px; padding:12px 15px; margin:10px 0; }
.snip { font-family: ui-monospace, monospace; font-size:.88rem; margin:3px 0; overflow-wrap:anywhere; }
mark { background:#ffd54a; color:#000; padding:0 2px; border-radius:3px; font-weight:700; }
.meta { color:#777; font-size:.9rem; overflow-wrap:anywhere; }
textarea { box-sizing:border-box; width:100%; padding:8px; border:1px solid #8888; border-radius:7px; font:inherit; resize:vertical; }
.grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(220px, 1fr)); gap:14px; }
.hint { font-weight:400; color:#777; font-size:.85rem; }
.nomatch { color:#a60; font-size:.88rem; margin:3px 0; }
.count { color:#2a8a3e; font-size:.88rem; font-weight:600; }
details { margin-top:10px; }
summary { cursor:pointer; font-weight:600; }
a { overflow-wrap:anywhere; }
footer { margin-top:30px; color:#777; font-size:.88rem; }
"""


def options(mapping, selected):
    return "".join(f'<option value="{k}"{" selected" if k == selected else ""}>{escape(v)}</option>'
                   for k, v in mapping.items())


def search_form(v, open_details=True):
    """Form for a new server-side search (needs credentials)."""
    return f"""
<form method="post" action="/search">
<div class="row">
 <div style="flex:1;min-width:240px"><label for="email">Email address</label>
 <input id="email" name="email" type="email" required placeholder="you@gmail.com" value="{escape(v['email'])}"></div>
 <div style="flex:1;min-width:240px"><label for="password">App password</label>
 <input id="password" name="password" type="password"{'' if v['saved_pw'] else ' required'} autocomplete="off" placeholder="{'saved for this session: leave blank to reuse' if v['saved_pw'] else '16-character app password'}"></div>
</div>
<label for="query">Search for</label>
<input id="query" name="query" type="text" value="{escape(v['query'])}" placeholder='e.g.  Du   or   "exact phrase"   or   invoice OR bill   (leave empty for everything)'>
<p class="note" style="margin:4px 0 0">Gmail's server search ignores upper/lower case and punctuation, so <em>Du</em> also finds <em>du</em>.
Download first, then use the filters in the next step (whole word, match case, near / not near …) for precision.
Each use of your search words is highlighted in the results.</p>
<div class="row">
 <div style="flex:1;min-width:200px"><label for="from_">Sender contains</label>
 <input id="from_" name="from_" type="text" value="{escape(v['from_'])}" placeholder="optional, e.g. du.ae"></div>
 <div style="flex:1;min-width:200px"><label for="subject">Subject contains</label>
 <input id="subject" name="subject" type="text" value="{escape(v['subject'])}" placeholder="optional, e.g. invoice"></div>
</div>
<label for="exclude">Exclude emails containing <span class="hint">(one word or phrase per line)</span></label>
<textarea id="exclude" name="exclude" rows="2" style="max-width:460px">{escape(v['exclude'])}</textarea>
<div class="row">
 <div><label for="since">On or after date</label>
 <input id="since" name="since" type="date" value="{escape(v['since'])}" style="max-width:170px"></div>
 <div><label for="before">Before date</label>
 <input id="before" name="before" type="date" value="{escape(v['before'])}" style="max-width:170px"></div>
 <div><label for="limit">Max emails to download (newest first)</label>
 <input id="limit" name="limit" type="number" min="1" max="{MAX_LIMIT}" value="{v['limit']}" required></div>
</div>
<details{' open' if open_details else ''}><summary>Server settings</summary>
<div class="row">
 <div><label for="host">IMAP host</label><input id="host" name="host" type="text" value="{escape(v['host'])}"></div>
 <div><label for="port">Port (SSL)</label><input id="port" name="port" type="number" value="{v['port']}"></div>
</div>
<label for="folder">Folder</label><input id="folder" name="folder" type="text" value="{escape(v['folder'])}">
<label for="syntax">Search syntax</label>
<select id="syntax" name="syntax">
 <option value="gmail"{' selected' if v['syntax']=='gmail' else ''}>Gmail search syntax (from:, after:, quotes … )</option>
 <option value="text"{' selected' if v['syntax']=='text' else ''}>Plain IMAP TEXT search (any IMAP server)</option>
</select>
</details>
<button type="submit">Search mailbox</button>
<button type="submit" formaction="/test" class="secondary" title="Log in and open the folder without downloading anything">Test connection</button>
</form>"""


def ta(name, label, value, rows=3):
    return (f'<div><label for="{name}">{label}</label>'
            f'<textarea id="{name}" name="{name}" rows="{rows}">{escape(value)}</textarea></div>')


TIPS = """
<details><summary>Tips &amp; examples</summary>
<ul class="note">
<li><strong>A common word used as a name</strong> (e.g. <em>Du</em>): put <code>Du</code> under <em>ALL of</em>, tick <em>Whole word</em> and <em>Match case</em>.
 Then open <em>Context</em> and list words that go with the company (<code>bill</code>, <code>account</code>) under <em>near</em>,
 and words that signal the other meaning under <em>not near</em>.</li>
<li><strong>Several spellings or synonyms:</strong> one per line under <em>AT LEAST ONE of</em>.</li>
<li><strong>Cut out noise:</strong> list newsletter words under <em>Must NOT contain</em>, or use <em>Sender / subject</em> rules.</li>
<li><strong>Mentioned more than once:</strong> set <em>appears at least</em> to 2 or more.</li>
<li><strong>Regular expression</strong> mode treats every line as a regex, e.g. <code>\bDu\b(?! [a-z])</code> is <em>Du</em> not followed by a lowercase word (use with <em>Match case</em>).</li>
<li><strong>Number</strong> modes read digit sequences like <code>+971 50 123 4505</code>, ignoring spaces, dashes and brackets; the length range applies only to them, and <em>Whole word</em> is ignored.</li>
<li>All lines in a box are combined as stated in its label; separate boxes are combined with AND.</li>
</ul></details>"""


def refine_form(v):
    r = {**REFINE_DEFAULTS, **v["refine"]}
    chk = lambda k: " checked" if r[k] else ""
    num = lambda k, w=80: f'<input type="number" name="{k}" value="{r[k]}" min="1" style="max-width:{w}px">'
    op = lambda *keys: " open" if any(r[k] for k in keys) else ""
    return f"""
<form method="post" action="/refine">
<div class="row">
 <div><label for="mode">Match type</label><select id="mode" name="mode">{options(MODES, r['mode'])}</select></div>
 <div><label for="scope">Look in</label><select id="scope" name="scope">{options(SCOPES, r['scope'])}</select></div>
</div>
<div class="grid">
 {ta('all_terms', 'Must contain ALL of <span class="hint">(one per line)</span>', r['all_terms'])}
 {ta('any_terms', 'Must contain AT LEAST ONE of', r['any_terms'])}
 {ta('not_terms', 'Must NOT contain ANY of', r['not_terms'])}
</div>
<p style="margin:12px 0 0">
 <label class="check"><input type="checkbox" name="whole_word" value="1"{chk('whole_word')}> Whole word only</label>
 <label class="check"><input type="checkbox" name="match_case" value="1"{chk('match_case')}> Match case (Du ≠ du)</label>
 <label class="check">Appears at least {num('min_count', 60)} time(s)</label>
</p>
<details{op('near_terms', 'notnear_terms')}><summary>Context: near / not near</summary>
<p class="note" style="margin:6px 0">Keep a match only if it is close to certain words, or not close to others.</p>
<div class="grid">
 {ta('near_terms', 'Match must be near ANY of', r['near_terms'], 2)}
 {ta('notnear_terms', 'Match must NOT be near ANY of', r['notnear_terms'], 2)}
</div>
<label class="check" style="margin-top:8px">“Near” means within {num('distance', 80)} characters before or after</label>
</details>
<details{op('from_in', 'from_out', 'subj_in', 'subj_out')}><summary>Sender &amp; subject rules</summary>
<div class="grid">
 {ta('from_in', 'Sender contains ANY of', r['from_in'], 2)}
 {ta('from_out', 'Sender contains NONE of', r['from_out'], 2)}
 {ta('subj_in', 'Subject contains ANY of', r['subj_in'], 2)}
 {ta('subj_out', 'Subject contains NONE of', r['subj_out'], 2)}
</div>
</details>
<details><summary>Number options</summary>
<label class="check" style="margin-top:8px">Number length (digits) {num('min_digits', 70)} to {num('max_digits', 70)}</label>
</details>
{TIPS}
<button type="submit">Filter current results</button>
</form>"""


def render_results(session):
    last = session["steps"][-1]
    hits = last["hits"]
    out = []
    uids = last["uids"]
    if not uids:
        out.append("<p><strong>No emails left in the candidate set.</strong> Use “Undo last step” and try a different filter.</p>")
    elif last.get("expects_hits"):
        without = sum(1 for u in uids if u not in hits)
        if without:
            out.append(f'<p class="note">{len(uids) - without} of {len(uids)} emails show a use of the search words in their readable text '
                       f'(listed first). The other {without} matched on the server only, for example through an attachment, '
                       f'an HTML detail or fuzzy matching, so no highlighted use can be shown. '
                       '<form class="inline" method="post" action="/keephits"><button class="secondary" style="margin:0">'
                       'Keep only emails with a visible match</button></form></p>')
    for uid in uids[:MAX_DISPLAY]:
        m = session["msgs"][uid]
        h = hits.get(uid)
        detail = ""
        if h:
            detail = f'<div class="count">{h["count"]} match{"es" if h["count"] != 1 else ""}</div>'
            detail += "".join(f'<div class="snip">{x}</div>' for x in h["snips"])
            if h["more"]:
                detail += f'<div class="meta">… and {h["more"]} more place(s) in this email</div>'
        elif last.get("expects_hits"):
            detail = '<div class="nomatch">No use of the search words found in the subject, sender or body text.</div>'
        link = f'<div><a href="{escape(m["url"])}" target="_blank" rel="noopener">Find in Gmail</a></div>' if m["url"] else ""
        out.append(f'<section class="result"><div><strong>{escape(m["subject"] or "(no subject)")}</strong></div>'
                   f'<div class="meta">{escape(m["sender"])} · {escape(m["date"])}</div>{detail}{link}</section>')
    if len(uids) > MAX_DISPLAY:
        out.append(f'<p class="note">Showing the first {MAX_DISPLAY} of {len(uids)}. Add another filter to narrow further.</p>')
    return "".join(out)


def render_page(session=None, message="", values=None):
    v = {"email": "", "query": "", "since": "", "before": "", "exclude": "", "from_": "", "subject": "",
         "limit": DEFAULT_LIMIT, "host": DEFAULT_IMAP_HOST, "port": DEFAULT_IMAP_PORT,
         "folder": DEFAULT_FOLDER, "syntax": "gmail", "refine": dict(REFINE_DEFAULTS)}
    v["saved_pw"] = bool(session and session.get("password"))
    if session:
        v["email"] = session["email"]
        v.update(session.get("search_values", {}))
    if values:
        v.update(values)

    body = [f"<h1>IMAP Search Tool</h1>",
            '<p class="note">Narrow a mailbox down in stages: one broad server search, then as many local filters as you need.</p>',
            '<div class="warning"><strong>Privacy:</strong> runs only on your computer. Your app password and the downloaded emails '
            'are held in memory for this session only (dropped after 2 idle hours or when the app stops) and never written to disk. '
            'Mailbox access is read-only. For Gmail use an App Password.</div>']
    if v["saved_pw"]:
        body.append(f'<form method="post" action="/forget" class="note" style="margin:10px 0">🔒 App password for '
                    f'<strong>{escape(session["email"])}</strong> is saved in memory for this session. '
                    f'<button class="secondary" style="margin:0 0 0 8px;padding:4px 10px">Forget password</button></form>')
    if message:
        body.append(message)

    if session and session["steps"]:
        steps = session["steps"]
        items = "".join(
            f'<li{" class=last" if i == len(steps) - 1 else ""}>{escape(s["label"])} → <strong>{len(s["uids"])}</strong> email(s)</li>'
            for i, s in enumerate(steps))
        undo = ('<form class="inline" method="post" action="/undo"><button class="secondary">Undo last step</button></form>'
                if len(steps) > 1 else "")
        body.append(f'<div class="panel"><h2>Steps so far</h2><ol class="steps">{items}</ol>{undo}'
                    '<form class="inline" method="post" action="/reset"><button class="secondary">Start over</button></form></div>')
        body.append(f'<div class="panel"><h2>Next step: filter these {len(steps[-1]["uids"])} emails</h2>{refine_form(v)}</div>')
        body.append(f'<div class="panel"><details><summary>Run a new server search instead (replaces everything above)</summary>'
                    f'{search_form(v, open_details=False)}</details></div>')
        body.append(f'<h2>Current candidates ({len(steps[-1]["uids"])})</h2>')
        body.append(render_results(session))
    else:
        body.append(f'<div class="panel"><h2>Step 1: broad server search</h2>{search_form(v)}</div>')

    body.append("<footer>IMAP over SSL · read-only · filters look at message subject, sender and readable text/HTML body parts; "
                "attachments and images are not searched.</footer>")
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1"><title>IMAP Search Tool</title>'
            f'<style>{CSS}</style></head><body>{"".join(body)}</body></html>')


def err(msg):
    return f"<p class='error'>{escape(msg)}</p>"


# ----------------------------------------------------------------- server --

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))   # never logs bodies

    def send_html(self, html, status=200, sid=None, clear_cookie=False):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if sid:
            self.send_header("Set-Cookie", f"sid={sid}; HttpOnly; SameSite=Strict; Path=/")
        elif clear_cookie:
            self.send_header("Set-Cookie", "sid=; Max-Age=0; HttpOnly; SameSite=Strict; Path=/")
        self.end_headers()
        self.wfile.write(body)

    def host_ok(self):
        # Defends against DNS-rebinding and cross-site form posts.
        allowed = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
        if self.headers.get("Host", "") not in allowed:
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin in {f"http://{h}" for h in allowed}

    def current_session(self):
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        sid = cookie["sid"].value if "sid" in cookie else ""
        return get_session(sid)

    def do_GET(self):
        if not self.host_ok():
            self.send_error(403)
            return
        self.send_html(render_page(self.current_session()))

    def do_POST(self):
        if not self.host_ok():
            self.send_error(403)
            return
        session = self.current_session()
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 20000:
                raise ValueError("Request too large.")
            form = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
            field = lambda name, default="": form.get(name, [default])[0]

            if self.path == "/search":
                self.handle_search(field)
            elif self.path == "/test":
                self.handle_test(field)
            elif self.path == "/forget":
                if session:
                    with session["lock"]:
                        session["password"] = None
                self.send_html(render_page(session, "<p class='note'>Password forgotten.</p>"))
            elif self.path in ("/refine", "/undo", "/reset", "/keephits"):
                if not session:
                    raise ValueError("Your session has expired. Run a new search.")
                with session["lock"]:
                    if self.path == "/refine":
                        self.handle_refine(session, field)
                    elif self.path == "/keephits":
                        last = session["steps"][-1]
                        kept = [u for u in last["uids"] if u in last["hits"]]
                        session["steps"].append({"label": "Keep only emails with a visible match", "uids": kept,
                                                 "hits": last["hits"], "expects_hits": True})
                        self.send_html(render_page(session))
                    elif self.path == "/undo":
                        if len(session["steps"]) > 1:
                            session["steps"].pop()
                        self.send_html(render_page(session))
                    else:
                        session["msgs"].clear()
                        session["steps"].clear()
                        self.send_html(render_page(session))
            else:
                self.send_error(404)
        except imaplib.IMAP4.error as e:
            self.send_html(render_page(session, err(f"IMAP error: {e}. Check your app password and account settings.")), 400)
        except (ValueError, RuntimeError, OSError, socket.timeout) as e:
            self.send_html(render_page(session, err(str(e))), 400)
        except Exception as e:
            self.send_html(render_page(session, err(f"Unexpected error: {e}")), 500)

    def password_for(self, session, email, field):
        """Typed password if given, else the one saved for this session and address."""
        typed = field("password").replace(" ", "")
        if typed:
            return typed
        if session and session.get("password") and session["email"] == email:
            return session["password"]
        return ""

    def remember(self, session, email, password):
        """Save credentials in the (possibly new) session after a successful login."""
        if session is None:
            session = get_session(None, create=True)
        with session["lock"]:
            session["email"] = email
            session["password"] = password
        return session

    def read_values(self, field):
        email_address = field("email").strip()
        return {
            "email": email_address,
            "query": field("query").strip(),
            "host": field("host", DEFAULT_IMAP_HOST).strip() or DEFAULT_IMAP_HOST,
            "port": int(field("port", str(DEFAULT_IMAP_PORT)) or DEFAULT_IMAP_PORT),
            "folder": field("folder", DEFAULT_FOLDER).strip() or DEFAULT_FOLDER,
            "syntax": "text" if field("syntax") == "text" else "gmail",
            "since": field("since").strip(),
            "before": field("before").strip(),
            "exclude": field("exclude"),
            "from_": field("from_").strip(),
            "subject": field("subject").strip(),
            "limit": int(field("limit", str(DEFAULT_LIMIT))),
        }

    def handle_test(self, field):
        values = self.read_values(field)
        session = self.current_session()
        password = self.password_for(session, values["email"], field)
        try:
            if "@" not in values["email"] or not password:
                raise ValueError("Enter your email address and app password to test the connection.")
            if any(c in values["folder"] for c in '"\r\n'):
                raise ValueError("Invalid folder name.")
            result = test_connection(values["host"], values["port"], values["email"], password, values["folder"])
        except imaplib.IMAP4.error as e:
            self.send_html(render_page(session, err(f"Connection failed: {e}. Check your address, app password and that IMAP is enabled."), values), 400)
            return
        except (ValueError, RuntimeError, OSError) as e:
            self.send_html(render_page(session, err(f"Connection failed: {e}"), values), 400)
            return
        session = self.remember(session, values["email"], password)
        self.send_html(render_page(session, f"<p class='ok'>✓ {escape(result)}</p>", values), sid=session["sid"])

    def handle_search(self, field):
        values = self.read_values(field)
        email_address = values["email"]
        password = self.password_for(self.current_session(), email_address, field)
        try:
            if "@" not in email_address or not password:
                raise ValueError("Enter your email address and app password.")
            if not 1 <= values["limit"] <= MAX_LIMIT:
                raise ValueError(f"Download limit must be between 1 and {MAX_LIMIT}.")
            if any(c in values["folder"] for c in '"\r\n'):
                raise ValueError("Invalid folder name.")
            since = parse_date(values["since"], "“On or after date”")
            before = parse_date(values["before"], "“Before date”")
            if since and before and since >= before:
                raise ValueError("“On or after date” must be earlier than “Before date”.")
            msgs, order, total = download(values["host"], values["port"], email_address, password,
                                          values["folder"], values["syntax"], values["query"], values["limit"],
                                          since, before, split_lines(values["exclude"]), values["from_"], values["subject"])
        except Exception as e:
            existing = self.current_session()
            if isinstance(e, imaplib.IMAP4.error):
                e = f"IMAP error: {e}. Check your app password and account settings."
            self.send_html(render_page(existing, err(str(e)), values), 400)
            return
        session = self.remember(self.current_session(), email_address, password)
        with session["lock"]:
            session["msgs"] = msgs
            session["search_values"] = {k: values[k] for k in ("query", "since", "before", "exclude", "from_", "subject", "host", "port", "folder", "syntax", "limit")}
            label = f"Server search {values['query']!r}" if values["query"] else "Server search (everything)"
            if since:
                label += f" from {since.isoformat()}"
            if before:
                label += f" before {before.isoformat()}"
            for text, shown in ((values["exclude"], "excluding"), (values["from_"], "sender"), (values["subject"], "subject")):
                if text.strip():
                    label += f" · {shown} {', '.join(split_lines(text))}"
            hits, terms = initial_hits(msgs, order, values["query"], values["syntax"])
            if terms:       # emails with a visible use of the search words first
                order = [u for u in order if u in hits] + [u for u in order if u not in hits]
            session["steps"] = [{"label": label, "uids": order, "hits": hits, "expects_hits": bool(terms)}]
            note = ""
            if total > len(order):
                note = (f"<p class='note'>The server found {total} emails but only the newest {len(order)} were downloaded "
                        f"(limit {values['limit']}). Raise the limit or make the server search more specific.</p>")
            self.send_html(render_page(session, note), sid=session["sid"])

    def handle_refine(self, session, field):
        mode, scope = field("mode"), field("scope")
        if mode not in MODES or scope not in SCOPES:
            raise ValueError("Unknown match type or scope.")
        p = {**REFINE_DEFAULTS, "mode": mode, "scope": scope,
             "whole_word": field("whole_word") == "1", "match_case": field("match_case") == "1"}
        for key in LIST_FIELDS:
            p[key] = field(key)
        for key, lo, hi in (("distance", 1, 2000), ("min_count", 1, 1000), ("min_digits", 1, 40), ("max_digits", 1, 40)):
            try:
                p[key] = int(field(key, str(REFINE_DEFAULTS[key])) or REFINE_DEFAULTS[key])
            except ValueError:
                raise ValueError(f"“{key}” must be a whole number.")
            if not lo <= p[key] <= hi:
                raise ValueError(f"“{key}” must be between {lo} and {hi}.")
        values = {"refine": dict(p)}
        try:
            apply_filter(session, p)
        except ValueError as e:
            self.send_html(render_page(session, err(str(e)), values), 400)
            return
        for key in LIST_FIELDS:         # keep the options, clear the entered terms for the next step
            values["refine"][key] = ""
        self.send_html(render_page(session, values=values))


if __name__ == "__main__":
    print(f"IMAP Search Tool running at http://{HOST}:{PORT}")
    print("Keep this terminal open while using the app. Press Ctrl+C to stop.")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
