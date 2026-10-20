"""Strict contiguous quotation checks with explicit, lossless typography equivalents."""
import copy
import difflib
import html
import re
import unicodedata

from ci.contracts import text

QUOTATION_POLICY = 'unicode-typography-v1'
MIN_QUOTATION_CHARACTERS = 20
QUOTATION_REQUIREMENT = (
    f'Each quotation must contain at least {MIN_QUOTATION_CHARACTERS} characters after '
    'Unicode and whitespace normalization. Adding spaces or blank lines does not count. '
    'Copy a substantive contiguous passage supporting the concern, not just an article '
    'title, table-of-contents label or navigation entry.')
_TYPOGRAPHY = str.maketrans({
    '\u2018': "'", '\u2019': "'", '\u201c': '"', '\u201d': '"',
    '\u00ad': '', '\ufb00': 'ff', '\ufb01': 'fi', '\ufb02': 'fl',
    '\ufb03': 'ffi', '\ufb04': 'ffl', '\ufb05': 'st', '\ufb06': 'st',
})


def normalize_quotation(value):
    # NFC retains mathematical symbols, superscripts, identifiers and numbers.
    # Do not case-fold, remove punctuation/negation, or join PDF line-end hyphens.
    return ' '.join(unicodedata.normalize('NFC', html.unescape(value)).translate(_TYPOGRAPHY).split())


def contains_quotation(source_text, quotation):
    # A contiguous span cannot truncate a word, identifier or numeric literal.
    prefix = r'(?<!\w)' if re.match(r'\w', quotation) else ''
    suffix = r'(?!\w)' if re.search(r'\w$', quotation) else ''
    if re.match(r'^[.,]\d', quotation):
        prefix = r'(?<!\d)'
    if re.search(r'\d[.,]$', quotation):
        suffix = r'(?!\d)'
    return re.search(prefix + re.escape(quotation) + suffix, source_text) is not None


class PassageValidationError(ValueError):
    def __init__(self, failures):
        self.failures = failures
        super().__init__('; '.join(row['reason'] for row in failures))


def source_excerpt(quotation, source_text):
    """A small exact-source reading hint, never an automatic replacement or match."""
    tokens = list(re.finditer(r'\S+', source_text))
    if not tokens:
        return None
    wanted = normalize_quotation(quotation[:2000]).split()
    actual = [normalize_quotation(token.group()) for token in tokens]
    match = difflib.SequenceMatcher(None, wanted, actual, autojunk=False).find_longest_match()
    if not match.size:
        return None
    start = tokens[max(0, match.b - 8)].start()
    end = tokens[min(len(tokens) - 1, match.b + min(match.size, 60) + 24)].end()
    return source_text[start:min(end, start + 900)]


def check_source_passages(candidate, source_text):
    """Validate every passage before reporting failure; retain original quote bytes."""
    source_id = candidate.get('id') or candidate.get('url', 'source')
    failures, seen, normalized_ids = [], set(), []
    normalized = normalize_quotation(source_text)
    literal_source = ' '.join(html.unescape(source_text).split())

    def fail(passage_id, reason, **extra):
        failures.append({'sourceId': source_id, 'passageId': passage_id,
                         'textPath': candidate.get('textPath'), 'reason': reason, **extra})

    passages = candidate.get('passages')
    if not isinstance(passages, list) or not passages:
        fail(None, 'A fetched source needs stable supporting passages: ' + str(source_id))
        raise PassageValidationError(failures)
    for index, passage in enumerate(passages):
        if not isinstance(passage, dict):
            fail(None, f'Source passages must be objects: {source_id}[{index}]')
            continue
        passage_id = passage.get('id')
        for field in ('id', 'locator', 'text'):
            try:
                text(passage.get(field), 'passage.' + field)
            except ValueError as problem:
                fail(passage_id, f'{source_id}[{index}]: {problem}', field=field)
        if isinstance(passage_id, str):
            if passage_id in seen:
                fail(passage_id, f'Source passage IDs must be unique: {source_id}/{passage_id}')
            seen.add(passage_id)
        quotation = passage.get('text')
        if not isinstance(quotation, str) or not quotation.strip():
            continue
        needle = normalize_quotation(quotation)
        if len(needle) < MIN_QUOTATION_CHARACTERS:
            fail(passage_id, 'Quotation is too short after normalization: '
                 + f'{source_id}/{passage_id} ({len(needle)} characters; '
                 + f'minimum {MIN_QUOTATION_CHARACTERS}). Copy a longer substantive contiguous '
                 'passage from the fetched text; adding spaces or blank lines does not increase normalized length.',
                 ruleId='quotation-too-short', normalizedCharacters=len(needle),
                 minimumCharacters=MIN_QUOTATION_CHARACTERS, claimedText=quotation,
                 sourceExcerpt=source_excerpt(quotation, source_text), excerptIsReadingHint=True)
        elif not contains_quotation(normalized, needle):
            fail(passage_id, 'Claimed passage was not found in independently fetched source bytes: '
                 + f'{source_id}/{passage_id}', ruleId='quotation-not-found', claimedText=quotation,
                 sourceExcerpt=source_excerpt(quotation, source_text),
                 excerptIsReadingHint=True)
        elif ' '.join(html.unescape(quotation).split()) not in literal_source:
            normalized_ids.append(passage_id)
    if failures:
        raise PassageValidationError(failures)
    value = copy.deepcopy(candidate)
    value.update(sourceStatus='fetched-and-passage-checked', quotationMatchPolicy=QUOTATION_POLICY,
                 typographyNormalizedPassages=normalized_ids)
    return value
