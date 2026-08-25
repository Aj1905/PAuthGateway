"""図 10(トークン費用)用の価格表。

論文の図 10 は USD 建ての棒グラフだが、価格は時期とともに変わる。
再現側で確実に測れるのはトークン数であり、USD は価格表を掛けた
派生値にすぎない。したがって:

* トークン数は常に報告する(測定値)。
* USD は価格が分かっているモデルについてのみ計算し、不明なら ``None`` を
  返して「未算出」と明記する(推測値を混ぜない)。

価格は環境変数 ``PAUTH_REPRO_PRICING`` に JSON
``{"model": [入力USD/1Mtok, 出力USD/1Mtok]}`` を渡して上書きできる。
"""

from __future__ import annotations

import json
import os

# 1M トークンあたりの USD。値は掲載時点の公開価格に基づく参考値であり、
# 引用の前に必ず出典を確認すること。
KNOWN_PRICING: dict[str, tuple[float, float]] = {
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-4o": (2.50, 10.00),
}

# 論文が図 10 で比較しているが、本パッケージが確実な価格を持たないモデル。
# 価格を与えるまで USD は算出しない。
UNPRICED_MODELS: tuple[str, ...] = ("gemini-3-flash-preview", "claude-sonnet-4.5")


def pricing_table() -> dict[str, tuple[float, float]]:
    table = dict(KNOWN_PRICING)
    override = os.environ.get("PAUTH_REPRO_PRICING")
    if override:
        for model, pair in json.loads(override).items():
            table[model] = (float(pair[0]), float(pair[1]))
    return table


def cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float | None:
    """USD 費用。価格が不明なら ``None``(推測しない)。"""
    pricing = pricing_table().get(model)
    if pricing is None:
        return None
    inp, out = pricing
    return prompt_tokens / 1e6 * inp + completion_tokens / 1e6 * out
