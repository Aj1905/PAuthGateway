"""論文が報告している値(比較の基準)。

ここに書いてよいのは PDF から直接読み取れた数値だけである。図から目視で
読み取った不確実な値は入れない(入れると再現側の差分がどちらの誤りか
判別できなくなる)。
"""

from __future__ import annotations

# --- 表 1: 評価スイートと試行数 ---------------------------------------
# (良性タスク数, 強制注入数, 試行数)
PAPER_TABLE1: dict[str, tuple[int, int, int]] = {
    "banking": (16, 52, 68),
    "slack": (19, 73, 92),
    "workspace": (40, 205, 245),
    "travel": (20, 200, 220),
    "shopping": (5, 104, 109),
}
PAPER_TABLE1_TOTAL = (100, 634, 734)

# --- 表 2: 偽陰性 / 偽陽性 --------------------------------------------
# (偽陰性数, 注入試行数, 偽陽性数, 良性試行数)
# 論文の表 2 は banking の良性試行を 16 と記す。表 1 の良性タスク数と一致する。
PAPER_TABLE2: dict[str, tuple[int, int, int, int]] = {
    "banking": (0, 52, 0, 16),
    "slack": (0, 73, 0, 19),
    "workspace": (0, 205, 0, 40),
    "travel": (0, 200, 0, 20),
    "shopping": (0, 104, 0, 5),
}
PAPER_TABLE2_TOTAL = (0, 634, 0, 100)

# --- 図 9: タスクあたりのルール数 -------------------------------------
# 図の全 100 本は目視では読み取れない。本文が明示している shopping 5 件の
# 合計ルール数だけを基準として持つ。
PAPER_FIG9_SHOPPING_TOTALS: tuple[int, ...] = (13, 17, 19, 21, 24)

# --- 図 10: 平均トークン費用 ------------------------------------------
# 棒ごとの値は図からの目視読み取りになるため入れない。本文が明示している
# 範囲だけを基準とする。
PAPER_FIG10_PER_TASK_USD_RANGE = (0.002, 0.038)
PAPER_FIG10_CHEAPEST_MODEL = "gemini-3-flash-preview"
PAPER_FIG10_MODELS = (
    "gpt-4.1",
    "gpt-5-mini",
    "gemini-3-flash-preview",
    "claude-sonnet-4.5",
)

# 論文が計画(run コード)生成に使ったモデル(表 2 の試行)
PAPER_PLANNER_MODEL = "gpt-4.1"

# 論文の DSL は本リポジトリの版 ID では G1 に相当する(docs/SYSTEM_MODEL.md)。
PAPER_DSL_PROFILE = "g1"
