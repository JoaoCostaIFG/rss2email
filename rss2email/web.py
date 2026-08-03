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
import logging as _logging
import signal as _signal
import threading as _threading
import time as _time
import traceback as _traceback
import urllib.parse as _urlparse
from argparse import Namespace as _Namespace
from contextlib import contextmanager as _contextmanager
from http.server import BaseHTTPRequestHandler as _BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer as _ThreadingHTTPServer

from . import LOG as _LOG
from . import command as _command
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
.fetch-ok {{ color: #2a7; }}
.fetch-err {{ color: #c33; }}
.fetch-none {{ color: #888; font-style: italic; }}
.fetch-detail {{ color: #666; font-size: 12px; }}
form.inline {{ display: inline; margin: 0; }}
button {{ font: inherit; cursor: pointer; }}
.run-status {{ margin: 1rem 0; padding: .75rem; background: #eef;
           border-radius: 4px; }}
.run-status .tail {{ font-family: monospace; font-size: 12px;
                     white-space: pre-wrap; background: #fff;
                     border: 1px solid #ddd; padding: .5rem;
                     max-height: 12rem; overflow: auto;
                     margin-top: .5rem; }}
.run-status .badge-ok {{ color: #2a7; font-weight: bold; }}
.run-status .badge-error {{ color: #c33; font-weight: bold; }}
.run-status .badge-running {{ color: #16d; font-weight: bold; }}
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
{run_block}
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
        active = '<span class="active-yes">active</span>'
        toggle = ('<form class="inline" method="post" action="/pause">'
                  '<input type="hidden" name="index" value="{i}">'
                  '<button type="submit">pause</button></form>').format(i=index)
    else:
        active = '<span class="active-no">paused</span>'
        toggle = ('<form class="inline" method="post" action="/unpause">'
                  '<input type="hidden" name="index" value="{i}">'
                  '<button type="submit">unpause</button></form>').format(i=index)
    fetch = _render_fetch_state(feed)
    state = '{}<br>{}'.format(active, fetch)
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


def _render_fetch_state(feed):
    "Render ``feed``'s last-fetch state for the web UI state column."
    status = getattr(feed, 'last_fetch_status', None)
    if status is None:
        return '<span class="fetch-none">never fetched</span>'
    when = _fmt_time(getattr(feed, 'last_fetch_time', None))
    if status == 'ok':
        http_status = getattr(feed, 'last_fetch_http_status', None)
        detail = ''
        if http_status is not None:
            detail = ' <span class="fetch-detail">(HTTP {})</span>'.format(
                int(http_status))
        return ('<span class="fetch-ok">fetched ok</span> '
                '<span class="fetch-detail">@ {}</span>{}').format(
                    when, detail)
    error_str = getattr(feed, 'last_fetch_error', None) or 'error'
    return ('<span class="fetch-err">error</span> '
            '<span class="fetch-detail">@ {}: {}</span>').format(
                when, _html.escape(error_str))


def _render_run_block():
    state = _RUN_STATE
    parts = ['<section class="run-status">']
    parts.append('<h2>Run</h2>')
    parts.append(
        '<form class="inline" method="post" action="/run">'
        '<button type="submit">Run (send email)</button></form>')
    parts.append(
        ' <form class="inline" method="post" action="/run-no-send">'
        '<button type="submit">Run (no send)</button></form>')
    parts.append(
        ' <form class="inline" method="post" action="/run-force-latest">'
        '<button type="submit">Run (force latest)</button></form>')
    parts.append('<p>')
    if state['running']:
        started = state['started']
        parts.append(
            '<span class="badge-running">running</span> '
            '({}) since {}'.format(
                _run_kind_label(state),
                _fmt_time(started)))
    elif state['status'] == 'ok':
        parts.append(
            'last run ({}) <span class="badge-ok">ok</span> '
            'started {}, finished {}'.format(
                _run_kind_label(state),
                _fmt_time(state['started']),
                _fmt_time(state['ended'])))
    elif state['status'] == 'error':
        parts.append(
            'last run ({}) <span class="badge-error">error</span> '
            'started {}, finished {}: {}'.format(
                _run_kind_label(state),
                _fmt_time(state['started']),
                _fmt_time(state['ended']),
                _html.escape(state['error'] or '')))
    else:
        parts.append('no runs yet. Use the buttons above to fetch '
                     '(and optionally send) all feeds.')
    parts.append('</p>')
    if state['tail']:
        parts.append('<div class="tail">{}</div>'.format(
            _html.escape('\n'.join(state['tail']))))
    parts.append('</section>')
    return '\n'.join(parts)


def _fmt_time(ts):
    if ts is None:
        return '?'
    return _time.strftime('%Y-%m-%d %H:%M:%S', _time.localtime(ts))


def _run_kind_label(state):
    """Human label for the kind of run shown in the status badges."""
    if state.get('force_latest'):
        return 'force-latest' if state['send'] else 'force-latest, no-send'
    return 'send' if state['send'] else 'no-send'


def _render_index(feeds, error=None):
    rows = []
    if feeds is None:
        # ``GET /`` while a background run is in progress: the worker
        # holds the datafile LOCK_EX for the whole run, so opening a
        # ``Feeds`` instance here would block until the run finishes.
        # Keep the page responsive (and pollable) by hiding the table
        # and showing just the run panel.
        rows = ['<tr><td colspan="6" class="empty">'
                'feed list is unavailable while a run is in progress; '
                'it will reappear when the run finishes.</td></tr>']
    else:
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
        error_block=error_block,
        run_block=_render_run_block(),
        rows='\n'.join(rows))


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


# --- Background "r2e run" orchestrator -------------------------------------
#
# The web UI deliberately does not hold the process-global ``rss2email.lock``
# (see the comment in ``rss2email.main`` about the ``web`` subcommand); per-
# request ``Feeds`` instances take the datafile ``LOCK_EX`` sidecar, which is
# exactly what a standalone ``r2e run`` invocation does too. So a run kicked
# off from the UI coexists with a concurrent cron ``r2e run`` the same way
# two CLI invocations would: whichever gets the lock first wins, the other
# blocks on ``lockf`` until the first finishes.
#
# State is in-memory only (this is an admin tool; restart forgets history)
# and serialized by ``_RUN_GUARD`` so two button clicks can't start twin
# runs that would just fight each other for the datafile lock.
_RUN_GUARD = _threading.Lock()
_RUN_TAIL_LINES = 200
_RUN_STATE = {
    'running': False,
    'send': None,        # True = send run, False = --no-send run
    'force_latest': False,  # True = --force-latest run
    'started': None,     # unix timestamp (float)
    'ended': None,       # unix timestamp (float)
    'status': None,      # 'ok' | 'error' | None
    'error': None,        # short error string (status == 'error')
    'tail': [],          # list of recent log lines (oldest first)
}


def _reset_run_state(send, force_latest=False):
    _RUN_STATE.update({
        'running': True,
        'send': send,
        'force_latest': force_latest,
        'started': _time.time(),
        'ended': None,
        'status': None,
        'error': None,
        'tail': [],
    })


def _run_worker(send, force_latest, configfiles, datafile_path):
    """Background thread body: drive ``rss2email.command.run`` once.

    A throwaway logging handler captures the run's output into
    ``_RUN_STATE['tail']`` so the index page can show the recent log
    lines (refreshing a stuck feed can take many minutes; without a tail
    the user has no feedback that anything is happening).
    """
    class _TailHandler(_logging.Handler):
        def emit(self, record):
            try:
                msg = self.format(record)
            except Exception:
                msg = record.getMessage()
            _RUN_STATE['tail'].append(msg)
            if len(_RUN_STATE['tail']) > _RUN_TAIL_LINES:
                del _RUN_STATE['tail'][:-_RUN_TAIL_LINES]

    tail_handler = _TailHandler()
    tail_handler.setLevel(_logging.INFO)
    tail_handler.setFormatter(
        _logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
    # Attach to the ``rss2email`` logger (not root) and temporarily
    # lower its level to INFO so the per-feed "refreshing feed" lines
    # reach the tail. Without this the logger (default level ERROR in
    # the web subprocess, which never got ``-V``) would drop INFO records
    # before any handler sees them.
    target_logger = _LOG
    orig_level = target_logger.level
    target_logger.addHandler(tail_handler)
    target_logger.setLevel(_logging.INFO)
    try:
        feeds = _feeds.Feeds(
            configfiles=list(configfiles) if configfiles else None,
            datafile_path=datafile_path)
        feeds.load()
        try:
            args = _Namespace(index=[], send=send, clean=False,
                            force_latest=force_latest)
            _command.run(feeds=feeds, args=args)
            _RUN_STATE['status'] = 'ok'
        except Exception as e:
            _RUN_STATE['status'] = 'error'
            _RUN_STATE['error'] = '{}: {}'.format(type(e).__name__, e)
            _LOG.error('background run failed:\n%s', _traceback.format_exc())
        finally:
            try:
                feeds.close()
            except Exception:
                pass
    except Exception as e:
        # Setup/teardown failure (couldn't acquire datafile lock, disk
        # error, etc.) -- still mark the run done so the UI stops saying
        # "running".
        _RUN_STATE['status'] = 'error'
        _RUN_STATE['error'] = '{}: {}'.format(type(e).__name__, e)
        _LOG.error('background run could not start:\n%s',
                   _traceback.format_exc())
    finally:
        target_logger.removeHandler(tail_handler)
        target_logger.setLevel(orig_level)
        _RUN_STATE['running'] = False
        _RUN_STATE['ended'] = _time.time()


def _start_run(send, force_latest, configfiles, datafile_path):
    """Try to start a background run. Return ``None`` on success, else an
    error string explaining why it didn't start (already running)."""
    if not _RUN_GUARD.acquire(blocking=False):
        return 'A run is already in progress. Wait for it to finish.'
    try:
        if _RUN_STATE['running']:
            return 'A run is already in progress. Wait for it to finish.'
        _reset_run_state(send=send, force_latest=force_latest)
    finally:
        _RUN_GUARD.release()
    t = _threading.Thread(
        target=_run_worker,
        args=(send, force_latest, configfiles, datafile_path),
        daemon=True)
    t.start()
    return None


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
                # While a background run holds the datafile lock, do not
                # try to load feeds here -- the lockf wait would freeze
                # the page until the run finishes. Render the run panel
                # alone so the user can still see the live status/tail.
                if _RUN_STATE['running']:
                    _write_html(self, _render_index(feeds=None))
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
                    elif path == '/run':
                        self._handle_run(send=True)
                    elif path == '/run-no-send':
                        self._handle_run(send=False)
                    elif path == '/run-force-latest':
                        self._handle_run(send=True, force_latest=True)
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
                    # DuplicateFeedName subclasses InvalidFeedName, so it
                    # must be caught first to get the right message.
                    raise _feeds_error_renderer(
                        'A feed named {!r} already exists.'.format(name))
                except _error.InvalidFeedName:
                    raise _feeds_error_renderer(
                        'Invalid feed name {!r}: names may contain '
                        'letters, digits, spaces, and '
                        '() ! ? + & , ; : \' " @ / ~ . _ -.'.format(name))
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

        def _handle_run(self, send, force_latest=False):
            # Kicking off a run does NOT touch the feeds list itself; it
            # spins up a background thread that opens its own ``Feeds``
            # instance (taking the datafile lockf just like a CLI
            # ``r2e run`` would). Reject a second concurrent click so two
            # button presses can't start twin runs that fight over the
            # datafile lock. The buttons stay enabled server-side; the
            # rejection is what prevents the user from queuing work.
            err = _start_run(
                send=send,
                force_latest=force_latest,
                configfiles=configfiles,
                datafile_path=datafile_path)
            if err is not None:
                # A run is already in progress (and holding the datafile
                # lock), so we must NOT open a Feeds instance here -- it
                # would block on the lockf until the run finishes, when
                # the whole point is to reject this second click at once.
                # Render the index in run-in-progress mode (table hidden),
                # with the rejection as an inline error.
                _write_html(self, _render_index(feeds=None, error=err))
                return
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