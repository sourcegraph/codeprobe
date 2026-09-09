"""codeprobe init — interactive setup wizard."""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import click

from codeprobe.adapters.models import model_set, validate_model
from codeprobe.cli._output_helpers import emit_envelope, resolve_mode
from codeprobe.cli.envelope import NextStep
from codeprobe.cli.errors import PrescriptiveError
from codeprobe.cli.wizard import (
    _load_json,
    ask_custom,
    ask_factorial_comparison,
    ask_mcp_comparison,
    ask_model_comparison,
    ask_prompt_comparison,
    validate_experiment_name,
)
from codeprobe.config.redact import (
    CredentialRequirement,
    externalize_mcp_credentials,
)
from codeprobe.core.experiment import (
    create_experiment_dir,
    ensure_default_experiment,
    find_experiment_candidates,
)
from codeprobe.core.mcp_discovery import discover_mcp_configs
from codeprobe.core.registry import available
from codeprobe.models.evalrc import EvalrcConfig
from codeprobe.models.experiment import Experiment, ExperimentConfig

# A ``${VAR}`` reference only resolves for a valid shell identifier, so the
# wizard rejects anything else rather than writing config that cannot run.
_ENV_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SOURCEGRAPH_ENV_VAR = "SOURCEGRAPH_TOKEN"

_GOAL_DEFAULTS = {
    1: "mcp-comparison",
    2: "model-comparison",
    3: "prompt-comparison",
    4: "custom",
    5: "factorial-comparison",
}


def run_init(
    path: str,
    *,
    json_flag: bool = False,
    no_json_flag: bool = False,
    json_lines_flag: bool = False,
) -> None:
    """Interactive wizard: What do you want to learn?"""
    mode = resolve_mode(
        "init", json_flag, no_json_flag, json_lines_flag,
    )

    target = Path(path).resolve()
    agents = available()
    if not agents:
        raise click.ClickException(
            "No agent adapters registered. Install an adapter first."
        )

    # Non-pretty mode (agent callers): create a working default experiment
    # instead of no-oping, so run's NO_EXPERIMENT -> init -> run loop
    # terminates (codeprobe-f7rl.12). Idempotent: an existing experiment is
    # never touched; multiple named experiments refuse prescriptively.
    if mode.mode != "pretty":
        codeprobe_dir = target / ".codeprobe"
        candidates = find_experiment_candidates(target)
        if len(candidates) > 1:
            raise PrescriptiveError(
                code="AMBIGUOUS_EXPERIMENT",
                message=(
                    f"Multiple experiments found in {codeprobe_dir}: "
                    + ", ".join(c.name for c in candidates)
                    + ". Use --config to specify which experiment."
                ),
                next_try_flag="--config",
                next_try_value=str(candidates[0]),
                detail={"candidates": [str(c) for c in candidates]},
            )

        created = not candidates
        exp_dir = (
            ensure_default_experiment(
                target, description="Auto-created by codeprobe init"
            )
            if created
            else candidates[0]
        )

        data: dict = {
            "target": str(target),
            "experiment_dir": str(exp_dir),
            "created": created,
            "configs": [],
            "interactive": False,
        }
        if not created:
            data["message"] = (
                "Experiment already exists at "
                f"{exp_dir / 'experiment.json'}; left unchanged."
            )
        emit_envelope(
            command="init",
            data=data,
            next_steps=[
                NextStep(
                    summary="Add a config",
                    command=(
                        f"codeprobe experiment add-config {exp_dir} "
                        "--label baseline --agent claude"
                    ),
                ),
                NextStep(
                    summary="Mine tasks",
                    command=f"codeprobe mine {path} --json",
                ),
                NextStep(
                    summary="Run",
                    command=f"codeprobe run {path} --json --agent claude",
                ),
            ],
        )
        return

    click.echo("Welcome to codeprobe!")
    click.echo()
    click.echo("What do you want to learn?")
    click.echo()
    click.echo("  1. Compare baseline agent vs MCP-augmented agent")
    click.echo("  2. Compare different models (e.g., Sonnet vs Opus)")
    click.echo("  3. Compare different prompts or instruction styles")
    click.echo("  4. Custom comparison")
    click.echo("  5. Factorial — vary models × prompts × tools together")
    click.echo()

    goal = click.prompt("Choose a goal", type=click.IntRange(1, 5), default=1)

    default_name = _GOAL_DEFAULTS[goal]
    experiment_name = click.prompt("Experiment name", default=default_name)
    validate_experiment_name(experiment_name)

    if goal == 1:
        evalrc, configs = _goal_mcp(agents, experiment_name)
    elif goal == 2:
        evalrc, configs = _goal_models(agents, experiment_name)
    elif goal == 3:
        evalrc, configs = _goal_prompts(agents, experiment_name)
    elif goal == 4:
        evalrc, configs = _goal_custom(agents, experiment_name)
    else:
        evalrc, configs = _goal_factorial(agents, experiment_name)

    # Every goal can attach an MCP config, and any literal credential inside
    # one is destroyed by save_experiment — leaving an arm run refuses to
    # dispatch. Rewrite them into ${VAR} references here so the experiment the
    # wizard writes is the experiment that runs.
    configs, credentials = _externalize_config_credentials(configs)

    # Create experiment directory
    experiment = Experiment(
        name=experiment_name,
        description=evalrc.description,
        configs=configs,
    )
    codeprobe_dir = target / ".codeprobe"
    codeprobe_dir.mkdir(exist_ok=True)

    from codeprobe.core.repo_hygiene import ensure_codeprobe_excluded

    ensure_codeprobe_excluded(target)

    exp_dir = create_experiment_dir(codeprobe_dir, experiment)

    # Summary
    click.echo()
    click.echo(f"Created {exp_dir.relative_to(target)}/")
    click.echo(f"  Configurations: {', '.join(c.label for c in configs)}")
    _report_credential_requirements(credentials)
    click.echo()
    click.echo("Next steps:")
    click.echo(f"  codeprobe mine {path}      # Mine tasks from your repo")
    click.echo(f"  codeprobe run {path}       # Run agents against tasks")
    click.echo(f"  codeprobe interpret {path}  # Analyze results")
    click.echo()
    # The experiment's own tasks/ dir starts empty: `mine` writes to the
    # shared .codeprobe/tasks/, and run/interpret pick them up automatically
    # via experiment discovery — no manual copy needed (codeprobe-j551).
    click.echo(
        f"Mined tasks are written to {path}/.codeprobe/tasks/ (shared); "
        f"run and interpret discover this experiment and use them automatically."
    )


