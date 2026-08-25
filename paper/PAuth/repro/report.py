"""測定結果を論文の表 1 / 表 2 / 図 9 / 図 10 の形に整えて出力する。

出力は Markdown(人間向け)、JSON(機械可読)、CSV(図 9 の元データ)、
SVG(図 9 の積み上げ棒)。SVG は外部依存を避けるため手書きで出す
(matplotlib は本リポジトリの依存に無い)。
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from repro import claims
from repro.harness import TaskOutcome
from repro.pricing import UNPRICED_MODELS, cost_usd


# ----------------------------------------------------------------------
# 集計
# ----------------------------------------------------------------------
def summarize(outcomes: list[TaskOutcome]) -> dict:
    """スイート別・全体の集計を作る。"""
    by_suite: dict[str, list[TaskOutcome]] = defaultdict(list)
    for o in outcomes:
        by_suite[o.suite].append(o)

    suites: dict[str, dict] = {}
    for name, group in by_suite.items():
        checkable = [o for o in group if o.checkable]
        prompt_tokens = sum(o.prompt_tokens for o in checkable)
        completion_tokens = sum(o.completion_tokens for o in checkable)
        models = sorted({o.planner_model for o in checkable if o.planner_model})
        model = models[0] if len(models) == 1 else "(mixed)"
        total_cost = cost_usd(model, prompt_tokens, completion_tokens)
        suites[name] = {
            "tasks_total": len(group),
            "tasks_checkable": len(checkable),
            "plan_unavailable": sum(1 for o in group if o.plan_status == "unavailable"),
            "dsl_rejected": sum(1 for o in group if o.plan_status == "dsl-rejected"),
            "benign_runs": len(checkable),
            "forced_injections": sum(o.n_injections for o in checkable),
            "test_runs": len(checkable) + sum(o.n_injections for o in checkable),
            "false_positives": sum(1 for o in checkable if o.is_false_positive),
            "false_negatives": sum(o.n_false_negatives for o in checkable),
            "crashed": sum(1 for o in checkable if o.crashed),
            # 空計画(ルール 0 = ツール呼び出し地点 0)と無呼び出し実行は、
            # 既定拒否のおかげで中身を見なくても FP=0 / FN=0 になる。
            # 判定として自明なので分けて数える。
            "empty_plans": sum(1 for o in checkable if o.n_rules == 0),
            "no_call_runs": sum(1 for o in checkable if o.benign_calls == 0),
            "planner_model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cost_usd": total_cost,
            "cost_usd_per_task": (
                total_cost / len(checkable) if total_cost is not None and checkable
                else None
            ),
        }

    keys = (
        "tasks_total", "tasks_checkable", "plan_unavailable", "dsl_rejected",
        "benign_runs", "forced_injections", "test_runs",
        "false_positives", "false_negatives", "crashed",
        "empty_plans", "no_call_runs",
        "prompt_tokens", "completion_tokens",
    )
    overall = {k: sum(s[k] for s in suites.values()) for k in keys}
    return {"suites": suites, "overall": overall}


# ----------------------------------------------------------------------
# Markdown
# ----------------------------------------------------------------------
def _delta(repro: int, paper: int) -> str:
    diff = repro - paper
    return "一致" if diff == 0 else f"{diff:+d}"


def render_markdown(
    summary: dict,
    outcomes: list[TaskOutcome],
    *,
    dsl_profile: str,
    model: str,
    skipped: dict[str, str],
) -> str:
    suites = summary["suites"]
    overall = summary["overall"]
    order = [n for n in claims.PAPER_TABLE1 if n in suites]
    lines: list[str] = []
    add = lines.append

    add("# PAuth 論文(arXiv:2603.17170)第 5 節 再現結果")
    add("")
    add(f"- Planner モデル: `{model}`(論文: `{claims.PAPER_PLANNER_MODEL}`)")
    add(f"- DSL 版: `{dsl_profile}`(論文の DSL は `{claims.PAPER_DSL_PROFILE}` に相当)")
    if skipped:
        for name, why in skipped.items():
            add(f"- 読み込めなかったスイート: `{name}` — {why}")
    add("")

    # --- 表 1 --------------------------------------------------------
    add("## 表 1 相当 — スイートと試行数")
    add("")
    add("| スイート | 良性(再現) | 良性(論文) | 差 | 強制注入(再現) | 強制注入(論文) | 差 | 試行(再現) | 試行(論文) |")
    add("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for name in order:
        s = suites[name]
        p_benign, p_inj, p_runs = claims.PAPER_TABLE1[name]
        add(
            f"| {name} | {s['benign_runs']} | {p_benign} | {_delta(s['benign_runs'], p_benign)} "
            f"| {s['forced_injections']} | {p_inj} | {_delta(s['forced_injections'], p_inj)} "
            f"| {s['test_runs']} | {p_runs} |"
        )
    p_benign, p_inj, p_runs = claims.PAPER_TABLE1_TOTAL
    add(
        f"| **合計** | **{overall['benign_runs']}** | {p_benign} | {_delta(overall['benign_runs'], p_benign)} "
        f"| **{overall['forced_injections']}** | {p_inj} | {_delta(overall['forced_injections'], p_inj)} "
        f"| **{overall['test_runs']}** | {p_runs} |"
    )
    add("")
    add("強制注入の件数は各ハーネスが生成したものであり、論文の 634 件と同一の")
    add("集合ではない。件数の差は難易度の差ではない(生成規則が違う)。")
    add("")

    # --- 計画の入手状況 ----------------------------------------------
    add("## 計画(run コード)の入手状況 — 論文には対応表が無い")
    add("")
    add("| スイート | タスク総数 | 判定対象 | 計画なし | DSL 棄却 | うち空計画 | うち良性無呼び出し |")
    add("|---|---:|---:|---:|---:|---:|---:|")
    for name in order:
        s = suites[name]
        add(
            f"| {name} | {s['tasks_total']} | {s['tasks_checkable']} "
            f"| {s['plan_unavailable']} | {s['dsl_rejected']} "
            f"| {s['empty_plans']} | {s['no_call_runs']} |"
        )
    add(
        f"| **合計** | **{overall['tasks_total']}** | **{overall['tasks_checkable']}** "
        f"| **{overall['plan_unavailable']}** | **{overall['dsl_rejected']}** "
        f"| **{overall['empty_plans']}** | **{overall['no_call_runs']}** |"
    )
    add("")
    add("論文は 100 タスク全件で計画が得られたと報告している。判定対象から外れた")
    add("タスクは表 2 の分母に入っていないので、FP/FN が 0 でも被覆率は論文より低い。")
    add("")
    nontrivial = overall["tasks_checkable"] - overall["no_call_runs"]
    add(
        f"さらに、判定対象 {overall['tasks_checkable']} 件のうち "
        f"{overall['empty_plans']} 件はツール呼び出しを 1 つも持たない空計画、"
        f"{overall['no_call_runs']} 件は良性実行でツール呼び出しが 1 度も起きなかった。"
    )
    add("既定拒否なので、これらは中身を検査しなくても FP=0 / FN=0 になる。")
    add(f"非自明な判定対象は **{nontrivial} 件**(論文は 100 件)。")
    add("")

    # --- 表 2 --------------------------------------------------------
    add("## 表 2 相当 — 偽陰性 / 偽陽性")
    add("")
    add("| スイート | 偽陰性(注入試行) | 論文 | 偽陽性(良性試行) | 論文 |")
    add("|---|---:|---:|---:|---:|")
    for name in order:
        s = suites[name]
        p_fn, p_fn_runs, p_fp, p_fp_runs = claims.PAPER_TABLE2[name]
        add(
            f"| {name} | {s['false_negatives']} ({s['forced_injections']}) | {p_fn} ({p_fn_runs}) "
            f"| {s['false_positives']} ({s['benign_runs']}) | {p_fp} ({p_fp_runs}) |"
        )
    p_fn, p_fn_runs, p_fp, p_fp_runs = claims.PAPER_TABLE2_TOTAL
    add(
        f"| **合計** | **{overall['false_negatives']}** ({overall['forced_injections']}) | {p_fn} ({p_fn_runs}) "
        f"| **{overall['false_positives']}** ({overall['benign_runs']}) | {p_fp} ({p_fp_runs}) |"
    )
    add("")
    if overall["crashed"]:
        add(
            f"注: 判定対象 {overall['tasks_checkable']} 件のうち {overall['crashed']} 件は "
            "良性実行中に run コードがクラッシュした。拒否ではないので偽陽性には数えて"
            "いないが、その先の呼び出しは検査されていない。"
        )
        add("")

    # --- 図 9 --------------------------------------------------------
    add("## 図 9 相当 — タスクあたりのルール数")
    add("")
    counted = [o for o in outcomes if o.rule_counts is not None]
    if counted:
        totals = sorted(o.rule_counts.total for o in counted)
        const = sum(o.rule_counts.constant_operand for o in counted)
        nonconst = sum(o.rule_counts.non_constant_operand for o in counted)
        asserts = sum(o.rule_counts.assertion for o in counted)
        add(f"- 対象タスク: {len(counted)} 件")
        add(f"- 1 タスクあたりのルール総数: 最小 {totals[0]} / 中央 {totals[len(totals) // 2]} / 最大 {totals[-1]}")
        add(f"- 内訳合計: 定数オペランド {const} / 非定数オペランド {nonconst} / assert {asserts}")
        shopping = sorted(
            o.rule_counts.total for o in counted if o.suite == "shopping"
        )
        if shopping:
            add(
                f"- shopping の合計ルール数: {tuple(shopping)}"
                f"(論文: {claims.PAPER_FIG9_SHOPPING_TOTALS} — 本リポジトリの "
                "shopping はタスク数自体が論文より少ない)"
            )
        add("- ルール総数 0 のタスクは、計画中の呼び出しがすべて引数なし"
            "(オペランド制約も assert も無く、ツール同一性だけで執行される)ことを意味する。")
        add("- 元データ: `figure9_rules.csv` / 図: `figure9_rules.svg`")
    else:
        add("- 判定対象のタスクが無いため未算出。")
    add("")

    # --- 図 10 -------------------------------------------------------
    add("## 図 10 相当 — 計画生成のトークン費用")
    add("")
    add("| スイート | Planner モデル | 入力トークン | 出力トークン | 1 タスクあたり USD |")
    add("|---|---|---:|---:|---:|")
    for name in order:
        s = suites[name]
        per_task = s["cost_usd_per_task"]
        if s["planner_model"] == "(none)":
            # スイート同梱の参照計画。Planner を呼んでいないので費用は定義上 0。
            cost_cell = "0(参照計画・LLM 未使用)"
        elif per_task is None:
            cost_cell = "未算出(価格不明)"
        else:
            cost_cell = f"{per_task:.4f}"
        add(
            f"| {name} | {s['planner_model']} | {s['prompt_tokens']} "
            f"| {s['completion_tokens']} | {cost_cell} |"
        )
    add("")
    lo, hi = claims.PAPER_FIG10_PER_TASK_USD_RANGE
    add(f"論文は 1 タスクあたり ${lo}〜${hi} と報告し、最安は "
        f"{claims.PAPER_FIG10_CHEAPEST_MODEL} だとしている。棒ごとの値は図から")
    add("確実に読み取れないため、ここでは基準に入れていない。")
    add("")
    add(f"価格表を持たないモデル: {', '.join(UNPRICED_MODELS)} "
        "— `PAUTH_REPRO_PRICING` で与えれば USD も算出する。")
    add("")
    add("キャッシュ済みの計画を使った場合、ここに出るのは**生成当時に記録された**")
    add("トークン数であり、再現実行そのものの費用ではない(再利用は無料)。")
    add("")

    # --- 差分の要約 --------------------------------------------------
    add("## この再現で論文と一致しないところ")
    add("")
    add("1. タスク数: slack は AgentDojo v1 に 21 件あり、論文の 19 件と一致しない。")
    add("   shopping は論文が 5 件、本リポジトリの実装は 2 件。")
    add("2. 強制注入: 論文はタスクごとに手で設計した 634 件。本再現は")
    add("   `benchmarks/forced_injection.py` が機械生成したもので、集合が異なる。")
    add("3. 被覆率: 論文は 100/100 タスクで計画が得られたが、本再現では計画が")
    add("   キャッシュに無い、または DSL が棄却するタスクがある。")
    add("4. 図 10 の USD: 価格表が一致する保証は無い。トークン数のほうが比較可能。")
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------
# 図 9 の出力
# ----------------------------------------------------------------------
def figure9_rows(outcomes: list[TaskOutcome]) -> list[dict]:
    rows = [
        {
            "suite": o.suite,
            "task_id": o.task_id,
            "constant_operand_rules": o.rule_counts.constant_operand,
            "non_constant_operand_rules": o.rule_counts.non_constant_operand,
            "assert_rules": o.rule_counts.assertion,
            "total_rules": o.rule_counts.total,
        }
        for o in outcomes
        if o.rule_counts is not None
    ]
    rows.sort(key=lambda r: (r["total_rules"], r["suite"], r["task_id"]))
    return rows


def write_figure9_csv(rows: list[dict], path: Path) -> None:
    header = (
        "rank,suite,task_id,constant_operand_rules,"
        "non_constant_operand_rules,assert_rules,total_rules"
    )
    lines = [header]
    for i, r in enumerate(rows, 1):
        lines.append(
            f"{i},{r['suite']},{r['task_id']},{r['constant_operand_rules']},"
            f"{r['non_constant_operand_rules']},{r['assert_rules']},{r['total_rules']}"
        )
    path.write_text("\n".join(lines) + "\n")


_COLORS = ("#4c78a8", "#f58518", "#54a24b")  # 定数 / 非定数 / assert
_LABELS = ("定数オペランド", "非定数オペランド", "assert")


def write_figure9_svg(rows: list[dict], path: Path) -> None:
    """図 9 の積み上げ棒を SVG で書き出す(外部依存なし)。"""
    if not rows:
        path.write_text("<svg xmlns='http://www.w3.org/2000/svg'/>\n")
        return
    left, right, top, bottom = 56, 16, 48, 40
    bar_w, gap = 9, 2
    plot_h = 260
    plot_w = len(rows) * (bar_w + gap)
    width = left + plot_w + right
    height = top + plot_h + bottom
    y_max = max(r["total_rules"] for r in rows)
    step = 10 if y_max > 20 else 5
    y_top = ((y_max + step - 1) // step) * step or step

    def y_of(value: float) -> float:
        return top + plot_h - plot_h * value / y_top

    parts = [
        f"<svg xmlns='http://www.w3.org/2000/svg' width='{width}' height='{height}' "
        f"viewBox='0 0 {width} {height}' font-family='sans-serif' font-size='11'>",
        f"<rect width='{width}' height='{height}' fill='white'/>",
    ]
    # y 軸グリッドと目盛り
    tick = 0
    while tick <= y_top:
        y = y_of(tick)
        parts.append(
            f"<line x1='{left}' y1='{y:.1f}' x2='{left + plot_w}' y2='{y:.1f}' "
            "stroke='#dddddd' stroke-width='1'/>"
        )
        parts.append(
            f"<text x='{left - 8}' y='{y + 4:.1f}' text-anchor='end' fill='#333'>{tick}</text>"
        )
        tick += step
    # 棒
    for i, r in enumerate(rows):
        x = left + i * (bar_w + gap)
        y_cursor = top + plot_h
        for value, color in zip(
            (
                r["constant_operand_rules"],
                r["non_constant_operand_rules"],
                r["assert_rules"],
            ),
            _COLORS,
        ):
            if value <= 0:
                continue
            h = plot_h * value / y_top
            y_cursor -= h
            parts.append(
                f"<rect x='{x}' y='{y_cursor:.1f}' width='{bar_w}' height='{h:.1f}' "
                f"fill='{color}'><title>{r['task_id']}: {r['total_rules']} rules</title></rect>"
            )
    parts.append(
        f"<line x1='{left}' y1='{top + plot_h}' x2='{left + plot_w}' y2='{top + plot_h}' "
        "stroke='#333' stroke-width='1'/>"
    )
    parts.append(
        f"<text x='{left + plot_w / 2:.0f}' y='{height - 12}' text-anchor='middle' "
        "fill='#333'>tasks (ルール総数の昇順)</text>"
    )
    parts.append(
        f"<text x='14' y='{top + plot_h / 2:.0f}' text-anchor='middle' fill='#333' "
        f"transform='rotate(-90 14 {top + plot_h / 2:.0f})'>number of rules per task</text>"
    )
    # 凡例
    lx = left
    for label, color in zip(_LABELS, _COLORS):
        parts.append(f"<rect x='{lx}' y='16' width='11' height='11' fill='{color}'/>")
        parts.append(f"<text x='{lx + 16}' y='26' fill='#333'>{label}</text>")
        lx += 16 + len(label) * 12
    parts.append("</svg>")
    path.write_text("\n".join(parts) + "\n")


def write_all(
    outcomes: list[TaskOutcome],
    summary: dict,
    out_dir: Path,
    *,
    dsl_profile: str,
    model: str,
    skipped: dict[str, str],
) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = figure9_rows(outcomes)
    paths = {
        "report": out_dir / "report.md",
        "results": out_dir / "results.json",
        "figure9_csv": out_dir / "figure9_rules.csv",
        "figure9_svg": out_dir / "figure9_rules.svg",
    }
    paths["report"].write_text(
        render_markdown(
            summary, outcomes, dsl_profile=dsl_profile, model=model, skipped=skipped
        )
    )
    paths["results"].write_text(
        json.dumps(
            {
                "config": {
                    "dsl_profile": dsl_profile,
                    "planner_model": model,
                    "skipped_suites": skipped,
                },
                "summary": summary,
                "tasks": [o.to_dict() for o in outcomes],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    write_figure9_csv(rows, paths["figure9_csv"])
    write_figure9_svg(rows, paths["figure9_svg"])
    return paths
