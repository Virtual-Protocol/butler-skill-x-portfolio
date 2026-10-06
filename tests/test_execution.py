"""Keys, the two argv shapes, outcome mapping, recovery, fills and whole runs."""

import json
import re
import unittest
from datetime import timedelta
from unittest import mock

from fake_bevo import ADDR, SAMPLE_BASKET, completed, install

KEY_RE = re.compile(r"^[A-Za-z0-9:_.\-]{1,128}$")
PRICE = {"TKNA": 2.0, "TKNB": 1.0, "TKNC": 4.0}
OLD = "2020-01-01T00:00:00Z"


class World:
    """Scripted reads plus a fake `acp` and `bevo-x`; every acp call is recorded."""

    def __init__(self, duty, fake, cash=5000.0, funded=True, usdc=6000.0, wallet=None, held=None):
        self.duty, self.fake, self.argvs, self.reply = duty, fake, [], None
        self.rows = []
        self.cfg, _ = duty.settings()
        fake.reads["/duties"] = {"duties": [{"id": "svc-1", "pocket": {"cashUsdc": cash, "funded": funded,
                                                                        "holdings": held or []}}]}
        tokens = [{"symbol": "USDC", "chainId": 8453, "tokenAddress": "0x" + "00" * 20, "balance": usdc}]
        tokens += wallet or []
        fake.reads["/user-assets"] = {"spot": {"available": True, "tokens": tokens}}
        fake.reads["/token-stats"] = {"tokens": [
            {"address": ADDR[r["s"]], "networkId": r["c"], "priceUsd": PRICE[r["s"]]} for r in SAMPLE_BASKET]}
        fake.reads["/token-search"] = {"tokens": [
            {"symbol": r["s"], "address": ADDR[r["s"]], "chainId": r["c"], "verified": True} for r in SAMPLE_BASKET]}
        fake.reads["/trade-executions"] = {"trades": self.rows, "nextCursor": None, "hasMore": False}

    def run_acp(self, argv, **kwargs):
        if argv[0] == "bevo-x":
            return completed(json.dumps({"posts": []}))
        self.argvs.append(argv)
        key = argv[argv.index("--idempotency-key") + 1]
        if self.reply is not None:
            return completed(json.dumps(self.reply))
        amount = float(argv[argv.index("--amount-in") + 1])
        buy = argv[3] == "usdc"
        sym = argv[argv.index("--token-out") + 1] if buy else next(s for s, a in ADDR.items() if a == argv[3])
        tx = "0x" + key[-8:].encode().hex()
        self.rows.append({"txHash": tx, "amountOut": amount / PRICE[sym] if buy else None,
                          "usdcReceived": None if buy else amount * PRICE[sym],
                          **({"tokenOutSymbol": sym, "tokenOutAddress": ADDR[sym], "chainOut": self.cfg["tok"][sym]["chain"]}
                             if buy else {})})
        self.fake.statuses[key] = {"state": "executed", "response": {"txHash": tx}}
        return completed(json.dumps({"status": "accepted", "idempotencyKey": key}))

    def go(self):
        with mock.patch.object(self.duty.subprocess, "run", self.run_acp):
            self.duty.run()


def setup(**kw):
    duty, fake = install()
    return duty, fake, World(duty, fake, **kw)


def deployed_core(duty, cfg, fake, **over):
    core = duty.fresh_core(None, cfg, duty.now())
    core.update(deployed=True, funded_at=duty.iso(duty.now()), cash=1000.0, contrib=5000.0, epoch=1,
                last_rebalance_at="2020-01-01T00:00:00Z",
                pos={"TKNA": {"qty": 1000.0, "cost": 1800.0, "px": 2.0, "addr": ADDR["TKNA"], "chain": 8453},
                     "TKNB": {"qty": 1500.0, "cost": 1400.0, "px": 1.0, "addr": ADDR["TKNB"], "chain": 8453},
                     "TKNC": {"qty": 125.0, "cost": 450.0, "px": 4.0, "addr": ADDR["TKNC"], "chain": 1}})
    core.update(over)
    fake.state["core"] = core
    return core


class KeyTest(unittest.TestCase):
    def test_keys_are_valid_and_distinct(self):
        duty, fake = install()
        keys = {duty.new_key("k3j9a2", e, s, side) for e in (1, 2) for s in ("TKNA", "TKNB") for side in ("buy", "sell")}
        self.assertEqual(len(keys), 8)
        for key in keys:
            self.assertRegex(key, KEY_RE)
            self.assertTrue(key.startswith("xp:svc-1:gk3j9a2:e"))

    def test_a_long_service_id_still_gives_a_valid_key(self):
        duty, fake = install()
        fake.SERVICE_ID = "u" * 200
        key = duty.new_key("abc123", 1, "TKNA", "buy")
        self.assertRegex(key, KEY_RE)
        self.assertNotEqual(key, duty.new_key("abc123", 2, "TKNA", "buy"))


