"""Fees, proposal rules, order book parsing, devig/matching, request signing,
and the import boundary around the order client."""

import ast
import base64
import pathlib
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from kalshi.fees import charged_fee, taker_fee_usd, worst_case_fee_per_contract_cents
from kalshi.http import KalshiSigner
from kalshi.odds_api import (american_to_prob, consensus, devig_multiplicative, match_market,
                             parse_odds_events, team_match_score)
from kalshi.orderbook import parse_orderbook
from kalshi.orders import to_v2_side_and_price
from kalshi.proposal import Agent, Decision, ProposalClosed, ScopeViolation, Status, TradeProposal

from tests import helpers as h

ROOT = pathlib.Path(__file__).resolve().parents[1]


class TestFees(unittest.TestCase):
    def test_classic_ceil_to_cent(self):
        s = h.settings()
        # 0.07 * 100 * 0.5 * 0.5 = 1.75 exactly
        self.assertAlmostEqual(taker_fee_usd(50, 100, s), 1.75)
        # 0.07 * 1 * 0.5 * 0.5 = 0.0175 -> $0.02
        self.assertAlmostEqual(taker_fee_usd(50, 1, s), 0.02)
        # 0.07 * 10 * 0.45 * 0.55 = 0.17325 -> $0.18
        self.assertAlmostEqual(taker_fee_usd(45, 10, s), 0.18)

    def test_kalshi_documented_rounding_example(self):
        # docs.kalshi.com/getting_started/fee_rounding: revenue -$0.055, model fee $0.00363825
        # trade fee $0.003639, aligned change -$0.06, so total charged $0.005 at cent precision.
        fee = charged_fee(Decimal("0.055"), 1, 0.07, 1.0, 0.01, buying=True)
        self.assertEqual(fee, Decimal("0.005000"))

    def test_direct_member_precision_is_cheaper(self):
        cent = charged_fee(Decimal("0.45"), 3, 0.07, 1.0, 0.01)
        centicent = charged_fee(Decimal("0.45"), 3, 0.07, 1.0, 0.0001)
        self.assertLess(centicent, cent)

    def test_worst_case_per_contract_bounds_bigger_fills(self):
        s = h.settings()
        for price in (5, 30, 50, 77, 95):
            per = worst_case_fee_per_contract_cents(price, s)
            for n in (2, 7, 50, 300):
                self.assertLessEqual(taker_fee_usd(price, n, s) * 100 / n, per + 1e-9)

    def test_zero_count(self):
        self.assertEqual(charged_fee(Decimal("0.5"), 0, 0.07), 0)


class TestProposal(unittest.TestCase):
    def test_reject_ends_proposal(self):
        p = h.proposal()
        p.add_verdict(Agent.SKEPTIC.value, Decision.REJECT, "stale")
        self.assertEqual(p.status, Status.REJECTED)
        with self.assertRaises(ProposalClosed):
            p.add_verdict(Agent.RISK.value, Decision.APPROVE, "late")

    def test_skeptic_cannot_modify(self):
        p = h.proposal()
        with self.assertRaises(ScopeViolation):
            p.add_verdict(Agent.SKEPTIC.value, Decision.MODIFY, "x", changes={"contract_count": 1})

    def test_unknown_agent_cannot_modify(self):
        p = h.proposal()
        with self.assertRaises(ScopeViolation):
            p.add_verdict("someone", Decision.MODIFY, "x", changes={"contract_count": 1})

    def test_roundtrip(self):
        p = h.fully_approved()
        q = TradeProposal.from_dict(p.to_dict())
        self.assertEqual(q.to_dict(), p.to_dict())

    def test_validation(self):
        with self.assertRaises(ValueError):
            h.proposal(side="maybe")
        with self.assertRaises(ValueError):
            h.proposal(price=100)


class TestOrderbook(unittest.TestCase):
    def test_fixed_point_format(self):
        b = parse_orderbook("T", {"orderbook_fp": {
            "yes_dollars": [["0.4400", "120.00"], ["0.4300", "50.00"]],
            "no_dollars": [["0.5300", "80.00"], ["0.5000", "10.00"]]}})
        self.assertEqual(b.best_bid_cents("yes"), 44)
        self.assertEqual(b.best_ask_cents("yes"), 47)          # 1 - 0.53
        self.assertEqual(b.best_ask_cents("no"), 56)           # 1 - 0.44
        self.assertEqual(b.ask_depth("yes"), 80.0)
        self.assertEqual(b.spread_cents(), 3)

    def test_legacy_cents_format(self):
        b = parse_orderbook("T", {"orderbook": {"yes": [[44, 120]], "no": [[53, 80]]}})
        self.assertEqual(b.best_ask_cents("yes"), 47)

    def test_subpenny_rounds_against_us(self):
        b = parse_orderbook("T", {"orderbook_fp": {"yes_dollars": [["0.4450", "5.00"]],
                                                   "no_dollars": [["0.5350", "5.00"]]}})
        self.assertEqual(b.best_ask_cents("yes"), 47)          # 46.5c ask -> 47
        self.assertEqual(b.best_bid_cents("yes"), 44)          # 44.5c bid -> 44

    def test_empty_side(self):
        b = parse_orderbook("T", {"orderbook_fp": {"yes_dollars": [], "no_dollars": []}})
        self.assertIsNone(b.best_ask_cents("yes"))
        self.assertIsNone(b.spread_cents())

    def test_v2_side_mapping(self):
        self.assertEqual(to_v2_side_and_price("yes", 45), ("bid", "0.4500"))
        self.assertEqual(to_v2_side_and_price("no", 45), ("ask", "0.5500"))


