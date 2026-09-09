"""Redact secrets from MCP config dicts before logging or serialization.

All Authorization header values are unconditionally replaced with
``[REDACTED]`` to prevent accidental exposure in logs, experiment.json,
or repr() output.

CLI-arg patterns (``--header "Authorization: token sgp_..."``), env values
containing known token prefixes, and other secret-shaped strings are also
redacted.

Redaction is lossy on purpose, so a config that reaches it holding a literal
secret loses that secret permanently. :func:`externalize_mcp_credentials` is
the inverse: it rewrites literal secrets into ``${VAR}`` references *before*
persistence, so the value survives as an environment lookup the runtime can
resolve. The two functions must cover the same locations — anything redaction
destroys that externalization misses becomes an ``UNUSABLE_MCP_CREDENTIAL``
failure at run time.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any

_SENSITIVE_HEADER_NAMES = frozenset({"authorization"})

# Prefixes that indicate a secret token value.  Kept intentionally broad —
# false positives (redacting a non-secret that starts with ``sk-``) are
# strictly better than false negatives (leaking a real key).
#
# This is the canonical prefix list for the whole codebase — the scoring
# sandbox (core/scoring/sandbox.py) and the trace content policy
# (trace/content_policy.py) both derive their free-text token regexes
# from it via :func:`token_freetext_pattern`, so a prefix added here
# propagates to every redaction surface.
TOKEN_PREFIXES = (
    "sgp_",  # Sourcegraph
    "ghp_",  # GitHub PAT
    "gho_",  # GitHub OAuth
    "ghs_",  # GitHub App
    "ghr_",  # GitHub Refresh
    "glpat-",  # GitLab PAT
    "sk-",  # OpenAI / Anthropic
    "sk-proj-",  # OpenAI project-scoped
    "sk-ant-",  # Anthropic
    "xoxb-",  # Slack bot
    "xoxp-",  # Slack user
    "xoxa-",  # Slack app
)

_AUTH_HEADER_RE = re.compile(
    r"^(Authorization:\s*(?:token|Bearer)\s+)\S+",
    re.IGNORECASE,
)

# Shell-style env-var references: ``${VAR}`` (POSIX braced) or ``$VAR`` (bare).
# Both forms are valid expansion sites (envsubst, bash, sh) and must round-trip
# through redaction so the runtime can substitute the real value at exec time.
# Canonical for the codebase: config/mcp_runtime.py expands against this same
# pattern, so what redaction preserves is exactly what the runtime resolves.
ENV_REFERENCE_RE = re.compile(
    r"\$(?:\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)\}|"
    r"(?P<bare>[A-Za-z_][A-Za-z0-9_]*))"
)


def _has_env_var_ref(value: str) -> bool:
    """True if *value* contains a shell-style env-var reference (``$VAR`` or ``${VAR}``)."""
    return bool(ENV_REFERENCE_RE.search(value))


def token_freetext_pattern() -> re.Pattern[str]:
    """Compile a regex matching token-shaped strings in free text.

    Each :data:`TOKEN_PREFIXES` entry followed by 16+ token-body chars
    (alphanumerics, underscore, hyphen — so ``sk-proj-`` / ``sk-ant-``
    long forms match in full). Shared by every redaction surface that
    scans free text rather than structured config.
    """
    return re.compile(
        "|".join(re.escape(p) + r"[A-Za-z0-9_\-]{16,}" for p in TOKEN_PREFIXES)
    )


def _is_secret(value: str) -> bool:
    """Heuristic: does *value* look like a secret token?"""
    return any(value.startswith(prefix) for prefix in TOKEN_PREFIXES)


def _redact_auth_arg(value: str) -> str:
    """Redact the token portion of an ``Authorization: <scheme> <token>`` string."""
    m = _AUTH_HEADER_RE.match(value)
    if m:
        return m.group(1) + "[REDACTED]"
    return value


def redact_mcp_headers(mcp_config: dict | None) -> dict | None:
    """Return a deep copy of *mcp_config* with secrets redacted.

    Handles three secret locations:
    1. Structured ``headers`` dicts (``{"Authorization": "token ..."}``).
    2. CLI ``args`` lists (``["--header", "Authorization: token sgp_..."]``).
    3. ``env`` dicts with token-shaped values.

    Returns ``None`` when *mcp_config* is ``None``.
    Returns an empty dict when *mcp_config* is empty.
    Non-standard structures (no ``mcpServers`` key) pass through unchanged.
    The original dict is never mutated.
    """
    if mcp_config is None:
        return None
    if not mcp_config:
        return {}

    result = copy.deepcopy(mcp_config)

    servers = result.get("mcpServers")
    if not isinstance(servers, dict):
        return result

    for _name, server_cfg in servers.items():
        if not isinstance(server_cfg, dict):
            continue

        # 1. Structured headers dict
        headers = server_cfg.get("headers")
        if isinstance(headers, dict):
            for key in list(headers):
                if key.lower() in _SENSITIVE_HEADER_NAMES:
                    if isinstance(headers[key], str):
                        # Preserve env-var references (``$VAR`` or ``${VAR}``) —
                        # they aren't secrets themselves, and redacting them
                        # breaks round-tripping through save/load.
                        if _has_env_var_ref(headers[key]):
                            continue
                        headers[key] = "[REDACTED]"

        # 2. CLI args list — redact "--header" value args and token-shaped args
        args = server_cfg.get("args")
        if isinstance(args, list):
            for i, arg in enumerate(args):
                if not isinstance(arg, str):
                    continue
                if _has_env_var_ref(arg):
                    # Env-var reference (``$VAR`` or ``${VAR}``) — not a secret;
                    # preserve verbatim so the runtime can expand it.
                    continue
                if _AUTH_HEADER_RE.match(arg):
                    args[i] = _redact_auth_arg(arg)
                elif _is_secret(arg):
                    args[i] = "[REDACTED]"

        # 3. Env dict — redact token-shaped values
        env = server_cfg.get("env")
        if isinstance(env, dict):
            for key in list(env):
                val = env[key]
                if not isinstance(val, str):
                    continue
                if _has_env_var_ref(val):
                    continue
                if _is_secret(val):
                    env[key] = "[REDACTED]"

    return result


# ---------------------------------------------------------------------------
# Externalization — write ``${VAR}`` references instead of literal secrets
# ---------------------------------------------------------------------------

# Server names that map onto the Sourcegraph token variable the rest of the
# codebase already reads (doctor's readiness check, config/defaults.py's
# MCP-family gate, the evalrc-mcp-comparison template). Inventing a codeprobe-
# namespaced variable for these would leave a user with a token exported under
# the name every other command expects and an MCP arm that cannot see it.
_SOURCEGRAPH_SERVER_NAMES = frozenset(
    {"sourcegraph", "sg", "sourcegraph-mcp", "sourcegraph-mcp-server"}
)
_SOURCEGRAPH_ENV_VAR = "SOURCEGRAPH_TOKEN"

# ``token <secret>`` / ``Bearer <secret>`` — the scheme is not secret, so it is
# kept and only the credential after it becomes a reference.
_HEADER_SCHEME_RE = re.compile(r"^(?P<scheme>(?:token|Bearer)\s+)(?=\S)", re.IGNORECASE)

_NON_IDENTIFIER_RE = re.compile(r"[^A-Za-z0-9]+")


@dataclass(frozen=True)
class CredentialRequirement:
    """One environment variable an MCP config needs exported at run time.

    *path* is the dotted location inside the config (for example
    ``mcpServers.sourcegraph.headers.Authorization``). *rewritten* is True
    when externalization put the reference there, False when the config
    already carried it. Never holds secret material.
    """

    env_var: str
    path: str
    rewritten: bool


def _display_path(path: tuple[str, ...]) -> str:
    return ".".join(path) if path else "<root>"


def _env_var_for_server(name: str) -> str:
    """Variable name to reference for *name*'s credential."""
    if name.lower() in _SOURCEGRAPH_SERVER_NAMES:
        return _SOURCEGRAPH_ENV_VAR
    slug = _NON_IDENTIFIER_RE.sub("_", name).strip("_").upper()
    return f"CODEPROBE_MCP_{slug}_TOKEN" if slug else "CODEPROBE_MCP_TOKEN"


