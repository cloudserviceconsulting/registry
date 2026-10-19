#!/usr/bin/env python3
"""Install or update ssm-wrapper from the public CLI registry (stdlib only for ``--zip`` mode).

``make install`` runs this script with no arguments: it resolves the update
channel from user config, downloads the latest universal zip, verifies SHA256,
builds with ``make -j1 build``, then ``sudo make -j1 install-internal`` when the
bundle Makefile defines that target; otherwise copies ``dist/ssm`` and bin
wrappers the same way ``install-internal`` does (for older registry zips).

By default this installs ``ssm`` and ``ssmc`` wrappers plus shell completions for
the shells listed under ``cli_updates.completion_shells`` in ``config.json``
(default ``bash`` only). Omit **``--shell``** / **``--alternative``** to follow that
config (same keys ``ssm -up`` uses). CLI flags override config for this run.

Use ``--alternative`` for ``ec2`` / ``ecs`` wrappers, matching completions (``ec2``, ``ecs``, ``ssm.py``), and a ``ssm``
symlink to ``/usr/local/ssm/ssm`` regardless of ``cli_updates.install_layout``.

``ssm -up`` invokes ``--zip PATH --sha256 HEX`` after the parent process has
already downloaded and verified the bundle; layout and shells follow the same
``cli_updates.install_layout`` and ``cli_updates.completion_shells`` keys (see
``libs.core.meta``). Optional ``--release-version`` enables
the post-install release-notes prompt (reads ``CHANGELOG.md`` from the extracted
bundle; same opt-out as the CLI used: ``SSM_SKIP_CHANGELOG_PROMPT=1``).

Optional ``--channel stable|nightly`` chooses the registry tree for this run.
After a successful install, writes ``cli_updates.channel`` (JSON ``null`` for
offline ``--zip`` installs without ``--channel`` — typical development),
``cli_updates.install_layout``, and ``cli_updates.completion_shells`` to match
what was installed. Useful for ``curl … | python3 -`` bootstrap installs. In that
pipe mode ``sys.stdin`` is the script stream, not the terminal; interactive
``sudo`` still uses the controlling TTY via ``/dev/tty`` when available.

This script is stdlib-only (no ``libs`` imports): release-notes parsing mirrors
``libs/core/changelog_preview.py`` so it runs under plain ``python3``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, TextIO, Tuple

# Keep in sync with libs/core/meta.py and libs/config/registry_urls.py
REGISTRY_RAW_BASE_STABLE = (
    "https://registry.demothinkupc.com/"
    "v1/cli/csc/ssm-wrapper"
)
REGISTRY_RAW_BASE_NIGHTLY = (
    "https://registry.demothinkupc.com/"
    "v1/cli/csc/ssm-wrapper-nightly"
)

# Makefile reads this to redirect pip / PyInstaller (see Makefile ``_Q``).
_INSTALL_QUIET_ENV = "INSTALL_FROM_REGISTRY_QUIET"
_INSTALL_LAYOUT_ENV = "INSTALL_LAYOUT"
_INSTALL_COMPLETION_SHELLS_ENV = "INSTALL_COMPLETION_SHELLS"

# Same paths as Makefile ``completions-cleanup-internal`` (``prefix=/usr/local``).
_INSTALL_TOTAL_STEPS = 6


class _Interrupted(Exception):
    """Installation stopped by user interrupt or signal."""

    def __init__(self, phase: str) -> None:
        super().__init__(phase)
        self.phase = phase


_INSTALL_PHASE: Optional[str] = None


def _install_verbose() -> bool:
    """True when install subprocess output should stream to the terminal."""
    if os.environ.get("SSM_INSTALL_VERBOSE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        return True
    return "--verbose" in sys.argv


def _step(n: int, text: str) -> None:
    """Print a numbered install step to stderr."""
    print(f"[{n}/{_INSTALL_TOTAL_STEPS}] {text}", file=sys.stderr, flush=True)


def _note(text: str) -> None:
    """Print a sub-line under the current step."""
    print(f"  {text}", file=sys.stderr, flush=True)


def _format_elapsed(seconds: float) -> str:
    s = int(max(0, seconds))
    m, sec = divmod(s, 60)
    if m:
        return f"{m}m{sec:02d}s"
    return f"{sec}s"


def _subprocess_interrupted(rc: int) -> bool:
    return rc < 0 or rc == 130


_REGISTRY_COMPLETION_INSTALL_PATHS: Tuple[str, ...] = (
    "/usr/local/share/bash-completion/completions/ssm",
    "/usr/local/share/bash-completion/completions/ssmc",
    "/usr/local/share/bash-completion/completions/ec2",
    "/usr/local/share/bash-completion/completions/ecs",
    "/usr/local/share/bash-completion/completions/ssm.py",
    "/usr/local/share/zsh/site-functions/_ssm",
    "/usr/local/share/zsh/site-functions/_ssmc",
    "/usr/local/share/zsh/site-functions/_ec2",
    "/usr/local/share/zsh/site-functions/_ecs",
    "/usr/local/share/zsh/site-functions/_ssm.py",
)


def _cleanup_registry_completion_files(run_privileged: Callable[[List[str]], None]) -> None:
    """Remove installed completion scripts (matches Makefile ``completions-cleanup-internal``)."""
    for path in _REGISTRY_COMPLETION_INSTALL_PATHS:
        run_privileged(["rm", "-f", path])


def _normalize_completion_shells(shell_append: Optional[List[str]]) -> Tuple[str, ...]:
    """Return ordered unique shells for completion installs (default: bash only)."""
    if not shell_append:
        return ("bash",)
    seen: List[str] = []
    for s in shell_append:
        if s not in seen:
            seen.append(s)
    return tuple(seen)


def _controlling_tty_available() -> bool:
    """Return True if this session has a readable controlling terminal (``/dev/tty``)."""
    try:
        f = open("/dev/tty", "r")
    except OSError:
        return False
    else:
        f.close()
        return True


def _open_controlling_tty_stdin() -> Optional[TextIO]:
    """
    Open the controlling terminal for reading (e.g. ``sudo`` password prompts).

    When the script is run as ``curl … | python3 -``, standard input is the
    consumed download pipe, not an interactive TTY.
    """
    try:
        return open("/dev/tty", "r", encoding="utf-8", errors="replace")
    except OSError:
        return None


def _can_prompt_privileged_commands() -> bool:
    """True if ``sudo`` may be able to read a password (root, stdin TTY, or ``/dev/tty``)."""
    if os.geteuid() == 0:
        return True
    if sys.stdin.isatty():
        return True
    return _controlling_tty_available()


def _changelog_skip_prompt() -> bool:
    """True when the post-install release-notes prompt should be skipped."""
    return os.environ.get("SSM_SKIP_CHANGELOG_PROMPT", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _extract_keepachangelog_section(text: str, version: str) -> Optional[str]:
    """
    Return the body under ``## [version]`` (optional date on the header line).

    Kept in sync with ``libs.core.changelog_preview.extract_keepachangelog_section``.
    """
    v = version.strip()
    if not v or "\n" in v or "[" in v or "]" in v:
        return None
    pattern = (
        rf"(?ms)^## \[{re.escape(v)}\][^\n]*\n"
        r"(.*?)"
        r"(?=^## \[|\Z)"
    )
    m = re.search(pattern, text)
    if not m:
        return None
    body = m.group(1).strip()
    lines = [
        ln
        for ln in body.splitlines()
        if not ln.strip().startswith("Tag release:")
    ]
    out = "\n".join(lines).strip()
    return out or None


_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def _truncate_url(url: str, max_len: int = 52) -> str:
    u = url.strip()
    if len(u) <= max_len:
        return u
    return u[: max_len - 3] + "..."


def _strip_inline_markdown(segment: str) -> str:
    """
    Turn common inline Markdown into plain text for terminal display.

    Handles ``code``, **bold**, __bold__, and [label](url) (URL shown in
    parentheses, truncated when very long).
    """
    s = segment
    s = re.sub(r"`([^`]+)`", r"\1", s)
    s = _MD_LINK_RE.sub(
        lambda m: f"{m.group(1).strip()} ({_truncate_url(m.group(2))})",
        s,
    )
    while "**" in s:
        s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s, count=1)
    s = re.sub(r"__(?!_)([^_]+)__(?!_)", r"\1", s)
    return s


def _release_notes_plain_text(section: str) -> str:
    """
    Convert a Keep-a-Changelog section body from Markdown to plain, structured text.

    ATX headings become title lines with spacing; list markers become ``•`` bullets
    with light indentation for nested items; horizontal rules are skipped.
    """
    out: List[str] = []
    for raw_line in section.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            if out and out[-1] != "":
                out.append("")
            continue

        stripped = line.strip()
        if re.fullmatch(r"[\s*_\-]{3,}", stripped):
            if out and out[-1] != "":
                out.append("")
            continue

        hm = re.match(r"^#{1,6}\s+(.+?)(?:\s+#+\s*)?$", stripped)
        if hm:
            title = _strip_inline_markdown(hm.group(1).strip())
            if out and out[-1] != "":
                out.append("")
            out.append(title)
            out.append("")
            continue

        bm = re.match(r"^(\s*)([-*]|\d+\.)\s+(.*)$", raw_line)
        if bm:
            indent_s = bm.group(1)
            text = bm.group(3)
            depth = min(len(indent_s.replace("\t", "    ")) // 2, 4)
            bullet = "  " * depth + "• "
            out.append(bullet + _strip_inline_markdown(text))
            continue

        out.append(_strip_inline_markdown(stripped))

    while len(out) > 1 and out[-1] == "" and out[-2] == "":
        out.pop()
    return "\n".join(out).strip()


def _preview_lines(
    text: str,
    *,
    max_lines: int = 60,
    max_line_len: int = 96,
) -> List[str]:
    """Split *text* into terminal lines with soft length and line caps."""
    lines: List[str] = []
    for raw in text.splitlines():
        s = raw.rstrip()
        if len(s) > max_line_len:
            s = s[: max_line_len - 3] + "..."
        lines.append(s)
        if len(lines) >= max_lines:
            lines.append("… (truncated)")
            break
    return lines


def _print_release_notes(title: str, lines: List[str]) -> None:
    """Print a simple changelog block (no themed terminal styles; stdlib-only)."""
    print()
    print(title)
    print()
    for ln in lines:
        print(ln)
    print()


def _maybe_prompt_release_notes(bundle_root: Path, release_label: str) -> None:
    """
    Offer to print the Keep a Changelog section for *release_label*.

    Reads ``CHANGELOG.md`` from the extracted bundle tree (the version that was
    just built and installed). Uses ``/dev/tty`` when ``sys.stdin`` is not a TTY
    (e.g. ``curl … | python3 -``).
    """
    if _changelog_skip_prompt():
        return
    rel = release_label.strip()
    if not rel:
        return
    chlog = bundle_root / "CHANGELOG.md"
    if not chlog.is_file():
        return
    try:
        raw = chlog.read_text(encoding="utf-8")
    except OSError:
        return
    section = _extract_keepachangelog_section(raw, rel)
    if not section:
        return
    plain = _release_notes_plain_text(section)
    lines = _preview_lines(plain)
    if not lines:
        return

    tty_in: Optional[TextIO] = None
    if sys.stdin.isatty():
        read_fn = None
    else:
        tty_in = _open_controlling_tty_stdin()
        if tty_in is None:
            return
        read_fn = tty_in

    if not sys.stdout.isatty():
        if tty_in is not None:
            tty_in.close()
        return

    prompt = (
        f"Show release notes for {rel}? [y/N, Enter to skip, auto-skip in 20s] "
    )
    ans = ""
    try:
        if read_fn is None:
            print(prompt, end="", flush=True)
            rlist, _, _ = select.select([sys.stdin], [], [], 20.0)
            if rlist:
                ans = sys.stdin.readline().strip().lower()
            else:
                print()
                return
        else:
            print(prompt, end="", flush=True)
            fd = read_fn.fileno()
            rlist, _, _ = select.select([fd], [], [], 20.0)
            if rlist:
                ans = read_fn.readline().strip().lower()
            else:
                print()
                return
    except (EOFError, KeyboardInterrupt):
        print()
        return
    finally:
        if tty_in is not None:
            tty_in.close()
    if ans != "y":
        return
    _print_release_notes(f"Changelog — [{rel}]", lines)


def _bundle_subprocess_env(
    *,
    layout: str = "default",
    completion_shells: Sequence[str] = ("bash",),
    quiet: bool = False,
) -> Dict[str, str]:
    env = {**os.environ}
    if quiet:
        env[_INSTALL_QUIET_ENV] = "1"
    env[_INSTALL_COMPLETION_SHELLS_ENV] = " ".join(completion_shells)
    if layout == "alternative":
        env[_INSTALL_LAYOUT_ENV] = "alternative"
    return env


def _die(msg: str, code: int = 1) -> None:
    print(f"error: {msg}", file=sys.stderr, flush=True)
    raise SystemExit(code)


def _caller_home() -> Path:
    """Home directory of the invoking user (handles ``sudo``)."""
    su = os.environ.get("SUDO_UID", "").strip()
    if su.isdigit():
        import pwd

        return Path(pwd.getpwuid(int(su)).pw_dir).resolve()
    return Path.home().resolve()


def _xdg_config_home() -> Path:
    """XDG config home for the invoking user (``sudo`` uses ``SUDO_UID`` home).

    Honors ``XDG_CONFIG_HOME`` when set; otherwise ``<caller_home>/.config``.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg:
        return Path(xdg).expanduser().resolve()
    return (_caller_home() / ".config").resolve()


