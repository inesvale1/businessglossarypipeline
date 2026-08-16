from __future__ import annotations

import base64
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitSourceError(RuntimeError):
    pass


@dataclass(frozen=True)
class GitRepoSettings:
    git_url: str
    ref: str = "main"
    username: str | None = None
    token_keyring_service: str | None = None
    token_keyring_username: str | None = None


def ensure_local_checkout(settings: GitRepoSettings, checkout_dir: Path) -> Path:
    """Clone (or update) `settings.git_url`@`settings.ref` into `checkout_dir`,
    with the full working tree materialized on disk (every file's content is
    downloaded). Use this when you already know exactly which folder(s) you
    want (see `sources_repos.<schema>.subpackages` in the README).

    For a large repo where you don't yet know which files matter, prefer
    `sync_blobless_tree` + `list_tracked_files_with_size` + `read_blob` below:
    they let you look at file *names* (and sizes) before paying to download
    any content.

    Authenticates via a Personal Access Token passed as a one-shot
    `http.extraHeader` on the git command line (`-c`, not `git config --global`),
    so the token is never written into `checkout_dir/.git/config` or into shell
    history/logs. LDAP is only the identity backend the *server* uses for its own
    login/API -- the git client itself never speaks LDAP, it just does HTTPS
    Basic auth, which is what this function sends.
    """
    auth_header, token = _auth_header(settings)
    env = _no_prompt_env()

    if (checkout_dir / ".git").exists():
        _run_git(
            ["-C", str(checkout_dir), "-c", "core.longpaths=true", "-c", f"http.extraHeader={auth_header}", "fetch", "--depth", "1", "origin", settings.ref],
            env=env,
            redact=token,
        )
        _run_git(["-C", str(checkout_dir), "-c", "core.longpaths=true", "checkout", settings.ref], env=env, redact=token)
        _run_git(["-C", str(checkout_dir), "-c", "core.longpaths=true", "reset", "--hard", f"origin/{settings.ref}"], env=env, redact=token)
    else:
        checkout_dir.parent.mkdir(parents=True, exist_ok=True)
        _run_git(
            ["-c", "core.longpaths=true", "-c", f"http.extraHeader={auth_header}", "clone", "--depth", "1", "--branch", settings.ref, settings.git_url, str(checkout_dir)],
            env=env,
            redact=token,
        )

    return checkout_dir


def sync_blobless_tree(settings: GitRepoSettings, tree_dir: Path) -> None:
    """Clone (or update) `settings.git_url`@`settings.ref` into `tree_dir` as a
    "blobless" partial clone (`--filter=blob:none`, `--no-checkout`): git
    downloads every commit and every directory/file *name*, but defers
    downloading any file *content* until something explicitly asks for a
    specific blob (see `read_blob`). This is what makes
    `list_tracked_files_with_size` cheap even on a huge monorepo.

    Requires the git server to support partial clone / protocol v2 (git 2.19+;
    GitLab, Gitea and GitHub have supported this for years over smart HTTP).
    If git.sefaz.ce.gov.br rejects `--filter`, use `ensure_local_checkout`
    (full clone) instead.
    """
    auth_header, token = _auth_header(settings)
    env = _no_prompt_env()

    if (tree_dir / ".git").exists():
        _run_git(
            ["-C", str(tree_dir), "-c", "core.longpaths=true", "-c", f"http.extraHeader={auth_header}", "fetch", "--filter=blob:none", "origin", settings.ref],
            env=env,
            redact=token,
        )
    else:
        tree_dir.parent.mkdir(parents=True, exist_ok=True)
        _run_git(
            [
                "-c", "core.longpaths=true", "-c", f"http.extraHeader={auth_header}", "clone", "--filter=blob:none", "--no-checkout",
                "--single-branch", "--branch", settings.ref, settings.git_url, str(tree_dir),
            ],
            env=env,
            redact=token,
        )


