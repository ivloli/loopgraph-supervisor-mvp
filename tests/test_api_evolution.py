import json
from http.client import HTTPConnection
from threading import Thread

from loopgraph_supervisor.adapters import FakeAgent, FakeVerifier
from loopgraph_supervisor.api import APIHandler, ThreadingHTTPServer
from loopgraph_supervisor.store import SQLiteStore
from loopgraph_supervisor.supervisor import Supervisor


def test_evolution_endpoint_persists_human_request(tmp_path):
    store = SQLiteStore(":memory:")
    supervisor = Supervisor(store, FakeAgent(["candidate"]), FakeVerifier(1))
    APIHandler.supervisor = supervisor
    server = ThreadingHTTPServer(("127.0.0.1", 0), APIHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = HTTPConnection(str(server.server_address[0]), int(server.server_address[1]))
        connection.request("POST", "/evolution/triggers", json.dumps({"target_id": "coding-supervisor", "reviewer": "DDHH", "comment": "Review retry policy"}), {"Content-Type": "application/json"})
        response = connection.getresponse()
        payload = json.loads(response.read())
    finally:
        server.shutdown()
        thread.join()
        server.server_close()

    assert response.status == 201
    assert payload["status"] == "EVOLUTION_REQUESTED"
    assert supervisor.evolution_triggers.get(payload["trigger_id"]) is not None
