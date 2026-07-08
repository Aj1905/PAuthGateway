# solution log

`plan.md` の 💬議論 項目を実装中に解決した記録。決定ごとに「決定・根拠・影響範囲・残課題」を書く。
🔬研究 項目は原則ここでは解決しない(解決を試みた場合はその範囲を明記する)。

書式: S番号は本書内の通し番号。plan.md の ID (B2 等) と相互参照する。設計対話の記録
(Q系列, 旧 grill.md)は本書末尾に統合済み(Q15-e 等の Q番号はそこを参照)。

---

## S1. Q15-e — 機械的検査の前段化の範囲(Stage 2 💬)

**決定:** LLM judge の手前に、以下 4 種の決定的 precheck を置く。実装は
`gateway/planning/prechecks.py`。

1. **明示禁止 tool** — `PrecheckPolicy.forbidden_tools`(denylist)に載る tool の呼び出しを拒否。
2. **宛先追加(recipient entailment)** — recipient 系パラメータ(パラメータ名トークンが
   recipient / iban / account / email / address / destination / target / channel / to)へ渡される
   文字列定数、および コード中の任意の位置に現れる IBAN / email パターンの文字列定数は、
   ユーザプロンプト内に出現しなければならない(大文字小文字・IBAN の空白は正規化)。
3. **金額(amount entailment)** — amount 系パラメータへ渡される数値定数はプロンプト中の
   数値トークン(数詞 one〜twelve を含む)として出現しなければならない。quantity 系は
   プロンプト出現 or `1`(「buy one」の暗黙既定)を許容。**変数渡し(cart.total 等)は対象外**
   (データフローは下流の A2/A3 + enforcer が担保)。date 系は v1 対象外(Q15 の operand
   リストに含まれず、危険度が低い)。
4. **read→write 拡張(任意)** — `PrecheckPolicy.write_tool_evidence`(tool → 根拠キーワード列)
   が宣言された tool は、プロンプトにキーワードのいずれかが出現しない限り呼び出し不可。
   キーワード表はポリシーデータであり、コアロジックには自然言語解釈を持ち込まない。

**強制点は 2 箇所:**

- `generate_code_with_self_repair` の retry loop 内(grammar 検査直後・judge の前)。
  違反は決定的な反例として修復指示に載せる。LLM 呼び出し不要なので judge より安価。
- `Gateway._accept_draft` の**ハードゲート**。プランナーの種類・キャッシュの有無に関わらず、
  受理直前に必ず走る。壊れた/古いプランナーやキャッシュ済みコードも構造的に弾く。

**根拠:** one-sided safety(grill Q15, 2026-06-09 方針)では「prompt から正当化できない
過剰認可の accept」だけが security failure で、over-rejection は retry で回復可能な
UX 問題。上記 4 種は誤検出しても over-rejection 側に倒れるため、決定的に前段化して
安全側の損しかない。judge の確率性(Q15-a)への依存を最小化する。

**影響:** fixture `C1_missing_iban` の `expected_accept` を True→False に反転した。
「usual account への支払い」で LLM が IBAN を捏造するケースは、Q15 の doctrine
(prompt にない recipient の許可 = 過剰認可)そのものであり、従来の True は doctrine
制定前の測定値の追認だった。捏造宛先の受理は本プロジェクトの最重要 failure である。

**残課題:** 宛先マーカー/キーワード表の網羅性は suite が増えると再訪。危険フロー
(untrusted-source → sink)の意味論は Stage 3 の 🔬研究 のまま。

## S2. freeform planner の主軸昇格の方式(Stage 2)

**決定:** 新戦略 `auto` を追加し、ingress の既定戦略にする。
`auto` = deterministic recognizer を fast path として先に試し、不一致なら
`llm-freeform`(suite 設定時)へフォールバック。recognizer が返した suite を
suite_loader が解決できない場合も freeform へフォールバックする。

**根拠:** recognizer は 4 正規表現で狭い(E2)が、一致した場合はゼロコスト・決定的で
最強の保証を持つ。捨てる理由がない。「主軸=freeform」の意味は「任意プロンプトが
入ってきたときに落とさず freeform 経路に乗ること」であり、fast path の温存と矛盾しない。

**影響:** `PAUTH_PLANNER_SUITE` 未設定時の `auto` は従来の deterministic と同じ受理集合
(フォールバック先が無い旨のエラーになるだけ)。よって既存デプロイの挙動は
freeform を明示的に設定しない限り変わらない。

## S3. semantic judge の多プロバイダ対応(Q15-a の前提整備)

**決定:** judge のモデル指定でプロバイダを自動判別する(`claude-*` → Anthropic API、
それ以外 → OpenAI API)。既定は従来どおり `claude-opus-4-8`(生成側 gpt-4.1 と
プロバイダごと分離、Q15-a の相関切り)。

**根拠:** 本開発環境に ANTHROPIC_API_KEY が無く(`me.env` 不在)、judge が
fail-closed で全 reject になり計測が回らない。OpenAI 系 judge(例 gpt-5-mini)は
プロバイダ内の別モデルとして相関切りが弱いが、「judge なし」よりは強い。
アンサンブル化(Q15-a 本体)は 🔬研究 のまま残す。

## S4. credential broker を採用するか(Stage 1 前提, B2 💬)

**決定:** **採用する。** gateway が SaaS credential を保持し、tool 実行も gateway が行う。

**根拠:** 現アーキテクチャの不変条件は「gateway が唯一の観測権威であり、tool を
自ら実行して署名済み envelope を記録する」(L3 保護)。実行主体が credential を
持たない構成(現 architecture.md §9「鍵を見ない」)では、実行が MCP shim 側に残り、
enforcement 点と実行点が分離して L3 の主張が崩れる。鍵集積点になるリスクは
self-host(ユーザ自身のマシン/VPC で動かす)を Stage 1 の前提とすることで受容する。

**影響:** `architecture.md` §9 の「鍵を見ない」→「鍵を持つ」書き換えが必要(plan.md
Stage 1 に既存項目あり)。broker 実装(保管・rotation・隔離)は最初の実 SaaS 統合と
同時に着手する。

**残課題:** 保管方式(OS keychain / file + permissions / Secrets Manager)は
self-host 手順の実装時に決定。

## S5. Stage 1 の最初の実ツール(💬 ブロッカー)

**決定:** **GitHub**(REST API を OpenAPI reflection で取り込む。公式 MCP server が
使える環境では MCP(HTTP) アダプタでも同一 SuiteSpec に正規化できる)。

**根拠:**
- personal access token で即日試せる(組織管理者権限・決済不要)。scope を絞った
  fine-grained PAT が credential broker の隔離テストに適する。
- read(issue 一覧・repo 読み取り)と write(issue 作成・コメント)の境界が明瞭で、
  read→write precheck・free-operand 3 段ポリシーの実地検証に向く。
