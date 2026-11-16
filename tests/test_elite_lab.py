from fractions import Fraction
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from commands import elite_lab as lab


class LabArithmeticTests(unittest.TestCase):
    def test_matrix_uses_exact_weighted_scores(self):
        table = lab._table("А:8,6; Б:7,8", 2, low=0, high=10)
        self.assertEqual(lab.weighted_scores([2, 3], table), {"А": Fraction(34, 5), "Б": Fraction(38, 5)})
        table = lab._table("А:8,1; Б:2,9", 2, low=0, high=10)
        self.assertEqual(lab._extreme(lab.weighted_scores([4, 3], table)), ("А", "Б"))

    def test_pairwise_count_and_actual_cycle(self):
        result = lab.pairwise("4:А>Б>В; 3:Б>В>А; 2:В>А>Б")
        self.assertEqual(result.votes, 9)
        self.assertEqual(result.winners, ())
        self.assertEqual(result.cycle, ("А", "Б", "В", "А"))
        self.assertEqual(result.totals, {"А": 0, "Б": 0, "В": 0})
        self.assertIn(("А", "В", 4, 5), result.meetings)

    def test_pairwise_unanimity_ties_and_bad_ballots(self):
        unanimous = lab.pairwise("100000:А>Б>В")
        self.assertEqual(unanimous.winners, ("А",))
        tied = lab.pairwise("5:А>Б; 5:б>а")
        self.assertEqual(tied.winners, ())
        self.assertEqual(tied.cycle, ())
        self.assertEqual(tied.totals, {"А": 0, "Б": 0})
        for raw in ("5:A>B; 1:A>C", "5:A>A", "0:A>B", "1.4:A>B"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                lab.pairwise(raw)

    def test_pert_order_expectation_and_constant_duration(self):
        self.assertEqual(lab.pert(2, 4, 12), (Fraction(5), Fraction(5, 3)))
        self.assertEqual(lab.pert(10, 10, 10), (Fraction(10), Fraction()))
        for values in ((3, 2, 5), (-1, 0, 1), (5, 5, 1001)):
            with self.subTest(values=values), self.assertRaises(ValueError):
                lab.pert(*values)

    def test_allocate_keeps_exact_sum_with_stable_ties_and_zero_weight(self):
        weights = lab._weights("А:1; Б:1; В:1; Г:0")
        result, quotas = lab.allocate(2, weights)
        self.assertEqual(result, {"А": 1, "Б": 1, "В": 0, "Г": 0})
        self.assertEqual(quotas["А"], Fraction(2, 3))
        for total in (0, 10, 1_000_000_000):
            result, _ = lab.allocate(total, lab._weights("А:0.1; Б:0.3; В:0.6"))
            self.assertEqual(sum(result.values()), total)
        with self.assertRaises(ValueError):
            lab._weights("А:0; Б:0")

    def test_regret_handles_costs_negative_values_and_ties(self):
        table = lab._table("А:-40,90; Б:-55,70", 2, low=-100, high=100)
        self.assertEqual(lab.scenario_regrets(table, lower_is_better=False), {"А": (0, 0), "Б": (15, 20)})
        self.assertEqual(lab.scenario_regrets(table, lower_is_better=True), {"А": (15, 20), "Б": (0, 0)})
        self.assertEqual(lab._extreme({"А": 5, "Б": 5}, smallest=True), ("А", "Б"))


class LabInteractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_has_five_commands_and_valid_result_is_private_v2(self):
        self.assertEqual({command.name for command in lab.lab.commands}, {"matrix", "pairs", "estimate", "split", "regret"})
        with patch.object(lab.v2, "send_response", AsyncMock()) as reply:
            await lab.matrix.callback(
                SimpleNamespace(), "Цена:1; Стиль:1", "А:10,3.5; Б:2.5,10",
            )
        message = reply.await_args.kwargs
        self.assertTrue(message["ephemeral"])
        self.assertIn("Цена −20% → Б", message["embed"].description)

    async def test_bad_input_reaches_private_response_with_actual_syntax(self):
        with patch.object(lab.v2, "send_response", AsyncMock()) as reply:
            await lab.regret.callback(SimpleNamespace(), "А:3,4; Б:6")
        self.assertTrue(reply.await_args.kwargs["ephemeral"])
        self.assertIn("/lab regret outcomes:", reply.await_args.kwargs["embed"].description)
