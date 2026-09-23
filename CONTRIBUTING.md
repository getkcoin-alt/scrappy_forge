# Contributing to Scrappy Forge

Thanks for taking a look at the project.

Scrappy Forge is still early enough that a thoughtful contribution can change the shape of the project, but mature enough that changes need to respect a few boundaries. The main one is simple: **the model should never gain authority just because it asked for it.**

## Good places to contribute

Useful contributions include:

- tests for tool, memory, MCP, OAuth and apply/rollback edge cases
- provider adapters and compatibility fixes
- better terminal UX
- documentation and examples
- performance work backed by measurements
- safer permission and approval flows
- connector/plugin interoperability
- failure recovery and observability
- macOS/Linux compatibility fixes
- small, well-scoped issues that make the first-run experience better

If you are new to the codebase, pick a small issue first. If the issue changes the trust model, execution permissions, credential handling or the apply path, open a discussion in the issue before writing a large patch.

## Local setup

Python 3.11+ is required.

```bash
git clone https://github.com/getkcoin-alt/scrappy_forge.git
cd scrappy_forge
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev,auth]'
pytest
ruff check .
```

You can also run the offline example without a model key:

```bash
python examples/offline_demo.py
```

## Before opening a pull request

Please:

1. Keep the change focused. One clear problem per PR is easier to review.
2. Add or update tests when behavior changes.
3. Run the relevant tests locally.
4. Run `ruff check .`.
5. Update docs when a user-visible command, option or boundary changes.
6. Do not add secrets, personal data, production credentials or copied private configuration.
7. Do not weaken approval, policy or verification checks to make a demo pass.
8. Explain what you tested and what you did **not** test.

## Architecture rules worth knowing

A few constraints are intentional:

- model output is a request, not authority
- tool arguments are validated before execution
- risky actions belong behind explicit policy/approval
- original-project application is user-controlled
- connectors and plugins do not grant themselves permission
- verification evidence must stay tied to the source state it verified

Read [ARCHITECTURE.md](ARCHITECTURE.md) and [SECURITY.md](SECURITY.md) before changing those areas.

## Pull request style

A useful PR description answers four things:

- What problem does this solve?
- What changed?
- How did you verify it?
- What remains uncertain?

Screenshots are welcome for terminal/UI changes. Reproduction steps are much more useful than “it doesn't work.”

## Security reports

Please **do not open a public issue for a vulnerability**. Follow [SECURITY.md](SECURITY.md).

## Licensing

The repository's license governs contributions once the public open-source license is selected. Until then, do not assume that public visibility alone grants reuse rights.

Thanks for helping make the boring parts reliable. That is usually where the real agent engineering lives.