- issue 本文 = 典型的な untrusted data source であり、injection 実験(external data →
  tool 引数改ざん)が現実的なシナリオで組める。
- 対抗候補 Slack は workspace 管理と app 審査の摩擦が大きい。銀行系実 API は入手不能。

**残課題:** GitHub API の巨大な tool surface は suite_filter(D1)前提。最初は
issues 系 5〜10 tool に絞った SuiteSpec を手で切り出す。

## S6. 側チャネル(生 Bash 等)の scope 宣言(Stage 1, B5 💬)

**決定:** Stage 1 は **禁止前提**(エージェントに生 Bash / 直接ネットワーク I/O を
与えないデプロイのみを保護対象として主張する)。別機構(allowlist / sandbox /
FS 仮想化)は Stage 6(Mode 2)の議題として持ち越す。

**根拠:** threat-model.md が既に側チャネルを scope 外と明記している。Stage 1 の主張
「乗っ取られたエージェントは承認計画の構造を超える SaaS 実行ができない」は
「外向き I/O が gateway 経由の tool call のみ」という前提でのみ成立する。この前提を
満たさないデプロイを黙って L3 と呼ぶことが最大の虚偽表示リスクであり、機構追加
より先に前提の明文化が必要(DESIGN_STATUS「保護レベルの正直表示」と同旨)。

**影響:** self-hosting.md / hooks README に「保護の前提条件」として明記する
(→ 完了 2026-07-08: SELF_HOSTING「Egress Lockdown」/ hooks README 4b。前提を egress
lockdown で機構化, Q10)。

## S7. メトリクス命名の確定(Q15-d)

**決定:** 以後、二層で別名計測する。

- **plan 層**(A1+judge+precheck): `over-authorization accept`(expected_accept=False の
  prompt を受理、または受理コードが must_not_call を呼ぶ)/ `over-rejection`
  (expected_accept=True の prompt を拒否)。
- **runtime 層**(B1–B4): 従来の FN = `over-authorization accept`(forced injection の
  permit)、従来の FP = `over-rejection`(benign call の deny)。

レポート出力では旧名 FP/FN を併記する(論文 Table 2 との対応を保つため)。
ユーザ要求の「FP 0」は本書の用語では **over-authorization accept = 0** を指す
(over-rejection は retry loop で回復可能なので当面許容)。

## S8. 空プラン(sentinel)の受理境界での拒否

**問題:** validator を最後まで通らなかったとき agentic A1 は `def run(): pass` sentinel を
返し、「gateway が綺麗に reject する」契約だった。しかし空プランは grammar 適合で
`prepare()` が成功し(rules=0)、`Gateway` は **accepted** を返していた。runtime は
全 call default-deny なので実害はないが、plan 層の verdict が嘘をつく
(C1_missing_iban が ACCEPT と報告されていた)。

**決定:** `Gateway._accept_draft` で `prepared.rules` が空のプランを
「plan authorizes no tool calls; rejected (default-deny)」として拒否する。
authorize する対象が無いプランの受理は無意味であり、sentinel 契約もこれで成立する。

**検証:** canonical freeform 6 prompts で over-authorization accept = 0 /
over-rejection = 0(B1 は grammar 表現力の限界による正しい reject、C1 は本修正で
正しく reject)。AI 生成 8 prompts でも over-auth accept = 0。

## S9. 計測で得た知見(2026-07-05)

- **precheck が judge の見逃しを実際に捕捉した:** `ai_free_no_constants` のキャッシュ済み
  コードはプレースホルダ宛先 `"recipient_iban"` を含み、Claude judge は PASS させていた。
  Q15-e ハードゲートが「recipient-like constant が prompt に無い」として決定的に reject。
  Q15-e 前段化(S1)の設計意図がそのまま実証された形。
- **OpenAI 系 judge の temperature:** gpt-5 系は temperature=0 を拒否する。judge 呼び出しは
  provider を問わず temperature を送らない(fail-closed のせいで全タスク sentinel 化する
  事故がスモークで発生 → 修正済み。judge エラーの fail-closed 挙動自体は正しく機能した)。
- **AgentDojo agentic 計測(確定値):** 4 suite 97 タスク、gpt-4.1 生成 ＋ precheck ＋
  gpt-5-mini judge、実費 $3.02。
  **over-authorization accept = 0 / 156 injection runs、runtime over-rejection = 0 / 18**
  (Stage 2 exit 達成)。ただし受理は 18/97(18.6%): plan-deny 55(judge 差し戻し→sentinel)、
  grammar skip 24(内訳: 多重代入 10 / ネストif・else 8 / ループ 2 / その他 4)、
  code-crash 3(生成コードの型エラー、PAuth エラーではない・安全側)。
  FP0 は保守側に倒して成立しており、受理率が次の主戦場(→ S10)。

## S10. Stage 4 方式の方向決定: プロンプト事前分解(2026-07-05)

**決定(ユーザ合意済み):** 文法拡張クラスの主軸として**事前分解方式**を採用する。
1 タスクを Appendix A 適合のサブプラン列 `[(guard_1, code_1), ..., (guard_n, code_n)]` に
分解し、guard は Appendix A の `<Condition>` 文法を再利用して **gateway が署名済み
envelope に対して決定的に評価**する。Appendix A 自体には触れないため、各サブプラン
内部は論文の保証をそのまま流用できる。

**鍵の観察:** 拒否ログのネスト if / else の多くは逐次依存(「成功したら次へ」)の
エンコードであり、ステージ境界＋guard に直列化できる。

**再証明が必要な性質(合成層のみ・テストで機械検証可能な範囲):**
1. 不活性性 — stage k のルールは guard_k が true 評価されるまで一切許可しない
2. 非累積性 — stage 遷移後、前 stage の消費済みルールは再活性しない

**限界(見積り):** ループ必須クラス(grammar skip 24 中 2 件 + plan-deny 内の巡回系)は
分解では救えない。多重代入 10 件は SSA 風リネームで分解と独立に救える可能性あり。

**残る研究課題:** stage 完了判定の意味論(エージェント申告は信用不可)、分解器の
忠実性(one-sided validator で安全側には倒せる)、guard 言語の肥大化抑止(規律:
`<Condition>` 固定)。

**次の一歩:** composite runtime ＋ 手書き参照分解で `B1_cheapest_under_80` を
オフラインで通す垂直スライス → 不活性/非累積の敵対的テスト → LLM 分解器 →
AgentDojo 拒否タスクの救済率計測。

## S11. 有界 fan-out ＋ 合成プランランタイム(実装済み, 2026-07-05)

