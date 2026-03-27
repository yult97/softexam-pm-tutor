#!/usr/bin/env python3
"""Update an installed skill in one command.

Supports two common install modes:
1. The installed skill directory is a git checkout -> uses `git pull --ff-only`
2. The installed skill directory is a plain copied folder -> clones fresh content
   into a temp directory and replaces the installed files while preserving local data

Preserved local data:
- assets/信息系统项目管理师教程(可搜索版).pdf
- references/book_chunks.jsonl
- learnings/feedback_events.jsonl
- learnings/promotion_candidates.md
- shared_memory/*.json
- shared_memory/*.jsonl
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


DEFAULT_REPO_URL = "https://github.com/yult97/softexam-pm-tutor.git"
DEFAULT_BRANCH = "main"
PRESERVE_GLOBS = (
    "assets/*.pdf",
    "references/book_chunks.jsonl",
    "learnings/*.jsonl",
    "learnings/*.md",
    "shared_memory/*.json",
    "shared_memory/*.jsonl",
)


def log(message: str) -> None:
    print(f"[update-skill] {message}")


def fail(message: str) -> int:
    print(f"[update-skill] ERROR: {message}", file=sys.stderr)
    return 1


def default_skill_dir() -> Path:
    if "__file__" in globals():
        script_dir = Path(__file__).resolve().parent
        if script_dir.name == "scripts":
            return script_dir.parent
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    return codex_home / "skills" / "softexam-pm-tutor"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update the installed softexam-pm-tutor skill.")
    parser.add_argument("--skill-dir", type=Path, default=default_skill_dir(), help="Installed skill directory.")
    parser.add_argument("--repo-url", default=DEFAULT_REPO_URL, help="Git repository URL.")
    parser.add_argument("--branch", default=DEFAULT_BRANCH, help="Git branch or ref to update from.")
    parser.add_argument(
        "--allow-dirty-git",
        action="store_true",
        help="Allow git checkout update even if the installed repo has local modifications.",
    )
    return parser.parse_args()


def run(command: list[str], cwd: Path | None = None) -> str:
    completed = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def is_git_repo(skill_dir: Path) -> bool:
    return (skill_dir / ".git").exists()


def git_dirty(skill_dir: Path) -> bool:
    status = run(["git", "status", "--porcelain"], cwd=skill_dir)
    return bool(status.strip())


def update_git_checkout(skill_dir: Path, repo_url: str, branch: str, allow_dirty: bool) -> None:
    if git_dirty(skill_dir) and not allow_dirty:
        raise RuntimeError("Installed skill is a git repo with local changes. Commit/stash first or pass --allow-dirty-git.")

    remotes = run(["git", "remote"], cwd=skill_dir).splitlines()
    if "origin" not in remotes:
        run(["git", "remote", "add", "origin", repo_url], cwd=skill_dir)

    current_branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=skill_dir) or branch
    target_branch = current_branch if current_branch != "HEAD" else branch

    log(f"Updating git checkout on branch {target_branch}...")
    run(["git", "fetch", "origin", branch, "--tags", "--prune"], cwd=skill_dir)
    run(["git", "pull", "--ff-only", "origin", target_branch], cwd=skill_dir)


def clone_repo(tmp_root: Path, repo_url: str, branch: str) -> Path:
    checkout_dir = tmp_root / "repo"
    run(["git", "clone", "--depth", "1", "--branch", branch, repo_url, str(checkout_dir)])
    return checkout_dir


def snapshot_preserved_files(skill_dir: Path, preserve_dir: Path) -> None:
    preserve_dir.mkdir(parents=True, exist_ok=True)
    for pattern in PRESERVE_GLOBS:
        for source in skill_dir.glob(pattern):
            if not source.is_file():
                continue
            relative = source.relative_to(skill_dir)
            target = preserve_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def copy_repo_without_git(source_dir: Path, target_dir: Path) -> None:
    def ignore(directory: str, names: list[str]) -> set[str]:
        ignored: set[str] = set()
        if Path(directory).resolve() == source_dir.resolve():
            ignored.add(".git")
        ignored.update(name for name in names if name == "__pycache__")
        return ignored

    shutil.copytree(source_dir, target_dir, ignore=ignore)


def overlay_preserved_files(preserve_dir: Path, target_dir: Path) -> None:
    if not preserve_dir.exists():
        return
    for source in preserve_dir.rglob("*"):
        if not source.is_file():
            continue
        relative = source.relative_to(preserve_dir)
        target = target_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def replace_plain_install(skill_dir: Path, repo_url: str, branch: str) -> None:
    with tempfile.TemporaryDirectory(prefix="softexam-pm-tutor-update-") as tmp_name:
        tmp_root = Path(tmp_name)
        preserve_dir = tmp_root / "preserve"
        new_dir = tmp_root / "new-skill"

        if skill_dir.exists():
            snapshot_preserved_files(skill_dir, preserve_dir)

        checkout_dir = clone_repo(tmp_root, repo_url, branch)
        copy_repo_without_git(checkout_dir, new_dir)
        overlay_preserved_files(preserve_dir, new_dir)

        backup_dir = skill_dir.with_name(f"{skill_dir.name}.backup")
        if backup_dir.exists():
            shutil.rmtree(backup_dir)

        if skill_dir.exists():
            skill_dir.rename(backup_dir)
        try:
            new_dir.rename(skill_dir)
        except Exception:
            if skill_dir.exists():
                shutil.rmtree(skill_dir, ignore_errors=True)
            if backup_dir.exists():
                backup_dir.rename(skill_dir)
            raise
        else:
            shutil.rmtree(backup_dir, ignore_errors=True)


def main() -> int:
    args = parse_args()
    skill_dir = args.skill_dir.expanduser().resolve()

    if is_git_repo(skill_dir):
        try:
            update_git_checkout(skill_dir, args.repo_url, args.branch, args.allow_dirty_git)
        except subprocess.CalledProcessError as exc:
            return fail(exc.stderr.strip() or str(exc))
        except RuntimeError as exc:
            return fail(str(exc))
    else:
        log("Installed skill is not a git checkout. Replacing files from a fresh clone while preserving local data...")
        try:
            replace_plain_install(skill_dir, args.repo_url, args.branch)
        except subprocess.CalledProcessError as exc:
            return fail(exc.stderr.strip() or str(exc))
        except Exception as exc:  # pragma: no cover - best effort fallback
            return fail(str(exc))

    log(f"Updated skill at {skill_dir}")
    log("Restart Codex / Claude Code / OpenClaw to pick up the new version.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
