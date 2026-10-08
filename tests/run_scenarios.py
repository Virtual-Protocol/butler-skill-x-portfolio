#!/usr/bin/env python3
"""Offline scenario replays through the hub's replay harness.

    python3 tests/run_scenarios.py --hub <butler-skills checkout> [scenario ...]

Each fixtures/scenarios/sNN.json describes a world (pocket, wallet, prices, X pages, seeded
state, timer ticks) and what the replay must record. The harness answers prompt() with an
error, so scenarios that need a reviewed worldview seed it (and the persisted candidates) in
state. Times written as "@now-3h" are resolved against the clock at run time.
"""

import argparse
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import fake_bevo  # noqa: E402

KEY_RE = re.compile(r"^xp:stub-service-id:g[a-z0-9]{6}:e\d+:[A-Z0-9]+:(buy|sell):a0$")
UNITS = {"m": 60, "h": 3600, "d": 86400}
NOW = datetime.now(timezone.utc).replace(microsecond=0)


def resolve(value, gen):
    """@now-3h becomes a time; @key:e2:TKNA:sell becomes that leg's idempotency key."""
    if isinstance(value, dict):
        return {k: resolve(v, gen) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, gen) for v in value]
    if isinstance(value, str) and value.startswith("@now"):
        match = re.fullmatch(r"@now(?:([+-])(\d+)([mhd]))?", value)
        offset = 0 if not match.group(1) else int(match.group(2)) * UNITS[match.group(3)] * (1 if match.group(1) == "+" else -1)
        return (NOW + timedelta(seconds=offset)).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, str) and value.startswith("@key:"):
        _, epoch, sym, side = value.split(":")
        return "xp:stub-service-id:g%s:%s:%s:%s:a0" % (gen, epoch, sym, side)
    return value


def make_core(spec, params):
    """The canonical core from the duty's own fresh_core, with the scenario's changes on top."""
    duty, fake = fake_bevo.install(params)
    cfg, _ = duty.settings()
    core = duty.fresh_core(None, cfg, NOW)
    core["gen"] = spec.get("gen", "tstgen")
    base = spec.get("preset")
    if base == "deployed":
        core.update(deployed=True, wait_done=True, funded_at=duty.iso(NOW - timedelta(days=2)), cash=1000.0, contrib=5000.0, epoch=1,
                    last_rebalance_at=duty.iso(NOW - timedelta(days=2)), tx={"TKNA": 40, "TKNB": 30, "TKNC": 10},
                    pos={"TKNA": {"qty": 1000.0, "cost": 1800.0, "px": 2.0}, "TKNB": {"qty": 1500.0, "cost": 1400.0, "px": 1.0},
                         "TKNC": {"qty": 125.0, "cost": 450.0, "px": 4.0}})
    core.update(resolve(spec.get("set", {}), core["gen"]))
    chains = {b["s"]: b["c"] for b in params["BASKET"]}
    for sym, pos in core["pos"].items():  # a position as a fill leaves it: the delivered contract and the chain
        pos.setdefault("addr", fake_bevo.ADDR.get(sym))
        pos.setdefault("chain", chains.get(sym))
    return core