**決定(ユーザ提案を修正の上採用):** ループは有界展開(bounded unrolling)で扱う。
ただし (1) 展開は LLM でなく gateway が機械的に行う(分解器はテンプレート1個のみ出力)、
(2) N は予想でなく**観測**(`N = min(len(観測リスト), N_max)`、リスト長は gateway 自身の
署名済み envelope から読む)、(3) **自動継続は禁止** — N_max 超過分は truncated として
報告し、継続はユーザ確認(Stage 5 grill)に接続する。N_max は認可ロジックではなく
被害半径の蓋(ポリシー定数)。

**実装:** `gateway/planning/composite.py`(型・guard 評価器・機械的 instantiate・
検証ゲート)+ `gateway/runtime/gateway.py`(`submit_user_prompt_composite`、stage
活性管理)。設計上の要点:

- guard 言語 = Appendix A `<Condition>` 固定(and/or・関係演算・定数添字・`len` のみ)。
  評価は gateway が bindings(自身の観測から導出した変数束縛)に対して行う
- stage 境界の**部分評価**: fan-out body の `products[i].field` を署名済み観測の定数に
  畳み込んでから、無改造の `pauth.prepare()` に通す。よって instance の rules は定数
  operand を持つ最タイトな形になる
- stage 遷移: guard 付き stage は guard true で前進(前 stage の残権限は失効)、
  guard 無し stage は現 stage 全 rule 消費で前進。後退は構造的に不可能
- **one-shot 消費**: flat enforcer は同一呼び出しの厳密リプレイを何度でも再認可する
  (rule は消費されない)ことを実装中に確認。composite 層では rule 消費を追跡し
  リプレイを拒否する(flat 経路より強い)
- v1 制約: guard が参照できるのは「その stage 内で当該 tool が一意に1回呼ばれる」
  代入変数のみ(束縛の曖昧さ排除)。sequential stage の code は自己完結
  (cross-stage 変数は guard と fan-out list のみ)

**検証(tests/test_composite.py, 14件・オフライン):** B1_cheapest の参照分解が
end-to-end で通る(Stage 2 で reject されていた意図クラスの救済を実証)。敵対的
テスト: 不活性性(guard 前の send_money 拒否)・非累積性(前進後の stage 1 リプレイ
拒否)・one-shot・off-list fan-out 拒否・N_max cap ＋ truncated 報告・guard false で
支払い stage が永久に閉じる・SPAM 宛先テンプレートの precheck 拒否。

**未了(次段):** LLM 分解器(プロンプト→CompositePlan を出す planner 戦略)、
AgentDojo 拒否タスクの意図クラス分類と救済率計測、集約型ループ(gather stage)の設計。

## S12. グリルフェーズのスコープと UX 原則(Stage 5)

**スコープ決定(ユーザ確定):**
- **非対象:** free operand の「値の真偽」判定。人間がエージェント無しで作業する場合でも
  負っていたはずの判断(汚染された請求書を信じてしまう等)は、エージェント経由でも
  人間の責任のまま。ここはシステムが解こうとしない。「全部を守るふり」を避け、
  グリル一箇所に人間の注意を集中させる境界として明示的に受容する。
- **対象:** グリルフェーズで人間が**良い判断を下しやすい聞き方**をすること(UX)。

**設計目標の再定義:** グリルの人間を「エージェント無しで注意深く作業する人間」と
**同等以上の情報位置・判断粒度**に置く。下回る箇所(束ね fan-out / 書き下流の目隠し /
グリル層への注入 Q14-c)だけがエージェントが新たに作った不利であり、そこを実装で
ごまかさない。同等なら「多少難しくても人間がやる」を許容する。

**中心原則:** 人間の注意は希少資源。**独立で・検証可能で・危険な判断だけ**に使い、
答えは毎回「再利用できる制約」に変えて二度と同じことを聞かない。

**罠(明示):** 「1 アクション = 1 質問」は疲労経由の盲判子に落ちる(束ねすぎ=rubber-stamp、
分けすぎ=fatigue、出口は同じ「考えずに許可」)。正しい粒度は**アクション数ではなく
独立したリスクの数**。同質なものは束ね、独立リスクは分ける。

**UX パターン(plan.md Stage 5 の部品の組み立て方):**
1. 例外だけ見せる(既知・許可リスト内は自動通過、新規/外部由来だけレビュー)
2. 出所・理由でグループ化(同一 provenance = 1 グループ 1 判断)
3. 答えを永続制約に昇格(「今後も許可?」→ 許可リスト追加 → 二度と聞かない。100問問題が縮む)
4. 安全側デフォルト(連打時は deny/制限側に倒れる。fail-closed)
5. 束ねの上限を可視化(N_max のUX版。境界超えは別判断)

既存部品との対応: G2(半対話)＋ Q14-a(信頼スコアで同質・既知を自動通過)＋ provenance
表示 ＋ サリエンス分離。

**残る依存:** どのフローを危険とみなすか(＝何についてグリルするか)は Stage 3 の
危険フロー検出が前提。グリル前倒しは人間判断の**置き場所**を早めるだけで、検出は
依然として必要。

## S13. 独立リスクの単位の機械判定(グリルのグルーピング根拠)

**問題:** グリルに「何を1判断として束ね、何を分けるか」を決める決定的な関数が要る。
これが決まればグルーピングは自動化される。

**判定入力(すべてプラン時 or stage 境界で静的に得られる):** 各副作用呼び出し site に
ついて、次の 3 属性の組で**リスクキー**を作る。

1. **provenance(出所)** — その operand が信頼できるソース由来か、untrusted ソース由来か。
   スライスの依存グラフから静的に導出(合成プランの stage 割りで既に持っている情報)。
   untrusted の場合はソース識別子(どの read から流れたか)も含める。
2. **novelty(新規性)** — 宛先/操作対象が、過去実績(履歴ストア)に存在するか。
3. **allowlist membership** — 宛先/操作対象が、ユーザの許可リスト(連絡先・既知 IBAN 等)に
   載っているか。

**決定規則(ドラフト):**
- allowlist に載る → **自動通過**(グリル不要)。人間の注意を使わない。
- allowlist 外だが trusted-provenance かつ既知(novelty=false) → **束ねて 1 判断**
  (「既知の宛先 N 件」)。
- untrusted-provenance または新規(novelty=true) → **独立リスク**。同一リスクキー
  (同一ソース×同一新規性)ごとに 1 グループ 1 判断。異なるリスクキーは分ける。

**リスクキー = (provenance_source, is_untrusted, is_novel, in_allowlist)**。同一キーの
呼び出し site を 1 グループに束ね、キーが違えば別グリル質問にする。これで
「同報 100 件(同一キー)= 1 問」「バラバラ 100 件(多キー)= キー数の問」が自動的に出る。

**one-sided 維持:** 判定が曖昧なとき(provenance 不明・履歴なし)は**独立リスク側**に
倒す(束ねない=より多く人間に見せる)。安全側は over-grill(過剰確認)であり、
over-authorization ではない。

