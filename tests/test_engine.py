from __future__ import annotations

from typing import cast

import pytest

from src.core.domain import (
    ChangedFile,
    CogGateQuizPayload,
    EventType,
    FileChangeType,
    GitHubPlatformContext,
    IPlatformAdapter,
    NormalizedPrEvent,
    PlatformContext,
    QuestionItem,
    QuizOptions,
    QuizPhase,
    QuizState,
    TenantConfig,
)
from src.core.engine import CogGateEngine, EngineOutcome, QuizGenerator, StateStore


class FakeAdapter(IPlatformAdapter):
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.completed_contexts: list[PlatformContext] = []
        self.current_head = "abc123"
        self.fail_fetch = False
        self.fail_completion = False
        self.fail_update = False

    async def create_check_run(
        self, context: PlatformContext, description: str
    ) -> PlatformContext:
        self.calls.append(("create_check", description))
        assert isinstance(context, GitHubPlatformContext)
        return context.model_copy(update={"check_run_id": 99})

    async def complete_check_run(
        self, context: PlatformContext, description: str
    ) -> None:
        self.calls.append(("complete_check", description))
        self.completed_contexts.append(context)
        if self.fail_completion:
            raise RuntimeError("platform unavailable")

    async def fetch_changed_files(
        self, context: PlatformContext
    ) -> list[ChangedFile]:
        self.calls.append(("fetch_files", context.head_commit_sha))
        if self.fail_fetch:
            raise RuntimeError("fetch failed")
        return [
            ChangedFile(
                path="src/service.py",
                change_type=FileChangeType.ADDED,
                new_content="def run(): pass",
            )
        ]

    async def get_current_pr_head(self, context: PlatformContext) -> str:
        self.calls.append(("get_head", context.head_commit_sha))
        return self.current_head

    async def post_quiz_comment(
        self, context: PlatformContext, markdown: str
    ) -> PlatformContext:
        self.calls.append(("post_quiz", markdown))
        assert isinstance(context, GitHubPlatformContext)
        return context.model_copy(update={"quiz_comment_id": 100})

    async def post_reply_comment(
        self, context: PlatformContext, markdown: str
    ) -> None:
        self.calls.append(("post_reply", markdown))

    async def update_quiz_comment(
        self, context: PlatformContext, markdown: str
    ) -> None:
        self.calls.append(("update_quiz", markdown))
        if self.fail_update:
            raise RuntimeError("comment update failed")


class FakeQuizGenerator:
    def __init__(self, quiz: CogGateQuizPayload | None = None) -> None:
        self.quiz = quiz or quiz_payload()
        self.calls = 0

    async def generate_quiz(
        self, changed_files: list[ChangedFile]
    ) -> CogGateQuizPayload:
        self.calls += 1
        return self.quiz


class FakeStateStore:
    def __init__(self) -> None:
        self.claimed: set[str] = set()
        self.event_owners: dict[str, str] = {}
        self.completed: set[str] = set()
        self.abandoned: set[str] = set()
        self.lease_available = True
        self.lease_owned = True
        self.released_tokens: list[str] = []
        self.states: dict[str, QuizState] = {}
        self.active_sha: str | None = None
        self.cooldown = False
        self.fail_complete_event = False

    async def claim_event(self, event_id: str) -> str | None:
        if event_id in self.claimed:
            return None
        self.claimed.add(event_id)
        owner = f"owner-{event_id}"
        self.event_owners[event_id] = owner
        return owner

    async def complete_event(self, event_id: str, owner: str) -> bool:
        if self.fail_complete_event:
            raise RuntimeError("event completion failed")
        if self.event_owners.get(event_id) != owner:
            return False
        self.completed.add(event_id)
        return True

    async def abandon_event(self, event_id: str, owner: str) -> bool:
        if self.event_owners.get(event_id) != owner:
            return False
        self.abandoned.add(event_id)
        self.claimed.discard(event_id)
        self.event_owners.pop(event_id, None)
        return True

    async def acquire_lease(self, context: PlatformContext) -> str | None:
        return "lease-token" if self.lease_available else None

    async def renew_lease(
        self, context: PlatformContext, token: str
    ) -> bool:
        return self.lease_owned and token == "lease-token"

    async def release_lease(
        self, context: PlatformContext, token: str
    ) -> bool:
        self.released_tokens.append(token)
        return True

    async def save_quiz_state(
        self,
        context: PlatformContext,
        state: QuizState,
        lease_token: str | None = None,
    ) -> None:
        if lease_token is not None and (
            not self.lease_owned or lease_token != "lease-token"
        ):
            raise RuntimeError(
                "Quiz state was not saved because lease ownership was lost."
            )
        self.states[state.head_commit_sha] = state
        self.active_sha = state.head_commit_sha

    async def get_active_sha(self, context: PlatformContext) -> str | None:
        return self.active_sha

    async def get_quiz_state(
        self, context: PlatformContext, head_commit_sha: str | None = None
    ) -> QuizState | None:
        if head_commit_sha is None:
            return None
        return self.states.get(head_commit_sha)

    async def is_in_cooldown(
        self, context: PlatformContext, user_id: str
    ) -> bool:
        return self.cooldown

    async def record_failed_attempt(
        self, context: PlatformContext, head_commit_sha: str, user_id: str
    ) -> int:
        state = self.states[head_commit_sha]
        updated = state.model_copy(update={"attempts": state.attempts + 1})
        self.states[head_commit_sha] = updated
        self.cooldown = True
        return updated.attempts

    async def clear_quiz_state(
        self, context: PlatformContext, head_commit_sha: str
    ) -> None:
        self.states.pop(head_commit_sha, None)
        if self.active_sha == head_commit_sha:
            self.active_sha = None


