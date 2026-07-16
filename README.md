# Locestra

> One interface. Local models. Real agents. Cloud only when needed.

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](LICENSE)
[![Status: Alpha](https://img.shields.io/badge/status-alpha-orange.svg)](docs/CURRENT_STATE.md)

Locestra is a local-first AI orchestration platform for software development. It connects local LLMs, repository-aware coding agents, scoped memory and knowledge, browser automation, and optional cloud escalation behind one OpenAI-compatible gateway.

Locestra is not another model or just another chat UI. It is the control plane that routes each task to the right model, agent, and tool while keeping ordinary work local and making cloud use explicit.

Locestra is an independent open-source project created and maintained by [DenisGeide](https://github.com/DenisGeide).

> [!WARNING]
> Locestra is an early development preview for a single trusted user on a Windows workstation. It is not production-ready or multi-user software. Do not expose its services to a LAN or the public Internet. Installation, configuration, APIs, and internal contracts may change.

## Current status

This first public snapshot contains the verified work from Stages 000–004:

- platform governance, permissions, lifecycle, and health contracts;
- deterministic task planning and automatic routing;
- scoped, inspectable long-term memory;
- repository knowledge, indexing, retrieval, and context envelopes.

Stage 005, the hardened local Qwen/Codex Coding Engine, is under final acceptance and will be published after its live, regression, lifecycle, and UI gates are complete. Stages 006–012 will add managed MCP integrations, a unified tool registry, durable voice and image jobs, interfaces, controlled self-improvement, and versioned evaluations.

See the [Roadmap](docs/ROADMAP.md) and [Current State](docs/CURRENT_STATE.md) for evidence, limitations, and the difference between implemented, verified, and planned capabilities.

## What Locestra is being built to do

- Route simple tasks to a fast local model and complex work to a stronger local model.
- Understand repositories, edit files, run tests, use Git, and create reviewable local commits.
- Escalate explicitly approved difficult programming tasks to a cloud coding agent.
- Retrieve current documentation and inspect websites with browser automation.
- Maintain scoped project knowledge and controlled long-term memory.
- Add voice, image, automation, and messaging capabilities as independent modules.
- Keep model selection, retries, fallbacks, evidence, and degraded states behind one interface.

## Architecture in one line

`Open WebUI -> OpenAI-compatible gateway -> planner/router -> local models, coding agents, memory, knowledge, and policy-gated tools`

Local execution is the default. Cloud execution is an explicit data boundary, not a silent fallback.

## Technology

Locestra builds on mature upstream projects:

- [Open WebUI](https://docs.openwebui.com/) — unified interface;
- [Ollama](https://docs.ollama.com/) — local model serving;
- [Qwen Code](https://github.com/QwenLM/qwen-code) — local coding agent;
- [OpenAI Codex](https://github.com/openai/codex) — optional cloud coding path;
- [FastAPI](https://fastapi.tiangolo.com/) — gateway and module APIs;
- [Context7](https://github.com/upstash/context7) — current documentation retrieval;
- [Playwright](https://playwright.dev/) — browser inspection and UI QA;
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper) — local speech transcription;
- [ComfyUI](https://github.com/comfy-org/ComfyUI) — image workflows;
- [n8n](https://docs.n8n.io/) — automation;
- [Docker Desktop](https://docs.docker.com/desktop/) — isolation and managed services.

Third-party applications, containers, cloud services, and model weights retain their own licenses and terms.

## Reference setup

The current reference implementation targets Windows 11 with an NVIDIA GPU and Docker Desktop. Before running it, install Git, Docker, Node.js, `uv`, Ollama, Qwen Code, and optionally Codex CLI.

```powershell
git clone https://github.com/DenisGeide/locestra.git
cd locestra
Copy-Item .env.example .env
# Review .env before running any service.
./scripts/bootstrap.ps1
./scripts/start.ps1
./scripts/doctor.ps1
```

Stop the platform with:

```powershell
./scripts/stop.ps1
```

The bootstrap and lifecycle scripts are currently reference-workstation tooling, not a universal installer. Read [Operations](docs/OPERATIONS.md) and [Configuration](docs/CONFIGURATION.md) before use.

## Documentation

- [Current State](docs/CURRENT_STATE.md)
- [Roadmap](docs/ROADMAP.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Operations](docs/OPERATIONS.md)
- [Configuration](docs/CONFIGURATION.md)
- [Permissions](docs/PERMISSIONS.md)
- [Security Model](docs/SECURITY_MODEL.md)
- [Project Charter](docs/PROJECT_CHARTER.md)

## Security

Never commit `.env`, credentials, model files, databases, logs, runtime artifacts, or private repository content. This alpha release is intended for a trusted local workstation only. See [SECURITY.md](SECURITY.md) before installation or reporting a vulnerability.

## License

Locestra source code is licensed under the GNU Affero General Public License v3.0 only (`AGPL-3.0-only`). See [LICENSE](LICENSE).
