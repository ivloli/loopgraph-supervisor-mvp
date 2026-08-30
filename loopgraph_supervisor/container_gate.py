from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

PINNED_NODE_IMAGE = "node@sha256:c610fcdfb1d5b4740dd70c284ed3cb16bb857e0f7166196e36a5501df7a3aa32"
PINNED_BUILDER_IMAGE = "debian@sha256:88200866dfff7ea7f5cbcb6ec7c8a701889efe6fe859fe64d6990e4b07ea4171"


@dataclass(frozen=True)
class ContainerGateResult:
    image: str
    workspace_write_allowed: bool
    read_only_context_preserved: bool
    read_only_context_write_denied: bool
    rootfs_write_denied: bool
    host_path_absent: bool
    holdout_absent: bool
    sibling_absent: bool
    network_denied: bool
    non_root: bool
    docker_socket_absent: bool
    passed: bool

    def document(self) -> dict[str, str | bool]:
        return asdict(self)

    def receipt_hash(self) -> str:
        return hashlib.sha256(json.dumps(self.document(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class DockerBuilderSecurityGate:
    def __init__(self, image: str = PINNED_NODE_IMAGE, docker: str = "docker"):
        if "@sha256:" not in image:
            raise ValueError("Builder container image must be pinned by digest")
        self.image = image
        self.docker = docker

    def probe(self, root: str | Path) -> ContainerGateResult:
        root = Path(root).resolve()
        workspace = root / "workspace"
        context = root / "context"
        holdout = root / "holdout"
        sibling = root / "sibling"
        for directory in (workspace, context, holdout, sibling):
            directory.mkdir(parents=True, exist_ok=False)
        workspace.chmod(0o777)
        (context / "visible.json").write_text('{"visible":true}')
        (holdout / "answer.txt").write_text("secret")
        (sibling / "baseline.txt").write_text("baseline")
        script = """
bool(){ if "$@"; then printf true; else printf false; fi; }
workspaceWrite=false; if printf ok > /workspace/write.txt 2>/dev/null; then workspaceWrite=true; fi
contextWriteDenied=false; if ! printf bad > /context/write.txt 2>/dev/null; then contextWriteDenied=true; fi
rootfsWriteDenied=false; if ! printf bad > /rootfs-write.txt 2>/dev/null; then rootfsWriteDenied=true; fi
networkDenied=true; if [ -e /sys/class/net/eth0 ]; then networkDenied=false; fi
printf '{"workspaceWrite":%s,"context":%s,"contextWriteDenied":%s,"rootfsWriteDenied":%s,"host":%s,"holdout":%s,"sibling":%s,"socket":%s,"uid":%s,"networkDenied":%s}\n' "$workspaceWrite" "$(bool test -e /context/visible.json)" "$contextWriteDenied" "$rootfsWriteDenied" "$(bool test -e /Users)" "$(bool test -e /holdout)" "$(bool test -e /sibling)" "$(bool test -e /var/run/docker.sock)" "$(id -u)" "$networkDenied"
"""
        name = f"loopgraph-gate-{uuid.uuid4().hex}"
        command = [
            self.docker,
            "run",
            "--rm",
            "--name",
            name,
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit=64",
            "--memory=256m",
            "--cpus=0.5",
            "--user=65534:65534",
            "--tmpfs=/tmp:rw,noexec,nosuid,size=32m",
            "--tmpfs=/sessions:rw,noexec,nosuid,size=32m,uid=65534,gid=65534,mode=0700",
            "--tmpfs=/home/builder:rw,noexec,nosuid,size=32m,uid=65534,gid=65534,mode=0700",
            f"--mount=type=bind,src={workspace},dst=/workspace",
            f"--mount=type=bind,src={context},dst=/context,readonly",
            self.image,
            "/bin/sh",
            "-c",
            script,
        ]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
        except subprocess.TimeoutExpired:
            subprocess.run([self.docker, "rm", "-f", name], capture_output=True, check=False)
            raise RuntimeError("Builder container security probe timed out")
        if result.returncode != 0:
            raise RuntimeError(f"Builder container security probe failed: {result.stderr[-2000:]}")
        outcome = json.loads(result.stdout.strip().splitlines()[-1])
        values = {
            "image": self.image,
            "workspace_write_allowed": outcome["workspaceWrite"],
            "read_only_context_preserved": outcome["context"],
            "read_only_context_write_denied": outcome["contextWriteDenied"],
            "rootfs_write_denied": outcome["rootfsWriteDenied"],
            "host_path_absent": not outcome["host"],
            "holdout_absent": not outcome["holdout"],
            "sibling_absent": not outcome["sibling"],
            "network_denied": outcome["networkDenied"],
            "non_root": outcome["uid"] != 0,
            "docker_socket_absent": not outcome["socket"],
        }
        return ContainerGateResult(**values, passed=all(value for key, value in values.items() if key != "image"))


def write_container_gate_receipt(path: str | Path, result: ContainerGateResult) -> None:
    document = {"schema_version": 1, "result": result.document(), "receipt_hash": result.receipt_hash()}
    Path(path).write_text(json.dumps(document, indent=2, sort_keys=True))


def load_container_gate_receipt(path: str | Path, expected_image: str) -> ContainerGateResult:
    document = json.loads(Path(path).read_text())
    result = ContainerGateResult(**document["result"])
    if document.get("schema_version") != 1 or document.get("receipt_hash") != result.receipt_hash():
        raise ValueError("Builder container gate receipt is invalid")
    checks = [value for key, value in asdict(result).items() if key not in {"image", "passed"}]
    if result.image != expected_image or not result.passed or not all(checks):
        raise ValueError("Builder container gate receipt does not authorize this image")
    return result
