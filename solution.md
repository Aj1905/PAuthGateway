# solution log

`plan.md` の 💬議論 項目を実装中に解決した記録。決定ごとに「決定・根拠・影響範囲・残課題」を書く。
🔬研究 項目は原則ここでは解決しない(解決を試みた場合はその範囲を明記する)。

書式: S番号は本書内の通し番号。plan.md / grill.md の ID (Q15-e, B2 等) と相互参照する。

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

**根拠:** THREAT_MODEL.md が既に側チャネルを scope 外と明記している。Stage 1 の主張
「乗っ取られたエージェントは承認計画の構造を超える SaaS 実行ができない」は
「外向き I/O が gateway 経由の tool call のみ」という前提でのみ成立する。この前提を
満たさないデプロイを黙って L3 と呼ぶことが最大の虚偽表示リスクであり、機構追加
より先に前提の明文化が必要(DESIGN_STATUS「保護レベルの正直表示」と同旨)。

**影響:** SELF_HOSTING.md / hooks README に「保護の前提条件」として明記する(未了)。

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
| #1 汚染データフロー(untrusted観測→sink) | 構造的 | 🔴 未対応。Stage 3(危険フロー検出)＋ N_max/grill 待ち。**唯一の構造的穴** |
| #2 judge false-pass(非定数の過剰認可クラス) | 確率的 | 🟡 F1 継続。別モデル化済み、アンサンブル未 |
| #3 precheck カバレッジ欠落 | 決定的 | 🟢 S14 で穴埋め(名前＋説明文＋ポリシー宣言) |
| #4 合成ランタイム実装リスク | 新規コード | 🟡 14 テストで検証、形式証明なし |

**結論:** 現状 FP を導きうる面で残る本丸は **#1(汚染データフロー)** のみが構造的。
#2 は確率依存の縮退面、#4 は実装保証の問題。#3 は決定的に閉じた。「任意プロンプトで
FP0」を主張するには #1 の危険フロー検出(Stage 3)が必須で、それまでは主張範囲を
「trusted ソースに対する fan-out ＋ precheck が覆う定数系＋実行時強制」に限定する。

---

## 環境メモ(2026-07-05)

- `.env` に OPENAI_API_KEY / PAUTH_MODEL あり。`../me.env`(ANTHROPIC_API_KEY)は不在
  → S3 の多プロバイダ judge で代替。Anthropic judge での本計測はキー投入後に再実行する。