class ArgvTest(unittest.TestCase):
    def test_both_shapes_are_exact_and_end_with_the_key(self):
        duty, fake = install()
        cfg, _ = duty.settings()
        seen = []
        buy = duty.make_leg("TKNA", cfg["tok"]["TKNA"], "buy", 10, None, 1.0)
        sell = duty.make_leg("TKNC", cfg["tok"]["TKNC"], "sell", 10, 3.0, 1.0, {"addr": ADDR["TKNC"], "chain": 1})
        buy.update(amt="12.5", key="k1")
        sell.update(amt="3", key="k2")
        with mock.patch.object(duty.subprocess, "run", lambda argv, **kw: seen.append(argv) or completed("{}")):
            duty.run_acp(buy)
            duty.run_acp(sell)
        c = ADDR["TKNC"]
        self.assertEqual(seen[0], ["acp", "trade", "--token-in", "usdc", "--amount-in", "12.5", "--token-out", "TKNA",
                                   "--chain-out", "8453", "--idempotency-key", "k1"])
        self.assertEqual(seen[1], ["acp", "trade", "--token-in", c, "--chain-in", "1", "--amount-in", "3",
                                   "--token-out", "usdc", "--idempotency-key", "k2"])

    def test_a_timeout_or_missing_cli_is_not_a_refusal(self):
        duty, fake = install()
        cfg, _ = duty.settings()
        leg = duty.make_leg("TKNA", cfg["tok"]["TKNA"], "buy", 10, None, 1.0)
        leg.update(amt="5", key="k")
        with mock.patch.object(duty.subprocess, "run", side_effect=FileNotFoundError):
            self.assertIsNone(duty.run_acp(leg))
        self.assertEqual(duty.classify(None), ("sending", "empty"))


class ClassifyTest(unittest.TestCase):
    def test_outcomes(self):
        duty, _ = install()
        table = [
            ({"status": "accepted"}, ("filed", "")),
            ({"ok": True}, ("filed", "")),
            ({"executed": True}, ("executed", "")),
            ({"asked": True}, ("asked", "")),
            ({"status": "manual_signing_required"}, ("asked", "")),
            ({"status": "refused", "code": "pocket_empty"}, ("refused", "pocket_empty")),
            ({"status": "refused", "code": "INSUFFICIENT_BALANCE"}, ("refused", "wallet_short")),
            ({"status": "refused", "code": "PRICE_IMPACT_HIGH"}, ("refused", "impact")),
            ({"status": "refused", "code": "LIFI_QUOTE_FAILED"}, ("refused", "retryable")),
            ({"status": "refused", "code": "UNVERIFIED_TICKER"}, ("refused", "bug")),
            ({"error": "something odd"}, ("refused", "other")),
            ({"code": "IDEMPOTENT_IN_FLIGHT"}, ("sending", "in_flight")),
            ({"code": "IDEMPOTENCY_KEY_REUSED"}, ("unknown", "unrecognized")),
            ({"unrecognized": True}, ("unknown", "unrecognized")),
            ({"hello": 1}, ("unknown", "unrecognized")),
        ]
        for answer, want in table:
            self.assertEqual(duty.classify(answer), want, answer)

    def test_noisy_output_still_parses(self):
        duty, _ = install()
        self.assertEqual(duty.answer_of('log line\n{"ok": true}'), {"ok": True})
        self.assertIsNone(duty.answer_of("no json here"))


class FirstDeploymentTest(unittest.TestCase):
    def test_buys_exactly_the_approved_weights_with_stored_keys(self):
        duty, fake, world = setup()
        world.go()
        buys = {a[a.index("--token-out") + 1]: a for a in world.argvs}
        self.assertEqual(len(world.argvs), 3)
        want = {"TKNA": "2000", "TKNB": "1500", "TKNC": "500"}  # 40 / 30 / 10 percent of $5,000
        for sym, amount in want.items():
            argv = buys[sym]
            self.assertEqual(argv[:4], ["acp", "trade", "--token-in", "usdc"])
            self.assertEqual(argv[argv.index("--amount-in") + 1], amount)
            self.assertEqual(argv[argv.index("--chain-out") + 1], str(world.cfg["tok"][sym]["chain"]))
            self.assertRegex(argv[-1], KEY_RE)
        core = fake.state["core"]
        self.assertTrue(core["deployed"])
        self.assertIsNone(core["pending"])
        self.assertEqual(core["tx"], {"TKNA": 40, "TKNB": 30, "TKNC": 10})
        self.assertAlmostEqual(core["cash"], 1000.0)
        self.assertAlmostEqual(core["pos"]["TKNA"]["qty"], 1000.0)
        self.assertEqual(core["pos"]["TKNA"]["addr"], ADDR["TKNA"])  # learned from the fill, never filed
        self.assertTrue(any("rebalance #1 settled" in n["text"] for n in fake.notes))

    def test_a_full_basket_is_scaled_to_leave_the_buffer(self):
        duty, fake = install({"HANDLES": ["alice"], "CAPITAL_USD": 1000, "MODE": "run",
                              "BASKET": [dict(SAMPLE_BASKET[0], w=100)]})
        world = World(duty, fake, cash=1000.0)
        world.go()
        spent = float(world.argvs[0][world.argvs[0].index("--amount-in") + 1])
        self.assertLessEqual(spent, 1000.0 - 2.0)
        self.assertGreater(spent, 990.0)

    def test_nothing_trades_while_the_pocket_is_unfunded(self):
        duty, fake, world = setup(cash=0.0, funded=False)
        world.go()
        self.assertEqual(world.argvs, [])
        self.assertFalse(fake.state["core"]["deployed"])

    def test_watch_mode_places_no_trade(self):
        duty, fake = install({"HANDLES": ["alice"], "CAPITAL_USD": 1000, "MODE": "watch", "BASKET": SAMPLE_BASKET})
        world = World(duty, fake)
        world.go()
        self.assertEqual(world.argvs, [])


