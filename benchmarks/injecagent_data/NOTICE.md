# InjecAgent data (vendored subset)

Source: InjecAgent -- Benchmarking Indirect Prompt Injections in Tool-Integrated
LLM Agents (Zhan et al., ACL Findings 2024). https://github.com/uiuc-kang-lab/InjecAgent
Paper: https://arxiv.org/abs/2403.02691

`tools.json` is the full toolkit/return schema set. `cases_dh_sample.json` is a
diverse 30-case sample of the direct-harm test cases (the full 510 dh + ds sets
are in the upstream repo). Vendored to keep this eval offline and reproducible;
see benchmarks/injecagent_adapter.py.