def prepare(scn, tmp):
    params = dict(fake_bevo.SAMPLE_PARAMS, **scn.get("params", {}))
    core_spec = scn.get("core", {})
    core = make_core(core_spec, params)
    gen = core["gen"]
    fx, state_dir = tmp / "fixtures", tmp / "state"
    fx.mkdir()
    state_dir.mkdir()
    prices = dict({"TKNA": 2.0, "TKNB": 1.0, "TKNC": 4.0}, **scn.get("prices", {}))
    pocket = dict({"cash": 5000.0, "funded": True, "holdings": []}, **scn.get("pocket", {}))
    wallet = dict({"usdc": 6000.0, "tokens": []}, **scn.get("wallet", {}))
    basket = {b["s"]: b for b in params["BASKET"] if b["s"] in fake_bevo.ADDR}
    (fx / "duties.json").write_text(json.dumps({"duties": [{"id": "stub-service-id", "pocket": {
        "cashUsdc": pocket["cash"], "funded": pocket["funded"], "holdings": pocket["holdings"]}}]}))
    tokens = [{"symbol": "USDC", "chainId": 8453, "tokenAddress": "0x" + "00" * 20, "balance": wallet["usdc"]}]
    tokens += [{"symbol": s, "chainId": basket[s]["c"], "tokenAddress": fake_bevo.ADDR[s], "balance": q}
               for s, q in wallet["tokens"]]
    (fx / "user-assets.json").write_text(json.dumps({"spot": {"available": True, "tokens": tokens}}))
    (fx / "token-stats.json").write_text(json.dumps({"tokens": [
        {"address": fake_bevo.ADDR[s], "networkId": b["c"], "priceUsd": prices[s]} for s, b in basket.items()]}))
    (fx / "token-search.json").write_text(json.dumps({"tokens": [
        {"symbol": s, "address": fake_bevo.ADDR[s], "chainId": b["c"], "verified": True} for s, b in basket.items()]}))
    rows = []
    for row in resolve(scn.get("executions", []), gen):  # the server's row names the settled hash, not the key
        key = row.pop("idempotencyKey", None)
        if key and key.split(":")[-2] == "buy":  # a buy row names what the rail delivered
            sym = key.split(":")[-3]
            row = dict({"tokenOutSymbol": sym, "tokenOutAddress": fake_bevo.ADDR[sym], "chainOut": basket[sym]["c"]}, **row)
        rows.append(dict(row, txHash="0x" + hashlib.sha256(key.encode()).hexdigest()) if key else row)
    (fx / "trade-executions.json").write_text(json.dumps({"trades": rows, "nextCursor": None, "hasMore": False}))
    ticks = [{"kind": "timer", "at": (NOW + timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%SZ"), "intervalSeconds": 3600}
             for i in range(scn.get("ticks", 0))]
    (fx / "xp-ticks.jsonl").write_text("".join(json.dumps(t) + "\n" for t in ticks))
    (tmp / "x.json").write_text(json.dumps(scn.get("x", {})))
    state = {"core": core}
    for key in ("wv", "queue", "x", "said"):
        if key in scn.get("state", {}):
            state[key] = resolve(scn["state"][key], gen)
    (state_dir / "state.json").write_text(json.dumps(state))
    return params, fx, state_dir


def replay(hub, params, fx, state_dir, tmp):
    env = dict(os.environ, PATH=str(HERE / "fake_bin") + os.pathsep + os.environ["PATH"], BEVO_FAKE_X=str(tmp / "x.json"))
    done = subprocess.run(
        [sys.executable, str(Path(hub) / "tests" / "replay.py"), "--standalone", str(ROOT), "--fixture", "xp-ticks",
         "--fixtures-dir", str(fx), "--state-dir", str(state_dir), "--no-download", "--params", json.dumps(params)],
        capture_output=True, text=True, env=env, timeout=120)
    out = done.stdout
    if "# ACTIONS_JSON_START" not in out:
        return done.returncode, None, None, done.stdout + done.stderr
    body = out.split("# ACTIONS_JSON_START")[1].split("# ACTIONS_JSON_END")[0]
    state = json.loads((state_dir / "state.json").read_text()) if (state_dir / "state.json").exists() else {}
    return done.returncode, json.loads(body), state, out + done.stderr


def dig(state, path):
    for part in path.split("."):
        if isinstance(state, list) and part.isdigit():
            state = state[int(part)] if int(part) < len(state) else None
        else:
            state = state.get(part) if isinstance(state, dict) else None
    return state


def check(scn, params, code, actions, state, output):
    problems = []
    if code != 0 or actions is None:
        return ["replay exited %s\n%s" % (code, output[-1500:])]
    want = scn.get("expect", {})
    chains = {b["s"]: str(b["c"]) for b in params["BASKET"]}
    owned = {a: s for s, a in fake_bevo.ADDR.items()}
    acp = [a for a in actions if a.get("call") == "acp"]
    notes = [a for a in actions if a.get("call") == "notify"]
    sides = []
    for action in acp:
        argv = action["argv"]
        buy = argv[:4] == ["acp", "trade", "--token-in", "usdc"]
        sell = argv[:3] == ["acp", "trade", "--token-in"] and argv[4] == "--chain-in" and argv[-4:-2] == ["--token-out", "usdc"]
        sym = argv[argv.index("--token-out") + 1] if buy else owned.get(argv[3])  # buys name a symbol, sells a learned contract
        if not (buy or sell) or sym not in chains or argv[-2] != "--idempotency-key" or not KEY_RE.match(argv[-1]):
            problems.append("an acp call has the wrong shape: %s" % argv)
            continue
        chain = argv[argv.index("--chain-out" if buy else "--chain-in") + 1]
        if chain != chains[sym]:
            problems.append("wrong chain in %s" % argv)
        sides.append("buy" if buy else "sell")
    exp = want.get("acp", {})
    if "count" in exp and len(acp) != exp["count"]:
        problems.append("acp calls: %d, wanted %d" % (len(acp), exp["count"]))
    if "sides" in exp and sides != exp["sides"]:
        problems.append("sides %s, wanted %s" % (sides, exp["sides"]))
    if "amounts" in exp:
        got = [a["argv"][a["argv"].index("--amount-in") + 1] for a in acp]
        if got != exp["amounts"]:
            problems.append("amounts %s, wanted %s" % (got, exp["amounts"]))
    if "max_buy_usd" in exp:
        spent = sum(float(a["argv"][a["argv"].index("--amount-in") + 1]) for a in acp if a["argv"][3] == "usdc")
        if spent > exp["max_buy_usd"]:
            problems.append("bought %.2f, cap %.2f" % (spent, exp["max_buy_usd"]))
    text = "\n".join(n["text"] for n in notes)
    for needle in want.get("notes_contain", []):
        if needle not in text:
            problems.append("no note contains %r" % needle)
    for needle in want.get("notes_absent", []):
        if needle in text:
            problems.append("a note contains %r" % needle)
    pushes = [n.get("push") or "" for n in notes if not n.get("quiet")]
    for needle in want.get("push_contains", []):
        if not any(needle in p for p in pushes):
            problems.append("no push contains %r" % needle)
    if "pushes" in want and len(pushes) != want["pushes"]:
        problems.append("pushes: %d, wanted %d" % (len(pushes), want["pushes"]))
    fails = [a for a in actions if a.get("call") == "fail"]
    dones = [a for a in actions if a.get("call") == "done"]
    if len(fails) != want.get("fails", 0):
        problems.append("fail calls: %s" % [f["reason"] for f in fails])
    if len(dones) != want.get("dones", 0):
        problems.append("done calls: %d" % len(dones))
    for needle in want.get("done_contains", []):
        if not any(needle in d.get("summary", "") for d in dones):
            problems.append("done summary lacks %r" % needle)
    for path, value in want.get("state", {}).items():
        if dig(state, path) != value:
            problems.append("state %s is %r, wanted %r" % (path, dig(state, path), value))
    return problems


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hub", default=os.environ.get("BUTLER_SKILLS_HUB"), help="a butler-skills checkout")
    parser.add_argument("names", nargs="*")
    args = parser.parse_args()
    if not args.hub or not (Path(args.hub) / "tests" / "replay.py").exists():
        parser.error("--hub (or BUTLER_SKILLS_HUB) must point at a butler-skills checkout")
    failed = 0
    for path in sorted((ROOT / "fixtures" / "scenarios").glob("s*.json")):
        if args.names and path.stem not in args.names:
            continue
        scn = json.loads(path.read_text())
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            params, fx, state_dir = prepare(copy.deepcopy(scn), tmp)
            code, actions, state, output = replay(args.hub, params, fx, state_dir, tmp)
            problems = check(scn, params, code, actions, state, output)
        print("%s %s: %s" % ("FAIL" if problems else "ok  ", path.stem, scn["name"]))
        for problem in problems:
            print("     - " + problem)
        failed += bool(problems)
    print("%d scenario(s) failed" % failed if failed else "all scenarios passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
