import os
import re
import json
import logging
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

    # One button for each unique pack represented by the detected emojis.
    pack_names = []
    for _, custom_id in unique:
        pack_name = DATA.get("id_packs", {}).get(str(custom_id))
        if pack_name and pack_name not in pack_names:
            pack_names.append(pack_name)

    keyboard_rows = []
    for pack_name in pack_names[:8]:
        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    "View other emojis from pack",
                    callback_data=f"pack:{pack_name}",
                )
            ]
        )

    reply_markup = InlineKeyboardMarkup(keyboard_rows) if keyboard_rows else None

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
    """Show every custom emoji in the pack represented by the clicked emoji."""
    query = update.callback_query
    if not query:
        return

    await query.answer()

    data = query.data or ""
    if not data.startswith("pack:"):
        return

    pack_name = data[5:].strip()
    if not pack_name:
        return

    try:
        sticker_set = await context.bot.get_sticker_set(pack_name)
    except Exception:
        logger.exception("Could not load pack: %s", pack_name)
        await query.message.reply_text(
            "❌ I could not open this emoji pack right now."
        )
        return

    pairs = []
    for sticker in sticker_set.stickers:
        custom_id = getattr(sticker, "custom_emoji_id", None)
        alt = getattr(sticker, "emoji", None)

        if not custom_id:
            continue

        custom_id = str(custom_id)
        alt = alt or "🙂"

        add_index_entry(alt, custom_id)
        DATA["id_packs"][custom_id] = pack_name
        pairs.append((alt, custom_id))

    add_pack_name(pack_name)
    save_data(DATA)

    if not pairs:
        await query.message.reply_text("❌ This pack has no readable custom emojis.")
        return

    # Telegram messages have a length limit. Send the pack in safe chunks.
    # Each chunk contains the emoji + its custom emoji ID.
    chunks = []
    current_pairs = []
    current_units = 0

    for pair in pairs:
        alt, custom_id = pair
        units = utf16_len(alt) + len(custom_id) + 2

        if current_pairs and current_units + units > 3500:
            chunks.append(current_pairs)
            current_pairs = []
            current_units = 0

        current_pairs.append(pair)
        current_units += units

    if current_pairs:
        chunks.append(current_pairs)

    for i, chunk in enumerate(chunks):
        lines = []
        entities = []
        cursor = 0

        for index, (alt, custom_id) in enumerate(chunk):
            if index:
                lines.append("\n")
                cursor += 1

            offset = cursor
            length = utf16_len(alt)

            lines.append(alt)
            cursor += length

            entities.append(
                MessageEntity(
                    type=MessageEntity.CUSTOM_EMOJI,
                    offset=offset,
                    length=length,
                    custom_emoji_id=custom_id,
                )
            )

            suffix = f" {custom_id}"
            id_offset = cursor + 1
            lines.append(suffix)
            id_length = utf16_len(custom_id)
            entities.append(
                MessageEntity(
                    type=MessageEntity.CODE,
                    offset=id_offset,
                    length=id_length,
                )
            )
            cursor += utf16_len(suffix)

        header = (
            f"📦 {sticker_set.title}\n"
            f"All emojis from pack: {len(pairs)}\n\n"
            if i == 0
            else ""
        )

        text = header + "".join(lines)

        # Header changes the entity offsets, so build the entities again for
        # the first chunk with the header's UTF-16 length.
        if i == 0:
            shift = utf16_len(header)
            entities = [
                MessageEntity(
                    type=MessageEntity.CUSTOM_EMOJI,
                    offset=e.offset + shift,
                    length=e.length,
                    custom_emoji_id=e.custom_emoji_id,
                )
                for e in entities
            ]

        try:
            await query.message.reply_text(text, entities=entities)
        except Exception:
            await query.message.reply_text(
                header + "\n".join(f"{alt} {cid}" for alt, cid in chunk)
            )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 Send, type, or forward a Telegram message.\n\n"
        "I can detect:\n"
        "• Premium/Custom Emoji + Custom Emoji ID\n"
        "• Custom Emoji inside text captions\n"
        "• Custom Emoji inside forwarded photos/videos/documents with captions\n"
        "• Multiple Premium Emojis in one message\n"
        "• Normal emoji -> known Premium Emoji + ID\n\n"
        "You can also send a Custom Emoji ID directly."
    )
    await update.message.reply_text(text)


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


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message:
        return

    # 1) Direct Custom Emoji ID support.
    if message.text and ID_RE.fullmatch(message.text.strip()):
        await handle_direct_id(update, context, message.text.strip())
        return

    # 2) Read both normal text and media captions.
    text = message.text if message.text is not None else message.caption
    if not text:
        # A photo/video/document without a caption contains no Telegram
        # MessageEntity data for us to inspect.
        return

    entities = message.entities if message.text is not None else message.caption_entities

    # 3) Detect Premium/Custom Emoji entities exactly as Telegram sent them.
    custom_pairs, covered_ranges = extract_custom_entities(text, entities)

    if custom_pairs:
        custom_ids = [custom_id for _, custom_id in custom_pairs]
        resolved = await index_custom_emoji_ids(context.bot, custom_ids)

        # Prefer Telegram's actual entity alt text, because it matches the
        # forwarded message exactly.
        pairs = custom_pairs[:]

        # If an entity was unusual/missing an alt, fill from the lookup result.
        resolved_map = {custom_id: alt for alt, custom_id in resolved}
        pairs = [
            (alt or resolved_map.get(custom_id) or "🙂", custom_id)
            for alt, custom_id in pairs
        ]

        # Also process ordinary emojis in the same message.
        normal_emojis = extract_normal_emojis(text, covered_ranges)
        for normal in normal_emojis:
            for custom_id in lookup_known_premium_ids(normal):
                pairs.append((display_alt_for_id(custom_id, normal), custom_id))

        await send_emoji_id_list(update, pairs)
        return

    # 4) No Premium Emoji entity: try ordinary Unicode emoji lookup.
    normal_emojis = extract_normal_emojis(text, [])

    if not normal_emojis:
        return

    pairs = []
    missing = []

    for normal in normal_emojis:
        ids = lookup_known_premium_ids(normal)
        if ids:
            for custom_id in ids:
                pairs.append((display_alt_for_id(custom_id, normal), custom_id))
        else:
            missing.append(normal)

    # If the user configured known public custom-emoji packs, lazily index them
    # the first time an ordinary emoji is requested.
    if missing and CUSTOM_EMOJI_PACKS:
        for pack_name in CUSTOM_EMOJI_PACKS:
            if pack_name not in DATA["packs"]:
                await index_custom_emoji_pack(context.bot, pack_name)

        pairs = []
        missing = []

        for normal in normal_emojis:
            ids = lookup_known_premium_ids(normal)
            if ids:
                for custom_id in ids:
                    pairs.append((display_alt_for_id(custom_id, normal), custom_id))
            else:
                missing.append(normal)

    if pairs:
        note = None
        if missing:
            note = (
                "ℹ️ No indexed Premium Emoji was found for: "
                + " ".join(missing)
            )
        await send_emoji_id_list(update, pairs, note=note)
    else:
        await update.message.reply_text(
            "ℹ️ I detected the normal emoji, but I don't have a matching "
            "Premium Emoji indexed yet.\n\n"
            "Forward/send any Premium Emoji from the pack first. "
            "I will learn that pack automatically and then normal-emoji "
            "lookup will work from the saved index."
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