def _ssm_config_dir() -> Path:
    root = os.environ.get("SSM_HOME", "").strip()
    if root:
        return Path(root).expanduser().resolve()
    return _xdg_config_home() / "ssm"


def _read_install_layout_from_config() -> str:
    """Return ``default`` or ``alternative`` from user ``config.json`` when set."""
    for base in (_ssm_config_dir(), _caller_home() / ".ssm"):
        path = base / "config.json"
        if not path.is_file():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            continue
        if not isinstance(raw, dict):
            continue
        cu = raw.get("cli_updates")
        if not isinstance(cu, dict):
            continue
        il = cu.get("install_layout")
        if isinstance(il, str):
            low = il.strip().lower()
            if low in ("default", "alternative"):
                return low
    return "default"


def _read_completion_shells_from_config() -> Tuple[str, ...]:
    """Return ``bash`` / ``zsh`` list from user ``config.json`` (default: bash only)."""
    for base in (_ssm_config_dir(), _caller_home() / ".ssm"):
        path = base / "config.json"
        if not path.is_file():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            continue
        if not isinstance(raw, dict):
            continue
        cu = raw.get("cli_updates")
        if not isinstance(cu, dict):
            continue
        shells_ordered: List[str] = []
        cs = cu.get("completion_shells")
        if isinstance(cs, list):
            for item in cs:
                if not isinstance(item, str):
                    continue
                s = item.strip().lower()
                if s in ("bash", "zsh") and s not in shells_ordered:
                    shells_ordered.append(s)
        if shells_ordered:
            return tuple(shells_ordered)
    return ("bash",)