class RecoveryTest(unittest.TestCase):
    def open_epoch(self, duty, fake, world, states):
        core = deployed_core(duty, world.cfg, fake)
        legs = []
        for sym, side, st in states:
            leg = duty.make_leg(sym, world.cfg["tok"][sym], side, 100.0, 10.0 if side == "sell" else None, PRICE[sym],
                                core["pos"].get(sym))
            leg.update(key=duty.new_key(core["gen"], 2, sym, side), st=st, amt="10" if st != "planned" else None)
            legs.append(leg)
        core["epoch"] = 2
        core["pending"] = {"epoch": 2, "at": duty.iso(duty.now()), "why": "t", "legs": legs, "targets": {},
                           "first": False, "stop_buys": False}
        fake.state["core"] = core
        return core

    def test_a_leg_the_ledger_never_saw_is_resent_with_the_same_key(self):
        duty, fake, world = setup()
        core = self.open_epoch(duty, fake, world, [("TKNA", "sell", "sending")])
        key = core["pending"]["legs"][0]["key"]
        duty.settle(core, duty.now())
        self.assertEqual(len(world.argvs), 0)  # settle resends through send(), which needs the patched run
        with mock.patch.object(duty.subprocess, "run", world.run_acp):
            duty.settle(core, duty.now())
        self.assertEqual([a[-1] for a in world.argvs], [key])
        self.assertEqual(world.argvs[0][world.argvs[0].index("--amount-in") + 1], "10")

    def test_a_leg_the_ledger_has_is_never_resent(self):
        duty, fake, world = setup()
        core = self.open_epoch(duty, fake, world, [("TKNA", "sell", "sending")])
        key = core["pending"]["legs"][0]["key"]
        fake.statuses[key] = {"state": "in_flight"}
        with mock.patch.object(duty.subprocess, "run", world.run_acp):
            duty.settle(core, duty.now())
        self.assertEqual(world.argvs, [])
        self.assertEqual(core["pending"]["legs"][0]["st"], "filed")

    def test_a_stale_epoch_is_cancelled_not_sent_late(self):
        duty, fake, world = setup()
        core = self.open_epoch(duty, fake, world, [("TKNA", "sell", "sending")])
        core["pending"]["at"] = "2020-01-01T00:00:00Z"
        with mock.patch.object(duty.subprocess, "run", world.run_acp):
            duty.settle(core, duty.now())
        self.assertEqual(world.argvs, [])
        self.assertEqual(core["pending"]["legs"][0]["st"], "cancelled")

    def test_an_executed_leg_is_applied_once_from_the_receipt(self):
        duty, fake, world = setup()
        core = self.open_epoch(duty, fake, world, [("TKNB", "sell", "filed")])
        leg = core["pending"]["legs"][0]
        fake.statuses[leg["key"]] = {"state": "executed", "txHash": "0xabc"}
        world.rows.append({"txHash": "0xABC", "usdcReceived": 9.5})
        duty.settle(core, duty.now())
        duty.settle(core, duty.now())
        self.assertEqual(leg["st"], "applied")
        self.assertAlmostEqual(core["cash"], 1009.5)
        self.assertAlmostEqual(core["pos"]["TKNB"]["qty"], 1490.0)

    def test_a_missing_receipt_falls_back_to_a_labelled_estimate(self):
        duty, fake, world = setup()
        core = self.open_epoch(duty, fake, world, [("TKNB", "sell", "filed")])
        leg = core["pending"]["legs"][0]
        fake.statuses[leg["key"]] = {"state": "executed"}
        for _ in range(duty.FILL_TICKS):
            duty.settle(core, duty.now())
        self.assertEqual(leg["fill"]["src"], "estimate")

    def test_a_refusal_and_a_rejected_approval_are_terminal(self):
        duty, fake, world = setup()
        core = self.open_epoch(duty, fake, world, [("TKNA", "sell", "filed"), ("TKNB", "sell", "asked")])
        a, b = core["pending"]["legs"]
        fake.statuses[a["key"]] = {"state": "refused"}
        fake.statuses[b["key"]] = {"state": "manual", "approvalStatus": "rejected"}
        duty.settle(core, duty.now())
        self.assertEqual((a["st"], b["st"]), ("refused", "cancelled"))

    def test_a_restart_mid_rebalance_resumes_and_keeps_the_worldview(self):
        duty, fake, world = setup(cash=1000.0, usdc=1000.0)
        core = self.open_epoch(duty, fake, world, [("TKNA", "sell", "sending"), ("TKNC", "buy", "planned")])
        fake.state["wv"] = {"version": 7, "tok": {}, "tw": {}}
        sell_key = core["pending"]["legs"][0]["key"]
        fake.state["core"] = core
        world.go()
        self.assertEqual(fake.state["wv"]["version"], 7)
        keys = [a[-1] for a in world.argvs]
        self.assertEqual(keys[0], sell_key)
        self.assertEqual(len(set(keys)), len(keys))
        self.assertEqual(fake.state["core"]["epoch"], 2)

    def test_a_replayed_run_never_makes_a_second_epoch_for_the_same_legs(self):
        duty, fake, world = setup()
        world.go()
        first = [a[-1] for a in world.argvs]
        world.go()
        self.assertEqual([a[-1] for a in world.argvs], first)  # nothing new was sent


