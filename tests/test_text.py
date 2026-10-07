"""The one rule for control characters in text from outside (U16, U17), checked against Unicode."""

import unicodedata

from sales_ops.text import find_control_character, has_control_character


def test_every_control_character_but_tab_and_line_breaks_is_found() -> None:
    control = [chr(c) for c in range(0x110000) if unicodedata.category(chr(c)) == "Cc"]
    assert len(control) == 65  # U+0000 to U+001F and U+007F to U+009F
    allowed = [c for c in control if not has_control_character(f"a{c}b")]
    assert allowed == ["\t", "\n", "\r"]
    assert all(find_control_character(f"a{c}b") == c for c in control if c not in allowed)


def test_no_other_character_is_taken_for_a_control_character() -> None:
    others = [chr(c) for c in range(0x110000) if unicodedata.category(chr(c)) not in ("Cc", "Cs")]
    assert find_control_character("".join(others)) is None


def test_every_text_is_searched_and_the_first_character_found_is_returned() -> None:
    assert find_control_character("clean", "bell\x07 then nul\x00") == "\x07"
    assert find_control_character("clean", "also clean") is None
    assert has_control_character() is False
