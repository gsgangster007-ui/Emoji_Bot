import os
import re
import json
import logging
import uuid
from pathlib import Path

import emoji as emoji_lib
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity
from telegram.ext import CallbackQueryHandler
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# Put your BotFather token here, or set BOT_TOKEN in Railway/your environment.
BOT_TOKEN = os.getenv("BOT_TOKEN", "PASTE_YOUR_BOT_TOKEN_HERE")

# Optional:
# CUSTOM_EMOJI_PACKS="pack_name_1,pack_name_2"
# If pack names are supplied, the bot can index those custom-emoji packs too.
CUSTOM_EMOJI_PACKS = [
    x.strip()
    for x in os.getenv("CUSTOM_EMOJI_PACKS", "").split(",")
    if x.strip()
]

DATA_FILE = Path(__file__).with_name("data.json")
ID_RE = re.compile(r"^\d{10,30}$")
EMBEDDED_ID_RE = re.compile(r"(?<!\d)(\d{10,30})(?!\d)")
PACK_CALLBACKS = {}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def utf16_len(text: str) -> int:
    """Telegram MessageEntity offsets/lengths use UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2


def utf16_slice(text: str, offset: int, length: int) -> str:
    """Slice text using Telegram's UTF-16 offset/length units."""
    raw = text.encode("utf-16-le")
    part = raw[offset * 2 : (offset + length) * 2]
    return part.decode("utf-16-le", errors="ignore")


def normalize_emoji(value: str) -> str:
    """Keep the normal emoji matching tolerant of variation-selector differences."""
    return value.replace("\ufe0f", "")


def load_data() -> dict:
    default = {"emoji_index": {}, "packs": [], "ids": {}, "id_packs": {}}

    try:
        if not DATA_FILE.exists():
            return default

        with DATA_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            return default

        data.setdefault("emoji_index", {})
        data.setdefault("packs", [])
        data.setdefault("ids", {})
        data.setdefault("id_packs", {})
        return data
    except Exception:
        logger.exception("Could not read data.json; using an empty index.")
        return default


def save_data(data: dict) -> None:
    """Atomically save the emoji index so a crash does not leave a half-written JSON."""
    tmp = DATA_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(DATA_FILE)


DATA = load_data()


def add_index_entry(alt: str, custom_emoji_id: str) -> None:
    """Store both exact and variation-selector-normalized forms."""
    if not alt or not custom_emoji_id:
        return

    keys = {alt, normalize_emoji(alt)}

    for key in keys:
        if not key:
            continue

        values = DATA["emoji_index"].setdefault(key, [])
        if custom_emoji_id not in values:
            values.append(custom_emoji_id)

    DATA["ids"][custom_emoji_id] = alt


def add_pack_name(pack_name: str) -> None:
    if pack_name and pack_name not in DATA["packs"]:
        DATA["packs"].append(pack_name)


async def index_custom_emoji_pack(bot, pack_name: str) -> int:
    """
    Index every custom emoji in a known custom-emoji sticker set.

    This is useful because Telegram's Bot API does not provide a global
    "search every custom emoji by normal emoji" method. Once a pack is known,
    its stickers can be indexed and searched locally.
    """
    if not pack_name:
        return 0

    try:
        sticker_set = await bot.get_sticker_set(pack_name)
    except Exception:
        logger.exception("Could not load custom emoji pack: %s", pack_name)
        return 0

    count = 0

    for sticker in sticker_set.stickers:
        custom_id = getattr(sticker, "custom_emoji_id", None)
        alt = getattr(sticker, "emoji", None)

        if not custom_id or not alt:
            continue

        custom_id = str(custom_id)
        add_index_entry(alt, custom_id)
        DATA["id_packs"][custom_id] = pack_name
        count += 1

    add_pack_name(pack_name)
    save_data(DATA)

    logger.info("Indexed %s custom emojis from pack %s", count, pack_name)
    return count


