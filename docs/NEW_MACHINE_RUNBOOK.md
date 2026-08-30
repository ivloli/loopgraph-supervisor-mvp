# New Machine Runbook

目标：在一台全新的 macOS 或 Linux 机器上下载、安装、测试并运行 LoopGraph Supervisor。

本 runbook 分成四条验证线：

```text
Track 0 依赖和静态验证
Track 1 A 版 Fake smoke test
Track 2 A 版真实 DeepSeek SDK
Track 3 B 版 DSH-native plugin
Track 4 Docker security/runtime gate
```

Track 0 和 Track 1 不需要 API key。Track 2、Track 3 的真实 DSH 调用需要凭据。当前仓库不把 DSH CLI 安装包、API key 或官方 runtime binary 提交进 Git。

## 1. 前置条件

### 必需

- Git 2.40+
- Python 3.11+
- `uv`
- Node.js 22+
- npm
- Docker Desktop（macOS）或 Docker Engine（Linux）
- `jq`（Track 2 JSON request helper）

### B 版额外需要

- 官方 `dsh` CLI；
- 一个可启动的 DSH `web` profile；
- DSH CLI 与 plugin peer dependency 兼容。

DSH CLI 不是 Python SDK 的一部分。A 版不需要 `dsh` CLI；B 版才需要。请使用 DeepSeek Harness 官方发布渠道安装 CLI，安装后确认：

```bash
dsh --version
dsh --help
```

如果新机器没有 `dsh`，可以先完成 A 版和 Docker Track；不要把 B 版的 CLI 缺失误判为 Python Supervisor 安装失败。

### 检查 Docker

```bash
docker version
docker info
```

`docker info` 必须成功。Linux 上当前用户应有 Docker 权限；如果需要 `sudo docker`，应先配置 Docker group，而不是修改项目脚本绕过权限。

## 2. 下载代码

```bash
git clone git@github.com:ivloli/loopgraph-supervisor.git
cd loopgraph-supervisor
```

如果目标机没有 GitHub SSH key，可以使用 HTTPS clone：

```bash
git clone https://github.com/ivloli/loopgraph-supervisor.git
cd loopgraph-supervisor
```

确认代码版本和工作区：

```bash
git status --short --branch
git log -1 --oneline
```

正式部署应固定到经过审核的 commit 或 tag，不要直接跟踪浮动的 `main`。

注意：当前本地工作区尚未创建首个 GitHub commit/remote；在第一次真实部署前，需要先把审核后的主项目 commit push 到这个地址，或者把上面 URL 替换成实际 remote。

## 3. 安装 A 版 Python 依赖

```bash
uv sync --extra dev --locked
```

验证依赖和质量工具：

```bash
uv run ruff check .
uv run mypy loopgraph_supervisor tests
uv run pytest -q
```

预期当前基线：

```text
Python tests: pass
Ruff: pass
mypy: pass
```

不要直接使用系统 `python`。统一使用 `uv run ...`，或者确认已激活当前仓库的 `.venv`。

## 4. 安装 B 版 plugin 依赖

```bash
cd packages/dsh-loopgraph-supervisor
npm ci
npm run build
npm test
cd ../..
```

这一步只构建和测试 plugin，不会自动安装到 DSH profile。

## 5. Track 1：A 版 Fake smoke test

先验证控制平面，不使用 API key：

```bash
DSH_MODE=fake \
SUPERVISOR_DB="$PWD/.run/supervisor-fake.db" \
PORT=8080 \
uv run python -m loopgraph_supervisor.main
```

另开一个终端：

```bash
cd /path/to/loopgraph-supervisor
curl -sS -X POST http://127.0.0.1:8080/workflows \
  -H 'content-type: application/json' \
  -d '{"id":"fake-1","goal":"produce a test artifact","max_attempts":3,"acceptance":{"commands":["true"]}}'

curl -sS http://127.0.0.1:8080/workflows/fake-1
```

