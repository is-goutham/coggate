from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import quote
from uuid import uuid4

from src.core.domain import PlatformContext, QuizState

_RENEW_LEASE_SCRIPT = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("EXPIRE", KEYS[1], ARGV[2])
end
return 0
"""

_RELEASE_LEASE_SCRIPT = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
end
return 0
"""

_SAVE_STATE_SCRIPT = """
if ARGV[4] ~= "" and redis.call("GET", KEYS[3]) ~= ARGV[4] then
    return 0
end
redis.call("SET", KEYS[1], ARGV[1], "EX", ARGV[2])
redis.call("SET", KEYS[2], ARGV[3], "EX", ARGV[2])
return 1
"""

_RECORD_FAILED_ATTEMPT_SCRIPT = """
local serialized = redis.call("GET", KEYS[1])
if not serialized then
    return -1
end
local state = cjson.decode(serialized)
state["attempts"] = state["attempts"] + 1
redis.call("SET", KEYS[1], cjson.encode(state), "KEEPTTL")
redis.call("SET", KEYS[2], "1", "EX", ARGV[1])
return state["attempts"]
"""

_CLEAR_STATE_SCRIPT = """
redis.call("DEL", KEYS[1])
if redis.call("GET", KEYS[2]) == ARGV[1] then
    redis.call("DEL", KEYS[2])
end
return 1
"""

_ABANDON_EVENT_SCRIPT = """
local serialized = redis.call("GET", KEYS[1])
if not serialized then
    return 0
end
local event = cjson.decode(serialized)
if event["status"] == "PROCESSING" and event["owner"] == ARGV[1] then
    return redis.call("DEL", KEYS[1])
end
return 0
"""

_COMPLETE_EVENT_SCRIPT = """
local serialized = redis.call("GET", KEYS[1])
if not serialized then
    return 0
end
local event = cjson.decode(serialized)
if event["status"] ~= "PROCESSING" or event["owner"] ~= ARGV[1] then
    return 0
end
redis.call("SET", KEYS[1], ARGV[2], "EX", ARGV[3])
return 1
"""


class AsyncRedis(Protocol):
    async def set(
        self,
        name: str,
        value: str,
        *,
        ex: int | None = None,
        nx: bool = False,
        xx: bool = False,
    ) -> bool | None: ...

    async def get(self, name: str) -> str | bytes | None: ...

    async def delete(self, *names: str) -> int: ...

    async def exists(self, name: str) -> int: ...

    async def eval(
        self, script: str, numkeys: int, *keys_and_args: str
    ) -> object: ...


