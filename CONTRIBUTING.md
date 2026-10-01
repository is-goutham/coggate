# Contributing to CogGate

Thank you for helping build CogGate. This project sits on a sensitive boundary:
it evaluates developer code, calls external platforms, stores gate state, and
can influence whether pull requests merge. Correctness and recoverability take
priority over implementation speed.

## Before you begin

For non-trivial changes, open or reference a GitHub issue describing:

- the problem or use case;
- the proposed behavior;
- affected platforms or core components;
- failure and recovery expectations;
- any security or compatibility considerations.

Small documentation fixes and narrowly scoped test improvements can be
submitted directly.

## Development setup

CogGate requires Python 3.11 or newer.

```bash
git clone https://github.com/is-goutham/coggate.git
cd coggate
python -m venv .venv
```

Activate the environment:

```powershell
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
```

```bash
# macOS/Linux
source .venv/bin/activate
```

Install dependencies:

```bash
python -m pip install -r requirements-dev.txt
```

Run the local checks (matching the CI workflow):

```bash
python -m pytest
python -m mypy function_app.py src tests
```

## Architecture rules

### Keep the core platform-independent

`src/core` must not depend directly on GitHub, Azure DevOps, or HTTP payload
shapes. Normalize platform events at ingress and interact with platforms through
adapter contracts.

### Preserve tenant and commit boundaries

Every stateful operation must be scoped to the correct platform, tenant,
repository, pull request, and head SHA. Never use wildcard Redis scans to locate
active quiz state.

### Make concurrency ownership explicit

Distributed leases must use unique owner tokens. Renewal and release must
compare the stored token atomically. State transitions that must remain
consistent belong in a transaction or Lua script.

### Design terminal paths deliberately

Every path after creating a pending platform check must eventually do one of
the following:

1. complete the check successfully;
2. persist enough information for recovery;
3. raise so durable infrastructure can retry or dead-letter the event.

Do not swallow failures or return a success-shaped fallback when the platform
may remain blocked.

### Treat repository content as hostile input

Changed code can contain secrets, malicious prompt instructions, Markdown
links, and user mentions. Keep source content inside the untrusted-data
boundary, redact before LLM submission, enforce input bounds, and sanitize
generated output before publication.

### Keep answer validation strict

Do not loosen the answer grammar to search arbitrary comment text for commands.
Exact parsing prevents accidental triggers, ambiguous answers, and command
smuggling.

## Testing expectations

Every behavioral change should include focused tests. Depending on the area,
cover:

- the expected success path;
- malformed or unauthorized input;
- duplicate delivery and concurrent execution;
- stale commit/SHA behavior;
- partial publication failures;
- retry and idempotency behavior;
- primary and fail-open recovery failures;
- tenant isolation;
- secret redaction and prompt injection attempts.

Prefer deterministic unit tests. Integration tests that require cloud resources
must be clearly marked and must not depend on a contributor's production
environment.

## Pull-request guidelines

Keep pull requests small enough to review as one coherent change. A good PR:

- explains the user-facing or operational problem;
- describes the chosen approach and important tradeoffs;
- calls out state-schema or API-contract changes;
- includes tests and documentation updates;
- contains no unrelated refactoring;
- identifies any deployment or migration steps.

Use this checklist in the PR description:

```text
- [ ] I kept platform-specific behavior outside the core.
- [ ] I added or updated tests for success and failure paths.
- [ ] I considered idempotency, concurrency, and stale-SHA behavior.
- [ ] I did not add secrets, customer code, or sensitive logs.
- [ ] pytest passes locally.
- [ ] strict mypy passes locally.
- [ ] I updated relevant documentation.
```

## Commit guidance

Write concise, imperative commit subjects that explain the meaningful change:

```text
Add owned Redis lease renewal
Handle stale SHA during quiz publication
Implement GitHub check-run completion
```

Avoid commits that combine unrelated cleanup with behavioral changes.

## Dependencies

Before adding a runtime dependency:

- confirm the standard library or an existing dependency cannot solve the
  problem cleanly;
- prefer actively maintained packages with clear licensing;
- pin a compatible version range;
- update both packaging metadata and requirement files where applicable;
- document any operational requirements.

## Documentation

The README describes the product and current implementation status. When
behavior changes, update the README and related public documentation rather
than leaving them contradictory.

## Security and sensitive data

Never commit:

- access tokens, connection strings, webhook secrets, or private keys;
- real customer repositories or proprietary code samples;
- unredacted webhook payloads containing personal or organization data;
- production Redis snapshots or application logs.

Report suspected vulnerabilities privately through GitHub private vulnerability
reporting when available. Do not open a public issue containing exploit details.

## Code of conduct

Be respectful, assume good intent, give actionable feedback, and focus review
comments on the work rather than the contributor. Harassment, discrimination,
and abusive behavior are not acceptable.