def context() -> GitHubPlatformContext:
    return GitHubPlatformContext(
        tenant_id="installation-123",
        owner="octo-org",
        repo="service",
        pr_number=42,
        pr_author_id="author-1",
        head_commit_sha="abc123",
    )


def event(
    *,
    event_id: str = "delivery-1",
    event_type: EventType = EventType.PR_UPDATED,
    author: str = "author-1",
    text: str = "/coggate answer q1=B",
    bot: bool = False,
) -> NormalizedPrEvent:
    values: dict[str, object] = {
        "event_id": event_id,
        "event_type": event_type,
        "context": context(),
        "is_bot_comment": bot,
    }
    if event_type is EventType.COMMENT_CREATED:
        values.update(
            {
                "comment_author_id": author,
                "comment_text": text,
                "comment_id": f"comment-{event_id}",
            }
        )
    return NormalizedPrEvent.model_validate(values)


def quiz_payload(*, gate_triggered: bool = True) -> CogGateQuizPayload:
    questions = (
        [
            QuestionItem(
                id="q1",
                question_text="What state remains if the write fails?",
                options=QuizOptions(
                    A="No state changes",
                    B="The cache remains stale",
                    C="The process exits",
                ),
                correct_option="B",
            )
        ]
        if gate_triggered
        else []
    )
    return CogGateQuizPayload(
        gate_triggered=gate_triggered, questions=questions
    )


def engine(
    adapter: FakeAdapter,
    state: FakeStateStore,
    generator: FakeQuizGenerator,
    *,
    should_trigger: bool = True,
) -> CogGateEngine:
    return CogGateEngine(
        adapter=adapter,
        state_store=cast(StateStore, state),
        quiz_generator=cast(QuizGenerator, generator),
        tenant_config=TenantConfig(tenant_id="installation-123", is_active=True),
        analyze_changes=lambda _: (should_trigger, "analysis result"),
    )


def seed_published_state(
    state: FakeStateStore, *, attempts: int = 0
) -> None:
    quiz_state = QuizState(
        phase=QuizPhase.PUBLISHED,
        head_commit_sha="abc123",
        pr_author_id="author-1",
        questions={"q1": "B"},
        attempts=attempts,
        platform_check_id=99,
        platform_thread_id=100,
    )
    state.states["abc123"] = quiz_state
    state.active_sha = "abc123"


@pytest.mark.asyncio
async def test_low_complexity_update_completes_without_quiz() -> None:
    adapter = FakeAdapter()
    state = FakeStateStore()
    generator = FakeQuizGenerator()

    outcome = await engine(
        adapter, state, generator, should_trigger=False
    ).process_event(event())

    assert outcome is EngineOutcome.PASSED
    assert generator.calls == 0
    assert [call[0] for call in adapter.calls] == [
        "create_check",
        "fetch_files",
        "complete_check",
    ]
    assert state.completed == {"delivery-1"}
    assert state.released_tokens == ["lease-token"]


@pytest.mark.asyncio
async def test_risky_update_publishes_commit_bound_quiz() -> None:
    adapter = FakeAdapter()
    state = FakeStateStore()
    generator = FakeQuizGenerator()

    outcome = await engine(adapter, state, generator).process_event(event())

    assert outcome is EngineOutcome.QUIZ_PUBLISHED
    published = state.states["abc123"]
    assert published.phase is QuizPhase.PUBLISHED
    assert published.questions == {"q1": "B"}
    assert published.platform_check_id == 99
    assert published.platform_thread_id == 100
    assert [call[0] for call in adapter.calls] == [
        "create_check",
        "fetch_files",
        "get_head",
        "get_head",
        "post_quiz",
    ]


