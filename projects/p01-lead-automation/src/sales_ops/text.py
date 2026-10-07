"""One rule for text that comes from outside: LLM output, human edits and API input (U16, U17).

Control characters make the text invalid. They are never cleaned up and accepted: the output
goes to review, and the request is answered with 422.
"""

import re

# Unicode control characters (category Cc: U+0000 to U+001F, U+007F to U+009F) other than tab,
# line feed and carriage return, which count as ordinary whitespace. NUL cannot even be stored
# in PostgreSQL.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def find_control_character(*texts: str) -> str | None:
    """The first control character in the texts, or None."""
    for text in texts:
        if match := _CONTROL.search(text):
            return match.group()
    return None


def has_control_character(*texts: str) -> bool:
    return find_control_character(*texts) is not None
