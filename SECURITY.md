# Security policy

## Supported versions

voice-agent is early-stage software. Security fixes are made on the latest release and on `main`.

| Version | Supported |
|---|---|
| 0.1.x (latest) | Yes |
| Older | No |

## Reporting a vulnerability

**Please do not report security problems in public issues, discussions or pull requests.**

Report them privately through **GitHub Security Advisories**: open the repository's **Security** tab and choose **Report a vulnerability**. Please include:

* the affected version or commit;
* steps to reproduce, or a proof of concept;
* the impact (what an attacker can do, and under which role or configuration);
* any suggested fix or mitigation.

We aim to acknowledge reports within a few days, keep you informed while we work on a fix, agree a disclosure date with you, and credit you in the advisory if you wish. The project is maintained on a best-effort basis.

When testing, use your own installation and fictional data. Do not access other people's data, and **do not place real phone calls** to people who have not agreed to them.

## Scope

In scope:

* the backend API and scheduler (`backend/`), the conversation engine (`engine/`) and the dashboard (`apps/dashboard/`);
* authentication, sessions, CSRF, workspace isolation and role checks;
* the LLM tool sandbox (getting an agent to perform actions outside its registered, validated tools, or across workspaces);
* the public "talk to us" links, uploads and imports, backups and restore, and the SSRF guard for provider URLs;
* the setup, start and backup scripts (`scripts/`).

Out of scope:

* vulnerabilities in third-party components (Ollama, whisper.cpp, Piper, Asterisk, FFmpeg/PyAV, npm or PyPI packages) unless voice-agent uses them unsafely. Please report those upstream;
* attacks that need administrator or code-execution access to the Mac running voice-agent;
* the security of your own reverse proxy, Asterisk server or SIP trunk;
* model behaviour that stays within the tool sandbox (for example, getting an agent to say something off-topic).

## More information

[docs/SECURITY.md](docs/SECURITY.md) describes the threat model, the implemented controls, known limitations, a hardening checklist and automated scan results. **No independent penetration test has been performed.**
