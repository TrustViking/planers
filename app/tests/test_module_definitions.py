from __future__ import annotations

import ast
from pathlib import Path

APP_ROOT: Path = Path(__file__).resolve().parents[1]
SKIPPED_DIR: str = "__pycache__"
OVERLOAD_NAME: str = "overload"

Definition = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef


def _is_overload(node: Definition) -> bool:
    for decorator in node.decorator_list:
        if isinstance(decorator, ast.Name) and decorator.id == OVERLOAD_NAME:
            return True
        if isinstance(decorator, ast.Attribute) and decorator.attr == OVERLOAD_NAME:
            return True
    return False


def find_duplicate_definitions(source: str) -> dict[str, list[int]]:
    """Имена, определённые на верхнем уровне модуля больше одного раза, и строки определений."""
    lines_by_name: dict[str, list[int]] = {}
    for node in ast.parse(source).body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if _is_overload(node):
            continue
        lines_by_name.setdefault(node.name, []).append(node.lineno)
    return {name: lines for name, lines in lines_by_name.items() if len(lines) > 1}


def _app_modules() -> list[Path]:
    return sorted(path for path in APP_ROOT.rglob("*.py") if SKIPPED_DIR not in path.parts)


def test_no_module_defines_a_name_twice() -> None:
    problems: list[str] = []
    for path in _app_modules():
        duplicates: dict[str, list[int]] = find_duplicate_definitions(path.read_text(encoding="utf-8"))
        for name, lines in duplicates.items():
            problems.append(f"{path.relative_to(APP_ROOT.parent)}: строки {lines}: {name}")
    assert not problems, "повторные определения:\n" + "\n".join(problems)


def test_finder_reports_second_definition() -> None:
    source: str = "def f(a, b):\n    pass\n\n\nclass C:\n    pass\n\n\ndef f(a):\n    pass\n"
    assert find_duplicate_definitions(source) == {"f": [1, 9]}


def test_finder_ignores_overloads_and_nested_names() -> None:
    source: str = (
        "import typing\n"
        "from typing import overload\n"
        "@overload\n"
        "def f(a: int) -> int: ...\n"
        "@typing.overload\n"
        "def f(a: str) -> str: ...\n"
        "def f(a):\n"
        "    return a\n"
        "class A:\n"
        "    def run(self): ...\n"
        "class B:\n"
        "    def run(self): ...\n"
    )
    assert find_duplicate_definitions(source) == {}
