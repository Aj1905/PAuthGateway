# Changelog

All notable changes to PAuth Gateway are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); the project aims to
follow [Semantic Versioning](https://semver.org/). Before 1.0, minor releases may
include breaking changes.

## [Unreleased]

Working toward the first tagged release, `0.1.0`.

### Added
- OS egress lockdown script that forces a dedicated non-admin agent user's
  outbound traffic through the gateway host only, so a `curl`/subprocess bypass
  is dropped by the kernel rather than reaching an external service.
- File-backed audit trail (`--audit-log`, JSONL) and value-free health/status
  endpoints (`GET /health`, `GET /sessions/<id>`).
- Confirmation-gated sinks: an untrusted-derived control operand (recipient or
  amount) is held for user confirmation before it can reach a side-effecting
  tool.
- `auto` planner strategy — the deterministic recognizer with an LLM free-form
  fallback.
- OpenAPI 3.x provider that reflects a spec document into task-scoped tools.

### Security
- Fixed an SSRF / local-file-read in the OpenAPI provider: spec and reflected
  tool-call URLs are restricted to http(s), and link-local (cloud-metadata)
  hosts are refused.
- The side-channel denylist now also matches namespaced tools (e.g.
  `suite__bash`), so a merged-suite side channel cannot slip past the
  exact-name gate.
- Agent-facing denial feedback is value-free by construction: no operand value
  can re-enter the agent's model context as an injection payload.

### Changed
- Consolidated design documentation under `docs/`; the repository root keeps
  only the conventional meta files.
