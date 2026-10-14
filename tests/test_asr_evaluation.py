import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from evaluate_stt_candidates import summarize
from prepare_asr_evaluation import g711, select_rows


def test_selection_excludes_seen_sentences_and_keeps_literal_quotes(tmp_path):
    rows = [
        [str(i), f"{i}.wav", '"Текст" 123', "текст 123", "", "32000", gender]
        for i, gender in [(1, "MALE"), (2, "FEMALE"), (3, "MALE"), (4, "FEMALE")]
    ]
    with (tmp_path / "test.tsv").open("w") as out:
        out.write("\n".join("\t".join(row) for row in rows) + "\n")
    chosen = select_rows(tmp_path, "test", 2, {1})
    assert len(chosen) == 2
    assert len({r[0] for r in chosen}) == 2
    assert {r[6] for r in chosen} == {"MALE", "FEMALE"}
    assert all(r[0] != "1" for r in chosen)
    assert all(r[2] == '"Текст" 123' for r in chosen)


@pytest.mark.parametrize("codec", ["pcmu", "pcma"])
def test_exact_g711_path_preserves_audio_length_and_lowband_signal(codec):
    sample = (0.3 * np.sin(2 * np.pi * 440 * np.arange(8000) / 16000)).astype(np.float32)
    decoded = g711(sample, codec)
    assert len(decoded) == len(sample)
    assert np.isfinite(decoded).all()
    assert 0.18 < float(np.sqrt(np.mean(decoded * decoded))) < 0.24


def test_clean_rows_cannot_raise_complex_accuracy():
    from asr_metrics import alignment, literal_numbers

    rows = []
    for condition, hypothesis in [("clean", "раз два"), ("complex", "")]:
        rows.append(
            {
                "base_id": "same-human",
                "condition": condition,
                "channel": "browser16k",
                "noise": "stationary",
                "gender": "FEMALE",
                "has_literal_numbers": False,
                "duration_s": 1,
                "elapsed_s": 0.1,
                "error": "Timeout" if not hypothesis else None,
                "hypothesis": hypothesis,
                "normalized": alignment("раз два", hypothesis),
                "raw": alignment("раз два", hypothesis, normalized=False),
                "literal_numbers": literal_numbers("раз два", hypothesis),
            }
        )
    result = summarize(rows)
    assert result["browser16k/clean"]["normalized"]["wer"] == 0
    assert result["complex_all_channels"]["normalized"]["wer"] == 1
    assert result["complex_all_channels"]["errors"] == 1
    assert not result["complex_all_channels"]["normalized"]["meets_wer_6_percent"]
