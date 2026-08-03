# Copyright (C) 2026 rss2email contributors
#
# This file is part of rss2email.
#
# rss2email is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 2 of the License, or (at your option) version 3 of
# the License.
#
# rss2email is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
# A PARTICULAR PURPOSE.  See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with
# rss2email.  If not, see <http://www.gnu.org/licenses/>.

"""A minimal local web UI for managing rss2email feeds.

This module powers the ``r2e web`` subcommand. It serves a small HTML
interface over Python's standard-library ``http.server`` and reuses the
existing :class:`rss2email.feeds.Feeds` data layer for all mutations, so the
web UI and the cron-driven ``r2e run`` job share the same on-disk config
and data files and coexist exactly as two independent ``r2e`` invocations
would (each request loads, mutates, and saves atomically; the datafile is
file-locked per the existing conventions in :mod:`rss2email.feeds`).

There is deliberately no authentication: run this behind your VPN or on
``127.0.0.1``.
"""

import html as _html
import signal as _signal
import threading as _threading
import urllib.parse as _urlparse
from contextlib import contextmanager as _contextmanager
from http.server import BaseHTTPRequestHandler as _BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer as _ThreadingHTTPServer

from . import LOG as _LOG
from . import error as _error
from . import feeds as _feeds


class _feeds_error_renderer(Exception):
    """Carries a user-facing error message out of the POST handlers."""


_PAGE_TEMPLATE = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>rss2email</title>
<style>
body {{ font: 14px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       margin: 1.5rem auto; max-width: 60rem; padding: 0 .5rem;
       color: #222; background: #fafafa; }}
h1 {{ font-size: 1.4rem; }}
table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; }}
th, td {{ border: 1px solid #ddd; padding: .35rem .5rem; text-align: left;
          vertical-align: top; }}
