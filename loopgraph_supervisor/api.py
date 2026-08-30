import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .supervisor import Supervisor


class APIHandler(BaseHTTPRequestHandler):
    supervisor: Supervisor | None = None
    web_root = Path(__file__).with_name("web")

    def _json(self, status: int, payload):
        body = json.dumps(payload, ensure_ascii=False, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def _static(self, name: str, content_type: str):
        body = (self.web_root / name).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parts = [part for part in urlparse(self.path).path.split("/") if part]
        try:
            if self.supervisor is None:
                raise RuntimeError("supervisor is not configured")
            if not parts:
                self._static("index.html", "text/html; charset=utf-8")
                return
            if parts == ["app.js"]:
                self._static("app.js", "text/javascript; charset=utf-8")
                return
            if parts == ["styles.css"]:
                self._static("styles.css", "text/css; charset=utf-8")
                return
            if parts == ["workflows"]:
                self._json(200, {"workflows": self.supervisor.list_workflows()})
                return
            if len(parts) == 2 and parts[0] == "workflows" and parts[1]:
                self._json(200, self.supervisor.explain(parts[1]))
                return
            if parts == ["evolution", "runs"]:
                self._json(200, {"error": "run id required"})
                return
            if len(parts) == 3 and parts[0] == "evolution" and parts[1] == "runs":
                run = self.supervisor.evolution_runs.get(parts[2])
                if run is None:
                    self._json(404, {"error": parts[2]})
                else:
                    self._json(200, run.__dict__)
                return
            self._json(404, {"error": "route not found"})
        except Exception as exc:
            self._json(500, {"error": str(exc)})

    def do_POST(self):
        parts = [part for part in urlparse(self.path).path.split("/") if part]
        try:
            if self.supervisor is None:
                raise RuntimeError("supervisor is not configured")
            payload = self._body()
            result: object
            if self.path == "/evolution/triggers":
                result = self.supervisor.request_evolution(payload["target_id"], payload["reviewer"], payload["comment"])
            elif self.path == "/workflows":
                result = self.supervisor.start(payload["id"], payload["goal"], payload.get("max_attempts", 3), payload.get("acceptance", {}))
            elif len(parts) == 3 and parts[0] == "workflows":
                workflow_id, action = parts[1], parts[2]
                if action == "pause":
                    result = self.supervisor.pause(workflow_id)
                elif action == "resume":
                    result = self.supervisor.resume(workflow_id)
                elif action == "hitl":
                    result = self.supervisor.decide_hitl(workflow_id, payload["decision"])
                elif action == "recover":
                    result = self.supervisor.recover_uncertain(workflow_id, payload["action"])
                elif action == "rollback":
                    result = self.supervisor.rollback(workflow_id, payload["version_id"])
                else:
                    self._json(404, {"error": "route not found"})
                    return
            else:
                self._json(404, {"error": "route not found"})
                return
            self._json(201, getattr(result, "__dict__", result))
        except sqlite3.IntegrityError as exc:
            self._json(409, {"error": f"workflow already exists: {exc}"})
        except KeyError as exc:
            self._json(404, {"error": str(exc)})
        except Exception as exc:
            self._json(500, {"error": str(exc)})


def serve(supervisor: Supervisor, host: str = "127.0.0.1", port: int = 8080):
    APIHandler.supervisor = supervisor
    ThreadingHTTPServer((host, port), APIHandler).serve_forever()
