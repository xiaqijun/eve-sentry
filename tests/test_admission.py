import pytest

from eve_risk.admission import AdmissionController, AdmissionResult


class FakeAdmissionRedis:
    def __init__(self) -> None:
        self.messages = set()
        self.members = set()
        self.groups = {}
        self.batches = {}
        self.job_markers = set()
        self.active = set()

    async def eval(self, script, number_of_keys, *args):
        if number_of_keys == 6:
            dedupe, member, group, _active, batch, job_marker = args[:6]
            job_id, *_rest, batch_id = args[5:]
            if dedupe in self.messages:
                return b"duplicate"
            self.messages.add(dedupe)
            owner = self.groups.get(group)
            if owner and owner != batch_id:
                return b"group_busy"
            if not owner and member in self.members:
                return b"member_rate"
            self.groups[group] = batch_id
            self.batches[batch] = self.batches.get(batch, 0) + 1
            self.members.add(member)
            self.active.add(job_id)
            self.job_markers.add(job_marker)
            return b"ok"
        if number_of_keys == 4 and len(args) == 6:
            group, batch, _active, job_marker, batch_id, job_id = args
            if job_marker not in self.job_markers:
                return 1
            self.job_markers.remove(job_marker)
            if self.groups.get(group) == batch_id:
                remaining = self.batches.get(batch, 0) - 1
                if remaining <= 0:
                    self.groups.pop(group, None)
                    self.batches.pop(batch, None)
                else:
                    self.batches[batch] = remaining
            self.active.discard(job_id)
            return 1
        if number_of_keys == 4:
            dedupe, member, group, _active, job_id, *_rest = args
            if dedupe in self.messages:
                return b"duplicate"
            if member in self.members:
                return b"member_rate"
            if group in self.groups:
                return b"group_busy"
            self.messages.add(dedupe)
            self.members.add(member)
            self.groups[group] = job_id
            self.active.add(job_id)
            return b"ok"
        group, _active, job_id = args
        self.groups.pop(group, None)
        self.active.discard(job_id)
        return 1


@pytest.mark.asyncio
async def test_duplicate_message_is_not_admitted_twice() -> None:
    redis = FakeAdmissionRedis()
    controller = AdmissionController(redis)
    kwargs = dict(
        job_id="job",
        msg_id="message",
        member_openid="member",
        group_openid="group",
        now_epoch=1,
        deadline_epoch=100,
    )
    assert await controller.admit(**kwargs) == AdmissionResult.OK
    kwargs["job_id"] = "job-2"
    assert await controller.admit(**kwargs) == AdmissionResult.DUPLICATE


@pytest.mark.asyncio
async def test_batch_admission_allows_same_message_and_releases_after_last_job() -> None:
    redis = FakeAdmissionRedis()
    controller = AdmissionController(redis)
    kwargs = dict(
        member_openid="member",
        group_openid="group",
        batch_id="batch-1",
        now_epoch=1,
        deadline_epoch=100,
    )

    assert await controller.admit_batch(job_id="job-1", msg_id="message:0", **kwargs) == AdmissionResult.OK
    assert await controller.admit_batch(job_id="job-2", msg_id="message:1", **kwargs) == AdmissionResult.OK
    assert (
        await controller.admit_batch(
            job_id="job-3",
            msg_id="other-message:0",
            **{**kwargs, "batch_id": "batch-2"},
        )
        == AdmissionResult.GROUP_BUSY
    )

    await controller.release("job-1", "group", "batch-1")
    assert redis.groups["limit:group:group"] == "batch-1"
    await controller.release("job-1", "group", "batch-1")
    assert redis.groups["limit:group:group"] == "batch-1"
    await controller.release("job-2", "group", "batch-1")
    assert "limit:group:group" not in redis.groups
