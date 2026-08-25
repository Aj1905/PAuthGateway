"""PAuth 論文第 5 節の再現実験ドライバ。

    .venv/bin/python paper/PAuth/repro/run.py                 # 既定(オフライン)
    .venv/bin/python paper/PAuth/repro/run.py --suites shopping
    .venv/bin/python paper/PAuth/repro/run.py --dsl g2 --out /tmp/repro-g2
    .venv/bin/python paper/PAuth/repro/run.py --allow-api --model gpt-4.1

既定ではキャッシュ済みの計画(`tests/experiment/cache/`)だけを使うので
API キーは要らない。`--allow-api` を付けたときだけ Planner が実際に
モデルを呼ぶ(費用が発生する)。

終了コードは、判定対象タスクで偽陰性が 1 件でもあれば 1、無ければ 0。
偽陽性は終了コードに影響させない(過剰拒否は不便であって権限の漏れでは
ないという論文の立場に合わせる。数値は報告書に出る)。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))        # `repro` パッケージ
sys.path.insert(0, str(_HERE.parents[2]))    # リポジトリ本体(pauth / gateway / benchmarks)

from repro.harness import DSL_PROFILES, PAPER_SUITES, run_suites  # noqa: E402
from repro.report import summarize, write_all  # noqa: E402


def _load_env_file(root: Path) -> None:
    """リポジトリ直下の .env を環境変数へ流し込む(API 利用時のみ意味を持つ)。"""
    import os

    env_path = root / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="PAuth 論文(arXiv:2603.17170)第 5 節の再現実験",
    )
    parser.add_argument(
        "--suites", nargs="+", default=list(PAPER_SUITES),
        help=f"対象スイート(既定: {' '.join(PAPER_SUITES)})",
    )
    parser.add_argument(
        "--dsl", choices=DSL_PROFILES, default="g1",
        help="DSL の版。既定 g1(論文の DSL 相当)。g2 は本リポジトリの拡張版",
    )
    parser.add_argument(
        "--model", default="gpt-4.1",
        help="Planner モデル(--allow-api のときだけ実際に呼ばれる)",
    )
    parser.add_argument(
        "--allow-api", action="store_true",
        help="キャッシュに計画が無いタスクでモデルを呼ぶことを許可する",
    )
    parser.add_argument("--limit", type=int, default=None, help="スイートあたりの最大タスク数")
    parser.add_argument(
        "--out", default=str(_HERE / "results"),
        help="出力ディレクトリ(既定: paper/PAuth/repro/results)",
    )
    parser.add_argument("--quiet", action="store_true", help="タスクごとの進捗を出さない")
    args = parser.parse_args(argv)

    repo_root = _HERE.parents[2]
    if args.allow_api:
        _load_env_file(repo_root)

    print(
        f"PAuth 再現実験 — スイート: {' '.join(args.suites)} / DSL: {args.dsl} / "
        f"Planner: {args.model}{'' if args.allow_api else ' (キャッシュのみ)'}\n"
    )
    outcomes, skipped = run_suites(
        tuple(args.suites),
        model=args.model,
        dsl_profile=args.dsl,
        allow_api=args.allow_api,
        limit=args.limit,
        progress=not args.quiet,
    )
    if not outcomes:
        print("測定対象のタスクが 1 件も無い。", file=sys.stderr)
        return 2

    summary = summarize(outcomes)
    paths = write_all(
        outcomes, summary, Path(args.out),
        dsl_profile=args.dsl, model=args.model, skipped=skipped,
    )

    overall = summary["overall"]
    print()
    print(
        f"判定対象 {overall['tasks_checkable']}/{overall['tasks_total']} タスク · "
        f"良性試行 {overall['benign_runs']} · 強制注入 {overall['forced_injections']}"
    )
    print(
        f"偽陰性(許可された注入) {overall['false_negatives']} · "
        f"偽陽性(拒否された良性タスク) {overall['false_positives']}"
    )
    for name, path in paths.items():
        print(f"  {name}: {path}")

    if overall["false_negatives"]:
        print("\n結果: 不一致 — 論文の偽陰性 0 を再現できていない。")
        return 1
    print("\n結果: この測定範囲では偽陰性 0(論文の表 2 と同方向)。")
    print("      被覆率と注入集合は論文と同一ではない。report.md の差分節を読むこと。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
