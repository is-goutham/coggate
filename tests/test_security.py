import pytest

from src.core.domain import ChangedFile, FileChangeType
from src.core.security import (
    REDACTED_SECRET,
    TRUNCATION_MARKER,
    build_untrusted_diff_prompt,
    sanitize_and_bound_code,
    sanitize_llm_markdown,
)


def changed_file(content: str) -> ChangedFile:
    return ChangedFile(
        path="src/service.py",
        change_type=FileChangeType.ADDED,
        new_content=content,
    )


def test_sanitize_and_bound_code_redacts_common_credentials() -> None:
    content = """
aws_key = "AKIAIOSFODNN7EXAMPLE"
password=super-secret
token = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature123"
-----BEGIN PRIVATE KEY-----
private material
-----END PRIVATE KEY-----
"""

    sanitized = sanitize_and_bound_code([changed_file(content)])

    assert sanitized.count(REDACTED_SECRET) == 4
    assert "super-secret" not in sanitized
    assert "private material" not in sanitized


def test_sanitize_and_bound_code_skips_binary_and_generated_files() -> None:
    files = [
        ChangedFile(
            path="image.png",
            change_type=FileChangeType.ADDED,
            is_binary=True,
        ),
        ChangedFile(
            path="generated.py",
            change_type=FileChangeType.ADDED,
            new_content="password=secret",
            is_generated=True,
        ),
    ]

    assert sanitize_and_bound_code(files) == ""


def test_sanitize_and_bound_code_enforces_exact_limit() -> None:
    sanitized = sanitize_and_bound_code([changed_file("x" * 500)], max_chars=100)

    assert len(sanitized) == 100
    assert sanitized.endswith(TRUNCATION_MARKER)


def test_sanitize_and_bound_code_rejects_impossible_limit() -> None:
    with pytest.raises(ValueError):
        sanitize_and_bound_code([], max_chars=len(TRUNCATION_MARKER))


def test_build_untrusted_diff_prompt_wraps_sanitized_content() -> None:
    prompt = build_untrusted_diff_prompt([changed_file("print('safe')")])

    assert prompt.startswith("<untrusted_code_diff>\n")
    assert prompt.endswith("\n</untrusted_code_diff>")


def test_sanitize_llm_markdown_neutralizes_links_and_mentions() -> None:
    text = "Ask @octocat to open [this page](https://evil.example)."

    assert sanitize_llm_markdown(text) == (
        "Ask [USER REMOVED] to open this page."
    )
