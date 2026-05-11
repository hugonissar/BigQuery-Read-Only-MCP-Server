# Contributing

Thanks for considering a contribution. This document covers what makes a useful issue, what makes a mergeable pull request, and — most importantly for this project — how to report a security issue safely.

## TL;DR

- **Found a security issue?** Don't open a public issue. Use [GitHub's private security advisory](https://github.com/hugonissar/BigQuery-Read-Only-MCP-Server/security/advisories/new) instead. Details in [Reporting security vulnerabilities](#reporting-security-vulnerabilities) below.
- **Found a bug?** Open an issue with steps to reproduce.
- **Want a feature?** Read [Scope](#scope) first, then open an issue to discuss before coding.
- **Want to submit a PR?** Open or claim an issue first, keep changes focused, run the local checks, and write a clear PR description.

## Code of conduct

Be decent. Disagree on technical merit, not on people. The maintainer reserves the right to remove comments, close issues, or block contributors who can't manage that.

This project follows the spirit of the [Contributor Covenant](https://www.contributor-covenant.org/version/2/1/code_of_conduct/) even when a formal `CODE_OF_CONDUCT.md` isn't checked in.

## Reporting security vulnerabilities

This is a security-sensitive tool. A bug here can leak data or rack up BigQuery costs for everyone running it. **Please do not file security issues as public GitHub issues.**

Instead, use one of these private channels:

1. **GitHub private security advisory** (preferred): go to the [Security tab](../../security/advisories/new) of this repository and open a private advisory. This stays invisible to the public until a fix is published.
2. **Email**: if private advisories aren't an option, email the maintainer (see the GitHub profile linked from commits).

What to include:

- A clear description of the issue and the impact (data leak, auth bypass, cost overrun, etc.)
- Steps to reproduce, ideally with a minimal SQL or HTTP payload
- The commit SHA or release tag you tested against
- Your suggested fix, if you have one

Response targets (best-effort, solo maintainer):

- Acknowledgement within 72 hours
- A triage assessment within 7 days
- Fix released as soon as practical, coordinated with the reporter
- Credit in the release notes if you want it

## Reporting bugs

Open an issue with:

- **What you expected to happen.** One sentence.
- **What actually happened.** One sentence plus the error message or log line.
- **How to reproduce.** A minimal SQL query or HTTP request, plus the relevant env-var values (redact secrets).
- **Environment.** Cloud Run vs local, Python version, MCP client (Claude Desktop, Cursor, etc.), region.
- **Logs.** Output of `gcloud run services logs tail` around the failure, with personally identifiable bits removed.

Bug reports without reproduction steps are slower to fix and may be closed. Not because they're unwelcome — just because they're harder to act on.

## Suggesting features

Open an issue describing the use case before writing code. This avoids you spending a weekend on a PR that won't be merged because it conflicts with the project's scope or threat model.

A good feature proposal includes:

- The problem you're solving (not just the solution you have in mind)
- Who else would benefit
- A sketch of the proposed change
- Any security or cost implications

## Scope

This project is deliberately narrow. It exists to give AI agents read-only, allowlisted, cost-capped access to BigQuery. Contributions that strengthen that mission are welcome. Contributions that broaden it usually aren't.

**In scope:**

- Tighter security defaults
- Better SQL parser coverage (edge cases, new BigQuery syntax)
- Performance improvements that don't compromise safety
- Better observability (structured logs, metrics, traces)
- Additional MCP client compatibility fixes
- Documentation, examples, deployment recipes
- Tests

**Out of scope (won't be merged):**

- Write support of any kind (INSERT, UPDATE, DELETE, MERGE, DDL, scripting). The "Read-Only" in the name is load-bearing.
- Generic database backends (Postgres, MySQL, Snowflake, etc.). Use [MCP Toolbox for Databases](https://github.com/googleapis/genai-toolbox) instead.
- Bundled auth providers (OAuth, OIDC, JWT issuers). The auth surface stays minimal; put a proxy in front if you need something richer.
- Embedded analytics, dashboards, or notebook integrations.
- New dependencies without a strong justification — the current dep list is short on purpose.

If you're unsure whether something is in scope, open an issue and ask before coding.

## Submitting pull requests

1. **Find or open an issue first.** PRs without a corresponding issue are likely to be closed unless the change is trivial (typos, doc fixes).
2. **Fork and branch.** Branch name should reference the issue: `fix/123-cte-validation` or `feat/45-add-metric`.
3. **Keep it focused.** One concern per PR. A 2000-line PR touching five subsystems will not get a useful review.
4. **Don't reformat unrelated code.** Whitespace and style changes in files you're not otherwise modifying make review harder.
5. **Update the README and docs** if you change behavior, config, or the API surface.
6. **Write a clear PR description.** What changed, why, and how to verify it. Link the issue with `Closes #123`.

## Code style

The project is a single Python file. Keep it readable.

- **Python 3.11+.** Use modern syntax (match statements, `X | Y` unions, `Self` type, etc.) where it helps clarity.
- **Type hints on all public functions and tool handlers.** Internal helpers can skip them when obvious.
- **Docstrings.** Every public function, tool, and class gets a docstring explaining what it does, what it returns, and any non-obvious behavior. Keep them accurate — stale docstrings are worse than missing ones.
- **No new third-party dependencies** without discussion in an issue first.
- **Logging over `print`.** Use the existing structured logger.
- **Constant-time comparison for any secret check** (`hmac.compare_digest`). Never `==`.
- **No SQL string interpolation against user input.** Use BigQuery parameter binding.

A formatter pass with `ruff format` is appreciated but not required.

## Testing

Run the server locally against a test BigQuery dataset before submitting:

```bash
export GCP_PROJECT_ID=your-test-project
export BQ_DATASET_ID=test_dataset
export BQ_ALLOWED_TABLE=test_table
export MCP_API_KEY=$(openssl rand -hex 32)

uvicorn main:app --host 0.0.0.0 --port 8080 --reload
```

For SQL-validation changes, include test queries in the PR description showing both the queries you expect to pass and the ones you expect to be rejected — with the expected error messages.

For security-sensitive changes (auth, allowlist, SQL parser), include adversarial test cases. Show the attack the change prevents.

## Documentation contributions

Doc improvements are some of the highest-value contributions for a tool like this. If you tried to deploy and got stuck somewhere the README didn't help, that's a bug in the docs — please open an issue or send a PR. Real-world deployment friction is hard for the maintainer to see from the inside.

## Recognition

First-time contributors get a shout-out in the release notes. Security reporters who follow the private disclosure process get a named credit (or anonymous, if preferred) once the fix is published.

## What to expect

This is maintained part-time by one person. Realistic expectations:

- Issues and PRs may sit for a week or two before triage
- Security reports are prioritized over everything else
- Out-of-scope PRs will be closed politely with an explanation, not silently ignored
- "Why was this closed" questions are welcome — closures aren't personal, they're scope-based

Thanks for reading this far. Patience and a clear issue are the two most useful things a contributor can bring.