def _externalize_config_credentials(
    configs: list[ExperimentConfig],
) -> tuple[list[ExperimentConfig], tuple[CredentialRequirement, ...]]:
    """Replace literal MCP secrets with ``${VAR}`` references in every config.

    Returns the rewritten configs and every environment variable they now
    reference, the caller's own references included.
    """
    rewritten: list[ExperimentConfig] = []
    requirements: list[CredentialRequirement] = []
    for config in configs:
        mcp_config, needed = externalize_mcp_credentials(config.mcp_config)
        requirements.extend(needed)
        rewritten.append(replace(config, mcp_config=mcp_config))
    return rewritten, tuple(requirements)


def _report_credential_requirements(
    requirements: Sequence[CredentialRequirement],
) -> None:
    """Name the variables that must be exported before ``run`` will dispatch.

    Only the unset ones: a variable already exported needs no instruction,
    and listing it invites the user to re-export a token they cannot see.
    """
    missing = sorted(
        {r.env_var for r in requirements if not os.environ.get(r.env_var)}
    )
    if not missing:
        return
    click.echo()
    click.echo("Your MCP arm reads these credentials from the environment")
    click.echo("(codeprobe never stores them in experiment.json). Before running:")
    for env_var in missing:
        click.echo(f"  export {env_var}=<value>")


def _prompt_agent(agents: list[str]) -> str:
    """Prompt for agent selection."""
    agents_str = ", ".join(agents)
    result: str = click.prompt(f"Agent ({agents_str})", default=agents[0])
    return result


def _prompt_model() -> str | None:
    """Prompt for optional model override."""
    model = click.prompt(
        "Model (optional, press Enter to skip)", default="", show_default=False
    )
    return model if model else None


_Result = tuple[EvalrcConfig, list[ExperimentConfig]]


def _prompt_mcp_config() -> str:
    """Prompt for MCP config with auto-discovery of known locations."""
    discovered = discover_mcp_configs()

    if discovered:
        click.echo()
        click.echo("Discovered MCP configurations:")
        for i, (p, servers) in enumerate(discovered, 1):
            click.echo(f"  {i}. {p}  ({len(servers)} servers)")
            for s in servers:
                click.echo(f"     - {s}")
        click.echo(f"  {len(discovered) + 1}. Enter a custom path")
        click.echo()

        choice = click.prompt(
            "Select MCP config",
            type=click.IntRange(1, len(discovered) + 1),
            default=1,
        )
        if choice <= len(discovered):
            return str(discovered[choice - 1][0])

    # Manual entry with tilde expansion
    while True:
        raw = click.prompt("Path to MCP config JSON")
        expanded = Path(raw).expanduser().resolve()
        if expanded.is_file():
            return str(expanded)
        click.echo(f"  Error: '{expanded}' does not exist. Try again.")