def _chown_path_to_invoker(path: Path) -> None:
    """When running as root via ``sudo``, set ownership to ``SUDO_UID`` / ``SUDO_GID``."""
    su = os.environ.get("SUDO_UID", "").strip()
    sg = os.environ.get("SUDO_GID", "").strip()
    if os.geteuid() != 0 or not su.isdigit() or not sg.isdigit():
        return
    try:
        uid, gid = int(su), int(sg)
        os.chown(path, uid, gid)
    except OSError:
        pass


def _persist_cli_update_prefs(
    *,
    registry_channel: Optional[str] = None,
    install_layout: Optional[str] = None,
    completion_shells: Optional[Sequence[str]] = None,
    clear_channel: bool = False,
    pinned_version: Optional[str] = None,
    clear_pinned: bool = False,
) -> None:
    """Merge ``cli_updates`` install prefs into the invoking user's ``config.json``.

    Uses the same directory resolution as config reads (``SSM_HOME`` / XDG under
    ``SUDO_UID``). Skips the write when an existing ``config.json`` is not valid
    JSON (avoids clobbering a broken file).

    Args:
        registry_channel: ``stable`` or ``nightly`` when the channel should be set.
        install_layout: ``default`` or ``alternative`` when layout should be updated.
        completion_shells: Shell names when ``completion_shells`` should be updated.
        clear_channel: When True and *registry_channel* is omitted, set ``channel``
            to JSON ``null`` (registry/offline zip installs without ``--channel``).
            When False and *registry_channel* is omitted, leave ``channel`` unchanged.
        pinned_version: When set, writes ``cli_updates.pinned_version``.
        clear_pinned: When True, sets ``pinned_version`` to JSON ``null``.
    """
    path = _ssm_config_dir() / "config.json"
    raw: Dict[str, Any]
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return
        if not isinstance(loaded, dict):
            return
        raw = loaded
    else:
        raw = {}
    cu = raw.get("cli_updates")
    if not isinstance(cu, dict):
        cu = {}
    cu.pop("last_applied_version", None)
    if registry_channel in ("stable", "nightly"):
        cu["channel"] = registry_channel
    elif clear_channel:
        cu["channel"] = None
    if install_layout is not None:
        layout = install_layout.strip().lower()
        cu["install_layout"] = layout if layout == "alternative" else "default"
    if completion_shells is not None:
        shells_ordered: List[str] = []
        for item in completion_shells:
            if not isinstance(item, str):
                continue
            s = item.strip().lower()
            if s in ("bash", "zsh") and s not in shells_ordered:
                shells_ordered.append(s)
        cu["completion_shells"] = shells_ordered or ["bash"]
    if clear_pinned:
        cu["pinned_version"] = None
    elif pinned_version is not None:
        cu["pinned_version"] = pinned_version.strip() or None
    raw["cli_updates"] = cu
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
        _chown_path_to_invoker(path)
        if path.parent.name == "ssm":
            _chown_path_to_invoker(path.parent)
    except OSError:
        pass