预期：workflow 能创建、验证并返回可观察状态。Fake mode 只能证明控制平面状态机，不能证明真实 DSH。

停止：

```bash
Ctrl-C
```

## 6. Track 2：A 版真实 DeepSeek SDK

A 版直接使用 Python SDK，不需要先启动 DSH CLI：

```text
Python Supervisor -> deepseek-harness-sdk -> DeepSeek Harness runtime
```

凭据建议来自 secret manager。当前开发脚本读取 `DEEPSEEK_API_KEY`，临时本地运行可以：

```bash
read -r DEEPSEEK_API_KEY
export DEEPSEEK_API_KEY
```

不要把 key 写入命令历史、Git、日志或截图。

启动：

```bash
export DSH_MODE=sdk
export DSH_MODEL=deepseek-v4-flash
export DSH_SESSION_ROOT="$PWD/.run/dsh-sessions-a"
export SUPERVISOR_DB="$PWD/.run/supervisor-a.db"
export PORT=8080
uv run python -m loopgraph_supervisor.main
```

提交一个最小 coding workflow：

```bash
curl -sS -X POST http://127.0.0.1:8080/workflows \
  -H 'content-type: application/json' \
  -d "$(jq -n --arg workspace "$PWD/fixtures/rsi-sample" '{id:"real-1",goal:"Inspect the fixture and run the acceptance tests",max_attempts:2,acceptance:{workspace:$workspace,commands:["pytest -q"],allowed_files:["calculator.py","tests/test_calculator.py"]}}')"
```

观察：

```bash
curl -sS http://127.0.0.1:8080/workflows/real-1 | jq .
```

重点检查：

- DSH session id；
- current node/status；
- attempt；
- verifier evidence；
- Git scope；
- HITL request；
- version/rollback metadata。

停止并清理凭据：

```bash
unset DEEPSEEK_API_KEY
Ctrl-C
```

## 7. Track 3：B 版 DSH-native plugin

先把本地 plugin 添加到 DSH web profile：

```bash
git clone https://github.com/ivloli/loopgraph-supervisor.git
cd loopgraph-supervisor
```

检查输出中存在 `dsh-loopgraph-supervisor` 和 `dsh-doublecheck`。然后启动：

```bash
git status --short --branch
git log -1 --oneline
```

在 DSH session 中执行：

```text
/loop start {"goal":"Fix the failing tests and run the acceptance command","maxAttempts":2,"acceptance":{"commands":["pytest -q"],"allowedFiles":["src/app.py","tests/test_app.py"]}}
/loop status
/loop logs 20
/loop explain
```

验证 pause/recovery/HITL：

```text
/loop pause
/loop resume
/loop retry 请根据 verifier feedback 修正 candidate
/loop approve 已检查 diff、测试和证据，同意晋级
/loop rollback <workflow-owned-version-id>
```

B 的 `/loop evolve` 当前尚未接入；人工主动 RSI 入口属于 A：

```bash
uv run loopgraph evolve coding-supervisor \
  "Review the latest verifier feedback and propose a bounded LoopSpec improvement" \
  --reviewer DDHH
```

停止 B：

```bash
Ctrl-C
```

## 8. Track 4：Docker security/runtime gate

Docker gate 是 Builder/runtime 的隔离验证，不是 A 版 Python Supervisor 启动的前置条件。

固定镜像：

```bash
docker pull debian@sha256:88200866dfff7ea7f5cbcb6ec7c8a701889efe6fe859fe64d6990e4b07ea4171
docker pull node@sha256:c610fcdfb1d5b4740dd70c284ed3cb16bb857e0f7166196e36a5501df7a3aa32
```

运行 container gate：

```bash
LOOPGRAPH_DOCKER_E2E=1 uv run pytest -q tests/test_container_gate.py
```

运行 controlled egress gate：

```bash
LOOPGRAPH_DOCKER_EGRESS_E2E=1 uv run pytest -q tests/test_egress_relay.py
```

