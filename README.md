# IMAP Search Tool

**Find the needle in a mailbox by narrowing it down in stages, not by guessing one perfect search.**

IMAP Search Tool is a small, local, browser-based app for searching an email account (Gmail, or any IMAP server). You run one broad search on the server, download the matches, and then keep filtering that candidate set with as many precise rules as you need, instantly and offline. Every use of your search words is highlighted in context, so you can see *why* an email matched.

It is a single Python file with **no dependencies**. Nothing is installed, nothing is uploaded, and nothing is written to disk.

## Why

Mail search is good at finding emails that contain a word and bad at finding emails that use it *in a particular way*:

- You want the phone company **Du**, but Gmail also returns every French "**du**" and every "**Du**bai".
- You want a number that **starts with +971 and ends with 05**, but mail search only matches whole words and can't see the end of a number.
- You searched for something, got hundreds of results, and can't see where the word even appears.

This tool treats search as a **funnel**. Each step works on the previous step's results (numbers below are illustrative):

| Step | What you do | Emails left |
|---|---|---|
| 1 | Server search: `Du` | 412 |
| 2 | ALL of `Du` · whole word · match case | 96 |
| 3 | …and near `bill`, `account` | 31 |
| 4 | …and not near `monsieur` · sender lacks `newsletter` | 12 |

You can undo any step, or start over, without re-downloading.

## Features

- **Two-phase search**: one server-side search to fetch candidates, then unlimited local refinement with no login and no waiting.
- **Highlighted evidence**: each result shows the matched text in context, a match count, and several snippets. This applies to the first search too. Emails the server returned but where no use of your words can be found are flagged and listed last, and a button removes them.
- **Rich filter rules** at every step:
  - must contain **ALL** of / **AT LEAST ONE** of / **NONE** of (one term per line)
  - **whole word** only and **match case** (so `Du` ≠ `du`)
  - appears **at least N times**
  - **near / not near** other words (within N characters)
  - sender and subject rules (contains any of / contains none of)
  - look in subject, sender, body, or all
- **Match types**: plain text, regular expression, or **number-aware** (starts with / ends with / contains, ignoring spaces, dashes, dots and brackets, with a digit-length range).
- **Server-side narrowing** in step 1: search text, sender, subject, exclude list and a date range, so you download less. Gmail search syntax is supported (`from:`, `subject:`, `"exact phrase"`, `OR`, `-word`, `has:attachment`…), as is plain IMAP search for other servers.
- **Connection test** button, and automatic detection of the right "All Mail" folder (it is localised in non-English Gmail accounts).
- **Private by design**: runs on `127.0.0.1` only, read-only mailbox access, everything held in memory.

## Quick start

Requires **Python 3.9+**. No packages to install.

```bash
git clone https://github.com/boxkutter/imap-search-tool.git
cd imap-search-tool
python3 imap_search_tool.py
```

Open <http://127.0.0.1:8765>, enter your address and an app password (see below), and click **Test connection**, then **Search mailbox**.

### Gmail app password

Gmail doesn't allow regular passwords over IMAP for most accounts. You need:

1. **2-Step Verification** turned on for your Google account.
2. An **App Password** (Google Account → Security → 2-Step Verification → App passwords). Use that 16-character password in this tool.
3. **IMAP enabled** (Gmail → Settings → See all settings → Forwarding and POP/IMAP).

Some Workspace and Advanced Protection accounts can't create app passwords. Other IMAP servers work with their normal credentials; set the host, port and folder under *Server settings*.

## Using it

### Step 1: server search

| Field | Meaning |
|---|---|
| **Search for** | Words or phrases sent to the server. With Gmail syntax you can use `"exact phrase"`, `a OR b`, `-excluded`, `from:`, `has:attachment`… Leave empty to download everything (newest first, up to the limit). |
| **Sender / Subject contains** | Optional narrowing. |
| **Exclude emails containing** | One word or phrase per line. |
| **On or after / Before date** | Date range by server receive date. *Before* is exclusive: for all of January use `2026-01-01` to `2026-02-01`. |
| **Max emails to download** | Newest N matches (default 1,500, max 5,000). |
| **Server settings** | IMAP host, port (SSL), folder, and search syntax (Gmail or plain IMAP). |

> **Important:** Gmail's server search ignores case and punctuation, and it matches whole words. So `Du` also finds `du`, and `971` finds `+971 50…` but **not** `+971501234505`. That is exactly why the local filters below exist. If your target might be written in a way the server can't match, use a looser first search (or an empty one with a date range) and let the local rules do the work.

Every use of your search words is highlighted in the results (operators like `from:` and negations like `-word` are ignored for highlighting).