def _read_update_channel() -> str:
    """Return ``stable`` or ``nightly`` from user ``config.json`` when set."""
    for base in (_ssm_config_dir(), _caller_home() / ".ssm"):
        path = base / "config.json"
        if not path.is_file():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            continue
        if not isinstance(raw, dict):
            continue
        cu = raw.get("cli_updates")
        if isinstance(cu, dict):
            ch = cu.get("channel")
            if isinstance(ch, str) and ch.strip().lower() in ("stable", "nightly"):
                return ch.strip().lower()
    return "stable"


def _resolve_install_channel(explicit: Optional[str]) -> str:
    """Return registry channel: *explicit* if set, else :func:`_read_update_channel`."""
    if explicit is not None:
        choice = explicit.strip().lower()
        if choice in ("stable", "nightly"):
            return choice
    return _read_update_channel()


def _registry_base(channel: str) -> str:
    ch = channel.strip().lower()
    if ch == "nightly":
        return REGISTRY_RAW_BASE_NIGHTLY.rstrip("/")
    return REGISTRY_RAW_BASE_STABLE.rstrip("/")


def _http_get(url: str, timeout: int = 120) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "ssm-wrapper-install-from-registry",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _http_get_with_progress(url: str, timeout: int = 180) -> bytes:
    """Download *url* with optional progress on stderr."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "ssm-wrapper-install-from-registry",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        total = resp.headers.get("Content-Length")
        total_n = int(total) if total and str(total).isdigit() else None
        chunks: List[bytes] = []
        read = 0
        block = 256 * 1024
        tty = sys.stderr.isatty()
        while True:
            part = resp.read(block)
            if not part:
                break
            chunks.append(part)
            read += len(part)
            if total_n:
                pct = min(100, int(read * 100 / total_n))
                msg = f"Downloading… {read // (1024 * 1024)}.{read % (1024 * 1024) // 102400} MB ({pct}%)"
            else:
                msg = f"Downloading… {read // (1024 * 1024)} MB"
            if tty:
                print(f"\r  {msg}", end="", file=sys.stderr, flush=True)
            elif read % (5 * 1024 * 1024) < block:
                _note(msg)
        if tty and read:
            print(file=sys.stderr)
        return b"".join(chunks)


def _semver_tuple(version: str) -> Tuple[int, int, int]:
    base = version.strip().split("-", 1)[0].split("+", 1)[0]
    parts = base.split(".")
    nums: List[int] = []
    for p in parts[:3]:
        m = re.match(r"^(\d+)", p.strip())
        nums.append(int(m.group(1)) if m else 0)
    while len(nums) < 3:
        nums.append(0)
    return nums[0], nums[1], nums[2]


_NIGHTLY_VERSION_RE = re.compile(
    r"^nightly-(\d{8})-([0-9a-f]{7})$",
    re.IGNORECASE,
)


def _version_sort_key(version: str) -> Tuple[Any, ...]:
    stripped = version.strip()
    nightly_m = _NIGHTLY_VERSION_RE.match(stripped)
    if nightly_m:
        return (0, int(nightly_m.group(1)), nightly_m.group(2).lower())
    core = _semver_tuple(stripped)
    return (1, core[0], core[1], core[2], stripped)


def _registry_version_gt(left: str, right: str) -> bool:
    return _version_sort_key(left) > _version_sort_key(right)


def _latest_registry_version(versions_doc: Dict[str, Any]) -> Optional[str]:
    entries = versions_doc.get("versions")
    if not isinstance(entries, list):
        return None
    best: Optional[str] = None
    for item in entries:
        if not isinstance(item, dict):
            continue
        ver = item.get("version")
        if not ver or not isinstance(ver, str):
            continue
        if best is None or _registry_version_gt(ver, best):
            best = ver
    return best


def _fetch_download_meta(base: str, version: str) -> Dict[str, Any]:
    root = base.strip().rstrip("/")
    meta_url = f"{root}/{version}/download/universal/universal"
    meta_raw = _http_get(meta_url)
    return json.loads(meta_raw.decode("utf-8"))


def _resolve_subprocess_stdio(
    sudoish: bool,
    *,
    stream_output: bool,
    log_file: Optional[TextIO],
) -> Tuple[Any, Any, Any, Optional[TextIO]]:
    """
    Return ``(stdin, stdout, stderr, tty_in_to_close)`` for ``Popen``.

    When *stream_output* is False and *log_file* is set, stdout/stderr go to the log.
    """
    tty_in: Optional[TextIO] = None
    if sudoish:
        if sys.stdin.isatty():
            stdin = sys.stdin
        else:
            tty_in = _open_controlling_tty_stdin()
            stdin = tty_in if tty_in is not None else subprocess.DEVNULL
    else:
        stdin = sys.stdin if sys.stdin.isatty() else subprocess.DEVNULL

    if stream_output:
        stdout = stderr = None
    elif log_file is not None:
        stdout = stderr = log_file
    else:
        stdout = stderr = subprocess.DEVNULL

    return stdin, stdout, stderr, tty_in


def _run_step_subprocess(
    argv: List[str],
    *,
    cwd: str,
    env: Dict[str, str],
    stream_output: bool = False,
    log_file: Optional[TextIO] = None,
    heartbeat_label: Optional[str] = None,
    capture_on_failure: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run *argv* with heartbeat lines; raise :class:`_Interrupted` on Ctrl-C."""
    sudoish = bool(argv) and argv[0] == "sudo"
    stdin, stdout, stderr, tty_in = _resolve_subprocess_stdio(
        sudoish,
        stream_output=stream_output,
        log_file=log_file,
    )
    proc: subprocess.Popen[Any]
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            env=env,
        )
    except FileNotFoundError as exc:
        _die(f"command not found: {exc}")
        raise AssertionError("unreachable")
    start = time.monotonic()
    last_hb = start
    hb_interval = 15.0 if sys.stderr.isatty() else 30.0
    try:
        while True:
            try:
                rc = proc.wait(timeout=1.0)
                break
            except subprocess.TimeoutExpired:
                now = time.monotonic()
                if heartbeat_label and now - last_hb >= hb_interval:
                    elapsed = _format_elapsed(now - start)
                    msg = f"  still {heartbeat_label}… {elapsed}"
                    if sys.stderr.isatty():
                        print(f"\r{msg}", end="", file=sys.stderr, flush=True)
                    else:
                        _note(msg.strip())
                    last_hb = now
        if sys.stderr.isatty() and heartbeat_label:
            print(file=sys.stderr)
    except KeyboardInterrupt:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        raise _Interrupted(_INSTALL_PHASE or "unknown")
    finally:
        if tty_in is not None:
            tty_in.close()

    if _subprocess_interrupted(rc):
        raise _Interrupted(_INSTALL_PHASE or "unknown")

    tail = ""
    if capture_on_failure and rc != 0:
        tail = "(see command output above)"
    cp = subprocess.CompletedProcess(argv, rc, None, tail)
    return cp