th {{ background: #eee; }}
.url {{ font-family: monospace; word-break: break-all; }}
.active-yes {{ color: #2a7; font-weight: bold; }}
.active-no {{ color: #c33; }}
form.inline {{ display: inline; margin: 0; }}
button {{ font: inherit; cursor: pointer; }}
.add-form {{ margin-top: 1.5rem; padding: .75rem; background: #eef;
             border-radius: 4px; }}
.add-form label {{ display: inline-block; width: 4rem; }}
.add-form input[type=text] {{ width: 18rem; }}
.error {{ background: #fdd; border: 1px solid #c33; padding: .5rem .75rem;
          margin: .5rem 0; border-radius: 4px; }}
.empty {{ color: #666; font-style: italic; }}
a {{ color: #16d; }}
</style>
</head>
<body>
<h1>rss2email feeds</h1>
{error_block}
<table>
<thead><tr>
<th>#</th><th>name</th><th>url</th><th>to</th><th>state</th><th>actions</th>
</tr></thead>
<tbody>
{rows}
</tbody>
</table>
<section class="add-form">
<h2>Add a feed</h2>
<form method="post" action="/add">
<p><label for="name">name</label>
<input id="name" name="name" type="text" required></p>
<p><label for="url">url</label>
<input id="url" name="url" type="text" required></p>
<p><label for="to">to</label>
<input id="to" name="to" type="text" placeholder="(use default)"></p>
<p><button type="submit">Add feed</button></p>
</form>
</section>
<script>
// Confirm delete using the feed name carried in a data-attribute (a proper
// HTML-attribute context, escaped by the server) rather than by interpolating
// the name into inline JS, which would be an XSS sink.
document.addEventListener('submit', function (ev) {{
  var form = ev.target;
  if (form.matches('form[data-name]')) {{
    if (!confirm('Delete feed ' + form.dataset.name + '?')) {{
      ev.preventDefault();
    }}
  }}
}});
</script>
</body>
</html>
"""


def _render_rows_row(index, feed):
    name = _html.escape(feed.name or '')
    url = _html.escape(feed.url or '')
    to = _html.escape(feed.to or '')
    if feed.active:
        state = '<span class="active-yes">active</span>'
        toggle = ('<form class="inline" method="post" action="/pause">'
                  '<input type="hidden" name="index" value="{i}">'
                  '<button type="submit">pause</button></form>').format(i=index)
    else:
        state = '<span class="active-no">paused</span>'
        toggle = ('<form class="inline" method="post" action="/unpause">'
                  '<input type="hidden" name="index" value="{i}">'
                  '<button type="submit">unpause</button></form>').format(i=index)
    delete = ('<form class="inline" method="post" action="/delete" '
              'data-name="{name}">'
              '<input type="hidden" name="index" value="{i}">'
              '<button type="submit">delete</button></form>').format(
                  i=index, name=name)
    return (
        '<tr>'
        '<td>{i}</td>'
        '<td>{name}</td>'
        '<td class="url">{url}</td>'
        '<td>{to}</td>'
        '<td>{state}</td>'
        '<td>{toggle} {delete}</td>'
        '</tr>').format(
            i=index, name=name, url=url, to=to,
            state=state, toggle=toggle, delete=delete)


def _render_index(feeds, error=None):
    rows = []
    for i, feed in enumerate(feeds):
        rows.append(_render_rows_row(i, feed))
    if not rows:
        rows = ['<tr><td colspan="6" class="empty">no feeds yet '
                '\u2014 add one below.</td></tr>']
    if error:
        error_block = '<div class="error">{}</div>'.format(_html.escape(error))
    else:
        error_block = ''
    return _PAGE_TEMPLATE.format(
        error_block=error_block, rows='\n'.join(rows))


def _redirect(self, location='/'):
    self.send_response(303)
    self.send_header('Location', location)
    self.send_header('Content-Length', '0')
    self.end_headers()


def _write_html(self, body, status=200):
    data = body.encode('utf-8')
    self.send_response(status)
    self.send_header('Content-Type', 'text/html; charset=utf-8')
    self.send_header('Content-Length', str(len(data)))
    self.end_headers()
    self.wfile.write(data)


def _parse_form(self):
    length = int(self.headers.get('Content-Length', 0) or 0)
    # Reject negative Content-Length up front: ``rfile.read(-1)`` reads
    # until EOF, so a client claiming ``Content-Length: -1`` would bypass
    # the ``_MAX_FORM_BYTES`` cap below and let it buffer an unbounded
    # body into memory.
    if length < 0:
        raise _feeds_error_renderer(
            'Invalid Content-Length ({}).'.format(length))
    if length > _MAX_FORM_BYTES:
        raise _feeds_error_renderer(
            'Form body too large ({} bytes; limit {} bytes).'.format(
                length, _MAX_FORM_BYTES))
    if length:
        try:
            raw = self.rfile.read(length)
        except Exception:
            raise _feeds_error_renderer('Could not read form body.')
    else:
        raw = b''
    try:
        text = raw.decode('utf-8', errors='replace')
    except Exception:
        text = raw.decode('latin-1', errors='replace')
    parsed = _urlparse.parse_qs(text, keep_blank_values=True)
    return {k: (v[0] if v else '') for k, v in parsed.items()}


def _is_csrf_safe(self):
    """Reject cross-origin browser POSTs (CSRF defense for the unauthed UI).

    Browsers always send ``Origin`` on cross-origin form POSTs (and on
    same-origin ones, in modern browsers). Non-browser clients (curl,
    the test suite) may omit it; absent Origin + absent Referer means
    "not a browser", which we allow. If either header is present it
    must point back at this server.

    We compare the request's Origin/Referer host+port against the
    server's *own* bound address (``self.server.server_address``),
    not the client-supplied ``Host`` header. A same-browser attacker
    page can forge matching ``Host``+``Origin`` headers, so trusting
    ``Host`` provides no defense.
    """
    exp_host, exp_port = self.server.server_address[:2]
    # When bound to the wildcard address we don't know which interface
    # the client actually used; accept any host and rely on the port.
    if exp_host in ('0.0.0.0', '::', ''):
        exp_host = None

    def _matches_self(url):
        if not url:
            return False
        try:
            parts = _urlparse.urlsplit(url)
        except ValueError:
            return False
        host = parts.hostname
        port = parts.port  # None if not specified
        if host is None:
            return False
        if exp_host is not None and (
                _loopback_equiv(host) != _loopback_equiv(exp_host)):
            return False
        if port is not None and port != exp_port:
            return False
        return True

    origin = self.headers.get('Origin')
    if origin:  # may legitimately be '' (empty); treat empties as absent
        return _matches_self(origin)
    referer = self.headers.get('Referer')
    if referer:
        return _matches_self(referer)
    return True


_MAX_FORM_BYTES = 1 * 1024 * 1024  # 1 MiB; admin-UI form bodies are tiny


_LOOPBACK_HOSTS = {'127.0.0.1', '::1', 'localhost'}


def _loopback_equiv(host):
    """Collapse loopback aliases so 127.0.0.1 / ::1 / localhost compare equal.

    A server bound to ``127.0.0.1`` is the same origin as ``localhost`` from
    a browser's standpoint; rejecting a same-machine POST just because the
    user typed ``http://localhost:port`` (so ``Origin: http://localhost``)
    would make the UI unusable. ``localhost`` and the two loopback IP
    literals are treated as one equivalence class for the CSRF host check.
    """
    return 'loopback' if host in _LOOPBACK_HOSTS else host


def make_handler(configfiles, datafile_path, write_lock):
    """Build a request-handler class bound to specific feed paths.

    A fresh :class:`~rss2email.feeds.Feeds` instance is constructed and
    loaded for each request, mirroring how a one-shot ``r2e`` invocation
    behaves. Mutating requests are serialized with ``write_lock`` so two
    concurrent POSTs cannot lose each other's updates.
    """

    @_contextmanager
    def _feeds_ctx():
        feeds = _feeds.Feeds(
            configfiles=list(configfiles) if configfiles else None,
            datafile_path=datafile_path)
        feeds.load()
        try:
            yield feeds
        finally:
            feeds.close()

    class Handler(_BaseHTTPRequestHandler):
        server_version = 'rss2email-web/1.0'

        def log_message(self, fmt, *args):
            _LOG.info('%s - %s', self.address_string(), fmt % args)

        def do_GET(self):
            path = _urlparse.urlparse(self.path).path
            with write_lock:
                if path != '/':
                    self._redirect('/')
                    return
                with _feeds_ctx() as feeds:
                    body = _render_index(feeds)
                _write_html(self, body)

        def do_POST(self):
            path = _urlparse.urlparse(self.path).path
            if not self._is_csrf_safe():
                # Cross-origin browser POST: a remote page open in the user's
                # browser trying to drive the unauthed localhost UI. Render
                # the index with an error rather than acting on the request.
                with write_lock, _feeds_ctx() as feeds:
                    body = _render_index(
                        feeds,
                        error='Cross-origin POST rejected (CSRF protection).')
                _write_html(self, body, status=403)
                return
            with write_lock:
                try:
                    if path == '/add':
                        self._handle_add()
                    elif path == '/delete':
                        self._handle_delete()
                    elif path == '/pause':
                        self._handle_set_active(active=False)
                    elif path == '/unpause':
                        self._handle_set_active(active=True)
                    else:
                        self._redirect('/')
                except _feeds_error_renderer as e:
                    with _feeds_ctx() as feeds:
                        body = _render_index(feeds, error=str(e))
                    _write_html(self, body)

        def _handle_add(self):
            form = _parse_form(self)
            name = form.get('name', '').strip()
            url = form.get('url', '').strip()
            to = form.get('to', '').strip() or None
            if not name or not url:
                raise _feeds_error_renderer(
                    'Both a name and a URL are required.')
            with _feeds_ctx() as feeds:
                try:
                    feed = feeds.new_feed(name=name, url=url, to=to)
                except _error.DuplicateFeedName:
                    raise _feeds_error_renderer(
                        'A feed named {!r} already exists.'.format(name))
                if not feed.to:
                    raise _feeds_error_renderer(
                        'No destination email address is set. Add a '
                        '"to" address, or set a default with '
                        '`r2e email <address>` first.')
                feeds.save_config()
                feeds.save_feeds()
            self._redirect('/')

        def _handle_delete(self):
            form = _parse_form(self)
            index = form.get('index', '').strip()
            if index == '':
                raise _feeds_error_renderer('No feed selected to delete.')
            with _feeds_ctx() as feeds:
                try:
                    feed = feeds.index(index)
                except _error.FeedIndexError:
                    raise _feeds_error_renderer(
                        'No feed at index {!r}.'.format(index))
                feeds.remove(feed)
                feeds.save_config()
                feeds.save_feeds()
            self._redirect('/')

        def _handle_set_active(self, active):
            form = _parse_form(self)
            index = form.get('index', '').strip()
            if index == '':
                raise _feeds_error_renderer(
                    'No feed selected to {}.'.format(
                        'pause' if not active else 'unpause'))
            with _feeds_ctx() as feeds:
                try:
                    feed = feeds.index(index)
                except _error.FeedIndexError:
                    raise _feeds_error_renderer(
                        'No feed at index {!r}.'.format(index))
                feed.active = active
                feeds.save_config()
            self._redirect('/')

        # reuse the module-level helpers bound to this instance
        _redirect = _redirect
        _write_html = _write_html
        _parse_form = _parse_form
        _is_csrf_safe = _is_csrf_safe

    return Handler


def serve(configfiles, datafile_path, host='127.0.0.1', port=8080):
    """Start the web UI server (blocking).

    ``configfiles`` and ``datafile_path`` are the same paths the rest of
    rss2email uses (typically resolved by :class:`rss2email.feeds.Feeds`
    from XDG environment variables, or overridden via ``-c`` / ``-d``).
    """
    write_lock = _threading.Lock()
    handler = make_handler(
        configfiles=configfiles, datafile_path=datafile_path,
        write_lock=write_lock)
    httpd = _ThreadingHTTPServer((host, port), handler)
    # Treat SIGTERM like SIGINT: shut down cleanly (tear down the listening
    # socket, stop serving). The default SIGTERM disposition would kill the
    # process without running `finally`, leaving the port in TIME_WAIT.
    # `signal.signal` raises `ValueError` outside the main thread; that's
    # the right failure mode if `serve()` is ever invoked off-main, so we
    # don't guard against it.
    def _term(signum, frame):
        raise KeyboardInterrupt
    previous_term = _signal.signal(_signal.SIGTERM, _term)
    actual_host, actual_port = httpd.server_address[:2]
    _LOG.info('rss2email web UI listening on http://%s:%s', actual_host,
              actual_port)
    _LOG.info('config: %r', configfiles)
    _LOG.info('data:   %r', datafile_path)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        _LOG.info('shutting down')
    finally:
        _signal.signal(_signal.SIGTERM, previous_term)
        httpd.server_close()