**実装の足場:** provenance はスライス由来で合成プランが既に保持。novelty/allowlist は
新規の永続ストア(履歴・許可リスト)が要る — これは横断項目の session 永続化(B1)と
同じ外部化の一部として設計する。許可リストへの昇格(S12 パターン3)がこのストアに書く。

**未了:** 履歴/許可リストストアのスキーマ、provenance ソース識別子の粒度(read tool 単位か
フィールド単位か)、risk key の同値判定の厳密化。次段の実装対象。

## S14. precheck カバレッジ欠落の穴埋め(FP面 #3, 2026-07-05)

**背景:** FP を導きうる面の棚卸しで、precheck の宛先/金額判定が**パラメータ名トークン
一致のみ**だった欠落を特定。`RECIPIENT_TOKENS` に "user"/"member"/"recipients"(複数形)が
無く、例えば slack の `invite_user_to_slack(user)`(名前 "user")や
`send_email(recipients)`(複数形)へ渡す捏造宛先が素通りしていた(IBAN/email 形の
グローバルscan だけが最後の砦だが、プレーンなユーザ名は捕捉できなかった)。

**対応:** 判定を **名前トークン ＋ 説明文キーワード ＋ ポリシー明示宣言**の複合に強化
(`_classify_param`)。
- 名前トークン拡張: recipients/payee/sender/receiver/contact/phone/mailbox 等を追加。
- 説明文キーワード: `RECIPIENT_DESC_SUBSTRINGS`(高精度句のみ: "recipient", "iban",
  "email address", "phone number", "the user to ", "share the file with" 等)。
  slack の `user: "The user to invite."` を「the user to 」で捕捉。読み取りの
  `user: "The user whose inbox to read."` は「the user whose」なので非該当 → 読みの
  over-rejection を避ける精度設計。金額は説明文 "amount" で判定(読みの "price ceiling" は非該当)。
- ポリシー宣言: `PrecheckPolicy.recipient_params` / `amount_params` で (tool, param) を
  明示指定でき、命名が特殊な suite を決定的に精密化できる(ヒューリスティックの上書き)。

**検証:** 新規テスト7件(`tests/test_prechecks.py`)。slack `invite_user_to_slack("Mallory")`
の捕捉、読み取り `read_inbox` の非該当、ポリシー宣言による上書き、読み価格フィルタの
非該当を確認。全52テスト通過。AgentDojo agentic 再計測(cached)で
**over-authorization = 0 / 156 を維持、受理数 18 も不変**(既存受理を壊さず gap のみ閉鎖)。

## FP を導きうる面の現状(棚卸し, 2026-07-05)

| 面 | 種別 | 状態 |
|---|---|---|
| 実行時 enforcer(paper コア) | 決定的 | ✅ FN=0/156 実測、導かない |
| #1 汚染データフロー(untrusted観測→sink) | 構造的 | 🟡 設計確定・実装未了。closure=grill 機構(S15/S17)。別建て検出は不要 |
| #2 judge false-pass(非定数の過剰認可クラス) | 確率的 | 🟡 F1 継続。別モデル化済み、アンサンブル未 |
| #3 precheck カバレッジ欠落 | 決定的 | 🟢 S14 で穴埋め(名前＋説明文＋ポリシー宣言) |
| #4 合成ランタイム実装リスク | 新規コード | 🟡 14 テストで検証、形式証明なし |
| agent フィードバック汚染(注入反射) | 決定的 | 🟢 S16 で構造的排除(値ゼロ・型強制)、実装済み |

**結論(S17 で更新):** #1 の closure は「別建ての危険フロー検出エンジン」ではなく
**grill 機構**(2フェーズ＋各 sink のオペランド確定地点での人間確認)と確定した。設計は
固まり、実装が未了。検出(trust ラベル/テイント)は正確さの要件でなく grill 選別の
最適化(規模対策)。「任意プロンプトで FP0」を主張するには #1 の grill 機構(Stage 5)の
実装が要り、それまでは主張範囲を「trusted ソース＋ precheck が覆う定数系＋実行時強制」
に限定する。

## S15. 汚染データフロー(#1)を grill フェーズで閉じる設計(2026-07-07)

FP を導きうる唯一の構造的な穴 #1(untrusted 観測 → sink)を、grill を軸に閉じる設計。
議論で到達した方針を記録する(実装は未了、Stage 3/5 の具体化)。

### 前提: 2フェーズの背骨(予想でなく観測を確認する)

順序を「捕捉 → 計画を1つ確定 → **読みスライスを先行実行**(副作用ゼロの読みのみ)→
観測した実値＋出所で grill → 凍結定数で書き込みを強制」に組む。危険な operand は
untrusted な**読み**由来で、読みを先に実行すれば実値that見えるので、予想の弱さ(値を
当てられない)は問題にならない。動的テイントで出所を追える(実行後なので静的解析より
容易)。

### 動かせない事実

書き込みの**後**に初めて生まれる汚染データ(投稿への返信等)は、grill 時点で存在しない。
**存在しない値は事前確認できない。** よって「事前一回で全部」は本物のケースには原理的に
不可能。閉じ方は単一機構でなく決定手続きになる。

### 主軸: 中身/制御オペランドの分離(決定的・大半を grill 無しで処理)

決定的ルールを1本引く:

> 書き込み後の未確認・untrusted 読みのデータは、**中身(content: 本文・説明文・
> メッセージ本体)オペランドにのみ流してよい。制御(control: 宛先・金額・ツール選択)
> オペランドに流れる場合はブロック。**

汚染that中身に入るだけなら、宛先=制御that確認済みゆえ被害は有界(「安全な宛先に汚い
中身that届く」)。事前 grill で「中身の中継可否」を一度確認しておけば受容できる。これは
flow-constrained free(E1)の具体形で、動的テイントで決定的に判定できる。

### 残る核(汚染that制御を握るタスク)への3手

「返信that『口座Yに送れ』と言ったら送る」型は、上記でブロックされ、かつ値that存在
しないので事前確認不能。取れる手:

1. **境界での最小再 grill(選択的 interleave)** — 書き込み後の untrusted 読みを合成
   プランの新 stage にし、活性時に観測した実値で grill を1回だけ回す。全 operand では
   なく危険な境界1点のみ。境界数=「自分that引き起こした要求の結果に制御を握らせて
   動く」回数(普通 0、掲示板例 1)。
2. **タスク分割/ユーザーへ差し戻し(対話的用途向け, S15 追補)** — 確定した安全部分
   (タスクA)を実行後に強制停止し、汚染読みを次のタスクBの先頭に持ってくる。判断点は
   消えず「タスクBの事前 grill」に**移動・包み直し**するだけ(根本解決ではない)。
   利点は (a) 全タスクthat同一の事前2フェーズに揃う一様性(途中で止まらない)、
   (b) plan-once 不変条件をより忠実に保つ(各タスクthat正真正銘一度だけ計画)、
   (c) タスクA結果を見たユーザーの**再プロンプトそのものthat確認**になり専用 grill 機構が
   要らない。**対話的用途に向き、無人用途には不向き**(タスクB起動に人間の再関与that
   要り、自律完了できない)。手1との交換関係: 「途中 grill 機構」と「セグメント間の
   人間の再関与」のトレードオフ。