def _find_extracted_bundle_root(extract_dir: Path) -> Path:
    if (extract_dir / "Makefile").is_file() and (extract_dir / "ssm.py").is_file():
        return extract_dir
    subdirs = [p for p in extract_dir.iterdir() if p.is_dir()]
    if len(subdirs) == 1:
        sole = subdirs[0]
        if (sole / "Makefile").is_file() and (sole / "ssm.py").is_file():
            return sole
    _die(
        "Bundle is missing Makefile or ssm.py (expected published universal zip layout).",
    )
    raise AssertionError("unreachable")


def _run_with_optional_tty(
    argv: List[str],
    *,
    cwd: str,
    layout: str = "default",
    completion_shells: Sequence[str] = ("bash",),
    quiet: bool = True,
    stream_output: bool = False,
    log_file: Optional[TextIO] = None,
    heartbeat_label: Optional[str] = None,
) -> None:
    """Run subprocess; inherit TTY for sudo password prompts when possible."""
    env = _bundle_subprocess_env(
        layout=layout,
        completion_shells=completion_shells,
        quiet=quiet,
    )
    try:
        proc = _run_step_subprocess(
            argv,
            cwd=cwd,
            env=env,
            stream_output=stream_output or _install_verbose(),
            log_file=log_file,
            heartbeat_label=heartbeat_label,
            capture_on_failure=True,
        )
    except _Interrupted:
        raise
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip()[:1200]
        _die(f"{' '.join(argv)} failed (exit {proc.returncode}). {tail}")


def _run_make_install_internal_returncode(
    bundle_root: Path,
    layout: str = "default",
    completion_shells: Sequence[str] = ("bash",),
) -> int:
    """Return exit code from ``make install-internal`` (0 = success)."""
    cwd = str(bundle_root.resolve())
    env = _bundle_subprocess_env(
        layout=layout,
        completion_shells=completion_shells,
        quiet=True,
    )
    shells_joined = " ".join(completion_shells)
    make_argv = [
        "make",
        "-j1",
        f"{_INSTALL_QUIET_ENV}=1",
        f"{_INSTALL_COMPLETION_SHELLS_ENV}={shells_joined}",
    ]
    if layout == "alternative":
        make_argv.append(f"{_INSTALL_LAYOUT_ENV}=alternative")
    make_argv.append("install-internal")
    if os.geteuid() != 0:
        argv = ["sudo", *make_argv]
    else:
        argv = list(make_argv)
    try:
        proc = _run_step_subprocess(
            argv,
            cwd=cwd,
            env=env,
            stream_output=_install_verbose(),
            heartbeat_label="installing under /usr/local",
        )
    except _Interrupted:
        raise
    except FileNotFoundError:
        return 127
    return int(proc.returncode)


def _install_alternative_completions_from_bundle(
    bundle_root: Path,
    run_privileged: Callable[[List[str]], None],
    completion_shells: Sequence[str],
) -> None:
    """Install ec2/ecs/ssm.py completion files when present in the bundle.

    Runs the same removal step as Makefile ``completions-cleanup-internal`` first so
    leftover ``ssm`` / ``ssmc`` completion scripts do not persist after an
    alternative layout install (notably when this fallback runs instead of
    ``make install-internal``).

    Args:
        bundle_root: Extracted universal zip root (may contain ``completions/``).
        run_privileged: Runs ``argv`` with sudo when needed (same as fallback helper).
        completion_shells: Shell names to install (``bash`` and/or ``zsh``).

    Returns:
        None
    """
    _cleanup_registry_completion_files(run_privileged)
    comp = bundle_root / "completions"
    if not comp.is_dir():
        return
    bash_d = "/usr/local/share/bash-completion/completions"
    zsh_d = "/usr/local/share/zsh/site-functions"
    shell_set = frozenset(completion_shells)
    bash_pairs = [
        ("ec2.bash", f"{bash_d}/ec2"),
        ("ecs.bash", f"{bash_d}/ecs"),
        ("ssm.py.bash", f"{bash_d}/ssm.py"),
    ]
    zsh_pairs = [
        ("ec2.zsh", f"{zsh_d}/_ec2"),
        ("ecs.zsh", f"{zsh_d}/_ecs"),
        ("ssm.py.zsh", f"{zsh_d}/_ssm.py"),
    ]
    if "bash" in shell_set:
        for src_name, dest in bash_pairs:
            src = comp / src_name
            if not src.is_file():
                continue
            dest_parent = str(Path(dest).parent)
            run_privileged(["install", "-d", dest_parent])
            run_privileged(["install", "-m", "0644", str(src), dest])
    if "zsh" in shell_set:
        for src_name, dest in zsh_pairs:
            src = comp / src_name
            if not src.is_file():
                continue
            dest_parent = str(Path(dest).parent)
            run_privileged(["install", "-d", dest_parent])
            run_privileged(["install", "-m", "0644", str(src), dest])


