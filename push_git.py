"""push_git.py
-----------
Automated Git stage, commit, and push utility for the crawler codebase.
Safely stages key tracked paths, commits with timestamped or custom messages,
and pushes to the designated or current git branch without crashing on Windows.
"""

from __future__ import annotations

import argparse
import subprocess as sp
from datetime import datetime
from pathlib import Path

DEFAULT_TRACKED_PATHS: list[str] = [
    ".gitignore",
    ".gitattributes",
    "pytest.ini",
    "requirements.txt",
    "cli.py",
    "crawler.py",
    "timeout_manager.py",
    "emergency_stop.py",
    "filter_websites.py",
    "safeguard_audit.py",
    "safeguard_captcha.py",
    "safeguard_config.py",
    "safeguard_engine.py",
    "safeguard_state.py",
    "safeguard_traffic.py",
    "push_git.py",
    "Collectors/",
    "Helpers/",
    "preprocessing/",
    "resources/",
    "scripts/",
    "tests/",
]


def find_repo_root(start_path: Path | str | None = None) -> Path:
    """Find the root directory containing .git, starting from start_path or the script's dir."""
    current = Path(start_path).resolve() if start_path else Path(__file__).resolve().parent
    for candidate in [current, *current.parents]:
        if (candidate / ".git").is_dir():
            return candidate
    return current


def get_current_branch(repo_root: Path) -> str:
    """Get the name of the currently checked out git branch."""
    try:
        res = sp.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
        branch = res.stdout.strip()
        if branch and branch != "HEAD":
            return branch
    except Exception:
        pass
    return "main"


