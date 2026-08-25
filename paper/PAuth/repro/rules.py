"""図 9 の分類: ルールを「定数オペランド / 非定数オペランド / assert」に分ける。

論文の図 9 は、タスクの全スライスについて
「定数オペランドのルール数 + 非定数オペランドのルール数 + assert 条件の
ルール数」を積み上げて 1 本の棒にしている。

本リポジトリの :class:`pauth.rule_compiler.Rule` は 1 ツール呼び出しにつき
1 個で、オペランドごとの期待式 ``arg_exprs`` と、連言に分解済みの
``guard`` を持つ。したがって論文の三分類は Rule から決定的に導ける:

* 定数オペランド   -- ``arg_exprs[i]`` がリテラル(またはリテラルのみの
  コンテナ)であるもの
* 非定数オペランド -- それ以外(封筒の値・let 束縛・演算を含む式)
* assert           -- ``guard`` の要素数

この対応付けは本パッケージ固有の解釈であり、論文側の実装が同じ数え方で
あることの保証ではない。図 9 との比較は「桁と分布の一致」までしか主張
できない。
"""

from __future__ import annotations

import ast
import dataclasses

from pauth.rule_compiler import Rule


@dataclasses.dataclass(frozen=True)
class RuleCounts:
    """1 タスク分のルール内訳(図 9 の 1 本の棒)。"""

    constant_operand: int
    non_constant_operand: int
    assertion: int

    @property
    def total(self) -> int:
        return self.constant_operand + self.non_constant_operand + self.assertion

    def to_dict(self) -> dict[str, int]:
        return {
            "constant_operand_rules": self.constant_operand,
            "non_constant_operand_rules": self.non_constant_operand,
            "assert_rules": self.assertion,
            "total_rules": self.total,
        }


def is_literal(expr: ast.expr) -> bool:
    """式がリテラル定数か(定数だけからなるコンテナも含む)。"""
    if isinstance(expr, ast.Constant):
        return True
    if isinstance(expr, (ast.List, ast.Tuple, ast.Set)):
        return all(is_literal(e) for e in expr.elts)
    if isinstance(expr, ast.Dict):
        keys = [k for k in expr.keys if k is not None]
        return all(is_literal(k) for k in keys) and all(
            is_literal(v) for v in expr.values
        )
    if isinstance(expr, ast.UnaryOp) and isinstance(expr.op, (ast.UAdd, ast.USub)):
        # -1 や +2.5 のような負リテラルも定数として数える
        return is_literal(expr.operand)
    return False


def count_rules(rules: list[Rule]) -> RuleCounts:
    """タスクの全ルールを図 9 の三分類で数える。"""
    constant = non_constant = assertion = 0
    for rule in rules:
        for arg in rule.arg_exprs:
            if is_literal(arg):
                constant += 1
            else:
                non_constant += 1
        assertion += len(rule.guard)
    return RuleCounts(constant, non_constant, assertion)