官方 Linux runtime handshake 还需要准备 exact-version artifact。运行前必须拥有：

```text
deepseek-harness-runtime-bin==0.1.1rc1 manylinux_2_28 Linux wheel
runtime binary
rg sidecar
Cordis config
每个文件的 SHA-256
```

准备后设置 artifact 目录和 wheel 路径：

```bash
export LOOPGRAPH_LINUX_RUNTIME_DIR=/absolute/path/to/frozen/runtime
export LOOPGRAPH_LINUX_RUNTIME_WHEEL=/absolute/path/to/runtime.whl
export LOOPGRAPH_DOCKER_RUNTIME_E2E=1
uv run pytest -q tests/test_docker_runtime.py
```

没有完整 artifact 时，Docker runtime E2E 应跳过或 fail closed，不要用 host binary 替代 Linux runtime。

## 9. Test/Coverage Gate

普通仓库验收：

```bash
uv run pytest -q
```

启用显式 quality contract 时，格式为：

```json
{
  "python_test_gate": {
    "command": ["python", "-m", "pytest", "-q"]
  },
  "coverage_gate": {
    "command": ["python", "-m", "coverage", "run", "-m", "pytest", "-q"],
    "report": ".run/coverage.json",
    "report_command": ["python", "-m", "coverage", "json", "-o", ".run/coverage.json"],
    "minimum_percent": 85,
    "baseline_percent": 85,
    "max_regression_percent": 0,
    "require_branch": true,
    "branch_minimum_percent": 80
  }
}
```

Test/Coverage Gate 是 Host-owned evidence。candidate 不能修改测试、coverage policy、report 或 proof。

## 10. 完整发布前检查

```bash
uv run ruff check .
uv run mypy loopgraph_supervisor tests
uv run pytest -q

cd packages/dsh-loopgraph-supervisor
npm ci
npm run build
npm test
cd ../..

git diff --check
```

真实 RSI 发布还必须确认：

- candidate 基于当前 canonical Git parent；
- LoopSpec Graph Gate 通过；
- Test/Coverage/Verifier/Canary 通过；
- candidate 不读取 holdout；
- PR head 与 approval 绑定；
- merge commit 已观察到；
- active version 已验证；
- post-activation canary 通过；
- rollback callback 后 active pointer 已验证恢复。

## 11. 停止和清理

```bash
tmux kill-session -t loopgraph 2>/dev/null || true
docker ps --filter 'name=loopgraph-' --format '{{.ID}} {{.Names}}'
```

只停止本项目容器：

```bash
containers=$(docker ps -q --filter 'name=loopgraph-')
if [ -n "$containers" ]; then docker rm -f $containers; fi
networks=$(docker network ls --format '{{.Name}}' | awk '/^loopgraph-/')
if [ -n "$networks" ]; then docker network rm $networks; fi
```

清理本地运行状态：

```bash
rm -rf .run .dsh-sessions-a
unset DEEPSEEK_API_KEY DSH_MODE DSH_MODEL DSH_SESSION_ROOT SUPERVISOR_DB PORT
```

不要删除 Git worktree 或持久化 evidence，除非已经完成审计或明确要清理。

## 12. 常见失败

### `python: command not found`

使用：

```bash
uv run python -m loopgraph_supervisor.main
```

或激活：

```bash
source .venv/bin/activate
```

### A 启动时报缺少 API key

先使用 `DSH_MODE=fake` 验证控制平面；真实 SDK 运行需要安全地注入 `DEEPSEEK_API_KEY`。

### B 时报 profile/plugin 错误

依次检查：

```bash
```

### Docker runtime 报 native module 或架构错误

确认：

- Linux runtime 是目标架构的 manylinux artifact；
- Debian/glibc substrate，不是 Alpine/musl；
- runtime directory 只有已审查文件；
- wheel 和 binary SHA-256 正确；
- 不要用 macOS Mach-O binary 替代 Linux binary。
