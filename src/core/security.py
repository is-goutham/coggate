from __future__ import annotations

import re

from src.core.domain import ChangedFile

REDACTED_SECRET = "[REDACTED_SECRET]"
TRUNCATION_MARKER = "\n[TRUNCATED_DUE_TO_SIZE_LIMIT]"

_SECRET_PATTERNS = (
    re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?"
        r"-----END [A-Z0-9 ]*PRIVATE KEY-----",
        re.DOTALL,
    ),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(
        r"(?i)\b(password|pwd|client_secret|access_token|api_key)\s*[:=]\s*"
        r"([\"']?)[^\s,;\"']+\2"
    ),
    re.compile(
        r"(?i)\b"
        r"(AccountKey|SharedAccessKey|SharedAccessSignature)"
        r"\s*=\s*[^;\s]+"
    ),
)
_MARKDOWN_LINK = re.compile(r"\[([^\]]*)\]\([^)]+\)")
_MENTION = re.compile(r"(?<![\w@])@[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")


def redact_secrets(text: str) -> str:
    """Replace common credential formats before code is sent to an LLM."""
    redacted = text
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(REDACTED_SECRET, redacted)
    return redacted


def sanitize_and_bound_code(
    changed_files: list[ChangedFile], max_chars: int = 100_000
) -> str:
    """Serialize changed text, redact credentials, and enforce a deterministic size bound."""
    if max_chars <= len(TRUNCATION_MARKER):
        raise ValueError("max_chars must be larger than the truncation marker.")

    sections: list[str] = []
    for changed_file in changed_files:
        if changed_file.is_binary or changed_file.is_generated:
            continue

        header = (
            f"FILE: {changed_file.path}\n"
            f"CHANGE_TYPE: {changed_file.change_type.value}\n"
        )
        body_parts = [header]
        if changed_file.old_content is not None:
            body_parts.append(f"OLD_CONTENT:\n{changed_file.old_content}\n")
        if changed_file.new_content is not None:
            body_parts.append(f"NEW_CONTENT:\n{changed_file.new_content}\n")
        sections.append("".join(body_parts))

    redacted = redact_secrets("\n".join(sections))
    if len(redacted) <= max_chars:
        return redacted

    content_limit = max_chars - len(TRUNCATION_MARKER)
    return redacted[:content_limit] + TRUNCATION_MARKER


def build_untrusted_diff_prompt(
    changed_files: list[ChangedFile], max_chars: int = 100_000
) -> str:
    """Place sanitized code inside the prompt-injection trust boundary."""
    code = sanitize_and_bound_code(changed_files, max_chars=max_chars)
    return f"<untrusted_code_diff>\n{code}\n</untrusted_code_diff>"


def sanitize_llm_markdown(text: str) -> str:
    """Neutralize generated links and mentions before publishing platform comments."""
    without_links = _MARKDOWN_LINK.sub(lambda match: match.group(1) or "[LINK REMOVED]", text)
    return _MENTION.sub("[USER REMOVED]", without_links)
