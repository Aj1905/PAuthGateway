"""Shared test infrastructure and hand-labelled corpora.

The former self-made L1/L2/L3 dataset layers were removed (2026-08-02):
benchmark frameworks (AgentDojo etc.) supply task/reference data instead.

What remains here is NOT replaceable by those frameworks:

* ``mock_mcp_server.py`` / ``mock_mcp_stdio.py`` -- fake MCP servers (HTTP /
  stdio) used to test the MCP suite providers in
  ``gateway/providers/mcp_suite.py``.
* ``grill_cases.py`` -- labelled dangerous-flow corpus for ``eval/grill_eval.py``.
* ``filter_cases.py`` -- labelled suite-filter recall corpus for
  ``eval/filter_recall.py``.
* ``extract_real_prompts.py`` / ``real_external_prompts.jsonl`` -- real user
  prompts extracted from Claude Code transcripts (output is gitignored).
"""
