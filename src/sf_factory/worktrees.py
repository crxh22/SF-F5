"""All git mechanics (design §1/§4): worktree lifecycle, commit helper, git-state
healing, the Tier-1 merge gate (rebase + full test suite, DoD §5.1), the
integration merge (serialized per target branch), and the diff primitives
(digest / full / merged-unit). No agent judgment at this tier.

Concurrency (§7): one ``asyncio.Lock`` per (repo, target branch). ``tier1_gate``
and ``integrate`` each run their whole section under it; the gap between a gate
and its merge is closed mechanically by ``integrate``'s target-HEAD-unchanged
assertion (``StaleGateError`` → the caller loops back to rebase), so a state
that was never rebased/tested against the post-sibling HEAD can never merge.

May import: models, config — NEVER db (the §1 import DAG); callers own all
persistence.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import signal
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from sf_factory.config import FactoryConfig
from sf_factory.models import GitError, new_id

#: Truncation marker appended by the bounded diff primitives.
_TRUNCATION_MARKER = "\n[truncated]"

_TRAILER_KEY_RE = re.compile(r"^[A-Za-z0-9-]+$")
_STAGE_ID_TRAILER_RE = re.compile(r"^Stage-Id:[ \t]*(\S+)[ \t]*$", re.MULTILINE)


class StaleGateError(GitError):
    """integrate()'s defense-in-depth assert failed: the target branch moved after
    the gate's rebase (a sibling merged in between). Not an infrastructure
    failure — the caller loops back to tier1_gate (§4 integrate contract)."""


@dataclass(frozen=True)
class Tier1Result:
    """passed: bool, rebase_conflict: bool, conflict_payload: str, tests_failed: bool,
    test_output_path: str | None."""

    passed: bool
    rebase_conflict: bool
    conflict_payload: str
    tests_failed: bool
    test_output_path: str | None


async def run_git(*args: str, cwd: Path | None = None) -> tuple[int, str, str]:
    """Run git, return (exit_code, stdout, stderr); never raises on nonzero exit."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=None if cwd is None else str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:  # git missing / cwd gone — spawn failure, not an exit
        raise GitError(f"cannot spawn git {' '.join(args)}: {exc}") from exc
    out_bytes, err_bytes = await proc.communicate()
    assert proc.returncode is not None
    return (
        proc.returncode,
        out_bytes.decode("utf-8", errors="replace"),
        err_bytes.decode("utf-8", errors="replace"),
    )


async def commit_paths(
    repo_root: Path, paths: Sequence[Path], message: str, *, trailers: Mapping[str, str]
) -> str | None:
    """git add+commit with trailer block (moved here from artifacts.py — the git-exec
    primitive has exactly one home); refuses non-worktree-root cwd and branch mismatch;
    None if nothing to commit; GitError otherwise.

    Branch guard: a detached HEAD has no branch for the commit to land on
    (the typical residue of a half-healed rebase) — refused, never guessed.
    """
    root = Path(repo_root)
    if not paths:
        raise GitError(f"commit_paths called with no paths in {root}")
    await _assert_worktree_root(root)
    branch = await _current_branch(root)
    if branch is None:
        raise GitError(f"refusing to commit on a detached HEAD in {root} (branch guard)")

    for key, value in trailers.items():
        if not _TRAILER_KEY_RE.fullmatch(key):
            raise GitError(f"invalid trailer key {key!r} (must match [A-Za-z0-9-]+)")
        if "\n" in value:
            raise GitError(f"trailer {key!r} value must be single-line, got {value!r}")

    path_args = [str(p) for p in paths]
    code, out, err = await run_git("add", "--", *path_args, cwd=root)
    if code != 0:
        raise GitError(f"git add failed in {root}: {(err or out).strip()}")

    # Scoped to the named paths: unrelated index state never rides along.
    code, out, err = await run_git("diff", "--cached", "--quiet", "--", *path_args, cwd=root)
    if code == 0:
        return None
    if code != 1:
        raise GitError(f"git diff --cached failed in {root}: {(err or out).strip()}")

    full_message = message.rstrip("\n")
    if trailers:
        block = "\n".join(f"{key}: {value}" for key, value in trailers.items())
        full_message = f"{full_message}\n\n{block}"
    code, out, err = await run_git("commit", "-m", full_message, "--", *path_args, cwd=root)
    if code != 0:
        raise GitError(f"git commit failed in {root}: {(err or out).strip()}")
    code, out, err = await run_git("rev-parse", "HEAD", cwd=root)
    if code != 0:
        raise GitError(f"git rev-parse HEAD failed in {root}: {(err or out).strip()}")
    return out.strip()


