"""roleradar makes no LLM calls. This test is what keeps that true.

Zero-LLM is the product's main practical advantage: no API key, no local model,
no GPU, and an install that finishes in seconds. That property is easy to lose
one convenient import at a time, so it is asserted rather than documented.

If you are here because this test failed: the fix is not to add the module to
the allowlist. It is to solve the problem deterministically, the way
classify.py, advise.py, and requirements.py already do.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[1] / "src" / "roleradar"

# Import roots that mean a network LLM call is in play somewhere.
FORBIDDEN_IMPORTS = {
    "litellm", "openai", "anthropic", "google.generativeai", "google.genai",
    "cohere", "mistralai", "ollama", "transformers", "llama_cpp",
    "langchain", "langchain_openai", "llama_index", "sentence_transformers",
    "tiktoken", "vertexai", "boto3",
}

# Strings that betray an LLM endpoint even when the client is hand-rolled.
FORBIDDEN_URL = re.compile(
    r"api\.openai\.com|api\.anthropic\.com|generativelanguage\.googleapis"
    r"|localhost:11434|127\.0\.0\.1:11434|/v1/chat/completions",
    re.I,
)


def _python_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def test_source_tree_is_not_empty() -> None:
    """Guard the guard: an empty glob would make every check below vacuous."""
    assert len(_python_files()) > 5


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: p.name)
def test_module_imports_no_llm_client(path: Path) -> None:
    tree = ast.parse(path.read_text(), str(path))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    # Compare on the dotted root so `openai.types.chat` is caught too.
    offenders = {
        name for name in imported
        if any(name == bad or name.startswith(bad + ".") for bad in FORBIDDEN_IMPORTS)
    }
    assert not offenders, f"{path.name} imports an LLM client: {sorted(offenders)}"


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: p.name)
def test_module_contains_no_llm_endpoint(path: Path) -> None:
    # This file names the endpoints in order to forbid them.
    if path.name == "test_no_llm.py":
        return
    match = FORBIDDEN_URL.search(path.read_text())
    assert match is None, f"{path.name} references an LLM endpoint: {match.group(0)!r}"


def test_declared_dependencies_carry_no_llm_sdk() -> None:
    """A transitive pull is still a pull. Check what we actually declare."""
    import tomllib

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text())
    declared = list(data["project"]["dependencies"])
    for extra in data["project"].get("optional-dependencies", {}).values():
        declared.extend(extra)

    names = {re.split(r"[<>=!\[ ]", d, maxsplit=1)[0].strip().lower() for d in declared}
    assert not (names & {b.replace("_", "-") for b in FORBIDDEN_IMPORTS}), (
        f"pyproject declares an LLM SDK: {sorted(names & FORBIDDEN_IMPORTS)}"
    )
