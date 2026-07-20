from __future__ import annotations

from enum import StrEnum

from redis.asyncio import Redis

ADMISSION_SCRIPT = """
local dedupe = KEYS[1]
local member = KEYS[2]
local group = KEYS[3]
local active = KEYS[4]
local job_id = ARGV[1]
local now = tonumber(ARGV[2])
local deadline = tonumber(ARGV[3])
local context_ttl = tonumber(ARGV[4])
local member_ttl = tonumber(ARGV[5])
local group_ttl = tonumber(ARGV[6])
local max_jobs = tonumber(ARGV[7])

if redis.call('EXISTS', dedupe) == 1 then return 'duplicate' end
redis.call('SET', dedupe, '1', 'EX', context_ttl)
if redis.call('EXISTS', member) == 1 then return 'member_rate' end
if redis.call('EXISTS', group) == 1 then return 'group_busy' end
redis.call('ZREMRANGEBYSCORE', active, '-inf', now)
if redis.call('ZCARD', active) >= max_jobs then return 'global_busy' end

redis.call('SET', member, '1', 'EX', member_ttl)
redis.call('SET', group, job_id, 'EX', group_ttl)
redis.call('ZADD', active, deadline, job_id)
return 'ok'
"""

RELEASE_SCRIPT = """
local group = KEYS[1]
local active = KEYS[2]
local job_id = ARGV[1]
if redis.call('GET', group) == job_id then redis.call('DEL', group) end
redis.call('ZREM', active, job_id)
return 1
"""


class AdmissionResult(StrEnum):
    OK = "ok"
    DUPLICATE = "duplicate"
    MEMBER_RATE = "member_rate"
    GROUP_BUSY = "group_busy"
    GLOBAL_BUSY = "global_busy"


class AdmissionController:
    def __init__(
        self,
        redis: Redis,
        *,
        context_ttl: int = 600,
        member_ttl: int = 60,
        group_ttl: int = 330,
        max_jobs: int = 3,
    ) -> None:
        self.redis = redis
        self.context_ttl = context_ttl
        self.member_ttl = member_ttl
        self.group_ttl = group_ttl
        self.max_jobs = max_jobs

    async def admit(
        self,
        *,
        job_id: str,
        msg_id: str,
        member_openid: str,
        group_openid: str,
        now_epoch: int,
        deadline_epoch: int,
    ) -> AdmissionResult:
        result = await self.redis.eval(
            ADMISSION_SCRIPT,
            4,
            f"qq:message:{msg_id}",
            f"limit:member:{member_openid}",
            f"limit:group:{group_openid}",
            "analysis:active",
            job_id,
            now_epoch,
            deadline_epoch,
            self.context_ttl,
            self.member_ttl,
            self.group_ttl,
            self.max_jobs,
        )
        if isinstance(result, bytes):
            result = result.decode()
        return AdmissionResult(str(result))

    async def release(self, job_id: str, group_openid: str) -> None:
        await self.redis.eval(
            RELEASE_SCRIPT,
            2,
            f"limit:group:{group_openid}",
            "analysis:active",
            job_id,
        )
