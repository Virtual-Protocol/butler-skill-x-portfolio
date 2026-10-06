"""Settings, targets, persistence, planning and sizing: the guards code owns."""

import copy
import json
import unittest
from datetime import timedelta

import fake_bevo
from fake_bevo import ADDR, SAMPLE_BASKET, SAMPLE_PARAMS, install


def cfg_of(duty):
    cfg, problems = duty.settings()
    assert not problems, problems
    return cfg


def new_core(duty, cfg):
    return duty.fresh_core(None, cfg, duty.now())


class SettingsTest(unittest.TestCase):
    def check(self, **change):
        params = copy.deepcopy(SAMPLE_PARAMS)
        params.update(change)
        duty, _ = install(params)
        return duty.settings()

    def test_sample_is_valid(self):
        cfg, problems = self.check()
        self.assertEqual(problems, [])
        self.assertEqual(set(cfg["basket"]), {"TKNA", "TKNB", "TKNC"})

    def test_bad_baskets_are_refused(self):
        bad = copy.deepcopy(SAMPLE_BASKET)
        cases = {
            "weights over 100": [dict(b, w=60) for b in bad],
            "empty": [],
            "9 tokens": [dict(bad[0], s="T%d" % i, w=1) for i in range(9)],
            "no chain": [{"s": "X", "w": 5}],
            "chain as text": [dict(bad[0], c="8453")],
            "duplicate symbol": [bad[0], dict(bad[1], s="TKNA")],
            "fractional weight": [dict(bad[0], w=10.5)],
        }
        for name, basket in cases.items():
            _, problems = self.check(BASKET=basket)
            self.assertTrue(problems, name)

    def test_a_basket_is_symbol_chain_weight_and_native_coins_are_tickers(self):
        basket = [{"s": "ETH", "c": 8453, "w": 30}, {"s": "SOL", "c": 1151111081099710, "w": 20}, {"s": "$bnb", "c": 56, "w": 10}]
        cfg, problems = self.check(BASKET=basket)
        self.assertEqual(problems, [])
        self.assertEqual(cfg["basket"]["ETH"], {"chain": 8453, "w": 30})
        self.assertEqual(set(cfg["basket"]), {"ETH", "SOL", "BNB"})

    def test_any_number_of_accounts_validates_and_duplicates_collapse(self):
        cfg, problems = self.check(HANDLES=["h%d" % i for i in range(12)] + ["@H3"])
        self.assertEqual(problems, [])
        self.assertEqual(len(cfg["handles"]), 12)

    def test_other_settings(self):
        for change in ({"HANDLES": []}, {"CAPITAL_USD": 50},
                       {"REBALANCE_HOURS": 500}, {"MODE": "yolo"}):
            self.assertTrue(self.check(**change)[1], change)

    def test_a_missing_mode_trades_nothing(self):
        params = {k: v for k, v in SAMPLE_PARAMS.items() if k != "MODE"}
        duty, _ = install(params)
        self.assertEqual(duty.settings()[0]["mode"], "watch")

    def test_params_fit_the_filing_limit(self):
        handles = ["alice_x%d" % i for i in range(8)]
        basket = [{"s": "TKN%d" % i, "c": 8453, "w": 12} for i in range(8)]
        basket[-1] = {"s": "SOLX", "c": 1151111081099710, "w": 4}
        params = {"HANDLES": handles, "CAPITAL_USD": 10000, "BASKET": basket, "REBALANCE_HOURS": 24, "MODE": "run"}
        size = len(json.dumps(params, separators=(",", ":")))
        self.assertLessEqual(size, 1024, size)