class TestOdds(unittest.TestCase):
    def test_american(self):
        self.assertAlmostEqual(american_to_prob(-110), 110 / 210)
        self.assertAlmostEqual(american_to_prob(150), 0.4)

    def test_devig_sums_to_one(self):
        fair = devig_multiplicative([american_to_prob(-110), american_to_prob(-110)])
        self.assertAlmostEqual(sum(fair), 1.0)
        self.assertAlmostEqual(fair[0], 0.5)

    def test_parse_and_match(self):
        payload = [{
            "id": "e1", "commence_time": "2026-10-11T17:00:00Z",
            "home_team": "New York Giants", "away_team": "Dallas Cowboys",
            "bookmakers": [
                {"key": "pinnacle", "last_update": "2026-10-10T17:59:00Z", "markets": [
                    {"key": "h2h", "outcomes": [{"name": "New York Giants", "price": 150},
                                                {"name": "Dallas Cowboys", "price": -170}]}]},
                {"key": "fanduel", "last_update": "2026-10-10T17:58:00Z", "markets": [
                    {"key": "h2h", "outcomes": [{"name": "New York Giants", "price": 145},
                                                {"name": "Dallas Cowboys", "price": -175}]}]},
            ]}]
        events = parse_odds_events(payload, "americanfootball_nfl", fetched_at=h.NOW)
        self.assertEqual(len(events[0].lines), 2)
        start = datetime(2026, 10, 11, 17, 0, tzinfo=timezone.utc)
        hit = match_market("Dallas", "Dallas at New York G Winner?", start, events, 36)
        self.assertIsNotNone(hit)
        ev, team = hit
        self.assertEqual(team, "Dallas Cowboys")
        mean, sd = consensus(ev.sources_for(team))
        self.assertTrue(0.6 < mean < 0.65)
        self.assertGreater(sd, 0)

    def test_team_name_variants(self):
        self.assertEqual(team_match_score("New York G", "New York Giants"), 1.0)
        self.assertEqual(team_match_score("New York G", "New York Jets"), 0.0)
        self.assertEqual(team_match_score("Cowboys", "Dallas Cowboys"), 1.0)
        self.assertEqual(team_match_score("Dallas", "Houston Texans"), 0.0)

    def test_three_way_market_skipped(self):
        payload = [{"id": "s", "home_team": "A", "away_team": "B", "commence_time": "2026-10-11T17:00:00Z",
                    "bookmakers": [{"key": "x", "markets": [{"key": "h2h", "outcomes": [
                        {"name": "A", "price": 200}, {"name": "B", "price": 150}, {"name": "Draw", "price": 220}]}]}]}]
        self.assertEqual(parse_odds_events(payload, "soccer"), [])


class TestSigning(unittest.TestCase):
    def test_message_strips_query(self):
        self.assertEqual(KalshiSigner.message("1700000000000", "get", "/trade-api/v2/portfolio/orders?limit=5"),
                         b"1700000000000GET/trade-api/v2/portfolio/orders")

    def test_rsa_pss_verifies_with_digest_length_salt(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        signer = KalshiSigner("kid", key)
        sig = base64.b64decode(signer.sign("1700000000000", "POST", "/trade-api/v2/portfolio/events/orders"))
        key.public_key().verify(
            sig, b"1700000000000POST/trade-api/v2/portfolio/events/orders",
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256())

    def test_ed25519_rfc8032_vector(self):
        # RFC 8032 section 7.1, TEST 1 (empty message).
        sk = ed25519.Ed25519PrivateKey.from_private_bytes(bytes.fromhex(
            "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"))
        expected = ("e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46b"
                    "d25bf5f0595bbe24655141438e7a100b")
        self.assertEqual(sk.sign(b"").hex(), expected)
        signer = KalshiSigner("kid", sk)
        sig = base64.b64decode(signer.sign("1", "GET", "/x"))
        sk.public_key().verify(sig, b"1GET/x")

    def test_loads_pem_with_escaped_newlines(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
        s = h.settings(kalshi_api_key_id="kid", kalshi_private_key_pem=pem)
        self.assertIsNotNone(KalshiSigner.from_settings(s))


class TestImportBoundary(unittest.TestCase):
    def test_only_execution_imports_order_client(self):
        offenders = []
        for path in ROOT.rglob("*.py"):
            rel = path.relative_to(ROOT).as_posix()
            if rel.startswith(("tests/", ".")) or rel == "kalshi/orders.py":
                continue
            tree = ast.parse(path.read_text(), filename=rel)
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
                if any(n == "kalshi.orders" or n.startswith("kalshi.orders.") for n in names):
                    offenders.append(rel)
                if isinstance(node, ast.ImportFrom) and node.module == "kalshi" and \
                        any(a.name == "orders" for a in node.names):
                    offenders.append(rel)
        self.assertEqual(sorted(set(offenders)), ["kalshi/agents/execution.py"])

    def test_risk_manager_has_no_llm_or_model_imports(self):
        src = (ROOT / "kalshi/agents/risk_manager.py").read_text()
        for banned in ("anthropic", "openai", "edge_finder", "odds_api", "confidence", "requests"):
            self.assertNotIn(f"import {banned}", src)
            self.assertNotIn(f".{banned} import", src)
        self.assertNotIn("fair_probability", src.split('"""', 2)[2])


if __name__ == "__main__":
    unittest.main()
