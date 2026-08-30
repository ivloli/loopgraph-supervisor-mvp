import os

from .adapters import CommandVerifier, DeepSeekHarnessAgent, FakeAgent, FakeVerifier
from .api import serve
from .ports import AgentExecutor, Verifier
from .store import SQLiteStore
from .supervisor import Supervisor


def build_supervisor() -> Supervisor:
    store = SQLiteStore(os.getenv("SUPERVISOR_DB", "supervisor.db"))
    mode = os.getenv("DSH_MODE", "sdk")
    agent: AgentExecutor
    verifier: Verifier
    if mode == "sdk":
        session_root = os.path.abspath(os.getenv("DSH_SESSION_ROOT", ".dsh-sessions"))
        agent = DeepSeekHarnessAgent(os.getcwd(), session_root, os.getenv("DSH_CORDIS"))
        verifier = CommandVerifier()
    elif mode == "fake":
        agent = FakeAgent()
        verifier = FakeVerifier(pass_on=2)
    else:
        raise ValueError("DSH_MODE must be 'sdk' or explicit demo mode 'fake'")
    return Supervisor(store, agent, verifier)


def main():
    serve(build_supervisor(), os.getenv("HOST", "127.0.0.1"), int(os.getenv("PORT", "8080")))


if __name__ == "__main__":
    main()
