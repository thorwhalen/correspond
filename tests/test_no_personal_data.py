"""The no-personal-data guard: nothing in this public repository identifies a real person or holds a real secret.

Channel data (messages, addresses, topics, chat ids, tokens) belongs in the operator's
environment and data root, never here. This scans every text file in the repository, not
just what git tracks, so a new file is covered before it is committed, for the mechanical
signs of a leak:

- an email address outside the reserved example domains;
- an absolute local home path;
- a ``github.com/<owner>`` URL, ``owner/repo`` string or ``@handle`` whose owner or handle
  is not a placeholder or one of this project's own orgs;
- a token (GitHub, Telegram bot, Slack, PyPI, AWS, an ``sk-`` API key, an ntfy ``tk_``
  token, a literal bearer token), a private key, a phone number, a Telegram supergroup id,
  credentials in a URL;
- an IPv4 address outside loopback and the documentation ranges.

It cannot catch a real name, a real ntfy topic, or a short chat or user id written as prose; that part is on whoever
writes the text. Strings that must look like leaks inside this file are built by
concatenation, so the guard does not trip on itself.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Project metadata names the package's own author; out of scope here.
_EXCLUDED_FILES = {"LICENSE"}
_SKIP_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "dist",
    "build",
    ".venv",
    "venv",
    ".tox",
    "node_modules",
    "handoffs",
    "scratch",
}
_TEXT_SUFFIXES = {
    ".py",
    ".md",
    ".toml",
    ".json",
    ".jsonl",
    ".txt",
    ".yml",
    ".yaml",
    ".sh",
    ".cfg",
    ".ini",
    ".csv",
    ".html",
    ".xml",
    ".rst",
    ".ipynb",
    "",
}

#: RFC 2606 / 6761 names reserved for examples.
PLACEHOLDER_DOMAINS = {"example.org", "example.com", "example.net"}
RESERVED_TLDS = (".example", ".invalid", ".test", ".localhost")
#: git's SSH user in remote URLs; not mailboxes.
ALLOWED_ADDRESSES = {"git" + "@" + "github.com"}
#: Fictional placeholders and this project's own GitHub owners.
ALLOWED_OWNERS = {"example", "example-org", "octocat", "owner", "thorwhalen", "i2mint"}

EMAIL_RE = re.compile(r"[A-Za-z0-9_.+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")
HOME_PATH_RE = re.compile(
    r"/(?:Users|home)/[A-Za-z0-9_.-]+|(?<![\w.])/"
    + r"root\b|/(?:private/)?var/folders/|/private/"
    + r"tmp/"
    + r"|/"
    + r"Volumes/[A-Za-z0-9_.-]+|\\\\"
    + r"wsl|/"
    + r"opt/[^\s\"']+/\.env"
    + r"|\b[A-Za-z]:(?:\\{1,2}|/)Users(?:\\{1,2}|/)[A-Za-z0-9_.-]+"
)
GITHUB_URL_RE = re.compile(
    r"(?<![\w.])github\.com[/:]([A-Za-z0-9][A-Za-z0-9-]{0,38})(?=[/\s\"')\]]|\.git|$)"
)
OWNER_REPO_RE = re.compile(
    r"(?:--repo[= ]|gh repo \w+ |\brepo\w*\s*[=:]\s*[\"']|\bREPO\s*=\s*[\"'])([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9._-]+)"
)
MENTION_RE = re.compile(r"(?<![\w.@/`:])@([A-Za-z0-9][A-Za-z0-9-]{1,38})\b(?![.(])")
SECRET_RES = {
    "a GitHub token": re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_" + r"pat_[A-Za-z0-9_]{22,})"
    ),
    "a Telegram bot token": re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),
    "a Slack token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    "a PyPI token": re.compile(r"\bpypi-[A-Za-z0-9_-]{40,}"),
    "a private key": re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----"),
    "a phone number": re.compile(r"(?<![\w/+])\+\d{10,15}\b"),
    "a Telegram supergroup id": re.compile(r"(?<![\w-])-100\d{10}\b"),
    "an AWS access key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "an API key": re.compile(r"\bsk-(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{20,}"),
    "an ntfy access token": re.compile(r"\btk_[A-Za-z0-9]{24,}"),
    "a bearer token": re.compile(
        r"\b[Bb]earer (?=[A-Za-z0-9._~+/-]*\d)(?=[A-Za-z0-9._~+/-]*[A-Za-z])[A-Za-z0-9._~+/-]{20,}"
    ),
    "credentials in a URL": re.compile(
        r"\b[a-z][a-z0-9+.-]*://[^\s/:@\"'<>]+:[^\s/@\"'<>]+@"
    ),
}
#: Loopback, "any", and the RFC 5737 documentation ranges are fine in examples.
#: Dotted quads of single digits read as versions (1.0.0.0) and are skipped too.
ALLOWED_IPV4_PREFIXES = (
    "127.",
    "0.0.0.0",
    "192.0.2.",
    "198.51.100.",
    "203.0.113.",
    "255.",
)
IPV4_RE = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?!\.?\d)(?!\w)")


def _text_files() -> list[Path]:
    return [
        path
        for path in REPO_ROOT.rglob("*")
        if path.is_file()
        and path.name not in _EXCLUDED_FILES
        and path.suffix in _TEXT_SUFFIXES
        and not any(
            part in _SKIP_DIRS
            or part.endswith((".dist-info", ".egg-info"))
            or part == "site-packages"
            for part in path.relative_to(REPO_ROOT).parts
        )
    ]


def _placeholder(domain: str) -> bool:
    domain = domain.lower()
    return any(
        domain == d or domain.endswith("." + d) for d in PLACEHOLDER_DOMAINS
    ) or domain.endswith(RESERVED_TLDS)


def emails_outside_placeholders(text: str) -> list[str]:
    return [
        m.group(0)
        for m in EMAIL_RE.finditer(text)
        if not _placeholder(m.group(1)) and m.group(0) not in ALLOWED_ADDRESSES
    ]


def handles_outside_allowlist(text: str, *, prose: bool) -> list[str]:
    found = [m.group(1) for m in GITHUB_URL_RE.finditer(text)]
    found += [
        m.group(1) for m in OWNER_REPO_RE.finditer(text) if not m.group(1).isdigit()
    ]
    if prose:
        found += [m.group(1) for m in MENTION_RE.finditer(text)]
    return sorted({h for h in found if h.lower() not in ALLOWED_OWNERS})


def secrets_in(text: str) -> list[str]:
    return [what for what, pattern in SECRET_RES.items() if pattern.search(text)]


def ips_outside_documentation(text: str) -> list[str]:
    return [
        m.group(0)
        for m in IPV4_RE.finditer(text)
        if all(int(part) <= 255 for part in m.group(0).split("."))
        and any(len(part) > 1 for part in m.group(0).split("."))
        and not m.group(0).startswith(ALLOWED_IPV4_PREFIXES)
    ]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def test_no_email_addresses_outside_the_example_domains():
    offenders = {
        str(p.relative_to(REPO_ROOT)): found
        for p in _text_files()
        if (found := emails_outside_placeholders(_read(p)))
    }
    assert not offenders, f"email address(es) outside the example domains: {offenders}"


def test_no_absolute_home_paths():
    offenders = {
        str(p.relative_to(REPO_ROOT)): found
        for p in _text_files()
        if (found := HOME_PATH_RE.findall(_read(p)))
    }
    assert not offenders, f"absolute home path(s): {offenders}"


def test_no_real_looking_handles():
    offenders = {}
    for path in _text_files():
        found = handles_outside_allowlist(_read(path), prose=path.suffix != ".py")
        if found:
            offenders[str(path.relative_to(REPO_ROOT))] = found
    assert not offenders, (
        f"handle(s) not on the placeholder allowlist {sorted(ALLOWED_OWNERS)}: {offenders}"
    )


def test_no_tokens_keys_phone_numbers_or_chat_ids():
    offenders = {
        str(p.relative_to(REPO_ROOT)): found
        for p in _text_files()
        if (found := secrets_in(_read(p)))
    }
    assert not offenders, f"secret-looking values: {offenders}"


def test_no_ip_addresses_outside_loopback_and_documentation_ranges():
    offenders = {
        str(p.relative_to(REPO_ROOT)): found
        for p in _text_files()
        if (found := ips_outside_documentation(_read(p)))
    }
    assert not offenders, (
        f"IP address(es) outside loopback and the documentation ranges: {offenders}"
    )


def test_the_guard_actually_catches_leaks():
    """Mutation check: each detector must fire on a planted leak, or the guard guards nothing."""
    planted_email = "someone" + "@" + "realmail.org"
    assert emails_outside_placeholders(f"write to {planted_email}") == [planted_email]
    assert (
        emails_outside_placeholders(
            "write to ada" + "@" + "mx.example.org or x" + "@" + "host.invalid"
        )
        == []
    )
    assert HOME_PATH_RE.search("/" + "Users" + "/someone/notes")
    assert HOME_PATH_RE.search("scp server:/" + "root/py/proj/data")
    assert HOME_PATH_RE.search("/" + "var/folders/xy/T/tmp123")
    assert handles_outside_allowlist(
        "see github.com/" + "someone-real" + "/notes", prose=False
    ) == ["someone-real"]
    assert (
        handles_outside_allowlist(
            "https://api.github.com/repos/octocat/hello-world", prose=False
        )
        == []
    )
    assert handles_outside_allowlist(
        "gh repo create " + "someone" + "/inbox --private", prose=False
    ) == ["someone"]
    assert handles_outside_allowlist(
        "thanks @" + "someone-real" + " for this", prose=True
    ) == ["someone-real"]
    assert handles_outside_allowlist("write to telegram:@octocat", prose=True) == []
    assert secrets_in("token: " + "ghp_" + "a" * 36) == ["a GitHub token"]
    assert secrets_in("bot " + "123456789" + ":" + "A" * 35) == ["a Telegram bot token"]
    assert secrets_in("xox" + "b-" + "1234567890-abc") == ["a Slack token"]
    assert secrets_in("pypi" + "-" + "A" * 45) == ["a PyPI token"]
    assert secrets_in("-----BEGIN OPENSSH " + "PRIVATE KEY-----") == ["a private key"]
    assert secrets_in("call +" + "15551234567") == ["a phone number"]
    assert secrets_in("chat -" + "1001234567890") == ["a Telegram supergroup id"]
    assert secrets_in("react +1, chat -4001, update 1757581200") == []
    assert secrets_in("key " + "AKIA" + "ABCDEFGHIJKLMNOP") == ["an AWS access key"]
    assert secrets_in("key " + "sk-" + "a1" * 12) == ["an API key"]
    assert secrets_in("NTFY_TOKEN=" + "tk_" + "a" * 29) == ["an ntfy access token"]
    assert secrets_in("Authorization: " + "Bearer " + "a1" * 16) == ["a bearer token"]
    assert secrets_in("key " + "ASIA" + "ABCDEFGHIJKLMNOP") == ["an AWS access key"]
    assert secrets_in("https://" + "someone:hunter22" + "@example.org/x") == [
        "credentials in a URL"
    ]
    assert HOME_PATH_RE.search("C:" + "\\\\Users\\\\someone") and HOME_PATH_RE.search(
        "D:" + "/" + "Users/someone"
    )
    assert (
        secrets_in(
            'print("Q:\\nA\\n"), Bearer authentication/authorization, sk-spinner-bounce-animation-delay'
        )
        == []
    )
    assert ips_outside_documentation("The server is " + "10.1.2" + ".3.") == [
        "10.1.2" + ".3"
    ]
    assert (
        ips_outside_documentation("version 1.0.0.0, OID 1.3.6.1.4.1, mask 255.255.255.0")
        == []
    )
    assert secrets_in("a task-list, a risk-free desk-top") == []
    assert ips_outside_documentation(
        "server " + "203.0.113.47" + " and " + "10.1.2" + ".3"
    ) == ["10.1.2" + ".3"]
    assert (
        ips_outside_documentation(
            "bind 127.0.0.1 or 0.0.0.0; see 198.51.100.7; version 1.2.3"
        )
        == []
    )
