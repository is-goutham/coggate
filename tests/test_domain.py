import pytest
from pydantic import ValidationError

from src.core.domain import (
    AdoPlatformContext,
    ChangedFile,
    CogGateQuizPayload,
    EventType,
    FileChangeType,
    GitHubPlatformContext,
    NormalizedPrEvent,
    QuestionItem,
    QuizOptions,
    QuizPhase,
    QuizState,
    TenantConfig,
)


def github_context() -> GitHubPlatformContext:
    return GitHubPlatformContext(
        tenant_id="installation-123",
        owner="octo-org",
        repo="service",
        pr_number=42,
        pr_author_id="user-7",
        head_commit_sha="abc123",
    )


def question(question_id: str = "q1") -> QuestionItem:
    return QuestionItem(
        id=question_id,
        question_text="What state remains if the downstream write fails?",
        options=QuizOptions(A="No state changes", B="The cache remains stale", C="The write retries"),
        correct_option="B",
    )


def test_tenant_config_uses_isolated_defaults() -> None:
    first = TenantConfig(tenant_id="one", is_active=True)
    second = TenantConfig(tenant_id="two", is_active=True)

    first.allowed_repos.append("octo-org/service")

    assert second.allowed_repos == ["*"]
    assert first.max_llm_tokens == 32_000
    assert first.max_attempts == 3


def test_tenant_config_requires_github_secrets_as_pair() -> None:
    with pytest.raises(ValidationError, match="must be configured together"):
        TenantConfig(
            tenant_id="one",
            is_active=True,
            github_app_id_secret_name="github-app-id",
        )


def test_changed_file_enforces_change_type_invariants() -> None:
    added = ChangedFile(
        path="src/new.py",
        change_type=FileChangeType.ADDED,
        new_content="print('new')",
    )
    assert added.old_content is None

    with pytest.raises(ValidationError, match="cannot contain old content"):
        ChangedFile(
            path="src/new.py",
            change_type=FileChangeType.ADDED,
            old_content="old",
            new_content="new",
        )

    with pytest.raises(ValidationError, match="require old_path"):
        ChangedFile(
            path="src/renamed.py",
            change_type=FileChangeType.RENAMED,
            new_content="new",
        )

    with pytest.raises(ValidationError, match="Binary files"):
        ChangedFile(
            path="asset.bin",
            change_type=FileChangeType.ADDED,
            new_content="not really binary",
            is_binary=True,
        )


def test_platform_context_is_discriminated() -> None:
    event = NormalizedPrEvent.model_validate(
        {
            "event_id": "delivery-1",
            "event_type": EventType.PR_UPDATED,
            "context": {
            "platform": "ado",
            "tenant_id": "contoso",
            "project": "payments",
            "repo_id": "repo-1",
            "pr_id": 12,
            "pr_author_id": "author-1",
            "head_commit_sha": "def456",
            },
        },
    )

    assert isinstance(event.context, AdoPlatformContext)


def test_models_reject_unknown_fields_and_coerced_ids() -> None:
    with pytest.raises(ValidationError):
        GitHubPlatformContext.model_validate(
            {
                "tenant_id": "installation-123",
                "owner": "octo-org",
                "repo": "service",
                "pr_number": "42",
                "pr_author_id": "user-7",
                "head_commit_sha": "abc123",
                "unexpected": True,
            }
        )


@pytest.mark.parametrize(
    ("event_type", "comment_fields"),
    [
        (EventType.COMMENT_CREATED, {}),
        (
            EventType.PR_UPDATED,
            {
                "comment_author_id": "user-7",
                "comment_text": "/coggate answer q1=A",
                "comment_id": "comment-1",
            },
        ),
    ],
)
def test_event_fields_match_event_type(
    event_type: EventType, comment_fields: dict[str, str]
) -> None:
    with pytest.raises(ValidationError):
        NormalizedPrEvent.model_validate(
            {
                "event_id": "delivery-1",
                "event_type": event_type,
                "context": github_context(),
                **comment_fields,
            }
        )


def test_comment_event_requires_complete_comment_identity() -> None:
    event = NormalizedPrEvent(
        event_id="delivery-1",
        event_type=EventType.COMMENT_CREATED,
        context=github_context(),
        comment_author_id="user-7",
        comment_text="/coggate answer q1=A",
        comment_id="comment-1",
    )

    assert event.comment_text == "/coggate answer q1=A"


def test_question_requires_exact_options() -> None:
    with pytest.raises(ValidationError):
        QuestionItem.model_validate(
            {
                "id": "q1",
                "question_text": "What happens?",
                "options": {"A": "One", "B": "Two"},
                "correct_option": "A",
            }
        )

    with pytest.raises(ValidationError):
        question("q4")


def test_quiz_payload_enforces_gate_invariants() -> None:
    assert CogGateQuizPayload(gate_triggered=False).questions == []
    assert len(CogGateQuizPayload(gate_triggered=True, questions=[question()]).questions) == 1

    with pytest.raises(ValidationError, match="requires 1-3"):
        CogGateQuizPayload(gate_triggered=True)

    with pytest.raises(ValidationError, match="No questions allowed"):
        CogGateQuizPayload(gate_triggered=False, questions=[question()])

    with pytest.raises(ValidationError, match="must be unique"):
        CogGateQuizPayload(gate_triggered=True, questions=[question(), question()])


def test_quiz_state_enforces_phase_invariants() -> None:
    generating = QuizState(
        phase=QuizPhase.GENERATING,
        head_commit_sha="abc123",
        pr_author_id="user-7",
    )
    assert generating.questions == {}

    pending = QuizState(
        phase=QuizPhase.PENDING_PUBLICATION,
        head_commit_sha="abc123",
        pr_author_id="user-7",
        questions={"q1": "B"},
        platform_check_id=99,
    )
    assert pending.platform_thread_id is None

    published = QuizState(
        phase=QuizPhase.PUBLISHED,
        head_commit_sha="abc123",
        pr_author_id="user-7",
        questions={"q1": "B"},
        platform_check_id=99,
        platform_thread_id=100,
    )
    assert published.attempts == 0


@pytest.mark.parametrize(
    "state",
    [
        {
            "phase": QuizPhase.GENERATING,
            "head_commit_sha": "abc123",
            "pr_author_id": "user-7",
            "questions": {"q1": "A"},
        },
        {
            "phase": QuizPhase.PENDING_PUBLICATION,
            "head_commit_sha": "abc123",
            "pr_author_id": "user-7",
            "questions": {"q1": "A"},
        },
        {
            "phase": QuizPhase.PUBLISHED,
            "head_commit_sha": "abc123",
            "pr_author_id": "user-7",
            "questions": {"q1": "A"},
            "platform_check_id": 99,
        },
        {
            "phase": QuizPhase.PUBLISHED,
            "head_commit_sha": "abc123",
            "pr_author_id": "user-7",
            "questions": {"q4": "A"},
            "platform_check_id": 99,
            "platform_thread_id": 100,
        },
    ],
)
def test_quiz_state_rejects_invalid_phase_data(state: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        QuizState.model_validate(state)
