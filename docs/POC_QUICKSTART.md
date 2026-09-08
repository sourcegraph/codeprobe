# POC Quickstart

The first decision in a CodeProbe POC is not which model to compare. It is
whose machine the run happens on, because that answer determines everything
downstream: which install path you use, who sees repository source, what you
are allowed to carry out of the engagement, and how long the whole thing takes.
Settle it in the first call.

If the customer will run CodeProbe themselves against their own code, go to
[Zero-code-access engagements](#the-customer-runs-it-and-you-never-touch-the-repo).
If you are running it on a repository you can legitimately clone — a public
repo, a demo repo, or a customer repo you have written access to — read on.

## Install the published CLI, not this checkout

This repository is here so you can read the docs, file POC issues, and look at
the source. It is not the install path. Install the released package:

```bash
uv tool install codeprobe     # or: pipx install codeprobe
```

`uv tool install` and `pipx` put the CLI in its own environment and on your
PATH. A plain `pip install` into whatever interpreter happens to be active can
land the package somewhere the `codeprobe` on your PATH is not, which surfaces
much later as a `ModuleNotFoundError` from a command that looked installed.

Then check readiness from inside the target repo, which must be a full clone
because shallow clones cannot be mined:

```bash
cd /path/to/target/repo
codeprobe doctor --repo . --agent claude
```

Use `--agent copilot` when GitHub Copilot CLI is the selected path, and add
`--private-ca /path/to/ca.pem` when the customer's proxy requires a custom CA.
Run `doctor` before you are on a call, not during one.

## Every run needs a container prepared in advance

`codeprobe run` refuses to start outside a container and exits with
`UNCONTAINED_REFUSED`. That refusal is deliberate: a run executes an autonomous
agent with `--dangerously-skip-permissions` alongside mined third-party test
and verifier scripts, so on a bare host it would have your full filesystem,
credential, and network access. Prepare the released images once, with
digest-pinned references:

```bash
codeprobe bootstrap \
  --agent-image 'registry.example.test/platform/codeprobe-agent@sha256:<agent-digest>' \
  --scoring-image 'registry.example.test/platform/codeprobe-scoring@sha256:<scoring-digest>'
```

Get the current digests from the upstream release you are running. Docker and
Podman both work, and offline archives, private registries, proxies, and
private CAs are covered in [oci_images.md](oci_images.md). Do this the day
before a customer session; pulling images over a conference-room network while
someone watches is how a POC loses twenty minutes.

## Let `assess` pick the workflow

Point `assess` at the repo before you plan anything, and let its score decide
which of the three paths you are on:

```bash
codeprobe assess /path/to/repo
```

A repo with real merged-PR history takes the
[standard workflow](workflows/standard.md), which mines ground truth straight
from merge commits and is the path you want in nearly every POC. A repo with
squashed history, a brand-new repo, or vendored code scores low and takes the
[cold-start workflow](workflows/cold-start.md), which generates synthetic
comprehension tasks from current repo state instead. When the interesting
behavior spans services — and this is the shape that shows Sourcegraph MCP and
cross-repo retrieval doing real work — use the
[cross-repo workflow](workflows/cross-repo.md) with
`codeprobe auth sourcegraph` and `--backend sourcegraph`.

The full flow with real commands and real output is in
[example-walkthrough.md](example-walkthrough.md). Read it once before your
first POC.

## Cap cost before the first run

Pass `--max-cost-usd` on every run, including your own rehearsals:

```bash
codeprobe mine .
codeprobe run . --agent claude --max-cost-usd 5.00
codeprobe interpret .
```

To compare configurations rather than exercise a single agent, start from
`codeprobe init`, a guided wizard that builds the comparison from a "what do
you want to learn?" question and saves you from hand-writing an experiment
profile in front of a customer. If you would rather drive the tool through a
coding agent, `codeprobe skills install` drops the packaged skills into
`~/.claude/skills/`; the workflow is in
[workflows/with-agents.md](workflows/with-agents.md).

Freeze the sampling plan and the configurations before results exist. A
comparison whose task set changes after you have seen a number is not a
comparison anyone should act on, and a customer's platform team will spot it.

## The customer runs it and you never touch the repo

For customers whose code cannot leave their environment, the
[Zero-Code-Access Operator Kit](zero-code-access/README.md) is the operating
contract rather than a suggestion. A data-owner technical owner runs everything
inside their environment, and Sourcegraph personnel never receive repository,
source, prompt, patch, trace, task-level result, log, or diagnostic access.

Your side of that engagement is
[support-methodology.md](zero-code-access/support-methodology.md) and
[coordination.md](zero-code-access/coordination.md), plus the intake, the
scheduling, and the bounded findings review. The customer gets
[data-owner-runbook.md](zero-code-access/data-owner-runbook.md) and the
templates. Note the sequencing constraint that catches people: the sampling
plan and the two-configuration profile are frozen before any results exist, and
one structured session capped at 45 minutes is the only synchronous contact.

Anything crossing the boundary crosses as an evidence bundle, defined in
[EVIDENCE_BUNDLE.md](EVIDENCE_BUNDLE.md).

## Snapshots default to hashes only, and should stay that way

`codeprobe snapshot create` is how a result leaves the customer's machine. The
default mode carries per-file `sha256` and size, never file bodies, and
including bodies takes an explicit `--allow-source-in-export` opt-in with a
pre-publish canary gate for secret inclusion. Scanning is deterministic
throughout, using `gitleaks`, `trufflehog`, or configured regex patterns, with
no LLM anywhere in the redaction path — a useful fact to have ready when a
security reviewer asks.

Do not reach for `--allow-source-in-export` to make a report more convincing.
The details are in [SNAPSHOT_REDACTION.md](SNAPSHOT_REDACTION.md).

## Set expectations from the support matrix, not from the demo

CodeProbe is beta software and the public contract is not a 1.0 stability
promise, so tell customers that before they build a procurement decision on a
number. The supported configuration is narrow: CPython 3.11 through 3.13,
Ubuntu 22.04 LTS on `linux/amd64`, and Docker on that runner. macOS is mining
only, Podman is preview, and Windows is unsupported. Claude Code is the
supported agent, GitHub Copilot CLI is preview, and Codex is unsupported for
repository-edit comparisons.

The current matrix is [support.md](support.md), with
`support_policy.json` as the machine-readable source. Enterprise deployment
questions go to [security/enterprise_deployment.md](security/enterprise_deployment.md).

## Where to send what

File issues about POC mechanics, missing runbook steps, and this
repository's docs here in `sourcegraph/codeprobe`. Product defects, CLI bugs,
and feature requests belong upstream in
[sjarmak/codeprobe](https://github.com/sjarmak/codeprobe), where the code is
maintained and released. When you hit something that cost you time on a live
customer call, write it down here the same day, while you still remember the
exact command and the exact error code.
