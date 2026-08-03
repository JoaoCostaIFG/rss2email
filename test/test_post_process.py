import os
import sys
import unittest
from email.message import Message
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

sys.path.insert(0, os.path.dirname(__file__))

from rss2email.feed import Feed
from rss2email.post_process import redirect as _redirect


class _Parsed:
    """Minimal stand-in for the feedparser result ``_process`` needs."""

    def __init__(self):
        self.entries = [{}]  # one entry; _process_entry is stubbed


class TestPostProcess(unittest.TestCase):
    """``Feed._process`` must use the message a ``post-process`` hook
    returns, not the original message rss2email built.

    The bundled ``downcase`` hook hides a latent bug because it mutates
    the message *in place* and returns the same object. A hook that
    constructs a fresh message (as the documented contract allows:
    "Post-processing hooks should return the possibly altered message")
    was silently discarded by ``_process`` yielding the original tuple.
    """

    def _make_feed(self):
        feed = Feed(name='pp-test', url='http://example.com/feed',
                    to='a@b.com')
        # Bypass network/format validation inside _process; we only want
        # to exercise the post-process plumbing.
        feed._check_for_errors = lambda parsed: None

        def fake_process_entry(parsed, entry):
            original = Message()
            original['Subject'] = 'original'
            original.set_payload('original body')
            return ('guid', {'hash': 'h'}, '<sender@example>', original)

        feed._process_entry = fake_process_entry
        return feed

    def test_hook_returning_new_message_is_used(self):
        feed = self._make_feed()

        def replace_message(feed, parsed, entry, guid, message):
            new = Message()
            new['Subject'] = 'replaced'
            new['X-Hook-Ran'] = 'yes'
            new.set_payload('hook body')
            return new

        feed.post_process = replace_message

        results = list(feed._process(parsed=_Parsed()))

        self.assertEqual(len(results), 1)
        guid, state, sender, message = results[0]
        self.assertEqual(message['Subject'], 'replaced')
        self.assertEqual(message['X-Hook-Ran'], 'yes')
        self.assertEqual(message.get_payload(), 'hook body')

    def test_hook_returning_none_skips_entry(self):
        feed = self._make_feed()
        feed.post_process = lambda **kw: None
        self.assertEqual(list(feed._process(parsed=_Parsed())), [])

    def test_in_place_hook_still_works(self):
        feed = self._make_feed()

        def mutate_in_place(feed, parsed, entry, guid, message):
            message.replace_header('Subject', 'mutated')
            return message

        feed.post_process = mutate_in_place
        results = list(feed._process(parsed=_Parsed()))
        self.assertEqual(len(results), 1)
        _, _, _, message = results[0]
        self.assertEqual(message['Subject'], 'mutated')


class TestRedirectHook(unittest.TestCase):
    """The redirect post-process hook must not crash or destroy structure
    when handed a multipart message (e.g. ``multipart-html = yes``).

    ``get_payload(decode=True)`` returns None for multipart, so the old
    code raised ``TypeError: decoding to str: need a bytes-like object,
    NoneType``; even if it hadn't, ``set_payload(str, charset=...)`` would
    have overwritten the parts list and collapsed the multipart message
    into a single string.
    """

    def _feed(self):
        return Feed(name='redir-test-feed', url='http://example.com/feed',
                    to='a@b.com')

    def _entry(self, link):
        return {'link': link, 'enclosures': [], 'links': []}

    def test_multipart_message_is_returned_unchanged(self):
        feed = self._feed()
        # Build a multipart/alternative message, then have the hook see
        # it; nothing is faked on the network side because the hook must
        # bail out *before* following any link for multipart input.
        msg = MIMEMultipart('alternative')
        msg.attach(MIMEText('plain body http://feed.example/redir',
                            'plain', 'us-ascii'))
        msg.attach(MIMEText('<p>html body <a href="http://feed.example/redir">'
                            'x</a></p>', 'html', 'us-ascii'))
        msg['Subject'] = 'multipart entry'
        original_bytes = msg.as_bytes()
        out = _redirect.process(
            feed=feed, parsed=object(), entry=self._entry('http://feed.example/redir'),
            guid='g', message=msg)
        self.assertIs(out, msg)
        self.assertEqual(out.as_bytes(), original_bytes)
        self.assertEqual(out.get_content_type(), 'multipart/alternative')
        # Both alternatives must still be present.
        parts = out.get_payload()
        self.assertEqual(len(parts), 2)
        self.assertEqual({p.get_content_type() for p in parts},
                         {'text/plain', 'text/html'})

    def test_single_part_message_is_still_rewritten(self):
        # Sanity check: the guard must not disable the hook for the
        # single-part case it was designed for. Use a link that is not a
        # redirect (so the network call is a no-op returning the same
        # URL) to keep the test offline.
        feed = self._feed()
        msg = MIMEText(
            'Visit http://example.com/entry for more.',
            'plain', 'us-ascii')
        msg['Subject'] = 'single part entry'
        class _Resp:
            def geturl(self):
                return 'http://example.com/entry'
        class _Opener:
            def open(self, request, timeout=None):
                return _Resp()
        import urllib.request
        orig_build = urllib.request.build_opener
        urllib.request.build_opener = lambda *a, **k: _Opener()
        try:
            out = _redirect.process(
                feed=feed, parsed=object(),
                entry=self._entry('http://example.com/entry'),
                guid='g', message=msg)
        finally:
            urllib.request.build_opener = orig_build
        self.assertEqual(out.get_content_type(), 'text/plain')


if __name__ == '__main__':
    unittest.main()