def _install_default_completions_from_bundle(
    bundle_root: Path,
    run_privileged: Callable[[List[str]], None],
    completion_shells: Sequence[str],
) -> None:
    """Install ssm/ssmc completion files when present in the bundle.

    Removes prior completion scripts under ``/usr/local/share/...`` first (same set
    as Makefile ``completions-cleanup-internal``).

    Args:
        bundle_root: Extracted universal zip root (may contain ``completions/``).
        run_privileged: Runs ``argv`` with sudo when needed (same as fallback helper).
        completion_shells: Shell names to install (``bash`` and/or ``zsh``).

    Returns:
        None
    """
    _cleanup_registry_completion_files(run_privileged)
    comp = bundle_root / "completions"
    if not comp.is_dir():
        return
    bash_d = "/usr/local/share/bash-completion/completions"
    zsh_d = "/usr/local/share/zsh/site-functions"
    shell_set = frozenset(completion_shells)
    bash_pairs = [
        ("ssm.bash", f"{bash_d}/ssm"),
        ("ssmc.bash", f"{bash_d}/ssmc"),
    ]
    zsh_pairs = [
        ("ssm.zsh", f"{zsh_d}/_ssm"),
        ("ssmc.zsh", f"{zsh_d}/_ssmc"),
    ]
    if "bash" in shell_set:
        for src_name, dest in bash_pairs:
            src = comp / src_name
            if not src.is_file():
                continue
            dest_parent = str(Path(dest).parent)
            run_privileged(["install", "-d", dest_parent])
            run_privileged(["install", "-m", "0644", str(src), dest])
    if "zsh" in shell_set:
        for src_name, dest in zsh_pairs:
            src = comp / src_name
            if not src.is_file():
                continue
            dest_parent = str(Path(dest).parent)
            run_privileged(["install", "-d", dest_parent])
            run_privileged(["install", "-m", "0644", str(src), dest])


def _copy_dist_to_usr_local_fallback(
    bundle_root: Path,
    *,
    layout: str = "default",
    completion_shells: Sequence[str] = ("bash",),
) -> None:
    """Mirror ``Makefile`` ``install-internal`` when that target is missing or fails."""
    dist_ssm = bundle_root / "dist" / "ssm"
    if not dist_ssm.is_dir():
        _die(f"Missing PyInstaller output (expected directory): {dist_ssm}")

    def _run_privileged(argv: List[str]) -> None:
        env = _bundle_subprocess_env(layout=layout, completion_shells=completion_shells)
        if os.geteuid() == 0:
            subprocess.run(
                argv,
                check=True,
                timeout=600,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
            )
        else:
            _run_with_optional_tty(
                ["sudo"] + argv,
                cwd=os.getcwd(),
                layout=layout,
                completion_shells=completion_shells,
            )

    _run_privileged(["cp", "-rf", str(dist_ssm), "/usr/local/"])

    if layout == "alternative":
        ec2_wrap = b"#!/usr/bin/env bash\nexec /usr/local/ssm/ssm ec2 \"$@\"\n"
        ecs_wrap = b"#!/usr/bin/env bash\nexec /usr/local/ssm/ssm ecs \"$@\"\n"
        with tempfile.NamedTemporaryFile(delete=False, suffix="-ec2.sh") as f:
            f.write(ec2_wrap)
            p_ec2 = f.name
        with tempfile.NamedTemporaryFile(delete=False, suffix="-ecs.sh") as f:
            f.write(ecs_wrap)
            p_ecs = f.name
        try:
            os.chmod(p_ec2, 0o755)
            os.chmod(p_ecs, 0o755)
            _run_privileged(["rm", "-f", "/usr/local/bin/ssmc", "/usr/local/bin/ec2", "/usr/local/bin/ecs"])
            _run_privileged(["install", "-m", "0755", p_ec2, "/usr/local/bin/ec2"])
            _run_privileged(["install", "-m", "0755", p_ecs, "/usr/local/bin/ecs"])
            _run_privileged(["rm", "-f", "/usr/local/bin/ssm"])
            _run_privileged(["ln", "-sf", "/usr/local/ssm/ssm", "/usr/local/bin/ssm"])
            _install_alternative_completions_from_bundle(
                bundle_root,
                _run_privileged,
                completion_shells,
            )
        finally:
            for p in (p_ec2, p_ecs):
                try:
                    os.unlink(p)
                except OSError:
                    pass
        return

    _run_privileged(["rm", "-f", "/usr/local/bin/ec2", "/usr/local/bin/ecs"])
    ssm_wrap = b"#!/usr/bin/env bash\nexec /usr/local/ssm/ssm ec2 \"$@\"\n"
    ssmc_wrap = b"#!/usr/bin/env bash\nexec /usr/local/ssm/ssm ecs \"$@\"\n"
    with tempfile.NamedTemporaryFile(delete=False, suffix="-ssm.sh") as f:
        f.write(ssm_wrap)
        p_ssm = f.name
    with tempfile.NamedTemporaryFile(delete=False, suffix="-ssmc.sh") as f:
        f.write(ssmc_wrap)
        p_ssmc = f.name
    try:
        os.chmod(p_ssm, 0o755)
        os.chmod(p_ssmc, 0o755)
        _run_privileged(["install", "-m", "0755", p_ssm, "/usr/local/bin/ssm"])
        _run_privileged(["install", "-m", "0755", p_ssmc, "/usr/local/bin/ssmc"])
    finally:
        for p in (p_ssm, p_ssmc):
            try:
                os.unlink(p)
            except OSError:
                pass
    _install_default_completions_from_bundle(
        bundle_root,
        _run_privileged,
        completion_shells,
    )


def _sudo_local_cleanup() -> None:
    """Remove prior CLI install under ``/usr/local`` (requires root or sudo)."""
    script = (
        "set -e; "
        "rm -f /usr/local/bin/ssm /usr/local/bin/ssmc /usr/local/bin/ec2 /usr/local/bin/ecs; "
        "rm -rf /usr/local/ssm /usr/local/ssmc"
    )
    if os.geteuid() == 0:
        subprocess.run(
            ["/bin/sh", "-c", script],
            check=True,
            timeout=120,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=_bundle_subprocess_env(layout="default"),
        )
    else:
        _run_with_optional_tty(["sudo", "/bin/sh", "-c", script], cwd=os.getcwd(), layout="default")


