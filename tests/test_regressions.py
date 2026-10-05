"""Review findings kept fixed: real ledger shapes, stuck legs, removals, unwind, mandate edits, scrub."""

import json
import unittest
from datetime import timedelta

from fake_bevo import SAMPLE_BASKET, install
from test_execution import World, deployed_core, setup


class LedgerShapeTest(unittest.TestCase):
    def test_a_deployment_runs_to_applied_on_the_servers_dto_shapes(self):
        duty, fake, world = setup()
        world.go()
        core = fake.state["core"]
        self.assertTrue(core["deployed"])
        self.assertIsNone(core["pending"])
        self.assertEqual(set(core["pos"]), {"TKNA", "TKNB", "TKNC"})

    def test_the_hash_is_read_from_the_response_and_rows_from_trades(self):
        duty, fake, world = setup()
        fake.reads["/trade-executions"] = {"trades": [{"txHash": "0xAB"}], "nextCursor": None}
        self.assertEqual(duty.trade_rows(), [{"txHash": "0xAB"}])
        core = deployed_core(duty, world.cfg, fake)
        leg = duty.make_leg("TKNA", world.cfg["tok"]["TKNA"], "buy", 100.0, None, 2.0)
        leg.update(key="k1", st="filed", amt="100")
        core["pending"] = {"epoch": 2, "at": duty.iso(duty.now()), "why": "t", "legs": [leg], "targets": {}, "first": False,
                           "stop_buys": False}
        fake.statuses["k1"] = {"state": "executed", "response": {"txHash": "0xab"}}
        world.rows.append({"txHash": "0xAB", "amountOut": 49.0})
        fake.reads["/trade-executions"] = {"trades": world.rows}
        duty.settle(core, duty.now())
        self.assertEqual(leg["st"], "applied")
        self.assertEqual(leg["fill"]["qty"], 49.0)


class StuckLegTest(unittest.TestCase):
    def test_an_unknown_leg_ages_out_and_the_epoch_closes(self):
        duty, fake, world = setup()
        core = deployed_core(duty, world.cfg, fake)
        leg = duty.make_leg("TKNA", world.cfg["tok"]["TKNA"], "buy", 100.0, None, 2.0)
        leg.update(key="k1", st="unknown", amt="100", sent_at="2020-01-01T00:00:00Z")
        core["pending"] = {"epoch": 2, "at": "2020-01-01T00:00:00Z", "why": "t", "legs": [leg], "targets": {}, "first": False,
                           "stop_buys": False}
        fake.statuses["k1"] = {"state": "unknown"}
        duty.settle(core, duty.now())
        duty.close_epoch(core)
        self.assertIsNone(core["pending"])
        self.assertEqual(core["resync"], ["TKNA"])

    def test_an_in_flight_conflict_waits_and_an_unrecognised_one_is_unknown(self):
        duty, fake, world = setup()
        self.assertEqual(duty.classify({"status": "conflict", "code": "IDEMPOTENT_IN_FLIGHT"})[0], "sending")
        self.assertEqual(duty.classify({"status": "conflict", "code": "IDEMPOTENT_UNKNOWN_OUTCOME"})[0], "unknown")


class BasketEditTest(unittest.TestCase):
    def test_an_owner_weight_change_stays_the_target(self):
        duty, fake, world = setup()
        core = deployed_core(duty, world.cfg, fake)
        core["tx"] = {"TKNA": 40, "TKNB": 30, "TKNC": 10}
        core["cfg_w"] = {"TKNA": 40, "TKNB": 30, "TKNC": 10}
        cfg = dict(world.cfg, tok={s: dict(r, w=30 if s == "TKNC" else r["w"]) for s, r in world.cfg["tok"].items()})
        core["halted"] = "2026-01-01T00:00:00Z"
        duty.mandate_change(core, cfg)
        self.assertEqual(duty.effective_targets(core, cfg)["TKNC"], 30)
        self.assertEqual(core["force"], "mandate")
        self.assertTrue(core["halted"])

    def test_a_removed_token_stays_held_valued_and_sold_on_unwind(self):
        params = {"HANDLES": ["alice"], "CAPITAL_USD": 1000, "MODE": "unwind", "BASKET": SAMPLE_BASKET[1:]}
        duty, fake = install(params)
        world = World(duty, fake, cash=1000.0)
        full = install()[0].settings()[0]
        world.cfg = full
        core = deployed_core(duty, full, fake)
        core["pos"]["TKNA"].update(addr=SAMPLE_BASKET[0]["a"], chain=8453)
        fake.state["core"] = core
        with self.assertRaises(SystemExit):
            world.go()
        sold = [a[3] for a in world.argvs]
        self.assertIn(SAMPLE_BASKET[0]["a"], sold)
        self.assertEqual(fake.state["core"]["pos"], {})

    def test_a_refused_unwind_sell_is_not_resent_and_the_duty_still_finishes(self):
        duty, fake = install({"HANDLES": ["alice"], "CAPITAL_USD": 1000, "MODE": "unwind", "BASKET": SAMPLE_BASKET})
        world = World(duty, fake, cash=1000.0)
        deployed_core(duty, world.cfg, fake)
        world.reply = {"status": "refused", "code": "PRICE_IMPACT_HIGH"}
        with self.assertRaises(SystemExit):
            world.go()
        self.assertEqual(len(world.argvs), 3)
        self.assertIn("(refused)", fake.dones[0])


class ExitTurnoverTest(unittest.TestCase):
    def test_an_all_zero_target_exits_within_the_turnover_cap(self):
        duty, fake, world = setup()
        core = deployed_core(duty, world.cfg, fake)
        nav, vals = duty.valuation(core, world.cfg, {s: {"p": p} for s, p in {"TKNA": 2.0, "TKNB": 1.0, "TKNC": 4.0}.items()})
        market = {s: {"p": p} for s, p in {"TKNA": 2.0, "TKNB": 1.0, "TKNC": 4.0}.items()}
        legs, _ = duty.plan_legs(core, world.cfg, {"TKNA": 0, "TKNB": 0, "TKNC": 0}, nav, vals, market, False)
        self.assertLessEqual(sum(l["usd_plan"] for l in legs), nav * duty.TURNOVER_CAP + 1)


class ScrubTest(unittest.TestCase):
    def test_a_mint_and_a_bare_domain_do_not_reach_a_note(self):
        duty, fake = install()
        text = duty.scrub("CA 7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU at pump.fun/coin/abc or t.me/scamdrop", 300)
        for bad in ("7xKXtg2", "pump.fun", "t.me"):
            self.assertNotIn(bad, text)


if __name__ == "__main__":
    unittest.main()
