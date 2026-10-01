from __future__ import annotations

import html
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Protocol

from src.core.ast_analyzer import AstAnalyzer
from src.core.domain import (
    AdoPlatformContext,
    ChangedFile,
    CogGateQuizPayload,
    EventType,
    GitHubPlatformContext,
    IPlatformAdapter,
    NormalizedPrEvent,
    PlatformContext,
    QuizPhase,
    QuizState,
    TenantConfig,
)
from src.core.parser import parse_answer_grammar
from src.core.security import sanitize_llm_markdown


class QuizGenerator(Protocol):
    async def generate_quiz(
        self, changed_files: list[ChangedFile]
    ) -> CogGateQuizPayload: ...


class StateStore(Protocol):
    async def claim_event(self, event_id: str) -> str | None: ...

    async def complete_event(self, event_id: str, owner: str) -> bool: ...

    async def abandon_event(self, event_id: str, owner: str) -> bool: ...

    async def acquire_lease(self, context: PlatformContext) -> str | None: ...

    async def renew_lease(self, context: PlatformContext, token: str) -> bool: ...

    async def release_lease(self, context: PlatformContext, token: str) -> bool: ...

    async def save_quiz_state(
        self,
        context: PlatformContext,
        state: QuizState,
        lease_token: str | None = None,
    ) -> None: ...

    async def get_active_sha(self, context: PlatformContext) -> str | None: ...

    async def get_quiz_state(
        self, context: PlatformContext, head_commit_sha: str | None = None
    ) -> QuizState | None: ...

    async def is_in_cooldown(
        self, context: PlatformContext, user_id: str
    ) -> bool: ...

    async def record_failed_attempt(
        self, context: PlatformContext, head_commit_sha: str, user_id: str
    ) -> int: ...

    async def clear_quiz_state(
        self, context: PlatformContext, head_commit_sha: str
    ) -> None: ...


class EngineOutcome(str, Enum):
    IGNORED = "ignored"
    DUPLICATE = "duplicate"
    BUSY = "busy"
    PASSED = "passed"
    QUIZ_PUBLISHED = "quiz_published"
    STALE_BYPASSED = "stale_bypassed"
    VERIFIED = "verified"
    INCORRECT = "incorrect"
    COOLDOWN = "cooldown"
    MAX_ATTEMPTS_BYPASSED = "max_attempts_bypassed"
    FAILED_OPEN = "failed_open"


@dataclass
class _ProcessingContext:
    platform: PlatformContext


