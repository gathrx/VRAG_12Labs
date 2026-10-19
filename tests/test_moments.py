"""Unit tests for citation parsing. The payload mirrors the documented response format:
https://docs.twelvelabs.io/v1.3/api-reference/responses/the-response-object
"""

from vrag.moments import (format_range, is_no_match, clean_no_match, moments_from_citations,
                          render_answer)
from vrag.twelvelabs_service import AnswerPart, parse_response

ITEM = "ksi_069e9870-3c4d-7abc-9012-3456789abcde"


def _part():
    text = ("A red car drives into the parking lot [1]. "
            "Later, the red car leaves through the gate [2].")
    i1 = text.index("[1]")
    i2 = text.index("[2]")
    return AnswerPart(text=text, annotations=[
        {"type": "video_citation", "start_index": i1, "end_index": i1 + 2, "item_id": ITEM,
         "start_sec": 34.0, "end_sec": 41.0},
        {"type": "video_citation", "start_index": i2, "end_index": i2 + 2,
         "item_id": ITEM.removeprefix("ksi_"), "start_sec": 95.5, "end_sec": 99.0},
        {"type": "url_citation", "start_index": 0, "end_index": 4, "url": "https://example.com", "title": "x"},
    ])


def test_moments_from_citations():
    moments, whole = moments_from_citations([_part()], ITEM, duration=120.0)
    assert not whole
    assert [(m.start_sec, m.end_sec) for m in moments] == [(34.0, 41.0), (95.5, 99.0)]
    assert moments[0].description == "A red car drives into the parking lot."
    assert moments[1].description == "Later, the red car leaves through the gate."
    assert all(m.verified for m in moments)


def test_out_of_range_and_whole_video():
    part = AnswerPart(text="Seen here [1] and here [2].", annotations=[
        {"type": "video_citation", "start_index": 10, "end_index": 12, "item_id": ITEM,
         "start_sec": 500.0, "end_sec": 510.0},
        {"type": "video_citation", "start_index": 23, "end_index": 25, "item_id": ITEM},
    ])
    moments, whole = moments_from_citations([part], ITEM, duration=120.0)
    assert moments == [] and whole


def test_marker_after_sentence_end():
    part = AnswerPart(text="The dog jumps over a fence. [1]", annotations=[
        {"type": "video_citation", "start_index": 28, "end_index": 30, "item_id": ITEM,
         "start_sec": 3.0, "end_sec": 6.0}])
    moments, _ = moments_from_citations([part], ITEM, duration=10.0)
    assert moments[0].description == "The dog jumps over a fence."


def test_render_answer_replaces_markers():
    out = render_answer([_part()], ITEM)
    assert "[1]" not in out and "`00:34–00:41`" in out and "`01:36–01:39`" in out


def test_no_match():
    assert is_no_match("NO_MATCH: The video shows only a beach.")
    assert clean_no_match("NO_MATCH: The video shows only a beach.") == "The video shows only a beach."
    assert format_range(3661, 3670) == "1:01:01–1:01:10"


def test_parse_response_dict():
    resp = {"id": "resp_1", "session_id": "sess_1", "status": "completed", "incomplete_details": None,
            "billing": {"amount": "0.01", "currency": "USD"},
            "output": [
                {"type": "message", "phase": "commentary", "content": [{"type": "output_text", "text": "x",
                                                                         "annotations": []}]},
                {"type": "message", "phase": "final_answer", "content": [
                    {"type": "output_text", "text": "Hi [1]", "annotations": [
                        {"type": "video_citation", "start_index": 3, "end_index": 5, "item_id": ITEM,
                         "start_sec": 1.0, "end_sec": 2.0}]}]}]}
    ans = parse_response(resp)
    assert ans.session_id == "sess_1" and ans.text == "Hi [1]"
    assert ans.parts[0].annotations[0]["start_sec"] == 1.0
    assert ans.billing["amount"] == "0.01"
