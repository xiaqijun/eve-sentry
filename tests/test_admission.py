import pytest

from eve_risk.admission import AdmissionController, AdmissionResult


class FakeAdmissionRedis:
    def __init__(self) -> None:
        self.messages = set()
        self.members = set()
        self.groups = {}
        self.active = set()

    async def eval(self, script, number_of_keys, *args):
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
