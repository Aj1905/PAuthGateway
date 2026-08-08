#!/usr/bin/env python3
"""Audit a PAuth manuscript against live repository terminology rules."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class Finding:
    severity: str
    rule: str
    line: int
    message: str
    excerpt: str


FORBIDDEN_PATTERNS = (
    (
        r"(?<![A-Za-z0-9_])GrammerValidator(?![A-Za-z0-9_])",
        "TERM_TYPO",
        "GrammerValidator は誤綴り。正本を確認する",
    ),
    (
        r"(?<![A-Za-z0-9_])DSLValidator(?![A-Za-z0-9_])",
        "TERM_LEGACY",
        "DSLValidator は旧称。正本を確認する",
    ),
    (
        r"(?<![A-Za-z0-9_])Compiler(?![A-Za-z0-9_])",
        "TERM_AMBIGUOUS",
        "裸の Compiler は正規の部品名ではない。ルールコンパイラの定義を確認する",
    ),
    (r"制限文法|(?i:restricted\s+grammar)", "TERM_LEGACY", "run コードの言語は DSL と呼ぶ"),
    (r"文法棄却", "FAILURE_TERM", "文法棄却ではなく DSL 棄却を使う"),
    (
        r"(?<![A-Za-z0-9_])(?:REF_REQUIRED_CALLS_PERMITTED|REF_NO_MISSING_CALLS|"
        r"REF_NO_EXCESS_CALLS_PERMITTED|REF_NO_EXCESS_CALLS|REF_EXACT_AUTHORIZATION)(?![A-Za-z0-9_])",
        "METRIC_LEGACY",
        "凍結済みの REF_* 指標を現行結果に使わない",
    ),
    (
        r"(?<![A-Za-z0-9_])(?:POLICY_OVER_GRANT|POLICY_UNDER_GRANT|POLICY_EXACT_GRANT)(?![A-Za-z0-9_])",
        "METRIC_FORBIDDEN",
        "トレース指標に禁止された POLICY_* 名を使わない",
    ),
)

SLOP_PATTERNS = (
    (r"非常に|極めて|画期的|包括的|多岐にわたる", "根拠の薄い評価語を具体的な事実へ置き換える"),
    (r"本節では|以下では|本稿では.*(?:説明|述べる|示す)", "章の予告を削り、内容から始める"),
    (r"注目すべき|重要なことは|言うまでもなく", "重要性を宣言せず、根拠を示す"),
    (r"Here's the thing|Let that sink in|The uncomfortable truth|It turns out", "英語の定型的な強調を削る"),
)

TECHNICAL_TOKEN_RE = re.compile(
    r"`([^`\n]+)`|\\texttt\{([^}\n]+)\}|"
    r"(?<![A-Za-z0-9_])([A-Z][a-z]+(?:[A-Z][A-Za-z0-9]+)+)(?![A-Za-z0-9_])|"
    r"(?<![A-Za-z0-9_])([A-Z][A-Z0-9_]{2,})(?![A-Za-z0-9_])"
)

TOKEN_IGNORE = {
    "AND",
    "ASCII",
    "DOI",
    "IEEE",
    "JSON",
    "JSONL",
    "LLM",
    "PDF",
    "RQ",
    "SVG",
    "URL",
}

KNOWN_BAD_TOKENS = {"Compiler", "DSLValidator", "GrammerValidator"}


def repo_root(start: Path) -> Path:
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=start,
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError("Git リポジトリのルートを特定できない")
    return Path(proc.stdout.strip()).resolve()


def line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def excerpt_at(text: str, offset: int) -> str:
    start = text.rfind("\n", 0, offset) + 1
    end = text.find("\n", offset)
    if end < 0:
        end = len(text)
    return text[start:end].strip()[:180]


def prose_view(text: str) -> str:
    """Blank code while preserving offsets and line numbers."""

    def blank(match: re.Match[str]) -> str:
        return re.sub(r"[^\n]", " ", match.group(0))

    text = re.sub(r"```.*?```", blank, text, flags=re.DOTALL)
    text = re.sub(r"\\begin\{(?:verbatim|lstlisting|minted)\}.*?\\end\{(?:verbatim|lstlisting|minted)\}", blank, text, flags=re.DOTALL)
    text = re.sub(r"`[^`\n]+`", blank, text)
    text = re.sub(r"\\texttt\{[^}\n]+\}", blank, text)
    return text


def manuscript_body_view(text: str) -> str:
    """Blank author metadata and bibliography while preserving offsets."""

    def blank_value(value: str) -> str:
        return re.sub(r"[^\n]", " ", value)

    def blank_match(match: re.Match[str]) -> str:
        return blank_value(match.group(0))

    first_h2 = re.search(r"(?m)^##\s+", text)
    if first_h2:
        text = blank_value(text[: first_h2.start()]) + text[first_h2.start() :]
    reference_heading = re.search(r"(?mi)^#{1,6}\s+(?:参考文献|references|bibliography)\s*$", text)
    if reference_heading:
        text = text[: reference_heading.start()] + blank_value(text[reference_heading.start() :])
    text = re.sub(r"\\(?:title|author|affiliation|email)\{.*?\}", blank_match, text, flags=re.DOTALL)
    return text


def detect_language(text: str) -> str:
    japanese = len(re.findall(r"[ぁ-んァ-ヶ一-龠]", text))
    letters = len(re.findall(r"[A-Za-zぁ-んァ-ヶ一-龠]", text))
    return "ja" if letters and japanese / letters >= 0.08 else "en"


def normalize_candidate(raw: str) -> Optional[str]:
    token = raw.strip().strip("*_")
    token = re.sub(r"\(.*\)$", "", token).strip()
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_*-]{2,}", token) is None:
        return None
    if token.upper() in TOKEN_IGNORE:
        return None
    if re.fullmatch(r"[IVXLCDM]+", token):
        return None
    if re.fullmatch(r"(?:RQ|H|G|P|S|R|E|C|GW)\d+(?:-\d+)?", token, re.IGNORECASE):
        return None
    if token.lower() in {"begin", "end", "figure", "section", "subsection", "table", "textbf"}:
        return None
    return token


def candidate_terms(text: str) -> dict[str, int]:
    found: dict[str, int] = {}
    for match in TECHNICAL_TOKEN_RE.finditer(text):
        raw = next((group for group in match.groups() if group is not None), "")
        token = normalize_candidate(raw)
        if token and token not in found:
            found[token] = match.start()
    return found


def audit_text(text: str, system_model: str, language: str) -> list[Finding]:
    findings: list[Finding] = []
    body = manuscript_body_view(text)
    prose = prose_view(body)

    for pattern, rule, message in FORBIDDEN_PATTERNS:
        for match in re.finditer(pattern, prose):
            findings.append(
                Finding("ERROR", rule, line_number(text, match.start()), message, excerpt_at(text, match.start()))
            )

    for match in re.finditer(r"(?<!tool\s)(?<![A-Za-z0-9_])calls?(?![A-Za-z0-9_])", prose, flags=re.IGNORECASE):
        prefix = prose[max(0, match.start() - 24) : match.start()].lower()
        suffix = prose[match.end() : match.end() + 16].lower()
        if re.search(r"(?:\bwe|\bi|\bthey|\bauthors?)\s+$", prefix) and re.match(r"\s+(?:this|it|the)\b", suffix):
            continue
        findings.append(
            Finding(
                "WARNING",
                "BARE_CALL",
                line_number(text, match.start()),
                "裸の call ではなく tool call / ツール呼び出しを使う",
                excerpt_at(text, match.start()),
            )
        )

    paragraphs: list[tuple[int, str]] = []
    cursor = 0
    for paragraph in re.split(r"\n[ \t]*\n", prose):
        start = prose.find(paragraph, cursor)
        if start < 0:
            continue
        cursor = start + len(paragraph)
        if paragraph.strip():
            paragraphs.append((start, paragraph))
    for start, paragraph in paragraphs:
        if re.search(r"(?:原論文|PAuth\s*論文|original\s+paper)", paragraph, re.IGNORECASE) and re.search(
            r"(?<![A-Za-z0-9_])DSL(?![A-Za-z0-9_])", paragraph
        ):
            if not re.search(r"(?<![A-Za-z0-9_])G[12](?![A-Za-z0-9_])", paragraph):
                findings.append(
                    Finding(
                        "WARNING",
                        "DSL_VERSION",
                        line_number(text, start),
                        "原論文と比較する DSL には G1 / G2 の版を付ける",
                        excerpt_at(text, start),
                    )
                )
        if re.search(r"(?:ToolExecutor|ツールアダプタ|Confirmer)", paragraph) and re.search(
            r"(?:ノード|段階|pipeline\s+node|stage)", paragraph, re.IGNORECASE
        ):
            findings.append(
                Finding(
                    "WARNING",
                    "COMPONENT_CLASS",
                    line_number(text, start),
                    "実行部、ツールアダプタ、確認器をパイプラインのノードや段階に数えていないか確認する",
                    excerpt_at(text, start),
                )
            )

    if language == "ja":
        for term in ("Planner", "GrammarValidator", "Slicer", "Enforcer", "EnvelopeStore"):
            for match in re.finditer(rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])", prose):
                findings.append(
                    Finding(
                        "WARNING",
                        "ENGLISH_PROSE_NAME",
                        line_number(text, match.start()),
                        f"日本語の地の文では {term} の正本上の日本語名を使う",
                        excerpt_at(text, match.start()),
                    )
                )

    for pattern, message in SLOP_PATTERNS:
        for match in re.finditer(pattern, prose, flags=re.IGNORECASE):
            findings.append(
                Finding("WARNING", "PROSE_SLOP", line_number(text, match.start()), message, excerpt_at(text, match.start()))
            )

    system_fold = system_model.casefold()
    for token, offset in candidate_terms(body).items():
        if token in KNOWN_BAD_TOKENS:
            continue
        if token.casefold() not in system_fold:
            findings.append(
                Finding(
                    "ERROR",
                    "UNDEFINED_TERM",
                    line_number(text, offset),
                    f"専門語候補 {token!r} が docs/SYSTEM_MODEL.md に見つからない。本文より先に正本へ定義する",
                    excerpt_at(text, offset),
                )
            )

    return sorted(findings, key=lambda item: (item.line, item.severity, item.rule, item.message))


def self_test() -> int:
    model = "DSL GrammarValidator 文法検証器 ツール呼び出し tool call"
    bad = "文法棄却を行うGrammerValidatorはrestricted grammarを検査する。NovelResearchTokenがcallを作る。"
    rules = {finding.rule for finding in audit_text(bad, model, "ja")}
    expected = {"TERM_TYPO", "TERM_LEGACY", "FAILURE_TERM", "UNDEFINED_TERM", "BARE_CALL"}
    missing = expected - rules
    if missing:
        print("self-test failed; missing rules:", ", ".join(sorted(missing)), file=sys.stderr)
        return 1
    good = "文法検証器は DSL に従うかを調べる。ツール呼び出しを記録する。"
    if any(finding.severity == "ERROR" for finding in audit_text(good, model, "ja")):
        print("self-test failed; compliant text produced an error", file=sys.stderr)
        return 1
    print("self-test passed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manuscript", nargs="?", type=Path)
    parser.add_argument("--repo-root", type=Path)
    parser.add_argument("--language", choices=("auto", "ja", "en"), default="auto")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        return self_test()
    if args.manuscript is None:
        parser.error("manuscript is required unless --self-test is used")

    manuscript = args.manuscript.resolve()
    if not manuscript.is_file():
        print(f"manuscript not found: {manuscript}", file=sys.stderr)
        return 2

    root = args.repo_root.resolve() if args.repo_root else repo_root(manuscript.parent)
    system_model_path = root / "docs" / "SYSTEM_MODEL.md"
    if not system_model_path.is_file():
        print(f"terminology source not found: {system_model_path}", file=sys.stderr)
        return 2

    text = manuscript.read_text(encoding="utf-8")
    model = system_model_path.read_text(encoding="utf-8")
    language = detect_language(text) if args.language == "auto" else args.language
    findings = audit_text(text, model, language)

    if args.as_json:
        print(json.dumps({"language": language, "findings": [asdict(item) for item in findings]}, ensure_ascii=False, indent=2))
    else:
        for finding in findings:
            print(f"{finding.severity} {manuscript}:{finding.line} [{finding.rule}] {finding.message}")
            if finding.excerpt:
                print(f"  {finding.excerpt}")
        errors = sum(item.severity == "ERROR" for item in findings)
        warnings = sum(item.severity == "WARNING" for item in findings)
        print(f"audit: {errors} error(s), {warnings} warning(s), language={language}")

    return 1 if any(item.severity == "ERROR" for item in findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