class RebalanceTest(unittest.TestCase):
    def test_a_persisted_change_sells_first_then_buys_within_the_cash(self):
        duty, fake, world = setup(cash=1000.0, usdc=1000.0)
        deployed_core(duty, world.cfg, fake, cand={"TKNA": {"pct": 10, "n": 2}, "TKNB": {"pct": 50, "n": 2}})
        world.go()
        sides = ["buy" if a[3] == "usdc" else "sell" for a in world.argvs]
        self.assertEqual(sides, sorted(sides, key=lambda s: s != "sell"))
        self.assertIn("sell", sides)
        self.assertIn("buy", sides)
        self.assertEqual(fake.state["core"]["tx"]["TKNA"], 10)
        spent = sum(float(a[a.index("--amount-in") + 1]) for a in world.argvs if a[3] == "usdc")
        self.assertLess(spent, 1000.0 + 0.99 * 1000.0 * 2.0)

    def test_the_spacing_blocks_a_second_rebalance(self):
        duty, fake, world = setup(cash=1000.0)
        deployed_core(duty, world.cfg, fake, last_rebalance_at=duty.iso(duty.now()), cand={"TKNA": {"pct": 10, "n": 2}})
        world.go()
        self.assertEqual(world.argvs, [])

    def test_a_single_review_never_trades(self):
        duty, fake, world = setup(cash=1000.0)
        deployed_core(duty, world.cfg, fake, cand={"TKNA": {"pct": 5, "n": 1}})
        world.go()
        self.assertEqual(world.argvs, [])

    def test_a_drawdown_halts_buys_but_not_sells(self):
        duty, fake, world = setup(cash=100.0, usdc=100.0)
        deployed_core(duty, world.cfg, fake, cash=100.0, contrib=10000.0, dd_at="2020-01-01T00:00:00Z",
                      cand={"TKNA": {"pct": 0, "n": 2}, "TKNB": {"pct": 60, "n": 2}})
        world.go()
        self.assertTrue(fake.state["core"]["halted"])
        self.assertTrue(world.argvs)
        self.assertTrue(all(a[2] == "--token-in" and a[3] != "usdc" for a in world.argvs))
        self.assertTrue(any(n["push"] for n in fake.notes))

    def test_a_refusal_for_lack_of_usdc_stops_the_buys_and_alerts(self):
        duty, fake, world = setup()
        world.reply = {"status": "refused", "code": "INSUFFICIENT_BALANCE"}
        world.go()
        self.assertEqual(len(world.argvs), 1)  # the first buy was refused; the rest were not sent
        self.assertTrue(any(n["push"] and "USDC" in n["push"] for n in fake.notes))

    def test_a_bug_class_refusal_fails_the_run(self):
        duty, fake, world = setup()
        world.reply = {"status": "refused", "code": "UNVERIFIED_TICKER"}
        world.go()
        self.assertTrue(fake.fails)


def verdict(tkna, tkn_b=30, tkn_c=10, ref="101", change="TKNA turned cautious"):
    return {"tokens": [{"sym": "TKNA", "thesis": "Alice turned cautious on TKNA", "stance": -1, "confidence": 0.7,
                        "for": [], "against": [ref]}],
            "accounts": [{"handle": "alice", "stance": "cautious", "record": "none"}],
            "sentiment": {"score": -1, "why": "caution is spreading"},
            "targets": [{"sym": "TKNA", "pct": tkna}, {"sym": "TKNB", "pct": tkn_b}, {"sym": "TKNC", "pct": tkn_c}],
            "claims": [], "out_of_basket": [], "changes": [change]}


def queued(duty, pid):
    return {"id": pid, "h": "alice", "at": duty.iso(duty.now()), "text": "TKNA looks weak to me this week, trimming", "conv": pid}


class ReviewFlowTest(unittest.TestCase):
    def test_an_unfunded_review_posts_the_ready_note_and_trades_nothing(self):
        duty, fake, world = setup(cash=0.0, funded=False)
        fake.state["queue"] = [queued(duty, "101")]
        fake.prompt_answers = [verdict(40)]
        world.go()
        ready = [n["text"] for n in fake.notes if "| ready |" in n["text"]]
        self.assertEqual(len(ready), 1)
        self.assertIn("TKNA 40%", ready[0])
        self.assertIn("cash 20%", ready[0])
        self.assertEqual(world.argvs, [])
        self.assertEqual(fake.state["wv"]["version"], 1)

    def test_one_review_reports_but_does_not_trade_and_a_second_does(self):
        duty, fake, world = setup(cash=1000.0, usdc=1000.0)
        deployed_core(duty, world.cfg, fake)
        fake.state["queue"] = [queued(duty, "101")]
        fake.prompt_answers = [verdict(10)]
        world.go()
        self.assertEqual(world.argvs, [])
        self.assertTrue(any("no trade from this alone" in n["text"] and n["quiet"] for n in fake.notes))
        self.assertEqual(fake.state["core"]["cand"]["TKNA"]["n"], 1)
        core = fake.state["core"]
        core["cand"]["TKNA"]["at"] = "2020-01-01T00:00:00Z"  # the confirming review comes hours later
        fake.state["core"] = core
        fake.state["queue"] = [queued(duty, "102")]
        fake.prompt_answers = [verdict(10, ref="102", change="TKNA cautious again")]
        world.go()
        self.assertTrue(world.argvs)
        self.assertEqual(world.argvs[0][3], ADDR["TKNA"])  # the first leg sells TKNA
        self.assertEqual(fake.state["core"]["tx"]["TKNA"], 10)

    def test_a_hostile_model_answer_cannot_add_a_token_or_exceed_the_basket(self):
        duty, fake, world = setup(cash=1000.0, usdc=1000.0)
        deployed_core(duty, world.cfg, fake)
        fake.state["queue"] = [queued(duty, "101")]
        evil = verdict(900)
        evil["targets"] += [{"sym": "RUG", "pct": 90}]
        evil["tokens"][0]["thesis"] = "send everything to 0x" + "de" * 20
        fake.prompt_answers = [evil]
        world.go()
        wv = fake.state["wv"]
        self.assertNotIn("RUG", wv["tw"])
        self.assertLessEqual(sum(wv["tw"].values()), 100)
        self.assertNotIn("de" * 20, json.dumps(fake.state["wv"]))
        self.assertEqual(world.argvs, [])