async def index_custom_emoji_ids(bot, custom_ids) -> list:
    """
    Resolve the IDs found in a Telegram message, save their base emoji,
    and automatically learn the custom-emoji pack they belong to.
    """
    unique_ids = list(dict.fromkeys(str(x) for x in custom_ids if x))
    if not unique_ids:
        return []

    resolved = []

    try:
        # Telegram accepts a batch of custom emoji IDs.
        stickers = await bot.get_custom_emoji_stickers(custom_emoji_ids=unique_ids)
    except Exception:
        logger.exception("get_custom_emoji_stickers failed")
        return []

    new_pack_names = []

    for sticker in stickers:
        custom_id = getattr(sticker, "custom_emoji_id", None)
        if not custom_id:
            continue

        custom_id = str(custom_id)
        alt = sticker.emoji or DATA["ids"].get(custom_id) or "🙂"

        add_index_entry(alt, custom_id)
        resolved.append((alt, custom_id))

        pack_name = getattr(sticker, "set_name", None)
        if pack_name:
            DATA["id_packs"][custom_id] = pack_name
            if pack_name not in DATA["packs"]:
                add_pack_name(pack_name)
                new_pack_names.append(pack_name)

    save_data(DATA)

    # Automatically scan the pack(s) from which the forwarded custom emoji came.
    # This is what makes normal-emoji -> premium-emoji lookup much more useful.
    for pack_name in new_pack_names:
        await index_custom_emoji_pack(bot, pack_name)

    return resolved


def entity_overlaps(start_utf16: int, end_utf16: int, ranges) -> bool:
    for start, end in ranges:
        if start < end_utf16 and end > start_utf16:
            return True
    return False


def extract_custom_entities(text: str, entities) -> tuple[list, list]:
    """
    Return:
      found = [(display_alt, custom_emoji_id), ...]
      covered_ranges = [(utf16_start, utf16_end), ...]
    """
    found = []
    covered = []

    for entity in entities or []:
        if entity.type != MessageEntity.CUSTOM_EMOJI:
            continue

        custom_id = getattr(entity, "custom_emoji_id", None)
        if not custom_id:
            continue

        alt = utf16_slice(text, entity.offset, entity.length) or "🙂"
        found.append((alt, str(custom_id)))
        covered.append((entity.offset, entity.offset + entity.length))

    return found, covered


def extract_normal_emojis(text: str, covered_ranges) -> list[str]:
    """
    Find ordinary Unicode emojis in the message while ignoring the characters
    already represented by Telegram CUSTOM_EMOJI entities.
    """
    result = []

    try:
        items = emoji_lib.emoji_list(text)
    except Exception:
        return result

    for item in items:
        start_char = item["match_start"]
        end_char = item["match_end"]
        value = item["emoji"]

        start_utf16 = utf16_len(text[:start_char])
        end_utf16 = utf16_len(text[:end_char])

        if entity_overlaps(start_utf16, end_utf16, covered_ranges):
            continue

        result.append(value)

    return result


def lookup_known_premium_ids(normal_emoji: str) -> list[str]:
    keys = [normal_emoji, normalize_emoji(normal_emoji)]

    result = []
    for key in keys:
        for custom_id in DATA["emoji_index"].get(key, []):
            if custom_id not in result:
                result.append(custom_id)

    return result


def display_alt_for_id(custom_id: str, fallback: str = "🙂") -> str:
    return DATA["ids"].get(str(custom_id), fallback)


