#!/usr/bin/env python3
"""Fail if a season literal appears in executable code.

A hardcoded season is the failure mode this project exists to avoid, and it is
silent: `spring_2027` keeps working right up until it does not, and then matches
nothing with no error to notice.

This parses the AST rather than grepping, because the honest version of this
check has to tell the difference between a season used as a value and one
mentioned in a docstring explaining why they are forbidden.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

SEASON = re.compile(r"\b(spring|summer|fall|autumn|winter)_(19|20)\d{2}\b", re.IGNORECASE)
ROOT = Path(__file__).resolve().parents[1] / "src"


def docstring_nodes(tree: ast.AST) -> set[int]:
    """Ids of string constants that are docstrings, which are allowed to mention them."""
    allowed: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    allowed.add(id(body[0].value))
    return allowed


def main() -> int:
    offences: list[str] = []
    for path in sorted(ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(), str(path))
        allowed = docstring_nodes(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in allowed:
                continue
            if SEASON.search(node.value):
                offences.append(f"{path.relative_to(ROOT.parent.parent)}:{node.lineno}: {node.value!r}")

    if offences:
        print("Hardcoded season literals found in executable code:")
        for line in offences:
            print(f"  {line}")
        print("\nDerive terms from roleradar.terms instead.")
        return 1
    print("no hardcoded season literals in src/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
