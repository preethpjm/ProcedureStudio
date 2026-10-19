"""
Lightweight, mostly-dependency-free nomenclature comparison, plus an
optional offline cross-lingual layer for cases where STEP and MBOM
nomenclature are in different languages (confirmed real case: French
part descriptions in a Safran MBOM, e.g. "STATOR RECTIFIE").

Design: a small curated glossary (precise, zero risk of confidently-wrong
output) is checked first, then offline machine translation (argos-translate)
fills in whatever the glossary doesn't cover — auto-expanding coverage
without hand-maintaining every word. Deliberately offline, not a cloud
API: this pipeline processes proprietary aerospace nomenclature, and
sending that to a third-party translation service on every match attempt
is not a decision to make silently.

Both layers degrade gracefully to "no signal" if unavailable — this must
never break the matching pipeline. A missing translation just means this
one signal contributes nothing to that comparison.
"""

import re
import logging

logger = logging.getLogger("bom_reconciliation.nomenclature")

# --- Same-language normalization (unchanged from earlier) ------------------

NORMALIZATION_RULES = [
    (re.compile(r"\bSCREWLOCK\b"), "SCREW LOCK"),
    (re.compile(r"\bTANGFREE\b"), "TANG FREE"),
    (re.compile(r"\bTANGLESS\b"), "TANG LESS"),
]

STOPWORDS = {"OF", "THE", "FOR", "TO", "A", "AN"}


def _stem(word: str) -> str:
    if len(word) <= 3:
        return word
    if word.endswith("IES"):
        return word[:-3] + "Y"
    if word.endswith("ES") and word[-3] not in "AEIOU":
        return word[:-2]
    if word.endswith("S") and not word.endswith("SS"):
        return word[:-1]
    return word


def normalize_and_tokenize(text: str) -> set:
    if not text:
        return set()
    text = text.upper().strip()
    for pattern, replacement in NORMALIZATION_RULES:
        text = pattern.sub(replacement, text)
    text = re.sub(r"[,\-/]", " ", text)
    tokens = [t for t in text.split() if t and t not in STOPWORDS]
    return {_stem(t) for t in tokens}


def token_similarity(name_a: str, name_b: str) -> float:
    tokens_a = normalize_and_tokenize(name_a)
    tokens_b = normalize_and_tokenize(name_b)
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    return len(intersection) / len(union)

# Common leading "type" nouns seen across real MBOM/STEP nomenclature in
# this project so far. Best-effort category signal, NOT a rigorous
# taxonomy — e.g. "SCREW, CAP, SOCKET HEAD" and "SCREW NAS1351N4-20" both
# resolve to type SCREW even though the rest of the phrase differs
# completely. Extend only when a real mismatch demonstrates the need.

KNOWN_COMPONENT_TYPES = {
    "SCREW", "WASHER", "SEAL", "RING", "SPRING", "VALVE", "PLUG", "INSERT",
    "PIN", "BOLT", "NUT", "BUSH", "BUSHING", "PLATE", "CAP", "COVER",
    "HOUSING", "BODY", "SHAFT", "GASKET", "BEARING", "CLAMP", "SLEEVE",
    "PISTON", "ROTOR", "STATOR", "MAGNET", "COIL", "WIRE", "CABLE",
    "BRACKET", "PACKING", "ASSEMBLY", "BOBBIN",
}

# Checked BEFORE the generic single-word bucket below. Confirmed real
# false-positive: "O-RING" and "BACK-UP RING" both reduced to the same
# generic "RING" type, causing the scorer to treat an O-ring and a
# back-up ring — genuinely different components, even though they're
# often installed as a pair — as an exact component-type match.
COMPOUND_TYPE_RULES = [
    ({"O", "RING"}, "O-RING"),
    ({"BACK", "RING"}, "BACK-UP-RING"),
    ({"RETAINING", "RING"}, "RETAINING-RING"),
    ({"SNAP", "RING"}, "SNAP-RING"),
]


def extract_component_type(text):
    tokens = normalize_and_tokenize(text)
    for required_tokens, type_name in COMPOUND_TYPE_RULES:
        if required_tokens <= tokens:
            return type_name
    for t in tokens:
        if t in KNOWN_COMPONENT_TYPES:
            return t
    return None


def component_type_match(name_a, name_b):
    """0.0 if either side has no recognized type word — treated as 'no
    signal available', not as a mismatch, so this never falsely drags a
    score down just because our keyword list is incomplete.
    """
    type_a = extract_component_type(name_a)
    type_b = extract_component_type(name_b)
    if type_a is None or type_b is None:
        return 0.0
    return 1.0 if type_a == type_b else 0.0

# --- Curated glossary --------------------------------------------------------

# Seeded only from vocabulary confirmed present in real MBOM/STEP data this
# pipeline has actually processed (F6137-D24903000, F6137-D24907000).
# Extend only when a real cross-language mismatch demonstrates the need.
FR_EN_GLOSSARY = {
    "STATOR": "STATOR",
    "ROTOR": "ROTOR",
    "BOBINE": "COIL",
    "AIMANT": "MAGNET",
    "VIS": "SCREW",
    "RONDELLE": "WASHER",
    "JOINT": "SEAL",
    "RESSORT": "SPRING",
    "PLAQUE": "PLATE",
    "CORPS": "BODY",
    "AXE": "SHAFT",
    "CULASSE": "HEAD",
    "COUVERCLE": "COVER",
    "ECROU": "NUT",
    "GOUPILLE": "PIN",
    "BAGUE": "RING",
    "FIL": "WIRE",
    "FILS": "WIRES",
    "BOITIER": "HOUSING",
    "RECTIFIE": "GROUND",
    "RECTIFIEE": "GROUND",
    "USINAGE": "MACHINING",
    "USINE": "MACHINED",
    "EMBASE": "BASE",
    "BOUCHON": "PLUG",
    "CAPUCHON": "CAP",
    "COLLIER": "CLAMP",
    "DOUILLE": "SLEEVE",
    "SIEGE": "SEAT",
    "ENSEMBLE": "ASSEMBLY",
    "SUPPORT": "SUPPORT",
    "VALVE": "VALVE",
    "PISTON": "PISTON",
    "CABLE": "CABLE",
    "PRECHARGE": "PRELOAD",
}