async def send_emoji_id_list(
    update: Update,
    pairs: list[tuple[str, str]],
    note: str | None = None,
) -> None:
    """
    Send output like the reference screenshot:
      [premium emoji] 535...
      [premium emoji] 535...

    If Telegram tells us which custom-emoji pack an ID belongs to, add a
    "View other emojis from pack" button for that pack.
    """
    if not update.message or not pairs:
        return

    unique = []
    seen_ids = set()

    for alt, custom_id in pairs:
        custom_id = str(custom_id)
        if custom_id in seen_ids:
            continue
        seen_ids.add(custom_id)
        unique.append((alt or display_alt_for_id(custom_id), custom_id))

    if not unique:
        return

    lines = []
    entities = []
    cursor_utf16 = 0

    for index, (alt, custom_id) in enumerate(unique):
        if index:
            lines.append("\n")
            cursor_utf16 += 1

        alt = alt or display_alt_for_id(custom_id)

        emoji_offset = cursor_utf16
        emoji_length = utf16_len(alt)

        lines.append(alt)
        cursor_utf16 += emoji_length

        entities.append(
            MessageEntity(
                type=MessageEntity.CUSTOM_EMOJI,
                offset=emoji_offset,
                length=emoji_length,
                custom_emoji_id=custom_id,
            )
        )

        # Render the Custom Emoji ID as Telegram inline-code text.
        # This makes the ID visually copyable/selectable like normal code text.
        id_offset = cursor_utf16 + 1
        lines.append(f" {custom_id}")
        id_length = utf16_len(custom_id)
        entities.append(
            MessageEntity(
                type=MessageEntity.CODE,
                offset=id_offset,
                length=id_length,
            )
        )
        cursor_utf16 += 1 + id_length

    output = "".join(lines)

    if note:
        output = f"{output}\n\n{note}"

    # Show exactly ONE pack button, bound to exactly ONE source pack.
    # Never merge packs. The button uses the first detected Premium Emoji
    # whose exact Telegram custom-emoji pack is known.
    selected_pack = None
    for _, custom_id in unique:
        pack_name = DATA.get("id_packs", {}).get(str(custom_id))
        if pack_name:
            selected_pack = pack_name
            break

    reply_markup = None
    if selected_pack:
        token = uuid.uuid4().hex[:12]
        PACK_CALLBACKS[token] = selected_pack
        # Keep memory bounded.
        if len(PACK_CALLBACKS) > 1000:
            for old_token in list(PACK_CALLBACKS)[:200]:
                PACK_CALLBACKS.pop(old_token, None)
        reply_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "View other emojis from pack",
                callback_data=f"pack:{token}",
            )
        ]])

    try:
        await update.message.reply_text(
            output,
            entities=entities,
            reply_markup=reply_markup,
        )
    except Exception:
        logger.exception("Could not send custom emoji output")
        plain_lines = [f"{alt} {custom_id}" for alt, custom_id in unique]
        if note:
            plain_lines.append("")
            plain_lines.append(note)
        await update.message.reply_text(
            "\n".join(plain_lines),
            reply_markup=reply_markup,
        )


