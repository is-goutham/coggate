from __future__ import annotations

import ast
import re
from pathlib import PurePosixPath

from src.core.domain import ChangedFile, FileChangeType

_EXCLUDED_EXTENSIONS = {
    ".css",
    ".json",
    ".lock",
    ".md",
    ".png",
    ".scss",
    ".txt",
    ".yaml",
    ".yml",
}
_EXCLUDED_PATH_PATTERNS = (
    re.compile(r"(^|/)(generated|vendor)/", re.IGNORECASE),
    re.compile(r"\.(bundle|min)\.js$", re.IGNORECASE),
    re.compile(r"(^|/)(test_[^/]+|[^/]+_(test|spec))\.py$", re.IGNORECASE),
    re.compile(r"\.(test|spec)\.[^.]+$", re.IGNORECASE),
)


class AstAnalyzer:
    @staticmethod
    def is_eligible_file(changed_file: ChangedFile) -> bool:
        normalized_path = changed_file.path.replace("\\", "/")
        suffix = PurePosixPath(normalized_path).suffix.lower()
        if changed_file.is_binary or changed_file.is_generated:
            return False
        if suffix in _EXCLUDED_EXTENSIONS:
            return False
        if suffix != ".py":
            return False
        if any(pattern.search(normalized_path) for pattern in _EXCLUDED_PATH_PATTERNS):
            return False
        return not (
            changed_file.change_type is FileChangeType.RENAMED
            and changed_file.new_content is None
        )

    @classmethod
    def evaluate_changes(cls, changed_files: list[ChangedFile]) -> tuple[bool, str]:
        eligible_files = [item for item in changed_files if cls.is_eligible_file(item)]
        if not eligible_files:
            return False, "No eligible Python code files changed."

        net_complexity_delta = 0
        highest_touched_complexity = 0

        for changed_file in eligible_files:
            old_functions = cls._extract_function_complexities(
                changed_file.old_content or "", changed_file.path
            )
            new_functions = cls._extract_function_complexities(
                changed_file.new_content or "", changed_file.path
            )

            for signature, new_score in new_functions.items():
                old_score = old_functions.get(signature, 0)
                delta = new_score - old_score
                net_complexity_delta += delta
                if delta != 0:
                    highest_touched_complexity = max(
                        highest_touched_complexity, new_score
                    )

            for signature, old_score in old_functions.items():
                if signature not in new_functions:
                    net_complexity_delta -= old_score

        if highest_touched_complexity > 8:
            return (
                True,
                "Triggered: New or modified function exceeded cognitive complexity 8 "
                f"(highest: {highest_touched_complexity}).",
            )
        if net_complexity_delta > 3:
            return (
                True,
                "Triggered: Net pull-request cognitive complexity delta exceeded +3 "
                f"(delta: +{net_complexity_delta}).",
            )
        return (
            False,
            "Passed: Cognitive complexity remained within thresholds "
            f"(net delta: {net_complexity_delta:+d}).",
        )

    @staticmethod
    def _extract_function_complexities(code: str, file_path: str) -> dict[str, int]:
        if not code.strip():
            return {}

        try:
            tree = ast.parse(code, filename=file_path)
        except SyntaxError:
            return {}

        extractor = _PythonFunctionExtractor()
        extractor.visit(tree)
        return extractor.complexities


class _PythonFunctionExtractor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.complexities: dict[str, int] = {}
        self._scope: list[str] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scope.append(node.name)
        for statement in node.body:
            self.visit(statement)
        self._scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._record_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._record_function(node)

    def _record_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        signature = ".".join([*self._scope, node.name])
        self.complexities[signature] = _CognitiveComplexityVisitor().score_body(node.body)

        self._scope.append(node.name)
        for statement in node.body:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.visit(statement)
        self._scope.pop()


class _CognitiveComplexityVisitor:
    def score_body(self, statements: list[ast.stmt]) -> int:
        return sum(self._score_node(statement, nesting=0) for statement in statements)

    def _score_nodes(self, nodes: list[ast.stmt], nesting: int) -> int:
        return sum(self._score_node(node, nesting) for node in nodes)

    def _score_node(self, node: ast.AST, nesting: int) -> int:
        if isinstance(node, ast.If):
            return self._score_if(node, nesting)
        if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            score = 1 + nesting
            score += self._score_nodes(node.body, nesting + 1)
            score += self._score_nodes(node.orelse, nesting + 1)
            return score
        if isinstance(node, ast.Try):
            score = self._score_nodes(node.body, nesting)
            score += self._score_nodes(node.orelse, nesting)
            score += self._score_nodes(node.finalbody, nesting)
            for handler in node.handlers:
                score += 1 + nesting
                score += self._score_nodes(handler.body, nesting + 1)
            return score
        if isinstance(node, ast.Match):
            score = 1 + nesting
            for case in node.cases:
                score += self._score_nodes(case.body, nesting + 1)
            return score
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return 0

        score = 0
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.IfExp):
                score += 1 + nesting
            elif isinstance(child, ast.stmt):
                score += self._score_node(child, nesting)
            else:
                score += self._score_expression(child, nesting)
        return score

    def _score_if(self, node: ast.If, nesting: int) -> int:
        score = 1 + nesting
        score += self._score_nodes(node.body, nesting + 1)
        if len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If):
            score += self._score_if(node.orelse[0], nesting)
        else:
            score += self._score_nodes(node.orelse, nesting + 1)
        return score

    def _score_expression(self, node: ast.AST, nesting: int) -> int:
        score = 1 + nesting if isinstance(node, ast.IfExp) else 0
        return score + sum(
            self._score_expression(child, nesting) for child in ast.iter_child_nodes(node)
        )
