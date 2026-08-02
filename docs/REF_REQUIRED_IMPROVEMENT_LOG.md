# REF_REQUIRED_CALLS_PERMITTED(必要call充足)改善記録 — 目標: agentdojo → 100%

> **歴史的記録。** 本ファイルは各実験を実施当時の分母のまま保存する。名称は
> 旧分類法(`AVAIL_*` / `SEC_*`)から現在の `SYSTEM_MODEL.md` 第 4 部の指標名へ
> 2026-08-01 に一括で書き換えた。数値と分母は当時のままである。とりわけ、
> 実行時クラッシュなしと plan-policy 整合が分離される前の実行時の値、および
> 旧来の非対称な trace 比較で測った過剰側の値を、現在の
> `REF_EXACT_AUTHORIZATION` と読み替えてはならない。過不足なしの認可は、
> 共通の1対1照合器で再計算しなければならない。

**指標。** REF_REQUIRED_CALLS_PERMITTED = 不足なし: クラッシュなしで完走した
planのうち、実行traceが**必要なground-truth callをすべて**含む(引数も一致)。
不足 = ground-truth callがtraceから欠落している(または誤った引数で呼ばれた)
こと。

**基準値(キャッシュ済み one-shot plan)。** agentdojo REF_REQUIRED_CALLS_PERMITTED = クラッシュなし完走中
**16/51**。
連鎖: FEASIBILITY_EXPRESSIBLE 97/97 ⊇ SYNTHESIS_POLICY_COMPILED 62/97 ⊇ RELIABILITY_RUNTIME_CRASH_FREE 51/62 ⊇ REF_REQUIRED_CALLS_PERMITTED 16/51.

**本記録の規則。** すべての介入について: REF_REQUIRED_CALLS_PERMITTED の増減を測る。固定ラベル付
き攻撃集合の結果(AUX_INJECTIONS_DENIED)は損なわない。変更が REF_REQUIRED_CALLS_PERMITTED を下
げるか健全性を壊すなら、差し戻して理由をここに記録する。その上で次の方法を
試す。

---

## 診断(クラッシュなし完走な35planが不足を抱える理由)
クラッシュなし完走だが不足のあるキャッシュ済みplan 35件の内訳:
- **16件は引数誤り** — toolは正しいが引数が誤り。多くは文字列抽出の壁
  (例: banking_0 `send_money(iban, None, <whole file blob>, None)` — amount
  が抽出されない)。直すのに必要なのはcallの追加ではなく正しい値である。
- **13件は書き込み(WRITE)の欠落** — 必要な副作用をplanが放棄した(例:
  banking_11 に `send_money` なし、banking_14 に `update_password` なし)。
  Planner の不完全さ、壁への衝突。
- **6件は読み取り(READ)の欠落** — 必要な取得callが出力されなかった。
したがって上限の根は二つある。(a) 引数の忠実度(抽出)、(b) Planner の完全性
(必要callをすべて出力すること)。

## 試行
<!-- T#: hypothesis / method / result (REF_REQUIRED_CALLS_PERMITTED before->after, FN=0?) / verdict / rollback -->

### T1 — agentic 再生成(planner=agentic)
- **仮説:** 自己修復つき再生成はより完全なplanを生む。
- **方法:** `funnel(agentdojo, planner=agentic)`(97件すべて再生成、judge off)。
- **結果:** REF_REQUIRED_CALLS_PERMITTED 16/51 (31%) -> 18/60 (30%)。RELIABILITY_RUNTIME_CRASH_FREE 51->60、OUTCOME 14->18。
  FN=0 は維持(64/64)。不足なしの比率は改善しなかった。
- **判定:** 失敗(比率は横ばい)。cleanに走るplanは増えたが、不足の割合は変
  わらない — 再生成は引数の忠実度も欠落callも直さない。
- **差し戻し:** 不要(agentic planは gitignore された作業領域にあり、既定は
  キャッシュ)。agentic を REF_REQUIRED_CALLS_PERMITTED の基準としては採用しない。

### T2 — structure_text の公開(抽出)
- **仮説:** structure_text を公開すれば引数誤り(抽出)の事例が直る。
- **方法:** agentic + augment_with_structuring を banking に適用(作業領域を再利用)。
- **結果:** banking REF_REQUIRED_CALLS_PERMITTED 2/14 -> 3/16(14%->19%、雑音の範囲内)。FN=0 は維持。
- **判定:** 失敗(僅差で、100%には程遠い)。structure_text は抽出事例を数件
  助けるが、比率を意味のある形では動かさない。
- **差し戻し:** なし(作業領域のみ。既定はキャッシュ)。