@pytest.mark.asyncio
async def test_stale_sha_completes_old_check_without_publishing() -> None:
    adapter = FakeAdapter()
    adapter.current_head = "newer-sha"
    state = FakeStateStore()

    outcome = await engine(
        adapter, state, FakeQuizGenerator()
    ).process_event(event())

    assert outcome is EngineOutcome.STALE_BYPASSED
    assert state.active_sha is None
    assert "post_quiz" not in [call[0] for call in adapter.calls]
    assert adapter.calls[-1][0] == "complete_check"


@pytest.mark.asyncio
async def test_duplicate_event_has_no_side_effects() -> None:
    adapter = FakeAdapter()
    state = FakeStateStore()
    state.claimed.add("delivery-1")

    outcome = await engine(
        adapter, state, FakeQuizGenerator()
    ).process_event(event())

    assert outcome is EngineOutcome.DUPLICATE
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_busy_lease_abandons_event_for_retry() -> None:
    adapter = FakeAdapter()
    state = FakeStateStore()
    state.lease_available = False

    outcome = await engine(
        adapter, state, FakeQuizGenerator()
    ).process_event(event())

    assert outcome is EngineOutcome.BUSY
    assert state.abandoned == {"delivery-1"}
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_only_author_can_answer() -> None:
    adapter = FakeAdapter()
    state = FakeStateStore()
    seed_published_state(state)

    outcome = await engine(
        adapter, state, FakeQuizGenerator()
    ).process_event(
        event(event_type=EventType.COMMENT_CREATED, author="other-user")
    )

    assert outcome is EngineOutcome.IGNORED
    assert state.states["abc123"].attempts == 0
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_correct_answer_completes_and_clears_gate() -> None:
    adapter = FakeAdapter()
    state = FakeStateStore()
    seed_published_state(state)

    outcome = await engine(
        adapter, state, FakeQuizGenerator()
    ).process_event(event(event_type=EventType.COMMENT_CREATED))

    assert outcome is EngineOutcome.VERIFIED
    assert state.active_sha is None
    assert [call[0] for call in adapter.calls] == [
        "complete_check",
        "update_quiz",
    ]


@pytest.mark.asyncio
async def test_incorrect_answer_records_attempt_and_cooldown() -> None:
    adapter = FakeAdapter()
    state = FakeStateStore()
    seed_published_state(state)

    outcome = await engine(
        adapter, state, FakeQuizGenerator()
    ).process_event(
        event(
            event_type=EventType.COMMENT_CREATED,
            text="/coggate answer q1=A",
        )
    )

    assert outcome is EngineOutcome.INCORRECT
    assert state.states["abc123"].attempts == 1
    assert state.cooldown
    assert adapter.calls[0][0] == "post_reply"


@pytest.mark.asyncio
async def test_cooldown_does_not_increment_attempts() -> None:
    adapter = FakeAdapter()
    state = FakeStateStore()
    seed_published_state(state, attempts=1)
    state.cooldown = True

    outcome = await engine(
        adapter, state, FakeQuizGenerator()
    ).process_event(
        event(
            event_type=EventType.COMMENT_CREATED,
            text="/coggate answer q1=A",
        )
    )

    assert outcome is EngineOutcome.COOLDOWN
    assert state.states["abc123"].attempts == 1


@pytest.mark.asyncio
async def test_max_attempts_bypasses_and_clears_gate() -> None:
    adapter = FakeAdapter()
    state = FakeStateStore()
    seed_published_state(state, attempts=3)

    outcome = await engine(
        adapter, state, FakeQuizGenerator()
    ).process_event(event(event_type=EventType.COMMENT_CREATED))

    assert outcome is EngineOutcome.MAX_ATTEMPTS_BYPASSED
    assert state.active_sha is None
    assert [call[0] for call in adapter.calls] == [
        "complete_check",
        "update_quiz",
    ]


@pytest.mark.asyncio
async def test_final_incorrect_attempt_immediately_bypasses_gate() -> None:
    adapter = FakeAdapter()
    state = FakeStateStore()
    seed_published_state(state, attempts=2)

    outcome = await engine(
        adapter, state, FakeQuizGenerator()
    ).process_event(
        event(
            event_type=EventType.COMMENT_CREATED,
            text="/coggate answer q1=A",
        )
    )

    assert outcome is EngineOutcome.MAX_ATTEMPTS_BYPASSED
    assert state.active_sha is None
    assert [call[0] for call in adapter.calls] == [
        "complete_check",
        "update_quiz",
    ]


