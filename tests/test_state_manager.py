from __future__ import annotations

import json

import pytest

from src.core.domain import GitHubPlatformContext, QuizPhase, QuizState
from src.core.state_manager import (
    _ABANDON_EVENT_SCRIPT,
    _CLEAR_STATE_SCRIPT,
    _COMPLETE_EVENT_SCRIPT,
    _RECORD_FAILED_ATTEMPT_SCRIPT,
    _RELEASE_LEASE_SCRIPT,
    _RENEW_LEASE_SCRIPT,
    _SAVE_STATE_SCRIPT,
    StateManager,
)


class MemoryRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def set(
        self,
        name: str,
        value: str,
        *,
        ex: int | None = None,
        nx: bool = False,
        xx: bool = False,
    ) -> bool | None:
        del ex
        if nx and name in self.values:
            return None
        if xx and name not in self.values:
            return None
        self.values[name] = value
        return True

    async def get(self, name: str) -> str | bytes | None:
        return self.values.get(name)

    async def delete(self, *names: str) -> int:
        deleted = 0
        for name in names:
            if name in self.values:
                del self.values[name]
                deleted += 1
        return deleted

    async def exists(self, name: str) -> int:
        return int(name in self.values)

    async def eval(
        self, script: str, numkeys: int, *keys_and_args: str
    ) -> object:
        keys = keys_and_args[:numkeys]
        args = keys_and_args[numkeys:]

        if script == _RENEW_LEASE_SCRIPT:
            return int(self.values.get(keys[0]) == args[0])
        if script == _ABANDON_EVENT_SCRIPT:
            serialized = self.values.get(keys[0])
            if serialized is None:
                return 0
            event = json.loads(serialized)
            if event["status"] != "PROCESSING" or event["owner"] != args[0]:
                return 0
            return await self.delete(keys[0])
        if script == _COMPLETE_EVENT_SCRIPT:
            serialized = self.values.get(keys[0])
            if serialized is None:
                return 0
            event = json.loads(serialized)
            if event["status"] != "PROCESSING" or event["owner"] != args[0]:
                return 0
            self.values[keys[0]] = args[1]
            return 1
        if script == _RELEASE_LEASE_SCRIPT:
            if self.values.get(keys[0]) != args[0]:
                return 0
            return await self.delete(keys[0])
        if script == _SAVE_STATE_SCRIPT:
            if args[3] and self.values.get(keys[2]) != args[3]:
                return 0
            self.values[keys[0]] = args[0]
            self.values[keys[1]] = args[2]
            return 1
        if script == _RECORD_FAILED_ATTEMPT_SCRIPT:
            serialized = self.values.get(keys[0])
            if serialized is None:
                return -1
            state = json.loads(serialized)
            state["attempts"] += 1
            self.values[keys[0]] = json.dumps(state)
            self.values[keys[1]] = "1"
            return state["attempts"]
        if script == _CLEAR_STATE_SCRIPT:
            await self.delete(keys[0])
            if self.values.get(keys[1]) == args[0]:
                await self.delete(keys[1])
            return 1
        raise AssertionError("Unexpected Lua script.")


def context() -> GitHubPlatformContext:
    return GitHubPlatformContext(
        tenant_id="installation:123",
        owner="octo-org",
        repo="service",
        pr_number=42,
        pr_author_id="user-7",
        head_commit_sha="abc123",
        check_run_id=99,
        quiz_comment_id=100,
    )


def published_state(attempts: int = 0) -> QuizState:
    return QuizState(
        phase=QuizPhase.PUBLISHED,
        head_commit_sha="abc123",
        pr_author_id="user-7",
        questions={"q1": "B"},
        attempts=attempts,
        platform_check_id=99,
        platform_thread_id=100,
    )