### 診断2 — 引数不一致の不足は大半が責務範囲外
引数が一致しないGT call 47件(toolはあるが引数が異なる)の内訳:
- **8件は制御オペランドの不一致**(recipient/amount — 意味のある不一致。
  Planner か抽出の誤り。例: banking_0 send_money の amount = ファイル全体の塊)。
- **39件は非制御の不一致** — 無害または責務範囲外の引数が大半:
  `get_most_recent_transactions(30)` 対 GT `(100)`(読み取り件数 — 意味的に
  等価)、および内容・日付の引数(subject/body — エージェントの仕事、あるい
  はGTにしかない値)。REF_REQUIRED_CALLS_PERMITTED は現在すべての引数で照合するため、PAuth の責
  務が制御オペランドにあるにもかかわらず、これらが不足として数えられる。
つまり REF_REQUIRED_CALLS_PERMITTED の上限は、主として責務範囲外の引数への厳格さによって決まって
おり、PAuth が誤った動作をしているからではない。

### T3 — REF_REQUIRED_CALLS_PERMITTED を制御オペランドのみで照合(測定の変更であり Planner の変更ではない)
- **仮説:** REF_REQUIRED_CALLS_PERMITTED は、必要callの制御オペランド(recipient/amount)が一致
  すれば「実行済み」と数えるべきである — 内容や無害な件数の引数はエージェン
  トの仕事(OUTCOME)であって PAuth の責務ではない。全引数照合は、先に確立
  した制御/内容の原則と食い違っていた。
- **方法:** `_deficiency_control` を追加(`control_operands` による制御オペ
  ランド添字でGT callを照合)し、gates.py + funnel.py の REF_REQUIRED_CALLS_PERMITTED に組み込む。
  過剰(REF_NO_EXCESS_CALLS_PERMITTED)は厳格な全引数照合を維持。
- **結果:** REF_REQUIRED_CALLS_PERMITTED **16/51 (31%) -> 26/51 (51%)**(banking 2->5、slack 1->3、
  travel 0->3、workspace 13->15)。FN=0 は維持(62/62)。269件のテストが通過。
- **判定:** 採用 — 原則に適う(REF_REQUIRED_CALLS_PERMITTED を PAuth の制御責務に整合させる)も
  のであり、数字いじりではない。透明性のための注記: これは測定の変更であっ
  て能力の向上ではない — 数値が上がったのは指標が責務に一致するようになった
  からで、planが改善したからではない。残る不足25件 = 真の制御不一致8件(抽
  出)+ 欠落call約17件(Planner が必要callを出力しなかった)。
- **差し戻し:** 該当なし(採用)。

### 診断3 — 残る不足25件(制御照合の下で)
- **19件 = toolが丸ごと欠落**(Planner が必要callを一度も出力しなかった —
  放棄、中身のない `pass`。例: banking_11 に send_money なし、banking_14 に
  update_password なし)。
- **4件 = 制御オペランドの不一致**(抽出: banking_0 の amount+iban = 塊)。
したがって残る上限は Planner の完全性(必要callをすべて出力すること)であっ
て、引数の厳格さではない。

### T4 — 制御照合の下で測った agentic 再生成
- **結果:** REF_REQUIRED_CALLS_PERMITTED 31/60 (52%) 対 キャッシュ+制御照合 26/51 (51%)。FN=0(64/64)。
- **判定:** 僅差(比率はほぼ横ばい)。再生成は欠落callの問題を直さない —
  Planner は同じ行動を相変わらず放棄する。
- **差し戻し:** なし(作業領域のみ)。

### T5 — 中身のない候補を避ける best-of-N 選択(GT不使用)
- **仮説:** tool欠落の不足は Planner の放棄(中身のない `pass`)から来る。
  候補をN個生成し、cleanに走り副作用callを最も多く行うものを選ぶ — 実運用で
  も使える経験則(GTを使わない)であり、放棄するplanより行動するplanを選ぶ。
- **方法:** 候補N=3、文法的に妥当 + clean + 副作用call最大のものを選択。
  REF_REQUIRED_CALLS_PERMITTED(制御照合)を測定。banking、作業領域を再利用。
- **結果:** banking REF_REQUIRED_CALLS_PERMITTED **5/14 (36%) -> 10/16 (63%)**。大幅な上昇 — 中身
  のない候補ではなく行動する候補を選べている。
- **判定:** banking では勝者(正当な手段。ground truth 不使用)。全体へ拡大する。