def get_branch_remote(repo_root: Path, branch: str) -> str:
    """Get the configured remote for a branch, defaulting to 'origin'."""
    try:
        res = sp.run(
            ["git", "config", f"branch.{branch}.remote"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
        rem = res.stdout.strip()
        if rem:
            return rem
    except Exception:
        pass
    return "origin"


def run_cmd(
    args: list[str],
    cwd: Path,
    check: bool = True,
    capture_output: bool = True,
) -> sp.CompletedProcess:
    """Run a single subprocess command safely without shell=True quoting issues."""
    return sp.run(args, cwd=cwd, check=check, capture_output=capture_output, text=True)


def stage_tracked_paths(repo: Path, paths: list[str]) -> list[str]:
    """Safely stage the provided paths, skipping missing ones and gracefully ignoring errors on ignored patterns."""
    existing_targets = [p for p in paths if (repo / p).exists()]
    if not existing_targets:
        return []

    # First attempt: batch add with --ignore-errors
    try:
        run_cmd(["git", "add", "--ignore-errors", *existing_targets], cwd=repo)
        return existing_targets
    except sp.CalledProcessError:
        pass

    # Fallback: stage each path individually so an ignored directory doesn't abort the rest
    staged: list[str] = []
    for target in existing_targets:
        try:
            run_cmd(["git", "add", "--ignore-errors", target], cwd=repo)
            staged.append(target)
        except sp.CalledProcessError:
            try:
                run_cmd(["git", "add", target], cwd=repo)
                staged.append(target)
            except sp.CalledProcessError:
                # Target might be completely excluded by .gitignore
                pass

    return staged


def run_full(
    root_path: Path | str | None = None,
    message: str | None = None,
    branch: str | None = None,
    remote: str | None = None,
    push: bool = True,
    stage_all: bool = False,
    tracked_paths: list[str] | None = None,
    no_verify: bool = True,
) -> str:
    """Run the complete git stage, commit, and push flow."""
    repo = find_repo_root(root_path)
    output_lines: list[str] = [f"[REPO] Repository root: {repo}"]

    # 1. Verify git repository
    if not (repo / ".git").is_dir():
        init_res = run_cmd(["git", "init"], cwd=repo)
        output_lines.append(f"[INIT] {init_res.stdout.strip() or 'Initialized git repo'}")

    # 2. Stage tracked files
    paths_to_stage = tracked_paths or DEFAULT_TRACKED_PATHS
    if stage_all:
        try:
            run_cmd(["git", "add", "-A"], cwd=repo)
            output_lines.append("[ADD] Staged all unignored changes (git add -A)")
        except sp.CalledProcessError as exc:
            output_lines.append(f"[ADD FAILED] {exc.stderr or exc.stdout or exc}")
    else:
        staged = stage_tracked_paths(repo, paths_to_stage)
        output_lines.append(f"[ADD] Staged target paths ({len(staged)} path(s))")

    # 3. Check if anything is staged to commit
    diff_cached_res = run_cmd(["git", "diff", "--cached", "--name-only"], cwd=repo)
    staged_files = [f for f in diff_cached_res.stdout.strip().splitlines() if f.strip()]

    commit_msg = message or f"updates {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    if staged_files:
        try:
            commit_res = run_cmd(["git", "commit", "-m", commit_msg], cwd=repo)
            first_line = commit_res.stdout.strip().splitlines()[0] if commit_res.stdout.strip() else "Committed"
            output_lines.append(f"[COMMIT] {first_line}")
        except sp.CalledProcessError as c_exc:
            output_lines.append(f"[COMMIT] {c_exc.stdout or c_exc.stderr or c_exc}")
    else:
        output_lines.append("[COMMIT] No staged changes; nothing to commit.")

    # 4. Push to remote
    target_branch = branch or get_current_branch(repo)
    target_remote = remote or get_branch_remote(repo, target_branch)
    if push:
        try:
            push_cmd = [
                "git",
                "-c",
                "http.postBuffer=524288000",
                "-c",
                "http.version=HTTP/1.1",
                "push",
            ]
            if no_verify:
                push_cmd.append("--no-verify")
            push_cmd.extend(["-u", target_remote, target_branch])
            push_res = run_cmd(push_cmd, cwd=repo)
            output_lines.append(f"[PUSH] Successfully pushed to {target_remote}/{target_branch}")
            if push_res.stdout.strip():
                output_lines.append(push_res.stdout.strip())
            if push_res.stderr.strip():
                output_lines.append(push_res.stderr.strip())
        except sp.CalledProcessError as exc:
            err_msg = f"[PUSH FAILED] Push to {target_remote}/{target_branch} failed:\n{exc.stderr or exc.stdout or exc}"
            output_lines.append(err_msg)
    else:
        output_lines.append(f"[PUSH] Skipped push (--no-push / dry-run). Remote: {target_remote}, Branch: {target_branch}")

    return "\n".join(output_lines)


def run_partial_cmd(root_path: Path | str | None = None) -> list[str]:
    """Execute git commands sequentially as split argument lists."""
    repo = find_repo_root(root_path)
    branch = get_current_branch(repo)

    commands: list[list[str]] = [
        ["git", "init"],
        ["git", "add", "--ignore-errors", *[p for p in DEFAULT_TRACKED_PATHS if (repo / p).exists()]],
        ["git", "commit", "-m", f"updates {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"],
        ["git", "-c", "http.postBuffer=524288000", "-c", "http.version=HTTP/1.1", "push", "--no-verify", "-u", "origin", branch],
    ]

    outputs: list[str] = []
    for cmd in commands:
        try:
            res = sp.run(cmd, cwd=repo, capture_output=True, text=True, check=False)
            out = res.stdout.strip() or res.stderr.strip()
            if out:
                outputs.append(f"[{' '.join(cmd[:2])}] {out}")
        except Exception as exc:
            outputs.append(f"[{' '.join(cmd[:2])}] Error: {exc}")

    return outputs


def main() -> None:
    """CLI runner for push_git."""
    parser = argparse.ArgumentParser(
        description="Auto Git push utility: stage tracked code files, commit, and push to current branch."
    )
    parser.add_argument(
        "-m",
        "--message",
        dest="message",
        type=str,
        default=None,
        help="Commit message (default: 'updates <timestamp>').",
    )
    parser.add_argument(
        "-b",
        "--branch",
        dest="branch",
        type=str,
        default=None,
        help="Target branch to push (default: active checked-out branch).",
    )
    parser.add_argument(
        "-r",
        "--remote",
        dest="remote",
        type=str,
        default=None,
        help="Git remote name (defaults to tracked upstream remote for active branch, or 'origin').",
    )
    parser.add_argument(
        "-a",
        "--all",
        dest="stage_all",
        action="store_true",
        default=False,
        help="Stage all modified and unignored files via 'git add -A'.",
    )
    parser.add_argument(
        "--no-push",
        "--dry-run",
        dest="no_push",
        action="store_true",
        default=False,
        help="Stage and commit changes without pushing to remote.",
    )
    parser.add_argument(
        "--verify",
        dest="no_verify",
        action="store_false",
        default=True,
        help="Run git pre-push hooks (by default --no-verify is used to prevent hanging on massive LFS historical scans).",
    )
    parser.add_argument(
        "-p",
        "--path",
        dest="path",
        type=str,
        default=None,
        help="Path to repository root (defaults to auto-detecting .git).",
    )

    args = parser.parse_args()
    root_path = find_repo_root(args.path)

    result = run_full(
        root_path=root_path,
        message=args.message,
        branch=args.branch,
        remote=args.remote,
        push=not args.no_push,
        stage_all=args.stage_all,
        no_verify=args.no_verify,
    )
    print(result)


if __name__ == "__main__":
    main()