V8 exact-pack button fix
- One View other emojis from pack button only.
- Button is bound to one exact source custom-emoji pack; packs are never merged.
- Clicking it returns only that pack's Telegram Premium/Custom Emoji entries and IDs.
- No header, pack title, count, decorative emoji, or unrelated pack content is emitted.
- Stickers without custom_emoji_id are skipped.
- If Telegram rejects rendering a custom emoji entity, fallback contains IDs only (never normal Unicode emoji).