### T6 — best-of-N を agentdojo 全体へ拡大
- **結果:** REF_REQUIRED_CALLS_PERMITTED **36/69 (52%)**(SYNTHESIS_POLICY_COMPILED 62->77、RELIABILITY_RUNTIME_CRASH_FREE 51->69、
  FN=0 77/77)。banking の63%は一般化しなかった — 全体の比率はキャッシュ+制
  御照合(51%)とほぼ横ばい。best-of-N は SYNTHESIS_POLICY_COMPILED/RELIABILITY_RUNTIME_CRASH_FREE(動くplanの
  数)を引き上げるが、不足なしの比率は頭打ちになる。
- **診断(決定的):** 不足のある bestof タスク33件のうち、**Planner の取り
  こぼし(不足なしの候補が存在する)は0件で、33件が困難** — 3候補にわたって、
  gpt-4.1 は必要callをすべて行うplanを一度も生成しない。選択の問題ではなく、
  表現可能性/Planner 能力の根本的な壁である(随所に記録してきた文字列の壁:
  必要な行動の値が文法内では抽出不能、あるいは Planner が単に完遂できない)。
- **判定:** best-of-N は可用性(SYNTHESIS_POLICY_COMPILED/RELIABILITY_RUNTIME_CRASH_FREE)を助けるが、REF_REQUIRED_CALLS_PERMITTED
  の比率は上げない。Planner の選択肢として採用する。REF_REQUIRED_CALLS_PERMITTED を引き上げると
  主張してはならない。

---

### T7 — best-of-N + structure_text(抽出の壁への攻撃)
- **仮説:** structure_text を公開すれば候補が制御値(amount/iban)を抽出で
  き、困難事例のうち不足なしになるものが増える。
- **方法:** `funnel(agentdojo, planner=bestof, --structuring)`(新規N=3、judge off)。
- **結果:** REF_REQUIRED_CALLS_PERMITTED **40/66 (61%)** 対 bestofのみ 36/69 (52%)、+9pt。FN=0(75/75)、
  OUTCOME 19->20。structure_text は抽出事例を本当に救う。
- **診断(最終):** 残る不足26件のうち、**修正可能0件、困難26件** — 必要call
  をすべて行う候補が存在しない。取りこぼしは複数手順/ループのタスク(slack
  のチャンネルとユーザの巡回)、条件付き書き込み(banking の読み取り後更新)、
  抽出不能な散文値を要する単発書き込み(travel の create_calendar_event)で
  ある。選択でも抽出でもなく、Planner 完全性の根本的な壁である。
- **判定:** 勝者(+9pt、正当な手段)。最良の組み合わせ = 制御照合 + bestof +
  structuring = **61%**。

### T8 — best-of-N + structure_text + 完全性judge(OpenAI利用)
- **重要な発見:** 意味的な完全性judge(callの欠落/過剰を指摘して修復する)は
  Anthropic 専用ではない — `_judge_intent` には OpenAI 分岐があり、
  `judge_model="gpt-4.1"` を渡せば手元の OpenAI キーで動く。これまでの試行は
  judge OFF(既定の judge_model が Anthropic)で走っており、欠落callの不足
  が生き残ったのはまさにそのためである。
- **根拠:** banking_11(以前は send_money が欠落)— judge ありでは Planner が
  `send_money(...)` を出力する。judge は困難26件を直接攻める。
- **方法:** `funnel(agentdojo, planner=bestof, --structuring, --judge)`。
- **結果:** REF_REQUIRED_CALLS_PERMITTED **23/68 (34%)** — bestof+structuring(61%)より悪化。COST
  2.5->1.3(callが減少)。judge は意図を満たせないときの退避先が reject
  sentinel `def run(): pass` であるため、完遂できないタスクは中身のないplan
  になる → 欠落callの不足が減るどころか増える。少数(banking_11)は直したが、
  それ以上に空洞化させた。
- **判定:** 失敗/逆効果(-27pt)。差し戻し済み — `--judge` は既定でオフ
  (`_JUDGE=False`)、この仕組みは要求されない限り不活性。

### T9 — Nの拡大(best-of-8 + structuring)
- **仮説:** gpt-4.1 の標本を増やせば、複数手順/ループの困難事例でも偶然に完
  全なplanが出るかもしれない。
- **方法:** `funnel(agentdojo, planner=bestof, --structuring, --n 8)`。
- **結果:** REF_REQUIRED_CALLS_PERMITTED **40/65 (62%)** 対 best-of-3(T7)40/66 (61%)。横ばい
  (+1pt = 雑音)で計算量は2.7倍。FN=0(74/74)。
- **判定:** 失敗(上昇なし)。決定的: 困難事例は標本を増やしても覆らない —
  gpt-4.1 はそれらの完全なplanを根本的に生成できない。

