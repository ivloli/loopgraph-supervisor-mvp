from __future__ import annotations

import hashlib
import importlib.metadata
import json
import shlex
import tempfile
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from .container_gate import PINNED_BUILDER_IMAGE, DockerBuilderSecurityGate
from .egress_relay import REVIEWED_DOCKER, DockerEgressRelay


@dataclass(frozen=True)
class LinuxRuntimeArtifact:
    version: str
    runtime_path: Path
    runtime_sha256: str
    rg_path: Path
    rg_sha256: str
    cordis_path: Path
    cordis_sha256: str
    wheel_path: Path
    wheel_sha256: str

    def verify(self) -> None:
        for path, expected in ((self.runtime_path, self.runtime_sha256), (self.rg_path, self.rg_sha256), (self.cordis_path, self.cordis_sha256)):
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError(f"Linux runtime artifact hash mismatch: {path.name}")
        if not self.wheel_path.is_file() or hashlib.sha256(self.wheel_path.read_bytes()).hexdigest() != self.wheel_sha256:
            raise ValueError("Linux runtime wheel hash mismatch")


@dataclass(frozen=True)
class RuntimeHandshakeReceipt:
    image: str
    sdk_version: str
    runtime_version: str
    runtime_sha256: str
    cordis_sha256: str
    server_name: str
    server_version: str
    network_mode: str
    passed: bool

    def receipt_hash(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class DockerSdkRuntime:
    """Creates a stdio Docker wrapper for a reviewed official Linux SDK runtime."""

    def __init__(self, root: str | Path, artifact: LinuxRuntimeArtifact, image: str = PINNED_BUILDER_IMAGE, docker: str = "/usr/local/bin/docker", relay: DockerEgressRelay | None = None):
        if Path(docker).resolve() != REVIEWED_DOCKER:
            raise ValueError("Docker SDK runtime requires the reviewed Docker executable")
        artifact.verify()
        raw_root = Path(root)
        if raw_root.is_symlink():
            raise ValueError("Docker SDK runtime root must not be a symlink")
        self.root = raw_root.resolve()
        if self.root.exists() and (not self.root.is_dir() or any(self.root.iterdir())):
            raise ValueError("Docker SDK runtime root must be a new empty non-symlink directory")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.artifact = artifact
        self.image = image
        self.docker = docker
        self.relay = relay
        if relay is not None and (not isinstance(relay, DockerEgressRelay) or not relay.active):
            raise ValueError("Docker SDK runtime requires a currently active egress relay")
        self.network = "none" if relay is None else relay.network
        self._authorized_network = self.network
        self.runtime_dir = artifact.runtime_path.parent.resolve()
        if artifact.rg_path.parent.resolve() != self.runtime_dir or artifact.cordis_path.parent.resolve() != self.runtime_dir:
            raise ValueError("Linux runtime, rg, and Cordis config must share one frozen directory")
        allowed = {artifact.runtime_path.name, artifact.rg_path.name, artifact.cordis_path.name}
        if {path.name for path in self.runtime_dir.iterdir()} != allowed:
            raise ValueError("Linux runtime directory contains unreviewed extra files")
        with tempfile.TemporaryDirectory(prefix="loopgraph-live-gate-") as gate_root:
            if not DockerBuilderSecurityGate(image, docker).probe(gate_root).passed:
                raise ValueError("live Docker Builder security gate did not pass")

    def wrapper(self) -> Path:
        if self.relay is not None and (not self.relay.active or self.relay.network != self._authorized_network):
            raise ValueError("Docker SDK runtime egress relay is no longer active")
        if self.network != self._authorized_network:
            raise ValueError("Docker SDK runtime network authorization was modified")
        workspace = self.root / "workspace"
        sessions = self.root / "sessions"
        home = self.root / "home"
        for directory in (workspace, sessions, home):
            if directory.exists() and directory.is_symlink():
                raise ValueError("Docker SDK runtime paths cannot be symlinks")
            directory.mkdir(parents=True, exist_ok=True)
            directory.chmod(0o777)
        name = f"loopgraph-sdk-{uuid.uuid4().hex}"
        nonce = uuid.uuid4().hex
        wrapper = self.root / f"docker-sdk-runtime-{nonce}"
        cidfile = self.root / f"docker-sdk-runtime-{nonce}.cid"
        command = [
            self.docker,
            "run",
            "--rm",
            "-i",
            "--name",
            name,
            "--cidfile",
            str(cidfile),
            f"--network={self.network}",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit=128",
            "--memory=512m",
            "--cpus=1",
            "--user=65534:65534",
            "--tmpfs=/tmp:rw,noexec,nosuid,size=64m",
            f"--mount=type=bind,src={self.runtime_dir},dst=/runtime,readonly",
            f"--mount=type=bind,src={workspace},dst=/workspace",
            f"--mount=type=bind,src={sessions},dst=/sessions",
            f"--mount=type=bind,src={home},dst=/home/builder",
            "-e",
            "HOME=/home/builder",
            "-e",
            "DSH_HOME=/home/builder",
            "-e",
            "DSH_CWD=/workspace",
            "-e",
            "DSH_SESSION_ROOT=/sessions",
            "-e",
            "DSH_CORDIS_CONFIG=/runtime/cordis.yml",
            "-e",
            "DEEPSEEK_API_KEY=builder-relay-placeholder",
            "-e",
            "DEEPSEEK_BASE_URL=http://egress:8080",
            self.image,
            "/runtime/dsh-jsonrpc-agent-pkg-linux-arm64",
        ]
        docker = shlex.quote(self.docker)
        script = "#!/bin/sh\ncleanup(){ if [ -s " + shlex.quote(str(cidfile)) + " ]; then " + docker + " rm -f \"$(cat " + shlex.quote(str(cidfile)) + ")\" >/dev/null 2>&1 || true; fi; }\ntrap cleanup EXIT INT TERM HUP\n" + " ".join(shlex.quote(item) for item in command) + ' "$@"\nstatus=$?\nexit $status\n'
        temporary = wrapper.with_suffix(".tmp")
        temporary.write_text(script)
        temporary.chmod(0o700)
        temporary.replace(wrapper)
        wrapper.chmod(0o700)
        return wrapper

    def probe_handshake(self) -> RuntimeHandshakeReceipt:
        from deepseek_harness.client import HarnessClient, HarnessConfig  # type: ignore[import-untyped]

        wrapper = self.wrapper()
        client = HarnessClient(HarnessConfig(runtime_bin=str(wrapper), cwd=str(self.root / "workspace"), env={}, request_timeout_seconds=30))
        try:
            client.start()
            initialized = client.initialize(cwd="/workspace", provider="deepseek-official", model="deepseek-v4-flash", max_tokens=64)
            server = initialized.serverInfo
            if server is None or server.name != "deepseek-harness-sdk-runtime" or server.version != "0.0.1":
                raise RuntimeError("Docker SDK runtime initialize response has an unexpected server identity")
            sdk_version = importlib.metadata.version("deepseek-harness-sdk")
            if sdk_version != self.artifact.version:
                raise RuntimeError("Python SDK and Linux runtime artifact versions differ")
            return RuntimeHandshakeReceipt(self.image, sdk_version, self.artifact.version, self.artifact.runtime_sha256, self.artifact.cordis_sha256, server.name, server.version, self.network, True)
        finally:
            client.close()


def write_runtime_handshake_receipt(path: str | Path, result: RuntimeHandshakeReceipt) -> None:
    document = {"schema_version": 1, "result": asdict(result), "receipt_hash": result.receipt_hash()}
    Path(path).write_text(json.dumps(document, indent=2, sort_keys=True))
