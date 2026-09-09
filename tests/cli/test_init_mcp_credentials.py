"""`init` must produce MCP arms that `run` will actually dispatch.

Regression guard for the wizard's own happy path: it embedded the live
Sourcegraph token in the experiment, ``save_experiment`` redacted it on the
way to disk, and the next command failed with UNUSABLE_MCP_CREDENTIAL. The
experiment was dead the moment it was created, one command before anything
reported it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from codeprobe.cli import main

_LITERAL_TOKEN = "sgp_live1234567890abcdefghij"


def _discovered_claude_config(tmp_path: Path) -> Path:
    """A Claude Code MCP config carrying a literal Sourcegraph token."""
    path = tmp_path / "claude_mcp.json"
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "sourcegraph": {
                        "type": "http",
                        "url": "https://sourcegraph.com/.api/mcp/all",
                        "headers": {"Authorization": f"token {_LITERAL_TOKEN}"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def _init_from_discovered_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[object, dict]:
    """Run the goal-1 wizard against one discovered config; return the result."""
    repo = tmp_path / "repo"
    repo.mkdir()
    discovered = _discovered_claude_config(tmp_path)
    monkeypatch.setattr(
        "codeprobe.cli.init_cmd.discover_mcp_configs",
        lambda: [(discovered, ["sourcegraph"])],
    )

    result = CliRunner().invoke(
        main,
        ["init", str(repo), "--no-json"],
        # goal, name, agent, model, MCP config choice
        input="1\n\n\n\n1\n",
    )

    experiments = list((repo / ".codeprobe").glob("*/experiment.json"))
    assert len(experiments) == 1, result.output
    return result, json.loads(experiments[0].read_text(encoding="utf-8"))


def _with_mcp(experiment: dict) -> dict:
    return next(c for c in experiment["configs"] if c["label"] == "with-mcp")


def test_discovered_literal_token_becomes_env_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SOURCEGRAPH_TOKEN", raising=False)

    _result, experiment = _init_from_discovered_config(tmp_path, monkeypatch)

    header = _with_mcp(experiment)["mcp_config"]["mcpServers"]["sourcegraph"][
        "headers"
    ]["Authorization"]
    assert header == "token ${SOURCEGRAPH_TOKEN}"


def test_persisted_experiment_never_holds_the_literal_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SOURCEGRAPH_TOKEN", raising=False)

    result, experiment = _init_from_discovered_config(tmp_path, monkeypatch)

    assert _LITERAL_TOKEN not in json.dumps(experiment)
    assert _LITERAL_TOKEN not in result.output


def test_wizard_output_names_the_variable_to_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SOURCEGRAPH_TOKEN", raising=False)

    result, _experiment = _init_from_discovered_config(tmp_path, monkeypatch)

    assert "export SOURCEGRAPH_TOKEN=" in result.output


def test_exported_variable_is_reported_as_satisfied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SOURCEGRAPH_TOKEN", _LITERAL_TOKEN)

    result, _experiment = _init_from_discovered_config(tmp_path, monkeypatch)

    assert "export SOURCEGRAPH_TOKEN=" not in result.output


def test_created_experiment_clears_run_credential_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The end-to-end contract: init's output survives run's preflight."""
    monkeypatch.delenv("SOURCEGRAPH_TOKEN", raising=False)
    _result, experiment = _init_from_discovered_config(tmp_path, monkeypatch)

    from codeprobe.config.mcp_runtime import resolve_mcp_runtime_config

    resolved = resolve_mcp_runtime_config(
        _with_mcp(experiment)["mcp_config"],
        environ={"SOURCEGRAPH_TOKEN": _LITERAL_TOKEN},
    )

    assert resolved is not None
    assert (
        resolved["mcpServers"]["sourcegraph"]["headers"]["Authorization"]
        == f"token {_LITERAL_TOKEN}"
    )
