"""
Evaluate classifier accuracy against labeled training data.
"""

from training.training_data import TRAINING_DATA
from core.classifier import classify


def evaluate():
    correct = 0
    total = len(TRAINING_DATA)
    failures = []

    for item in TRAINING_DATA:
        result = classify(item["utterance"])
        expected = item["intent"]
        actual = result.intent.value

        if actual == expected:
            correct += 1
        else:
            failures.append({
                "utterance": item["utterance"],
                "expected": expected,
                "actual": actual,
                "confidence": result.confidence,
            })

    accuracy = (correct / total) * 100
    print(f"\n{'='*60}")
    print(f"  CLASSIFIER ACCURACY: {correct}/{total} = {accuracy:.1f}%")
    print(f"{'='*60}")

    if failures:
        print(f"\n❌ Failures ({len(failures)}):")
        for f in failures:
            print(f"   \"{f['utterance']}\"")
            print(f"      Expected: {f['expected']}")
            print(f"      Got:      {f['actual']} ({f['confidence']:.0%})")
    else:
        print("\n✅ All tests passed!")


if __name__ == "__main__":
    evaluate()