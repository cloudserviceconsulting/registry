#!/usr/bin/env python3
"""Install or update ssm-wrapper from the public CLI registry (stdlib only for ``--zip`` mode).

``make install`` runs this script with no arguments: it resolves the update
channel from user config, downloads the latest universal zip, verifies SHA256,
builds with ``make -j1 build``, then ``sudo make -j1 install-internal`` when the
bundle Makefile defines that target; otherwise copies ``dist/ssm`` and bin
wrappers the same way ``install-internal`` does (for older registry zips).

``ssm -up`` invokes ``--zip PATH --sha256 HEX`` after the parent process has
already downloaded and verified the bundle. Optional ``--release-version`` enables
the post-install release-notes prompt (reads ``CHANGELOG.md`` from the extracted
bundle; same opt-out as the CLI used: ``SSM_SKIP_CHANGELOG_PROMPT=1``).

Optional ``--channel stable|nightly`` overrides ``config.json`` for this run
(useful for ``curl … | python3 -`` bootstrap installs). In that mode ``sys.stdin``
is the script pipe, not the terminal; interactive ``sudo`` still uses the
controlling TTY via ``/dev/tty`` when available.

This script is stdlib-only (no ``libs`` imports): release-notes parsing mirrors
``libs/core/changelog_preview.py`` so it runs under plain ``python3``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, TextIO, Tuple

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

    prompt = "Show release notes for this version in the terminal? (y/N) "
    try:
        if read_fn is None:
            ans = input(prompt).strip().lower()
        else:
            print(prompt, end="", flush=True)
            ans = read_fn.readline().strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return
    finally:
        if tty_in is not None:
            tty_in.close()
    if ans != "y":
        return
    _print_release_notes(f"Changelog — [{rel}]", lines)


def _bundle_subprocess_env() -> Dict[str, str]:
    return {**os.environ, _INSTALL_QUIET_ENV: "1"}


def _die(_msg: str, code: int = 1) -> None:
    raise SystemExit(code)


def _caller_home() -> Path:
    """Home directory of the invoking user (handles ``sudo``)."""
    su = os.environ.get("SUDO_UID", "").strip()
    if su.isdigit():
        import pwd

        return Path(pwd.getpwuid(int(su)).pw_dir).resolve()
    return Path.home().resolve()


def _xdg_config_home() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg:
        return Path(xdg).expanduser().resolve()
    return (_caller_home() / ".config").resolve()


def _ssm_config_dir() -> Path:
    root = os.environ.get("SSM_HOME", "").strip()
    if root:
        return Path(root).expanduser().resolve()
    return _xdg_config_home() / "ssm"


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
        headers={"User-Agent": "ssm-wrapper-install-from-registry"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


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


def _run_with_optional_tty(argv: List[str], *, cwd: str) -> None:
    """Run subprocess; inherit TTY for sudo password prompts when possible."""
    env = _bundle_subprocess_env()
    sudoish = bool(argv) and argv[0] == "sudo"
    try:
        if sys.stdin.isatty():
            if sudoish:
                proc = subprocess.run(
                    argv,
                    cwd=cwd,
                    stdin=sys.stdin,
                    stdout=None,
                    stderr=None,
                    timeout=3600,
                    check=False,
                    env=env,
                )
            else:
                proc = subprocess.run(
                    argv,
                    cwd=cwd,
                    stdin=sys.stdin,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=3600,
                    check=False,
                    env=env,
                )
        elif sudoish:
            tty_in = _open_controlling_tty_stdin()
            if tty_in is None:
                proc = subprocess.run(
                    argv,
                    cwd=cwd,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=3600,
                    check=False,
                    env=env,
                )
            else:
                try:
                    proc = subprocess.run(
                        argv,
                        cwd=cwd,
                        stdin=tty_in,
                        stdout=None,
                        stderr=None,
                        timeout=3600,
                        check=False,
                        env=env,
                    )
                finally:
                    tty_in.close()
        else:
            proc = subprocess.run(
                argv,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3600,
                check=False,
                env=env,
            )
    except FileNotFoundError as exc:
        _die(f"command not found: {exc}")
    except subprocess.TimeoutExpired:
        _die(f"command timed out: {argv!r}")
    if proc.returncode != 0:
        tail = ""
        if hasattr(proc, "stderr") and proc.stderr:
            tail = str(proc.stderr).strip()[:1200]
        _die(f"{' '.join(argv)} failed (exit {proc.returncode}). {tail}")


def _run_make_install_internal_returncode(bundle_root: Path) -> int:
    """Return exit code from ``make install-internal`` (0 = success)."""
    cwd = str(bundle_root.resolve())
    env = _bundle_subprocess_env()
    if os.geteuid() != 0:
        # ``sudo`` often resets the environment; ``env VAR=1`` keeps the Makefile quiet flag.
        argv = [
            "sudo",
            "env",
            f"{_INSTALL_QUIET_ENV}=1",
            "make",
            "-j1",
            "install-internal",
        ]
    else:
        argv = ["make", "-j1", "install-internal"]
    try:
        if sys.stdin.isatty():
            if os.geteuid() != 0:
                proc = subprocess.run(
                    argv,
                    cwd=cwd,
                    stdin=sys.stdin,
                    stdout=None,
                    stderr=None,
                    timeout=3600,
                    env=env,
                )
            else:
                proc = subprocess.run(
                    argv,
                    cwd=cwd,
                    stdin=sys.stdin,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=3600,
                    env=env,
                )
        elif os.geteuid() != 0:
            tty_in = _open_controlling_tty_stdin()
            if tty_in is None:
                proc = subprocess.run(
                    argv,
                    cwd=cwd,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=3600,
                    env=env,
                )
            else:
                try:
                    proc = subprocess.run(
                        argv,
                        cwd=cwd,
                        stdin=tty_in,
                        stdout=None,
                        stderr=None,
                        timeout=3600,
                        env=env,
                    )
                finally:
                    tty_in.close()
        else:
            proc = subprocess.run(
                argv,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3600,
                env=env,
            )
    except FileNotFoundError:
        return 127
    except subprocess.TimeoutExpired:
        return 124
    return int(proc.returncode)


def _copy_dist_to_usr_local_fallback(bundle_root: Path) -> None:
    """Mirror ``Makefile`` ``install-internal`` when that target is missing or fails."""
    dist_ssm = bundle_root / "dist" / "ssm"
    if not dist_ssm.is_dir():
        _die(f"Missing PyInstaller output (expected directory): {dist_ssm}")

    def _run_privileged(argv: List[str]) -> None:
        if os.geteuid() == 0:
            subprocess.run(
                argv,
                check=True,
                timeout=600,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=_bundle_subprocess_env(),
            )
        else:
            _run_with_optional_tty(["sudo"] + argv, cwd=os.getcwd())

    _run_privileged(["cp", "-rf", str(dist_ssm), "/usr/local/"])

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


def _sudo_local_cleanup() -> None:
    """Remove prior CLI install under ``/usr/local`` (requires root or sudo)."""
    script = (
        "set -e; "
        "rm -f /usr/local/bin/ssm /usr/local/bin/ssmc; "
        "rm -rf /usr/local/ssm /usr/local/ssmc"
    )
    if os.geteuid() == 0:
        subprocess.run(
            ["/bin/sh", "-c", script],
            check=True,
            timeout=120,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=_bundle_subprocess_env(),
        )
    else:
        _run_with_optional_tty(["sudo", "/bin/sh", "-c", script], cwd=os.getcwd())


def _run_build_and_install_internal(bundle_root: Path) -> None:
    cwd = str(bundle_root.resolve())
    _run_with_optional_tty(
        ["make", "-j1", "build"],
        cwd=cwd,
    )
    rc = _run_make_install_internal_returncode(bundle_root)
    if rc != 0:
        _copy_dist_to_usr_local_fallback(bundle_root)


def _install_from_zip(
    zip_path: Path,
    expected_sha256: str,
    *,
    release_label: Optional[str] = None,
) -> None:
    tmp_root = Path(tempfile.mkdtemp(prefix="ssm-wrapper-extract-"))
    try:
        data = zip_path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != expected_sha256.strip().lower():
            _die("Bundle checksum does not match expected value (possible tampering).")
        zcopy = tmp_root / "bundle.zip"
        zcopy.write_bytes(data)
        with zipfile.ZipFile(zcopy) as zf:
            zf.extractall(tmp_root)
        zcopy.unlink(missing_ok=True)
        bundle_root = _find_extracted_bundle_root(tmp_root)
        _sudo_local_cleanup()
        _run_build_and_install_internal(bundle_root)
        label = (release_label or "").strip()
        if label:
            _maybe_prompt_release_notes(bundle_root, label)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def _full_registry_install(*, channel_override: Optional[str] = None) -> None:
    if not _can_prompt_privileged_commands():
        _die(
            "A terminal is required for sudo password prompts. "
            "Use an interactive shell (or save the script and run "
            "`python3 install_from_registry.py`), run `sudo -v` first, "
            "or configure passwordless sudo.",
        )
    channel = _resolve_install_channel(channel_override)
    base = _registry_base(channel)
    try:
        doc = json.loads(_http_get(f"{base}/versions").decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        _die(f"Could not read registry versions: {exc}")
    latest = _latest_registry_version(doc)
    if not latest:
        _die("Registry returned no versions.")
    try:
        dl = _fetch_download_meta(base, latest)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        _die(f"Could not read download metadata: {exc}")
    url = dl.get("download_url")
    expected = dl.get("shasum")
    if not url or not expected:
        _die("Registry metadata missing download_url or shasum.")
    expected = str(expected).strip().lower()
    try:
        data = _http_get(str(url), timeout=180)
    except OSError as exc:
        _die(f"Could not download bundle: {exc}")
    digest = hashlib.sha256(data).hexdigest()
    if digest != expected:
        _die("Downloaded bundle checksum does not match registry (possible tampering).")
    fd, zpath = tempfile.mkstemp(prefix="ssm-bundle-", suffix=".zip")
    os.close(fd)
    zp = Path(zpath)
    try:
        zp.write_bytes(data)
        _install_from_zip(zp, expected, release_label=latest)
    finally:
        try:
            zp.unlink(missing_ok=True)
        except OSError:
            pass


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
            "config.json, else stable). Useful when piping this script from curl."
        ),
    )
    args = p.parse_args()
    if bool(args.zip) ^ bool(args.sha256):
        _die("Both --zip and --sha256 are required together.")
    if args.zip is not None:
        if not args.zip.is_file():
            _die(f"Zip not found: {args.zip}")
        assert args.sha256 is not None
        rv = (args.release_version or "").strip() or None
        _install_from_zip(args.zip, args.sha256, release_label=rv)
        return
    _full_registry_install(channel_override=args.channel)


if __name__ == "__main__":
    main()