def _detect_sourcegraph_in_mcp(
    discovered: list[tuple[Path, list[str]]],
    mcp_data: dict | None = None,
) -> bool:
    """Return True if any discovered MCP config contains a Sourcegraph server.

    Checks server names for common Sourcegraph patterns (e.g.
    ``sourcegraph``, ``sourcegraph-mcp-server``).
    """
    sg_names = {"sourcegraph", "sourcegraph-mcp-server"}
    for _path, server_names in discovered:
        for name in server_names:
            if name.lower() in sg_names:
                return True
    if mcp_data:
        for name in mcp_data.get("mcpServers", {}):
            if name.lower() in sg_names:
                return True
    return False


def _prompt_sourcegraph_token_var() -> str:
    """Resolve which environment variable the MCP arm reads its token from.

    The token value is deliberately never collected. ``save_experiment``
    redacts literal credentials, so one embedded here would be gone by the
    time ``run`` looked for it; the config references a variable instead and
    the runtime resolves it from the environment.
    """
    from codeprobe.mining.sg_auth import exported_token_var, load_cached_token

    exported = exported_token_var()
    if exported is not None:
        click.echo(f"  Using ${exported} from your environment.")
        return exported

    click.echo()
    click.echo("  The MCP arm reads its Sourcegraph token from the environment at")
    click.echo("  run time; codeprobe never stores credentials in the experiment.")
    if load_cached_token() is not None:
        click.echo("  Your `codeprobe auth` cache serves mining, not the MCP arm.")

    while True:
        raw: str = click.prompt(
            "  Environment variable holding the token",
            default=_SOURCEGRAPH_ENV_VAR,
        )
        name = raw.strip().lstrip("$").strip("{}").strip()
        if _ENV_VAR_NAME_RE.match(name):
            return name
        click.echo(f"  Error: '{name}' is not a valid environment variable name.")


def _prompt_sourcegraph_url() -> str | None:
    """Prompt for optional custom Sourcegraph instance URL."""
    url = click.prompt(
        "Sourcegraph URL (press Enter for sourcegraph.com)",
        default="",
        show_default=False,
    )
    return url if url else None


def _extract_sourcegraph_mcp(
    discovered: list[tuple[Path, list[str]]],
) -> dict | None:
    """Load the Sourcegraph MCP config from a discovered config file.

    Returns the full MCP config dict with only the Sourcegraph server,
    or None if no Sourcegraph server is found.
    """
    import json

    sg_names = {"sourcegraph", "sg", "sourcegraph-mcp"}
    for path, server_names in discovered:
        matching = [n for n in server_names if n.lower() in sg_names]
        if not matching:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        servers = data.get("mcpServers", {})
        for name in matching:
            if name in servers:
                return {"mcpServers": {name: servers[name]}}
    return None


def _load_discovered_config(path: Path) -> dict | None:
    """Load a discovered MCP config file and return its mcpServers dict."""
    import json

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    servers = data.get("mcpServers", {})
    if servers:
        return {"mcpServers": servers}
    return None


def _goal_mcp(agents: list[str], name: str) -> _Result:
    """Goal 1: MCP comparison prompts."""
    agent = _prompt_agent(agents)
    model = _prompt_model()

    discovered = discover_mcp_configs()

    # Auto-detect: if any MCP configs are discovered, let the user pick one
    if discovered:
        click.echo()
        click.echo("Discovered MCP configurations:")
        for i, (p, servers) in enumerate(discovered, 1):
            server_list = ", ".join(servers)
            click.echo(f"  {i}. {p}  ({server_list})")
        manual_idx = len(discovered) + 1
        click.echo(f"  {manual_idx}. Enter a Sourcegraph token manually")
        click.echo()

        choice = click.prompt(
            "Select MCP config",
            type=click.IntRange(1, manual_idx),
            default=1,
        )
        if choice <= len(discovered):
            path = discovered[choice - 1][0]
            mcp_config = _load_discovered_config(path)
            if mcp_config:
                click.echo(f"  Using {path}")
                return ask_mcp_comparison(
                    experiment_name=name,
                    agent=agent,
                    model=model,
                    mcp_config=mcp_config,
                )

    # No discovered configs or user chose manual entry
    token_var = _prompt_sourcegraph_token_var()
    sg_url = _prompt_sourcegraph_url()
    return ask_mcp_comparison(
        experiment_name=name,
        agent=agent,
        model=model,
        sourcegraph_token="${" + token_var + "}",
        sourcegraph_url=sg_url,
    )


