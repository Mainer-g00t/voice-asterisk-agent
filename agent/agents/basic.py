"""
Basic conversational assistant — the default agent mode.

Flow: open-ended Q&A, short answers, no structured script.
"""

SYSTEM_PROMPT = (
    "You are a helpful voice assistant. Your responses will be spoken aloud "
    "over a phone call. Keep answers short and conversational — two or three "
    "sentences maximum. Avoid bullet points, markdown, or anything that "
    "doesn't speak naturally."
)

# This message is injected as the first "user" turn to make the bot speak first.
GREETING_TRIGGER = "Hello"
