# Custom Emoji ID Bot - Updated

This version keeps the original Custom Emoji ID lookup and adds:

1. Detects Telegram Premium/Custom Emoji entities from normal messages.
2. Detects multiple Premium Emojis in one message.
3. Detects Premium Emojis in forwarded text.
4. Detects Premium Emojis in captions of forwarded photos/videos/documents.
5. Replies in the requested style:
   `[Premium Emoji] [Custom Emoji ID]`
6. Automatically learns the custom-emoji pack when it sees a Premium Emoji ID.
7. Saves the learned emoji -> ID mapping in `data.json`.
8. If a normal Unicode emoji is sent later, the bot searches its learned Premium Emoji index and returns the Premium Emoji + ID.
9. Optional `CUSTOM_EMOJI_PACKS` environment variable can seed public custom-emoji packs.

## Important Telegram limitation

Telegram Bot API does not provide a global API that means:
"given any normal emoji, search every Premium Emoji on Telegram."

So the bot cannot honestly guarantee a match for an arbitrary normal emoji before it has access to a relevant custom-emoji pack.

This update solves that as far as the Bot API allows:
- Forward/send a Premium Emoji once.
- The bot reads its Custom Emoji ID.
- It automatically discovers the sticker-set name when Telegram provides it.
- It indexes the whole custom-emoji pack.
- Then normal emoji searches can return matching Premium Emoji IDs from that learned pack.
- The index is saved in `data.json`, so it survives restarts/Railway redeploys if the file is persisted.

For additional known public custom-emoji packs, set:

`CUSTOM_EMOJI_PACKS=pack_name_1,pack_name_2`

in Railway Variables.

## Install

`pip install -r requirements.txt`

## Railway start command

`python main.py`

## Bot token

Set the Railway variable:

`BOT_TOKEN=YOUR_BOTFATHER_TOKEN`

Do not put your real token into the source code or share it publicly.

## data.json

The bot automatically updates this file. Do not delete it if you want the learned Premium Emoji mappings to remain available.


## New pack button

When the bot detects a Premium/Custom Emoji and Telegram provides its sticker
set name, the reply now includes:

`View other emojis from pack`

Pressing that button loads the whole custom-emoji pack and sends every emoji
in that pack together with its Custom Emoji ID.

Large packs are automatically split into multiple Telegram messages to stay
within Telegram's message-size limit.

The pack name is learned from the Custom Emoji ID, so you do not have to type
the pack name manually.
