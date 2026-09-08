# T5 保存済み候補の診断（2026-09-08）

実装の改善はまだない。元の目標は GT_EXACT_AUTHORIZATION 60/97、
GT_NO_MISSING_CALLS 80/97 であり、今回の測定でも未達である。

## 測定条件と証拠

- 採点コードは lab の d15916a。AgentDojo 97課題、構造化読み取りあり。
- 入力は `tests/experiment/funnel_scratch/struct_p4_gpt-5_1_bestof_agentdojo_{banking,slack,travel,workspace}/{task}/cand{0,1,2}.py` の291本。
- 全候補の存在を事前確認。API鍵を環境から外し、生成関数も例外を返す関数へ置換した。新規生成なし、費用の見積・実測とも$0。
- 既存の `eval.funnel._bestof_plan` で選択し、`measure(..., "headless")` で再採点。課題別の指標と選択されたコードのSHA-256は `evidence-codex-t5-baseline.json`。
- 各候補を同じ `prepare`、`execute_generated_code`、`_reference_fidelity` で照合。候補別の不足数・過剰数・クラッシュとSHA-256は `evidence-codex-t5-candidates.json`。
- 正解を使う候補選択は診断専用。運用可能な選択器として採用していない。

## 結果

| 条件 | GT_EXACT_AUTHORIZATION | GT_NO_MISSING_CALLS |
|---|---:|---:|
| 現行の候補選択 | 41/97 | 49/97 |
| 正解を使った候補選択の上限 | 50/97 | 52/97 |
| 上記から未コンパイル・クラッシュ候補を除外 | 47/97 | 50/97 |
| 元の目標 | 60/97 | 80/97 |

後二行の選択上限は、指標ごとに最良候補を選んだ値であり、同一の候補選択方針が
両指標を同時に達成した結果ではない。クラッシュなしの照合上限も、人間確認や
実環境でのタスク完了を保証しない。

現行選択の GT_NO_EXCESS_CALLS は68/97、SYNTHESIS_POLICY_COMPILED は94/97、
RELIABILITY_RUNTIME_CRASH_FREE は88/97、OUTCOME_TASK_COMPLETED は21/97。
AUX_INJECTIONS_DENIED は94件通過、未コンパイル3件は対象外であり、97件通過とは書かない。

掲示板の旧値「41/47」は、T16の過不足なし41と、それ以前の不足なし47を混ぜていた。
同一系列の再採点値41/49へ訂正した。改善が起きたわけではない。

## 次の作業への帰結

候補選択だけの変更は、目標到達の主手段にならない。保存された候補集合自体に
必要な処理が欠けている。次は不足が全候補で残る課題について、
既存DSL(G2)で表せる処理の生成失敗と、表現できない処理を区別する。
前者には汎用の生成・修復指示、後者にはDSLの設計変更が必要になる。
正解の固定値や課題名を生成規則へ埋め込むこと、採点条件を変えて改善に見せることはしない。
