import unittest

from backend.services.evaluation import evaluate_binary_classifier


class EvaluationTests(unittest.TestCase):
    def test_evaluate_binary_classifier_balanced_case(self) -> None:
        records = [
            {"ground_truth": 1, "score": 0.95},
            {"ground_truth": 1, "score": 0.70},
            {"ground_truth": 0, "score": 0.10},
            {"ground_truth": 0, "score": 0.80},
        ]

        result = evaluate_binary_classifier(records, threshold=0.65)

        self.assertEqual(result["confusion"], {"tp": 2, "fp": 1, "tn": 1, "fn": 0})
        self.assertEqual(result["metrics"]["precision"], 0.6667)
        self.assertEqual(result["metrics"]["recall"], 1.0)
        self.assertEqual(result["metrics"]["fpr"], 0.5)
        self.assertIsNotNone(result["auc"])
        self.assertGreaterEqual(len(result["roc_curve"]), 2)

    def test_evaluate_binary_classifier_handles_single_class(self) -> None:
        records = [
            {"ground_truth": 0, "score": 0.0},
            {"ground_truth": 0, "score": 0.2},
            {"ground_truth": 0, "score": 0.4},
        ]

        result = evaluate_binary_classifier(records, threshold=0.5)

        self.assertEqual(result["samples"], 3)
        self.assertEqual(result["positives"], 0)
        self.assertIsNone(result["auc"])
        self.assertTrue(result["warnings"])