class CogGateEngine:
    def __init__(
        self,
        *,
        adapter: IPlatformAdapter,
        state_store: StateStore,
        quiz_generator: QuizGenerator,
        tenant_config: TenantConfig,
        analyze_changes: Callable[
            [list[ChangedFile]], tuple[bool, str]
        ] = AstAnalyzer.evaluate_changes,
        logger: logging.Logger | None = None,
    ) -> None:
        self._adapter = adapter
        self._state = state_store
        self._quiz_generator = quiz_generator
        self._tenant_config = tenant_config
        self._analyze_changes = analyze_changes
        self._logger = logger or logging.getLogger(__name__)

    async def process_event(self, event: NormalizedPrEvent) -> EngineOutcome:
        if self._should_ignore(event):
            return EngineOutcome.IGNORED

        event_owner = await self._state.claim_event(event.event_id)
        if event_owner is None:
            return EngineOutcome.DUPLICATE

        processing = _ProcessingContext(event.context.model_copy(deep=True))
        lease_token: str | None = None
        event_completed = False

        try:
            lease_token = await self._state.acquire_lease(processing.platform)
            if lease_token is None:
                return EngineOutcome.BUSY

            try:
                if event.event_type is EventType.PR_UPDATED:
                    outcome = await self._process_pr_update(
                        processing, lease_token
                    )
                else:
                    outcome = await self._process_comment(
                        event, processing, lease_token
                    )
            except Exception as primary_error:
                self._logger.exception(
                    "CogGate event processing failed",
                    extra={"event_id": event.event_id},
                )
                if (
                    event.event_type is not EventType.PR_UPDATED
                    or _platform_check_id(processing.platform) is None
                ):
                    raise
                if not await self._state.renew_lease(
                    processing.platform, lease_token
                ):
                    await self._retire_platform_artifacts(processing.platform)
                    raise
                try:
                    if _platform_thread_id(processing.platform) is not None:
                        await self._adapter.update_quiz_comment(
                            processing.platform,
                            "CogGate bypassed this quiz due to an internal system failure.",
                        )
                    await self._adapter.complete_check_run(
                        processing.platform,
                        "CogGate bypassed due to an internal system failure.",
                    )
                    await self._state.clear_quiz_state(
                        processing.platform,
                        processing.platform.head_commit_sha,
                    )
                    outcome = EngineOutcome.FAILED_OPEN
                except Exception as recovery_error:
                    self._logger.critical(
                        "CogGate fail-open recovery failed",
                        exc_info=True,
                        extra={"event_id": event.event_id},
                    )
                    raise recovery_error from primary_error

            completed = await self._state.complete_event(
                event.event_id, event_owner
            )
            if not completed:
                raise RuntimeError("Event claim ownership was lost before completion.")
            event_completed = True
            return outcome
        finally:
            if not event_completed:
                await self._state.abandon_event(event.event_id, event_owner)
            if lease_token is not None:
                await self._state.release_lease(
                    processing.platform, lease_token
                )

    async def _process_pr_update(
        self, processing: _ProcessingContext, lease_token: str
    ) -> EngineOutcome:
        existing_state = await self._state.get_quiz_state(
            processing.platform, processing.platform.head_commit_sha
        )
        if existing_state is not None:
            await self._require_lease(processing.platform, lease_token)
            processing.platform = _hydrate_context(
                processing.platform, existing_state
            )
            if existing_state.phase is QuizPhase.PUBLISHED:
                return EngineOutcome.QUIZ_PUBLISHED

            await self._adapter.complete_check_run(
                processing.platform,
                "CogGate bypassed while recovering an incomplete quiz publication.",
            )
            await self._state.clear_quiz_state(
                processing.platform, processing.platform.head_commit_sha
            )
            return EngineOutcome.FAILED_OPEN

        processing.platform = await self._adapter.create_check_run(
            processing.platform, "Analyzing pull-request complexity."
        )
        context = processing.platform
        check_id = _platform_check_id(context)
        if check_id is None:
            raise ValueError("Platform adapter did not return a check identifier.")

        changed_files = await self._adapter.fetch_changed_files(context)
        should_trigger, reason = self._analyze_changes(changed_files)
        if not should_trigger:
            await self._adapter.complete_check_run(context, reason)
            return EngineOutcome.PASSED

        await self._require_lease(processing.platform, lease_token)
        generating_state = QuizState(
            phase=QuizPhase.GENERATING,
            head_commit_sha=context.head_commit_sha,
            pr_author_id=context.pr_author_id,
            platform_check_id=check_id,
        )
        await self._state.save_quiz_state(
            context, generating_state, lease_token
        )

        quiz = await self._quiz_generator.generate_quiz(changed_files)
        if not quiz.gate_triggered:
            await self._adapter.complete_check_run(
                context, "No high-risk architectural paths were identified."
            )
            await self._state.clear_quiz_state(
                context, context.head_commit_sha
            )
            return EngineOutcome.PASSED

        await self._require_lease(processing.platform, lease_token)
        current_head = await self._adapter.get_current_pr_head(context)
        if current_head != context.head_commit_sha:
            await self._adapter.complete_check_run(
                context, "Analysis superseded by a newer pull-request commit."
            )
            await self._state.clear_quiz_state(
                context, context.head_commit_sha
            )
            return EngineOutcome.STALE_BYPASSED

        pending_state = QuizState(
            phase=QuizPhase.PENDING_PUBLICATION,
            head_commit_sha=context.head_commit_sha,
            pr_author_id=context.pr_author_id,
            questions={
                question.id: question.correct_option
                for question in quiz.questions
            },
            platform_check_id=check_id,
        )
        await self._state.save_quiz_state(context, pending_state, lease_token)

        await self._require_lease(processing.platform, lease_token)
        current_head = await self._adapter.get_current_pr_head(context)
        if current_head != context.head_commit_sha:
            await self._adapter.complete_check_run(
                context, "Quiz publication superseded by a newer pull-request commit."
            )
            await self._state.clear_quiz_state(
                context, context.head_commit_sha
            )
            return EngineOutcome.STALE_BYPASSED

        markdown = sanitize_llm_markdown(render_quiz_markdown(quiz))
        processing.platform = await self._adapter.post_quiz_comment(
            context, markdown
        )
        thread_id = _platform_thread_id(processing.platform)
        if thread_id is None:
            raise ValueError("Platform adapter did not return a quiz comment identifier.")

        published_state = pending_state.model_copy(
            update={
                "phase": QuizPhase.PUBLISHED,
                "platform_thread_id": thread_id,
            }
        )
        await self._state.save_quiz_state(
            processing.platform, published_state, lease_token
        )
        return EngineOutcome.QUIZ_PUBLISHED

    async def _process_comment(
        self,
        event: NormalizedPrEvent,
        processing: _ProcessingContext,
        lease_token: str,
    ) -> EngineOutcome:
        context = processing.platform
        if event.comment_author_id != context.pr_author_id:
            return EngineOutcome.IGNORED

        answers = parse_answer_grammar(event.comment_text or "")
        if answers is None:
            return EngineOutcome.IGNORED

        active_sha = await self._state.get_active_sha(context)
        if active_sha is None or active_sha != context.head_commit_sha:
            return EngineOutcome.IGNORED

        state = await self._state.get_quiz_state(context, active_sha)
        if state is None or state.phase is not QuizPhase.PUBLISHED:
            return EngineOutcome.IGNORED

        if event.comment_author_id != state.pr_author_id:
            return EngineOutcome.IGNORED

        processing.platform = _hydrate_context(context, state)
        context = processing.platform
        author_id = event.comment_author_id
        if author_id is None:
            return EngineOutcome.IGNORED

        if await self._state.is_in_cooldown(context, author_id):
            await self._require_lease(context, lease_token)
            await self._adapter.post_reply_comment(
                context,
                "You are in a cooldown following an incorrect attempt.",
            )
            return EngineOutcome.COOLDOWN

        if state.attempts >= self._tenant_config.max_attempts:
            await self._require_lease(context, lease_token)
            return await self._bypass_max_attempts(context, active_sha)

        if answers == state.questions:
            await self._require_lease(context, lease_token)
            await self._adapter.complete_check_run(
                context, "Comprehension verified."
            )
            await self._adapter.update_quiz_comment(
                context, "Comprehension verified. Pull request unlocked."
            )
            await self._state.clear_quiz_state(context, active_sha)
            return EngineOutcome.VERIFIED

        if answers.keys() != state.questions.keys():
            await self._require_lease(context, lease_token)
            await self._adapter.post_reply_comment(
                context, "Answer every question shown in the quiz."
            )
            return EngineOutcome.IGNORED

        await self._require_lease(context, lease_token)
        attempts = await self._state.record_failed_attempt(
            context, active_sha, author_id
        )
        if attempts >= self._tenant_config.max_attempts:
            return await self._bypass_max_attempts(context, active_sha)
        await self._adapter.post_reply_comment(
            context,
            "Incorrect answer. "
            f"Attempt {attempts}/{self._tenant_config.max_attempts}; cooldown active.",
        )
        return EngineOutcome.INCORRECT

    async def _require_lease(
        self, context: PlatformContext, token: str
    ) -> None:
        if not await self._state.renew_lease(context, token):
            raise RuntimeError("Pull-request execution lease ownership was lost.")

    async def _bypass_max_attempts(
        self, context: PlatformContext, active_sha: str
    ) -> EngineOutcome:
        await self._adapter.complete_check_run(
            context,
            "Maximum attempts reached; CogGate bypassed according to tenant policy.",
        )
        await self._adapter.update_quiz_comment(
            context,
            "Comprehension gate bypassed after the maximum number of attempts.",
        )
        await self._state.clear_quiz_state(context, active_sha)
        return EngineOutcome.MAX_ATTEMPTS_BYPASSED

    async def _retire_platform_artifacts(
        self, context: PlatformContext
    ) -> None:
        if _platform_thread_id(context) is not None:
            await self._adapter.update_quiz_comment(
                context,
                "This quiz was superseded before CogGate could finish processing it.",
            )
        await self._adapter.complete_check_run(
            context,
            "CogGate processing was superseded by another worker.",
        )

    @staticmethod
    def _should_ignore(event: NormalizedPrEvent) -> bool:
        if event.is_bot_comment:
            return True
        if event.event_type is EventType.COMMENT_CREATED:
            return parse_answer_grammar(event.comment_text or "") is None
        return False


