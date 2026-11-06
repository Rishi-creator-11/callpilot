"""Conservative paraphrase matcher for explicitly approved FAQ answers.

No generative answers: returned text is copied from a single approved fact.
When the phrasing is ambiguous, return None and escalate.
"""
import re
from difflib import SequenceMatcher

_ALIASES = {
    "purchase": "buy", "purchasing": "buy", "purchased": "buy",
    "buying": "buy", "bought": "buy",
    "containers": "container", "halves": "half",
    "operating": "opening", "working": "opening",
    "hours": "hour", "times": "time",
}
_STOP = {"can","could","may","i","we","you","your","our","a","an","the","do","does",
         "did","is","are","it","there","me","please","what","which","would","like",
         "to","of","for","with","tell","about","my"}
_OPPOSITES = [("half","full"),("yes","no"),("accept","refuse"),("open","closed")]

def terms(text: str) -> tuple[str,...]:
    words = re.findall(r"[a-z0-9]+", text.casefold())
    return tuple(_ALIASES.get(w,w) for w in words if w not in _STOP)

def match_approved(question: str, facts: list[dict]) -> dict | None:
    query = terms(question)
    qset = set(query)
    if not qset or len(qset) < 2:
        return None
    candidates=[]
    for fact in facts:
        known=terms(fact["question"])
        kset=set(known)
        if not kset or len(kset)<2:
            continue
        if any((a in qset and b in kset) or (b in qset and a in kset) for a,b in _OPPOSITES):
            continue
        similarity=SequenceMatcher(None," ".join(query)," ".join(known)).ratio()
        overlap=len(qset & kset)/len(qset | kset)
        # Only very close paraphrases; do not invent answers for loosely related queries.
        if qset == kset:
            score=1.0
        elif similarity >= .87 and overlap >= .66:
            score=(similarity+overlap)/2
        else:
            continue
        candidates.append((score,fact))
    candidates.sort(key=lambda x:x[0],reverse=True)
    if not candidates:
        return None
    # Do not arbitrarily choose between equally relevant, conflicting FAQs.
    if len(candidates)>1 and candidates[0][0]-candidates[1][0]<.08:
        return None
    return candidates[0][1]