async def _assert_worktree_root(path: Path) -> None:
    """Refuse to operate unless ``path`` is the toplevel of its own work tree —
    a stray git add/commit in a non-root dir would operate on an ancestor repo
    (harvested guard, D-0002)."""
    code, out, err = await run_git("rev-parse", "--show-toplevel", cwd=path)
    if code != 0:
        raise GitError(f"path is not inside a git work tree: {path} ({(err or out).strip()})")
    toplevel = Path(out.strip()).resolve()
    if toplevel != path.resolve():
        raise GitError(
            f"refusing to operate: {path} is not a work tree root "
            f"(toplevel resolves to {toplevel})"
        )


async def _current_branch(worktree: Path) -> str | None:
    """Current branch name, or None when HEAD is detached; GitError if unresolvable."""
    code, out, err = await run_git("rev-parse", "--abbrev-ref", "HEAD", cwd=worktree)
    if code != 0:
        raise GitError(f"cannot resolve HEAD in {worktree}: {(err or out).strip()}")
    name = out.strip()
    return None if name == "HEAD" else name


def _bound(text: str, max_bytes: int) -> str:
    """Bound text to max_bytes (UTF-8), appending a truncation marker when cut."""
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text
    marker = _TRUNCATION_MARKER.encode("utf-8")
    keep = max(0, max_bytes - len(marker))
    return (data[:keep] + marker)[:max_bytes].decode("utf-8", errors="replace")


