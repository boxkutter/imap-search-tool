"""Unit tests for the filtering engine and search building (no network needed).

Run from the project root:  python3 -m unittest discover -s tests -v
"""
import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import imap_search_tool as T  # noqa: E402


def msg(subject="", sender="", body=""):
    return {"uid": b"1", "subject": subject, "sender": sender, "date": "", "body": body, "url": ""}


def rules(**kw):
    return T.compile_rules({**T.REFINE_DEFAULTS, **kw})


def matches(r, m):
    res = T.run_rules(r, m)
    return res is not None


class FakeConn:
    """Records the arguments of a UID SEARCH."""
    def __init__(self):
        self.args, self.literal = None, None

    def uid(self, command, *args):
        self.args = (command,) + args
        return "OK", [b"1 2 3"]


class MatcherTests(unittest.TestCase):
    def spans(self, mode, text, haystack, **kw):
        opts = dict(ignore_case=True, whole_word=False, min_digits=7, max_digits=15)
        opts.update(kw)
        return T.build_matcher(mode, text, opts["ignore_case"], opts["whole_word"],
                               opts["min_digits"], opts["max_digits"])(haystack)

    def test_whole_word_excludes_longer_words(self):
        self.assertEqual(len(self.spans("contains", "Du", "Dubai du Du.", whole_word=True)), 2)
        self.assertEqual(len(self.spans("contains", "Du", "Dubai du Du.")), 3)

    def test_match_case(self):
        self.assertEqual(len(self.spans("contains", "Du", "du Du DU", ignore_case=False)), 1)

    def test_contains_escapes_regex_characters(self):
        self.assertEqual(len(self.spans("contains", "a.b", "a.b axb")), 1)

    def test_regex_and_error(self):
        self.assertEqual(len(self.spans("regex", r"\bDu\b(?! [a-z])", "Du Wei, Du bist", ignore_case=False)), 1)
        with self.assertRaises(ValueError):
            self.spans("regex", "(", "x")

    def test_number_modes_ignore_formatting(self):
        text = "call +971 (0) 50 123-4505 or 0097150 123 4506"
        self.assertEqual(len(self.spans("num_ends", "05", text)), 1)
        self.assertEqual(len(self.spans("num_starts", "+971", text)), 1)       # + must be present
        self.assertEqual(len(self.spans("num_contains", "971", text)), 2)

    def test_number_length_range(self):
        text = "in 2005 or +971 50 123 4505"
        found = self.spans("num_ends", "05", text, min_digits=7)       # the year is too short
        self.assertEqual([text[s:e] for s, e in found], ["+971 50 123 4505"])
        self.assertEqual(len(self.spans("num_ends", "05", text, min_digits=1)), 2)

    def test_number_without_digits_is_error(self):
        with self.assertRaises(ValueError):
            self.spans("num_ends", "abc", "x")


class RuleTests(unittest.TestCase):
    M = msg("Your Du bill", "billing@du.ae", "Hello Du Wei, your bill is ready. Du account.")

    def test_all_any_not(self):
        self.assertTrue(matches(rules(all_terms="Du\nbill"), self.M))
        self.assertFalse(matches(rules(all_terms="Du\ninvoice"), self.M))
        self.assertTrue(matches(rules(any_terms="invoice\nbill"), self.M))
        self.assertFalse(matches(rules(all_terms="Du", not_terms="account"), self.M))

    def test_near_and_not_near(self):
        self.assertTrue(matches(rules(all_terms="Du", near_terms="bill", distance=30), self.M))
        self.assertFalse(matches(rules(all_terms="Du", near_terms="zebra"), self.M))
        # every "Du" is near something that is not "Wei" only if one hit survives
        self.assertTrue(matches(rules(all_terms="Du", notnear_terms="Wei", distance=8), self.M))
        self.assertFalse(matches(rules(all_terms="Wei", notnear_terms="Du", distance=8), self.M))

    def test_min_count(self):
        self.assertTrue(matches(rules(all_terms="Du", whole_word=True, match_case=True, min_count=3), self.M))
        self.assertFalse(matches(rules(all_terms="Du", whole_word=True, match_case=True, min_count=4), self.M))

    def test_sender_and_subject_rules(self):
        self.assertTrue(matches(rules(all_terms="Du", from_in="du.ae"), self.M))
        self.assertFalse(matches(rules(all_terms="Du", from_out="du.ae"), self.M))
        self.assertTrue(matches(rules(all_terms="Du", subj_in="bill"), self.M))
        self.assertFalse(matches(rules(all_terms="Du", subj_out="bill"), self.M))

    def test_scope(self):
        self.assertFalse(matches(rules(all_terms="Hello", scope="subject"), self.M))
        self.assertTrue(matches(rules(all_terms="Hello", scope="body"), self.M))