### Step 2 and beyond: filters

All terms are entered **one per line**. The lines in one box are combined as the box says; different boxes are combined with **AND**.

| Box | Keeps an email when… |
|---|---|
| **Must contain ALL of** | every line matches |
| **Must contain AT LEAST ONE of** | at least one line matches |
| **Must NOT contain ANY of** | no line matches |
| **Near ANY of** / **NOT near ANY of** *(Context)* | a match has (or doesn't have) one of these words within *N* characters before or after it |
| **Sender / Subject rules** | the sender or subject contains any of / none of the entries |

Options: **Whole word only**, **Match case**, **Appears at least N time(s)**, and **Look in** (subject, sender, body, everything).

**Match types**

| Type | Behaviour |
|---|---|
| Text contains | Plain substring |
| Regular expression | Python regex per line, e.g. `\bDu\b(?! [a-z])` |
| Number starts with / ends with / contains | Reads digit sequences such as `+971 (0) 50 123-4505`, ignores spaces, dashes, dots and brackets, and compares digits only. A leading `+` in a *starts with* query must be present in the email. The digit-length range (default 7–15) stops short numbers, like years, from matching. |

### Worked examples

**A common word used as a name: the company "Du"**

1. Step 1: search `Du`. Results are highlighted, so you immediately see the French "du" hits.
2. Step 2: *ALL of* `Du`, tick **Whole word** and **Match case**. "du mois" and "Dubai" disappear.
3. Step 3: *Context* → *near* `bill`, `account`, `mobile`. Only mentions in a customer context remain.
4. Step 4: *NOT near* `Herr`, or *Sender lacks* `newsletter`, to cut remaining noise.

**A phone number that starts with +971 and ends with 05**

1. Step 1: search `971`.
2. Step 2: *Number starts with* `+971`.
3. Step 3: *Number ends with* `05`, with the length range set to 12–12 (3 country-code digits + 7 + 2).

Or in one regex step (every line is a regex; this tolerates spaces, non-breaking spaces, dots, dashes, brackets and line breaks between digits):

```
\+971(?:[\s.()-]*\d){0,7}[\s.()-]*05(?!\d)
```

## Privacy and security

- The server binds to `127.0.0.1` only and rejects requests with a foreign `Host` or `Origin` header (protection against DNS rebinding and cross-site requests).
- The mailbox is opened **read-only**. The tool never moves, deletes, labels or sends mail.
- Your **app password** is kept **in the app's memory only**, so repeat searches don't need it again. It is saved only after a successful login, is never written to disk or logged, and is never sent back to the page. It is dropped by **Forget password**, after 2 idle hours, or when you stop the app.
- Downloaded emails are also held in memory only (up to ~500k characters of text per email) and dropped after 2 idle hours, on **Start over**, or when you stop the app.
- Access is tied to a random session cookie (`HttpOnly`, `SameSite=Strict`). While a password is saved, anyone using your browser on the same computer could run searches. Click **Forget password** when you finish, particularly on a shared machine.
- Email content is HTML-escaped before display.

If you find a security problem, please open an issue describing it in general terms (without exploit details) and the maintainer will follow up.

## Limitations

- Only the **subject, sender and readable text/HTML body parts** are searched. Attachments, images and scanned PDFs are not. (Gmail's server search may still match attachment text, so such emails are flagged "no visible match".)
- Step 1 downloads full messages, including attachments, so very large result sets can be slow. Narrow the search or lower the limit.
- Dates use the date the server received the message.
- Gmail's All Mail excludes Spam and Trash.
- Number detection can merge adjacent numbers separated by one space (e.g. `2024 971 50…`); the digit-length range limits this.
- Plain IMAP mode needs ASCII for the exclude / sender / subject fields (Gmail mode accepts any text).
- It is a local tool for one user, not a multi-user web service. Do not expose it beyond localhost.

## Troubleshooting

| Problem | What to try |
|---|---|
| *Authentication failed* | Use an **App Password**, not your normal password. Spaces are ignored. |
| *Could not open folder* | The tool tries your account's All Mail folder automatically and otherwise lists the folders available. Copy one into *Server settings → Folder*. If no folders are listed, enable IMAP in Gmail settings. |
| *No emails left* | A filter was too strict. Click **Undo last step**, or check the highlighted snippets to see how your target is really written (odd spacing, dashes, `00971`, no `+`). |
| *Session has expired* | Run a new server search. |

## Development

```bash
python3 -m unittest discover -s tests -v
```

The unit tests cover the filtering engine and search construction and need no network. The whole app is `imap_search_tool.py` (standard library only), so it is easy to read and modify. Issues and pull requests are welcome.

## License

[MIT](LICENSE)
