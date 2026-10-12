V7 forwarded-message detection fix

- Detects CUSTOM_EMOJI entities from forwarded text and captions.
- Also scans nested/raw Telegram message payloads for custom_emoji_id values.
- Detects numeric Custom Emoji IDs embedded anywhere in text.
- Resolves IDs in batches and returns actual Telegram Premium/Custom Emoji + copyable ID.
- Keeps exactly one "View other emojis from pack" button.
- Pack results remain strict premium/custom-emoji only; no normal Unicode emoji fallback.
- Normal Unicode emoji lookup still uses the learned/indexed premium emoji packs.

Start command: python main.py
