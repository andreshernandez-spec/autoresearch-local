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

    def tree(self, ref: str = "HEAD") -> str:
        """The id of the files at a commit: two commits of the same change share it."""
        return self("rev-parse", "--verify", f"{ref}^{{tree}}").strip()

    def branch(self) -> str:
        """Current branch, empty when the checkout is detached."""
        return self("branch", "--show-current").strip()

    def branch_exists(self, name: str) -> bool:
        return self.ok("rev-parse", "--verify", "--quiet", f"refs/heads/{name}")

    def commit_exists(self, sha: str) -> bool:
        return self.ok("cat-file", "-e", f"{sha}^{{commit}}")

    def is_ancestor(self, older: str, newer: str) -> bool:
        return self.ok("merge-base", "--is-ancestor", older, newer)

    def status(self, *pathspecs: str) -> list[tuple[str, str]]:
        """(two-letter code, path) of changed and untracked paths, ignored files left out.

        Read with -z: no quoting, so names with spaces or accents come out as they are.
        """
        out = self("status", "--porcelain", "-z", "--untracked-files=all", "--", *pathspecs)
        fields, items, i = out.split("\0"), [], 0
        while i < len(fields):
            entry = fields[i]
            i += 1
            if len(entry) < 4:
                continue
            code, path = entry[:2], entry[3:]
            if code[0] in "RC":
                i += 1  # the next field is the path it was renamed or copied from
            items.append((code, path))
        return items

    def dirty(self, *pathspecs: str) -> list[str]:
        return [path for _, path in self.status(*pathspecs)]

    def tracked_changes(self) -> list[str]:
        """Changes to files git already tracks (untracked files left out)."""
        return [path for code, path in self.status() if code != "??"]

    def blob(self, ref: str, path: str) -> str | None:
        """Object id of a file at a commit, None if it is not there."""
        return self("rev-parse", "--verify", "--quiet", f"{ref}:{path}", check=False).strip() or None

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

    def prune_worktrees(self) -> None:
        """Forget worktrees whose folder is gone. Touches no branch and no file."""
        self("worktree", "prune")