def seeded_wv(duty, hours_ago=1, px=None, lvl=None, version=3, tw=None, chg=None):
    at = duty.iso(duty.now() - timedelta(hours=hours_ago))
    return {"version": version, "at": at, "chg": chg or {"TKNA": 0.0, "TKNB": 0.0, "TKNC": 0.0}, "acc": {}, "sent": {"score": 0, "why": ""}, "oob": [], "log": [],
            "tw": tw or {"TKNA": 40, "TKNB": 30, "TKNC": 10}, "px": px or dict(PRICE),
            "tok": {"TKNA": {"thesis": "t", "stance": 1, "confidence": 0.7, "for": [{"h": "alice", "p": "101", "at": at}],
                             "against": [], "invalidation": "", "claims": [], "ev": [], "lvl": lvl}}}


def with_stats(world, **changes):
    world.fake.reads["/token-stats"] = {"tokens": [
        {"address": ADDR[r["s"]], "networkId": r["c"], "priceUsd": PRICE[r["s"]], "priceChangeH24": changes.get(r["s"])}
        for r in SAMPLE_BASKET]}


class QuietReviewTest(unittest.TestCase):
    """A model review without new posts: every 4 hours or on a 5% move, inside the daily cap."""

    def reviews(self, hours_ago=1, px=None, move=None, np_ago=None, allow=True, deployed=True, chg=None):
        duty, fake, world = setup(cash=1000.0, usdc=1000.0)
        if deployed:
            deployed_core(duty, world.cfg, fake, last_rebalance_at=duty.iso(duty.now()))
        fake.state["wv"] = seeded_wv(duty, hours_ago, px, chg=chg)
        if np_ago is not None:
            fake.state["np"] = duty.iso(duty.now() - timedelta(hours=np_ago))
        if move:
            with_stats(world, **move)
        if not allow:
            fake.allow = lambda key, **kw: key != "model"
        fake.prompt_answers = [verdict(40)]
        world.go()
        return len(fake.prompts), fake, world

    def test_it_fires_after_four_hours_and_not_before(self):
        self.assertEqual(self.reviews(hours_ago=5)[0], 1)
        self.assertEqual(self.reviews(hours_ago=3)[0], 0)

    def test_it_fires_on_a_five_percent_move_since_the_last_review_or_in_24h(self):
        self.assertEqual(self.reviews(px=dict(PRICE, TKNB=0.9))[0], 1)  # +11% since the last review
        self.assertEqual(self.reviews(px=dict(PRICE, TKNB=0.98))[0], 0)  # +2%
        self.assertEqual(self.reviews(move={"TKNC": -6.0})[0], 1)
        self.assertEqual(self.reviews(move={"TKNC": -4.0})[0], 0)
        # a token that is simply volatile (same 24h change as at the last review) is not a new move
        self.assertEqual(self.reviews(move={"TKNC": -6.0}, chg={"TKNC": -6.0})[0], 0)

    def test_it_respects_the_gap_the_daily_cap_and_the_deployed_state(self):
        self.assertEqual(self.reviews(hours_ago=9, np_ago=1)[0], 0)
        self.assertEqual(self.reviews(hours_ago=9, allow=False)[0], 0)
        self.assertEqual(self.reviews(hours_ago=9, deployed=False)[0], 0)

    def test_a_no_post_review_updates_the_worldview_and_the_prompt_says_nothing_is_new(self):
        count, fake, _ = self.reviews(hours_ago=5)
        self.assertEqual(fake.state["wv"]["version"], 4)
        self.assertIn("None are new this review.", fake.prompts[0]["text"])
        self.assertIn("volume", fake.prompts[0]["text"])


