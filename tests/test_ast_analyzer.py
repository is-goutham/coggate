import pytest

from src.core.ast_analyzer import AstAnalyzer
from src.core.domain import ChangedFile, FileChangeType


def changed_file(
    *,
    path: str = "src/service.py",
    change_type: FileChangeType = FileChangeType.MODIFIED,
    old: str | None = None,
    new: str | None = None,
    is_binary: bool = False,
    is_generated: bool = False,
) -> ChangedFile:
    return ChangedFile(
        path=path,
        change_type=change_type,
        old_content=old,
        new_content=new,
        is_binary=is_binary,
        is_generated=is_generated,
    )


@pytest.mark.parametrize(
    "file",
    [
        changed_file(path="README.md", new="# docs"),
        changed_file(path="tests/test_service.py", new="def test_it(): pass"),
        changed_file(path="src/service.ts", new="function run() {}"),
        changed_file(path="src/generated/models.py", new="def run(): pass"),
        changed_file(path="src/service.py", is_binary=True),
        changed_file(path="src/service.py", new="def run(): pass", is_generated=True),
    ],
)
def test_is_eligible_file_excludes_non_production_python(file: ChangedFile) -> None:
    assert not AstAnalyzer.is_eligible_file(file)


def test_evaluate_changes_passes_when_complexity_is_unchanged() -> None:
    code = """
def run(value):
    if value:
        return value
    return None
"""

    triggered, reason = AstAnalyzer.evaluate_changes([changed_file(old=code, new=code)])

    assert not triggered
    assert "net delta: +0" in reason


def test_evaluate_changes_triggers_for_net_delta_over_three() -> None:
    new_code = """
def run(a, b, c, d):
    if a:
        work()
    if b:
        work()
    if c:
        work()
    if d:
        work()
"""

    triggered, reason = AstAnalyzer.evaluate_changes(
        [
            changed_file(
                change_type=FileChangeType.ADDED,
                new=new_code,
            )
        ]
    )

    assert triggered
    assert "delta exceeded +3" in reason


def test_evaluate_changes_triggers_for_deep_single_function() -> None:
    new_code = """
def run(a, b, c):
    if a:
        if b:
            if c:
                while c:
                    try:
                        work()
                    except RuntimeError:
                        recover()
"""

    triggered, reason = AstAnalyzer.evaluate_changes(
        [
            changed_file(
                change_type=FileChangeType.ADDED,
                new=new_code,
            )
        ]
    )

    assert triggered
    assert "exceeded cognitive complexity 8" in reason


def test_evaluate_changes_accounts_for_deleted_functions() -> None:
    old_code = """
def removed(a, b, c, d):
    if a:
        work()
    if b:
        work()
    if c:
        work()
    if d:
        work()
"""

    triggered, reason = AstAnalyzer.evaluate_changes(
        [
            changed_file(
                change_type=FileChangeType.DELETED,
                old=old_code,
            )
        ]
    )

    assert not triggered
    assert "net delta: -4" in reason


def test_extract_function_complexities_uses_qualified_names() -> None:
    code = """
class Service:
    def run(self, enabled):
        if enabled:
            return True
        return False
"""

    complexities = AstAnalyzer._extract_function_complexities(code, "service.py")

    assert complexities == {"Service.run": 1}


def test_invalid_python_is_treated_as_no_analyzable_functions() -> None:
    triggered, reason = AstAnalyzer.evaluate_changes(
        [
            changed_file(
                change_type=FileChangeType.ADDED,
                new="def incomplete(",
            )
        ]
    )

    assert not triggered
    assert "net delta: +0" in reason