3. **deny(無人用途の退避)** — 制約不能・無人なら安全側に倒す(plan.md Stage 5)。

### 決定手続き(まとめ)

1. 実行してデータフローを追い、「書き込み後 untrusted 読み → sink」の境界を動的テイント
   で検出(検出の手間は手1/手2で共通、省けない)。
2. 汚染that**中身**にしか流れない → 中身/制御ルールで決定的に通す(対話・無人どちらも可)。
3. 汚染that**制御**に流れる → 対話的なら手1(境界再 grill)or 手2(タスク分割)、無人なら手3(deny)。

**帰結:** #1 は「事前一回」には閉じない(存在しない値は確認できない壁that絶対)。だが
中身/制御分離で大半を決定的に片付け、残る核だけを境界1点の再 grill/タスク分割/deny に
縮められる。到達可能な下限であり、既存の合成プラン機構(stage＋凍結＋テイント)で
実装できる。棚卸し表の #1 は、Stage 3=「deny する検出器」でなく「grill に上げる検出器」
と読み替える。

## S16. agent 向けフィードバックの構造的無害化(実装済み, 2026-07-07)

**問題:** deny 時にゲートウェイthat agent へ返す理由文は、agent のモデルコンテキストに
入る。ここに汚染された operand 値(注入メール由来の宛先等)を引用すると、それthat
「信頼された」フィードバックとして再注入される(前ターンの穴1)。

**方針: 無害化(フィルタ)でなく構造的排除。** フィルタは blocklist で未知の注入形を
取りこぼし確実にならない。代わりに、agent 向け理由文を**信頼できるトークンだけ**から
組み立て、値を渡せる経路を型で塞ぐ。

**実装(`gateway/runtime/feedback.py`):**
- `ReasonCode`(閉じた列挙)＋ 固定テンプレート表。テンプレートは値を一切埋め込まない。
- `build_agent_feedback(code, *, tool, param_index)` — **operand 値を受け取る引数that
  存在しない**。ツール名(検証済み識別子)と引数位置(整数)だけ。だから構造的に汚染
  バイトthat出力に入り得ない。不正な tool 識別子は placeholder に置換(防御的)。
- `validate_identifier` / `assert_safe_suite` — 登録時に tool/param 名を安全な文字集合・
  長さ上限で検証(untrusted OpenAPI spec that名前に注入を仕込む二次経路を塞ぐ)。
  `Gateway._accept_draft` で suite 受理時に強制。
- `classify_reason` — enforcer の自由文理由を ReasonCode に写像(値は捨てる。誤分類は
  別の安全テンプレートを選ぶだけで、値を漏らさない)。

**結線:**
- `CallResult.agent_reason`(値ゼロ)を全 deny に付与(`Gateway._finalize_agent_reason`)。
- `AgentChannel` は deny 時に**内部 reason でなく agent_reason を wire に載せる**
  (内部 reason は値を含みうるので agent に見せない)。
- 汚染された実値は、人間が判断する確認ダイアログ(モデルコンテキスト外の側チャネル)
  にのみ流し、agent のコンテキストには戻さない。

**検証(`tests/test_feedback.py`, 8件):**
- 単体: builder に値引数thatない/不正 tool 名は placeholder 化。
- **性質テスト**: 注入形の operand 値5種(SQL/日本語 system 偽装/命令上書き等)で
  実際に deny させ、`agent_reason` that**どの値でもバイト同一・値を一切含まない**ことを
  証明。全60テスト通過。

**残余(scope外・明示):** これthat確実に閉じるのは**フィードバック経路**。読み取りの
戻り値そのもの(汚染返信)that agent のコンテキストに入る別経路は、一般的なプロンプト
注入問題で PAuth の scope 外(行動境界は守るthatモデルの認知は守らない)。

## S17. #1 の最終整理: 検出は中核でなく grill 機構＋UX に解ける(2026-07-07)

数ターンの議論で、#1(汚染データフロー)を「別建ての危険フロー検出モジュール」として
切り出したのは**過剰な分類**と判明。実体は2つに解ける:

1. **closure(正確さ)= grill 機構**。読みスライスを先行実行して実値を確定 → 人間が
   側チャネルで確認 → 凍結定数で書き込み(2フェーズ, S15)。書き込み後の読み(掲示板
   返信型)も「**各 sink のオペランド確定地点で必ず grill**」で自動的に閉じる。実行that
   その地点に到達した時点(=書き込みの後)で grill されるので、「書き込み後だ」と**検出
   する必要すらない**。→ 前ターンで私that「検出that正確さに必要」と言ったのは誤り、撤回。

2. **選別(規模・疲労)= grill-me UX**(S12/S13)。100件 fan-out で「全部 grill」that
   盲判子化するのを防ぐための、出所ラベル＋新規性による例外提示。これは human-factors
   の失敗(人間that確認しきれない)への UX 介入で、「grill での判断は人間の責任」という
   合意バケツに属する。**PAuth 中核の強制ではなく grill-me の読みやすさ改善**。

**帰結:**
- 別建ての「決定的危険フロー検出エンジン」は中核要件として**存在しない**。上の2つに吸収。
- trust ラベル＋テイントは「必ず grill」→「危険なものだけ grill」への最適化。制限文法
  ゆえ静的検出で足りる。規模that問題化してから足す後回し項目。
- agent-drives(無改造 Claude Code)では、grill はタスク分割でなく **1プラン＋その sink
  呼び出しをその場で確認ゲート**(分割は認可空白を作るため不可, S15)。
- 作る順序: **(1) grill 機構(2フェーズ＋sink ゲート＋側チャネル確認)を先に。** 
  (2) 中身/制御分離ルールで大半を grill 無しに。(3) UX 選別は規模対策として後で。

**実装状況(S18 参照):** 確認ゲート付き sink の中核を実装済み。残るは側チャネル確認 UI と
2フェーズ(読みスライス先行実行)の自動化。

## S18. 確認ゲート付き sink の実装(#1 closure の中核, 2026-07-07)

**実装(`gateway/runtime/confirmation.py` ＋ `gateway/runtime/gateway.py`):**
- `SourceTrust(untrusted_tools)` — どのツールが untrusted なデータを返すかのラベル。
  合成ランタイムが untrusted ツールの観測を記録した瞬間、その戻り値の scalar 群を
  `tainted_values` に加える(`collect_scalars` でオブジェクト/dict/list を再帰展開)。