async def show_pack(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show ONLY Premium/Custom Emojis from the exact pack bound to this button."""
    query = update.callback_query
    if not query:
        return

    await query.answer()

    data = query.data or ""
    if not data.startswith("pack:"):
        return

    token = data[5:].strip()
    pack_name = PACK_CALLBACKS.get(token)

    # Backward compatibility for buttons created before this update.
    # Old callbacks may contain a list; deliberately use ONE pack only.
    if isinstance(pack_name, (list, tuple)):
        pack_name = pack_name[0] if pack_name else None
    if not pack_name:
        pack_name = token if token else None
    if not pack_name:
        return

    try:
        sticker_set = await context.bot.get_sticker_set(pack_name)
    except Exception:
        logger.exception("Could not load exact pack: %s", pack_name)
        return

    pairs = []
    seen_ids = set()

    for sticker in sticker_set.stickers:
        # STRICT: only stickers that Telegram marks with a Custom Emoji ID.
        custom_id = getattr(sticker, "custom_emoji_id", None)
        if not custom_id:
            continue

        custom_id = str(custom_id)
        if custom_id in seen_ids:
            continue
        seen_ids.add(custom_id)

        # The visible character is only a carrier for Telegram's CUSTOM_EMOJI
        # entity. It is never intentionally returned as a normal emoji.
        alt = getattr(sticker, "emoji", None) or DATA["ids"].get(custom_id) or "▫️"
        add_index_entry(alt, custom_id)
        DATA["id_packs"][custom_id] = pack_name
        pairs.append((alt, custom_id))

    add_pack_name(pack_name)
    save_data(DATA)

    if not pairs:
        # No unrelated text/emoji is emitted for an empty/non-custom pack.
        return

    # Split safely under Telegram's message length limit.
    chunks = []
    current = []
    units = 0
    for alt, custom_id in pairs:
        row_units = utf16_len(alt) + utf16_len(custom_id) + 2
        if current and units + row_units > 3500:
            chunks.append(current)
            current = []
            units = 0
        current.append((alt, custom_id))
        units += row_units
    if current:
        chunks.append(current)

    for chunk in chunks:
        parts = []
        entities = []
        cursor = 0

        for index, (alt, custom_id) in enumerate(chunk):
            if index:
                parts.append("\n")
                cursor += 1

            emoji_offset = cursor
            emoji_length = utf16_len(alt)
            parts.append(alt)
            cursor += emoji_length
            entities.append(
                MessageEntity(
                    type=MessageEntity.CUSTOM_EMOJI,
                    offset=emoji_offset,
                    length=emoji_length,
                    custom_emoji_id=custom_id,
                )
            )

            parts.append(" ")
            cursor += 1
            id_offset = cursor
            parts.append(custom_id)
            id_length = utf16_len(custom_id)
            cursor += id_length
            entities.append(
                MessageEntity(
                    type=MessageEntity.CODE,
                    offset=id_offset,
                    length=id_length,
                )
            )

        text_out = "".join(parts)
        try:
            # Output contains ONLY the exact pack's Premium Emoji + its ID.
            await query.message.reply_text(text_out, entities=entities)
        except Exception:
            # Never leak Unicode fallback emoji if Telegram rejects an entity.
            await query.message.reply_text("\n".join(cid for _, cid in chunk))


async def handle_direct_id(update: Update, context: ContextTypes.DEFAULT_TYPE, raw: str):
    emoji_id = raw.strip()

    try:
        stickers = await context.bot.get_custom_emoji_stickers(
            custom_emoji_ids=[emoji_id]
        )
    except Exception as exc:
        logger.exception("get_custom_emoji_stickers failed")
        await update.message.reply_text(
            "❌ Telegram could not look up this Custom Emoji ID.\n\n"
            f"ID: {emoji_id}\n"
            f"Error: {type(exc).__name__}"
        )
        return

    if not stickers:
        await update.message.reply_text(
            "❌ Custom Emoji not found.\n\n"
            "The ID may be invalid or the emoji may have been removed."
        )
        return

    sticker = stickers[0]
    alt = sticker.emoji or "🙂"
    await index_custom_emoji_ids(context.bot, [emoji_id])

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔗 Open Emoji Info",
                    url=(
                        f"https://t.me/{context.bot.username}?start={emoji_id}"
                        if context.bot.username
                        else "https://t.me/"
                    ),
                )
            ]
        ]
    )

    try:
        text = f"{alt} {emoji_id}"
        entity = MessageEntity(
            type=MessageEntity.CUSTOM_EMOJI,
            offset=0,
            length=utf16_len(alt),
            custom_emoji_id=emoji_id,
        )
        id_entity = MessageEntity(
            type=MessageEntity.CODE,
            offset=utf16_len(alt) + 1,
            length=utf16_len(emoji_id),
        )

        await update.message.reply_text(
            text,
            entities=[entity, id_entity],
            reply_markup=keyboard,
        )
    except Exception:
        await update.message.reply_text(f"{alt} {emoji_id}", reply_markup=keyboard)


def collect_custom_ids_from_raw(value) -> list[str]:
    """Recursively recover custom_emoji_id values from Telegram's raw message dict."""
    found = []

    def walk(obj):
        if isinstance(obj, dict):
            custom_id = obj.get("custom_emoji_id")
            if custom_id:
                custom_id = str(custom_id)
                if custom_id not in found:
                    found.append(custom_id)
            for child in obj.values():
                walk(child)
        elif isinstance(obj, (list, tuple)):
            for child in obj:
                walk(child)

    walk(value)
    return found


def collect_message_sources(message):
    """Collect every text/caption-like field that may carry Telegram entities."""
    sources = []

    if getattr(message, "text", None):
        sources.append((message.text, getattr(message, "entities", None) or []))
    if getattr(message, "caption", None):
        sources.append((message.caption, getattr(message, "caption_entities", None) or []))

    # Newer Telegram messages may contain quoted/replied text with its own entities.
    quote = getattr(message, "quote", None)
    if quote and getattr(quote, "text", None):
        sources.append((quote.text, getattr(quote, "entities", None) or []))

    return sources


async def resolve_ids_in_batches(bot, ids: list[str]) -> list:
    """Resolve Custom Emoji IDs safely in Bot API sized batches."""
    unique_ids = list(dict.fromkeys(str(x) for x in ids if x))
    stickers = []
    for i in range(0, len(unique_ids), 200):
        batch = unique_ids[i:i + 200]
        try:
            stickers.extend(await bot.get_custom_emoji_stickers(custom_emoji_ids=batch))
        except Exception:
            logger.exception("Could not resolve custom emoji ID batch")
    return stickers


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message:
        return

    # Direct Custom Emoji ID support.
    if getattr(message, "text", None) and ID_RE.fullmatch(message.text.strip()):
        await handle_direct_id(update, context, message.text.strip())
        return

    sources = collect_message_sources(message)

    pairs = []
    seen_ids = set()
    all_custom_ids = []
    all_normal_emojis = []

    def add_pair(alt, custom_id):
        custom_id = str(custom_id)
        if custom_id in seen_ids:
            return
        seen_ids.add(custom_id)
        pairs.append((alt or DATA.get("ids", {}).get(custom_id) or "▫" , custom_id))

    # 1) Read CUSTOM_EMOJI entities from every available text/caption source.
    # This is the important forwarded-message fix: we do not assume only
    # message.text/message.entities exists.
    for text_value, entities in sources:
        custom_pairs, covered_ranges = extract_custom_entities(text_value, entities)
        for alt, custom_id in custom_pairs:
            all_custom_ids.append(custom_id)
            add_pair(alt, custom_id)

        # Also detect bare numeric Custom Emoji IDs mixed into the text.
        all_custom_ids.extend(EMBEDDED_ID_RE.findall(text_value))

        # Keep ordinary emoji lookup support, excluding ranges that are already
        # Telegram CUSTOM_EMOJI entities.
        all_normal_emojis.extend(extract_normal_emojis(text_value, covered_ranges))

    # 2) Defensive raw-dict scan. Some forwarded/quoted Telegram payloads can
    # carry entity data in nested structures. Recover every custom_emoji_id.
    try:
        raw_message = message.to_dict()
        all_custom_ids.extend(collect_custom_ids_from_raw(raw_message))
    except Exception:
        logger.exception("Could not inspect raw Telegram message")

    all_custom_ids = list(dict.fromkeys(str(x) for x in all_custom_ids if x))

    # 3) Resolve every discovered ID to the actual Telegram Custom Emoji.
    if all_custom_ids:
        stickers = await resolve_ids_in_batches(context.bot, all_custom_ids)
        resolved_ids = set()

        for sticker in stickers:
            custom_id = getattr(sticker, "custom_emoji_id", None)
            if not custom_id:
                continue
            custom_id = str(custom_id)
            resolved_ids.add(custom_id)

            alt = getattr(sticker, "emoji", None) or DATA.get("ids", {}).get(custom_id) or "▫"
            add_index_entry(alt, custom_id)

            pack_name = getattr(sticker, "set_name", None)
            if pack_name:
                DATA["id_packs"][custom_id] = pack_name
                add_pack_name(pack_name)

            add_pair(alt, custom_id)

        save_data(DATA)

        # Learn newly discovered packs so ONE pack button can later show all
        # premium/custom emojis from all relevant packs.
        pack_names = []
        for custom_id in all_custom_ids:
            pack_name = DATA.get("id_packs", {}).get(custom_id)
            if pack_name and pack_name not in pack_names:
                pack_names.append(pack_name)
        for pack_name in pack_names:
            await index_custom_emoji_pack(context.bot, pack_name)

    # 4) Normal Unicode emoji -> already-indexed Premium Emoji lookup.
    # Normal emojis themselves are NEVER returned as pack results.
    for normal in all_normal_emojis:
        for custom_id in lookup_known_premium_ids(normal):
            add_pair(DATA.get("ids", {}).get(str(custom_id)) or normal, custom_id)

    if pairs:
        await send_emoji_id_list(update, pairs)
        return

    if all_normal_emojis:
        await message.reply_text(
            "I found normal Unicode emoji in this message, but no matching "
            "Premium/Custom Emoji is indexed yet. Send/forward the matching "
            "Premium Emoji or its pack once, then I can return its Premium Emoji + ID."
        )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Unhandled bot error", exc_info=context.error)


def main():
    if BOT_TOKEN == "PASTE_YOUR_BOT_TOKEN_HERE":
        raise RuntimeError(
            "Set BOT_TOKEN in the environment or replace "
            "'PASTE_YOUR_BOT_TOKEN_HERE' in main.py."
        )

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))

    # Handle text, photos with captions, videos with captions, documents with
    # captions, forwarded messages, and other message types supported by PTB.
    app.add_handler(
        MessageHandler(filters.ALL & ~filters.COMMAND, handle_message)
    )

    # Button: "View other emojis from pack"
    app.add_handler(CallbackQueryHandler(show_pack, pattern=r"^pack:"))

    app.add_error_handler(error_handler)

    print("Custom Emoji ID Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
