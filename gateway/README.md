# run() ゲート実験

このディレクトリは、既存の PAuth precision 実験および prompt-injection 実験とは
意図的に分離している。

この実験は、LLM が生成する権限記述言語 run() に対する保守的なフロントゲートを
検証する。このゲートは任意の自然言語理解を解こうとはしない。代わりに、小さく
決定的に認識できる部分集合に属するプロンプトだけを受理し、その部分集合から
canonical な run() を導出し、LLM の出力が canonical run() と完全一致する場合のみ
受理する。

これが zero false accept を狙う唯一の信頼できる方法である。その代償は false reject
の多さだ。曖昧なプロンプトは、人間なら妥当な意図を推測できる場合であっても拒否
される。

## 実行

オフラインの決定的 fixture translator:

```bash
.venv/bin/python run_gate_experiment/run_experiment.py --backend fixture
```

オプションの LLM translator:

```bash
.venv/bin/python run_gate_experiment/run_experiment.py --backend llm --model gpt-4.1-mini --temperature 0.2
```

このランナーは以下を行う:

- いくつかの自然言語タスクプロンプトを準備する;
- translator に run() JSON を生成させる;
- 決定的ゲートが結果を拒否した場合、OK になるか safety cap に達するまで同じ
  プロンプトをリトライする;
- 未対応または injection されたプロンプトが拒否されることを検証する;
- 受理された run() document を改変し、すべての改変が拒否されることを検証する;
- 受理された run() document を PAuth-style の `run()` コードに変換し、`pauth.prepare()`
  を実行して、受理されたケースが slice/rule 生成へ進めることを確認する。

## 解釈

これは、任意の NL-to-run() 変換を安全にできるという証明ではない。より狭い主張を
示すものである:

> ユーザープロンプトが決定的で監査可能な部分集合に属し、かつ run() 出力が verifier
> の導出した canonical run() と完全一致するなら、verifier はその部分集合内での
> 誤変換の受理を回避できる。

部分集合の外にあるものはすべて拒否される。