def render_quiz_markdown(quiz: CogGateQuizPayload) -> str:
    lines = [
        "## CogGate Architectural Comprehension Quiz",
        "",
        "Reply with `/coggate answer q1=A q2=B` using every question shown.",
    ]
    for question in quiz.questions:
        lines.extend(
            (
                "",
                f"### {html.escape(question.id.upper())}",
                html.escape(question.question_text),
                "",
                f"- **A:** {html.escape(question.options.A)}",
                f"- **B:** {html.escape(question.options.B)}",
                f"- **C:** {html.escape(question.options.C)}",
            )
        )
    return "\n".join(lines)


def _platform_check_id(context: PlatformContext) -> int | None:
    if isinstance(context, GitHubPlatformContext):
        return context.check_run_id
    return context.status_id


def _platform_thread_id(context: PlatformContext) -> int | None:
    if isinstance(context, GitHubPlatformContext):
        return context.quiz_comment_id
    return context.thread_id


def _hydrate_context(
    context: PlatformContext, state: QuizState
) -> PlatformContext:
    if isinstance(context, GitHubPlatformContext):
        return context.model_copy(
            update={
                "check_run_id": state.platform_check_id,
                "quiz_comment_id": state.platform_thread_id,
            }
        )
    if isinstance(context, AdoPlatformContext):
        return context.model_copy(
            update={
                "status_id": state.platform_check_id,
                "thread_id": state.platform_thread_id,
            }
        )
    raise TypeError(f"Unsupported platform context: {type(context).__name__}")
