#!/usr/bin/env python3
"""Serve disposable repository generations without stale mtime-only validators."""

import http.server
import socketserver


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    # EL8's Python 3.6 has no http.server.ThreadingHTTPServer.
    daemon_threads = True


class Repository(http.server.SimpleHTTPRequestHandler):
    def send_head(self):
        # The URL stays fixed while the test switches generations. Two signed
        # indexes can have the same one-second mtime but different contents.
        # A 304 in that case silently keeps the client's old package inventory.
        if 'If-Modified-Since' in self.headers:
            del self.headers['If-Modified-Since']
        return super().send_head()


if __name__ == '__main__':
    ThreadingHTTPServer(('0.0.0.0', 8080), Repository).serve_forever()
