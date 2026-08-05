"""
NW Discord Bot — модуль мониторинга онлайна.
Каждые POLL_INTERVAL_SECONDS секунд запрашивает {API_BASE_URL}/nwst/api/online
и публикует/обновляет PNG-карточку с текущим онлайном в указанном канале.

Требования: discord.py, aiohttp, Pillow (см. requirements.txt)
Все настройки — через переменные окружения (см. .env.example).
"""

import asyncio
import io
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

import aiohttp
import discord
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont

try:
    from dotenv import load_dotenv
    load_dotenv()  # локально подхватит .env; на Railway переменные и так в окружении
except ImportError:
    pass

# ─────────────────────────────────────────────
#  КОНФИГ — берём из переменных окружения
# ─────────────────────────────────────────────


def _get_env(name: str, required: bool = True, default: str | None = None) -> str | None:
    val = os.environ.get(name, default)
    if required and not val:
        raise RuntimeError(f"Не задана обязательная переменная окружения: {name}")
    return val


DISCORD_TOKEN = _get_env("DISCORD_TOKEN")
ONLINE_CHANNEL_ID = int(_get_env("ONLINE_CHANNEL_ID"))
POLL_INTERVAL_SECONDS = int(_get_env("POLL_INTERVAL_SECONDS", required=False, default="120"))

# Прямой парсинг sqstat — никакого стороннего API больше не нужно
SQSTAT_BASE_URL = _get_env("SQSTAT_BASE_URL", required=False, default="https://breaking.proxy.sqstat.ru").rstrip("/")
CLAN_ID = _get_env("CLAN_ID", required=False, default="127")
# Фильтр по тегу клана в нике игрока (пусто = без фильтра, показывать всех)
CLAN_TAG_FILTER = _get_env("CLAN_TAG_FILTER", required=False, default="apes")
# Поддержка нескольких тегов через запятую: "❀A❀,APES" -> совпадение хотя бы с одним
CLAN_TAGS = [t.strip().upper() for t in CLAN_TAG_FILTER.split(",") if t.strip()]
# Название клана для шапки/футера карточки
CLAN_DISPLAY_NAME = _get_env("CLAN_DISPLAY_NAME", required=False, default="Apes")

# Уведомление о конце засида — опционально
_seed_channel = os.environ.get("SEED_ALERT_CHANNEL_ID")
_seed_role = os.environ.get("SEED_ALERT_ROLE_ID")
SEED_ALERT_CHANNEL_ID = int(_seed_channel) if _seed_channel else None
SEED_ALERT_ROLE_ID = int(_seed_role) if _seed_role else None
SEED_MAP_KEYWORDS = ["seed", "сид"]

# ─────────────────────────────────────────────
# Доп. сервер без sqstat (PSTN) — парсим через внутренний Next.js Server Action
# squadbrowser.app. Требует периодического обновления NEXT_ACTION при редеплое
# их сайта (см. .env.example и инструкцию в README).
# ─────────────────────────────────────────────
SQUADBROWSER_SERVER_ID = _get_env("SQUADBROWSER_SERVER_ID", required=False, default="")
SQUADBROWSER_NEXT_ACTION = _get_env("SQUADBROWSER_NEXT_ACTION", required=False, default="")
SQUADBROWSER_PAGE_URL = _get_env("SQUADBROWSER_PAGE_URL", required=False, default="https://squadbrowser.app/")
SQUADBROWSER_LABEL = _get_env("SQUADBROWSER_LABEL", required=False, default="PSTN")
# Обычно не нужно — action на squadbrowser отвечает и без сессии.
# Если бот получает 401/403, скопируй заголовок Cookie из DevTools и вставь сюда как есть.
SQUADBROWSER_COOKIE = _get_env("SQUADBROWSER_COOKIE", required=False, default="")
SQUADBROWSER_ENABLED = bool(SQUADBROWSER_SERVER_ID and SQUADBROWSER_NEXT_ACTION)

# Папка с флагами фракций: flags/afu.png, flags/rgf.png ...
FLAGS_DIR = "flags"

# ─────────────────────────────────────────────
#  ЛОГИРОВАНИЕ
# ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("nw-online-bot")

# ─────────────────────────────────────────────
#  БОТ
# ─────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True  # нужно для команд !roster / !pstndebug
bot = commands.Bot(command_prefix="!", intents=intents)

# ─────────────────────────────────────────────
#  ГЕНЕРАЦИЯ PNG С ОНЛАЙНОМ
# ─────────────────────────────────────────────

# Цвета
C_BG = (15, 15, 30)
C_CARD = (22, 22, 45)
C_BORDER = (0, 180, 140)
C_HEADER_BG = (10, 10, 22)
C_WHITE = (255, 255, 255)
C_GREY = (160, 160, 180)
C_GREEN = (0, 212, 140)
C_RED_DOT = (220, 60, 60)

