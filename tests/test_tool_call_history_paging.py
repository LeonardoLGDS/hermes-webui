from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SESSIONS_JS = (ROOT / "static" / "sessions.js").read_text(encoding="utf-8")


def test_sessions_js_resyncs_tool_calls_after_history_window_replacement():
    """History paging replaces S.messages with a larger window.

    Legacy sessions keep tool card data in session.tool_calls, so that side data
    must be refreshed alongside the message window. Otherwise renderMessages()
    can keep stale anchors and show unloaded/thinking placeholders while the
    user scrolls through history.
    """
    assert "function _syncToolCallsForLoadedMessages(messages, sessionToolCalls)" in SESSIONS_JS
    assert "_syncToolCallsForLoadedMessages(msgs, data.session.tool_calls);" in SESSIONS_JS
    # P0 windowing refactor installs _setMessageSegments(nextSegments) between
    # the wholesale replace and the resync; adjacency pin becomes ordering pin.
    _replace_i = SESSIONS_JS.index("S.messages = nextMessages;")
    _resync_i = SESSIONS_JS.index("_syncToolCallsForLoadedMessages(nextMessages, responseSession.tool_calls);")
    assert _replace_i < _resync_i, (
        "the history-window replace must be followed by the tool-call resync for the same window"
    )
    # P0 windowing refactor moved the full-transcript wholesale replace into
    # _ensureAllMessagesLoaded installFullTranscript() helper (carry-forward
    # result is `carried`; a _setMessageSegments(null) reset now sits between
    # the replace and the truncation reset). Pin the invariant by ordering
    # within that helper instead of the old byte-adjacent block.
    _it = SESSIONS_JS[SESSIONS_JS.index("const installFullTranscript"):]
    _replace_i2 = _it.index("S.messages = carried;")
    for _needle in ("_messagesTruncated = false;", "_oldestIdx = 0;", "_syncToolCallsForLoadedMessages(carried,"):
        assert _replace_i2 < _it.index(_needle), (
            "installFullTranscript must clear truncation, rebase _oldestIdx and resync tool calls after the wholesale replace"
        )


def test_sessions_js_clears_session_tool_calls_when_messages_have_own_metadata():
    assert "const hasTc=Array.isArray(m.tool_calls)&&m.tool_calls.length>0;" in SESSIONS_JS
    assert "const hasTu=Array.isArray(m.content)&&m.content.some(p=>p&&p.type==='tool_use');" in SESSIONS_JS
    assert "windowOffset" not in SESSIONS_JS
    assert "copy.assistant_msg_idx=idx-offset;" not in SESSIONS_JS
    assert "S.toolCalls=sessionToolCalls.map(tc=>({...tc,done:true}));" in SESSIONS_JS
    assert "S.toolCalls=[];" in SESSIONS_JS
