import hashlib
import json
from threading import Lock, RLock
from typing import Any

from .domain import AgentInput, AgentOutput, DecisionRecord, ImprovementProposal, Node, Version, Workflow, WorkflowStatus, utc_now
from .evolution_run import EvolutionRunStore
from .evolution_trigger import EvolutionTriggerStore
from .git_workspace import GitWorkspace
from .loopspec import LoopSpec, coding_spec_chain, load_loopspec
from .loopspec_interpreter import LoopSpecInterpreter
from .ports import AgentExecutor, Verifier
from .proposal_worker import EvolutionProposalWorker, ProposalResult
from .spec_store import LoopSpecStore
from .store import SQLiteStore, decode
from .workspace import WorkspaceManager


class Supervisor:
    """Durable state machine that controls DSH execution and explains decisions."""

    def __init__(self, store: SQLiteStore, agent: AgentExecutor, verifier: Verifier, workspaces: WorkspaceManager | None = None, loopspec_path: str | None = None):
        self.store = store
        self.agent = agent
        self.verifier = verifier
        self.workspaces = workspaces or WorkspaceManager()
        self._locks: dict[str, RLock] = {}
        self._locks_guard = Lock()
        self.specs = LoopSpecStore(store)
        self.evolution_triggers = EvolutionTriggerStore(store)
        self.evolution_runs = EvolutionRunStore(store)
        initial_specs = (load_loopspec(loopspec_path),) if loopspec_path else coding_spec_chain()
        self.default_spec = initial_specs[-1]
        self._validate_runtime_nodes(self.default_spec)
        if self.specs.active(self.default_spec.spec_id) is None:
            self.specs.save(initial_specs[0], status="ACTIVE")
            for spec in initial_specs[1:]:
                self.specs.save(spec, status="CANDIDATE")
                self.specs.save(spec, status="ACTIVE", allow_human_activation=True)

    def _workflow_lock(self, workflow_id: str) -> RLock:
        with self._locks_guard:
            return self._locks.setdefault(workflow_id, RLock())

    def start(self, workflow_id: str, goal: str, max_attempts: int = 3, acceptance: dict[str, Any] | None = None) -> Workflow:
        contract = dict(acceptance or {})
        source_workspace = contract.get("workspace")
        if source_workspace and contract.get("isolate", True):
            contract["source_workspace"] = source_workspace
            contract["workspace"] = self.workspaces.prepare(workflow_id, source_workspace)
            contract["isolated"] = True
        workflow = Workflow(workflow_id, goal, max_attempts=max_attempts, acceptance=contract)
        spec = self.specs.active("coding-supervisor") or self.default_spec
        if max_attempts > spec.max_iterations:
            raise ValueError(f"requested max_attempts {max_attempts} exceeds active LoopSpec limit {spec.max_iterations}")
        workflow.spec_id = spec.spec_id
        workflow.spec_revision = spec.revision
        workflow.spec_hash = spec.content_hash()
        workspace = workflow.acceptance.get("workspace")
        baseline_sha = ""
        if workspace and GitWorkspace(workspace).available:
            baseline_sha = GitWorkspace(workspace).head()
            workflow.acceptance["baseline_sha"] = baseline_sha
        workflow.acceptance["spec_revision"] = self._spec_revision(goal, max_attempts, workflow.acceptance)
        self.store.create_workflow(workflow)
        self.store.save_contract(workflow_id, workflow.acceptance)
        if workspace and GitWorkspace(workspace).available:
            baseline_id = f"version:{workflow_id}:baseline"
            GitWorkspace(workspace).tag(f"loopgraph-{workflow_id}-baseline", baseline_sha)
            self.store.save_version(Version(baseline_id, workflow_id, "", {"baseline_commit": baseline_sha}, status="BASELINE"))
            workflow.active_version = baseline_id
            self.store.save_workflow(workflow)
        self.store.append_event(workflow_id, "workflow_started", to_node=Node.EXECUTE.value, payload={"goal": goal})
        self._decision(workflow, "PLAN", "Why should this workflow start?", "start", ["The user supplied a new goal"], [], [], "No Agent action has happened yet", "Create a durable workflow before invoking DSH")
        return self.run(workflow_id)

    def request_evolution(self, target_id: str, reviewer: str, comment: str) -> dict[str, object]:
        trigger_id, trigger = self.evolution_triggers.create_human_request(target_id, reviewer, comment)
        return {"status": "EVOLUTION_REQUESTED", "trigger_id": trigger_id, "target_id": trigger.target_id, "source": trigger.source, "reason": trigger.reason, "reviewer": reviewer, "comment": comment}

    def consume_evolution(self, worker: EvolutionProposalWorker, trigger_id: str) -> ProposalResult:
        return worker.consume(trigger_id)

    def run(self, workflow_id: str) -> Workflow:
        with self._workflow_lock(workflow_id):
            workflow = self.store.get_workflow(workflow_id)
            while workflow.status == WorkflowStatus.RUNNING:
                if workflow.pause_requested:
                    workflow.status = WorkflowStatus.PAUSED
                    self._transition(workflow, WorkflowStatus.PAUSED, "pause_requested", workflow.current_node)
                    break
                if workflow.current_node == Node.EXECUTE:
                    self._execute(workflow)
                elif workflow.current_node == Node.VERIFY:
                    self._verify(workflow)
                elif workflow.current_node == Node.PROMOTE:
                    self._promote(workflow)
                else:
                    raise ValueError(f"unsupported executable node: {workflow.current_node}")
                workflow = self.store.get_workflow(workflow_id)
            return workflow

    def pause(self, workflow_id: str) -> Workflow:
        workflow = self.store.get_workflow(workflow_id)
        workflow.pause_requested = True
        self.store.save_workflow(workflow)
        self.store.append_event(workflow_id, "pause_requested", to_node=workflow.current_node.value)
        return workflow

    def resume(self, workflow_id: str) -> Workflow:
        workflow = self.store.get_workflow(workflow_id)
        workflow.pause_requested = False
        if workflow.status == WorkflowStatus.PAUSED:
            workflow.status = WorkflowStatus.RUNNING
        self.store.save_workflow(workflow)
        self.store.append_event(workflow_id, "resumed", to_node=workflow.current_node.value)
        return self.run(workflow_id)

    def decide_hitl(self, workflow_id: str, decision: str) -> Workflow:
        with self._workflow_lock(workflow_id):
            workflow = self.store.get_workflow(workflow_id)
            request = self.store.open_hitl(workflow_id)
            if request is None:
                raise ValueError("no open HITL request")
            if decision not in {"approve", "retry", "reject"}:
                raise ValueError(f"unsupported HITL decision: {decision}")
            context = decode(request["context"], {})
            if decision == "approve":
                if request["reason"] != "promotion_review":
                    raise ValueError("approve requires a verified promotion_review request")
                current_revision = self._spec_revision(workflow.goal, workflow.max_attempts, workflow.acceptance)
                if context.get("attempt") != workflow.attempt or context.get("spec_revision") != workflow.acceptance.get("spec_revision") or current_revision != workflow.acceptance.get("spec_revision"):
                    raise ValueError("approval request does not match the current attempt and spec revision")
                workspace = workflow.acceptance.get("workspace")
                if not workspace or GitWorkspace(workspace).candidate_fingerprint() != context.get("candidate_fingerprint"):
                    raise ValueError("candidate changed after review; re-run verification before approval")
                if context.get("loop_spec_revision") != workflow.spec_revision or context.get("loop_spec_hash") != workflow.spec_hash:
                    raise ValueError("approval request does not match the workflow LoopSpec")
                workflow.status, workflow.current_node = WorkflowStatus.RUNNING, self._node(workflow, self._spec_next(workflow, "hitl", "approve", workflow.attempt - 1))
            elif decision == "retry":
                if workflow.attempt >= workflow.max_attempts:
                    raise ValueError("HITL retry budget is exhausted")
                workflow.status, workflow.current_node = WorkflowStatus.RUNNING, self._node(workflow, self._spec_next(workflow, "hitl", "retry", workflow.attempt - 1))
            else:
                workflow.status, workflow.current_node = WorkflowStatus.FAILED, self._node(workflow, self._spec_next(workflow, "hitl", "reject", workflow.attempt - 1))
            self.store.resolve_hitl(request["id"], decision)
            self._decision(workflow, "HITL", "What should happen after human review?", decision, [f"Human selected {decision}"], [{"hitl_request": request["id"], "attempt": context.get("attempt"), "spec_revision": context.get("spec_revision"), "candidate_fingerprint": context.get("candidate_fingerprint")}], [{"option": option, "rejected_because": "not selected"} for option in ("approve", "retry", "reject") if option != decision], "Human decision overrides automatic policy", f"Move workflow to {workflow.current_node.value}")
            self.store.save_workflow(workflow)
        return self.run(workflow_id)

    def recover_uncertain(self, workflow_id: str, action: str) -> Workflow:
        with self._workflow_lock(workflow_id):
            workflow = self.store.get_workflow(workflow_id)
            if workflow.status != WorkflowStatus.UNCERTAIN:
                raise ValueError("recovery action requires UNCERTAIN workflow status")
            intent = self.store.get_open_execution(workflow_id)
            if intent is None:
                raise ValueError("uncertain workflow has no open execution intent")
            token = intent["token"]
            workspace = workflow.acceptance.get("workspace")
            if action == "verify-existing":
                self.store.save_attempt(workflow.id, intent["attempt"], token, decode(intent["request"], {}), {"response": "Recovered uncertain workspace for independent verification", "recovered": True}, f"workflow-{workflow.id}")
                self.store.finish_execution(token, "RECOVERED")
                workflow.attempt = intent["attempt"]
                workflow.status, workflow.current_node = WorkflowStatus.RUNNING, Node.VERIFY
                self._decision(workflow, "UNCERTAIN_VERIFY", "Why verify the existing workspace?", "verify_existing", ["The prior DSH result is missing", "Workspace evidence may already contain the candidate"], [{"execution_token": token}], [], "Verification may find a partial candidate", "Run independent acceptance checks without re-invoking DSH")
            elif action == "retry-same-attempt":
                self.store.set_execution_status(token, "RETRY_APPROVED")
                workflow.attempt = intent["attempt"]
                workflow.status, workflow.current_node = WorkflowStatus.RUNNING, Node.EXECUTE
                self._decision(workflow, "UNCERTAIN_RETRY", "Why retry the same execution identity?", "retry_same_attempt", ["A human explicitly accepted possible duplicate external effects"], [{"execution_token": token}], [], "DSH may repeat an earlier side effect", "Re-invoke DSH with the same attempt and session identity")
            elif action == "restore-baseline":
                if not workspace:
                    raise ValueError("baseline restore requires a workspace")
                restored = GitWorkspace(workspace).restore_contract_paths(workflow.acceptance.get("baseline_sha", "HEAD"), workflow.acceptance.get("allowed_files", []))
                self.store.finish_execution(token, "RESTORED")
                workflow.status, workflow.current_node = WorkflowStatus.FAILED, Node.FAILED
                self._decision(workflow, "UNCERTAIN_RESTORE", "Why restore the baseline?", "restore_baseline", ["A human chose to discard uncertain external effects"], [{"restored_files": restored, "execution_token": token}], [], "The uncertain candidate is discarded", "Return the isolated worktree to the recorded baseline")
            elif action == "abort-preserve":
                self.store.finish_execution(token, "ABORTED_UNCERTAIN")
                workflow.status, workflow.current_node = WorkflowStatus.FAILED, Node.FAILED
                self._decision(workflow, "UNCERTAIN_ABORT", "Why preserve the uncertain workspace?", "abort_preserve", ["A human chose forensic preservation over automatic cleanup"], [{"workspace": workspace, "execution_token": token}], [], "The worktree remains dirty and must be handled manually", "Stop execution without hiding possible side effects")
            else:
                raise ValueError("unsupported recovery action")
            self.store.save_workflow(workflow)
            self.store.append_event(workflow.id, "uncertain_recovery", to_node=workflow.current_node.value, payload={"action": action, "token": token})
        return self.run(workflow_id) if workflow.status == WorkflowStatus.RUNNING else workflow

    def rollback(self, workflow_id: str, version_id: str) -> Workflow:
        workflow = self.store.get_workflow(workflow_id)
        version = self.store.get_version(workflow_id, version_id)
        if version is None:
            raise KeyError(version_id)
        artifact = decode(version["artifact"], {})
        workspace = workflow.acceptance.get("workspace")
        target_commit = artifact.get("candidate_commit", "") or artifact.get("baseline_commit", "")
        if workspace and target_commit:
            git = GitWorkspace(workspace)
            if git.available:
                git.switch_to(target_commit)
        workflow.active_version = version_id
        self._decision(workflow, "ROLLBACK", "Why should the active version change?", "rollback", ["The requested target version exists in this workflow", "The Git workspace was clean before switching"], [{"version_id": version_id, "commit": target_commit}], [], "Rollback may restore an older behavior", f"Set active_version to {version_id} and workspace commit to {target_commit}")
        self.store.save_workflow(workflow)
        self.store.append_event(workflow_id, "version_rolled_back", payload={"version_id": version_id})
        return workflow

    def explain(self, workflow_id: str) -> dict[str, Any]:
        return {"workflow": self.store.get_workflow(workflow_id).__dict__, **self.store.explain(workflow_id)}

    def list_workflows(self) -> list[dict[str, Any]]:
        return [workflow.__dict__ for workflow in self.store.list_workflows()]

    def _execute(self, workflow: Workflow) -> None:
        from_node = workflow.current_node
        pending = self.store.get_open_execution(workflow.id)
        if pending is not None:
            if pending["status"] == "STARTED":
                workflow.attempt = pending["attempt"]
                workflow.status = WorkflowStatus.UNCERTAIN
                self.store.save_workflow(workflow)
                self.store.append_event(workflow.id, "execution_uncertain", from_node=from_node.value, to_node=from_node.value, payload={"execution_token": pending["token"], "attempt": pending["attempt"]})
                self._decision(workflow, "UNCERTAIN", "Why stop automatic recovery?", "wait_for_human", ["A STARTED intent has no durable result", "DSH may already have changed the workspace"], [{"execution_token": pending["token"]}], [], "Automatic retry could duplicate side effects", "Ask a human to verify, retry, restore, or abort")
                return
            workflow.attempt = pending["attempt"]
            token = pending["token"]
            request = AgentInput(**decode(pending["request"], {}))
            self._decision(workflow, "RECOVER_INTENT", "Why reuse this execution identity?", "resume_attempt", ["A STARTED execution intent exists without a durable result"], [{"execution_token": token}], [], "The external DSH call may have partially executed", "Resume with the same attempt and session identity")
        else:
            workflow.attempt += 1
            token = f"{workflow.id}:{workflow.attempt}"
            previous = self.store.latest_verification(workflow.id)
            feedback = previous["feedback"] if previous else ""
            proposal = self.store.latest_proposal(workflow.id)
            proposal_data = dict(proposal) if proposal else None
            request = AgentInput(workflow.id, workflow.goal, workflow.attempt, feedback=feedback, proposal=proposal_data, acceptance=workflow.acceptance)
            self.store.save_workflow(workflow)
            self.store.start_execution(workflow.id, workflow.attempt, token, request.__dict__)
        if self.store.get_attempt(token) is None:
            try:
                output = self.agent.execute(request)
            except Exception as error:
                workflow.status = WorkflowStatus.UNCERTAIN
                self.store.save_workflow(workflow)
                self.store.append_event(workflow.id, "execution_uncertain", from_node=from_node.value, to_node=from_node.value, payload={"execution_token": token, "attempt": workflow.attempt, "error": str(error)})
                self._decision(workflow, "UNCERTAIN", "Why stop after the DSH error?", "wait_for_human", ["The DSH call raised before a durable result was recorded", "Remote or workspace side effects may already exist"], [{"execution_token": token, "error": str(error)}], [], "Automatic retry could duplicate an operation whose outcome is unknown", "Ask a human to verify, retry the same attempt, restore, or preserve")
                return
            self.store.save_attempt(workflow.id, workflow.attempt, token, request.__dict__, output.artifact, output.session_id)
            self.store.finish_execution(token, "COMPLETED")
            self._decision(workflow, "EXECUTE", "Why invoke DSH now?", "execute", ["Current node is AGENT_EXECUTE", "No durable result exists for this execution token"], [{"execution_token": token}], [], "The Agent may modify the workspace", "Produce a candidate artifact for verification")
        else:
            self.store.finish_execution(token, "COMPLETED")
            self._decision(workflow, "RECOVER", "Why avoid invoking DSH again?", "reuse_attempt", ["A durable result already exists for this execution token"], [{"execution_token": token}], [], "The external call may already have side effects", "Reuse the recorded result and preserve idempotency")
        workflow.current_node = self._node(workflow, self._spec_next(workflow, "execute", "pass", workflow.attempt - 1))
        self._save_transition(workflow, from_node)

    def _verify(self, workflow: Workflow) -> None:
        from_node = workflow.current_node
        row = self.store.get_attempt(f"{workflow.id}:{workflow.attempt}")
        if row is None:
            raise RuntimeError("current attempt is missing")
        artifact = decode(row["output"], {})
        result = self.verifier.verify(AgentOutput(artifact, artifact.get("response", ""), row["session_id"]), workflow.acceptance)
        workspace = workflow.acceptance.get("workspace")
        if workspace and GitWorkspace(workspace).available:
            git = GitWorkspace(workspace)
            changed = git.changed_files()
            allowed = workflow.acceptance.get("allowed_files", [])
            scope_passed = not allowed or all(path in allowed for path in changed)
            result.evidence.append({"type": "git_scope", "changed_files": changed, "allowed_files": allowed, "passed": scope_passed})
            if not scope_passed:
                result.passed = False
                result.feedback += f"\nChanged files outside allowed scope: {changed}"
        self.store.save_verification(workflow.id, workflow.attempt, result.passed, result.feedback, result.evidence)
        if result.passed:
            if workspace and GitWorkspace(workspace).available:
                prepared = GitWorkspace(workspace).prepare_candidate(workflow.id, workflow.attempt)
                artifact.update(prepared)
                self.store.save_attempt(workflow.id, workflow.attempt, row["execution_token"], decode(row["input"], {}), artifact, row["session_id"])
            if workflow.acceptance.get("require_promotion_approval", True):
                request_id = f"hitl:promote:{workflow.id}:{workflow.attempt}"
                candidate_fingerprint = GitWorkspace(workspace).candidate_fingerprint() if workspace else ""
                self.store.save_hitl(request_id, workflow.id, "promotion_review", {"attempt": workflow.attempt, "spec_revision": workflow.acceptance.get("spec_revision"), "loop_spec_revision": workflow.spec_revision, "loop_spec_hash": workflow.spec_hash, "candidate_fingerprint": candidate_fingerprint, "evidence": result.evidence, "changed_files": [item.get("changed_files", []) for item in result.evidence if item.get("type") == "git_scope"]})
                workflow.status, workflow.current_node = WorkflowStatus.WAITING_HITL, self._node(workflow, self._spec_next(workflow, "verify", "approve", workflow.attempt - 1))
                self._decision(workflow, "PROMOTION_REVIEW_REQUIRED", "Why pause before creating a Git version?", "wait_for_human", ["Independent verification passed", "Human review is required before AI-authored changes enter Git history"], result.evidence, [{"option": "auto_promote", "rejected_because": "promotion approval policy is enabled"}], "The candidate has not been committed yet", "Let a human inspect evidence and approve, retry, or reject")
            else:
                workflow.current_node = self._node(workflow, self._spec_next(workflow, "verify", "auto_promote", workflow.attempt - 1))
                self._decision(workflow, "VERIFY_PASS", "Why can this artifact advance?", "promote_candidate", ["Verifier passed", "Promotion approval is explicitly disabled for this contract"], result.evidence, [], "Promotion makes the artifact active", "Create a new promoted version")
        elif workflow.attempt < workflow.max_attempts:
            proposal = ImprovementProposal(f"proposal:{workflow.id}:{workflow.attempt}", workflow.id, workflow.attempt, result.feedback, "Address the verifier feedback in the next DSH session", ["Use verifier feedback as the next prompt context"], ["The next verification passes", "No new verification failure is introduced"], "LOW")
            self.store.save_proposal(proposal)
            workflow.current_node = self._node(workflow, self._spec_next(workflow, "verify", "retry", workflow.attempt - 1))
            self._decision(workflow, "RETRY", "Why retry instead of stopping?", "retry", ["Verification failed but the retry budget remains"], result.evidence, [{"option": "fail", "rejected_because": "retry budget remains"}], "Repeated execution can create additional workspace changes", "Ask DSH to address the feedback")
        else:
            request_id = f"hitl:{workflow.id}:{workflow.attempt}"
            self.store.save_hitl(request_id, workflow.id, "max_attempts_reached", {"feedback": result.feedback, "evidence": result.evidence})
            workflow.status, workflow.current_node = WorkflowStatus.WAITING_HITL, self._node(workflow, self._spec_next(workflow, "verify", "exhausted", workflow.attempt - 1))
            self._decision(workflow, "HITL_REQUIRED", "Why require a human?", "wait_for_human", ["Verification failed", "Automatic retry budget is exhausted"], result.evidence, [{"option": "retry", "rejected_because": "automatic budget exhausted"}, {"option": "reject", "rejected_because": "human must choose"}], "Continuing without review may make an unjustified change", "Pause until a human approves, retries, or rejects")
        self._save_transition(workflow, from_node)

    def _promote(self, workflow: Workflow) -> None:
        from_node = workflow.current_node
        row = self.store.get_attempt(f"{workflow.id}:{workflow.attempt}")
        if row is None:
            raise RuntimeError("cannot promote without a durable attempt")
        artifact = decode(row["output"], {})
        workspace = workflow.acceptance.get("workspace")
        if workspace and GitWorkspace(workspace).available:
            git = GitWorkspace(workspace)
            artifact["candidate_commit"] = git.promote_prepared(workflow.id, workflow.attempt, artifact)
        version = Version(f"version:{workflow.id}:{workflow.attempt}", workflow.id, workflow.active_version, artifact)
        self.store.save_version(version)
        workflow.active_version = version.id
        workflow.current_node, workflow.status = self._node(workflow, self._spec_next(workflow, "promote", "pass", workflow.attempt - 1)), WorkflowStatus.COMPLETED
        self._save_transition(workflow, from_node)

    def _decision(self, workflow: Workflow, decision_type: str, question: str, decision: str, rationale: list[str], evidence: list[dict[str, Any]], alternatives: list[dict[str, str]], risk: str, expected_effect: str) -> None:
        record = DecisionRecord(f"decision:{workflow.id}:{decision_type}:{workflow.attempt}:{utc_now()}", workflow.id, workflow.attempt, decision_type, question, decision, rationale, evidence, alternatives, risk, expected_effect)
        self.store.save_decision(record)

    @staticmethod
    def _spec_revision(goal: str, max_attempts: int, acceptance: dict[str, Any]) -> str:
        contract = {key: value for key, value in acceptance.items() if key != "spec_revision"}
        body = json.dumps({"goal": goal, "max_attempts": max_attempts, "acceptance": contract}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(body.encode()).hexdigest()

    def _transition(self, workflow: Workflow, status: WorkflowStatus, event_type: str, from_node: Node) -> None:
        workflow.status = status
        self.store.save_workflow(workflow)
        self.store.append_event(workflow.id, event_type, from_node=from_node.value, to_node=workflow.current_node.value)

    def _save_transition(self, workflow: Workflow, from_node: Node) -> None:
        self.store.save_workflow(workflow)
        self.store.append_event(workflow.id, "state_transition", from_node=from_node.value, to_node=workflow.current_node.value, payload={"status": workflow.status.value, "attempt": workflow.attempt})

    def _workflow_spec(self, workflow: Workflow) -> LoopSpec:
        spec = self.specs.revision(workflow.spec_id, workflow.spec_revision)
        if not workflow.spec_hash:
            spec = self.specs.active(workflow.spec_id) or self.default_spec
            workflow.spec_id = spec.spec_id
            workflow.spec_revision = spec.revision
            workflow.spec_hash = spec.content_hash()
            self.store.save_workflow(workflow)
        if spec is None or spec.content_hash() != workflow.spec_hash:
            raise RuntimeError("workflow LoopSpec revision/hash is unavailable or changed")
        return spec

    def _spec_next(self, workflow: Workflow, source: str, outcome: str, iteration: int) -> str:
        spec = self._workflow_spec(workflow)
        source_id = self._spec_node_for_role(spec, source)
        return LoopSpecInterpreter(spec).transition(source_id, outcome, max(iteration, 0)).target  # type: ignore[arg-type]

    @staticmethod
    def _spec_node_for_role(spec: LoopSpec, role: str) -> str:
        aliases = {"hitl": "human_gate"}
        canonical_role = aliases.get(role, role)
        matches = [node.id for node in spec.nodes if node.role == canonical_role or (node.role is None and node.id == role)]
        if len(matches) != 1:
            raise ValueError(f"LoopSpec has no unique runtime role: {role}")
        return matches[0]

    @staticmethod
    def _validate_runtime_nodes(spec) -> None:
        supported = {"dsh_execute", "verifier", "human_gate", "promotion", "terminal"}
        unknown = sorted({node.kind for node in spec.nodes} - supported)
        if unknown:
            raise ValueError(f"LoopSpec contains unsupported runtime node kinds: {unknown}")

    def _node(self, workflow: Workflow, node_id: str) -> Node:
        spec = self._workflow_spec(workflow)
        node = next((item for item in spec.nodes if item.id == node_id), None)
        nodes = {"execute": Node.EXECUTE, "verify": Node.VERIFY, "human_gate": Node.HITL, "promote": Node.PROMOTE, "complete": Node.COMPLETE, "failed": Node.FAILED}
        try:
            return nodes[node.role if node is not None and node.role is not None else node_id]
        except KeyError as error:
            raise ValueError(f"LoopSpec node is not supported by the A interpreter: {node_id}") from error