@pytest.mark.asyncio
async def test_event_claim_is_idempotent_and_can_complete() -> None:
    redis = MemoryRedis()
    manager = StateManager(redis)

    owner = await manager.claim_event("delivery:1")
    assert owner is not None
    assert await manager.claim_event("delivery:1") is None

    assert await manager.complete_event("delivery:1", owner)

    event = json.loads(next(iter(redis.values.values())))
    assert event["status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_only_processing_event_claims_can_be_abandoned() -> None:
    redis = MemoryRedis()
    manager = StateManager(redis)

    owner = await manager.claim_event("delivery-1")
    assert owner is not None
    assert not await manager.abandon_event("delivery-1", "other-owner")
    assert await manager.abandon_event("delivery-1", owner)
    replacement_owner = await manager.claim_event("delivery-1")
    assert replacement_owner is not None

    assert not await manager.complete_event("delivery-1", owner)
    assert await manager.complete_event("delivery-1", replacement_owner)

    assert not await manager.abandon_event("delivery-1", replacement_owner)
    assert await manager.claim_event("delivery-1") is None


@pytest.mark.asyncio
async def test_lease_requires_matching_owner_token() -> None:
    redis = MemoryRedis()
    manager = StateManager(redis)
    platform_context = context()

    token = await manager.acquire_lease(platform_context)

    assert token is not None
    assert not await manager.acquire_lease(platform_context)
    assert not await manager.renew_lease(platform_context, "other-token")
    assert await manager.renew_lease(platform_context, token)
    assert not await manager.release_lease(platform_context, "other-token")
    assert await manager.release_lease(platform_context, token)


@pytest.mark.asyncio
async def test_quiz_state_is_saved_under_active_sha() -> None:
    redis = MemoryRedis()
    manager = StateManager(redis)
    platform_context = context()

    await manager.save_quiz_state(platform_context, published_state())

    assert await manager.get_active_sha(platform_context) == "abc123"
    assert await manager.get_quiz_state(platform_context) == published_state()


@pytest.mark.asyncio
async def test_quiz_state_rejects_mismatched_context_sha() -> None:
    manager = StateManager(MemoryRedis())
    mismatched_state = published_state().model_copy(
        update={"head_commit_sha": "different"}
    )

    with pytest.raises(ValueError, match="must match"):
        await manager.save_quiz_state(context(), mismatched_state)


@pytest.mark.asyncio
async def test_quiz_state_save_requires_current_lease_owner_when_provided() -> None:
    redis = MemoryRedis()
    manager = StateManager(redis)
    platform_context = context()
    token = await manager.acquire_lease(platform_context)
    assert token is not None

    with pytest.raises(RuntimeError, match="lease ownership was lost"):
        await manager.save_quiz_state(
            platform_context, published_state(), "other-token"
        )

    await manager.save_quiz_state(
        platform_context, published_state(), token
    )
    assert await manager.get_quiz_state(platform_context) == published_state()


@pytest.mark.asyncio
async def test_failed_attempt_atomically_updates_state_and_cooldown() -> None:
    redis = MemoryRedis()
    manager = StateManager(redis)
    platform_context = context()
    await manager.save_quiz_state(platform_context, published_state())

    attempts = await manager.record_failed_attempt(
        platform_context, "abc123", "user:7"
    )

    assert attempts == 1
    assert await manager.is_in_cooldown(platform_context, "user:7")
    state = await manager.get_quiz_state(platform_context)
    assert state is not None
    assert state.attempts == 1


@pytest.mark.asyncio
async def test_failed_attempt_requires_existing_state() -> None:
    manager = StateManager(MemoryRedis())

    with pytest.raises(LookupError, match="missing quiz state"):
        await manager.record_failed_attempt(context(), "abc123", "user-7")


@pytest.mark.asyncio
async def test_clear_state_only_removes_matching_active_pointer() -> None:
    redis = MemoryRedis()
    manager = StateManager(redis)
    platform_context = context()
    await manager.save_quiz_state(platform_context, published_state())

    await manager.clear_quiz_state(platform_context, "older-sha")
    assert await manager.get_active_sha(platform_context) == "abc123"

    await manager.clear_quiz_state(platform_context, "abc123")
    assert await manager.get_active_sha(platform_context) is None
    assert await manager.get_quiz_state(platform_context, "abc123") is None
