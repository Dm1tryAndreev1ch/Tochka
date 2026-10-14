"""Reference-independent word scoring; numeric values are never rewritten."""

import re
import unicodedata
from collections import Counter


def tokens(text, *, normalized=True):
    if not normalized:
        return text.split()
    text = unicodedata.normalize("NFC", text).casefold().replace("ё", "е")
    # Preserve decimal/grouped number tokens; do not expand numerals or word numbers.
    return re.findall(r"[+-]?\d+(?:[.,:/-]\d+)*|[^\W\d_]+", text, flags=re.UNICODE)


def alignment(reference, hypothesis, *, normalized=True):
    ref, hyp = tokens(reference, normalized=normalized), tokens(hypothesis, normalized=normalized)
    costs = [[0] * (len(hyp) + 1) for _ in range(len(ref) + 1)]
    for i in range(len(ref) + 1):
        costs[i][0] = i
    for j in range(len(hyp) + 1):
        costs[0][j] = j
    for i, word in enumerate(ref, 1):
        for j, other in enumerate(hyp, 1):
            costs[i][j] = min(costs[i - 1][j] + 1, costs[i][j - 1] + 1, costs[i - 1][j - 1] + (word != other))
    i, j, edits = len(ref), len(hyp), []
    while i or j:
        if i and j and ref[i - 1] == hyp[j - 1] and costs[i][j] == costs[i - 1][j - 1]:
            edits.append(("correct", ref[i - 1], hyp[j - 1]))
            i -= 1
            j -= 1
        elif i and j and costs[i][j] == costs[i - 1][j - 1] + 1:
            edits.append(("substitution", ref[i - 1], hyp[j - 1]))
            i -= 1
            j -= 1
        elif i and costs[i][j] == costs[i - 1][j] + 1:
            edits.append(("deletion", ref[i - 1], None))
            i -= 1
        else:
            edits.append(("insertion", None, hyp[j - 1]))
            j -= 1
    edits.reverse()
    counts = Counter(edit[0] for edit in edits)
    result = {name: counts[name] for name in ("correct", "substitution", "deletion", "insertion")}
    result.update(reference_words=len(ref), errors=costs[-1][-1], edits=edits)
    return result


def literal_numbers(reference, hypothesis):
    """Strict supplemental metric, deliberately does not hide word/digit formatting."""
    ref = [word for word in tokens(reference) if any(c.isdecimal() for c in word)]
    hyp = [word for word in tokens(hypothesis) if any(c.isdecimal() for c in word)]
    result = alignment(" ".join(ref), " ".join(hyp), normalized=False)
    result["exact_sequence"] = ref == hyp
    return result


def aggregate(rows, key="normalized"):
    total = {
        name: sum(row[key][name] for row in rows)
        for name in ("reference_words", "errors", "substitution", "deletion", "insertion")
    }
    total["utterances"] = len(rows)
    total["wer"] = total["errors"] / total["reference_words"] if total["reference_words"] else None
    # Compare integer counts directly, with no rounded percentage deciding acceptance.
    total["meets_wer_6_percent"] = bool(
        total["reference_words"] and 100 * total["errors"] <= 6 * total["reference_words"]
    )
    return total
