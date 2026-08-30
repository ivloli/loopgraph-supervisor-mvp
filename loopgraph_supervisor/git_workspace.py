import hashlib
import os
import subprocess


class GitWorkspace:
    def __init__(self, workspace: str):
        self.workspace = workspace

    @property
    def available(self) -> bool:
        result = self.run("rev-parse", "--is-inside-work-tree")
        return result.returncode == 0 and result.stdout.strip() == "true"

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", *args], cwd=self.workspace, capture_output=True, text=True, check=False)

    def head(self) -> str:
        result = self.run("rev-parse", "HEAD")
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "workspace has no Git HEAD")
        return result.stdout.strip()

    def commit_parent(self, commit_sha: str) -> str:
        result = self.run("rev-parse", f"{commit_sha}^")
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "candidate commit has no parent")
        return result.stdout.strip()

    def diff_files(self, base_commit: str, head_commit: str) -> tuple[str, ...]:
        result = self.run("diff", "--name-only", "--no-renames", base_commit, head_commit, "--")
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "failed to inspect candidate diff")
        return tuple(sorted(path for path in result.stdout.splitlines() if path))

    def changed_files(self) -> list[str]:
        result = self.run("status", "--porcelain=v1", "-z")
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip())
        records = result.stdout.split("\0")
        paths: list[str] = []
        index = 0
        while index < len(records):
            record = records[index]
            index += 1
            if len(record) < 4:
                continue
            paths.append(record[3:])
            if "R" in record[:2] or "C" in record[:2]:
                if index < len(records) and records[index]:
                    paths.append(records[index])
                index += 1
        return sorted(set(paths))

    def candidate_fingerprint(self) -> str:
        digest = hashlib.sha256(self.head().encode())
        for path in sorted(self.changed_files()):
            digest.update(b"\0path\0" + path.encode())
            full_path = os.path.join(self.workspace, path)
            if os.path.lexists(full_path):
                digest.update(b"\0mode\0" + str(os.lstat(full_path).st_mode).encode())
            if os.path.islink(full_path):
                digest.update(b"\0symlink\0" + os.readlink(full_path).encode())
            elif os.path.isfile(full_path):
                digest.update(b"\0file\0")
                with open(full_path, "rb") as source:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        digest.update(chunk)
            elif os.path.isdir(full_path):
                digest.update(b"\0directory\0")
                for root, directories, files in os.walk(full_path):
                    directories.sort()
                    for name in sorted(files):
                        nested = os.path.join(root, name)
                        digest.update(b"\0nested\0" + os.path.relpath(nested, self.workspace).encode())
                        with open(nested, "rb") as source:
                            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                                digest.update(chunk)
            else:
                digest.update(b"\0deleted\0")
        return digest.hexdigest()

    def candidate_commit(self, message: str) -> str:
        files = self.changed_files()
        if not files:
            raise RuntimeError("cannot promote a workspace with no Git changes")
        result = self.run("add", "--", *files)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip())
        result = self.run("commit", "-m", message)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip())
        return self.head()

    def prepare_candidate(self, workflow_id: str, attempt: int) -> dict[str, object]:
        files = self.changed_files()
        if not files:
            raise RuntimeError("cannot prepare a candidate with no Git changes")
        result = self.run("add", "--", *files)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip())
        tree = self.run("write-tree")
        if tree.returncode != 0:
            raise RuntimeError(tree.stderr.strip())
        parent = self.head()
        message = f"supervisor: promote {workflow_id} attempt {attempt}"
        commit = self.run("commit-tree", tree.stdout.strip(), "-p", parent, "-m", message)
        if commit.returncode != 0:
            raise RuntimeError(commit.stderr.strip())
        return {"candidate_commit": commit.stdout.strip(), "candidate_tree": tree.stdout.strip(), "candidate_parent": parent, "candidate_files": files}

    def promote_prepared(self, workflow_id: str, attempt: int, artifact: dict[str, object]) -> str:
        commit_sha = str(artifact.get("candidate_commit", ""))
        tree_sha = str(artifact.get("candidate_tree", ""))
        parent_sha = str(artifact.get("candidate_parent", ""))
        if not commit_sha or not tree_sha or not parent_sha:
            raise RuntimeError("prepared candidate metadata is incomplete")
        tag = f"loopgraph-{workflow_id}-{attempt}"
        tagged = self.run("rev-parse", "--verify", f"refs/tags/{tag}")
        if tagged.returncode == 0:
            if tagged.stdout.strip() != commit_sha:
                raise RuntimeError("candidate tag does not match the reviewed snapshot")
            return commit_sha
        if self.head() != parent_sha:
            raise RuntimeError("candidate parent changed after review")
        current_tree = self.run("write-tree")
        if current_tree.returncode != 0 or current_tree.stdout.strip() != tree_sha:
            raise RuntimeError("staged candidate changed after review")
        result = self.run("update-ref", "HEAD", commit_sha, parent_sha)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "failed to atomically promote reviewed candidate")
        self.tag(tag, commit_sha)
        return commit_sha

    def promote(self, workflow_id: str, attempt: int) -> str:
        tag = f"loopgraph-{workflow_id}-{attempt}"
        tagged = self.run("rev-parse", "--verify", f"refs/tags/{tag}")
        if tagged.returncode == 0:
            return tagged.stdout.strip()
        message = f"supervisor: promote {workflow_id} attempt {attempt}"
        files = self.changed_files()
        if files:
            commit_sha = self.candidate_commit(message)
        else:
            subject = self.run("log", "-1", "--pretty=%s")
            if subject.returncode != 0 or subject.stdout.strip() != message:
                raise RuntimeError("cannot reconcile promotion: workspace is clean and HEAD is not this workflow candidate")
            commit_sha = self.head()
        self.tag(tag, commit_sha)
        return commit_sha

    def tag(self, name: str, commit_sha: str) -> None:
        existing = self.run("rev-parse", "--verify", f"refs/tags/{name}")
        if existing.returncode == 0:
            if existing.stdout.strip() != commit_sha:
                raise RuntimeError(f"tag {name} points to a different commit")
            return
        result = self.run("tag", name, commit_sha)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip())

    def switch_to(self, commit_sha: str) -> str:
        if self.changed_files():
            raise RuntimeError("cannot rollback a dirty Git workspace")
        result = self.run("switch", "--detach", commit_sha)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip())
        return self.head()

    def restore_contract_paths(self, commit_sha: str, paths: list[str]) -> list[str]:
        changed = self.changed_files()
        unexpected = [path for path in changed if path not in paths]
        if unexpected:
            raise RuntimeError(f"refusing baseline restore with out-of-scope changes: {unexpected}")
        restored = []
        for path in changed:
            tracked = self.run("ls-files", "--error-unmatch", "--", path)
            if tracked.returncode == 0:
                result = self.run("restore", "--source", commit_sha, "--staged", "--worktree", "--", path)
                if result.returncode != 0:
                    raise RuntimeError(result.stderr.strip())
            else:
                full_path = os.path.join(self.workspace, path)
                if os.path.isdir(full_path):
                    import shutil
                    shutil.rmtree(full_path)
                elif os.path.exists(full_path):
                    os.remove(full_path)
            restored.append(path)
        return restored
