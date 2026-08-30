from __future__ import annotations

import hashlib
import json
import os
import subprocess
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from .container_gate import PINNED_NODE_IMAGE

REVIEWED_DOCKER = Path("/usr/local/bin/docker").resolve()

RELAY_SCRIPT = r"""
const http=require('http'),https=require('https'),fs=require('fs');
const allowed=new Set(['/chat/completions','/v1/chat/completions']);
let requests=0,active=0;
http.createServer((req,res)=>{
 if(req.method==='GET'&&req.url==='/health'){res.writeHead(200);return res.end('ok')}
 const path=new URL(req.url,'http://relay').pathname;
 if(req.method!=='POST'||!allowed.has(path)){res.writeHead(403);return res.end('forbidden')}
 let chunks=[],size=0;req.on('data',c=>{size+=c.length;if(size>2097152)req.destroy();else chunks.push(c)});
 req.on('end',()=>{let body;try{body=JSON.parse(Buffer.concat(chunks))}catch{return res.writeHead(400).end('invalid json')};if(body.model!=='deepseek-v4-flash'||!Array.isArray(body.messages)||!Number.isInteger(body.max_tokens)||body.max_tokens<1||body.max_tokens>8192)return res.writeHead(403).end('request outside budget');if(requests>=16||active>=1)return res.writeHead(429).end('builder budget exhausted');requests++;active++;
  const headers={'content-type':'application/json','accept':req.headers['accept']||'*/*'};const keyPath='/run/secrets/deepseek_api_key';if(fs.existsSync(keyPath))headers.authorization='Bearer '+fs.readFileSync(keyPath,'utf8').trim();let finished=false;const finish=()=>{if(!finished){finished=true;active--}};
  const up=https.request({hostname:'api.deepseek.com',port:443,path:req.url,method:'POST',headers,timeout:30000},r=>{res.writeHead(r.statusCode||502,{'content-type':r.headers['content-type']||'application/json'});r.on('end',finish);r.pipe(res)});
  up.on('timeout',()=>up.destroy(new Error('timeout')));up.on('error',e=>{finish();if(!res.headersSent)res.writeHead(502);res.end('upstream error')});up.end(JSON.stringify(body));});
}).listen(8080,'0.0.0.0');
"""


