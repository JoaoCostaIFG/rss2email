import os
import sys
import unittest
from email.message import Message

sys.path.insert(0, os.path.dirname(__file__))

from rss2email.feed import Feed


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


if __name__ == '__main__':
    unittest.main()