- **ゲート判定**: sink 呼び出しの前に、**制御オペランド**(recipient/amount。precheck の
  `_classify_param` を再利用)の値が `tainted_values` にあり、かつ未承認なら、実行せず
  `PENDING_CONFIRMATION` で保留。ルールは消費せず stage も進めない。
- **中身/制御分離(S15)を実装で担保**: 汚染データが content オペランド(body 等)に
  流れても gate しない。control(宛先・金額)に流れたときだけ gate。
- **側チャネル API**: `Gateway.pending_confirmations()`(実値付き・人間向け)/
  `Gateway.confirm(id, approved)`。承認で `(tool, value)` を whitelist し、agent の
  同一呼び出しの再試行that通る。
- **S16 と結線**: 保留の理由文は値ゼロ(tool＋位置のみ)。`classify_reason` が
  PENDING_CONFIRMATION に写像し、agent には値ゼロのフィードバックのみ。**汚染された
  実値は人間の側チャネルにのみ流れ、agent のモデルコンテキストには戻らない。**

**検証(`tests/test_confirmation.py`, 8件・オフライン):** untrusted read(iban 攻撃者
制御)→ send_money 宛先の危険フローthat保留される/side channel が実値を持つ/agent_reason
that実値を含まない/承認で通る/拒否で通らない/再試行で重複要求thaでない/ラベル無しなら
gate しない/汚染データthat content(body)なら gate しない。全68テスト通過。

**設計上の限界(明示):**
- 値マッチングのテイント(operand 値that tainted 集合に一致するか)。衝突すれば
  over-gate(過剰確認=回復可能)で、under-gate(過剰認可)にはならない安全側。
- 合成ランタイム上のみ実装(session 経路は未対応)。stage 跨ぎの変数伝播は現状 guard/
  fanout のみなので、危険フローは1 stage 内(read→sink)を対象。
- `SourceTrust` は既定で「未列挙=trusted」。安全優先デプロイは逆(未列挙=untrusted)を
  選べる(gate that広がるだけで安全側)。

**未了(S18 時点):** (1) 側チャネルの確認 UI。(2) 2フェーズ自動化。(3) grill-me UX 選別。
(4) **session 経路への展開** → S19 で解消。

## S19. 確認ゲートを実プロダクト経路(session)に一本化(2026-07-07)

**問題(致命的だった):** S18 の確認ゲートは合成ランタイム(composite)経路にしか無く、
`handle_tool_call` の分岐で「composite があれば gate 付き、無ければ session」となっていた。
freeform/recognizer の普通のプランは全て session 経路を通り、CompositePlan を作る planner
も未実装。よって**ゲートは `test_confirmation.py` 内でしか発火せず、実プロンプトが通る
経路では #1 が開いたまま**だった。「#1 closure 実装」は「ラボでは閉じ、プロダクトでは
未接続」の意味で、言い過ぎだった。

**原因:** ゲートを「配管(観測・束縛追跡)that既にある composite」に付けたthat、そこthat
既定でない道だった。実装の都合で場所を選ぶと「動くthat誰も通らない」コードthaでき、
テストthat緑なので気づきにくい。

**修正(一本化):** テイントとゲートのロジックを共有化し、session 経路にも適用。
- gate フィールド(`source_trust`/`docs_by_name`/`tainted_values`/`pending`/`confirmed` 等)を
  `_Session` にも追加。`_accept_draft` で populate。
- `_confirmation_gate` と `_apply_taint` を両経路(session/composite)共用に(ロジック単一実装、
  重複なし)。`_handle_tool_call_session` に gate(実行前)＋ taint(record 後)を挿入。
- `pending_confirmations`/`confirm` を active state(composite or session)参照に。

**既存挙動は無傷:** `source_trust` 既定は空なので tainted_values that空 → ゲート発火せず、
既存の全テスト・L2 再生・shopping 実験は不変。

**検証:** `test_confirmation.py` に session 経路のテスト3件を追加(freeform 相当の単一 run() を
`submit_user_prompt_with_planner` で投げ、`gw._composite is None` を確認した上でゲート発火・
承認で通過・ラベル無しなら不発火)。全71テスト通過。

**教訓(記録):** 「実装した」と「実際に効いている」は別。部品thatテストで緑でも、実
トラフィックthat通る経路に繋がっていなければ守りはゼロ。セキュリティ機構は「どの道を
実トラフィックthat通るか」を先に確認し、付けやすい場所でなく**通る場所**に付ける。

**残る #1 の穴 → S20 で2つとも修正:**

## S20. 静的プロベナンステイント ＋ fail-closed(2026-07-07)

エンジニアリングで解ける2つの致命的穴(#2 under-gate, #3 fail-open)を実装。

**#2: 値マッチング → 静的プロベナンステイント(`confirmation.static_taint`)**
- 制限文法(単一代入・ループ無し・ツール結果→変数that明示)ゆえ、**コード＋信頼ラベル
  から「どの制御オペランドthat untrusted 由来か」を静的に計算できる**。値でなく**依存**を
  追うので、変換(`amount = msg.amount * 2`、`min(...)` 選択)を経ても taint that落ちない。
- `static_taint(code, docs, source_trust, policy)` → `{(tool, param_index)}`。プラン受理時
  (session)/ stage 活性時(composite)に計算し `state.gated_operands` に格納。
- ゲートは `(tool, position)` メンバーシップで判定(laundering 不可)。確認は値でキー
  (承認した値だけ通す)。→ **金額・選択の laundering that塞がった**。
- 検証: `test_static_taint_survives_transformation_laundering`、
  `test_laundered_amount_is_gated_end_to_end`(`msg.amount * 2` that実際に gate される)。

**#3: fail-open → fail-closed(`SourceTrust.fail_closed`)**
- `SourceTrust` に `trusted_tools` ＋ `default_untrusted` を追加。`is_untrusted` は
  「明示 trusted → False、明示 untrusted → True、それ以外 → default_untrusted」。
- `SourceTrust.fail_closed(trusted_tools=...)` = 未列挙は全て untrusted。新しい源をラベル
  し忘れても over-gate(回復可能)に倒れ、**黙って無防備にならない**。
- Gateway 既定の `SourceTrust()` は後方互換で fail-open のまま(gate off)。**本番は
  fail_closed ＋ 自データ読みを trusted 宣言**、を推奨として明記。
- 検証: `test_fail_closed_gates_unlabelled_source` / `test_fail_closed_respects_trusted_declaration`。

**残る #1 の限界(既知・要研究 or 別軸):**
- fan-out stage は観測定数that畳み込み済みで provenance that落ちる → untrusted リストの
  fan-out that under-gate しうる(fan-out は未だ live path でない)。テンプレート解析で
  塞げるthat未実装。
- 静的テイントは trusted ツールthat untrusted 引数で呼ばれた場合その結果も untrusted と
  みなす(over-approx・安全側)。過剰 gate that増えるthat FP には倒れない。

## S21. 側チャネル既定拒否 ＋ 保護レベルの正直報告(#4/B5, 2026-07-07)

#4 のうち**セキュリティ核心で実装可能な部分**を実装。完全なデプロイ(実 SaaS・隔離 VM・
パッケージング)はインフラ作業として残す。

