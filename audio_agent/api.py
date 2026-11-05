"""Localhost HTTP API for the TP UI (stdlib only).

Security model: bound to 127.0.0.1 only; browser calls are allowed only from origins in
agent.allowed_origins (CORS + Chrome Private Network Access preflight); optional shared
token in X-Agent-Token. There is no endpoint that prices, sends or confirms an order.

Endpoints
  GET  /health                       agent, sources, buffers, ASR status
  GET  /config                       effective config (token redacted)
  POST /replay        {"seconds": 30, "roles": ["broker"]}  or  {"target": "last_segment"}
  POST /replay/stop
  GET  /proposals                    current (unexpired) order proposals
  POST /proposals/<id>/ack  {"action": "loaded" | "dismissed"}
  GET  /events                       Server-Sent Events: proposal.new, proposal.closed,
                                     transcript, segment, replay.started/stopped, health, ...
"""
from __future__ import annotations

import json
import logging
import queue
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .agent import Agent

log = logging.getLogger(__name__)

ACK_RE = re.compile(r"^/proposals/([0-9a-f]{6,32})/ack$")


def make_handler(agent: Agent):
    class Handler(BaseHTTPRequestHandler):
        server_version = "TPAudioAgent/1"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # route through logging, not stderr
            log.debug("%s %s", self.address_string(), fmt % args)

        # ---- helpers -------------------------------------------------------
        def _origin_ok(self) -> bool:
            origin = self.headers.get("Origin")
            if origin is None:
                return True  # non-browser client on this machine (curl, tests)
            return origin in agent.cfg.agent.allowed_origins

        def _cors(self):
            origin = self.headers.get("Origin")
            if origin and origin in agent.cfg.agent.allowed_origins:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Agent-Token")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Private-Network", "true")

        def _auth_ok(self) -> bool:
            tok = agent.cfg.agent.token
            if not tok:
                return True
            if self.headers.get("X-Agent-Token") == tok:
                return True
            # EventSource can't set headers, so /events may carry ?token=
            if self.path.startswith("/events?"):
                from urllib.parse import parse_qs
                return parse_qs(self.path.split("?", 1)[1]).get("token", [None])[0] == tok
            return False

        def _json(self, code: int, body) -> None:
            data = json.dumps(body, default=str).encode()
            self.send_response(code)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 65536:
                raise ValueError("body too large")
            if n == 0:
                return {}
            raw = json.loads(self.rfile.read(n))
            if not isinstance(raw, dict):
                raise ValueError("body must be a JSON object")
            return raw

        def _guard(self) -> bool:
            if not self._origin_ok():
                self._json(403, {"error": "origin not allowed"})
                return False
            if not self._auth_ok():
                self._json(401, {"error": "bad or missing X-Agent-Token"})
                return False
            return True

        # ---- verbs -----------------------------------------------------------
        def do_OPTIONS(self):
            if not self._origin_ok():
                self.send_response(403)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(204)
            self._cors()
            self.send_header("Access-Control-Max-Age", "600")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            if not self._guard():
                return
            path = self.path.split("?", 1)[0]
            if path == "/health":
                return self._json(200, agent.health())
            if path == "/config":
                return self._json(200, agent.cfg.redacted())
            if path == "/proposals":
                return self._json(200, {"proposals": agent.proposals.list()})
            if path == "/events":
                return self._sse()
            return self._json(404, {"error": "not found"})

        def do_POST(self):
            if not self._guard():
                return
            path = self.path.split("?", 1)[0]
            try:
                body = self._body()
            except (ValueError, json.JSONDecodeError) as e:
                return self._json(400, {"error": f"bad body: {e}"})
            if path == "/replay":
                target = body.get("target", "last_seconds")
                if target not in ("last_seconds", "last_segment"):
                    return self._json(400, {"error": "target must be last_seconds or last_segment"})
                roles = body.get("roles")
                if roles is not None and (not isinstance(roles, list) or not all(isinstance(r, str) for r in roles)):
                    return self._json(400, {"error": "roles must be a list of strings"})
                seconds = body.get("seconds")
                if seconds is not None and not isinstance(seconds, (int, float)):
                    return self._json(400, {"error": "seconds must be a number"})
                try:
                    return self._json(200, agent.replay(seconds, roles, target))
                except (ValueError, LookupError) as e:
                    return self._json(409, {"error": str(e)})
                except Exception as e:
                    log.exception("replay failed")
                    return self._json(500, {"error": f"{type(e).__name__}: {e}"})
            if path == "/replay/stop":
                agent.stop_replay()
                return self._json(200, {"ok": True})
            m = ACK_RE.match(path)
            if m:
                action = body.get("action")
                if action not in ("loaded", "dismissed"):
                    return self._json(400, {"error": "action must be loaded or dismissed"})
                ok = agent.proposals.close(m.group(1), action)
                return self._json(200 if ok else 404, {"ok": ok})
            return self._json(404, {"error": "not found"})

        def _sse(self):
            q = agent.bus.subscribe()
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                # initial snapshot so a reconnecting UI is immediately consistent
                self._send_event("hello", {"proposals": agent.proposals.list(), "health": agent.health()})
                while not agent._stop.is_set():
                    try:
                        msg = q.get(timeout=15)
                        self._send_event(msg["event"], msg["data"])
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            finally:
                agent.bus.unsubscribe(q)
                self.close_connection = True

        def _send_event(self, event: str, data) -> None:
            payload = json.dumps(data, default=str)
            self.wfile.write(f"event: {event}\ndata: {payload}\n\n".encode())
            self.wfile.flush()

    return Handler


class ApiServer:
    def __init__(self, agent: Agent):
        self.agent = agent
        a = agent.cfg.agent
        self.httpd = ThreadingHTTPServer((a.host, a.port), make_handler(agent))
        self.httpd.daemon_threads = True
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="http", daemon=True)

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    def start(self):
        self._thread.start()
        log.info("API listening on http://%s:%d", self.agent.cfg.agent.host, self.port)

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