class WorktreeManager:
    """Owns worktree dirs per projects.<id>.worktrees_dir and one asyncio.Lock PER
    TARGET BRANCH: the whole rebase→test→merge sequence and worktree add/remove run
    under it — two stages gating concurrently would otherwise merge a state never
    rebased/tested against the post-sibling HEAD (silently voiding DoD §5.1) and
    contend on git index/ref locks."""

    def __init__(self, cfg: FactoryConfig) -> None:
        self._cfg = cfg
        # (repo identity, branch) -> lock. Repo identity = resolved common git
        # dir, so a linked worktree and its main repo share the same key space.
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    # ------------------------------------------------------------------ locking

    def _lock(self, repo_key: str, branch: str) -> asyncio.Lock:
        return self._locks.setdefault((repo_key, branch), asyncio.Lock())

    async def _repo_key(self, path: Path) -> str:
        code, out, err = await run_git(
            "rev-parse", "--path-format=absolute", "--git-common-dir", cwd=path
        )
        if code != 0:
            raise GitError(f"not a git worktree: {path} ({(err or out).strip()})")
        return str(Path(out.strip()).resolve())

    # ----------------------------------------------------------------- lifecycle

    def _worktrees_dir(self, repo_root: Path) -> Path:
        """projects.<id>.worktrees_dir for the project whose workspace is repo_root."""
        resolved = Path(repo_root).resolve()
        for project in self._cfg.projects.values():
            if Path(project.workspace).resolve() == resolved:
                return Path(project.worktrees_dir)
        raise GitError(f"no configured project workspace matches repo root {repo_root}")

    async def create(
        self,
        repo_root: Path,
        unit_id: str,
        branch: str,
        base_branch: str,
        *,
        new_branch: bool = True,
    ) -> Path:
        """Idempotent `git worktree add` (`-b` when new_branch; else checkout of the
        existing branch — used for Validator scratch worktrees, §3.1). Runs
        `git worktree prune` first (crash-orphaned half-registrations are cleaned, not
        escalated); verifies an existing path is a registered worktree on the expected
        branch, else GitError (never mask inconsistent state).

        Mechanics pinned here:
        - the worktree path is <worktrees_dir>/<unit_id> (the Validator scratch
          worktree passes the '-validate'-suffixed unit id, §3.1);
        - new_branch=True with an already-existing branch attaches to it without
          `-b` (crash resume: the branch outlived its pruned worktree);
        - new_branch=False checks the branch's content out DETACHED at its tip —
          git refuses a second checkout of a branch already checked out in the
          unit worktree, and the scratch worktree needs content, not the ref. A
          re-create re-syncs a stale scratch checkout to the branch tip.
        """
        root = Path(repo_root)
        if not unit_id or "/" in unit_id or "\\" in unit_id or ".." in unit_id:
            raise GitError(f"unsafe unit id for worktree path: {unit_id!r}")
        path = (self._worktrees_dir(root) / unit_id).resolve()
        repo_key = await self._repo_key(root)
        async with self._lock(repo_key, base_branch):
            code, out, err = await run_git("worktree", "prune", cwd=root)
            if code != 0:
                raise GitError(f"git worktree prune failed in {root}: {(err or out).strip()}")

            entry = await self._registered_entry(root, path)
            if entry is not None:
                return await self._verify_existing(root, path, branch, entry, new_branch)
            if path.exists() and any(path.iterdir()):
                raise GitError(
                    f"path exists but is not a registered worktree: {path} "
                    "(remove it or let recovery clean it — never masked)"
                )

            path.parent.mkdir(parents=True, exist_ok=True)
            branch_exists = await self._branch_exists(root, branch)
            if new_branch:
                if branch_exists:
                    # Crash resume: branch created, worktree lost+pruned — attach.
                    args = ["worktree", "add", str(path), branch]
                else:
                    args = ["worktree", "add", "-b", branch, str(path), base_branch]
            else:
                if not branch_exists:
                    raise GitError(
                        f"cannot create scratch worktree: branch {branch!r} does not exist"
                    )
                args = ["worktree", "add", "--detach", str(path), branch]
            code, out, err = await run_git(*args, cwd=root)
            if code != 0:
                raise GitError(
                    f"git {' '.join(args)} failed in {root}: {(err or out).strip()}"
                )
            return path

    async def _verify_existing(
        self,
        root: Path,
        path: Path,
        branch: str,
        entry: dict[str, str | None],
        new_branch: bool,
    ) -> Path:
        """Idempotent-create verification of an already-registered worktree."""
        if new_branch:
            expected_ref = f"refs/heads/{branch}"
            if entry["branch"] != expected_ref:
                found = entry["branch"] or "detached HEAD"
                raise GitError(
                    f"existing worktree at {path} is on the wrong branch: "
                    f"expected {expected_ref}, found {found}"
                )
            return path
        if entry["branch"] is not None:
            raise GitError(
                f"existing scratch worktree at {path} unexpectedly has a branch "
                f"checked out ({entry['branch']}); expected a detached checkout"
            )
        code, out, err = await run_git("rev-parse", "--verify", f"{branch}^{{commit}}", cwd=root)
        if code != 0:
            raise GitError(f"branch {branch!r} unresolvable in {root}: {(err or out).strip()}")
        tip = out.strip()
        if entry["HEAD"] != tip:
            code, out, err = await run_git("checkout", "--detach", branch, cwd=path)
            if code != 0:
                raise GitError(
                    f"cannot re-sync scratch worktree {path} to {branch}: "
                    f"{(err or out).strip()}"
                )
        return path

    async def _branch_exists(self, root: Path, branch: str) -> bool:
        code, _, _ = await run_git(
            "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", cwd=root
        )
        return code == 0

    async def _registered_entry(self, root: Path, path: Path) -> dict[str, str | None] | None:
        """The `git worktree list --porcelain` entry for ``path``, or None."""
        code, out, err = await run_git("worktree", "list", "--porcelain", cwd=root)
        if code != 0:
            raise GitError(f"git worktree list failed in {root}: {(err or out).strip()}")
        target = path.resolve()
        current: dict[str, str | None] = {}
        for raw_line in out.splitlines() + [""]:
            line = raw_line.strip()
            if not line:
                if current.get("worktree") and Path(str(current["worktree"])).resolve() == target:
                    current.setdefault("branch", None)
                    current.setdefault("HEAD", None)
                    return current
                current = {}
            elif line.startswith("worktree "):
                current["worktree"] = line.removeprefix("worktree ")
            elif line.startswith("HEAD "):
                current["HEAD"] = line.removeprefix("HEAD ")
            elif line.startswith("branch "):
                current["branch"] = line.removeprefix("branch ")
            elif line == "detached":
                current["branch"] = None
        return None

    async def remove(self, repo_root: Path, worktree: Path) -> None:
        """`git worktree remove --force`; GitError on failure.

        Runs under ALL of this repo's branch locks (acquired in sorted order —
        deadlock-free): no gate, merge or create may be mid-flight anywhere in
        the repo while a worktree vanishes (§4 add/remove-under-lock rule).
        """
        root = Path(repo_root)
        repo_key = await self._repo_key(root)
        keys = sorted(key for key in self._locks if key[0] == repo_key)
        async with contextlib.AsyncExitStack() as stack:
            for key in keys:
                await stack.enter_async_context(self._locks[key])
            code, out, err = await run_git(
                "worktree", "remove", "--force", str(worktree), cwd=root
            )
            if code != 0:
                raise GitError(
                    f"git worktree remove failed for {worktree}: {(err or out).strip()}"
                )

    # ------------------------------------------------------------------- healing

    async def heal_git_state(self, worktree: Path) -> list[str]:
        """Mechanically abort in-progress rebase/merge/cherry-pick (`.git/rebase-merge`,
        `rebase-apply`, `MERGE_HEAD` present) left by a crash; verify expected branch;
        return actions taken. Run by Scheduler.recover() and as tier1_gate/integrate
        preamble — deterministic, no judgment: a SIGKILL mid-gate must resume
        mechanically, never degrade into a human escalation."""
        wt = Path(worktree)
        code, out, err = await run_git(
            "rev-parse", "--path-format=absolute", "--git-dir", cwd=wt
        )
        if code != 0:
            raise GitError(f"not a git worktree: {wt} ({(err or out).strip()})")
        git_dir = Path(out.strip())
        actions: list[str] = []

        if (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists():
            actions.append(await self._abort("rebase", wt, ("--abort", "--quit")))
        if (git_dir / "MERGE_HEAD").exists():
            actions.append(await self._abort("merge", wt, ("--abort",)))
        if (git_dir / "CHERRY_PICK_HEAD").exists():
            actions.append(await self._abort("cherry-pick", wt, ("--abort", "--quit")))

        # Verify the worktree is usable: HEAD must resolve. Detachment is legal
        # (scratch worktrees) but noteworthy right after an abort restored state.
        branch = await _current_branch(wt)
        if actions and branch is None:
            actions.append("warning: HEAD detached after healing")
        return actions

    async def _abort(self, operation: str, worktree: Path, flags: Sequence[str]) -> str:
        """Try `git <op> <flag>` in order; first success wins; all fail -> GitError."""
        errors: list[str] = []
        for flag in flags:
            code, out, err = await run_git(operation, flag, cwd=worktree)
            if code == 0:
                return f"{operation} {flag}"
            errors.append(f"{operation} {flag}: {(err or out).strip()}")
        raise GitError(
            f"cannot abort in-progress {operation} in {worktree}: {'; '.join(errors)}"
        )

    # ---------------------------------------------------------------- merge gate

    async def tier1_gate(
        self, worktree: Path, target_branch: str, test_cmd: list[str], timeout_s: int
    ) -> Tier1Result:
        """DoD §5.1, purely mechanical, under the target-branch lock: heal_git_state;
        rebase onto target — on conflict abort rebase and return conflict payload; else
        run the full test suite. The CALLER (StageExecutor, which owns db access —
        worktrees never imports db) registers the suite as the kind='tests' process
        and, after a successful rebase, re-resolves the stage's artifact_refs.git_commit
        at the new branch head (rebase rewrote history; old shas survive only in
        reflog) — mechanical: same path + same sha256. No agent judgment."""
        wt = Path(worktree)
        if not test_cmd:
            raise GitError(f"tier1_gate called with an empty test_cmd for {wt}")
        repo_key = await self._repo_key(wt)
        async with self._lock(repo_key, target_branch):
            await self.heal_git_state(wt)
            branch = await _current_branch(wt)
            if branch is None:
                raise GitError(f"cannot gate {wt}: HEAD is detached (no branch to rebase)")

            code, out, err = await run_git("rebase", target_branch, cwd=wt)
            if code != 0:
                payload = await self._conflict_payload(wt, target_branch, out, err)
                await self._abort("rebase", wt, ("--abort", "--quit"))
                return Tier1Result(
                    passed=False,
                    rebase_conflict=True,
                    conflict_payload=payload,
                    tests_failed=False,
                    test_output_path=None,
                )

            output_path = self._test_output_path()
            exit_code = await self._run_test_suite(test_cmd, wt, timeout_s, output_path)
            if exit_code != 0:
                return Tier1Result(
                    passed=False,
                    rebase_conflict=False,
                    conflict_payload="",
                    tests_failed=True,
                    test_output_path=str(output_path),
                )
            return Tier1Result(
                passed=True,
                rebase_conflict=False,
                conflict_payload="",
                tests_failed=False,
                test_output_path=str(output_path),
            )

    async def _conflict_payload(
        self, worktree: Path, target_branch: str, out: str, err: str
    ) -> str:
        """Assemble the Tier-1 conflict payload BEFORE the abort wipes the evidence."""
        parts = [f"$ git rebase {target_branch}", out.strip(), err.strip()]
        code, status_out, _ = await run_git("status", "--porcelain=v1", cwd=worktree)
        if code == 0:
            parts += ["$ git status --porcelain", status_out.rstrip()]
        code, diff_out, _ = await run_git("diff", cwd=worktree)
        if code == 0:
            parts += ["$ git diff (conflict hunks)", diff_out.rstrip()]
        return "\n".join(part for part in parts if part) + "\n"

    def _test_output_path(self) -> Path:
        log_dir = Path(self._cfg.process.ndjson_log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        return log_dir / f"{new_id('tier1')}.log"

    async def _run_test_suite(
        self, test_cmd: list[str], cwd: Path, timeout_s: int, output_path: Path
    ) -> int:
        """Run the suite in its own process group, combined output to file; on
        timeout terminate->kill the GROUP (config grace), report nonzero."""
        with open(output_path, "wb") as fh:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *test_cmd,
                    cwd=str(cwd),
                    stdout=fh,
                    stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True,
                )
            except OSError as exc:
                raise GitError(f"cannot spawn test suite {test_cmd!r}: {exc}") from exc
            try:
                exit_code = await asyncio.wait_for(proc.wait(), timeout=timeout_s)
            except TimeoutError:
                await self._kill_group(proc)
                fh.write(
                    f"\n[tier1_gate] test suite timed out after {timeout_s}s; "
                    "process group terminated\n".encode()
                )
                return -signal.SIGKILL
        return exit_code

    async def _kill_group(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.pid is not None
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), timeout=self._cfg.process.terminate_grace_s)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()

    async def integrate(self, repo_root: Path, branch: str, target_branch: str) -> str:
        """Fast-forward/no-ff merge of a gated branch into target, under the same
        target-branch lock, with a `Stage-Id:` trailer in the merge commit (keys
        merged_unit_diffs); asserts target HEAD unchanged since the gate's rebase
        (defense in depth — else caller loops back to rebase); returns merge commit
        sha; GitError on failure.

        Mechanics pinned here: the merge always uses --no-ff (a plain
        fast-forward would create no commit to carry the trailer); the Stage-Id
        value is the unit id, derived mechanically from the §2 branch naming
        ('stage/<id>' / 'phase/<id>' -> '<id>'); a stale gate raises
        StaleGateError (a GitError subclass) so callers can loop back without
        string-matching.
        """
        root = Path(repo_root)
        repo_key = await self._repo_key(root)
        async with self._lock(repo_key, target_branch):
            await self.heal_git_state(root)
            current = await _current_branch(root)
            if current != target_branch:
                raise GitError(
                    f"integrate requires {root} checked out on {target_branch!r}, "
                    f"found {current or 'detached HEAD'!r}"
                )
            if not await self._branch_exists(root, branch):
                raise GitError(f"cannot integrate: branch {branch!r} does not exist")

            code, out, err = await run_git(
                "merge-base", "--is-ancestor", target_branch, branch, cwd=root
            )
            if code == 1:
                raise StaleGateError(
                    f"target {target_branch!r} moved since {branch!r} was gated — "
                    "re-run tier1_gate before integrating"
                )
            if code != 0:
                raise GitError(
                    f"merge-base --is-ancestor failed in {root}: {(err or out).strip()}"
                )

            unit_id = branch.split("/", 1)[1] if "/" in branch else branch
            message = f"integrate {branch} into {target_branch}\n\nStage-Id: {unit_id}"
            code, out, err = await run_git("merge", "--no-ff", "-m", message, branch, cwd=root)
            if code != 0:
                # Best-effort cleanup; the failure itself is what escalates.
                await run_git("merge", "--abort", cwd=root)
                raise GitError(
                    f"merge of {branch} into {target_branch} failed: {(err or out).strip()}"
                )
            code, out, err = await run_git("rev-parse", "HEAD", cwd=root)
            if code != 0:
                raise GitError(f"git rev-parse HEAD failed in {root}: {(err or out).strip()}")
            return out.strip()

    # ------------------------------------------------------------ diff primitives

    async def diff_digest(self, worktree: Path, target_branch: str, max_bytes: int) -> str:
        """Bounded digest (stat + hunk headers) — CP-1 input ONLY (DoD §3.4); never
        sufficient for Tier 2."""
        wt = Path(worktree)
        code, stat_out, err = await run_git(
            "diff", "--stat", f"{target_branch}...HEAD", cwd=wt
        )
        if code != 0:
            raise GitError(f"git diff --stat failed in {wt}: {(err or stat_out).strip()}")
        code, diff_out, err = await run_git("diff", f"{target_branch}...HEAD", cwd=wt)
        if code != 0:
            raise GitError(f"git diff failed in {wt}: {(err or diff_out).strip()}")
        digest = "\n".join(
            ("== diffstat ==", stat_out.rstrip(), "== hunks ==", _hunk_headers(diff_out))
        )
        return _bound(digest, max_bytes)

    async def full_diff(self, worktree: Path, target_branch: str, max_bytes: int) -> str:
        """Full unified diff (bodies, size-bounded) of the gating unit vs target —
        Tier-2 input (§3.1)."""
        wt = Path(worktree)
        code, out, err = await run_git("diff", f"{target_branch}...HEAD", cwd=wt)
        if code != 0:
            raise GitError(f"git diff failed in {wt}: {(err or out).strip()}")
        return _bound(out, max_bytes)

    async def merged_unit_diffs(
        self, repo_root: Path, target_branch: str, since_ref: str, max_bytes_per_unit: int
    ) -> Mapping[str, str]:
        """unit_id -> full diff for every unit merged into target_branch since since_ref
        (the contract-freeze commit), keyed by merge-commit Stage-Id trailers — Tier-2
        sibling visibility (§3.1).

        Walks first-parent merge commits only (the integration mainline written
        by integrate()); a unit merged more than once contributes its diffs
        concatenated chronologically; merge commits without a Stage-Id trailer
        are not factory units and are skipped.
        """
        root = Path(repo_root)
        code, out, err = await run_git(
            "log",
            "--first-parent",
            "--merges",
            "--reverse",
            "--format=%H",
            f"{since_ref}..{target_branch}",
            cwd=root,
        )
        if code != 0:
            raise GitError(
                f"git log {since_ref}..{target_branch} failed in {root}: "
                f"{(err or out).strip()}"
            )
        collected: dict[str, list[str]] = {}
        for sha in out.split():
            code, message, err = await run_git("show", "-s", "--format=%B", sha, cwd=root)
            if code != 0:
                raise GitError(f"git show {sha} failed in {root}: {(err or message).strip()}")
            matches = _STAGE_ID_TRAILER_RE.findall(message)
            if not matches:
                continue  # not a factory integration merge
            unit_id = matches[-1]
            code, diff, err = await run_git("diff", f"{sha}^1", sha, cwd=root)
            if code != 0:
                raise GitError(f"git diff {sha}^1 {sha} failed in {root}: {(err or diff).strip()}")
            collected.setdefault(unit_id, []).append(diff)
        return {
            unit_id: _bound("".join(parts), max_bytes_per_unit)
            for unit_id, parts in collected.items()
        }


def _hunk_headers(diff_text: str) -> str:
    """File headers + @@ hunk headers only — body lines stripped (state machine,
    not prefix-matching: a removed line starting '--' must never leak in)."""
    kept: list[str] = []
    in_preamble = False
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            in_preamble = True
            kept.append(line)
        elif line.startswith("@@"):
            in_preamble = False
            kept.append(line)
        elif in_preamble:
            kept.append(line)  # index/---/+++/mode/rename/Binary lines
    return "\n".join(kept)
