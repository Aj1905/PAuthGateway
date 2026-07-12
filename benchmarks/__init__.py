"""Adapters that map external agent-security benchmarks into PAuth's neutral
:class:`~pauth.suites.base.SuiteSpec`.

This package is the single home for every third-party evaluation framework we
consume, so the eval scripts, tests and the gateway config all import *forward*
into here (never into ``tests/`` or ``eval/``). Each adapter reflects a
framework's tools, environments, tasks and injections into a ``SuiteSpec``; the
rest of the codebase stays framework-agnostic.

Current adapters:
* ``agentdojo_adapter`` -- AgentDojo (banking / slack / travel / workspace),
  paired with ``forced_injection`` for its injection generation.

Planned homes for the same seam (see docs / discussion): InjecAgent, tau-bench.
"""