EN_FR_GLOSSARY = {v: k for k, v in FR_EN_GLOSSARY.items()}


def _expand_with_glossary(tokens: set) -> set:
    expanded = set(tokens)
    for t in tokens:
        if t in FR_EN_GLOSSARY:
            expanded.add(FR_EN_GLOSSARY[t])
        if t in EN_FR_GLOSSARY:
            expanded.add(EN_FR_GLOSSARY[t])
    return expanded


# --- Offline machine translation (optional layer) ---------------------------

_translation_cache = {}
_mt_available = None

# Separate from _mt_available (which only tracks whether the argostranslate
# PACKAGE imports) — this tracks whether translation actually WORKS, i.e.
# whether the fr<->en language packages from setup_translation.py are
# actually installed on this machine. See is_translation_available() below
# for why this distinction matters.
_mt_health_checked = False
_mt_health_ok = False


def _get_translator():
    global _mt_available
    if _mt_available is False:
        return None
    try:
        import argostranslate.translate
        _mt_available = True
        return argostranslate.translate
    except ImportError:
        _mt_available = False
        return None


def _mt_translate(text: str, from_code: str, to_code: str) -> str:
    """Returns translated text, or the ORIGINAL text unchanged if MT is
    unavailable/uninstalled/fails — never raises, never blocks matching.
    """
    if not text:
        return ""
    cache_key = (text, from_code, to_code)
    if cache_key in _translation_cache:
        return _translation_cache[cache_key]

    translate_mod = _get_translator()
    if translate_mod is None:
        return text

    try:
        result = translate_mod.translate(text, from_code, to_code)
    except Exception:
        result = text

    _translation_cache[cache_key] = result
    return result


def is_translation_available() -> bool:
    """Actively verifies the offline MT layer can really translate a word —
    not just that the argostranslate PACKAGE imports successfully.

    Confirmed real failure mode: argostranslate==1.9.6 (from
    requirements.txt) installs and imports cleanly, so _get_translator()
    happily returns the module — but the two .argosmodel language packages
    (Language/translate-en_fr-1_9.argosmodel and translate-fr_en-1_9)
    listed in scripts/setup_translation.py are a SEPARATE manual
    installation step. If that step was never run on a given machine,
    every real translate() call raises internally, which _mt_translate()
    deliberately swallows via a broad `except Exception` (so matching never
    crashes) — but that also means cross-lingual matching silently
    degrades to glossary-only with NO visible sign anything is wrong.
    Call this once (e.g. at app/reconciliation startup) to surface that
    state instead of discovering it later as an unexplained run of
    'translated_candidate' matches that never actually fire.

    Cached after the first real check: translate() either works or it
    doesn't for the lifetime of this process, so re-attempting it on every
    reconcile() call would be wasted work for no new information.
    """
    global _mt_health_checked, _mt_health_ok
    if _mt_health_checked:
        return _mt_health_ok

    _mt_health_checked = True
    translate_mod = _get_translator()

    if translate_mod is None:
        logger.warning(
            "Cross-lingual matching: argostranslate is not installed — "
            "cross_lingual_similarity() will fall back to the curated "
            "glossary only. Run: pip install argostranslate"
        )
        _mt_health_ok = False
        return False

    try:
        # "BOBINE" is deliberately a word ALSO in the curated glossary, so
        # a working MT engine and a non-working one are both able to
        # return *a* result here — the real test is whether the engine
        # itself produced it (i.e. didn't just error out internally).
        result = translate_mod.translate("BOBINE", "fr", "en")
        _mt_health_ok = bool(result) and result.strip() != ""
    except Exception as exc:
        logger.warning(
            "Cross-lingual matching: argostranslate is installed but a "
            "test translation failed (%s) — the fr<->en language packages "
            "are most likely not installed. Run "
            "backend/scripts/setup_translation.py with the .argosmodel "
            "files in backend/Language/. Falling back to the curated "
            "glossary only in the meantime.", exc,
        )
        _mt_health_ok = False

    return _mt_health_ok


def cross_lingual_similarity(name_a: str, name_b: str) -> float:
    """Combines the curated glossary (precise, known terms) with offline MT
    (automatic, everything else). Either one finding real token overlap is
    enough. Falls back gracefully to glossary-only if argos-translate isn't
    installed/set up yet — this function never fails outright.
    """
    tokens_a = _expand_with_glossary(normalize_and_tokenize(name_a))
    tokens_b = _expand_with_glossary(normalize_and_tokenize(name_b))

    tokens_a |= normalize_and_tokenize(_mt_translate(name_a, "fr", "en"))
    tokens_a |= normalize_and_tokenize(_mt_translate(name_a, "en", "fr"))
    tokens_b |= normalize_and_tokenize(_mt_translate(name_b, "fr", "en"))
    tokens_b |= normalize_and_tokenize(_mt_translate(name_b, "en", "fr"))

    if not tokens_a or not tokens_b:
        return 0.0
    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    return len(intersection) / len(union)