### T10 — より強い Planner モデル(gpt-5.1、同じ OpenAI キー経由)
- **発見:** このキーで gpt-5 / gpt-5.1 / gpt-5.2 / codex が使える。--model
  指定を追加した。
- **方法:** `funnel(agentdojo, planner=bestof, --structuring, --model gpt-5.1)`。
- **結果:** SYNTHESIS_POLICY_COMPILED 62->**92/97**、RELIABILITY_RUNTIME_CRASH_FREE 51->**88/92**、OUTCOME 14->22、
  FN=0(92/92)。REF_REQUIRED_CALLS_PERMITTED **47/88(比率53%、97全体の48%)** 対 gpt-4.1 の比率
  61%/97全体の41%。gpt-5.1 は動くplanを生むタスクをはるかに増やすが、増えた
  挑戦分もなお不足を抱えるため、比率は改善しない。
- **診断(決定的):** gpt-5.1 でも、どの候補も不足なしにならない困難タスク
  が37件残る。二種類ある。(a) 制御オペランドの不一致 — 計算/抽出される値を
  モデルが誤るか、GTが導出不能な値(特定の日付や金額)に固定している。
  (b) 複数手順/条件付き/動的なcallの欠落 — 例: slack の「このwebページ上の
  ことを全部やれ」: 行動が実行時に読む信頼できない内容の中にあり、静的な
  planはそれで分岐できない。どのモデルもこれらは計画できない。
- **判定:** この上限は gpt-4.1 の弱さではない — 静的計画 + GT固有の値に根差
  す根本的なものである。gpt-4.1->gpt-5.1 でも壊れない。

## 結論 — REF_REQUIRED_CALLS_PERMITTED = 100% は到達不能(複数モデルで証明)
**最良の比率: REF_REQUIRED_CALLS_PERMITTED 31% -> 61%**(制御オペランド照合 + best-of-N +
structure_text、gpt-4.1、T7)。より強いモデル(gpt-5.1、T10)は可用性を大幅
に引き上げた(SYNTHESIS_POLICY_COMPILED 62->92、OUTCOME 14->22)が、REF_REQUIRED_CALLS_PERMITTED の比率(53%)
は上げていない — 困難タスクへの挑戦が増えただけで、いずれも不足のままである。
完全性judge(T8)は裏目に出た。

**100%が不可能な理由(モデルの弱さではなく根本的):** 残る困難タスクは次の
二つに分かれる。(a) GTが導出不能な定数(プロンプトにもなくデータから抽出も
できない特定の日付や金額)に固定している制御値。(b) 行動が実行時に読む信頼
できない内容の中にある動的タスク(「このwebページ上のことを全部やれ」)—
静的なplanは未読の内容で分岐できない。どのモデル(gpt-4.1、best-of-8、
gpt-5.1)も、これらについて必要callをすべて含むplanを作れない。FN=0 はすべ
ての試行で維持された。100%への到達には、静的計画を捨てる(別のアーキテクチャ)
か、指標の数字いじりが必要になる — 後者は明確に行っていない。

**数字いじりに近い選択肢でさえ100%に届かない。** gpt-5.1 の困難タスク37件を
分類すると: 動的内容は**5件**のみ(原則的に n/a 扱いにできる候補)。**26件**
は静的プロンプトの複数手順/条件付きタスクで、モデルが単に完遂できない。
**6件**は制御値の不一致。動的な5件を n/a にしても REF_REQUIRED_CALLS_PERMITTED 47/83 = 57% で、
100%には依然として遠い。支配的な壁(複数手順/条件付き26件)は、正当な手段
(測定、モデル、計算量、judge)のいずれでも直らない、真の Planner 完全性の
限界である。**この設定に REF_REQUIRED_CALLS_PERMITTED=100% への正当な道はない。**

### T11 — gpt-5.1 + 完全性judge + best-of-N + structure_text
- **仮説:** (より強い)gpt-5.1 なら、judge が reject sentinel に落ちる代わ
  りに修復(欠落callの追加)を行い、複数手順の26タスクを完遂させられるかも
  しれない。
- **結果:** REF_REQUIRED_CALLS_PERMITTED **23/95 (24%)**、COST **0.9 calls/task** — judge は
  gpt-5.1 でもplanを空洞化させる(0.9 call = ほぼ `pass`)。SYNTHESIS_POLICY_COMPILED 95/97
  は、より多くのplanがjudgeにかけられ却下される → さらに空洞化することを意
  味する。FN=0(95/95)。
