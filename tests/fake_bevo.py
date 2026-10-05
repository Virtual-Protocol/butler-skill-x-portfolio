"""A scripted stand-in for the `bevo` SDK, for unit tests of duty.py.

`install(params)` puts a fresh fake on sys.modules["bevo"], sets PARAMS and returns the
freshly imported duty module together with the fake, so each test starts from nothing.
"""

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_BASKET = [
    {"s": "TKNA", "c": 8453, "a": "0x" + "a1" * 20, "w": 40},
    {"s": "TKNB", "c": 8453, "a": "0x" + "b2" * 20, "w": 30},
    {"s": "TKNC", "c": 1, "a": "0x" + "c3" * 20, "w": 10},
]
SAMPLE_PARAMS = {"HANDLES": ["alice", "bob", "carol"], "CAPITAL_USD": 5000, "BASKET": SAMPLE_BASKET,
                 "REBALANCE_HOURS": 24, "MODE": "run"}


class BevoError(Exception):
    def __init__(self, message, code=None, retry_after_s=None, reason=None):
        super().__init__(message)
        self.code, self.retry_after_s, self.reason = code, retry_after_s, reason


class State(dict):
    """Top-level assignment saves; JSON only, like the real one."""

    def __setitem__(self, key, value):
        json.dumps(value)
        super().__setitem__(key, json.loads(json.dumps(value)))


class FakeBevo:
    BevoError = BevoError
    SERVICE_ID = "svc-1"

    def __init__(self):
        self.state = State()
        self.logs, self.notes, self.fails, self.dones = [], [], [], []
        self.reads, self.statuses, self.prompts, self.sent = {}, {}, [], []
        self.prompt_answers = []

    def log(self, message):
        self.logs.append(str(message))

    def notify(self, text, quiet=False, push=None):
        self.notes.append({"text": str(text), "quiet": bool(quiet), "push": push})
        return {"ok": True}

    def fail(self, reason):
        self.fails.append(str(reason))

    def done(self, summary=None):
        self.dones.append(summary)
        raise SystemExit(0)

    def allow(self, key, per_day=None, per_hour=None, max_usd=None, usd=0):
        return True

    def sleep(self, seconds):
        return None

    def ticks(self):
        return iter(())

    def read(self, path, params=None):
        value = self.reads.get(path)
        if isinstance(value, BaseException):
            raise value
        if value is None:
            raise BevoError("no fixture for %s" % path)
        return value

    def exec_status(self, key, route="trade"):
        return self.statuses.get(key, {"state": "not_found"})

    def prompt(self, text, *, system=None, schema=None, max_tokens=None):
        self.prompts.append({"text": text, "system": system, "schema": schema})
        answer = self.prompt_answers.pop(0) if self.prompt_answers else BevoError("none", code="unavailable")
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def key(self, *parts):
        if not parts or any(p is None or str(p) == "" for p in parts):
            raise ValueError("empty key part")
        joined = ":".join(str(p) for p in parts)
        safe = re.sub(r"[^A-Za-z0-9:_.\-]", "-", joined)
        if len(safe) <= 128:
            return safe
        return safe[:111] + "." + hashlib.sha256(joined.encode()).hexdigest()[:16]


def completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def install(params=None):
    fake = FakeBevo()
    sys.modules["bevo"] = fake
    os.environ["PARAMS"] = json.dumps(SAMPLE_PARAMS if params is None else params)
    spec = importlib.util.spec_from_file_location("duty_under_test", ROOT / "duty.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, fake
