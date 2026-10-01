from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
PositiveInt = Annotated[int, Field(strict=True, gt=0)]
NonNegativeInt = Annotated[int, Field(strict=True, ge=0)]
QuestionId = Annotated[str, StringConstraints(pattern=r"^q[1-3]$")]
AnswerOption = Annotated[str, StringConstraints(pattern=r"^[A-C]$")]


class CogGateModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class PlatformType(str, Enum):
    GITHUB = "github"
    ADO = "ado"


class EventType(str, Enum):
    PR_UPDATED = "pr_updated"
    COMMENT_CREATED = "comment_created"


class FileChangeType(str, Enum):
    ADDED = "ADDED"
    MODIFIED = "MODIFIED"
    DELETED = "DELETED"
    RENAMED = "RENAMED"


class ChangedFile(CogGateModel):
    path: NonEmptyString
    change_type: FileChangeType
    old_path: NonEmptyString | None = None
    old_content: str | None = None
    new_content: str | None = None
    is_binary: bool = False
    is_generated: bool = False

    @model_validator(mode="after")
    def validate_contents(self) -> ChangedFile:
        if self.change_type is FileChangeType.ADDED and self.old_content is not None:
            raise ValueError("Added files cannot contain old content.")
        if self.change_type is FileChangeType.DELETED and self.new_content is not None:
            raise ValueError("Deleted files cannot contain new content.")
        if self.change_type is FileChangeType.RENAMED and self.old_path is None:
            raise ValueError("Renamed files require old_path.")
        if self.is_binary and (self.old_content is not None or self.new_content is not None):
            raise ValueError("Binary files cannot contain text content.")
        return self


class TenantConfig(CogGateModel):
    tenant_id: NonEmptyString
    is_active: bool
    allowed_repos: list[NonEmptyString] = Field(default_factory=lambda: ["*"], min_length=1)
    max_llm_tokens: PositiveInt = 32_000
    max_attempts: PositiveInt = 3
    github_app_id_secret_name: NonEmptyString | None = None
    github_private_key_secret_name: NonEmptyString | None = None

    @model_validator(mode="after")
    def validate_github_secret_pair(self) -> TenantConfig:
        app_secret_set = self.github_app_id_secret_name is not None
        key_secret_set = self.github_private_key_secret_name is not None
        if app_secret_set != key_secret_set:
            raise ValueError("GitHub App ID and private key secret names must be configured together.")
        return self


class GitHubPlatformContext(CogGateModel):
    platform: Literal["github"] = "github"
    tenant_id: NonEmptyString
    owner: NonEmptyString
    repo: NonEmptyString
    pr_number: PositiveInt
    pr_author_id: NonEmptyString
    head_commit_sha: NonEmptyString
    check_run_id: PositiveInt | None = None
    quiz_comment_id: PositiveInt | None = None


class AdoPlatformContext(CogGateModel):
    platform: Literal["ado"] = "ado"
    tenant_id: NonEmptyString
    project: NonEmptyString
    repo_id: NonEmptyString
    pr_id: PositiveInt
    pr_author_id: NonEmptyString
    head_commit_sha: NonEmptyString
    status_id: PositiveInt | None = None
    thread_id: PositiveInt | None = None


PlatformContext = Annotated[
    GitHubPlatformContext | AdoPlatformContext,
    Field(discriminator="platform"),
]


class NormalizedPrEvent(CogGateModel):
    event_id: NonEmptyString
    event_type: EventType
    context: PlatformContext
    comment_author_id: NonEmptyString | None = None
    comment_text: NonEmptyString | None = None
    comment_id: NonEmptyString | None = None
    is_bot_comment: bool = False

    @model_validator(mode="after")
    def validate_event_fields(self) -> NormalizedPrEvent:
        comment_fields = (self.comment_author_id, self.comment_text, self.comment_id)
        if self.event_type is EventType.COMMENT_CREATED:
            if any(value is None for value in comment_fields):
                raise ValueError(
                    "Comment events require comment_author_id, comment_text, and comment_id."
                )
        elif any(value is not None for value in comment_fields):
            raise ValueError("PR update events cannot contain comment fields.")
        return self


class QuizOptions(CogGateModel):
    A: NonEmptyString
    B: NonEmptyString
    C: NonEmptyString


class QuestionItem(CogGateModel):
    id: QuestionId
    question_text: NonEmptyString
    options: QuizOptions
    correct_option: AnswerOption


class CogGateQuizPayload(CogGateModel):
    gate_triggered: bool
    questions: list[QuestionItem] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def validate_question_logic(self) -> CogGateQuizPayload:
        question_count = len(self.questions)
        if self.gate_triggered and not 1 <= question_count <= 3:
            raise ValueError("Gate triggered requires 1-3 questions.")
        if not self.gate_triggered and question_count:
            raise ValueError("No questions allowed if gate not triggered.")

        question_ids = [question.id for question in self.questions]
        if len(set(question_ids)) != len(question_ids):
            raise ValueError("Question IDs must be unique.")
        return self


class QuizPhase(str, Enum):
    GENERATING = "GENERATING"
    PENDING_PUBLICATION = "PENDING_PUBLICATION"
    PUBLISHED = "PUBLISHED"


class QuizState(CogGateModel):
    phase: QuizPhase
    head_commit_sha: NonEmptyString
    pr_author_id: NonEmptyString
    questions: dict[QuestionId, AnswerOption] = Field(default_factory=dict, max_length=3)
    attempts: NonNegativeInt = 0
    platform_check_id: PositiveInt | None = None
    platform_thread_id: PositiveInt | None = None

    @model_validator(mode="after")
    def validate_phase_invariants(self) -> QuizState:
        if self.phase is QuizPhase.GENERATING:
            if self.questions or self.platform_thread_id is not None:
                raise ValueError("Generating state cannot contain questions or a thread ID.")
            return self

        if not 1 <= len(self.questions) <= 3:
            raise ValueError("Publication states require 1-3 answers.")
        if self.platform_check_id is None:
            raise ValueError("Publication states require a platform check ID.")
        if self.phase is QuizPhase.PENDING_PUBLICATION and self.platform_thread_id is not None:
            raise ValueError("Pending publication state cannot contain a thread ID.")
        if self.phase is QuizPhase.PUBLISHED and self.platform_thread_id is None:
            raise ValueError("Published state requires a platform thread ID.")
        return self


class IPlatformAdapter(ABC):
    @abstractmethod
    async def create_check_run(
        self, context: PlatformContext, description: str
    ) -> PlatformContext:
        """Create a pending platform check and return its identifier in the context."""

    @abstractmethod
    async def complete_check_run(
        self, context: PlatformContext, description: str
    ) -> None:
        """Complete the current platform check successfully."""

    @abstractmethod
    async def fetch_changed_files(self, context: PlatformContext) -> list[ChangedFile]:
        """Fetch changed files with their old and new text content."""

    @abstractmethod
    async def get_current_pr_head(self, context: PlatformContext) -> str:
        """Return the platform's current pull-request head commit SHA."""

    @abstractmethod
    async def post_quiz_comment(
        self, context: PlatformContext, markdown: str
    ) -> PlatformContext:
        """Post the quiz and return its comment or thread identifier in the context."""

    @abstractmethod
    async def post_reply_comment(self, context: PlatformContext, markdown: str) -> None:
        """Post a reply associated with the pull request or quiz thread."""

    @abstractmethod
    async def update_quiz_comment(
        self, context: PlatformContext, markdown: str
    ) -> None:
        """Update or resolve the published quiz comment or thread."""