class InvalidationExitTest(unittest.TestCase):
    """A market-verified level from an earlier review: de-risking sells only, spacing aside."""

    def go(self, lvl=None, tw=None, version=3, move=None, spacing=True):
        duty, fake, world = setup(cash=1000.0, usdc=1000.0)
        deployed_core(duty, world.cfg, fake, last_rebalance_at=duty.iso(duty.now()) if spacing else "2020-01-01T00:00:00Z")
        fake.state["wv"] = seeded_wv(duty, 1, lvl=lvl, tw=tw, version=version)
        if move:
            with_stats(world, **move)
        world.go()
        return fake, world

    def test_it_sells_down_to_the_models_target_and_ignores_the_spacing(self):
        fake, world = self.go({"m": "price_below", "x": 2.5, "v": 2, "at": OLD}, tw={"TKNA": 20, "TKNB": 30, "TKNC": 10})
        self.assertEqual([(a[3], a[a.index("--amount-in") + 1]) for a in world.argvs], [(ADDR["TKNA"], "500")])
        self.assertEqual(fake.state["core"]["tx"]["TKNA"], 20)
        note = [n for n in fake.notes if "| invalidation |" in n["text"]]
        self.assertTrue(note and note[0]["quiet"] and "price below 2.5" in note[0]["text"])

    def test_a_zero_target_exits_the_whole_position(self):
        _, world = self.go({"m": "price_below", "x": 2.5, "v": 2, "at": OLD}, tw={"TKNA": 0, "TKNB": 30, "TKNC": 10})
        self.assertEqual(world.argvs[0][world.argvs[0].index("--amount-in") + 1], "1000")

    def test_it_never_buys_and_never_sells_up_to_a_higher_target(self):
        _, world = self.go({"m": "price_below", "x": 2.5, "v": 2, "at": OLD}, tw={"TKNA": 60, "TKNB": 40, "TKNC": 10})
        self.assertEqual(world.argvs, [])  # spacing blocks the rebalance and the level only allows sells

    def test_a_level_set_by_the_latest_review_is_not_armed(self):
        _, world = self.go({"m": "price_below", "x": 2.5, "v": 3, "at": OLD}, tw={"TKNA": 0, "TKNB": 30, "TKNC": 10})
        self.assertEqual(world.argvs, [])

    def test_an_unbroken_level_does_nothing_and_a_24h_level_is_checked_against_token_stats(self):
        _, world = self.go({"m": "price_below", "x": 1.5, "v": 2, "at": OLD}, tw={"TKNA": 0})
        self.assertEqual(world.argvs, [])
        _, world = self.go({"m": "change_24h_below", "x": -10.0, "v": 2, "at": OLD}, tw={"TKNA": 0, "TKNB": 30, "TKNC": 10},
                           move={"TKNA": -12.0})
        self.assertEqual(len(world.argvs), 1)

    def test_a_post_cannot_set_it_off_in_the_same_review(self):
        duty, fake, world = setup(cash=1000.0, usdc=1000.0)
        deployed_core(duty, world.cfg, fake, last_rebalance_at=duty.iso(duty.now()))
        fake.state["queue"] = [queued(duty, "101")]
        ans = verdict(0, 30, 10)
        ans["tokens"][0]["level"] = {"metric": "price_below", "value": 2.5}  # already broken at 2.0: dropped
        fake.prompt_answers = [ans]
        world.go()
        self.assertIsNone(fake.state["wv"]["tok"]["TKNA"]["lvl"])
        self.assertEqual(world.argvs, [])

    @staticmethod
    def duty_now():
        return install()[0].iso(install()[0].now())

    def test_a_young_level_is_not_armed(self):
        _, world = self.go({"m": "price_below", "x": 2.5, "v": 2, "at": self.duty_now()}, tw={"TKNA": 0, "TKNB": 30, "TKNC": 10})
        self.assertEqual(world.argvs, [])

    def test_a_level_fires_once_even_when_the_order_is_refused(self):
        duty, fake, world = setup(cash=1000.0, usdc=1000.0)
        deployed_core(duty, world.cfg, fake)
        fake.state["wv"] = seeded_wv(duty, 1, lvl={"m": "price_below", "x": 2.5, "v": 2, "at": OLD}, tw={"TKNA": 0})
        world.reply = {"status": "refused", "code": "PRICE_IMPACT_HIGH"}
        world.go()
        world.go()
        self.assertEqual(len(world.argvs), 1)
        self.assertEqual(fake.state["core"]["fired"], {"TKNA": ["price_below", 2.5, 2]})

    def test_it_waits_for_a_settings_change_and_cannot_raise_a_target(self):
        _, world = self.go({"m": "price_below", "x": 2.5, "v": 2, "at": OLD}, tw={"TKNA": 0, "TKNB": 30, "TKNC": 10})
        self.assertEqual(len(world.argvs), 1)
        duty, fake, world = setup(cash=1000.0, usdc=1000.0)
        deployed_core(duty, world.cfg, fake, force="mandate")
        fake.state["wv"] = seeded_wv(duty, 1, lvl={"m": "price_below", "x": 2.5, "v": 2, "at": OLD}, tw={"TKNA": 0})
        world.go()
        self.assertEqual(world.argvs, [])
        duty, fake, world = setup(cash=1000.0, usdc=1000.0)
        deployed_core(duty, world.cfg, fake, tx={"TKNA": 10, "TKNB": 30, "TKNC": 10})
        fake.state["wv"] = seeded_wv(duty, 1, lvl={"m": "price_below", "x": 2.5, "v": 2, "at": OLD}, tw={"TKNA": 30})
        world.go()
        self.assertEqual(world.argvs[0][world.argvs[0].index("--amount-in") + 1], "750")  # to the target in force, 10%
        self.assertEqual(fake.state["core"]["tx"]["TKNA"], 10)

    def test_watch_mode_places_no_exit(self):
        duty, fake = install(dict(__import__("fake_bevo").SAMPLE_PARAMS, MODE="watch"))
        world = World(duty, fake, cash=1000.0, usdc=1000.0)
        deployed_core(duty, world.cfg, fake)
        fake.state["wv"] = seeded_wv(duty, lvl={"m": "price_below", "x": 2.5, "v": 2, "at": OLD}, tw={"TKNA": 0})
        world.go()
        self.assertEqual(world.argvs, [])


class UnwindTest(unittest.TestCase):
    def test_sells_the_book_then_finishes(self):
        duty, fake = install({"HANDLES": ["alice"], "CAPITAL_USD": 1000, "MODE": "unwind", "BASKET": SAMPLE_BASKET})
        world = World(duty, fake, cash=1000.0)
        deployed_core(duty, world.cfg, fake)
        with self.assertRaises(SystemExit):
            world.go()
        self.assertEqual(len(world.argvs), 3)
        self.assertTrue(all(a[3] != "usdc" for a in world.argvs))
        self.assertEqual(len(fake.dones), 1)
        self.assertIn("sold everything", fake.dones[0])


BTC_CB = "0x" + "cb" * 20