# Размеры
IMG_W = 700
PAD = 24
ROW_H = 36
CARD_PAD = 14
HEADER_H = 64
FOOTER_H = 28
FLAG_SIZE = 22

SERVER_LABELS = {
    "Invasion": "INVASION",
    "AAS": "МИКС",
    "Spec Ops": "SPEC OPS",
    "Custom": "CUSTOM",
    SQUADBROWSER_LABEL: SQUADBROWSER_LABEL.upper(),
}


_FONT_DIR = os.path.dirname(os.path.abspath(__file__))
_PREFERRED_FONT = os.path.join(_FONT_DIR, "NotoSans-Cyrillic.ttf")
_SYMBOLS_FONT = os.path.join(_FONT_DIR, "NotoSansSymbols-Fallback.ttf")
_font_cache: dict[int, ImageFont.FreeTypeFont] = {}
_symbol_font_cache: dict[int, ImageFont.FreeTypeFont] = {}


def load_font(size: int):
    """
    Шрифт с поддержкой кириллицы. Приоритет:
    1. Забандленный NotoSans-Cyrillic.ttf рядом с bot.py (работает в любом окружении, включая Railway)
    2. Любой другой .ttf рядом с bot.py
    3. Системные шрифты (если вдруг есть)
    4. Дефолтный PIL-шрифт (НЕ поддерживает кириллицу — крайний случай)
    """
    if size in _font_cache:
        return _font_cache[size]

    font = None
    if os.path.exists(_PREFERRED_FONT):
        try:
            font = ImageFont.truetype(_PREFERRED_FONT, size)
        except Exception as e:
            log.warning(f"Не удалось загрузить {_PREFERRED_FONT}: {e}")

    if font is None:
        try:
            for f in os.listdir(_FONT_DIR):
                if f.endswith(".ttf") and f != os.path.basename(_SYMBOLS_FONT):
                    font = ImageFont.truetype(os.path.join(_FONT_DIR, f), size)
                    break
        except Exception:
            pass

    if font is None:
        for path in [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        ]:
            if os.path.exists(path):
                try:
                    font = ImageFont.truetype(path, size)
                    break
                except Exception:
                    pass

    if font is None:
        log.warning("Кириллический шрифт не найден — текст на русском будет отображаться квадратиками!")
        font = ImageFont.load_default()

    _font_cache[size] = font
    return font


def load_symbol_font(size: int):
    """
    Запасной шрифт для символов/дингбатов, которых нет в основном шрифте
    (например ❀ и подобные украшения в никах/тегах кланов).
    """
    if size in _symbol_font_cache:
        return _symbol_font_cache[size]
    font = None
    if os.path.exists(_SYMBOLS_FONT):
        try:
            font = ImageFont.truetype(_SYMBOLS_FONT, size)
        except Exception as e:
            log.warning(f"Не удалось загрузить {_SYMBOLS_FONT}: {e}")
    if font is None:
        font = load_font(size)
    _symbol_font_cache[size] = font
    return font


_cmap_cache: dict[str, set] = {}


def _get_font_cmap(font_path: str) -> set:
    """Множество кодпоинтов, реально поддерживаемых шрифтом (через таблицу cmap)."""
    if font_path in _cmap_cache:
        return _cmap_cache[font_path]
    codepoints: set = set()
    try:
        from fontTools.ttLib import TTFont as _TTFont
        tt = _TTFont(font_path, lazy=True)
        codepoints = set(tt.getBestCmap().keys())
    except Exception as e:
        log.warning(f"Не удалось прочитать cmap {font_path}: {e}")
    _cmap_cache[font_path] = codepoints
    return codepoints


def _font_has_glyph(font_path: str, ch: str) -> bool:
    if ch.isspace():
        return True
    return ord(ch) in _get_font_cmap(font_path)


def draw_text_mixed(draw: ImageDraw.ImageDraw, xy: tuple, text: str, size: int, fill, anchor=None):
    """
    Рисует текст, автоматически переключаясь на запасной шрифт символов
    для символов, отсутствующих в основном (кириллическом) шрифте.
    Не поддерживает произвольные anchor-режимы PIL — только левый верхний угол по x,y.
    """
    main_font = load_font(size)
    sym_font = load_symbol_font(size)
    main_has = os.path.exists(_PREFERRED_FONT)
    x, y = xy
    for ch in text:
        use_main = _font_has_glyph(_PREFERRED_FONT, ch) if main_has else True
        font = main_font if use_main else sym_font
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font)