class StateManager:
    def __init__(
        self,
        redis: AsyncRedis,
        *,
        key_prefix: str = "coggate",
        event_ttl_seconds: int = 3_600,
        lease_ttl_seconds: int = 120,
        quiz_ttl_seconds: int = 86_400,
        cooldown_ttl_seconds: int = 300,
    ) -> None:
        self._redis = redis
        self._prefix = key_prefix
        self._event_ttl = event_ttl_seconds
        self._lease_ttl = lease_ttl_seconds
        self._quiz_ttl = quiz_ttl_seconds
        self._cooldown_ttl = cooldown_ttl_seconds

    async def claim_event(self, event_id: str) -> str | None:
        owner = str(uuid4())
        event = {
            "status": "PROCESSING",
            "owner": owner,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        result = await self._redis.set(
            self._event_key(event_id),
            json.dumps(event, separators=(",", ":")),
            nx=True,
            ex=self._event_ttl,
        )
        return owner if result else None

    async def complete_event(self, event_id: str, owner: str) -> bool:
        event = {
            "status": "COMPLETED",
            "timestamp": datetime.now(UTC).isoformat(),
        }
        result = await self._redis.eval(
            _COMPLETE_EVENT_SCRIPT,
            1,
            self._event_key(event_id),
            owner,
            json.dumps(event, separators=(",", ":")),
            str(self._event_ttl),
        )
        return bool(result)

    async def abandon_event(self, event_id: str, owner: str) -> bool:
        result = await self._redis.eval(
            _ABANDON_EVENT_SCRIPT,
            1,
            self._event_key(event_id),
            owner,
        )
        return bool(result)

    async def acquire_lease(self, context: PlatformContext) -> str | None:
        token = str(uuid4())
        acquired = await self._redis.set(
            self._lease_key(context),
            token,
            nx=True,
            ex=self._lease_ttl,
        )
        return token if acquired else None

    async def renew_lease(self, context: PlatformContext, token: str) -> bool:
        result = await self._redis.eval(
            _RENEW_LEASE_SCRIPT,
            1,
            self._lease_key(context),
            token,
            str(self._lease_ttl),
        )
        return bool(result)

    async def release_lease(self, context: PlatformContext, token: str) -> bool:
        result = await self._redis.eval(
            _RELEASE_LEASE_SCRIPT,
            1,
            self._lease_key(context),
            token,
        )
        return bool(result)

    async def save_quiz_state(
        self,
        context: PlatformContext,
        state: QuizState,
        lease_token: str | None = None,
    ) -> None:
        if state.head_commit_sha != context.head_commit_sha:
            raise ValueError("Quiz state SHA must match the platform context SHA.")

        result = await self._redis.eval(
            _SAVE_STATE_SCRIPT,
            3,
            self._state_key(context, state.head_commit_sha),
            self._active_sha_key(context),
            self._lease_key(context),
            state.model_dump_json(),
            str(self._quiz_ttl),
            state.head_commit_sha,
            lease_token or "",
        )
        if not result:
            raise RuntimeError("Quiz state was not saved because lease ownership was lost.")

    async def get_active_sha(self, context: PlatformContext) -> str | None:
        return self._decode(await self._redis.get(self._active_sha_key(context)))

    async def get_quiz_state(
        self, context: PlatformContext, head_commit_sha: str | None = None
    ) -> QuizState | None:
        active_sha = head_commit_sha or await self.get_active_sha(context)
        if active_sha is None:
            return None

        serialized = await self._redis.get(self._state_key(context, active_sha))
        decoded = self._decode(serialized)
        return QuizState.model_validate_json(decoded) if decoded is not None else None

    async def is_in_cooldown(
        self, context: PlatformContext, user_id: str
    ) -> bool:
        return bool(await self._redis.exists(self._cooldown_key(context, user_id)))

    async def record_failed_attempt(
        self, context: PlatformContext, head_commit_sha: str, user_id: str
    ) -> int:
        result = await self._redis.eval(
            _RECORD_FAILED_ATTEMPT_SCRIPT,
            2,
            self._state_key(context, head_commit_sha),
            self._cooldown_key(context, user_id),
            str(self._cooldown_ttl),
        )
        if not isinstance(result, (int, str, bytes, bytearray)):
            raise TypeError("Redis returned an invalid failed-attempt result.")
        attempts = int(result)
        if attempts < 0:
            raise LookupError("Cannot record an attempt for missing quiz state.")
        return attempts

    async def clear_quiz_state(
        self, context: PlatformContext, head_commit_sha: str
    ) -> None:
        await self._redis.eval(
            _CLEAR_STATE_SCRIPT,
            2,
            self._state_key(context, head_commit_sha),
            self._active_sha_key(context),
            head_commit_sha,
        )

    def _event_key(self, event_id: str) -> str:
        return f"{self._prefix}:event:{self._component(event_id)}"

    def _lease_key(self, context: PlatformContext) -> str:
        return f"{self._base_pr_key(context)}:lease"

    def _active_sha_key(self, context: PlatformContext) -> str:
        return f"{self._base_pr_key(context)}:active_sha"

    def _state_key(self, context: PlatformContext, head_commit_sha: str) -> str:
        return (
            f"{self._base_pr_key(context)}:state:"
            f"{self._component(head_commit_sha)}"
        )

    def _cooldown_key(self, context: PlatformContext, user_id: str) -> str:
        return (
            f"{self._base_pr_key(context)}:cooldown:"
            f"{self._component(user_id)}"
        )

    def _base_pr_key(self, context: PlatformContext) -> str:
        if context.platform == "github":
            repository = f"{context.owner}/{context.repo}"
            pull_request_id = str(context.pr_number)
        else:
            repository = f"{context.project}/{context.repo_id}"
            pull_request_id = str(context.pr_id)

        return ":".join(
            (
                self._prefix,
                self._component(context.platform),
                self._component(context.tenant_id),
                self._component(repository),
                self._component(pull_request_id),
            )
        )

    @staticmethod
    def _component(value: str) -> str:
        return quote(value, safe="")

    @staticmethod
    def _decode(value: str | bytes | None) -> str | None:
        if isinstance(value, bytes):
            return value.decode("utf-8")
        return value