class AddressFromFillTest(unittest.TestCase):
    """The basket names a symbol and a chain; the contract is learned from what the rail delivered."""

    def world(self, sym, row_sym, row_addr, wallet_row=True, mode="run", search=None, chain=8453, net=8453):
        duty, fake = install({"HANDLES": ["alice"], "CAPITAL_USD": 1000, "MODE": mode,
                              "BASKET": [{"s": sym, "c": chain, "w": 50}]})
        self.duty, self.fake, self.argvs, rows = duty, fake, [], []
        usdc = {"symbol": "USDC", "chainId": chain, "tokenAddress": "0x" + "dd" * 20, "balance": 1000.0}
        held = {"symbol": row_sym, "chainId": chain, "tokenAddress": row_addr, "balance": 250.0, "usdValueUsd": 500.0}
        fake.reads["/duties"] = {"duties": [{"id": "svc-1", "pocket": {"cashUsdc": 1000.0, "funded": True, "holdings": []}}]}
        fake.reads["/user-assets"] = {"spot": {"available": True, "tokens": [usdc] + ([held] if wallet_row else [])}}
        fake.reads["/token-stats"] = {"tokens": [{"address": row_addr or "native", "networkId": net, "priceUsd": 2.0}]}
        fake.reads["/token-search"] = {"tokens": search if search is not None else [
            {"symbol": row_sym, "address": row_addr or "native", "chainId": chain, "verified": True, "matchedAlias": sym != row_sym}]}
        fake.reads["/trade-executions"] = {"trades": rows}

        def run(argv, **kwargs):
            if argv[0] == "bevo-x":
                return completed(json.dumps({"posts": []}))
            self.argvs.append(argv)
            key = argv[-1]
            tx = "0x" + key[-8:].encode().hex()
            buy = argv[3] == "usdc"
            rows.append({"txHash": tx, "amountOut": 250.0 if buy else None, "usdcReceived": None if buy else 450.0,
                         **({"tokenOutSymbol": row_sym, "tokenOutAddress": row_addr, "chainOut": chain} if buy else {})})
            fake.statuses[key] = {"state": "executed", "response": {"txHash": tx}}
            return completed(json.dumps({"status": "accepted"}))

        self.run_acp = run

    def go(self):
        with mock.patch.object(self.duty.subprocess, "run", self.run_acp):
            self.duty.run()

    def test_an_alias_is_bought_by_symbol_and_the_delivered_token_is_sold_by_its_address(self):
        self.world("BTC", "cbBTC", BTC_CB)
        self.go()
        self.assertEqual(self.argvs[0][:8], ["acp", "trade", "--token-in", "usdc", "--amount-in", self.argvs[0][5],
                                             "--token-out", "BTC"])
        self.assertEqual(self.argvs[0][8:10], ["--chain-out", "8453"])
        self.assertEqual(self.fake.state["core"]["pos"]["BTC"]["addr"], BTC_CB)
        self.duty.MODE = "unwind"
        with self.assertRaises(SystemExit):
            self.go()
        sell = self.argvs[1]
        self.assertEqual((sell[3], sell[4], sell[5]), (BTC_CB, "--chain-in", "8453"))
        self.assertEqual(sell[sell.index("--amount-in") + 1], "250")

    def test_a_gas_coin_is_matched_by_symbol_and_sold_by_its_ticker(self):
        self.world("ETH", "ETH", None, search=[], net=1)  # server shape: one native ETH row, on network 1
        self.go()
        self.assertEqual(self.argvs[0][7], "ETH")
        self.assertEqual(self.fake.state["core"]["pos"]["ETH"]["addr"], "native")
        self.assertIn(("/token-stats", {"tokens": "native:1"}), self.fake.read_log)
        self.duty.MODE = "unwind"
        with self.assertRaises(SystemExit):
            self.go()
        self.assertEqual(self.argvs[1][3:6], ["ETH", "--chain-in", "8453"])

    def test_a_ticker_bought_with_the_dollar_leg_is_not_mistaken_for_a_gas_coin(self):
        for raw in (None, "0x" + "e" * 40, "1" * 32):
            self.world("PEPE", "PEPE", raw, chain=1, net=1)
            self.fake.reads["/token-search"] = {"tokens": [{"symbol": "PEPE", "address": ADDR["TKNA"], "chainId": 1,
                                                            "verified": True}]}
            self.fake.reads["/user-assets"]["spot"]["tokens"][1]["tokenAddress"] = None
            self.fake.reads["/token-stats"] = {"tokens": [{"address": ADDR["TKNA"], "networkId": 1, "priceUsd": 2.0}]}
            self.go()
            pos = self.fake.state["core"]["pos"]["PEPE"]
            self.assertNotEqual(pos.get("addr"), "native" if raw is None else None)
            self.assertNotIn(("/token-stats", {"tokens": "native:1"}), self.fake.read_log)

    def test_a_position_without_an_address_takes_it_from_its_holding_row(self):
        self.world("TKNA", "TKNA", ADDR["TKNA"], mode="unwind")
        self.fake.state["core"] = dict(self.duty.fresh_core(None, self.duty.settings()[0], self.duty.now()), deployed=True,
                                       wait_done=True, funded_at=self.duty.iso(self.duty.now()), cash=1000.0, contrib=1000.0,
                                       pos={"TKNA": {"qty": 250.0, "cost": None, "px": None, "addr": None, "chain": 8453}})
        with self.assertRaises(SystemExit):
            self.go()
        self.assertEqual(self.argvs[0][3], ADDR["TKNA"])

    def test_an_unresolved_position_is_skipped_with_a_reason_never_guessed(self):
        self.world("TKNA", "TKNA", ADDR["TKNA"], wallet_row=False)
        core = self.duty.fresh_core(None, self.duty.settings()[0], self.duty.now())
        core.update(deployed=True, wait_done=True, funded_at=self.duty.iso(self.duty.now()), cash=1000.0, contrib=1000.0, epoch=1,
                    last_rebalance_at="2020-01-01T00:00:00Z", tx={"TKNA": 50}, cand={"TKNA": {"pct": 0, "n": 2}},
                    pos={"TKNA": {"qty": 250.0, "cost": None, "px": 2.0, "addr": None, "chain": 8453}})
        self.fake.state["core"] = core
        self.go()
        self.assertEqual([a for a in self.argvs if a[3] != "usdc"], [])
        self.assertTrue(any("TKNA skipped" in line for line in self.fake.logs))
        self.assertIsNone(self.fake.state["core"]["pos"]["TKNA"]["addr"])

    def test_unwind_leaves_an_unresolved_position_unsold_and_says_so(self):
        self.world("TKNA", "TKNA", ADDR["TKNA"], wallet_row=False, mode="unwind")
        core = self.duty.fresh_core(None, self.duty.settings()[0], self.duty.now())
        core.update(deployed=True, wait_done=True, funded_at=self.duty.iso(self.duty.now()), cash=1000.0, contrib=1000.0,
                    pos={"TKNA": {"qty": 250.0, "cost": None, "px": 2.0, "addr": None, "chain": 8453}})
        self.fake.state["core"] = core
        with self.assertRaises(SystemExit):
            self.go()
        self.assertEqual(self.argvs, [])
        self.assertIn("Left unsold: TKNA", self.fake.dones[0])


