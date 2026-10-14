import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from asr_metrics import aggregate, alignment, literal_numbers, tokens


def test_basic_normalization_does_not_rewrite_numbers():
    assert tokens("Ёлка, 375-29-123! 10,5") == ["елка", "375-29-123", "10,5"]
    assert alignment("375 29", "375 28")["errors"] == 1
    assert alignment("-15", "15")["errors"] == 1
    assert tokens("+375 и -10,5") == ["+375", "и", "-10,5"]
    assert alignment("двадцать девять", "29")["errors"] == 2


def test_empty_failed_hypothesis_is_all_deletions():
    row = alignment("Имя Анна телефон 123", "")
    assert (row["errors"], row["deletion"], row["reference_words"]) == (4, 4, 4)


def test_aggregate_is_word_weighted_and_threshold_is_exact():
    rows = [{"normalized": alignment("один два три четыре", "один")}, {"normalized": alignment("два", "два")}]
    assert aggregate(rows)["wer"] == 3 / 5
    assert aggregate(
        [
            {
                "normalized": {
                    "reference_words": 100,
                    "errors": 6,
                    "substitution": 6,
                    "deletion": 0,
                    "insertion": 0,
                }
            }
        ]
    )["meets_wer_6_percent"]
    assert not aggregate(
        [
            {
                "normalized": {
                    "reference_words": 99,
                    "errors": 6,
                    "substitution": 6,
                    "deletion": 0,
                    "insertion": 0,
                }
            }
        ]
    )["meets_wer_6_percent"]


def test_raw_scoring_and_literal_number_errors_remain_visible():
    assert alignment("Анна, 123", "анна 124", normalized=False)["errors"] == 2
    assert literal_numbers("номер 123 и 29", "номер 123 и 28")["substitution"] == 1
    assert not literal_numbers("в 2026 году", "в две тысячи двадцать шестом году")["exact_sequence"]