def list_tracked_files_with_size(tree_dir: Path, ref: str, settings: GitRepoSettings) -> list[tuple[str, int]]:
    """Return every file path at `origin/<ref>` with its byte size, reading
    only tree/blob *metadata* -- no file content is fetched by this call.
    Call `sync_blobless_tree` first.

    On a blobless clone, `-l` (sizes) can still require the promisor remote to
    be contacted for objects git doesn't have locally yet, so this needs the
    same auth header as the initial clone -- a plain `git ls-tree` without it
    fails auth on that on-demand fetch even though the clone itself succeeded.

    Uses `-z` (NUL-terminated entries) so paths come back raw. Without it, git
    C-quotes any path with spaces or non-ASCII bytes (both common in this
    codebase's Portuguese file/directory names) as octal escapes inside
    double quotes, e.g. `"...Arrecada\303\247\303\243o.odt"` -- passing that
    literal quoted string on to `git show origin/<ref>:<path>` (in `read_blob`)
    then fails with "path does not exist" because it's no longer the real path.
    """
    auth_header, token = _auth_header(settings)
    env = _no_prompt_env()
    result = subprocess.run(
        ["git", "-C", str(tree_dir), "-c", "core.longpaths=true", "-c", f"http.extraHeader={auth_header}", "ls-tree", "-r", "-l", "-z", f"origin/{ref}"],
        capture_output=True, env=env,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace")
        safe_stderr = stderr.replace(token, "***") if token else stderr
        raise GitSourceError(f"git ls-tree falhou:\n{safe_stderr.strip()}")

    files: list[tuple[str, int]] = []
    stdout = result.stdout.decode("utf-8", errors="replace")
    for entry in stdout.split("\0"):
        if not entry:
            continue
        # format: "<mode> <type> <hash> <size>\t<path>"
        meta, _, path = entry.partition("\t")
        if not path:
            continue
        parts = meta.split()
        if len(parts) < 4 or parts[1] != "blob":
            continue
        try:
            size = int(parts[3])
        except ValueError:
            size = 0
        files.append((path, size))
    return files


def list_recently_touched_paths(
    tree_dir: Path, ref: str, settings: GitRepoSettings, since_days: int,
) -> dict[str, str]:
    """Return every path touched by a commit in the last `since_days` days at
    `origin/<ref>`, mapped to the ISO-8601 date of the most recent such
    commit -- read entirely from commit/tree metadata already present after
    `sync_blobless_tree` (a blobless clone only omits *blob* content, not
    commits or trees), so this makes no additional network call and fetches
    no file content.

    Bounded with `git log --since=...` rather than walking full history: for
    a repo with years of commits, most of that history is irrelevant to an
    "is this file stale" question, and `--since` lets git stop traversing
    once it passes the date boundary instead of diffing every commit ever
    made (`git log --name-only` over full history is CPU-bound tree-diffing,
    not network, and was observed taking many minutes on this project's
    larger repos before this bound was added).

    A path *absent* from the returned dict was not touched in the window --
    core/relevance_filter.py treats that as stale and drops it. This is a
    deliberate behavior change from "look up this path's last-touched date":
    a path git.sefaz.ce.gov.br doesn't report in `--since` days is exactly
    the set of files the age filter exists to drop, so there is no case
    where knowing the *exact* older date would change the outcome.

    Used by core/relevance_filter.py to drop stale files (last touched more
    than `max_file_age_days` ago) before any blob is downloaded or sent to
    the LLM.

    Uses a control-character marker (`\\x01`) at the start of each commit's
    line instead of `-z`: `-z` NUL-terminates the whole `git log` stream
    (including multi-line commit bodies), which would make it ambiguous
    where one commit's file list ends and the next commit's marker begins.
    `\\x01` cannot appear in a commit date and is not a real filename
    character, so it's a safe one-line-per-commit marker while `--name-only`
    still emits one plain path per line beneath it.
    """
    auth_header, token = _auth_header(settings)
    env = _no_prompt_env()
    result = subprocess.run(
        [
            "git", "-C", str(tree_dir), "-c", "core.longpaths=true", "-c", f"http.extraHeader={auth_header}",
            "log", f"--since={int(since_days)}.days", "--name-only", "--no-renames", "--format=\x01%cI", f"origin/{ref}",
        ],
        capture_output=True, env=env,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace")
        safe_stderr = stderr.replace(token, "***") if token else stderr
        raise GitSourceError(f"git log falhou:\n{safe_stderr.strip()}")

    touched: dict[str, str] = {}
    current_date: str | None = None
    stdout = result.stdout.decode("utf-8", errors="replace")
    for line in stdout.split("\n"):
        if line.startswith("\x01"):
            current_date = line[1:].strip()
            continue
        path = line.strip()
        if not path or current_date is None:
            continue
        # First time we see a path (log is newest-first), that's its most
        # recent modification within the window -- skip later (older) hits.
        touched.setdefault(path, current_date)
    return touched


def read_blob(tree_dir: Path, ref: str, relative_path: str, settings: GitRepoSettings) -> str:
    """Fetch and return the text content of one file at `origin/<ref>`.

    On a blobless clone this triggers git's on-demand fetch of just this one
    blob from the server (transparent to the caller; git caches it locally
    afterwards) -- the whole point of only calling this for files that passed
    the relevance filter. That on-demand fetch needs the same auth header as
    the initial clone (see `list_tracked_files_with_size`). Also needs
    `core.longpaths=true`: on Windows, git disambiguates a `<rev>:<path>`
    argument from a literal filename by `stat()`-ing it relative to `tree_dir`
    first, and that combined path can exceed the 260-char MAX_PATH for a
    deeply nested Java package tree, failing with "Filename too long" even
    though no file is actually being written.
    """
    auth_header, token = _auth_header(settings)
    env = _no_prompt_env()
    result = subprocess.run(
        ["git", "-C", str(tree_dir), "-c", "core.longpaths=true", "-c", f"http.extraHeader={auth_header}", "show", f"origin/{ref}:{relative_path}"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
    )
    if result.returncode != 0:
        safe_stderr = result.stderr.replace(token, "***") if token else result.stderr
        raise GitSourceError(f"git show origin/{ref}:{relative_path} falhou:\n{safe_stderr.strip()}")
    return result.stdout


def read_blob_bytes(tree_dir: Path, ref: str, relative_path: str, settings: GitRepoSettings) -> bytes:
    """Like `read_blob`, but returns the raw bytes with no text decoding.

    Needed for binary document formats (.odt/.docx are zip containers, .pdf
    has its own binary structure) -- decoding them as UTF-8 text, as
    `read_blob` does for source code, would corrupt the bytes.
    """
    auth_header, token = _auth_header(settings)
    env = _no_prompt_env()
    result = subprocess.run(
        ["git", "-C", str(tree_dir), "-c", "core.longpaths=true", "-c", f"http.extraHeader={auth_header}", "show", f"origin/{ref}:{relative_path}"],
        capture_output=True, env=env,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace")
        safe_stderr = stderr.replace(token, "***") if token else stderr
        raise GitSourceError(f"git show origin/{ref}:{relative_path} falhou:\n{safe_stderr.strip()}")
    return result.stdout


def _auth_header(settings: GitRepoSettings) -> tuple[str, str]:
    token = _read_keyring(settings.token_keyring_service, settings.token_keyring_username)
    if not token:
        raise GitSourceError(
            f"Nenhum token encontrado no keyring (service={settings.token_keyring_service!r}, "
            f"username={settings.token_keyring_username!r}). Grave um Personal Access Token com:\n"
            f"  python scripts/store_keyring_secret.py --service {settings.token_keyring_service} "
            f"--username {settings.token_keyring_username}"
        )
    username = settings.username or settings.token_keyring_username or "git"
    header = "Authorization: Basic " + base64.b64encode(f"{username}:{token}".encode()).decode()
    return header, token


def _no_prompt_env() -> dict[str, str]:
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0"}


def _run_git(args: list[str], env: dict[str, str], redact: str) -> None:
    result = subprocess.run(["git", *args], capture_output=True, text=True, env=env)
    if result.returncode != 0:
        # Never let the token leak into a raised error / printed log.
        safe_stderr = result.stderr.replace(redact, "***") if redact else result.stderr
        raise GitSourceError(f"git {' '.join(_redact_headers(args))} falhou:\n{safe_stderr.strip()}")


def _redact_headers(args: list[str]) -> list[str]:
    return ["http.extraHeader=Authorization: Basic ***" if a.startswith("http.extraHeader=") else a for a in args]


def _read_keyring(service: str | None, username: str | None) -> str:
    if not service or not username:
        return ""
    try:
        import keyring
        return keyring.get_password(service, username) or ""
    except Exception:
        return ""
