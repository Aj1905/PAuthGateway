"""Classify each tool's RETURN by whether it is schema-structured.

The product connects tools via MCP / OpenAPI, whose response schemas give A1 a
structured, field-typed return (``object {name: string, rating: number}`` /
``list of object {...}``). AgentDojo, by contrast, has many tools that return a
bare ``str`` -- a human-readable, newline-joined blob. The restricted grammar
cannot split a string (no methods, no loops), so any task whose correct solution
must destructure such a blob is *outside the surface the product targets*, not a
real limitation of it.

This module makes that objective and measurable. It classifies every tool's
return type -- independent of whether A1 happened to pass -- so a scope decision
keys on the tool schema, never on our own success (which would be cherry-picking).

Categories:
* STRUCTURED   -- return is an object / list of objects / typed scalar the plan
                  can field-access. This is what MCP/OpenAPI provide.
* TEXT_BLOB    -- return is ``str`` / ``list of str`` from a data getter: the
                  plan must parse prose the grammar cannot parse. Out of scope
                  under the structured-schema premise.
* STATUS       -- a ``str`` returned by an *action* (send_/book_/create_...): a
                  confirmation message, never destructured. In scope.

Run:  .venv/bin/python -m eval.schema_scope
"""

from __future__ import annotations

from pauth.suites.dining import build_suite as build_dining_suite
from pauth.suites.shopping import build_suite as build_shopping_suite
from benchmarks.agentdojo_adapter import AGENTDOJO_SUITES, load_suite

# Offline, structured-native suites (the product surface: MCP/OpenAPI shapes).
_OFFLINE_SUITES = {"shopping": build_shopping_suite, "dining": build_dining_suite}

STRUCTURED, TEXT_BLOB, STATUS = "STRUCTURED", "TEXT_BLOB", "STATUS"

# A getter's return is data the plan consumes; an action's str return is a status.
_GETTER_PREFIXES = ("get_", "list_", "read_", "search_", "find_", "retrieve_", "show_")
# Markers that a bare-``str`` getter is encoding a COLLECTION or a prose document
# in one string (the case the grammar cannot destructure). A scalar-string getter
# (get_iban -> one IBAN, get_current_day -> one date) is NOT one of these.
_COLLECTION_MARKERS = ("all_", "_information", "webpage", "file", "channels",
                       "users", "messages", "emails", "list", "results")


def classify_return(tool_name: str, returns: str) -> str:
    """Classify a tool's rendered return type. ``returns`` is ToolDoc.returns."""
    r = returns.strip()
    has_fields = "object {" in r or "object<" in r
    is_getter = tool_name.startswith(_GETTER_PREFIXES)
    list_of_text = r.startswith(("list of str", "list of string"))
    bare_text = r in ("str", "string", "str|None", "string|None")
    if has_fields:
        return STRUCTURED
    # A list-of-strings, or a bare-string getter whose name implies a collection /
    # document, is a text blob the plan must parse -> out of scope.
    if list_of_text or (bare_text and is_getter
                        and any(m in tool_name for m in _COLLECTION_MARKERS)):
        return TEXT_BLOB
    if bare_text and not is_getter:
        return STATUS  # an action's confirmation string; never destructured
    # scalar str (IBAN, day), numbers, bool, datetime, None, typed unions: a single
    # value the plan can use/compare directly -> in scope.
    return STRUCTURED


def classify_suite(suite) -> list[tuple[str, str, str]]:
    """Return [(tool_name, rendered_return, category)] for a suite."""
    out = []
    for name in sorted(suite.tools):
        returns = suite.tools[name].doc.returns
        out.append((name, returns, classify_return(name, returns)))
    return out


def main() -> int:
    print("Tool-return schema scope (structured-schema premise)\n")
    grand = {STRUCTURED: 0, TEXT_BLOB: 0, STATUS: 0}
    named = ([(n, _OFFLINE_SUITES[n]()) for n in _OFFLINE_SUITES]
             + [(n, load_suite(n)) for n in AGENTDOJO_SUITES])
    for sname, suite in named:
        rows = classify_suite(suite)
        counts = {STRUCTURED: 0, TEXT_BLOB: 0, STATUS: 0}
        for _, _, cat in rows:
            counts[cat] += 1
            grand[cat] += 1
        blobs = [n for n, _, c in rows if c == TEXT_BLOB]
        print(f"== {sname} ==  {len(rows)} tools  "
              f"[STRUCTURED {counts[STRUCTURED]} | TEXT_BLOB {counts[TEXT_BLOB]} | STATUS {counts[STATUS]}]")
        if blobs:
            print(f"   TEXT_BLOB getters (out of scope): {', '.join(blobs)}")
    print(f"\nOverall: STRUCTURED {grand[STRUCTURED]} | TEXT_BLOB {grand[TEXT_BLOB]} | STATUS {grand[STATUS]}")
    print("\nTEXT_BLOB getters return prose the restricted grammar cannot parse; a")
    print("task whose correct solution must destructure one is out of scope under an")
    print("MCP/OpenAPI structured-schema deployment.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
