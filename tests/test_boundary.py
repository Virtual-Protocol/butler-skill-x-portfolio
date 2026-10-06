"""The structure the container's lint and the template standard rely on, checked on the source."""

import ast
import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = (ROOT / "duty.py").read_text()
TREE = ast.parse(SOURCE)
ASKS = re.compile(r"\breply\b|\bconfirm\b|let me know|\bshould i\b|do you want|\bshall i\b|yes\s*/\s*no", re.I)


def function(name):
    return next(n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name == name)


def assigned_names(node):
    names = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
            names.add(sub.id)
        elif isinstance(sub, ast.ExceptHandler) and sub.name:
            names.add(sub.name)
    return names


class BoundaryTest(unittest.TestCase):
    def test_exactly_one_model_call_inside_a_guarded_function(self):
        calls = [n for n in ast.walk(TREE) if isinstance(n, ast.Call) and ast.unparse(n.func) == "bevo.prompt"]
        self.assertEqual(len(calls), 1)
        inside = [n for n in ast.walk(function("read_posts_with_model")) if n is calls[0]]
        self.assertEqual(len(inside), 1)
        tries = [n for n in ast.walk(function("read_posts_with_model")) if isinstance(n, ast.Try)]
        self.assertTrue(any(ast.unparse(h.type) == "bevo.BevoError" for t in tries for h in t.handlers))

    def test_model_names_are_confined_to_that_function(self):
        inner = assigned_names(function("read_posts_with_model"))
        self.assertTrue(inner and all(n.startswith("mo_") for n in inner), inner)
        outside = [n.id for top in TREE.body if not (isinstance(top, ast.FunctionDef) and top.name == "read_posts_with_model")
                   for n in ast.walk(top) if isinstance(n, ast.Name) and n.id.startswith("mo_")]
        self.assertEqual(outside, [])

    def test_the_model_function_returns_only_numbers(self):
        for node in ast.walk(function("read_posts_with_model")):
            if isinstance(node, ast.Return):
                self.assertTrue(isinstance(node.value, (ast.Constant, ast.UnaryOp, ast.Call)), ast.unparse(node))
                if isinstance(node.value, ast.Call):
                    self.assertEqual(ast.unparse(node.value.func), "len")

    def test_three_shell_calls_with_the_key_last(self):
        runs = [n for n in ast.walk(TREE) if isinstance(n, ast.Call) and ast.unparse(n.func) == "subprocess.run"]
        heads = sorted(ast.unparse(n.args[0].elts[0]) + " " + ast.unparse(n.args[0].elts[1]) for n in runs)
        self.assertEqual(heads, ["'acp' 'trade'", "'acp' 'trade'", "'bevo-x' 'search'"])
        for call in runs:
            elts = call.args[0].elts
            if ast.unparse(elts[0]) == "'acp'":
                self.assertEqual(ast.unparse(elts[-2]), "'--idempotency-key'")
                self.assertEqual(ast.unparse(elts[-1]), "leg['key']")
                literal = [ast.unparse(e) for e in elts if isinstance(e, ast.Constant)]
                for banned in ("--accept-impact-bps", "--side", "--recipient", "--leverage"):
                    self.assertNotIn(repr(banned), literal)

    def test_no_money_verb_other_than_spot_trade(self):
        for banned in ("bevo-send", "send-transaction", "acp card", "perp", "--leverage", "bevo.buy", "bevo.trade"):
            self.assertNotIn(banned, SOURCE)

    def test_no_url_todo_or_baked_address(self):
        self.assertIsNone(re.search(r"https?://", SOURCE))
        self.assertIsNone(re.search(r"TODO|FIXME", SOURCE))
        self.assertIsNone(re.search(r"0x[0-9a-fA-F]{40}", SOURCE))
        self.assertIsNone(re.search(r"[1-9A-HJ-NP-Za-km-z]{32,44}", re.sub(r"\s", " ", " ".join(
            c.value for c in ast.walk(TREE) if isinstance(c, ast.Constant) and isinstance(c.value, str) and " " not in c.value))))

    def test_no_note_asks_a_question(self):
        speaking = {"note", "stamp", "bevo.notify", "bevo.done"}
        seen = 0
        for call in (n for n in ast.walk(TREE) if isinstance(n, ast.Call) and ast.unparse(n.func) in speaking):
            for node in ast.walk(call):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    seen += 1
                    self.assertIsNone(ASKS.search(node.value), node.value)
                    self.assertFalse(node.value.strip().endswith("?"), node.value)
        self.assertGreater(seen, 20)

    def test_size_budget(self):
        self.assertLessEqual(len(SOURCE), 65000)  # the server's cap is 65536; v2 (any handle count, no addresses) is ~65K; a fork has under 600 chars of room

    def test_the_recipe_has_no_default_token_chain_or_cap(self):
        recipe = json.loads((ROOT / "recipe.json").read_text())
        props = recipe["params"]["properties"]
        for name in ("HANDLES", "CAPITAL_USD", "BASKET"):
            self.assertIn(name, recipe["params"]["required"])
            self.assertNotIn("default", props[name])
        self.assertEqual(sorted(props), ["BASKET", "CAPITAL_USD", "HANDLES", "MODE", "REBALANCE_HOURS"])
        text = (ROOT / "recipe.json").read_text() + (ROOT / "README.md").read_text()
        self.assertIsNone(re.search(r"0x[0-9a-fA-F]{8,}", text))
        self.assertLessEqual(len((ROOT / "README.md").read_bytes()), 4096)


@unittest.skipUnless(os.environ.get("BEVO_AST"), "set BEVO_AST to the container's bevo_ast.py to check its facts")
class ContainerFactsTest(unittest.TestCase):
    def test_the_container_lint_facts_are_clean(self):
        done = subprocess.run([sys.executable, os.environ["BEVO_AST"]], input=SOURCE, capture_output=True, text=True)
        facts = json.loads(done.stdout)
        self.assertIsNone(facts["syntaxError"])
        for name in ("taintedMoney", "badKeys", "selfLoops", "retiredCalls", "badBevoImports", "literalMoneyAmounts"):
            self.assertEqual(facts[name], [], name)
        self.assertTrue(facts["shellMoneyCalls"] and all(c["keyed"] for c in facts["shellMoneyCalls"]))
        self.assertTrue({r["path"] for r in facts["readPaths"]} <= {"/duties", "/user-assets", "/token-stats", "/token-search", "/trade-executions"})


if __name__ == "__main__":
    unittest.main()
