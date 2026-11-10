#!/usr/bin/env python3
"""Commit python-programming to the python branch and merge it into main."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BRANCH = "python"
FOLDER = "python-programming"
WORKFLOWS = (
    ".github/workflows/deploy-python-pipeline.yml",
    ".github/workflows/destroy-python-resources.yml",
)
REMOTE = "origin"
ASCII_GLYPHS = {
    "A": (" ### ", "#   #", "#####", "#   #", "#   #"),
    "B": ("#### ", "#   #", "#### ", "#   #", "#### "),
    "C": (" ####", "#    ", "#    ", "#    ", " ####"),
    "D": ("#### ", "#   #", "#   #", "#   #", "#### "),
    "E": ("#####", "#    ", "#### ", "#    ", "#####"),
    "H": ("#   #", "#   #", "#####", "#   #", "#   #"),
    "N": ("#   #", "##  #", "# # #", "#  ##", "#   #"),
    "O": (" ### ", "#   #", "#   #", "#   #", " ### "),
    "P": ("#### ", "#   #", "#### ", "#    ", "#    "),
    "R": ("#### ", "#   #", "#### ", "# #  ", "#  ##"),
    "S": (" ####", "#    ", " ### ", "    #", "#### "),
    "T": ("#####", "  #  ", "  #  ", "  #  ", "  #  "),
    "Y": ("#   #", " # # ", "  #  ", "  #  ", "  #  "),
    "-": ("     ", "     ", "#####", "     ", "     "),
    " ": ("     ", "     ", "     ", "     ", "     "),
}


def banner(title: str) -> None:
    figlet = shutil.which("figlet")
    if figlet:
        subprocess.run([figlet, "-f", "slant", title], check=True)
        return

    python = shutil.which("python")
    if python:
        available = subprocess.run(
            [python, "-c", "import pyfiglet"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if available.returncode == 0:
            subprocess.run(
                [python, "-m", "pyfiglet", "-f", "slant", title], check=True
            )
            return

    print()
    for row in range(5):
        print("  ".join(ASCII_GLYPHS[char.upper()][row] for char in title))
    print()


def git(
    repo_root: Path,
    *arguments: str,
    check: bool = True,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_root), *arguments],
        check=check,
        text=True,
        capture_output=capture_output,
    )


def fetch_branches(repo_root: Path) -> str:
    """Fetch the target branch and main; return the ref to base the commit on.

    The branch may not exist on the remote yet (first run in a new repo); if
    so, start it from main and the later push creates it.
    """
    exists = git(
        repo_root,
        "ls-remote",
        "--exit-code",
        "--heads",
        REMOTE,
        BRANCH,
        check=False,
        capture_output=True,
    )
    if exists.returncode == 0:
        git(
            repo_root,
            "fetch",
            REMOTE,
            f"+refs/heads/{BRANCH}:refs/remotes/{REMOTE}/{BRANCH}",
            f"+refs/heads/main:refs/remotes/{REMOTE}/main",
        )
        return f"refs/remotes/{REMOTE}/{BRANCH}"
    if exists.returncode != 2:
        raise RuntimeError(
            f"Could not query {REMOTE} for branch {BRANCH}: {exists.stderr.strip()}"
        )
    print(f"Branch {BRANCH} not found on {REMOTE}; creating it from main.")
    git(repo_root, "fetch", REMOTE, f"+refs/heads/main:refs/remotes/{REMOTE}/main")
    return f"refs/remotes/{REMOTE}/main"


def remove_worktree(repo_root: Path, worktree: Path) -> None:
    result = git(
        repo_root,
        "worktree",
        "remove",
        "--force",
        str(worktree),
        check=False,
        capture_output=True,
    )
    if result.returncode:
        print(
            f"Warning: could not remove temporary worktree {worktree}: "
            f"{result.stderr.strip()}",
            file=sys.stderr,
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Commit python-programming and its workflows to origin/python, "
            "then merge that commit into origin/main."
        )
    )
    parser.add_argument(
        "message",
        nargs="*",
        help="commit message (default: Update python programming)",
    )
    args = parser.parse_args()
    commit_message = " ".join(
        args.message).strip() or "Update python programming"

    try:
        repo_root = Path(__file__).resolve().parent
        root_result = git(
            repo_root, "rev-parse", "--show-toplevel", capture_output=True
        )
        repo_root = Path(root_result.stdout.strip())
        source_folder = repo_root / FOLDER
        if not source_folder.is_dir():
            raise RuntimeError(
                f"Expected folder does not exist: {source_folder}")
        source_workflows = [repo_root / workflow for workflow in WORKFLOWS]
        missing_workflows = [
            str(workflow) for workflow in source_workflows if not workflow.is_file()
        ]
        if missing_workflows:
            raise RuntimeError(
                "Expected workflow file(s) not found: "
                + ", ".join(missing_workflows)
            )

        banner("Branch-Python")
        base_ref = fetch_branches(repo_root)
        with tempfile.TemporaryDirectory(prefix="git-python-") as temp_name:
            temp_root = Path(temp_name)
            target_worktree = temp_root / "target"
            main_worktree = temp_root / "main"
            target_added = False
            main_added = False
            try:
                git(
                    repo_root,
                    "worktree",
                    "add",
                    "--detach",
                    str(target_worktree),
                    base_ref,
                )
                target_added = True

                target_folder = target_worktree / FOLDER
                if target_folder.exists():
                    shutil.rmtree(target_folder)
                shutil.copytree(
                    source_folder,
                    target_folder,
                    symlinks=True,
                    ignore=shutil.ignore_patterns(".git"),
                )
                for workflow, source_workflow in zip(
                    WORKFLOWS, source_workflows
                ):
                    target_workflow = target_worktree / workflow
                    target_workflow.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source_workflow, target_workflow)

                git(
                    repo_root,
                    "-C",
                    str(target_worktree),
                    "add",
                    "-A",
                    "--",
                    FOLDER,
                    *WORKFLOWS,
                )
                staged = git(
                    repo_root,
                    "-C",
                    str(target_worktree),
                    "diff",
                    "--cached",
                    "--quiet",
                    "--",
                    FOLDER,
                    *WORKFLOWS,
                    check=False,
                )
                if staged.returncode == 1:
                    git(
                        repo_root,
                        "-C",
                        str(target_worktree),
                        "commit",
                        "-m",
                        commit_message,
                        "--",
                        FOLDER,
                        *WORKFLOWS,
                    )
                elif staged.returncode != 0:
                    raise RuntimeError(
                        "Could not inspect staged Python changes.")
                else:
                    print(
                        f"No changes in {FOLDER} or its workflows to commit."
                    )

                target_commit_result = git(
                    repo_root,
                    "-C",
                    str(target_worktree),
                    "rev-parse",
                    "HEAD",
                    capture_output=True,
                )
                target_commit = target_commit_result.stdout.strip()
                git(
                    repo_root,
                    "push",
                    REMOTE,
                    f"{target_commit}:refs/heads/{BRANCH}",
                )
                fetch_branches(repo_root)

                git(
                    repo_root,
                    "worktree",
                    "add",
                    "--detach",
                    str(main_worktree),
                    f"refs/remotes/{REMOTE}/main",
                )
                main_added = True
                git(
                    repo_root,
                    "-C",
                    str(main_worktree),
                    "merge",
                    "--no-edit",
                    target_commit,
                )
                main_commit_result = git(
                    repo_root,
                    "-C",
                    str(main_worktree),
                    "rev-parse",
                    "HEAD",
                    capture_output=True,
                )
                git(
                    repo_root,
                    "push",
                    REMOTE,
                    f"{main_commit_result.stdout.strip()}:refs/heads/main",
                )
            finally:
                if main_added:
                    remove_worktree(repo_root, main_worktree)
                if target_added:
                    remove_worktree(repo_root, target_worktree)
                git(repo_root, "worktree", "prune", check=False)

        banner("Done")
        return 0
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
