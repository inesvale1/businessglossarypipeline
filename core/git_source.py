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
            ["-C", str(checkout_dir), "-c", f"http.extraHeader={auth_header}", "fetch", "--depth", "1", "origin", settings.ref],
            env=env,
            redact=token,
        )
        _run_git(["-C", str(checkout_dir), "checkout", settings.ref], env=env, redact=token)
        _run_git(["-C", str(checkout_dir), "reset", "--hard", f"origin/{settings.ref}"], env=env, redact=token)
    else:
        checkout_dir.parent.mkdir(parents=True, exist_ok=True)
        _run_git(
            ["-c", f"http.extraHeader={auth_header}", "clone", "--depth", "1", "--branch", settings.ref, settings.git_url, str(checkout_dir)],
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
            ["-C", str(tree_dir), "-c", f"http.extraHeader={auth_header}", "fetch", "--filter=blob:none", "origin", settings.ref],
            env=env,
            redact=token,
        )
    else:
        tree_dir.parent.mkdir(parents=True, exist_ok=True)
        _run_git(
            [
                "-c", f"http.extraHeader={auth_header}", "clone", "--filter=blob:none", "--no-checkout",
                "--single-branch", "--branch", settings.ref, settings.git_url, str(tree_dir),
            ],
            env=env,
            redact=token,
        )


def list_tracked_files_with_size(tree_dir: Path, ref: str) -> list[tuple[str, int]]:
    """Return every file path at `origin/<ref>` with its byte size, reading
    only tree/blob *metadata* -- no file content is fetched by this call.
    Call `sync_blobless_tree` first.
    """
    result = subprocess.run(
        ["git", "-C", str(tree_dir), "ls-tree", "-r", "-l", f"origin/{ref}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise GitSourceError(f"git ls-tree falhou:\n{result.stderr.strip()}")

    files: list[tuple[str, int]] = []
    for line in result.stdout.splitlines():
        # format: "<mode> <type> <hash> <size>\t<path>"
        meta, _, path = line.partition("\t")
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


def read_blob(tree_dir: Path, ref: str, relative_path: str) -> str:
    """Fetch and return the text content of one file at `origin/<ref>`.

    On a blobless clone this triggers git's on-demand fetch of just this one
    blob from the server (transparent to the caller; git caches it locally
    afterwards) -- the whole point of only calling this for files that passed
    the relevance filter.
    """
    result = subprocess.run(
        ["git", "-C", str(tree_dir), "show", f"origin/{ref}:{relative_path}"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        raise GitSourceError(f"git show origin/{ref}:{relative_path} falhou:\n{result.stderr.strip()}")
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