def text_length_mixed(draw: ImageDraw.ImageDraw, text: str, size: int) -> float:
    main_font = load_font(size)
    sym_font = load_symbol_font(size)
    main_has = os.path.exists(_PREFERRED_FONT)
    total = 0.0
    for ch in text:
        use_main = _font_has_glyph(_PREFERRED_FONT, ch) if main_has else True
        font = main_font if use_main else sym_font
        total += draw.textlength(ch, font=font)
    return total


def load_flag(team_code: str, size: int):
    variants = [f"{team_code}.png", f"{team_code.lower()}.png", f"{team_code.upper()}.png"]
    for name in variants:
        path = os.path.join(FLAGS_DIR, name)
        if os.path.exists(path):
            return Image.open(path).convert("RGBA").resize((size, size), Image.LANCZOS)
    return None


def generate_online_image(data: dict, game_state: dict | None = None) -> bytes:
    """Генерирует PNG-карточку с онлайном клана."""
    total = data.get("total_online", 0)
    servers = data.get("servers", {})
    now_msk = (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%H:%M МСК")
    game_state = game_state or {}

    active_servers = []
    for key, label in SERVER_LABELS.items():
        srv = servers.get(key, {})
        players = srv.get("players", []) if isinstance(srv, dict) else []
        by_team = srv.get("by_team", {}) if isinstance(srv, dict) else {}
        cur_map = players[0].get("cur_map", "") if players and isinstance(players[0], dict) else ""
        srv_online = players[0].get("srv_online", 0) if players and isinstance(players[0], dict) else 0
        if players:
            active_servers.append((key, label, players, by_team, cur_map, srv_online))

    total_players = sum(len(p) for _, _, p, _, _, _ in active_servers)
    cards_count = len(active_servers) or 1
    if not active_servers:
        total_players = 1

    map_game_extra = sum(
        (18 if cur_map else 0) + (22 if game_state.get(key) else 0)
        for key, _, _, _, cur_map, _ in active_servers
    )

    img_h = (
        HEADER_H + PAD
        + cards_count * (CARD_PAD * 2 + 28)
        + total_players * ROW_H
        + map_game_extra
        + cards_count * PAD
        + FOOTER_H + PAD
    )

    img = Image.new("RGB", (IMG_W, img_h), C_BG)
    draw = ImageDraw.Draw(img)

    fn_title = load_font(18)
    fn_server = load_font(15)
    fn_player = load_font(14)
    fn_small = load_font(11)
    fn_count = load_font(13)

    # ── Шапка ──────────────────────────────────────
    draw.rectangle([0, 0, IMG_W, HEADER_H], fill=C_HEADER_BG)
    draw.rectangle([0, 0, 4, HEADER_H], fill=C_BORDER)
    draw_text_mixed(draw, (PAD, 10), CLAN_DISPLAY_NAME, 18, C_BORDER)
    draw_text_mixed(draw, (PAD, 34), f"В ИГРЕ // {CLAN_DISPLAY_NAME.upper()}", 11, C_GREY)

    dot_x = IMG_W - PAD - 8
    dot_y = 20
    draw.ellipse([dot_x - 6, dot_y - 6, dot_x + 6, dot_y + 6], fill=C_GREEN if total > 0 else C_RED_DOT)
    count_text = f"{total} бойцов онлайн"
    bbox = draw.textbbox((0, 0), count_text, font=fn_count)
    tw = bbox[2] - bbox[0]
    draw.text((dot_x - 12 - tw, dot_y - 8), count_text, font=fn_count, fill=C_GREEN if total > 0 else C_GREY)
    draw.text((IMG_W - PAD - draw.textlength(now_msk, font=fn_small), 40), now_msk, font=fn_small, fill=C_GREY)

    # ── Карточки серверов ──────────────────────────
    y = HEADER_H + PAD

    if not active_servers:
        draw.text((PAD, y + 10), "Никого нет в игре", font=fn_server, fill=C_GREY)
    else:
        for key, label, players, by_team, cur_map, srv_online in active_servers:
            count = len(players)
            game_extra = 22 if game_state.get(key) else 0
            map_extra = 18 if cur_map else 0
            card_h = CARD_PAD * 2 + 28 + count * ROW_H + game_extra + map_extra

            draw.rectangle([PAD, y, IMG_W - PAD, y + card_h], fill=C_CARD)
            draw.rectangle([PAD, y, PAD + 3, y + card_h], fill=C_BORDER)

            draw.text((PAD + CARD_PAD, y + CARD_PAD), label, font=fn_server, fill=C_WHITE)
            clan_str = f"{count} клан"
            tot_str = f"  👥 {srv_online}" if srv_online else ""
            cnt_str = clan_str + tot_str
            draw.text(
                (IMG_W - PAD - CARD_PAD - draw.textlength(cnt_str, font=fn_small), y + CARD_PAD + 3),
                cnt_str, font=fn_small, fill=C_BORDER,
            )

            line_y = y + CARD_PAD + 26
            draw.line([PAD + CARD_PAD, line_y, IMG_W - PAD - CARD_PAD, line_y], fill=(40, 40, 70), width=1)

            row_y = line_y + 6

            if cur_map:
                draw.text((PAD + CARD_PAD, row_y), f"🗺  {cur_map}", font=fn_small, fill=C_GREY)
                row_y += 18

            state = game_state.get(key)
            if state and state.get("since"):
                now = datetime.now(timezone.utc) + timedelta(hours=3)
                elapsed_min = int((now - state["since"]).total_seconds() // 60)
                h, m = divmod(elapsed_min, 60)
                elapsed_str = f"{h}ч {m}м" if h else f"{m}м"
                teams_str = " vs ".join(sorted(state["teams"]))
                draw.text(
                    (PAD + CARD_PAD, row_y),
                    f"⏱  Игра идёт {elapsed_str}  ·  {teams_str}",
                    font=fn_small, fill=C_BORDER,
                )
                row_y += 22

            if by_team:
                for team, names in by_team.items():
                    flag_img = load_flag(team, FLAG_SIZE)
                    for entry in names:
                        name = entry.get("name", "") if isinstance(entry, dict) else str(entry)
                        text_x = PAD + CARD_PAD + FLAG_SIZE + 10
                        if flag_img:
                            img.paste(flag_img, (PAD + CARD_PAD, row_y + (ROW_H - FLAG_SIZE) // 2), flag_img)
                        else:
                            draw.text((PAD + CARD_PAD, row_y + 8), team[:3].upper(), font=fn_small, fill=C_BORDER)
                            text_x = PAD + CARD_PAD + 36
                        draw_text_mixed(draw, (text_x, row_y + 9), name, 14, C_WHITE)
                        row_y += ROW_H
            else:
                for p in players:
                    name = p.get("name", "") if isinstance(p, dict) else str(p)
                    draw_text_mixed(draw, (PAD + CARD_PAD + 10, row_y + 9), f"› {name}", 14, C_WHITE)
                    row_y += ROW_H

            y += card_h + PAD

    # ── Футер ──────────────────────────────────────
    footer_y = img_h - FOOTER_H
    draw.line([0, footer_y, IMG_W, footer_y], fill=(30, 30, 55), width=1)
    draw_text_mixed(draw, (PAD, footer_y + 8), f"{CLAN_DISPLAY_NAME} Tracker", 11, C_GREY)
    draw.text(
        (IMG_W - PAD - draw.textlength("made by stl", font=fn_small), footer_y + 8),
        "made by stl", font=fn_small, fill=(80, 80, 100),
    )

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf.read()


# ─────────────────────────────────────────────
#  ОПРОС API И ПУБЛИКАЦИЯ
# ─────────────────────────────────────────────

_online_message_id: dict[int, int] = {}
_online_loop_started = False
_game_state: dict[str, dict] = {}
_map_state: dict[str, str] = {}
# Копится по мере опросов sqstat: "голые" ники (без тега клана, в нижнем регистре)
# всех когда-либо увиденных участников клана. Используется, чтобы узнавать своих
# на сервере без sqstat (PSTN) — там нет тега, но ник обычно тот же самый.
# ВАЖНО: sqstat не отдаёт настоящий SteamID64 в clan.php (там просто порядковый
# номер слота вида "54"), поэтому матчинг идёт по нику, а не по ID.
_known_clan_names: set[str] = set()

# Кириллица и латиница часто визуально неотличимы (А/A, Е/E, Р/P и т.д.) — люди
# copy-paste'ят тег откуда попало, и в никах гуляют вперемешку разные варианты
# одного и того же тега. Нормализуем такие "омоглифы" к латинице ПЕРЕД сравнением,
# чтобы не пришлось вручную перечислять каждую комбинацию в CLAN_TAG_FILTER.
_HOMOGLYPH_MAP = str.maketrans(
    "АВЕКМНОРСТУХаеорсух",
    "ABEKMHOPCTYXaeopcyx",
)
# Декоративные "цветочки", которые часто оборачивают теги — если в CLAN_TAG_FILTER
# перечислен не тот вариант символа, всё равно срежем его как обрамляющий мусор.
_DECOR_CHARS = "❀✿✾✽✼✻✺✹✸❁❃❋"


def _normalize_homoglyphs(s: str) -> str:
    return s.translate(_HOMOGLYPH_MAP)


def _has_clan_tag(name: str) -> bool:
    """Проверяет, есть ли в нике буквально один из CLAN_TAGS — с учётом омоглифов (А/A и т.п.)."""
    if not CLAN_TAGS:
        return False
    norm_name = _normalize_homoglyphs(name).upper()
    return any(_normalize_homoglyphs(tag).upper() in norm_name for tag in CLAN_TAGS)


def _bare_name(name: str) -> str:
    """
    Убирает тег клана и обрамляющий мусор (скобки/разделители/цветочки-декорации),
    чтобы сравнивать чистые ники. Кириллица/латиница нормализуется, так что
    '✿A✿ stl' и '✿А✿ stl' (латинская и кириллическая A) дадут один результат.
    """
    result = _normalize_homoglyphs(name)
    for tag in CLAN_TAGS:
        result = re.sub(re.escape(_normalize_homoglyphs(tag)), "", result, flags=re.IGNORECASE)
    result = result.strip(" \t\r\n-_|[](){}:·•" + _DECOR_CHARS)
    return result.strip().lower()


def _log_roster_dump(chunk_size: int = 40):
    """Пишет в лог весь текущий список известных ников клана — чтобы не лезть за этим в Discord-команду."""
    if not _known_clan_names:
        log.info("sqstat: ростер пуст — известных ников клана пока нет")
        return
    names = sorted(_known_clan_names)
    for i in range(0, len(names), chunk_size):
        part = names[i:i + chunk_size]
        log.info(f"sqstat ростер [{i + 1}-{i + len(part)}/{len(names)}]: {', '.join(part)}")


SQSTAT_SERVER_MAP = {
    "7": "Invasion",
    "1": "AAS",
    "6": "Spec Ops",
    "9": "Custom",
    "10": "Custom",
    "11": "Custom",
}

_HEADERS_CLAN = {
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": SQSTAT_BASE_URL,
    "Referer": f"{SQSTAT_BASE_URL}/clan/{CLAN_ID}",
    "User-Agent": "Mozilla/5.0",
}
_HEADERS_PUB = {
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": f"{SQSTAT_BASE_URL}/",
    "User-Agent": "Mozilla/5.0",
}


async def _post_with_cookie(session: aiohttp.ClientSession, url: str, data: dict, headers: dict) -> str:
    """sqstat иногда отдаёт JS-редирект с PHPSESID вместо JSON — повторяем запрос с куки."""
    async with session.post(url, data=data, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as r:
        raw = await r.text()
    if "PHPSESID=" in raw and "document.cookie" in raw:
        m = re.search(r"PHPSESID=([^ ;]+)", raw)
        if m:
            async with session.post(
                url, data=data, headers=headers,
                cookies={"PHPSESID": m.group(1)},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r2:
                raw = await r2.text()
    return raw


def _fix_mojibake(s: str) -> str:
    """
    squadbrowser иногда отдаёт кириллицу/эмодзи как UTF-8-байты, ошибочно
    прочитанные как Windows-1252 (например 'ÐŸÐµÑ€Ð²Ñ‹Ð¹' вместо 'Первый').
    Пытаемся откатить это обратно; если не получается — отдаём как есть
    (единичные ники всё равно могут остаться повреждёнными — это баг на их стороне).
    """
    if not s:
        return s
    try:
        fixed = s.encode("cp1252").decode("utf-8")
        if fixed.count("Ð") < s.count("Ð") or fixed != s:
            return fixed
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    return s


async def fetch_squadbrowser_live() -> dict | None:
    """
    Забирает live-данные сервера без sqstat (PSTN) через внутренний Next.js
    Server Action сайта squadbrowser.app. Это не публичный API — Next-Action
    хэш зашит в конкретную сборку их сайта и может измениться при редеплое.
    Если бот вдруг перестал видеть этот сервер — скорее всего именно это,
    нужно достать новый хэш из DevTools (см. .env.example) и обновить
    SQUADBROWSER_NEXT_ACTION.
    """
    headers = {
        "Content-Type": "text/plain;charset=UTF-8",
        "Accept": "text/x-component",
        "Next-Action": SQUADBROWSER_NEXT_ACTION,
        "Next-Router-State-Tree": '["",{"children":["__PAGE__",{},null,null]},null,null,true]',
        "Origin": "https://squadbrowser.app",
        "Referer": SQUADBROWSER_PAGE_URL,
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
        ),
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }
    if SQUADBROWSER_COOKIE:
        headers["Cookie"] = SQUADBROWSER_COOKIE

    payload = json.dumps([SQUADBROWSER_SERVER_ID])

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                SQUADBROWSER_PAGE_URL, data=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                raw = await resp.text()
                if resp.status != 200:
                    log.warning(
                        f"fetch_squadbrowser_live: HTTP {resp.status} — возможно, устарел "
                        f"Next-Action хэш или нужны куки (SQUADBROWSER_COOKIE). Ответ: {raw[:200]!r}"
                    )
    except Exception as e:
        log.error(f"fetch_squadbrowser_live: сетевая ошибка: {e}")
        return None

    # Ответ — построчный RSC-стрим Next.js: "0:{...}\n1:{...ok...}\n"
    payload_json = None
    for line in raw.splitlines():
        if ":" not in line:
            continue
        _, _, rest = line.partition(":")
        rest = rest.strip()
        if not rest.startswith('{"ok"'):
            continue
        try:
            payload_json = json.loads(rest)
            break
        except json.JSONDecodeError:
            continue

    if not payload_json or not payload_json.get("ok"):
        log.warning(
            f"fetch_squadbrowser_live: неожиданный формат ответа, возможно устарел "
            f"Next-Action хэш (первые 200 симв.): {raw[:200]!r}"
        )
        return None

    live = (payload_json.get("data") or {}).get("live") or {}
    if not live:
        return None  # сервер офлайн или нет данных

    cur_map = live.get("current_map", "") or ""
    srv_online = live.get("current_players", 0) or 0

    players = []
    for p in live.get("players", []):
        name = _fix_mojibake((p.get("display_name") or "").strip())
        if not name:
            continue
        steam_id = str(p.get("steam_id", "") or "")
        team = (p.get("team") or "").strip() or "Unknown"
        players.append({
            "name": name,
            "team": team,
            "cur_map": cur_map,
            "srv_online": srv_online,
            "steam_id": steam_id,
        })

    return {"cur_map": cur_map, "srv_online": srv_online, "players": players}


async def fetch_online_data() -> dict | None:
    """
    Парсит онлайн клана напрямую с sqstat (без стороннего сервера):
    1. ajax/clan.php action=list — игроки клана по серверам
    2. ajax/public.php action=statistics — текущая карта и онлайн каждого сервера
    Возвращает структуру в формате, ожидаемом generate_online_image().
    """
    grouped: dict[str, list] = {"Invasion": [], "AAS": [], "Spec Ops": [], "Custom": []}

    try:
        async with aiohttp.ClientSession() as session:
            raw_clan = await _post_with_cookie(
                session, f"{SQSTAT_BASE_URL}/ajax/clan.php",
                {"clan_id": CLAN_ID, "action": "list"}, _HEADERS_CLAN,
            )
            clan_data = json.loads(raw_clan)

            # Полный ростер клана (панелька) — все участники, независимо от того,
            # онлайн они сейчас или нет. Отдельное поле в том же ответе clan.php,
            # не путать с data["servers"] (там только те, кто сейчас в игре).
            roster_total = 0
            for p in clan_data.get("players", []):
                if not isinstance(p, dict):
                    continue
                raw_name = (p.get("name") or "").strip()
                if not raw_name:
                    continue
                bare = _bare_name(raw_name)
                if bare:
                    _known_clan_names.add(bare)
                    roster_total += 1

            if roster_total == 0:
                log.warning(
                    f"sqstat: clan_data['players'] пуст или отсутствует (ключи в ответе: "
                    f"{list(clan_data.keys())}) — полный ростер не загружен, "
                    f"известные ники будут копиться только из тех, кто сейчас в игре"
                )
            log.info(f"sqstat: полный ростер клана — {roster_total} ников, известно всего: {len(_known_clan_names)}")
            _log_roster_dump()

            server_info: dict[str, dict] = {}
            try:
                raw_pub = await _post_with_cookie(
                    session, f"{SQSTAT_BASE_URL}/ajax/public.php",
                    {"action": "statistics"}, _HEADERS_PUB,
                )
                pub_data = json.loads(raw_pub)
                for sid, sinfo in pub_data.get("servers", {}).items():
                    if isinstance(sinfo, dict):
                        server_info[str(sid)] = {
                            "map": sinfo.get("map", ""),
                            "online": int(sinfo.get("online", 0) or 0),
                        }
            except Exception as e:
                log.warning(f"public.php (карты/онлайн серверов): {e}")

            raw_total = 0
            matched_total = 0
            seen_team_codes: set[str] = set()

            for srv_id, srv_data in clan_data.get("servers", {}).items():
                if not srv_data or isinstance(srv_data, list):
                    continue

                srv_name = SQSTAT_SERVER_MAP.get(str(srv_id), "Custom")
                info = server_info.get(str(srv_id), {})
                cur_map = info.get("map", "")
                srv_online = info.get("online", 0)

                for player_id, player_data in srv_data.items():
                    if not isinstance(player_data, dict):
                        continue
                    name = player_data.get("name", "").strip()
                    if not name or len(name) >= 50:
                        continue
                    raw_total += 1
                    name_bare = _bare_name(name)
                    is_tagged = _has_clan_tag(name)
                    is_in_roster = bool(name_bare) and name_bare in _known_clan_names
                    if CLAN_TAGS and not (is_tagged or is_in_roster):
                        continue
                    matched_total += 1
                    team_code = player_data.get("team", "")
                    if team_code:
                        seen_team_codes.add(str(team_code))

                    bare = name_bare
                    if bare:
                        _known_clan_names.add(bare)

                    grouped.setdefault(srv_name, []).append({
                        "name": name,
                        "team": team_code,
                        "cur_map": cur_map,
                        "srv_online": srv_online,
                        "bare_name": bare,
                    })

            log.info(
                f"sqstat: всего игроков на серверах {raw_total}, прошло фильтр тегов {matched_total}, "
                f"коды фракций в текущей выдаче: {sorted(seen_team_codes)}, "
                f"известно ников клана всего: {len(_known_clan_names)}"
            )

            if SQUADBROWSER_ENABLED:
                try:
                    pstn = await fetch_squadbrowser_live()
                    if pstn:
                        matched_pstn = []
                        for p in pstn["players"]:
                            p_bare = _bare_name(p["name"])
                            is_known = bool(p_bare) and p_bare in _known_clan_names
                            is_tagged = _has_clan_tag(p["name"])
                            if is_known or is_tagged:
                                matched_pstn.append(p)
                        log.info(
                            f"{SQUADBROWSER_LABEL} (squadbrowser): игроков на сервере {len(pstn['players'])}, "
                            f"опознано как свои {len(matched_pstn)} (известно ников клана: {len(_known_clan_names)})"
                        )
                        grouped[SQUADBROWSER_LABEL] = matched_pstn
                except Exception as e:
                    log.error(f"squadbrowser: {e}", exc_info=True)

    except Exception as e:
        log.error(f"fetch_online_data (sqstat scrape): {e}", exc_info=True)
        return None

    total_online = sum(len(v) for v in grouped.values())

    servers_with_teams = {}
    for server, players in grouped.items():
        by_team: dict = {}
        for p in players:
            team = p.get("team", "Unknown") or "Unknown"
            by_team.setdefault(team, []).append(p)
        servers_with_teams[server] = {"players": players, "by_team": by_team}

    return {"status": "success", "total_online": total_online, "servers": servers_with_teams}


def is_seed_map(map_name: str) -> bool:
    return any(kw in map_name.lower() for kw in SEED_MAP_KEYWORDS)


def detect_game_changes(data: dict) -> list[str]:
    """Обновляет _game_state/_map_state, возвращает список серверов где засид закончился."""
    now = datetime.now(timezone.utc) + timedelta(hours=3)
    servers = data.get("servers", {})
    seed_ended: list[str] = []

    for srv_key in SERVER_LABELS:
        srv = servers.get(srv_key, {})
        players = srv.get("players", []) if isinstance(srv, dict) else []
        cur_map = players[0].get("cur_map", "") if players and isinstance(players[0], dict) else ""

        prev_map = _map_state.get(srv_key, "")
        if prev_map and cur_map and prev_map != cur_map:
            if is_seed_map(prev_map) and not is_seed_map(cur_map):
                log.info(f"Засид закончился на {srv_key}: {prev_map!r} -> {cur_map!r}")
                seed_ended.append(srv_key)
        if cur_map:
            _map_state[srv_key] = cur_map

        current_teams = frozenset(
            p.get("team", "").strip() for p in players if isinstance(p, dict) and p.get("team", "").strip()
        )

        if not current_teams:
            _game_state.pop(srv_key, None)
            continue

        prev = _game_state.get(srv_key)
        if prev is None:
            _game_state[srv_key] = {"teams": current_teams, "since": now}
        elif current_teams != prev["teams"]:
            log.info(f"Новая игра на {srv_key}: {set(prev['teams'])} -> {set(current_teams)}")
            _game_state[srv_key] = {"teams": current_teams, "since": now}

    return seed_ended


async def _send_seed_alert(seed_ended: list[str]):
    if not SEED_ALERT_CHANNEL_ID:
        return
    channel = bot.get_channel(SEED_ALERT_CHANNEL_ID)
    if not channel:
        log.warning(f"_send_seed_alert: канал {SEED_ALERT_CHANNEL_ID} не найден")
        return
    role_mention = f"<@&{SEED_ALERT_ROLE_ID}>" if SEED_ALERT_ROLE_ID else ""
    if SEED_ALERT_ROLE_ID and channel.guild:
        role = channel.guild.get_role(SEED_ALERT_ROLE_ID)
        if role:
            role_mention = role.mention
    await channel.send(f"{role_mention} сервер засидился, заходите <3".strip())
    log.info(f"Seed alert отправлен ({seed_ended})")


async def post_or_edit_online(channel: discord.TextChannel):
    data = await fetch_online_data()
    if data is None:
        log.warning("post_or_edit_online: нет данных с апишки")
        return

    seed_ended = detect_game_changes(data)
    if seed_ended and SEED_ALERT_CHANNEL_ID:
        asyncio.create_task(_send_seed_alert(seed_ended))

    try:
        img_bytes = generate_online_image(data, _game_state)
    except Exception as e:
        log.error(f"generate_online_image: {e}", exc_info=True)
        return

    ch_id = channel.id

    if ch_id in _online_message_id:
        try:
            old_msg = await channel.fetch_message(_online_message_id[ch_id])
            await old_msg.delete()
        except discord.NotFound:
            pass
        except Exception as e:
            log.warning(f"Не удалось удалить старое сообщение: {e}")
        del _online_message_id[ch_id]

    file = discord.File(io.BytesIO(img_bytes), filename="online.png")
    msg = await channel.send(file=file)
    _online_message_id[ch_id] = msg.id
    log.info(f"Онлайн опубликован в #{channel.name}")


async def start_online_loop():
    global _online_loop_started
    if _online_loop_started:
        log.warning("start_online_loop: уже запущен, пропускаем")
        return
    _online_loop_started = True

    await bot.wait_until_ready()
    log.info(f"Онлайн-трекер запущен, канал {ONLINE_CHANNEL_ID}, интервал {POLL_INTERVAL_SECONDS}с")

    while True:
        try:
            channel = bot.get_channel(ONLINE_CHANNEL_ID)
            if channel:
                await post_or_edit_online(channel)
            else:
                log.warning(f"Онлайн-канал {ONLINE_CHANNEL_ID} не найден")
        except Exception as e:
            log.error(f"Ошибка онлайн-цикла: {e}", exc_info=True)
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


# ─────────────────────────────────────────────
#  ОТЛАДОЧНЫЕ КОМАНДЫ
# ─────────────────────────────────────────────


def _chunk_text(text: str, limit: int = 1900):
    """Режет длинный текст на куски, безопасные для лимита сообщений Discord (2000 симв.)."""
    lines = text.split("\n")
    chunks, cur = [], ""
    for line in lines:
        if len(cur) + len(line) + 1 > limit:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur:
        chunks.append(cur)
    return chunks


@bot.command(name="roster")
async def cmd_roster(ctx: commands.Context):
    """Показывает все ники, которые бот сейчас считает участниками клана (после снятия тега)."""
    if not _known_clan_names:
        await ctx.send("Пока не знаю ни одного ника клана — подожди следующего опроса sqstat.")
        return
    names = sorted(_known_clan_names)
    body = "\n".join(names)
    header = f"Известно ников клана: {len(names)}\n```\n"
    footer = "\n```"
    for i, chunk in enumerate(_chunk_text(body, 1900 - len(header) - len(footer))):
        text = f"{header}{chunk}{footer}" if i == 0 else f"```\n{chunk}\n```"
        await ctx.send(text)


@bot.command(name="pstndebug")
async def cmd_pstn_debug(ctx: commands.Context):
    """Свежий запрос к squadbrowser + построчный разбор: кто опознан своим, кто нет и почему."""
    if not SQUADBROWSER_ENABLED:
        await ctx.send("PSTN/squadbrowser не настроен (нет SQUADBROWSER_SERVER_ID / SQUADBROWSER_NEXT_ACTION).")
        return

    await ctx.send("Опрашиваю squadbrowser...")
    pstn = await fetch_squadbrowser_live()
    if not pstn:
        await ctx.send("Не удалось получить данные с squadbrowser (см. логи Railway — скорее всего протух Next-Action).")
        return

    lines = [f"Карта: {pstn['cur_map']}  Онлайн: {pstn['srv_online']}  Игроков в списке: {len(pstn['players'])}", ""]
    for p in pstn["players"]:
        p_bare = _bare_name(p["name"])
        is_known = bool(p_bare) and p_bare in _known_clan_names
        is_tagged = _has_clan_tag(p["name"])
        matched = is_known or is_tagged
        mark = "✅" if matched else "  "
        reason = "roster" if is_known else ("tag" if is_tagged else "-")
        lines.append(f"{mark} {p['name']!r:30} -> bare={p_bare!r:20} matched={matched} ({reason})")

    body = "\n".join(lines)
    for chunk in _chunk_text(body, 1900):
        await ctx.send(f"```\n{chunk}\n```")


# ─────────────────────────────────────────────
#  СОБЫТИЯ БОТА
# ─────────────────────────────────────────────


@bot.event
async def on_ready():
    log.info(f"Бот запущен: {bot.user} ({bot.user.id})")
    log.info(f"Фильтр тегов клана: {CLAN_TAGS}")
    asyncio.create_task(start_online_loop())


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)