@pytest.mark.asyncio
async def test_primary_failure_completes_check_fail_open() -> None:
    adapter = FakeAdapter()
    adapter.fail_fetch = True
    state = FakeStateStore()

    outcome = await engine(
        adapter, state, FakeQuizGenerator()
    ).process_event(event())

    assert outcome is EngineOutcome.FAILED_OPEN
    assert state.completed == {"delivery-1"}
    assert state.released_tokens == ["lease-token"]
    assert adapter.calls[-1] == (
        "complete_check",
        "CogGate bypassed due to an internal system failure.",
    )
    recovery_context = adapter.completed_contexts[-1]
    assert isinstance(recovery_context, GitHubPlatformContext)
    assert recovery_context.check_run_id == 99


@pytest.mark.asyncio
async def test_comment_failure_uses_hydrated_context_for_fail_open() -> None:
    adapter = FakeAdapter()
    adapter.fail_update = True
    state = FakeStateStore()
    seed_published_state(state)

    with pytest.raises(RuntimeError, match="comment update failed"):
        await engine(
            adapter, state, FakeQuizGenerator()
        ).process_event(event(event_type=EventType.COMMENT_CREATED))

    assert state.active_sha == "abc123"
    assert state.states["abc123"].questions == {"q1": "B"}
    assert state.abandoned == {"delivery-1"}


@pytest.mark.asyncio
async def test_failed_fail_open_abandons_event_and_raises() -> None:
    adapter = FakeAdapter()
    adapter.fail_fetch = True
    adapter.fail_completion = True
    state = FakeStateStore()

    with pytest.raises(RuntimeError, match="platform unavailable"):
        await engine(
            adapter, state, FakeQuizGenerator()
        ).process_event(event())

    assert state.abandoned == {"delivery-1"}
    assert state.completed == set()
    assert state.released_tokens == ["lease-token"]


@pytest.mark.asyncio
async def test_event_completion_failure_does_not_destroy_published_quiz() -> None:
    adapter = FakeAdapter()
    state = FakeStateStore()
    state.fail_complete_event = True

    with pytest.raises(RuntimeError, match="event completion failed"):
        await engine(
            adapter, state, FakeQuizGenerator()
        ).process_event(event())

    assert state.states["abc123"].phase is QuizPhase.PUBLISHED
    assert not adapter.completed_contexts
    assert state.abandoned == {"delivery-1"}


@pytest.mark.asyncio
async def test_retry_with_published_state_does_not_duplicate_quiz() -> None:
    adapter = FakeAdapter()
    state = FakeStateStore()
    seed_published_state(state)

    outcome = await engine(
        adapter, state, FakeQuizGenerator()
    ).process_event(event())

    assert outcome is EngineOutcome.QUIZ_PUBLISHED
    assert adapter.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase", [QuizPhase.GENERATING, QuizPhase.PENDING_PUBLICATION]
)
async def test_retry_recovers_incomplete_publication(
    phase: QuizPhase,
) -> None:
    adapter = FakeAdapter()
    state = FakeStateStore()
    state.states["abc123"] = QuizState(
        phase=phase,
        head_commit_sha="abc123",
        pr_author_id="author-1",
        questions={"q1": "B"} if phase is QuizPhase.PENDING_PUBLICATION else {},
        platform_check_id=99,
    )
    state.active_sha = "abc123"

    outcome = await engine(
        adapter, state, FakeQuizGenerator()
    ).process_event(event())

    assert outcome is EngineOutcome.FAILED_OPEN
    assert state.active_sha is None
    assert [call[0] for call in adapter.calls] == ["complete_check"]


@pytest.mark.asyncio
async def test_lost_lease_retires_own_check_without_clearing_state() -> None:
    adapter = FakeAdapter()
    state = FakeStateStore()
    state.lease_owned = False

    with pytest.raises(RuntimeError, match="lease ownership was lost"):
        await engine(
            adapter, state, FakeQuizGenerator()
        ).process_event(event())

    assert "post_quiz" not in [call[0] for call in adapter.calls]
    assert adapter.calls[-1] == (
        "complete_check",
        "CogGate processing was superseded by another worker.",
    )
    assert state.active_sha is None


@pytest.mark.asyncio
async def test_partial_answer_does_not_consume_attempt() -> None:
    adapter = FakeAdapter()
    state = FakeStateStore()
    quiz_state = QuizState(
        phase=QuizPhase.PUBLISHED,
        head_commit_sha="abc123",
        pr_author_id="author-1",
        questions={"q1": "B", "q2": "A"},
        platform_check_id=99,
        platform_thread_id=100,
    )
    state.states["abc123"] = quiz_state
    state.active_sha = "abc123"

    outcome = await engine(
        adapter, state, FakeQuizGenerator()
    ).process_event(event(event_type=EventType.COMMENT_CREATED))

    assert outcome is EngineOutcome.IGNORED
    assert state.states["abc123"].attempts == 0
    assert adapter.calls == [
        ("post_reply", "Answer every question shown in the quiz.")
    ]
