# InjecAgent data (vendored)

Source: InjecAgent -- Benchmarking Indirect Prompt Injections in Tool-Integrated
LLM Agents (Zhan et al., ACL Findings 2024). https://github.com/uiuc-kang-lab/InjecAgent
Paper: https://arxiv.org/abs/2403.02691

- `tools.json` -- full toolkit / parameter / return-schema set (38 toolkits).
- `cases_dh_base.json` -- all 510 direct-harm base test cases.
- `cases_ds_base.json` -- all 544 data-stealing base test cases.

Trimmed to the fields the adapter needs. Vendored to keep this eval offline and
reproducible; see benchmarks/injecagent_adapter.py.