def _print_build_log_tail(log_path: Path, *, max_lines: int = 25) -> None:
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    if not lines:
        return
    _note(f"Build log (last {max_lines} lines): {log_path}")
    for line in lines[-max_lines:]:
        _note(line)


def _run_build_and_install_internal(
    bundle_root: Path,
    *,
    layout: str = "default",
    completion_shells: Sequence[str] = ("bash",),
) -> None:
    global _INSTALL_PHASE
    cwd = str(bundle_root.resolve())
    log_path: Optional[Path] = None
    log_f: Optional[TextIO] = None
    verbose = _install_verbose()
    if not verbose:
        fd, log_name = tempfile.mkstemp(prefix="ssm-wrapper-install-", suffix=".log")
        os.close(fd)
        log_path = Path(log_name)
        log_f = open(log_path, "w", encoding="utf-8")

    _INSTALL_PHASE = "build"
    _step(
        4,
        "Building ssm-wrapper (venv, dependencies, PyInstaller) — usually 2–5 minutes",
    )
    build_ok = False
    try:
        _run_with_optional_tty(
            ["make", "-j1", "build"],
            cwd=cwd,
            layout=layout,
            completion_shells=completion_shells,
            quiet=False,
            stream_output=verbose,
            log_file=log_f,
            heartbeat_label="building",
        )
        build_ok = True
    except _Interrupted:
        if log_path is not None and log_path.is_file():
            _print_build_log_tail(log_path)
        raise
    except SystemExit:
        if log_path is not None and log_path.is_file():
            _print_build_log_tail(log_path)
        raise
    finally:
        if log_f is not None:
            log_f.close()
        if build_ok and log_path is not None and log_path.is_file():
            try:
                log_path.unlink()
            except OSError:
                pass

    _step(5, "Preparing privileged install (sudo)")
    if os.geteuid() != 0:
        _note("Administrator password needed to write /usr/local (sudo)")
        _run_with_optional_tty(
            ["sudo", "-v"],
            cwd=os.getcwd(),
            layout=layout,
            completion_shells=completion_shells,
        )

    _INSTALL_PHASE = "install"
    _step(6, "Installing under /usr/local (replacing prior ssm-wrapper files)")
    _sudo_local_cleanup()
    try:
        rc = _run_make_install_internal_returncode(
            bundle_root,
            layout,
            completion_shells,
        )
    except _Interrupted:
        raise
    if rc != 0:
        _copy_dist_to_usr_local_fallback(
            bundle_root,
            layout=layout,
            completion_shells=completion_shells,
        )


def _install_from_zip(
    zip_path: Path,
    expected_sha256: str,
    *,
    release_label: Optional[str] = None,
    install_layout: str = "default",
    completion_shells: Sequence[str] = ("bash",),
    registry_channel: Optional[str] = None,
    pinned_version: Optional[str] = None,
    clear_pinned: bool = False,
) -> None:
    tmp_root = Path(tempfile.mkdtemp(prefix="ssm-wrapper-extract-"))
    try:
        _step(1, "Reading and verifying bundle checksum")
        data = zip_path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != expected_sha256.strip().lower():
            _die("Bundle checksum does not match expected value (possible tampering).")
        _step(2, "Extracting bundle")
        zcopy = tmp_root / "bundle.zip"
        zcopy.write_bytes(data)
        with zipfile.ZipFile(zcopy) as zf:
            zf.extractall(tmp_root)
        zcopy.unlink(missing_ok=True)
        bundle_root = _find_extracted_bundle_root(tmp_root)
        _run_build_and_install_internal(
            bundle_root,
            layout=install_layout,
            completion_shells=completion_shells,
        )
        label = (release_label or "").strip()
        if label:
            print(
                f"Installed ssm-wrapper {label} to /usr/local/ssm (ssm, ssmc). Done.",
                file=sys.stderr,
                flush=True,
            )
            _maybe_prompt_release_notes(bundle_root, label)
        else:
            print(
                "Installed ssm-wrapper to /usr/local/ssm (ssm, ssmc). Done.",
                file=sys.stderr,
                flush=True,
            )
        _persist_cli_update_prefs(
            registry_channel=registry_channel,
            install_layout=install_layout,
            completion_shells=completion_shells,
            clear_channel=(registry_channel is None),
            pinned_version=pinned_version,
            clear_pinned=clear_pinned,
        )
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def _registry_versions_sorted(doc: Dict[str, Any]) -> List[str]:
    entries = doc.get("versions")
    if not isinstance(entries, list):
        return []
    ids: List[str] = []
    for item in entries:
        if isinstance(item, dict):
            ver = item.get("version")
            if ver and isinstance(ver, str) and ver.strip():
                ids.append(ver.strip())
    return sorted(ids, key=_version_sort_key, reverse=True)


def _version_in_registry_doc(doc: Dict[str, Any], version: str) -> bool:
    want = version.strip()
    return want in _registry_versions_sorted(doc)


def _print_installer_version_list(channel: str, doc: Dict[str, Any], latest: str) -> None:
    print(f"Available versions ({channel}):", file=sys.stderr, flush=True)
    for ver in _registry_versions_sorted(doc):
        tags: List[str] = []
        if ver == latest:
            tags.append("latest")
        suffix = f" ({', '.join(tags)})" if tags else ""
        print(f"  {ver}{suffix}", file=sys.stderr, flush=True)


