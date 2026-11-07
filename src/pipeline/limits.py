"""Platform limits shared across modules. A leaf: imports nothing from the pipeline."""

from __future__ import annotations

#: Telegram's own ceilings, not ours. A text message is capped at 4096
#: characters and a media caption at 1024; the sending client splits or trims
#: anything longer before the bot is ever called. Both were silent.
TELEGRAM_TEXT_LIMIT = 4096
TELEGRAM_CAPTION_LIMIT = 1024

#: ``answerCallbackQuery`` text (the toast over a tapped button) is capped at
#: 200 characters by the Bot API; a longer one is rejected, not trimmed.
TELEGRAM_CALLBACK_ANSWER_LIMIT = 200
#: ``callback_data`` is capped at 1-64 BYTES (not characters) by the Bot API.
TELEGRAM_CALLBACK_DATA_LIMIT = 64
