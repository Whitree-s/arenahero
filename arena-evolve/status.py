"""进程间状态交换：生产者原子写、监控只读。

deploy.py / run_evolve.py 把运行状态写成 JSON 快照，monitor.py 轮询读取。
故意不用 socket/队列——监控进程崩了、没启动、启动晚了都不影响生产者，
线上 Agent 的 15s tick 预算也不会被 HTTP 阻塞。
"""

import json
import os
import tempfile
import time

LIVE_STATUS = "results/live_status.json"
EVOLVE_STATUS = "results/evolve_status.json"


def write_status(path, obj):
    """原子写：先写临时文件再 rename，读方永远看不到半截 JSON。

    失败不抛异常——监控是旁路，绝不能拖垮生产者。
    """
    try:
        obj = dict(obj)
        obj["written_at"] = time.time()
        d = os.path.dirname(os.path.abspath(path))
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(obj, f, ensure_ascii=False, default=str)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return True
    except Exception:
        return False


def read_status(path, stale_after=None):
    """读快照；文件不存在返回 None。

    stale_after: 秒数；超过该时长未更新则在返回值里标记 stale=True。
    """
    try:
        with open(path) as f:
            obj = json.load(f)
    except (OSError, ValueError):
        return None
    age = time.time() - obj.get("written_at", 0)
    obj["age"] = age
    if stale_after is not None:
        obj["stale"] = age > stale_after
    return obj
