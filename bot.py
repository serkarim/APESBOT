"""
NW Discord Bot — модуль мониторинга онлайна (sqstat + pstn.sqstat.ru).
Каждые POLL_INTERVAL_SECONDS секунд запрашивает данные с обоих сайтов sqstat
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
import unicodedata
from datetime import datetime, timedelta, timezone

import aiohttp
import aiosqlite
import discord
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont

try:
    from dotenv import load_dotenv
    load_dotenv()
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

# Основной sqstat
SQSTAT_BASE_URL = _get_env("SQSTAT_BASE_URL", required=False, default="https://breaking.proxy.sqstat.ru").rstrip("/")
CLAN_ID = _get_env("CLAN_ID", required=False, default="127")

# Второй sqstat (PSTN)
PSTN_SQSTAT_BASE_URL = _get_env("PSTN_SQSTAT_BASE_URL", required=False, default="https://pstn.sqstat.ru").rstrip("/")
PSTN_CLAN_ID = _get_env("PSTN_CLAN_ID", required=False, default="21")
PSTN_LABEL = _get_env("PSTN_LABEL", required=False, default="PSTN")

# Фильтр по тегу клана в нике игрока
CLAN_TAG_FILTER = _get_env("CLAN_TAG_FILTER", required=False, default="apes")
CLAN_TAGS = [t.strip().upper() for t in CLAN_TAG_FILTER.split(",") if t.strip()]
CLAN_DISPLAY_NAME = _get_env("CLAN_DISPLAY_NAME", required=False, default="Apes")

# Уведомления о конце засида
_seed_channel = os.environ.get("SEED_ALERT_CHANNEL_ID")
_seed_role = os.environ.get("SEED_ALERT_ROLE_ID")
SEED_ALERT_CHANNEL_ID = int(_seed_channel) if _seed_channel else None
SEED_ALERT_ROLE_ID = int(_seed_role) if _seed_role else None
SEED_MAP_KEYWORDS = ["seed", "сид"]

# ─────────────────────────────────────────────
# Система увалов (отпуск/инактив)
# ─────────────────────────────────────────────
# Канал, где висит сообщение с кнопками "Уйти в увал" / "Отменить" (для игроков)
_leave_channel = os.environ.get("LEAVE_CHANNEL_ID")
LEAVE_CHANNEL_ID = int(_leave_channel) if _leave_channel else None
# Канал, куда бот постит карточки увалов (для офицерского состава)
_officer_leave_channel = os.environ.get("OFFICER_LEAVE_CHANNEL_ID")
OFFICER_LEAVE_CHANNEL_ID = int(_officer_leave_channel) if _officer_leave_channel else None

# Файл SQLite-базы для хранения увалов. ВАЖНО: на Railway по умолчанию файловая
# система эфемерна между РЕДЕПЛОЯМИ (обычные рестарты процесса файл не трогают).
# Чтобы увалы переживали и редеплои — подключи Railway Volume и примонтируй его
# на путь из LEAVES_DB_PATH (например /data), иначе база будет пересоздаваться
# с нуля при каждом новом деплое.
LEAVES_DB_PATH = _get_env("LEAVES_DB_PATH", required=False, default="leaves.db")

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
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ─────────────────────────────────────────────
#  ГЕНЕРАЦИЯ PNG С ОНЛАЙНОМ
# ─────────────────────────────────────────────

C_BG = (15, 15, 30)
C_CARD = (22, 22, 45)
C_BORDER = (0, 180, 140)
C_HEADER_BG = (10, 10, 22)
C_WHITE = (255, 255, 255)
C_GREY = (160, 160, 180)
C_GREEN = (0, 212, 140)
C_RED_DOT = (220, 60, 60)

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
    PSTN_LABEL: PSTN_LABEL.upper(),
    f"{PSTN_LABEL} 1": f"{PSTN_LABEL.upper()} 1",
    f"{PSTN_LABEL} 2": f"{PSTN_LABEL.upper()} 2",
    f"{PSTN_LABEL} 3": f"{PSTN_LABEL.upper()} 3",
}

_FONT_DIR = os.path.dirname(os.path.abspath(__file__))
_PREFERRED_FONT = os.path.join(_FONT_DIR, "NotoSans-Cyrillic.ttf")
_SYMBOLS_FONT = os.path.join(_FONT_DIR, "NotoSansSymbols-Fallback.ttf")
_font_cache: dict[int, ImageFont.FreeTypeFont] = {}
_symbol_font_cache: dict[int, ImageFont.FreeTypeFont] = {}


def load_font(size: int):
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
    main_font = load_font(size)
    sym_font = load_symbol_font(size)
    main_has = os.path.exists(_PREFERRED_FONT)
    x, y = xy
    for ch in text:
        use_main = _font_has_glyph(_PREFERRED_FONT, ch) if main_has else True
        font = main_font if use_main else sym_font
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font)


def load_flag(team_code: str, size: int):
    variants = [f"{team_code}.png", f"{team_code.lower()}.png", f"{team_code.upper()}.png"]
    for name in variants:
        path = os.path.join(FLAGS_DIR, name)
        if os.path.exists(path):
            return Image.open(path).convert("RGBA").resize((size, size), Image.LANCZOS)
    return None


def generate_online_image(data: dict, game_state: dict | None = None) -> bytes:
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

    fn_server = load_font(15)
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
    count_text = f"{total} апездолов онлайн"
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
            clan_str = f"{count} наших"
            tot_str = f"  всего игроков {srv_online}" if srv_online else ""
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
_known_clan_names: set[str] = set()

_HOMOGLYPH_MAP = str.maketrans("АВЕКМНОРСТУХаеорсух", "ABEKMHOPCTYXaeopcyx")


def _normalize_homoglyphs(s: str) -> str:
    return s.translate(_HOMOGLYPH_MAP)


def _has_clan_tag(name: str) -> bool:
    if not CLAN_TAGS:
        return False
    norm_name = _normalize_homoglyphs(name).upper()
    return any(_normalize_homoglyphs(tag).upper() in norm_name for tag in CLAN_TAGS)


def _is_letter(ch: str) -> bool:
    return unicodedata.category(ch).startswith("L")


def _has_letter(s: str) -> bool:
    return any(_is_letter(ch) for ch in s)


def _keep_letters_digits_spaces(s: str) -> str:
    return "".join(ch for ch in s if _is_letter(ch) or ch.isdigit() or ch.isspace())


def _bare_name(name: str) -> str:
    result = _normalize_homoglyphs(name)
    for tag in CLAN_TAGS:
        result = re.sub(re.escape(_normalize_homoglyphs(tag)), "", result, flags=re.IGNORECASE)

    tokens = [t for t in result.split() if _has_letter(t)]
    result = " ".join(tokens)
    result = _keep_letters_digits_spaces(result)

    return result.strip().lower()


def _log_roster_dump(chunk_size: int = 40):
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


async def _post_with_cookie(session: aiohttp.ClientSession, url: str, data: dict, headers: dict) -> str:
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


async def _fetch_sqstat_instance(session: aiohttp.ClientSession, base_url: str, clan_id: str, default_srv_prefix: str = None) -> dict:
    """Запрашивает ростер, сервера и игроков с любого sqstat-сайта."""
    headers_clan = {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": base_url,
        "Referer": f"{base_url}/clan/{clan_id}",
        "User-Agent": "Mozilla/5.0",
    }
    headers_pub = {
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{base_url}/",
        "User-Agent": "Mozilla/5.0",
    }

    result = {"players_by_server": {}, "roster_names": set()}

    try:
        raw_clan = await _post_with_cookie(
            session, f"{base_url}/ajax/clan.php",
            {"clan_id": clan_id, "action": "list"}, headers_clan,
        )
        clan_data = json.loads(raw_clan)

        # 1. Пополнение известных ников
        for p in clan_data.get("players", []):
            if isinstance(p, dict):
                raw_name = (p.get("name") or "").strip()
                bare = _bare_name(raw_name)
                if bare:
                    result["roster_names"].add(bare)

        # 2. Получение статы серверов (карта, суммарный онлайн)
        server_info = {}
        try:
            raw_pub = await _post_with_cookie(
                session, f"{base_url}/ajax/public.php",
                {"action": "statistics"}, headers_pub,
            )
            pub_data = json.loads(raw_pub)
            for sid, sinfo in pub_data.get("servers", {}).items():
                if isinstance(sinfo, dict):
                    server_info[str(sid)] = {
                        "map": sinfo.get("map", ""),
                        "online": int(sinfo.get("online", 0) or 0),
                    }
        except Exception as e:
            log.warning(f"{base_url} public.php error: {e}")

        # 3. Парсинг текущего онлайна
        active_servers = [sid for sid, sdata in clan_data.get("servers", {}).items() if sdata and not isinstance(sdata, list)]
        multiple_servers = len(active_servers) > 1

        for srv_id, srv_data in clan_data.get("servers", {}).items():
            if not srv_data or isinstance(srv_data, list):
                continue

            # Если парсим PSTN: разделяем на PSTN 1, PSTN 2 (если их несколько)
            if default_srv_prefix:
                if multiple_servers:
                    srv_name = f"{default_srv_prefix} {srv_id}"
                else:
                    srv_name = default_srv_prefix
            else:
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

                name_bare = _bare_name(name)
                if name_bare:
                    result["roster_names"].add(name_bare)

                team_code = player_data.get("team", "")
                result["players_by_server"].setdefault(srv_name, []).append({
                    "name": name,
                    "team": team_code,
                    "cur_map": cur_map,
                    "srv_online": srv_online,
                    "bare_name": name_bare,
                })

    except Exception as e:
        log.error(f"Ошибка парсинга sqstat ({base_url}): {e}")

    return result

async def fetch_online_data() -> dict | None:
    """Парсит оба ресурса sqstat (Основной и PSTN) параллельно и объединяет выдачу."""
    grouped: dict[str, list] = {"Invasion": [], "AAS": [], "Spec Ops": [], "Custom": [], PSTN_LABEL: []}

    try:
        async with aiohttp.ClientSession() as session:
            # Запускаем одновременно оба запроса
            main_task = _fetch_sqstat_instance(session, SQSTAT_BASE_URL, CLAN_ID)
            pstn_task = _fetch_sqstat_instance(session, PSTN_SQSTAT_BASE_URL, PSTN_CLAN_ID, default_srv_prefix=PSTN_LABEL)

            main_res, pstn_res = await asyncio.gather(main_task, pstn_task)

            # Объединяем полученный ростер игроков
            _known_clan_names.update(main_res["roster_names"])
            _known_clan_names.update(pstn_res["roster_names"])
            _log_roster_dump()

            # Функция фильтрации игроков по тегам или по ростеру
            def filter_players(players_list):
                matched = []
                for p in players_list:
                    name = p["name"]
                    bare = p["bare_name"]
                    is_tagged = _has_clan_tag(name)
                    is_in_roster = bool(bare) and bare in _known_clan_names
                    if not CLAN_TAGS or is_tagged or is_in_roster:
                        matched.append(p)
                return matched

            # Формируем результаты основной группы
            for srv_name, players in main_res["players_by_server"].items():
                grouped.setdefault(srv_name, []).extend(filter_players(players))

            # Формируем результаты PSTN группы
            for srv_name, players in pstn_res["players_by_server"].items():
                grouped.setdefault(srv_name, []).extend(filter_players(players))

    except Exception as e:
        log.error(f"fetch_online_data error: {e}", exc_info=True)
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
    """Показывает все ники, которые бот сейчас считает участниками клана."""
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


# ─────────────────────────────────────────────
#  СИСТЕМА УВАЛОВ (ОТПУСК/ИНАКТИВ) — БД
# ─────────────────────────────────────────────

# Кэш активных увалов в памяти для быстрых проверок (user_id -> запись из БД).
# Источник истины — SQLite (LEAVES_DB_PATH), кэш просто зеркалит его и грузится
# заново из БД при каждом старте бота (см. on_ready), так что рестарты процесса
# ничего не теряют. Теряется только при полном редеплое БЕЗ примонтированного
# Railway Volume — см. комментарий у LEAVES_DB_PATH.
_active_leaves: dict[int, dict] = {}


async def db_init():
    async with aiosqlite.connect(LEAVES_DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS leaves (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                user_name TEXT,
                days INTEGER NOT NULL,
                reason TEXT,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                officer_message_id INTEGER,
                status TEXT NOT NULL DEFAULT 'active',
                added_by TEXT
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_leaves_user_status ON leaves(user_id, status)")
        await db.commit()
    log.info(f"БД увалов инициализирована: {LEAVES_DB_PATH}")


async def db_add_leave(user_id: int, user_name: str, days: int, reason: str,
                        officer_message_id: int | None, added_by: str = "self") -> int:
    started = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(LEAVES_DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO leaves (user_id, user_name, days, reason, started_at, officer_message_id, status, added_by) "
            "VALUES (?, ?, ?, ?, ?, ?, 'active', ?)",
            (user_id, user_name, days, reason, started, officer_message_id, added_by),
        )
        await db.commit()
        return cur.lastrowid


async def db_cancel_leave(user_id: int) -> dict | None:
    ended = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(LEAVES_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM leaves WHERE user_id = ? AND status = 'active' ORDER BY id DESC LIMIT 1", (user_id,)
        )
        row = await cur.fetchone()
        if not row:
            return None
        await db.execute("UPDATE leaves SET status = 'cancelled', ended_at = ? WHERE id = ?", (ended, row["id"]))
        await db.commit()
        return dict(row)


async def db_get_active_leaves() -> list[dict]:
    async with aiosqlite.connect(LEAVES_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM leaves WHERE status = 'active' ORDER BY started_at")
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def db_get_active_leave(user_id: int) -> dict | None:
    async with aiosqlite.connect(LEAVES_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM leaves WHERE user_id = ? AND status = 'active' ORDER BY id DESC LIMIT 1", (user_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def _load_active_leaves_cache():
    """Вызывается при старте бота — подтягивает активные увалы из БД в память."""
    _active_leaves.clear()
    for row in await db_get_active_leaves():
        _active_leaves[row["user_id"]] = row
    log.info(f"Увалы: загружено из БД активных записей — {len(_active_leaves)}")


def _leave_embed(member: discord.Member | discord.User, days: int, reason: str,
                  cancelled: bool = False, migrated: bool = False) -> discord.Embed:
    if cancelled:
        title = "✅ Увал завершён"
        color = discord.Color.green()
    elif migrated:
        title = "🌴 Увал (перенесён вручную)"
        color = discord.Color.gold()
    else:
        title = "🌴 Новый увал"
        color = discord.Color.orange()
    embed = discord.Embed(title=title, color=color, timestamp=datetime.now(timezone.utc))
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Игрок", value=member.mention, inline=True)
    embed.add_field(name="Дней в инактиве", value=str(days), inline=True)
    embed.add_field(name="Причина", value=reason or "—", inline=False)
    embed.set_footer(text=f"ID: {member.id}")
    return embed


class LeaveModal(discord.ui.Modal, title="Уйти в увал"):
    days = discord.ui.TextInput(
        label="Сколько дней в инактиве",
        placeholder="Например: 7",
        required=True,
        max_length=4,
    )
    reason = discord.ui.TextInput(
        label="Причина",
        style=discord.TextStyle.paragraph,
        placeholder="Коротко опиши причину увала",
        required=True,
        max_length=500,
    )

    async def on_submit(self, interaction: discord.Interaction):
        raw_days = self.days.value.strip()
        if not raw_days.isdigit() or not (0 < int(raw_days) <= 365):
            await interaction.response.send_message(
                "Количество дней должно быть числом от 1 до 365. Попробуй ещё раз через кнопку.",
                ephemeral=True,
            )
            return
        days_val = int(raw_days)
        reason_val = self.reason.value.strip()

        member = interaction.user
        embed = _leave_embed(member, days_val, reason_val)

        officer_msg_id = None
        if OFFICER_LEAVE_CHANNEL_ID:
            officer_channel = bot.get_channel(OFFICER_LEAVE_CHANNEL_ID)
            if officer_channel:
                try:
                    msg = await officer_channel.send(embed=embed)
                    officer_msg_id = msg.id
                except Exception as e:
                    log.error(f"Не удалось отправить карточку увала офицерам: {e}")
            else:
                log.warning(f"OFFICER_LEAVE_CHANNEL_ID={OFFICER_LEAVE_CHANNEL_ID} — канал не найден")
        else:
            log.warning("OFFICER_LEAVE_CHANNEL_ID не задан — карточка увала никуда не отправлена")

        leave_id = await db_add_leave(member.id, str(member), days_val, reason_val, officer_msg_id, added_by="self")
        _active_leaves[member.id] = {
            "id": leave_id,
            "user_id": member.id,
            "user_name": str(member),
            "days": days_val,
            "reason": reason_val,
            "officer_message_id": officer_msg_id,
            "status": "active",
        }

        await interaction.response.send_message(
            f"Увал оформлен на {days_val} дн. Хорошего отдыха! Вернёшься — жми «Отменить» на этом же сообщении.",
            ephemeral=True,
        )
        log.info(f"Увал: {member} ({member.id}) — {days_val} дн., причина: {reason_val!r}")


class LeaveView(discord.ui.View):
    """Постоянные кнопки. custom_id фиксированный, чтобы работать и после рестарта бота."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Уйти в увал", style=discord.ButtonStyle.primary, emoji="🌴", custom_id="nw_leave_go")
    async def go_leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id in _active_leaves:
            await interaction.response.send_message(
                "У тебя уже оформлен увал. Сначала нажми «Отменить», если хочешь оформить новый.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(LeaveModal())

    @discord.ui.button(label="Отменить", style=discord.ButtonStyle.secondary, emoji="↩️", custom_id="nw_leave_cancel")
    async def cancel_leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        _active_leaves.pop(interaction.user.id, None)
        leave = await db_cancel_leave(interaction.user.id)
        if not leave:
            await interaction.response.send_message("У тебя сейчас нет активного увала.", ephemeral=True)
            return

        if OFFICER_LEAVE_CHANNEL_ID and leave.get("officer_message_id"):
            officer_channel = bot.get_channel(OFFICER_LEAVE_CHANNEL_ID)
            if officer_channel:
                try:
                    msg = await officer_channel.fetch_message(leave["officer_message_id"])
                    updated = _leave_embed(interaction.user, leave["days"], leave["reason"], cancelled=True)
                    await msg.edit(embed=updated)
                except discord.NotFound:
                    pass
                except Exception as e:
                    log.warning(f"Не удалось обновить карточку увала при отмене: {e}")

        await interaction.response.send_message("Увал отменён, с возвращением! 👋", ephemeral=True)
        log.info(f"Увал отменён: {interaction.user} ({interaction.user.id})")


@bot.command(name="leavepanel")
@commands.has_permissions(manage_guild=True)
async def cmd_leave_panel(ctx: commands.Context):
    """Публикует (или переpubликует) панель с кнопками увала в LEAVE_CHANNEL_ID."""
    if not LEAVE_CHANNEL_ID:
        await ctx.send("LEAVE_CHANNEL_ID не задан в переменных окружения — некуда постить панель.")
        return
    channel = bot.get_channel(LEAVE_CHANNEL_ID)
    if not channel:
        await ctx.send(f"Канал LEAVE_CHANNEL_ID={LEAVE_CHANNEL_ID} не найден (бот туда не добавлен?).")
        return

    embed = discord.Embed(
        title="🌴 Увал",
        description=(
            "Уходишь в отпуск от игры — нажми **Уйти в увал** и заполни форму "
            "(сколько дней и причина). Офицерский состав увидит карточку с твоим ником и аватаркой.\n\n"
            "Вернулся раньше срока — жми **Отменить**."
        ),
        color=discord.Color.blurple(),
    )
    await channel.send(embed=embed, view=LeaveView())
    if channel.id != ctx.channel.id:
        await ctx.send(f"Готово, панель опубликована в {channel.mention}.")


@cmd_leave_panel.error
async def cmd_leave_panel_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("Эта команда доступна только тем, у кого есть право «Управление сервером».")
    else:
        log.error(f"cmd_leave_panel: {error}", exc_info=True)


@bot.command(name="addleave")
@commands.has_permissions(manage_guild=True)
async def cmd_add_leave(ctx: commands.Context, member: discord.Member, days: int, *, reason: str):
    """
    Миграция/ручное добавление увала офицером за игрока.
    Использование: !addleave @Игрок 14 причина текстом до конца строки
    """
    if not (0 < days <= 365):
        await ctx.send("Количество дней должно быть от 1 до 365.")
        return
    if member.id in _active_leaves:
        await ctx.send(f"У {member.mention} уже есть активный увал в системе — сначала `!removeleave` его.")
        return

    embed = _leave_embed(member, days, reason, migrated=True)
    officer_msg_id = None
    if OFFICER_LEAVE_CHANNEL_ID:
        officer_channel = bot.get_channel(OFFICER_LEAVE_CHANNEL_ID)
        if officer_channel:
            try:
                msg = await officer_channel.send(embed=embed)
                officer_msg_id = msg.id
            except Exception as e:
                log.error(f"Не удалось отправить карточку увала (миграция): {e}")

    leave_id = await db_add_leave(member.id, str(member), days, reason, officer_msg_id, added_by=str(ctx.author))
    _active_leaves[member.id] = {
        "id": leave_id,
        "user_id": member.id,
        "user_name": str(member),
        "days": days,
        "reason": reason,
        "officer_message_id": officer_msg_id,
        "status": "active",
    }
    await ctx.send(f"Увал для {member.mention} добавлен ({days} дн., перенесено вручную).")
    log.info(f"Увал добавлен вручную ({ctx.author}): {member} ({member.id}) — {days} дн., причина: {reason!r}")


@bot.command(name="removeleave")
@commands.has_permissions(manage_guild=True)
async def cmd_remove_leave(ctx: commands.Context, member: discord.Member):
    """Снимает увал с игрока за него (если сам не может/не хочет нажать кнопку)."""
    _active_leaves.pop(member.id, None)
    leave = await db_cancel_leave(member.id)
    if not leave:
        await ctx.send(f"У {member.mention} нет активного увала.")
        return

    if OFFICER_LEAVE_CHANNEL_ID and leave.get("officer_message_id"):
        officer_channel = bot.get_channel(OFFICER_LEAVE_CHANNEL_ID)
        if officer_channel:
            try:
                msg = await officer_channel.fetch_message(leave["officer_message_id"])
                updated = _leave_embed(member, leave["days"], leave["reason"], cancelled=True)
                await msg.edit(embed=updated)
            except discord.NotFound:
                pass
            except Exception as e:
                log.warning(f"Не удалось обновить карточку увала при снятии: {e}")

    await ctx.send(f"Увал для {member.mention} снят.")
    log.info(f"Увал снят вручную ({ctx.author}): {member} ({member.id})")


@bot.command(name="activeleaves")
@commands.has_permissions(manage_guild=True)
async def cmd_active_leaves(ctx: commands.Context):
    """Список всех активных увалов прямо из БД."""
    rows = await db_get_active_leaves()
    if not rows:
        await ctx.send("Активных увалов нет.")
        return
    lines = []
    for r in rows:
        started = r["started_at"][:10] if r.get("started_at") else "?"
        lines.append(f"<@{r['user_id']}> — {r['days']} дн., с {started}, причина: {r['reason']}")
    body = "\n".join(lines)
    for chunk in _chunk_text(body, 1900):
        await ctx.send(chunk)


for _cmd in (cmd_add_leave, cmd_remove_leave, cmd_active_leaves):
    @_cmd.error
    async def _leave_cmd_error(ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("Эта команда доступна только тем, у кого есть право «Управление сервером».")
        elif isinstance(error, commands.MemberNotFound):
            await ctx.send("Не нашёл такого участника — упомяни его через @.")
        elif isinstance(error, commands.BadArgument):
            await ctx.send("Проверь аргументы команды, например: `!addleave @Игрок 14 причина`.")
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send("Не хватает аргументов, например: `!addleave @Игрок 14 причина`.")
        else:
            log.error(f"leave command error: {error}", exc_info=True)


# ─────────────────────────────────────────────
#  СОБЫТИЯ БОТА
# ─────────────────────────────────────────────


@bot.event
async def on_ready():
    log.info(f"Бот запущен: {bot.user} ({bot.user.id})")
    log.info(f"Фильтр тегов клана: {CLAN_TAGS}")
    bot.add_view(LeaveView())  # чтобы кнопки увала работали и после рестарта бота
    await db_init()
    await _load_active_leaves_cache()
    if LEAVE_CHANNEL_ID and OFFICER_LEAVE_CHANNEL_ID:
        log.info(f"Увалы: канал игроков {LEAVE_CHANNEL_ID}, канал офицеров {OFFICER_LEAVE_CHANNEL_ID}")
    else:
        log.warning(
            "Увалы: не заданы LEAVE_CHANNEL_ID и/или OFFICER_LEAVE_CHANNEL_ID — "
            "система увалов не будет работать, пока обе переменные не заданы"
        )
    asyncio.create_task(start_online_loop())


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)