def _full_registry_install(
    *,
    channel_override: Optional[str] = None,
    install_layout: str = "default",
    completion_shells: Sequence[str] = ("bash",),
    install_version: Optional[str] = None,
    list_versions_only: bool = False,
) -> None:
    if not list_versions_only and not _can_prompt_privileged_commands():
        _die(
            "A terminal is required for sudo password prompts. "
            "Use an interactive shell (or save the script and run "
            "`python3 install_from_registry.py`), run `sudo -v` first, "
            "or configure passwordless sudo.",
        )
    channel = _resolve_install_channel(channel_override)
    base = _registry_base(channel)
    _step(1, f"Fetching version list from registry ({channel})")
    try:
        doc = json.loads(_http_get(f"{base}/versions").decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        _die(f"Could not read registry versions: {exc}")
    latest = _latest_registry_version(doc)
    if not latest:
        _die("Registry returned no versions.")
    if list_versions_only:
        _print_installer_version_list(channel, doc, latest)
        return

    target = (install_version or "").strip() or latest
    explicit = bool((install_version or "").strip())
    if explicit and not _version_in_registry_doc(doc, target):
        _die(
            f"Version {target!r} not found on channel {channel!r}. "
            f"Run: python3 install_from_registry.py --list-versions "
            f"{f'--channel {channel} ' if channel_override else ''}".strip(),
        )

    _step(2, f"Downloading bundle {target}")
    try:
        dl = _fetch_download_meta(base, target)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        _die(f"Could not read download metadata: {exc}")
    url = dl.get("download_url")
    expected = dl.get("shasum")
    if not url or not expected:
        _die("Registry metadata missing download_url or shasum.")
    expected = str(expected).strip().lower()
    try:
        data = _http_get_with_progress(str(url), timeout=180)
    except OSError as exc:
        _die(f"Could not download bundle: {exc}")
    digest = hashlib.sha256(data).hexdigest()
    if digest != expected:
        _die("Downloaded bundle checksum does not match registry (possible tampering).")
    fd, zpath = tempfile.mkstemp(prefix="ssm-bundle-", suffix=".zip")
    os.close(fd)
    zp = Path(zpath)
    pin: Optional[str] = None
    clear_pin = not explicit or target == latest
    if explicit and _registry_version_gt(latest, target):
        pin = target
    try:
        zp.write_bytes(data)
        _install_from_zip(
            zp,
            expected,
            release_label=target,
            install_layout=install_layout,
            completion_shells=completion_shells,
            registry_channel=channel,
            pinned_version=pin,
            clear_pinned=clear_pin,
        )
    finally:
        try:
            zp.unlink(missing_ok=True)
        except OSError:
            pass


def _write_cli_updates_from_args(args: argparse.Namespace) -> None:
    """Persist ``cli_updates`` from Makefile / ``--write-cli-updates`` (no install).

    Local / development installs (``make install-dev``) clear ``channel`` to JSON
    ``null`` unless ``--channel`` is passed explicitly.
    """
    layout = "alternative" if args.alternative else "default"
    if args.shell:
        shells = _normalize_completion_shells(args.shell)
    else:
        shells = ("bash",)
    _persist_cli_update_prefs(
        registry_channel=args.channel,
        install_layout=layout,
        completion_shells=shells,
        clear_channel=(args.channel is None),
    )


def main() -> None:
    """Parse CLI flags and run a full registry install or a staged zip install."""
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--zip", type=Path, help="Pre-downloaded universal zip path")
    p.add_argument(
        "--sha256",
        help="Lowercase SHA256 hex of the zip (required with --zip)",
    )
    p.add_argument(
        "--release-version",
        help=(
            "Registry version id for this bundle (enables optional release-notes "
            "prompt after install)"
        ),
    )
    p.add_argument(
        "--channel",
        choices=("stable", "nightly"),
        help=(
            "Registry channel for this install (default: cli_updates.channel in "
            "config.json, else stable). After success, written back to "
            "cli_updates.channel (JSON null for zip installs without this flag), "
            "cli_updates.install_layout, and cli_updates.completion_shells. "
            "Useful when piping this script from curl."
        ),
    )
    p.add_argument(
        "--alternative",
        action="store_true",
        help=(
            "Install ec2/ecs wrappers and completions (ec2, ecs, ssm.py); symlink "
            "/usr/local/bin/ssm to /usr/local/ssm/ssm. Overrides "
            "cli_updates.install_layout for this run."
        ),
    )
    p.add_argument(
        "--shell",
        action="append",
        choices=("bash", "zsh"),
        metavar="SHELL",
        help=(
            "Install completions for this shell (repeatable): bash or zsh. "
            "Overrides cli_updates.completion_shells for this run."
        ),
    )
    p.add_argument(
        "--write-cli-updates",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Stream build output to the terminal (or set SSM_INSTALL_VERBOSE=1).",
    )
    p.add_argument(
        "--list-versions",
        action="store_true",
        help="Print registry versions for the channel and exit (no install).",
    )
    p.add_argument(
        "--release",
        dest="install_version",
        metavar="VERSION",
        help="Install this registry version instead of latest (full registry mode).",
    )
    args = p.parse_args()

    def _run() -> None:
        if args.write_cli_updates:
            _write_cli_updates_from_args(args)
            return
        config_layout = _read_install_layout_from_config()
        layout = "alternative" if args.alternative else config_layout
        if args.shell:
            shells = _normalize_completion_shells(args.shell)
        else:
            shells = _read_completion_shells_from_config()
        if bool(args.zip) ^ bool(args.sha256):
            _die("Both --zip and --sha256 are required together.")
        if args.zip is not None:
            if not args.zip.is_file():
                _die(f"Zip not found: {args.zip}")
            assert args.sha256 is not None
            rv = (args.release_version or "").strip() or None
            _install_from_zip(
                args.zip,
                args.sha256,
                release_label=rv,
                install_layout=layout,
                completion_shells=shells,
                registry_channel=args.channel,
            )
            return
        _full_registry_install(
            channel_override=args.channel,
            install_layout=layout,
            completion_shells=shells,
            install_version=args.install_version,
            list_versions_only=args.list_versions,
        )

    try:
        _run()
    except (KeyboardInterrupt, _Interrupted) as exc:
        phase = getattr(exc, "phase", "unknown")
        if phase == "install":
            state = "Install may be incomplete; re-run the installer."
        else:
            state = "Your previous install was not changed."
        print(f"\nInstallation cancelled. {state}", file=sys.stderr, flush=True)
        raise SystemExit(130) from exc
