"""Deterministic text and code comparison used by peer matching, novelty, variation and similarity."""
from __future__ import annotations

import re

STOP = {
    "the", "a", "an", "to", "of", "and", "or", "for", "in", "on", "with", "add", "adds", "added", "new",
    "support", "supports", "allow", "allows", "when", "is", "are", "be", "by", "that", "it", "its", "as",
    "from", "can", "this", "into", "via", "using", "use", "uses", "user", "users", "option", "options",
    "feature", "features", "value", "values", "will", "should", "not", "no", "any", "all", "each", "has",
    "have", "if", "else", "than", "then", "also", "more", "less", "make", "makes", "way", "ways", "them",
    "their", "which", "who", "what", "where", "how", "you", "your", "one", "two", "able", "given", "per",
}

_WORD = re.compile(r"[a-z][a-z0-9]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def words(text: str) -> set[str]:
    """Content words, with camelCase and snake_case split and simple plurals folded."""
    text = _CAMEL.sub(" ", text or "").replace("_", " ").replace("-", " ").lower()
    out = set()
    for w in _WORD.findall(text):
        if w in STOP or len(w) < 3:
            continue
        if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.add(w)
    return out


def jaccard(a: str | set, b: str | set) -> float:
    wa = a if isinstance(a, set) else words(a)
    wb = b if isinstance(b, set) else words(b)
    return len(wa & wb) / len(wa | wb) if wa and wb else 0.0


def containment(small: str | set, big: str | set) -> float:
    """Share of ``small``'s words that ``big`` also has (asymmetric)."""
    ws = small if isinstance(small, set) else words(small)
    wb = big if isinstance(big, set) else words(big)
    return len(ws & wb) / len(ws) if ws else 0.0


def match_score(a: str, b: str) -> float:
    """How well two behaviour descriptions match: the larger of Jaccard and a damped containment."""
    wa, wb = words(a), words(b)
    if not wa or not wb:
        return 0.0
    small, big = (wa, wb) if len(wa) <= len(wb) else (wb, wa)
    return max(jaccard(wa, wb), 0.85 * containment(small, big)) if len(small) >= 3 else jaccard(wa, wb)


# --------------------------------------------------------------------------- code similarity

_CODE_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+|==|!=|<=|>=|->|=>|[^\sA-Za-z0-9_]")


def code_tokens(text: str) -> list[str]:
    """Tokens of the added lines of a diff (or of plain code); comments and blank lines dropped."""
    lines = []
    is_diff = bool(re.search(r"^(\+\+\+ |@@ )", text or "", re.M))
    for line in (text or "").splitlines():
        if is_diff:
            if not line.startswith("+") or line.startswith("+++"):
                continue
            line = line[1:]
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "//", "/*", "*")):
            continue
        lines.append(stripped)
    return _CODE_TOKEN.findall("\n".join(lines))


def shingles(tokens: list[str], k: int = 7) -> set[tuple[str, ...]]:
    return {tuple(tokens[i:i + k]) for i in range(max(len(tokens) - k + 1, 0))}


def code_similarity(ours: str, theirs: str, k: int = 7) -> float:
    """Share of our k-token shingles that also appear in theirs (1.0 = a verbatim copy)."""
    a, b = shingles(code_tokens(ours), k), shingles(code_tokens(theirs), k)
    return len(a & b) / len(a) if a and b else 0.0