class TargetsTest(unittest.TestCase):
    def setUp(self):
        self.duty, self.fake = install()
        self.cfg = cfg_of(self.duty)
        self.core = new_core(self.duty, self.cfg)

    def test_sane_targets_keeps_basket_symbols_and_bounds(self):
        raw = [{"sym": "TKNA", "pct": 70}, {"sym": "TKNB", "pct": -5}, {"sym": "NOPE", "pct": 50},
               {"sym": "TKNC", "pct": 80}]
        out = self.duty.sane_targets(raw, self.cfg, {})
        self.assertEqual(set(out), {"TKNA", "TKNB", "TKNC"})
        self.assertTrue(all(isinstance(v, int) and v >= 0 for v in out.values()))
        self.assertLessEqual(sum(out.values()), 100)

    def test_sane_targets_falls_back_on_garbage(self):
        out = self.duty.sane_targets("nonsense", self.cfg, {"TKNA": 25})
        self.assertEqual(out, {"TKNA": 25, "TKNB": 0, "TKNC": 0})

    def review(self, tw, ref="1", hours=0):
        t = self.duty.now() + timedelta(hours=hours)
        wv = {"tw": tw, "tok": {"TKNA": {"for": [{"p": ref, "at": self.duty.iso(t - timedelta(minutes=1))}], "against": []}}}
        self.duty.register_review(self.core, self.cfg, wv, {}, t)

    def test_one_review_never_changes_the_targets_in_force(self):
        self.core["deployed"] = True
        self.review({"TKNA": 10, "TKNB": 30, "TKNC": 10})
        self.assertEqual(self.duty.effective_targets(self.core, self.cfg), {"TKNA": 40, "TKNB": 30, "TKNC": 10})

    def test_the_same_change_with_a_new_post_later_persists(self):
        self.review({"TKNA": 10}, ref="1")
        self.review({"TKNA": 10}, ref="2", hours=13)
        self.assertEqual(self.duty.effective_targets(self.core, self.cfg)["TKNA"], 10)

    def test_the_same_post_or_a_too_soon_review_does_not_confirm(self):
        self.review({"TKNA": 10}, ref="1")
        self.review({"TKNA": 10}, ref="1", hours=13)
        self.review({"TKNA": 10}, ref="2", hours=1)
        self.assertEqual(self.duty.effective_targets(self.core, self.cfg)["TKNA"], 40)
        self.assertEqual(self.core["cand"]["TKNA"]["n"], 1)

    def test_a_market_driven_change_persists_over_two_reviews_without_a_new_post(self):
        def view(tm):
            return {"tw": {"TKNA": 10}, "tm": {"TKNA": tm}, "tok": {"TKNA": {"for": [{"p": "1", "at": "2020-01-01T00:00:00Z"}], "against": []}}}
        moved, flat = {"TKNA": {"p": 2.0, "chg": -8.0}}, {"TKNA": {"p": 2.0, "chg": 1.0}}
        t0 = self.duty.now()
        self.duty.register_review(self.core, self.cfg, view(["change_24h_pct"]), moved, t0)
        self.duty.register_review(self.core, self.cfg, view([]), moved, t0 + timedelta(hours=13))  # no data cited, same post
        self.assertEqual(self.core["cand"]["TKNA"]["n"], 1)
        self.duty.register_review(self.core, self.cfg, view(["price"]), moved, t0 + timedelta(hours=1))  # too soon
        self.assertEqual(self.core["cand"]["TKNA"]["n"], 1)
        self.duty.register_review(self.core, self.cfg, view(["price"]), flat, t0 + timedelta(hours=13))  # the market did not move
        self.assertEqual(self.core["cand"]["TKNA"]["n"], 1)
        self.duty.register_review(self.core, self.cfg, view(["price"]), moved, t0 + timedelta(hours=13))
        self.assertEqual(self.duty.effective_targets(self.core, self.cfg)["TKNA"], 10)

    def test_a_change_first_proposed_without_market_data_is_not_confirmed_by_data_alone(self):
        def view(tm):
            return {"tw": {"TKNA": 10}, "tm": {"TKNA": tm}, "tok": {"TKNA": {"for": [{"p": "1", "at": "2020-01-01T00:00:00Z"}], "against": []}}}
        moved, t0 = {"TKNA": {"p": 2.0, "chg": -8.0}}, self.duty.now()
        self.duty.register_review(self.core, self.cfg, view([]), moved, t0)
        self.duty.register_review(self.core, self.cfg, view(["price"]), moved, t0 + timedelta(hours=13))
        self.assertEqual(self.core["cand"]["TKNA"]["n"], 1)

    def test_indirect_evidence_with_a_cited_post_counts_as_new_evidence_and_nothing_else_does(self):
        def view(ev):
            return {"tw": {"TKNA": 70}, "tok": {"TKNA": {"for": [{"p": "1", "at": "2020-01-01T00:00:00Z"}], "against": [], "ev": ev}}}
        t0 = self.duty.now()
        self.duty.register_review(self.core, self.cfg, view([]), {}, t0)
        self.duty.register_review(self.core, self.cfg, view([]), {}, t0 + timedelta(hours=13))
        self.assertEqual(self.core["cand"]["TKNA"]["n"], 1)
        old = {"via": "category", "p": "55", "at": "2020-01-02T00:00:00Z"}  # a post from before the first review
        self.duty.register_review(self.core, self.cfg, view([old]), {}, t0 + timedelta(hours=13))
        self.assertEqual(self.core["cand"]["TKNA"]["n"], 1)
        new = {"via": "category", "p": "9", "at": self.duty.iso(t0 + timedelta(hours=12))}
        self.duty.register_review(self.core, self.cfg, view([new]), {}, t0 + timedelta(hours=13))
        self.assertEqual(self.core["cand"]["TKNA"]["n"], 2)

    def test_a_review_that_says_no_rebalance_is_justified_clears_the_proposal(self):
        self.review({"TKNA": 10})
        wv = {"tw": {"TKNA": 10}, "rb": {"ok": False, "why": "noise"}, "tok": {"TKNA": {"for": [{"p": "2"}], "against": []}}}
        self.duty.register_review(self.core, self.cfg, wv, {}, self.duty.now() + timedelta(hours=13))
        self.assertNotIn("TKNA", self.core["cand"])

    def test_a_reversal_resets_the_count(self):
        self.review({"TKNA": 10})
        self.review({"TKNA": 70}, ref="2", hours=13)
        self.assertEqual(self.duty.effective_targets(self.core, self.cfg)["TKNA"], 40)

    def test_a_proposal_that_goes_away_resets(self):
        self.review({"TKNA": 10})
        self.review({"TKNA": 40}, ref="2", hours=13)
        self.review({"TKNA": 10}, ref="3", hours=26)
        self.assertEqual(self.duty.effective_targets(self.core, self.cfg)["TKNA"], 40)

    def test_targets_in_force_never_sum_past_100(self):
        self.core["cand"] = {"TKNA": {"pct": 90, "n": 2}, "TKNB": {"pct": 90, "n": 2}}
        self.assertLessEqual(sum(self.duty.effective_targets(self.core, self.cfg).values()), 100)


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.duty, self.fake = install()
        self.cfg = cfg_of(self.duty)
        self.core = new_core(self.duty, self.cfg)
        self.market = {s: {"p": 1.0} for s in self.cfg["basket"]}
        self.core["cash"] = 2000.0
        self.core["pos"] = {"TKNA": {"qty": 2000.0, "cost": 2000.0, "px": 1.0, "addr": ADDR["TKNA"], "chain": 8453},
                            "TKNB": {"qty": 1000.0, "cost": 1000.0, "px": 1.0, "addr": ADDR["TKNB"], "chain": 8453}}

    def plan(self, eff, **kw):
        nav, vals = self.duty.valuation(self.core, self.cfg, self.market)
        return self.duty.plan_legs(self.core, self.cfg, eff, nav, vals, self.market, kw.get("halted", False))

    def test_inside_the_band_nothing_trades(self):
        legs, why = self.plan({"TKNA": 40, "TKNB": 20, "TKNC": 0})
        self.assertEqual((legs, why), ([], "band"))

    def test_sells_come_first_and_never_exceed_the_book(self):
        legs, _ = self.plan({"TKNA": 10, "TKNB": 20, "TKNC": 30})
        self.assertEqual([l["side"] for l in legs][0], "sell")
        for leg in legs:
            if leg["side"] == "sell":
                self.assertLessEqual(leg["qty_plan"], self.core["pos"][leg["sym"]]["qty"])

    def test_a_zero_target_exits_the_whole_position(self):
        legs, _ = self.plan({"TKNA": 0, "TKNB": 20, "TKNC": 0})
        sale = [l for l in legs if l["sym"] == "TKNA"][0]
        self.assertEqual(sale["qty_plan"], 2000.0)

    def test_turnover_is_capped(self):
        self.core["pos"]["TKNA"]["qty"] = 4000.0
        self.core["cash"] = 0.0
        legs, _ = self.plan({"TKNA": 0.0, "TKNB": 0, "TKNC": 0}, halted=False)
        nav = 5000.0
        soft = [l for l in legs if l["side"] == "buy"]
        self.assertLessEqual(sum(l["usd_plan"] for l in soft), 0.5 * nav + 1)

    def test_a_halt_places_no_buy(self):
        legs, _ = self.plan({"TKNA": 10, "TKNB": 20, "TKNC": 40}, halted=True)
        self.assertTrue(legs)
        self.assertFalse([l for l in legs if l["side"] == "buy"])

    def test_the_ethereum_minimum_is_25_dollars(self):
        self.assertEqual(self.duty.min_leg(1), 25.0)
        self.assertEqual(self.duty.min_leg(8453), 2.0)
        self.core["cash"], self.core["pos"] = 100.0, {}
        legs, _ = self.plan({"TKNA": 0, "TKNB": 0, "TKNC": 10})
        self.assertEqual(legs, [])  # a $10 Ethereum leg is below the floor

    def test_every_leg_names_a_basket_symbol_and_chain_and_sells_the_learned_address(self):
        legs, _ = self.plan({"TKNA": 10, "TKNB": 20, "TKNC": 30})
        spec = self.cfg["basket"]
        for leg in legs:
            self.assertEqual(leg["chain"], spec[leg["sym"]]["chain"])
            self.assertEqual(leg["ref"], self.core["pos"][leg["sym"]]["addr"] if leg["side"] == "sell" else None)


