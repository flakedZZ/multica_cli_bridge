"""Owner/session scoped polling preferences. Retry counters deliberately live elsewhere in memory."""
import json


TASK_TYPES = {"general", "image", "development", "document", "research"}


class PollingSettings:
    def __init__(self, directory):
        self.path = directory / "polling_settings.json"
        self.values = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        if not isinstance(self.values, dict):
            raise ValueError("轮询配置格式错误")

    @staticmethod
    def key(user, session, kind):
        return json.dumps([user, session, kind], ensure_ascii=False)

    def effective(self, job, default_interval=20, default_failures=5):
        result = {"poll_seconds": default_interval, "max_failures": default_failures}
        for kind in ("", job.get("task_type") or "general"):
            result.update(self.values.get(self.key(job["user_id"], job["session_id"], kind), {}))
        result.update(job.get("polling_override", {}))
        return result

    def update(self, user, session, kind, changes):
        key = self.key(user, session, kind)
        self.values[key] = {**self.values.get(key, {}), **changes}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.values, ensure_ascii=False), encoding="utf-8")
        temp.replace(self.path)


def validate_changes(interval, failures):
    changes = {}
    if interval != 0:
        if type(interval) is not int or not 5 <= interval <= 600:
            raise ValueError("轮询间隔需为5～600秒")
        changes["poll_seconds"] = interval
    if failures != 0:
        if type(failures) is not int or not 1 <= failures <= 50:
            raise ValueError("连续失败次数需为1～50次")
        changes["max_failures"] = failures
    if not changes:
        raise ValueError("请提供要修改的轮询间隔或失败次数")
    return changes
