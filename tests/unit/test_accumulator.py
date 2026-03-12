import pytest
from src.server.accumulator import TokenAccumulator


def make(min_chars: int = 80) -> TokenAccumulator:
    return TokenAccumulator(min_flush_chars=min_chars)


# 1. Short token with no boundary → no flush
def test_no_flush_short():
    acc = make()
    assert acc.add("Hello") == []


# 2. Flush on period
def test_flush_on_period():
    acc = make()
    result = acc.add("Hello.")
    assert result == ["Hello."]


# 3. Flush on exclamation mark
def test_flush_on_exclamation():
    acc = make()
    result = acc.add("Wow!")
    assert result == ["Wow!"]


# 4. Flush on question mark
def test_flush_on_question():
    acc = make()
    result = acc.add("Really?")
    assert result == ["Really?"]


# 5. Flush on newline
def test_flush_on_newline():
    acc = make()
    result = acc.add("Line\n")
    assert result == ["Line"]


# 6. Flush when buffer reaches min_flush_chars (no boundary)
def test_flush_on_min_chars():
    acc = make(min_chars=10)
    # Add 5 chars – no flush
    assert acc.add("Hello") == []
    # Add enough to cross 10, with a space to split at
    result = acc.add(" World X")
    assert len(result) == 1
    assert result[0].strip() != ""


# 7. Multiple boundaries in a single add() call
def test_multiple_boundaries_single_add():
    acc = make()
    result = acc.add("Hello. World! How are you?")
    assert len(result) == 3
    assert result[0] == "Hello."
    assert result[1] == "World!"
    assert result[2] == "How are you?"


# 8. Explicit flush returns remaining buffer
def test_explicit_flush():
    acc = make()
    acc.add("Incomplete sentence")
    result = acc.flush()
    assert result == ["Incomplete sentence"]


# 9. Explicit flush on empty buffer → empty list
def test_explicit_flush_empty():
    acc = make()
    assert acc.flush() == []


# 10. Buffer is cleared after flush
def test_buffer_cleared_after_flush():
    acc = make()
    acc.add("Some text")
    acc.flush()
    assert acc.flush() == []


# 11. Whitespace-only buffer flushes to empty list
def test_whitespace_only_flush():
    acc = make()
    acc.add("   ")
    assert acc.flush() == []


# 12. Simulated incremental LLM token stream
def test_incremental_llm_stream():
    acc = make(min_chars=20)
    tokens = ["The", " sky", " is", " blue", "."]
    segments: list[str] = []
    for tok in tokens:
        segments.extend(acc.add(tok))
    # Should flush at the period
    assert any("blue" in s for s in segments)
    # Nothing left in buffer
    assert acc.flush() == []
