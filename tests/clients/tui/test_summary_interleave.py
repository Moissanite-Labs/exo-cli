"""Memory upkeep must not duplicate the assistant message it interrupts visually."""
import pytest

from exo.protocol import schema as s
from exo.core.types import Message, ContentPart
from test_tui import run


@pytest.mark.parametrize("continued", [False, True])
def test_summary_between_delta_and_completion_does_not_duplicate_reply(tmp_path, continued):
    async def scenario(app, pilot, client, eng):
        def emit(kind, body):
            app.handle(s.Event(seq=999, type=kind, session=app.session, body=body))
        text = "A reply interrupted by memory upkeep."
        emit("message.delta", s.MessageDelta(message_id="reply", text=text))
        await pilot.pause()
        emit("memory.summarized", s.MemorySummarized(
            node_id="test", level=1, start=1, end=2, before_tokens=100, after_tokens=50))
        if continued:
            emit("message.delta", s.MessageDelta(message_id="reply", text=" More text."))
            text += " More text."
        emit("message.completed", s.MessageCompleted(message=Message(
            id="reply", role="assistant", content=[ContentPart(type="text", text=text)])))
        await pilot.pause()
        assert app.transcript_text().count(text) == 1
    run(tmp_path, scenario)


def test_completion_updates_its_own_block_and_replay_does_not_duplicate(tmp_path):
    async def scenario(app, pilot, client, eng):
        def emit(body):
            app.handle(s.Event(seq=999, type="test", session=app.session, body=body))

        emit(s.MessageDelta(message_id="first", text="First partial"))
        emit(s.MessageDelta(message_id="second", text="Second partial"))
        first = s.MessageCompleted(message=Message(id="first", role="assistant", content=[ContentPart(type="text", text="First complete")]))
        emit(first)
        assert app.cur_id == "second"
        assert app.cur.text == "Second partial"
        emit(s.MessageDelta(message_id="second", text=" continued"))
        emit(s.MessageCompleted(message=Message(id="second", role="assistant", content=[ContentPart(type="text", text="Second complete")])))
        emit(first)
        await pilot.pause()
        transcript = app.transcript_text()
        assert transcript.count("First complete") == 1
        assert transcript.count("Second complete") == 1
        app.clear_transcript()
        assert not app.reply_blocks

    run(tmp_path, scenario)