**(A) 側チャネル既定拒否(`gateway/runtime/protection.py`)**
- `SideChannelPolicy` — bash/shell/sh/exec/subprocess/system/eval 等を既定 denylist に。
  `handle_tool_call` の**最上段**で、denylist の tool を無条件 deny(session/composite の
  手前)。plan.md Stage 1 の「禁止前提」を**文書でなく機構として強制**。
- `allowlist` で個別に例外化可能。理由文は値ゼロ(S16、`SIDE_CHANNEL_DENIED`)。
- 既定で有効。既存 suite の tool 名(send_money 等)は denylist に無いので無影響。
- **限界(正直に)**: これthat止めるのは「gateway を通った Bash 呼び出し」だけ。フックを
  通らないサブプロセスや直接ネットワークは、この method に届かないので防げない。それは
  下の保護レベル報告で開示する。

**(B) 保護レベルの正直報告(L0–L3)**
- `ProtectionInputs`(clean prompt 捕捉 / tool routing / gateway 実行 / 側チャネル拒否 /
  隔離ランタイム)から `assess()` that L0–L3 ＋ **caveat(bypass リスク)**を計算。
- `Gateway.protection_report()` — in-process gateway は L3 相当(prompt 捕捉・routing・実行)
  だが、**非隔離なら「out-of-band 実行は隔離ランタイム無しには防げない」を caveat として
  必ず開示**。「滑らかに L0 へ劣化」を防ぎ、DESIGN_STATUS の「保護レベルを正直表示」を実装。
- 検証(`tests/test_protection.py`, 13件): Bash 等の deny・値ゼロ理由・allowlist 例外・
  L0–L3 判定・非隔離での bypass caveat・隔離なら caveat 無し。全89テスト通過。

**#4 の残(インフラ・別作業、この環境では未完):** 実 Claude Code/実 SaaS への配線、
隔離エージェントモード(VM/コンテナで外向きを gateway 経由に強制)、パッケージング。
コード側の honest-reporting と側チャネル強制は済み、**「無防備なのに L3 と誤称する」
リスクは塞いだ**。out-of-band 迂回の根本防止は隔離モード(インフラ)を要する。

## S22. 傍受プロキシ・アダプタ(intercept → inspect → forward/block の核)(2026-07-07)

「一旦遮断 → 中身を見て取得 → 本来の宛先へ送り直す/ブロック」を、**テスト可能な
enforcement 核**として実装。TLS 終端・ネットワーク配線(mitmproxy・CA・出口一本化・
base URL 差し替え)は**この環境でテストできないインフラのシェル**なので分離した。

**実装(`gateway/serving/proxy.py`)**
- `InterceptingProxy(gateway, model_upstream, submit=...)`。
- **推論チャネル**(`handle_inference`): Anthropic 形式の messages から clean prompt を
  抽出(`capture_prompt`)→ gateway に submit(プラン確立)→ **常に model へ転送**。推論は
  enforcement 点でない(止めるとエージェントthat思考できない)。「capture is not enforcement」。
- **ツールチャネル**(`handle_tool`): gateway.handle_tool_call で認可 → **permit なら転送
  (=実行、gateway の SuiteSpec runner that実 SaaS クライアント)、deny なら値ゼロの
  block 応答**(S16、内部 reason でなく agent_reason)。403 + `pauth_denied`。
- 側チャネル(bash 等)も proxy 経由で block される(S21 と結線)。

**検証(`tests/test_proxy.py`, 8件・ソケット不要):** prompt 抽出(string/block content)・
推論の常時転送＋捕捉・ツール permit の転送・危険フローの block(汚染値that block 応答に
出ない)・側チャネル block。全97テスト通過。

