# Security Policy

This project is security-sensitive by design: it gates AI-agent access to BigQuery, and a vulnerability here can leak data, bypass cost controls, or rack up BigQuery bills for everyone running it. Responsible disclosure is appreciated and taken seriously.

## Supported versions

This is an early-stage project with rolling releases. Security fixes are applied as follows:

| Version | Supported |
|---|---|
| Latest tagged release | Yes |
| `main` branch | Yes (development) |
| Older tagged releases | No — please upgrade |

If you're running this in production, pin to a tagged release and watch the repository for security advisories. Don't deploy `main` directly.

## Reporting a vulnerability

**Do not open a public GitHub issue for security problems.** Public issues alert attackers before a fix exists.

Use one of these private channels instead:

**GitHub private security advisory (preferred).** Open one at <https://github.com/hugonissar/BigQuery-Read-Only-MCP-Server/security/advisories/new>. This is invisible to the public and lets the maintainer coordinate a fix with you before disclosure.

### What to include in your report

The more of this you can provide, the faster the fix:

- A clear description of the vulnerability and its impact (data exposure, auth bypass, cost amplification, etc.)
- Affected version or commit SHA
- Step-by-step reproduction — ideally a minimal SQL query, HTTP request, or config that triggers the issue
- Logs or output showing the unexpected behavior
- Any proof-of-concept code
- Your suggested fix or mitigation, if you have one
- Whether you'd like credit and under what name (or to remain anonymous)

## Response and disclosure process

Best-effort targets from a solo maintainer:

| Phase | Target |
|---|---|
| Initial acknowledgement | Within 72 hours |
| Triage assessment (severity + plan) | Within 7 days |
| Fix released for critical issues | As soon as practical, typically days |
| Fix released for lower-severity issues | Coordinated with the reporter |
| Public disclosure | After the fix is released, with reporter credited |

For serious vulnerabilities, an embargo period is observed: details stay private until users have had reasonable time to upgrade. The exact timeline is coordinated with the reporter.

## In scope

The following are treated as security issues and qualify for the private disclosure process:

- **Allowlist bypass.** Any SQL that successfully queries a table not in `BQ_DATASET_ID` × `BQ_ALLOWED_TABLE`.
- **Read-only bypass.** Any path that lets DDL, DML, scripting, or procedural SQL execute, including via SQL injection, comment tricks, or parser confusion.
- **Authentication bypass.** Any way to call the MCP server or `/admin` endpoint without the correct key, including timing attacks against the comparison.
- **Cost amplification.** Any technique that bypasses `MAX_SCAN_MB`, `MAX_RESULT_ROWS`, `BQ_JOB_TIMEOUT_SECS`, or the rate limiter to cause unexpectedly large BigQuery jobs.
- **Information disclosure.** Schema, query, or result data leaking to unauthenticated clients, leaking across tenants (multi-key deployments), or appearing in logs in violation of stated logging behavior.
- **Denial of service.** Any low-cost-to-attacker request that disables the server or exhausts its resources in ways the rate limiter and concurrency caps don't prevent.
- **Privilege escalation.** Any way for a regular API key holder to perform admin actions, or for a request to obtain BigQuery permissions beyond what the service account holds.
- **Default-configuration weaknesses.** Issues that affect deployments following the README's recommended setup.

## Out of scope

The following are not treated as vulnerabilities in this project:

- **Prompt injection of the LLM client.** The server is a tool the LLM uses; prompt-injection defense belongs at the model layer. This is documented in the README's security model.
- **Misconfiguration by the operator.** Examples: granting the service account `bigquery.admin`, disabling the rate limiter, setting `MAX_SCAN_MB` to a huge value, exposing the service without an API key, committing keys to git. These are operator errors, not server bugs.
- **Vulnerabilities in dependencies.** Report those upstream to the affected project. If a dep update is needed, file a regular issue.
- **Issues requiring already-compromised credentials.** If the attacker has the API key, the admin key, or GCP credentials, they already have what the server protects.
- **Theoretical attacks without practical impact.** Reports must include a working proof of concept or a clear, realistic exploitation path.
- **Automated scanner output without analysis.** Drive-by reports from web scanners (missing HSTS, missing CSP, TLS cipher trivia on Cloud Run's managed endpoint, etc.) without a demonstrated impact will be closed.
- **Social engineering** of the maintainer or contributors.
- **Physical attacks** or attacks requiring access to the operator's GCP project.

## Recognition

Reporters who follow this process get:

- Public credit in the security advisory and release notes (or kept anonymous if you prefer)
- A mention in a `SECURITY_CREDITS` file if/when one exists
- The maintainer's thanks

This project doesn't currently offer monetary bug bounties.

## Hardening checklist for operators

Independent of vulnerability reports, operators can reduce risk by following the README's deployment guidance. The high-impact items:

- Use a dedicated service account with the minimum IAM roles listed in the README
- Grant `bigquery.dataViewer` at dataset or table scope, never project-wide
- Set `MCP_ADMIN_KEY` to a separate value from `MCP_API_KEY`
- Tune `MAX_SCAN_MB` to the smallest value that fits your workload
- Pin to a tagged release, not `main`
- Watch this repository for security advisories (Settings → Notifications → Custom → Security alerts)
- Rotate API keys periodically and on any suspicion of compromise
- Restrict network access via Cloud Run ingress settings or Cloud Armor where feasible
- Audit BigQuery jobs created by the service account via Cloud Audit Logs

## Questions

Non-vulnerability security questions (how does X work, is Y safe, etc.) can be asked in public GitHub issues. Anything that touches on a potential exploit goes through the private channels above.