def _reference(env_var: str) -> str:
    return "${" + env_var + "}"


def _externalize_headers(
    server_cfg: dict,
    base: tuple[str, ...],
    env_var: str,
    rewritten: set[str],
) -> None:
    headers = server_cfg.get("headers")
    if not isinstance(headers, dict):
        return
    for key in list(headers):
        value = headers[key]
        if key.lower() not in _SENSITIVE_HEADER_NAMES or not isinstance(value, str):
            continue
        if _has_env_var_ref(value):
            continue
        # Mirrors redact_mcp_headers: ANY literal value under a sensitive
        # header is destroyed on save, token-shaped or not, so every one of
        # them has to be externalized — not just the recognized prefixes.
        scheme = _HEADER_SCHEME_RE.match(value)
        headers[key] = (
            scheme.group("scheme") + _reference(env_var)
            if scheme
            else _reference(env_var)
        )
        rewritten.add(_display_path((*base, "headers", key)))


def _externalize_args(
    server_cfg: dict,
    base: tuple[str, ...],
    env_var: str,
    rewritten: set[str],
) -> None:
    args = server_cfg.get("args")
    if not isinstance(args, list):
        return
    for i, arg in enumerate(args):
        if not isinstance(arg, str) or _has_env_var_ref(arg):
            continue
        header_arg = _AUTH_HEADER_RE.match(arg)
        if header_arg:
            args[i] = header_arg.group(1) + _reference(env_var)
        elif _is_secret(arg):
            args[i] = _reference(env_var)
        else:
            continue
        rewritten.add(_display_path((*base, "args", str(i))))


