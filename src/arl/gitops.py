"""Git through a short allow-list.

arl reads state, commits its own paths, creates branches and worktrees, and
fast-forwards a track. It never pushes, resets, cleans, rebases, amends or
deletes a branch, so nothing it does can lose work.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .util import ArlError

_ALLOWED = {
    "rev-parse",
    "status",
    "add",
    "commit",
    "branch",
    "switch",
    "merge",
    "worktree",
    "diff",
    "log",
    "show",
    "cat-file",
    "config",
    "merge-base",
}
_NEVER = {"--force", "-f", "--hard", "--amend", "--no-verify", "--discard-changes", "-C", "-D", "-d", "-M"}


def _guard(args: tuple[str, ...], remove_ok: bool) -> None:
    if not args or args[0] not in _ALLOWED:
        raise ArlError(f"arl does not run 'git {' '.join(args)}'")
    sub, rest = args[0], args[1:]
    flags = set()
    for a in rest:
        if a == "--":
            break
        if a.startswith("-"):
            flags.add(a)
    bad = flags & _NEVER
    if sub == "worktree" and rest[:1] == ("remove",):
        if not remove_ok:
            raise ArlError("arl removes only the worktrees it made for a rerun")
        bad -= {"--force"}
    if bad:
        raise ArlError(f"arl does not run 'git {' '.join(args)}' ({', '.join(sorted(bad))})")
    if sub == "merge" and "--ff-only" not in flags:
        raise ArlError("arl merges only with --ff-only")
    if sub == "branch" and flags - {"--list", "--show-current"}:
        raise ArlError(f"arl only creates and lists branches, not 'git {' '.join(args)}'")
    if sub == "config" and "--get" not in flags:
        raise ArlError("arl only reads git config")


class Git:
    def __init__(self, cwd: Path):
        self.cwd = Path(cwd)

    def __call__(self, *args: str, check: bool = True, _remove_ok: bool = False) -> str:
        _guard(args, _remove_ok)
        proc = subprocess.run(["git", *args], cwd=self.cwd, capture_output=True, text=True)
        if check and proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()
            raise ArlError(f"git {' '.join(args)} failed in {self.cwd}:\n{detail}")
        return proc.stdout

    def ok(self, *args: str) -> bool:
        _guard(args, False)
        return subprocess.run(["git", *args], cwd=self.cwd, capture_output=True).returncode == 0

    # --- reading ---------------------------------------------------------------

    def top(self) -> Path:
        return Path(self("rev-parse", "--show-toplevel").strip())

    def head(self, ref: str = "HEAD") -> str:
        return self("rev-parse", "--verify", f"{ref}^{{commit}}").strip()

    def branch(self) -> str:
        """Current branch, empty when the checkout is detached."""
        return self("branch", "--show-current").strip()

    def branch_exists(self, name: str) -> bool:
        return self.ok("rev-parse", "--verify", "--quiet", f"refs/heads/{name}")

    def commit_exists(self, sha: str) -> bool:
        return self.ok("cat-file", "-e", f"{sha}^{{commit}}")

    def is_ancestor(self, older: str, newer: str) -> bool:
        return self.ok("merge-base", "--is-ancestor", older, newer)

    def dirty(self, *pathspecs: str) -> list[str]:
        """Changed and untracked paths, ignored files left out."""
        out = self("status", "--porcelain", "--untracked-files=all", "--", *pathspecs)
        return [line[3:] for line in out.splitlines() if line.strip()]

    def changed(self, older: str, newer: str, *paths: str) -> list[str]:
        out = self("diff", "--name-only", older, newer, "--", *paths)
        return [line for line in out.splitlines() if line.strip()]

    def email(self) -> str:
        return self("config", "--get", "user.email", check=False).strip()

    def worktrees(self) -> list[dict[str, str]]:
        trees: list[dict[str, str]] = []
        for line in self("worktree", "list", "--porcelain").splitlines():
            if line.startswith("worktree "):
                trees.append({"path": line[9:]})
            elif line.startswith("HEAD ") and trees:
                trees[-1]["head"] = line[5:]
            elif line.startswith("branch ") and trees:
                trees[-1]["branch"] = line[7:].removeprefix("refs/heads/")
        return trees

    def main_worktree(self) -> Path:
        return Path(self.worktrees()[0]["path"])

    # --- writing ---------------------------------------------------------------

    def create_branch(self, name: str, start: str) -> None:
        self("branch", name, start)

    def switch(self, name: str) -> None:
        self("switch", "--quiet", name)

    def switch_new(self, name: str, start: str) -> None:
        self("switch", "--quiet", "-c", name, start)

    def fast_forward(self, to: str) -> None:
        self("merge", "--quiet", "--ff-only", to)

    def commit_paths(self, message: str, pathspecs: list[str]) -> str | None:
        """Commit only these paths. Returns the new commit, or None if nothing changed."""
        if not self.dirty(*pathspecs):
            return None
        self("add", "-A", "--", *pathspecs)
        self("commit", "--quiet", "-m", message, "--", *pathspecs)
        return self.head()

    def commit_all(self, message: str) -> str | None:
        if not self.dirty():
            return None
        self("add", "-A")
        self("commit", "--quiet", "-m", message)
        return self.head()

    def add_worktree(self, path: Path, branch: str) -> None:
        self("worktree", "add", "--quiet", str(path), branch)

    def add_detached(self, path: Path, sha: str) -> None:
        self("worktree", "add", "--quiet", "--detach", str(path), sha)

    def remove_worktree(self, path: Path) -> None:
        """Only for the throwaway trees arl makes for a rerun."""
        self("worktree", "remove", "--force", str(path), _remove_ok=True)