class SizingTest(unittest.TestCase):
    def setUp(self):
        self.duty, self.fake = install()
        self.cfg = cfg_of(self.duty)
        self.core = new_core(self.duty, self.cfg)
        self.sent = []
        self.duty.send = lambda core, leg, t: (self.sent.append(leg), leg.update(st="filed"))

    def epoch(self, usd_plans):
        legs = [self.duty.make_leg(s, self.cfg["basket"][s], "buy", u, None, None) for s, u in usd_plans.items()]
        for leg in legs:
            leg["key"] = "k-" + leg["sym"]
        self.core["pending"] = {"epoch": 1, "at": self.duty.iso(self.duty.now()), "why": "t", "legs": legs,
                                "targets": {}, "first": False, "stop_buys": False}

    def test_buys_scale_down_to_the_pockets_cash_and_never_up(self):
        self.epoch({"TKNA": 2000.0, "TKNB": 3000.0})
        self.duty.send_buys(self.core, {"cash": 1000.0, "funded": True}, {"usdc": 5000.0, "qty": {}}, self.duty.now())
        self.assertLessEqual(sum(float(l["amt"]) for l in self.sent), 1000.0 - 2.0)

    def test_buys_are_bounded_by_wallet_usdc(self):
        self.epoch({"TKNA": 400.0})
        self.duty.send_buys(self.core, {"cash": 5000.0, "funded": True}, {"usdc": 100.0, "qty": {}}, self.duty.now())
        self.assertLessEqual(float(self.sent[0]["amt"]), 99.0)

    def test_an_unfunded_pocket_cancels_every_buy(self):
        self.epoch({"TKNA": 400.0})
        self.duty.send_buys(self.core, {"cash": 5000.0, "funded": False}, {"usdc": 9999.0, "qty": {}}, self.duty.now())
        self.assertEqual(self.sent, [])
        self.assertEqual(self.core["pending"]["legs"][0]["st"], "cancelled")

    def test_a_halted_portfolio_buys_nothing(self):
        self.epoch({"TKNA": 400.0})
        self.core["halted"] = "now"
        self.duty.send_buys(self.core, {"cash": 5000.0, "funded": True}, {"usdc": 9999.0, "qty": {}}, self.duty.now())
        self.assertEqual(self.sent, [])

    def test_a_leg_under_the_minimum_is_dropped(self):
        self.epoch({"TKNC": 20.0})  # chain 1 needs $25
        self.duty.send_buys(self.core, {"cash": 5000.0, "funded": True}, {"usdc": 9999.0, "qty": {}}, self.duty.now())
        self.assertEqual(self.sent, [])

    def test_sell_quantity_is_floored_and_capped(self):
        self.core["pos"] = {"TKNA": {"qty": 10.123456789, "cost": 1.0, "px": 1.0, "addr": ADDR["TKNA"], "chain": 8453}}
        leg = self.duty.make_leg("TKNA", self.cfg["basket"]["TKNA"], "sell", 5, 99.0, 1.0, self.core["pos"]["TKNA"])
        wallet = {"usdc": 1.0, "qty": {(ADDR["TKNA"], 8453): {"q": 3.987654321}}}
        self.assertEqual(self.duty.sell_qty(self.core, leg, wallet, None), 3.98765432)
        self.assertEqual(self.duty.sell_qty(self.core, leg, {"usdc": 1.0, "qty": {}}, None), 10.12345678)

    def test_apply_fill_tracks_cost_basis_and_realized_pnl(self):
        buy = self.duty.make_leg("TKNA", self.cfg["basket"]["TKNA"], "buy", 100, None, 1.0)
        self.core["cash"] = 1000.0
        self.duty.apply_fill(self.core, buy, {"qty": 100.0, "usd": 100.0, "px": 1.0, "addr": ADDR["TKNA"]})
        sell = self.duty.make_leg("TKNA", self.cfg["basket"]["TKNA"], "sell", 60, 40.0, 1.5, self.core["pos"]["TKNA"])
        self.duty.apply_fill(self.core, sell, {"qty": 40.0, "usd": 60.0, "px": 1.5})
        pos = self.core["pos"]["TKNA"]
        self.assertAlmostEqual(pos["qty"], 60.0)
        self.assertAlmostEqual(pos["cost"], 60.0)
        self.assertAlmostEqual(self.core["realized"], 20.0)
        self.assertAlmostEqual(self.core["cash"], 960.0)


if __name__ == "__main__":
    unittest.main()