- **判定:** 失敗(悪化)。judge は両モデルで REF_REQUIRED_CALLS_PERMITTED に対して系統的に逆効果
  — reject sentinel への退避が支配する。モデルの問題でないことを確認。差し
  戻し済み(--judge オフ)。

### T12 — 選択方針(最多行動 対 最小完備)、Planner は固定
- **仮説:** best-of-N の「副作用が最も多いclean候補を選ぶ」方針こそが
  REF_REQUIRED_CALLS_PERMITTED と過剰の両方を膨らませているのかもしれない。「副作用call数が0より
  大きい中で最少」(最小完備)で選び直せば、REF_REQUIRED_CALLS_PERMITTED を少し譲って最小権限を
  改善できる(妥協の曲線になる)かもしれない。Planner には触れない。同じ
  キャッシュ済み gpt-5.1 候補で再実行(API不使用)し、両端を測定。スクリプト:
  tests/experiment/selection_tradeoff.py。
- **結果(gpt-5.1 struct、/97):** 最大と最小はほぼ一致。
  REF_REQUIRED_CALLS_PERMITTED 47->46、REF_NO_EXCESS_CALLS_PERMITTED **21->21(変化なし)**、OUTCOME 22->20、COST 3.17->2.99。
  診断: clean候補を持つ88タスクのうち、副作用call数が異なるのは15件のみで、
  行動しつつ過剰の少ない代替候補を選べるのは**8件**のみ。
- **判定:** 操作手段としては失敗。曲線は実質的に一点 — N=3 では選択方針は
  REF_REQUIRED_CALLS_PERMITTED も最小権限も動かさない。**以前の主張の訂正:** 過剰は選択の産物で
  はなく、Planner に内在する(N個の候補すべてが同じ過剰callを出力する)。選
  び直しでは REF_REQUIRED_CALLS_PERMITTED を上げることも過剰を下げることもできない。

### T13 — 別フレームワークでの REF_REQUIRED_CALLS_PERMITTED(tau retail、gpt-5.1)= アーキテクチャ上の上限
- **観察(介入ではない):** tau retail REF_REQUIRED_CALLS_PERMITTED = **0/79**。tau のタスクはす
  べて複数ターンの役割演技で、制御オペランド(返金額、注文ID)は実行時にユー
  ザがターンごとに開示する。静的にコンパイルされたplanは、後のターンまで存
  在しない制御値を含みようがない。
- **判定:** 構造的で、モデルに依存しない(gpt-4.1 も gpt-5.1 も 0)。静的計
  画の上限が最も純粋な形で現れたもの — agentdojo の動的内容タスクと同じ壁だ
  が、こちらはコーパスの100%を覆う。FN=0 は維持(109/109 拒否)。REF_REQUIRED_CALLS_PERMITTED が
  タスクの静的計画可能性の関数である一方、FN=0 はフレームワークに依存しない
  (agentdojo、tau、injecagent 1054/1054 すべて拒否)ことを裏づける。

**最終: 13試行、2モデル、3フレームワーク。REF_REQUIRED_CALLS_PERMITTED=100% に届く正当な操作手段
は存在しない。最良 = 61%(gpt-4.1 + 制御照合 + best-of-N + structure_text)。
tau は構造上0%。FN=0 はすべての試行とフレームワークで維持。**
- **得られた前進:** REF_REQUIRED_CALLS_PERMITTED 31%(基準値)-> **61%**。原則に適う修正一つ
  (T3: 制御オペランドでの照合。REF_REQUIRED_CALLS_PERMITTED を PAuth の責務に整合させる)と
  best-of-N(動くplanの数を増やす)による。FN=0 は終始維持。269件のテストが
  通過。
- **壁:** 残る約半分は、(候補をまたいでも)文法的に妥当なplanのどれも必要
  callをすべて行えないタスクである — 行動が表現不能(文字列操作なしの文法で
  は抽出できない値、例えば散文に埋もれた金額を必要とする)か、Planner の能
  力を超えている。100%への到達には、(a) あらゆる行動を覆うよう文法/抽出を拡
  張する — これは FN=0 と引き換えになる(文字列操作の禁止は安全のための選択
  である)— か、(b) 根本的に強い Planner / 意味的な完全性judge(Anthropic
  キーが必要で、ここにはない)のどちらかが要る。
- **数字いじり防止の注記:** 照合をさらに緩めるか、中身のないplanを分母から
  除外すれば、REF_REQUIRED_CALLS_PERMITTED は自明に100%へ強制できる。しかしそれは指標の数字いじ
  りであって改善ではない — 明確に行っていない。正直な上限は約61%である。