class SummaryTests(unittest.TestCase):
    def test_nearby_hits_share_a_snippet_and_are_highlighted(self):
        text = "Du is here and Du is there. " + "x" * 400 + " Du again"
        spans = [(0, 2), (15, 17), (len(text) - 8, len(text) - 6)]
        out = T.summarize(text, spans)
        self.assertEqual(out["count"], 3)
        self.assertEqual(len(out["snips"]), 2)
        self.assertEqual(out["snips"][0].count("<mark>"), 2)

    def test_html_in_email_is_escaped(self):
        out = T.summarize("<script>Du</script>", [(8, 10)])
        self.assertNotIn("<script>", out["snips"][0])

    def test_limit_reports_hidden_places(self):
        text = (" " * 300).join(["Du"] * 8)
        spans = [(i, i + 2) for i in range(len(text)) if text.startswith("Du", i)]
        out = T.summarize(text, spans, limit=5)
        self.assertEqual((len(out["snips"]), out["more"]), (5, 3))


class SearchBuildingTests(unittest.TestCase):
    def test_highlight_terms_skip_operators_and_negations(self):
        q = 'Du -newsletter from:du.ae "exact phrase" (bill OR invoice) after:2025/01/01'
        self.assertEqual(T.highlight_terms(q, "gmail"), ["Du", "exact phrase", "bill", "invoice"])
        self.assertEqual(T.highlight_terms("a b", "text"), ["a b"])
        self.assertEqual(T.highlight_terms("", "gmail"), [])

    def test_gmail_raw(self):
        self.assertEqual(T.gmail_raw("Du", ["spam", "a b"], "du.ae", "bill"),
                         'Du -"spam" -"a b" from:(du.ae) subject:(bill)')

    def test_gmail_search_with_dates(self):
        c = FakeConn()
        T.server_search(c, "gmail", "Du", date(2026, 1, 5), date(2026, 2, 1), ["spam"])
        self.assertEqual(c.args, ("SEARCH", "SINCE", "5-Jan-2026", "BEFORE", "1-Feb-2026",
                                  "X-GM-RAW", '"Du -\\"spam\\""'))

    def test_non_ascii_goes_as_literal(self):
        c = FakeConn()
        T.server_search(c, "gmail", "café")
        self.assertEqual(c.literal, "café".encode())
        self.assertEqual(c.args, ("SEARCH", "CHARSET", "UTF-8", "X-GM-RAW"))

    def test_plain_imap_search(self):
        c = FakeConn()
        T.server_search(c, "text", "Du", exclude=["spam"], from_="du.ae", subject="bill")
        self.assertEqual(c.args, ("SEARCH", "NOT", "TEXT", '"spam"', "FROM", '"du.ae"',
                                  "SUBJECT", '"bill"', "TEXT", '"Du"'))
        with self.assertRaises(ValueError):
            T.server_search(FakeConn(), "text", "Du", exclude=["café"])

    def test_empty_search_is_all(self):
        c = FakeConn()
        T.server_search(c, "gmail", "")
        self.assertEqual(c.args, ("SEARCH", "ALL"))

    def test_dates(self):
        self.assertEqual(T.parse_date("2026-03-01", "x"), date(2026, 3, 1))
        self.assertIsNone(T.parse_date("", "x"))
        with self.assertRaises(ValueError):
            T.parse_date("nope", "x")

    def test_folder_name_decoding(self):
        self.assertEqual(T.decode_folder("[Gmail]/Alle Nachrichten"), "[Gmail]/Alle Nachrichten")
        self.assertEqual(T.decode_folder("&AOQ-"), "ä")


if __name__ == "__main__":
    unittest.main()