class MarketIdTest(unittest.TestCase):
    """Before a fill the market id is the VERIFIED token-search row on the basket's chain, kept a day."""

    def setUp(self):
        self.duty, self.fake = install({"HANDLES": ["alice"], "CAPITAL_USD": 1000, "MODE": "watch",
                                        "BASKET": [{"s": "TKNA", "c": 8453, "w": 50}]})
        self.cfg = self.duty.settings()[0]
        self.fake.reads["/token-search"] = {"tokens": [
            {"symbol": "TKNA", "address": "0x" + "11" * 20, "chainId": 1, "verified": True},
            {"symbol": "TKNA", "address": "0x" + "22" * 20, "chainId": 8453, "verified": False},
            {"symbol": "OTHER", "address": "0x" + "33" * 20, "chainId": 8453, "verified": True},
            {"symbol": "TKNA", "address": ADDR["TKNA"], "chainId": 8453, "verified": True}]}
        self.core = self.duty.fresh_core(None, self.cfg, self.duty.now())

    def searches(self):
        return [p for path, p in self.fake.read_log if path == "/token-search"]

    def test_the_verified_row_on_the_right_chain_is_used_and_cached_for_a_day(self):
        self.assertEqual(self.duty.market_ids(self.cfg, self.core, self.duty.now()), {"TKNA": ADDR["TKNA"]})
        self.duty.market_ids(self.cfg, self.core, self.duty.now() + timedelta(hours=23))
        self.assertEqual(self.searches(), [{"q": "TKNA"}])
        self.duty.market_ids(self.cfg, self.core, self.duty.now() + timedelta(hours=25))
        self.assertEqual(len(self.searches()), 2)

    def test_no_verified_row_means_no_id_and_a_new_look_next_time(self):
        self.fake.reads["/token-search"] = {"tokens": [{"symbol": "TKNA", "address": ADDR["TKNA"], "chainId": 8453, "verified": False}]}
        self.assertEqual(self.duty.market_ids(self.cfg, self.core, self.duty.now()), {"TKNA": None})
        self.duty.market_ids(self.cfg, self.core, self.duty.now())
        self.assertEqual(len(self.searches()), 2)

    def test_the_learned_address_wins_and_no_search_is_made(self):
        self.core["pos"]["TKNA"] = {"qty": 1.0, "addr": "0x" + "44" * 20, "chain": 8453}
        self.assertEqual(self.duty.market_ids(self.cfg, self.core, self.duty.now()), {"TKNA": "0x" + "44" * 20})
        self.assertEqual(self.searches(), [])

    def test_a_token_with_no_market_row_is_valued_from_the_wallet_and_the_model_is_told(self):
        world = World(self.duty, self.fake, cash=1000.0)
        deployed_core(self.duty, self.cfg, self.fake, tx={"TKNA": 50}, pos={
            "TKNA": {"qty": 250.0, "cost": 400.0, "px": None, "addr": ADDR["TKNA"], "chain": 8453}})
        self.fake.reads["/token-stats"] = {"tokens": []}
        world.fake.reads["/user-assets"] = {"spot": {"available": True, "tokens": [
            {"symbol": "USDC", "chainId": 8453, "tokenAddress": "0x" + "dd" * 20, "balance": 1000.0},
            {"symbol": "TKNA", "chainId": 8453, "tokenAddress": ADDR["TKNA"], "balance": 250.0, "usdValueUsd": 500.0}]}}
        self.fake.state["queue"] = [queued(self.duty, "101")]
        self.fake.prompt_answers = [verdict(50, 0, 0)]
        world.go()
        self.assertAlmostEqual(self.fake.state["core"]["pos"]["TKNA"]["px"], 2.0)
        self.assertIn("market data MISSING", self.fake.prompts[0]["text"])


if __name__ == "__main__":
    unittest.main()