**残(インフラのシェル、未実装):** mitmproxy アドオン等で (a) TLS 終端(base URL 差し替え
that本命、MITM＋CA thaピンニング無ければ可)、(b) この核に parsed request を渡し
`response`/`block_response` を wire に返す配線、(c) SaaS 側接続の proxy 化。**enforcement
ロジックは完成、残りはネットワーク配線と隔離**(#4' のインフラ部分)。

---

## 環境メモ(2026-07-05)

- `.env` に OPENAI_API_KEY / PAUTH_MODEL あり。`../me.env`(ANTHROPIC_API_KEY)は不在
  → S3 の多プロバイダ judge で代替。Anthropic judge での本計測はキー投入後に再実行する。

---

# 設計審議ログ(Q系列 — 旧 grill.md を統合, 2026-07-08)

実装の前後で行った設計対話の記録(質問・推奨・ユーザ回答・収束)。上の S 系列(実装中に
確定した決定)と同じ「決定の結果＋根拠」構成なので本書に一本化した。Q番号は plan.md /
architecture.md / threat-model.md から ID 参照される出典。実装の詳細は該当する S番号を
参照(例: Q15 → S1/S3/S7/S9/S14、Q10 → S21/S22＋egress lockdown)。

## Q2. snapshot か git か

**質問:** 「git に commit」を「独立 snapshot ストアに記録」に置き換えれば commit churn /
ブランチ汚染 / pre-commit hook コスト / WIP 固定化 のコストがほぼ消える。それでも git を
使う理由は?(選択肢 α=snapshot で十分 / β=外部監査可能性 / γ=cross-file semantic
checkpoint / δ=共有・協調)

**推奨:** α。**理由:** 要件は「失敗時に直前の良好状態へ戻れること」。per-file snapshot で
十分で、recoverability のために git のコストを払う理由がない。

**ユーザ回答→収束:** snapshot 復元は git と等価に決定的(両方 OS 級 file write)・LLM 非介在。
「git の方が決定的」は誤りで、正しくは「git は atomic(cross-file 整合性)」が利点。
**収束: γ'** — cross-file atomic checkpoint は要るが git そのものでなく agent 専用ブランチ。

**状態:** 方向決定済・実装未了(主に Mode 2 のロールバック harness の話で PAuth 本体外)。

## Q5. Phase tracking の配置場所

**質問:** read→plan→write ループの phase tracking をどこに置くか?(P1=harness /
P2=agent 自己申告 / P3=専用 sub-agent)

**推奨:** P1(ただし大幅な harness 改造が要る)。

**ユーザ回答:** 直接回答なし。「Claude Code の中身は変えたくない、アタッチメントが欲しい」
への方向転換。**状態:** アタッチメント方針で moot(放棄)。

## Q6. Agent 出力 → run() ブリッジ方式

**質問:** Claude Code は run() を持たない。PAuth は run() を入口にする。どう繋ぐか?
(M1=upfront plan / M2=自由実行＋capability gate / M3=declare→execute→再 declare /
M4=後付け解釈)

**推奨:** M1 を基本に、replan 時のみ M3 を限定許可。

**ユーザ回答:** 直接回答なし。**状態:** 実装済み(ゲートウェイ＋AgentChannel。
`submit_user_prompt` で一度計画、`handle_tool_call` で毎回強制)。

## Q8. 実装スコープ(3問同時 — AskUserQuestion)

**質問1(実ツール層):** AgentDojo suite(推奨)/ 自前ミニ suite / 両方。
**質問2(エージェント):** scenario runner(推奨)/ 実 LLM / 両方。
**質問3(user→gateway 直結):** Python API 分離(推奨)/ HTTP・gRPC / 構造保証のみ。

**ユーザ回答:** すべて推奨(AgentDojo / scenario runner / Python API 分離)。
**状態:** 実装完了(8 シナリオ・24 attempts 期待通り)。

## Q9. A1(prompt → run())の戦略

**質問:** ゲートウェイ内で run() を誰が生成するか?(L1=決定的 recognizer のみ /
L2=LLM 翻訳＋決定的 verifier / L3=LLM 翻訳＋緩い verifier)

**推奨:** L1 で確定。**理由:** L2 は LLM を挟んでも受理範囲が L1 と同じで純粋な overhead。
L3 は受理範囲を広げる代わりに zero FP/FN 保証を犠牲にする。

**ユーザ回答:** 直接回答なし(直 NL slice でなく run() コードを挟む理由を質問→説明。
以後ゲートウェイ実装のリオーガナイズ＋Claude Code 統合へ展開)。
**状態:** 実装済み(L1 recognizer ＋ LLM freeform の両経路。S2 の `auto` 戦略で統合)。

## Q10. Claude Code の tool 呼び出しを *どこで* 捕まえるか

**質問:** Claude Code を無改造で動かしたまま外部 tool 呼び出しを gateway で捕捉する
メカニズムは?(I1=MCP サーバ / I2=HTTP forward proxy / I3=netns＋DNS hijack /
I4=Claude Code 改造)

**推奨:** I1(MCP)を基本、I2(HTTP proxy)を補助層。**理由:** I1 単独は Bash `curl` で
容易に bypass。I3 は macOS で苦しい。I4 は要件違反。I1+I2 で正規経路と抜け道の二段防御。
ただし「Claude Code 自身が積極的に bypass しない」前提を許容する必要がある。

**2026-07-08 決着(場合B / サイドチャネル迂回):** I1+I2 の意味的捕捉(実装は S22 の
`InterceptingProxy`、側チャネル既定拒否は S21)に加え、**OS の egress ロックダウン**で
「生 Bash・直接ネットワーク I/O を持たない」Stage 1 前提を*強制*する方針に確定。エージェントを
専用の非管理ユーザで動かし外向き通信をゲートウェイのホスト:ポートだけに制限する
(`gateway/deploy/egress_lockdown.sh`)。迂回は「外部宛て→カーネルで drop」か
「ゲートウェイ宛て→default-deny で拒否」の二択になり、中身を解読せず fail-closed になる。

**成立条件(必須):** エージェントが**非管理ユーザ**であること。管理者権限を与えると
ルールを外せ本制御は無効(self-hosting.md「Egress Lockdown」に明記)。
**範囲外:** 非ネットワーク副作用(ローカルファイル等)・DoH 独自リゾルバ固定 → 別レイヤ。

## Q15. Validator 強化: Semantic judge(LLM as a judge)

**背景:** validator は「LLM 出力に対するテスト関数」として作用するが、現状のテストが文法
のみで**意味論(prompt の intent 捕捉)を見ていない**。結果、LLM が grammar 適合のため
intent を勝手に削る現象(B1 / two_products / post_action)が素通りしていた。Q14(PreAuth
grill=対話確認)とは別レイヤで、Q15 は agentic A1 内で LLM が LLM 出力を自動判定する。

**2026-06-09 方針(片側安全性):** 目的は intent 完全同値の証明ではない(自然言語の曖昧さ
ゆえ不可能に近い)。**prompt から正当化できない過剰認可(over-authorization)だけを弾く**。
狭すぎる plan の over-rejection は retry/clarification で回復可能な UX 問題として許容し、
過剰認可 accept を最優先で避ける。判定軸: side-effect / operand / data-flow / 条件緩和の
entailment。

**質問:** semantic judge を retry loop にどう組み込むか?(J1=grammar 直後 / J2=外側 独立
ループ / J3=retry の最初 / J4=入れず Q14 に振る)

**推奨→採用: J1**(grammar OK → judge → 両方 OK で成功、NG は反例を retry に載せる)。
既存の grammar feedback loop と同じ messages stream に統合でき実装単純。文法未達コードを
判定する意味はない(intent 違反は grammar 違反より上位)。

**ユーザ回答:** one-sided semantic validator ＋ retry loop を採用。具体実装(prompt / IR /
決定的 validator との分担 / fixture)は最も研究が要る部分として残す。

**状態: 実装済み(v1)＋継続最適化(plan.md Stage 2)。** 実装と決定は S 系列に展開:
- Q15-e(機械的検査の前段化)→ **S1**(`prechecks.py`: 禁止 tool・宛先・金額/数量・
  read→write の決定的 precheck。誤検出は over-rejection 側に倒れ安全)。カバレッジ穴埋めは **S14**。
- Q15-a(judge の確率性・別モデル化)→ **S3**(provider 自動判別。別モデル化済、アンサンブル未)。
- Q15-c(judge への注入)→ 判定 system prompt に「コメント/変数名の主張を信用するな」を明記(解決)。
- Q15-d(over-authorization accept の別名計測)→ **S7**。実測は **S9**(4 suite 97 タスクで
  over-authorization accept = 0)。
- 残(Q15-b over-rejection 計測・Q15-f prompt/IR/評価指標)→ plan.md Stage 2 の🔬研究として継続。

## まとめ(Q系列の状態)

| Q | トピック | 状態 |
|---|---|---|
| Q2 | snapshot vs git | γ'(cross-file atomic, agent 専用ブランチ)・実装未了 |
| Q5 | Phase tracking | 放棄(アタッチメント方針で moot) |
| Q6 | Agent → run() ブリッジ | 実装済み(ゲートウェイ + AgentChannel) |
| Q8 | 実装スコープ | 全推奨採用・実装済み |
| Q9 | A1 戦略 | 実装済み(L1 recognizer + LLM freeform, S2) |
| Q10 | 捕捉メカニズム | 決着(2026-07-08): I1+I2(S21/S22) + egress ロックダウン。非管理ユーザ前提 |
| Q15 | Validator semantic judge | 実装済み(v1, S1/S3/S7/S9/S14) + Stage 2 継続 |