def _goal_models(agents: list[str], name: str) -> _Result:
    """Goal 2: Model comparison prompts."""
    agent = _prompt_agent(agents)
    ms = model_set(agent)
    if ms is not None and ms.known_tokens():
        examples = ", ".join(ms.known_tokens()[:4])
        prompt_text = f"Models to compare (comma-separated; e.g. {examples})"
        default = ms.default or None
    else:
        prompt_text = "Models to compare (comma-separated)"
        default = None
    models_raw = click.prompt(prompt_text, default=default, show_default=bool(default))
    models = [m.strip() for m in models_raw.split(",") if m.strip()]
    if not models:
        raise click.BadParameter("At least one model is required.")
    # Reject unknown tokens here (prescriptive error) so the wizard never
    # writes a config that will only fail much later at run time.
    for m in models:
        validate_model(agent, m)

    return ask_model_comparison(
        experiment_name=name,
        agent=agent,
        models=models,
    )


def _goal_factorial(agents: list[str], name: str) -> _Result:
    """Goal 5: factorial — cross-product of models × prompt-variants × tools.

    Collects the model axis (required), an optional prompt-variant axis, and an
    optional tools axis (baseline vs one MCP config). The product is built by
    :func:`ask_factorial_comparison` (codeprobe-r2bg).
    """
    agent = _prompt_agent(agents)

    ms = model_set(agent)
    if ms is not None and ms.known_tokens():
        examples = ", ".join(ms.known_tokens()[:4])
        prompt_text = f"Models to compare (comma-separated; e.g. {examples})"
        default = ms.default or None
    else:
        prompt_text = "Models to compare (comma-separated)"
        default = None
    models_raw = click.prompt(prompt_text, default=default, show_default=bool(default))
    models = [m.strip() for m in models_raw.split(",") if m.strip()]
    if not models:
        raise click.BadParameter("At least one model is required.")
    for m in models:
        validate_model(agent, m)

    variants_raw = click.prompt(
        "Prompt/instruction variant paths (comma-separated, Enter to skip)",
        default="",
        show_default=False,
    )
    variants = [v.strip() for v in variants_raw.split(",") if v.strip()] or None

    mcp_path = click.prompt(
        "MCP config JSON for a tools arm (adds baseline vs MCP, Enter to skip)",
        default="",
        show_default=False,
    )
    tool_configs: list[dict] | None = None
    if mcp_path.strip():
        tool_configs = [
            {"label": "baseline", "mcp_config": None, "preambles": ()},
            {
                "label": "with-mcp",
                "mcp_config": _load_json(mcp_path.strip()),
                "preambles": (),
            },
        ]

    return ask_factorial_comparison(
        experiment_name=name,
        agent=agent,
        models=models,
        variants=variants,
        tool_configs=tool_configs,
    )


def _goal_prompts(agents: list[str], name: str) -> _Result:
    """Goal 3: Prompt comparison prompts."""
    agent = _prompt_agent(agents)
    model = _prompt_model()
    variants_raw = click.prompt("Instruction variant paths (comma-separated)")
    variants = [v.strip() for v in variants_raw.split(",") if v.strip()]
    if not variants:
        raise click.BadParameter("At least one instruction variant path is required.")

    return ask_prompt_comparison(
        experiment_name=name,
        agent=agent,
        model=model,
        variants=variants,
    )


def _goal_custom(agents: list[str], name: str) -> _Result:
    """Goal 4: Custom comparison prompts."""
    count = click.prompt(
        "Number of configurations", type=click.IntRange(2, 10), default=2
    )

    configs_input: list[dict] = []
    for i in range(1, count + 1):
        click.echo(f"\n--- Configuration {i} ---")
        label = click.prompt("Label")
        agent = _prompt_agent(agents)
        model = _prompt_model()
        entry: dict = {"label": label, "agent": agent}
        if model:
            entry["model"] = model
        configs_input.append(entry)

    return ask_custom(
        experiment_name=name,
        configs=configs_input,
    )
