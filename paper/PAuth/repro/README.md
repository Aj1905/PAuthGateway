# PAuth 論文 第 5 節 再現実験

参照論文: *PAuth — Precise Task-Scoped Authorization For Agents*
(Sharma, Jiang, Lin & Chen, arXiv:2603.17170 v1)。PDF は `../2603.17170v1-pauth.pdf`
(git 追跡外)。書誌は `../README.md`。

本リポジトリの `pauth`(GrammarValidator / Slicer / RuleCompiler / Enforcer /
EnvelopeStore)、`benchmarks`(AgentDojo 適合層と強制注入生成)、
`gateway` をモジュールとして使い、論文第 5 節の測定をやり直す。

## 使い方

キャッシュ済みの計画だけで回る(API キー不要):

```bash
.venv/bin/python paper/PAuth/repro/run.py
```

主なオプション:

```bash
.venv/bin/python paper/PAuth/repro/run.py --suites shopping banking --dsl g2 --out /tmp/repro
.venv/bin/python paper/PAuth/repro/run.py --allow-api --model gpt-4.1   # 計画を実際に生成する(課金される)
```

| オプション | 既定 | 意味 |
|---|---|---|
| `--suites` | 論文の 5 スイート | `banking slack workspace travel shopping` |
| `--dsl` | `g1` | DSL の版。`g1` が論文の DSL 相当、`g2` は本リポジトリの拡張版 |
| `--model` | `gpt-4.1` | Planner モデル(`--allow-api` のときだけ実際に呼ばれる) |
| `--allow-api` | 無効 | キャッシュに計画が無いタスクでモデルを呼ぶことを許可する |
| `--limit` | なし | スイートあたりの最大タスク数(動作確認用) |
| `--out` | `paper/PAuth/repro/results` | 出力先 |

出力:

| ファイル | 内容 |
|---|---|
| `report.md` | 表 1 / 表 2 / 図 9 / 図 10 に対応する再現結果と、論文との差分 |
| `results.json` | タスク単位の全測定値(機械可読) |
| `figure9_rules.csv` | 図 9 の元データ(タスクごとの三分類ルール数) |
| `figure9_rules.svg` | 図 9 相当の積み上げ棒(外部依存なしで生成) |

終了コードは、偽陰性(許可されてしまった強制注入)が 1 件でもあれば `1`。
偽陽性は終了コードに反映しない(過剰拒否は権限の漏れではないという論文の
立場に合わせる。数値は `report.md` に出る)。

## 何を測っているか

論文 図 6 の A1→A3・B1→B4 に対応する:

1. **Planner** — run コード(計画)を得る。キャッシュ済みの計画
   (`tests/experiment/cache/<suite>/<task>.py`)を優先し、
   shopping はスイート同梱の参照計画を使う。
2. **GrammarValidator / Slicer / RuleCompiler** — DSL 検査、スライス導出、
   ルール compile。ここは決定的で LLM を使わない。
3. **良性実行** — Enforcer 越しに run コードを実行する。1 つでも拒否された
   ツール呼び出しがあれば、そのタスクは偽陽性。
4. **強制注入** — 各注入ツール呼び出しを Enforcer に提示する。許可されたら
   偽陰性。

## 論文と一致しないところ(既知)

再現側の測定値そのものより、この節のほうが重要である。

1. **タスク数**。論文の表 1 は slack 19・shopping 5 だが、AgentDojo v1 の
   slack は 21 件あり、本リポジトリの shopping 実装は 2 件しかない。
   合計 100 タスクという分母は再現できない。
2. **強制注入の集合**。論文は「各ユーザータスクに合わせて設計した」634 件の
   注入を使う。この 634 件は公開されていない。本再現は
   `benchmarks/forced_injection.py` が機械生成したもの(オペランド改竄と
   他タスク由来の off-plan 呼び出し)で、集合が違う。**件数の多少は難易度の
   多少ではない**。off-plan ツール呼び出しは既定拒否だけで落ちるので、
   本当に執行器を試すのは「同じツール・改竄オペランド」の側である。
3. **被覆率**。論文は 100 タスク全件で正しい計画が得られたと報告する。
   本再現ではキャッシュ済みの計画が DSL に棄却されるタスクがあり、
   判定対象は 100 件に届かない。判定対象から外れたタスクは表 2 の分母にも
   入らないので、**偽陰性 0 は「論文と同じ範囲で 0」ではない**。
   `report.md` の「計画の入手状況」表を必ず併読すること。
4. **DSL の版**。論文の DSL は本リポジトリの版 ID で `g1` に相当する。
   キャッシュ済みの計画は `g2` 向けのプロンプトで生成されているため、
   `--dsl g1` では棄却が増える。どちらの版で回したかは `report.md` の
   冒頭に出る。
5. **図 9 の分類**。論文は「定数オペランド / 非定数オペランド / assert」の
   3 分類でルールを数えているが、数え方の定義は本文に無い。本再現の
   対応付け(`rules.py` の docstring)は解釈であり、論文側の実装と同じで
   ある保証はない。比較できるのは分布の桁までである。
6. **図 10 の USD**。測定できるのはトークン数で、USD は価格表を掛けた
   派生値にすぎない。gemini-3-flash-preview と claude-sonnet-4.5 の価格は
   本パッケージが持っていないので、USD は算出せず「未算出」と出す。
   価格を与えるなら `PAUTH_REPRO_PRICING='{"model": [入力, 出力]}'`。
   またキャッシュ済みの計画を使った場合、報告されるトークン数は**生成当時に
   記録された値**であって、再現実行そのものの費用ではない。

## 再現できないこと(設計上)

- **論文の 0 偽陽性 / 0 偽陰性そのものの検証**。注入集合が非公開で、
  しかも著者自身が設計したものなので、この数値は「著者が用意した注入を
  著者の実装が全部止めた」ことしか意味しない。本再現も同じ構造の限界を
  持つ。別の注入生成規則でも 0 が保たれるかは、`--suites` を替えるより
  `benchmarks/forced_injection.py` を替えて確かめるべき問題である。
- **論文の実装との一致**。本リポジトリは論文の記述からの再実装であり、
  著者のコードは公開されていない(`../README.md` の確認日時点)。
  一致するのは機構の設計であって、実装ではない。

## 関連

- リポジトリ側の常設評価: `eval/fpfn.py`(同じ測定をリポジトリ全スイートで
  行う)、`eval/check.py`(強制攻撃の統制検査)
- 用語と構成要素の定義: `docs/SYSTEM_MODEL.md`
- 評価指標の正本: `eval/metrics.py`