@dataclass(frozen=True)
class EgressProbeResult:
    image: str
    relay_script_hash: str
    direct_network_denied: bool
    relay_health_ok: bool
    disallowed_path_denied: bool
    deepseek_path_reached_upstream: bool
    bridge_peer_denied: bool
    credential_not_in_metadata: bool
    relay_non_root: bool
    out_of_budget_request_denied: bool
    passed: bool

    def document(self) -> dict[str, str | bool]:
        return asdict(self)

    def receipt_hash(self) -> str:
        return hashlib.sha256(json.dumps(self.document(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class DockerEgressRelay:
    def __init__(self, docker: str = "/usr/local/bin/docker", image: str = PINNED_NODE_IMAGE, api_key: str | None = None, api_key_file: str | Path | None = None):
        if Path(docker).resolve() != REVIEWED_DOCKER:
            raise ValueError("egress relay requires the reviewed Docker executable")
        if image != PINNED_NODE_IMAGE:
            raise ValueError("egress relay requires the pinned relay image")
        if api_key is not None and api_key_file is not None:
            raise ValueError("provide api_key or api_key_file, not both")
        self.docker = docker
        self.image = image
        self.network = f"loopgraph-internal-{uuid.uuid4().hex}"
        self.external_network = f"loopgraph-egress-{uuid.uuid4().hex}"
        self.container = f"loopgraph-relay-{uuid.uuid4().hex}"
        self.api_key = api_key
        self.api_key_file = Path(api_key_file).resolve() if api_key_file is not None else None
        self.active = False
        self.secret_path: str | None = None

    def __enter__(self) -> DockerEgressRelay:
        self._run("network", "create", "--internal", self.network)
        self._run("network", "create", self.external_network)
        key_args: list[str] = []
        if self.api_key_file is not None:
            stat = self.api_key_file.stat()
            if not self.api_key_file.is_file() or stat.st_mode & 0o077 or stat.st_uid != os.getuid():
                raise ValueError("API key file must be a mode-0600 file owned by the current user")
            self.secret_path = str(self.api_key_file)
            key_args = [f"--mount=type=bind,src={self.api_key_file},dst=/run/secrets/deepseek_api_key,readonly"]
        elif self.api_key is not None:
            from tempfile import NamedTemporaryFile

            secret = NamedTemporaryFile("w", prefix="loopgraph-relay-key-", delete=False)
            secret.write(self.api_key)
            secret.close()
            Path(secret.name).chmod(0o600)
            self.secret_path = secret.name
            key_args = [f"--mount=type=bind,src={secret.name},dst=/run/secrets/deepseek_api_key,readonly"]
        try:
            self._run("run", "-d", "--rm", "--name", self.container, "--network", self.external_network, "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges", f"--user={os.getuid()}:{os.getgid()}", "--pids-limit=64", "--memory=128m", "--cpus=0.25", "--tmpfs=/tmp:rw,noexec,nosuid,size=16m", *key_args, self.image, "node", "-e", RELAY_SCRIPT)
            self._run("network", "connect", "--alias", "egress", self.network, self.container)
            self.active = True
        except Exception:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        subprocess.run([self.docker, "rm", "-f", self.container], capture_output=True)
        subprocess.run([self.docker, "network", "rm", self.network], capture_output=True)
        subprocess.run([self.docker, "network", "rm", self.external_network], capture_output=True)
        if self.secret_path and self.api_key is not None:
            Path(self.secret_path).unlink(missing_ok=True)
        self.active = False

    def probe(self) -> EgressProbeResult:
        script = r"""
const request=(path,method='GET',body)=>new Promise(resolve=>{const r=require('http').request({host:'egress',port:8080,path,method,headers:{'content-type':'application/json'}},x=>{x.resume();x.on('end',()=>resolve(x.statusCode))});r.on('error',()=>resolve(0));r.end(method==='POST'?JSON.stringify(body||{model:'deepseek-v4-flash',messages:[{role:'user',content:'probe'}],max_tokens:1}):undefined)});
(async()=>{let directDenied=false;try{await fetch('https://example.com',{signal:AbortSignal.timeout(2000)})}catch{directDenied=true};const health=await request('/health');const denied=await request('/not-allowed','POST');const budget=await request('/chat/completions','POST',{model:'other',messages:[],max_tokens:1});const upstream=await request('/chat/completions','POST');console.log(JSON.stringify({directDenied,health,denied,budget,upstream}))})();
"""
        result = subprocess.run([self.docker, "run", "--rm", "--network", self.network, self.image, "node", "-e", script], capture_output=True, text=True, timeout=45, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"egress probe failed: {result.stderr[-1000:]}")
        outcome = json.loads(result.stdout.strip().splitlines()[-1])
        relay_ip = self._run("inspect", "-f", f'{{{{(index .NetworkSettings.Networks "{self.external_network}").IPAddress}}}}', self.container)
        bridge_peer = subprocess.run([self.docker, "run", "--rm", "--network", "bridge", self.image, "node", "-e", f"fetch('http://{relay_ip}:8080/health',{{signal:AbortSignal.timeout(1500)}}).then(()=>process.exit(1)).catch(()=>process.exit(0))"], capture_output=True, timeout=15, check=False)
        inspect = subprocess.run([self.docker, "inspect", self.container], capture_output=True, text=True, timeout=15, check=False)
        metadata = json.loads(inspect.stdout)[0]
        values = {
            "image": self.image,
            "relay_script_hash": hashlib.sha256(RELAY_SCRIPT.encode()).hexdigest(),
            "direct_network_denied": outcome["directDenied"],
            "relay_health_ok": outcome["health"] == 200,
            "disallowed_path_denied": outcome["denied"] == 403,
            "deepseek_path_reached_upstream": 200 <= outcome["upstream"] < 300 or outcome["upstream"] in {400, 401, 422},
            "bridge_peer_denied": bridge_peer.returncode == 0,
            "credential_not_in_metadata": self.api_key is None or self.api_key not in inspect.stdout,
            "relay_non_root": metadata["Config"]["User"] not in {"", "0", "0:0"},
            "out_of_budget_request_denied": outcome["budget"] == 403,
        }
        return EgressProbeResult(**values, passed=all(value for key, value in values.items() if key not in {"image", "relay_script_hash"}))

    def _run(self, *args: str) -> str:
        result = subprocess.run([self.docker, *args], capture_output=True, text=True, timeout=60, check=False)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f"docker {' '.join(args)} failed")
        return result.stdout.strip()


def write_egress_receipt(path: str, result: EgressProbeResult) -> None:
    with open(path, "w") as destination:
        json.dump({"schema_version": 1, "result": result.document(), "receipt_hash": result.receipt_hash()}, destination, indent=2, sort_keys=True)


def load_egress_receipt(path: str) -> EgressProbeResult:
    with open(path) as source:
        document = json.load(source)
    result = EgressProbeResult(**document["result"])
    checks = [value for key, value in result.document().items() if key not in {"image", "relay_script_hash", "passed"}]
    expected_script = hashlib.sha256(RELAY_SCRIPT.encode()).hexdigest()
    if document.get("schema_version") != 1 or document.get("receipt_hash") != result.receipt_hash() or result.image != PINNED_NODE_IMAGE or result.relay_script_hash != expected_script or not result.passed or not all(checks):
        raise ValueError("Builder egress gate receipt is invalid")
    return result