def _externalize_env(
    server_cfg: dict,
    base: tuple[str, ...],
    rewritten: set[str],
) -> None:
    env = server_cfg.get("env")
    if not isinstance(env, dict):
        return
    for key in list(env):
        value = env[key]
        if not isinstance(value, str) or _has_env_var_ref(value):
            continue
        if not _is_secret(value):
            continue
        # The server already names the variable it wants; reference that
        # rather than inventing a second name for the same secret.
        env[key] = _reference(key)
        rewritten.add(_display_path((*base, "env", key)))


def _env_references(value: Any, path: tuple[str, ...]) -> list[tuple[str, str]]:
    """Collect ``(display_path, variable)`` for every reference in *value*."""
    if isinstance(value, str):
        return [
            (_display_path(path), match.group("braced") or match.group("bare"))
            for match in ENV_REFERENCE_RE.finditer(value)
        ]
    if isinstance(value, list):
        return [
            found
            for index, item in enumerate(value)
            for found in _env_references(item, (*path, str(index)))
        ]
    if isinstance(value, dict):
        return [
            found
            for key, item in value.items()
            for found in _env_references(item, (*path, str(key)))
        ]
    return []


def externalize_mcp_credentials(
    mcp_config: dict | None,
) -> tuple[dict | None, tuple[CredentialRequirement, ...]]:
    """Return a copy with literal secrets replaced by ``${VAR}`` references.

    Covers the same three locations as :func:`redact_mcp_headers` — structured
    ``headers``, CLI ``args``, and ``env`` dicts — because a literal secret in
    any of them is destroyed when the experiment is persisted, leaving an arm
    that ``run`` refuses to dispatch.

    Returns the rewritten config plus every environment variable it now needs,
    including references the caller supplied itself. The original dict is never
    mutated and no return value carries secret material.
    """
    if mcp_config is None:
        return None, ()

    result = copy.deepcopy(mcp_config)
    rewritten: set[str] = set()

    servers = result.get("mcpServers")
    if isinstance(servers, dict):
        for name, server_cfg in servers.items():
            if not isinstance(server_cfg, dict):
                continue
            base = ("mcpServers", str(name))
            env_var = _env_var_for_server(str(name))
            _externalize_headers(server_cfg, base, env_var, rewritten)
            _externalize_args(server_cfg, base, env_var, rewritten)
            _externalize_env(server_cfg, base, rewritten)

    requirements = tuple(
        CredentialRequirement(env_var=var, path=path, rewritten=path in rewritten)
        for path, var in _env_references(result, ())
    )
    return result, requirements
