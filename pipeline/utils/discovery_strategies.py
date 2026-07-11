"""Regex patterns shared by the discovery phases (scripts 1 and 2).

Identifies user stories written in the Connextra format
("As a ..., I want ..., so that ...") and title tags such as ``[US]``,
``[User Story]`` or ``US-42:``. Keeping the patterns in one module
guarantees the two discovery phases stay consistent.
"""
from __future__ import annotations

import re


# Tag regex, applied to the issue title. Matches (case-insensitive):
#   [US], [US-42], [US_42], [User Story], and "US-42" at the start of
#   the title.
REGEX_TAG_COMBINED = re.compile(
    r'(\[\s*US\s*\]'
    r'|\[\s*US[-_\s]*\d+\s*\]'
    r'|\[\s*user[\s_-]*story\s*\]'
    r'|^\s*US\s*[-_]?\s*\d+\b)',
    re.IGNORECASE
)


# Connextra regexes, applied to the issue body. The PARTIAL form matches
# the minimal role+action pattern; the COMPLETE form also requires the
# motivation clause ("so that" / "in order to" / "so I can"). The
# blacklist excludes English idioms such as "as a matter of fact" that
# would otherwise be read as roles.
_BLACKLIST_AS_A = (
    r"matter\s+of\s+fact"
    r"|result"
    r"|consequence"
    r"|rule"
    r"|side\s+note"
    r"|reminder"
    r"|general\s+rule"
)

REGEX_CONNEXTRA_PARTIAL = re.compile(
    rf'as\s+an?\s+(?!{_BLACKLIST_AS_A})\S+[^.]{{0,300}}?'
    r'(i\s+want|i\s+need|i\s+would\s+like|i\s+should\s+be\s+able\s+to|i\s+can)',
    re.IGNORECASE | re.DOTALL
)

REGEX_CONNEXTRA_COMPLETE = re.compile(
    rf'as\s+an?\s+(?!{_BLACKLIST_AS_A})\S+[^.]{{0,300}}?'
    r'(i\s+want|i\s+need|i\s+would\s+like)[^.]{{0,300}}?'
    r'(so\s+that|in\s+order\s+to|so\s+i\s+can)',
    re.IGNORECASE | re.DOTALL
)
