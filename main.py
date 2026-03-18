from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional
from urllib import parse, request

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from yt_dlp import YoutubeDL


logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
LOGGER = logging.getLogger("musicbot")


CALLBACK_PICK = "pick"
CALLBACK_NAV = "nav"
CALLBACK_NEW_SEARCH = "open_search"
USER_SESSIONS_KEY = "search_sessions"
CHAT_SESSIONS_KEY = "search_sessions"
ACTIVE_SESSION_ID_KEY = "active_session_id"
ACTIVE_PAGE_KEY = "active_page"
BTN_PREV_PAGE = "⬅️ Назад"
BTN_NEXT_PAGE = "➡️ Вперед"
BTN_NEW_SEARCH = "🔎 Новый поиск"


@dataclass(frozen=True)
class SearchSource:
    name: str
    prefix: str
    limit: int


@dataclass(frozen=True)
class AppConfig:
    download_dir: Path = Path("downloads")
    page_size: int = 5
    search_limit: int = 24
    search_source_timeout_seconds: int = 7
    search_cache_ttl_seconds: int = 15 * 60
    download_cache_ttl_seconds: int = 60 * 60
    max_search_cache_items: int = 120
    max_download_cache_items: int = 40
    stale_download_file_age_seconds: int = 12 * 60 * 60
    main_search_button: str = "🔎 Поиск музыки"
    non_music_hints: tuple[str, ...] = ("track analysis", "osu!", "gameplay", "reaction")
    search_sources: tuple[SearchSource, ...] = (
        SearchSource(name="YouTube", prefix="ytsearch", limit=16),
        SearchSource(name="SoundCloud", prefix="scsearch", limit=8),
    )


@dataclass(frozen=True)
class EnvConfig:
    telegram_token: str
    genius_token: Optional[str]
    cookies_from_browser: Optional[str]
    cookies_file: Optional[Path]


@dataclass(frozen=True)
class TrackOption:
    title: str
    artist: str
    duration: Optional[int]
    url: str
    source: str


@dataclass
class SearchSession:
    query_label: str
    results: list[TrackOption]


@dataclass
class CacheRecord:
    ts: float
    value: Any


@dataclass
class CachedFileRecord:
    ts: float
    path: Path


class QuietYDLLogger:
    def debug(self, msg: str) -> None:
        return None

    def warning(self, msg: str) -> None:
        return None

    def error(self, msg: str) -> None:
        if msg:
            LOGGER.warning("yt-dlp: %s", msg)


class TimedCache:
    def __init__(self, ttl_seconds: int, max_items: int) -> None:
        self._ttl_seconds = ttl_seconds
        self._max_items = max_items
        self._data: dict[str, CacheRecord] = {}

    def get(self, key: str) -> Any:
        self.prune()
        record = self._data.get(key)
        if not record:
            return None
        record.ts = time.time()
        return record.value

    def set(self, key: str, value: Any) -> None:
        self.prune()
        self._data[key] = CacheRecord(ts=time.time(), value=value)
        self._prune_by_size()

    def prune(self) -> None:
        now = time.time()
        stale = [key for key, record in self._data.items() if now - record.ts > self._ttl_seconds]
        for key in stale:
            del self._data[key]
        self._prune_by_size()

    def _prune_by_size(self) -> None:
        while len(self._data) > self._max_items:
            oldest_key = min(self._data, key=lambda item_key: self._data[item_key].ts)
            del self._data[oldest_key]


def load_env_file(path: Path = Path(".env")) -> dict[str, str]:
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, raw_value = line.split("=", 1)
        normalized_key = key.strip()
        normalized_value = raw_value.strip().strip('"').strip("'")
        if normalized_key:
            values[normalized_key] = normalized_value
    return values


def load_env_config(path: Path = Path(".env")) -> EnvConfig:
    env = load_env_file(path)
    telegram_token = env.get("TELEGRAM_TOKEN", "")
    if not telegram_token:
        raise RuntimeError("Не найден TELEGRAM_TOKEN в .env файле.")

    genius_token = env.get("GENIUS_ACCESS_TOKEN") or env.get("GENIUS_TOKEN")
    cookies_from_browser = env.get("YTDLP_COOKIES_FROM_BROWSER") or env.get("YT_COOKIES_FROM_BROWSER")

    cookie_file_raw = env.get("YTDLP_COOKIES_FILE") or env.get("YT_COOKIES_FILE")
    cookie_file: Optional[Path] = None
    if cookie_file_raw:
        candidate = Path(cookie_file_raw.strip())
        if candidate.exists():
            cookie_file = candidate
        else:
            LOGGER.warning("Cookie file не найден: %s", candidate)

    return EnvConfig(
        telegram_token=telegram_token,
        genius_token=genius_token,
        cookies_from_browser=cookies_from_browser.strip() if cookies_from_browser else None,
        cookies_file=cookie_file,
    )


def normalize_query(query: str) -> str:
    return " ".join(query.lower().split())


def format_duration(seconds: Optional[int]) -> str:
    if not isinstance(seconds, int) or seconds <= 0:
        return "--:--"
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_speed(speed_bytes_per_sec: Optional[float]) -> str:
    if not speed_bytes_per_sec:
        return ""
    kb = speed_bytes_per_sec / 1024
    if kb < 1024:
        return f"{kb:.0f} KB/s"
    return f"{(kb / 1024):.2f} MB/s"


def format_eta(eta_seconds: Optional[float]) -> str:
    if eta_seconds is None:
        return ""
    try:
        eta = int(eta_seconds)
    except (TypeError, ValueError):
        return ""
    minutes, seconds = divmod(max(0, eta), 60)
    return f"{minutes:02d}:{seconds:02d}"


def trim_label(text: str, max_len: int = 52) -> str:
    compact = " ".join(text.split())
    if len(compact) <= max_len:
        return compact
    return f"{compact[: max_len - 1]}…"


def build_main_menu(config: AppConfig) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton(config.main_search_button)]],
        resize_keyboard=True,
        is_persistent=True,
    )


def build_page_control_keyboard(
    config: AppConfig,
    options: list[TrackOption],
    page: int,
) -> ReplyKeyboardMarkup:
    start_idx = page * config.page_size
    end_idx = min(start_idx + config.page_size, len(options))
    count_on_page = max(0, end_idx - start_idx)

    rows: list[list[KeyboardButton]] = []
    if count_on_page > 0:
        number_row = [KeyboardButton(str(idx + 1)) for idx in range(count_on_page)]
        rows.append(number_row)

    total_pages = max(1, (len(options) + config.page_size - 1) // config.page_size)
    nav_row: list[KeyboardButton] = []
    if page > 0:
        nav_row.append(KeyboardButton(BTN_PREV_PAGE))
    if page < total_pages - 1:
        nav_row.append(KeyboardButton(BTN_NEXT_PAGE))
    if nav_row:
        rows.append(nav_row)

    rows.append([KeyboardButton(BTN_NEW_SEARCH)])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)


def build_results_text(query_label: str, total_results: int, page: int, total_pages: int) -> str:
    return (
        f"Результаты: {query_label}\n"
        f"Найдено треков: {total_results}\n"
        f"Страница {page + 1}/{total_pages}\n"
        "Выбери трек кнопкой ниже."
    )


def build_results_keyboard(
    config: AppConfig,
    session_id: int,
    options: list[TrackOption],
    page: int,
) -> InlineKeyboardMarkup:
    start_idx = page * config.page_size
    end_idx = min(start_idx + config.page_size, len(options))
    total_pages = max(1, (len(options) + config.page_size - 1) // config.page_size)

    rows: list[list[InlineKeyboardButton]] = []
    for idx in range(start_idx, end_idx):
        option = options[idx]
        label = trim_label(f"{idx + 1}. {option.artist} - {option.title} ({option.source})")
        rows.append([InlineKeyboardButton(label, callback_data=f"{CALLBACK_PICK}:{session_id}:{idx}")])

    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️", callback_data=f"{CALLBACK_NAV}:{session_id}:{page - 1}"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("➡️", callback_data=f"{CALLBACK_NAV}:{session_id}:{page + 1}"))
    if nav_row:
        rows.append(nav_row)

    rows.append([InlineKeyboardButton("🔎 Новый поиск", callback_data=CALLBACK_NEW_SEARCH)])
    return InlineKeyboardMarkup(rows)


def apply_cookie_options(ydl_options: dict[str, Any], env: EnvConfig) -> None:
    if env.cookies_from_browser:
        ydl_options["cookiesfrombrowser"] = (env.cookies_from_browser,)
    if env.cookies_file:
        ydl_options["cookiefile"] = str(env.cookies_file)


class SearchService:
    def __init__(self, config: AppConfig, env: EnvConfig) -> None:
        self._config = config
        self._env = env
        self._search_cache = TimedCache(config.search_cache_ttl_seconds, config.max_search_cache_items)
        self._genius_cache = TimedCache(config.search_cache_ttl_seconds, config.max_search_cache_items)

    async def resolve_query_with_genius(self, query: str) -> tuple[str, Optional[dict[str, str]]]:
        if not self._env.genius_token:
            return query, None

        cache_key = f"genius::{normalize_query(query)}"
        cached = self._genius_cache.get(cache_key)
        if cached is not None:
            return cached

        loop = asyncio.get_running_loop()
        resolved = await loop.run_in_executor(None, self._resolve_query_with_genius_sync, query)
        self._genius_cache.set(cache_key, resolved)
        return resolved

    async def search_tracks(self, query: str, limit: int) -> list[TrackOption]:
        cache_key = normalize_query(query)
        cached = self._search_cache.get(cache_key)
        if cached is not None:
            return cached[:limit]

        loop = asyncio.get_running_loop()
        future_to_source = {
            loop.run_in_executor(None, self._search_source_sync, source, query): source.name
            for source in self._config.search_sources
        }

        source_lists: list[list[TrackOption]] = []
        done, pending = await asyncio.wait(
            future_to_source.keys(),
            timeout=self._config.search_source_timeout_seconds,
            return_when=asyncio.ALL_COMPLETED,
        )

        for future in done:
            try:
                source_result = future.result()
            except Exception as exc:
                LOGGER.warning("Ошибка фонового поиска: %s", exc)
                continue
            if source_result:
                source_lists.append(source_result)

        for pending_future in pending:
            source_name = future_to_source.get(pending_future, "unknown")
            LOGGER.info(
                "Источник %s не ответил за %s сек, пропускаю.",
                source_name,
                self._config.search_source_timeout_seconds,
            )
            pending_future.cancel()

        merged_results = self._merge_source_results(source_lists, limit)
        self._search_cache.set(cache_key, merged_results)
        return merged_results

    def _resolve_query_with_genius_sync(self, query: str) -> tuple[str, Optional[dict[str, str]]]:
        if not self._env.genius_token:
            return query, None

        params = parse.urlencode({"q": query})
        url = f"https://api.genius.com/search?{params}"
        req = request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self._env.genius_token}",
                "User-Agent": "musicbot/1.0",
            },
        )
        try:
            with request.urlopen(req, timeout=8) as response:
                if response.status != 200:
                    return query, None
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            LOGGER.warning("Ошибка Genius API: %s", exc)
            return query, None

        hits = payload.get("response", {}).get("hits", [])
        if not hits:
            return query, None

        result = hits[0].get("result", {})
        artist = (result.get("primary_artist") or {}).get("name")
        title = result.get("title")
        if not artist or not title:
            return query, None

        refined_query = f"{artist} - {title}"
        return refined_query, {"artist": artist, "title": title}

    def _search_source_sync(self, source: SearchSource, query: str) -> list[TrackOption]:
        ydl_options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "default_search": f"{source.prefix}{source.limit}",
            "noplaylist": True,
            "ignoreerrors": True,
            "socket_timeout": 8,
            "extract_flat": "in_playlist",
            "logger": QuietYDLLogger(),
        }
        apply_cookie_options(ydl_options, self._env)

        with YoutubeDL(ydl_options) as ydl:
            try:
                payload = ydl.extract_info(query, download=False)
            except Exception as exc:
                LOGGER.warning("Ошибка поиска (%s): %s", source.name, exc)
                return []

        entries = payload.get("entries", []) if isinstance(payload, dict) else []
        tracks: list[TrackOption] = []
        for entry in entries:
            if not entry:
                continue

            option = self._entry_to_track_option(entry, source)
            if option is not None:
                tracks.append(option)
        return tracks

    def _entry_to_track_option(self, entry: dict[str, Any], source: SearchSource) -> Optional[TrackOption]:
        title = str(entry.get("title", "Без названия"))
        lowered_title = title.lower()
        if any(marker in lowered_title for marker in self._config.non_music_hints):
            return None

        duration = entry.get("duration")
        if isinstance(duration, int) and duration < 35:
            return None

        age_limit = entry.get("age_limit")
        if isinstance(age_limit, int) and age_limit >= 18:
            return None

        video_url = entry.get("webpage_url")
        video_id = entry.get("id")
        if not video_url and video_id and source.prefix.startswith("ytsearch"):
            video_url = f"https://www.youtube.com/watch?v={video_id}"
        if not video_url:
            return None

        artist = entry.get("uploader") or entry.get("channel") or "Неизвестный исполнитель"
        return TrackOption(
            title=title,
            artist=str(artist),
            duration=duration if isinstance(duration, int) else None,
            url=str(video_url),
            source=source.name,
        )

    @staticmethod
    def _merge_source_results(source_lists: list[list[TrackOption]], limit: int) -> list[TrackOption]:
        merged: list[TrackOption] = []
        seen_urls: set[str] = set()
        index = 0

        while len(merged) < limit:
            has_candidates = False
            for source_items in source_lists:
                if index >= len(source_items):
                    continue
                has_candidates = True
                item = source_items[index]
                if item.url in seen_urls:
                    continue
                seen_urls.add(item.url)
                merged.append(item)
                if len(merged) >= limit:
                    break
            if not has_candidates:
                break
            index += 1
        return merged


class DownloadService:
    def __init__(self, config: AppConfig, env: EnvConfig) -> None:
        self._config = config
        self._env = env
        self._download_cache: dict[str, CachedFileRecord] = {}
        self._config.download_dir.mkdir(exist_ok=True)

    def cleanup_download_directory(self) -> None:
        now = time.time()
        for file_path in self._config.download_dir.iterdir():
            if not file_path.is_file():
                continue
            try:
                age = now - file_path.stat().st_mtime
                if age > self._config.stale_download_file_age_seconds:
                    file_path.unlink(missing_ok=True)
            except OSError:
                continue

    def download_audio(
        self,
        video_url: str,
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> Optional[Path]:
        cached_file = self._get_cached_download(video_url)
        if cached_file:
            self._emit_progress("Файл взят из кэша, отправляю...", progress_callback)
            return cached_file

        file_id = uuid.uuid4().hex
        output_template = str(self._config.download_dir / f"{file_id}.%(ext)s")
        progress_state = {"last_percent_step": -1}

        def on_download_progress(data: dict[str, Any]) -> None:
            status = data.get("status")
            if status == "downloading":
                downloaded = data.get("downloaded_bytes") or 0
                total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
                speed = format_speed(data.get("speed"))
                eta = format_eta(data.get("eta"))
                if total > 0:
                    percent = (downloaded / total) * 100
                    percent_step = int(percent // 5)
                    if percent_step > progress_state["last_percent_step"]:
                        progress_state["last_percent_step"] = percent_step
                        suffix_parts = [part for part in (speed, f"ETA {eta}" if eta else "") if part]
                        suffix = f" ({', '.join(suffix_parts)})" if suffix_parts else ""
                        self._emit_progress(f"Скачивание: {percent:.1f}%{suffix}", progress_callback)
                elif progress_state["last_percent_step"] < 0:
                    self._emit_progress("Скачивание...", progress_callback)
            elif status == "finished":
                self._emit_progress("Скачивание завершено, подготавливаю файл...", progress_callback)
            elif status == "error":
                self._emit_progress("Ошибка при скачивании.", progress_callback)

        ydl_options: dict[str, Any] = {
            "format": "bestaudio[ext=m4a][abr<=128]/bestaudio[abr<=96]/bestaudio[ext=mp3]/worstaudio/bestaudio",
            "outtmpl": output_template,
            "quiet": True,
            "no_warnings": True,
            "nocheckcertificate": True,
            "noplaylist": True,
            "continuedl": True,
            "retries": 8,
            "fragment_retries": 8,
            "skip_unavailable_fragments": True,
            "socket_timeout": 25,
            "concurrent_fragment_downloads": 1,
            "http_chunk_size": 1048576,
            "extractor_retries": 3,
            "logger": QuietYDLLogger(),
            "progress_hooks": [on_download_progress],
        }
        apply_cookie_options(ydl_options, self._env)

        with YoutubeDL(ydl_options) as ydl:
            try:
                info = ydl.extract_info(video_url, download=True)
                prepared_file = Path(ydl.prepare_filename(info))
                if prepared_file.exists():
                    self._cache_download(video_url, prepared_file)
                    return prepared_file

                matches = list(self._config.download_dir.glob(f"{file_id}.*"))
                if matches:
                    self._cache_download(video_url, matches[0])
                    return matches[0]
                return None
            except Exception as exc:
                LOGGER.warning("Ошибка загрузки yt-dlp: %s", exc)
                return None

    def _emit_progress(self, text: str, callback: Optional[Callable[[str], None]]) -> None:
        if callback:
            try:
                callback(text)
            except Exception:
                pass
        LOGGER.info("[download] %s", text)

    def _prune_download_cache(self) -> None:
        now = time.time()
        to_remove: list[str] = []
        for url, record in self._download_cache.items():
            is_expired = now - record.ts > self._config.download_cache_ttl_seconds
            is_missing = not record.path.exists()
            if is_expired or is_missing:
                if is_expired and record.path.exists():
                    record.path.unlink(missing_ok=True)
                to_remove.append(url)

        for url in to_remove:
            del self._download_cache[url]

        while len(self._download_cache) > self._config.max_download_cache_items:
            oldest_url = min(self._download_cache, key=lambda key: self._download_cache[key].ts)
            old_path = self._download_cache[oldest_url].path
            if old_path.exists():
                old_path.unlink(missing_ok=True)
            del self._download_cache[oldest_url]

    def _get_cached_download(self, video_url: str) -> Optional[Path]:
        self._prune_download_cache()
        record = self._download_cache.get(video_url)
        if not record:
            return None
        if not record.path.exists():
            del self._download_cache[video_url]
            return None
        record.ts = time.time()
        return record.path

    def _cache_download(self, video_url: str, path: Path) -> None:
        self._prune_download_cache()
        self._download_cache[video_url] = CachedFileRecord(ts=time.time(), path=path)
        self._prune_download_cache()


class MusicBotController:
    def __init__(self, config: AppConfig, search_service: SearchService, download_service: DownloadService) -> None:
        self._config = config
        self._search_service = search_service
        self._download_service = download_service
        self._session_store: dict[str, SearchSession] = {}

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message:
            return
        await update.message.reply_text(
            "Выбери кнопку ниже, чтобы начать поиск музыки.",
            reply_markup=build_main_menu(self._config),
        )

    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.message.text:
            return

        text = update.message.text.strip()
        if text.startswith("/"):
            return

        if text == self._config.main_search_button:
            await self._send_search_prompt(update, context)
            return

        awaiting_query = bool(context.user_data.get("awaiting_query"))
        if awaiting_query:
            await self._run_search(update, context, text)
            return

        if await self._handle_control_text(update, context, text):
            return

        if text:
            await update.message.reply_text(
                "Нажми «🔎 Поиск музыки» и введи запрос.",
                reply_markup=build_main_menu(self._config),
            )

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        callback_query = update.callback_query
        if not callback_query:
            return

        await callback_query.answer()
        payload = callback_query.data or ""
        LOGGER.info("[callback] user=%s payload=%s", getattr(callback_query.from_user, "id", "unknown"), payload)

        if payload == CALLBACK_NEW_SEARCH:
            context.user_data["awaiting_query"] = True
            if callback_query.message:
                await callback_query.message.reply_text("Введи новый запрос: исполнитель или трек.")
            return

        action, session_id, value = self._parse_callback_payload(payload)
        if action is None:
            await callback_query.answer("Некорректная кнопка. Сделай новый поиск.", show_alert=True)
            return

        session = self._get_session(context, session_id)
        if session is None:
            await callback_query.answer("Сессия устарела. Нажми «Новый поиск».", show_alert=True)
            if callback_query.message:
                await callback_query.message.reply_text("⚠️ Эта выдача устарела. Нажми «🔎 Новый поиск».")
            return

        if action == CALLBACK_NAV:
            await self._handle_navigation(callback_query, context, session_id, value, session)
            return
        if action == CALLBACK_PICK:
            await self._handle_pick(callback_query, value, session)

    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        LOGGER.exception("Unhandled bot error: %s", context.error)
        if isinstance(update, Update) and update.effective_message:
            try:
                await update.effective_message.reply_text("⚠️ Внутренняя ошибка. Попробуй ещё раз.")
            except Exception:
                pass

    async def _send_search_prompt(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        context.user_data["awaiting_query"] = True
        chat = update.effective_chat
        if not chat:
            return
        await chat.send_message(
            "Напиши исполнителя или трек. Сначала уточняю через Genius, затем ищу в YouTube и SoundCloud."
        )

    async def _run_search(self, update: Update, context: ContextTypes.DEFAULT_TYPE, query: str) -> None:
        chat = update.effective_chat
        if not chat:
            return

        status_message = await chat.send_message("🔎 Ищу в базах...")
        refined_query, genius_meta = await self._search_service.resolve_query_with_genius(query)
        options = await self._search_service.search_tracks(refined_query, self._config.search_limit)
        if not options:
            await status_message.edit_text("❌ Ничего не найдено. Попробуй другой запрос.")
            return

        query_label = refined_query
        if genius_meta and normalize_query(refined_query) != normalize_query(query):
            query_label = f"{query} (Genius: {refined_query})"

        session_id = status_message.message_id
        self._save_session(context, session_id, SearchSession(query_label=query_label, results=options))
        context.user_data["awaiting_query"] = False
        self._set_active_session(context, session_id, page=0)

        total_pages = max(1, (len(options) + self._config.page_size - 1) // self._config.page_size)
        await status_message.edit_text(
            build_results_text(query_label, len(options), page=0, total_pages=total_pages),
            reply_markup=build_results_keyboard(self._config, session_id, options, page=0),
        )
        await chat.send_message(
            "Выбор трека кнопками:",
            reply_markup=build_page_control_keyboard(self._config, options, page=0),
        )

    async def _handle_control_text(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        text: str,
    ) -> bool:
        if not update.message:
            return False
        LOGGER.info("[control] user=%s text=%s", getattr(update.effective_user, "id", "unknown"), text)

        if text == BTN_NEW_SEARCH:
            await self._send_search_prompt(update, context)
            return True

        active_session_id, active_page = self._get_active_session_state(context)
        if active_session_id is None:
            return False

        session = self._get_session(context, active_session_id)
        if session is None:
            await update.message.reply_text("⚠️ Выдача устарела. Нажми «🔎 Новый поиск».")
            return True

        total_pages = max(1, (len(session.results) + self._config.page_size - 1) // self._config.page_size)
        if text == BTN_PREV_PAGE:
            new_page = max(0, active_page - 1)
            await self._send_results_page(update.message, context, active_session_id, session, new_page)
            return True
        if text == BTN_NEXT_PAGE:
            new_page = min(total_pages - 1, active_page + 1)
            await self._send_results_page(update.message, context, active_session_id, session, new_page)
            return True

        if text.isdigit():
            local_number = int(text)
            start = active_page * self._config.page_size
            end = min(start + self._config.page_size, len(session.results))
            count_on_page = end - start
            if local_number < 1 or local_number > count_on_page:
                await update.message.reply_text("Неверный номер трека на этой странице.")
                return True
            track_index = start + (local_number - 1)
            await self._download_and_send_track(update.message, session.results[track_index])
            return True

        return False

    async def _handle_navigation(
        self,
        callback_query,
        context: ContextTypes.DEFAULT_TYPE,
        session_id: int,
        requested_page: int,
        session: SearchSession,
    ) -> None:
        if not callback_query.message:
            await callback_query.answer("Сообщение не найдено. Сделай новый поиск.", show_alert=True)
            return
        total_pages = max(1, (len(session.results) + self._config.page_size - 1) // self._config.page_size)
        page = max(0, min(requested_page, total_pages - 1))
        await callback_query.message.edit_text(
            build_results_text(session.query_label, len(session.results), page=page, total_pages=total_pages),
            reply_markup=build_results_keyboard(self._config, session_id, session.results, page),
        )
        self._set_active_session(context, session_id=session_id, page=page)
        await callback_query.message.reply_text(
            "Выбор трека кнопками:",
            reply_markup=build_page_control_keyboard(self._config, session.results, page),
        )

    async def _handle_pick(self, callback_query, value: int, session: SearchSession) -> None:
        if not callback_query.message:
            await callback_query.answer("Сообщение не найдено. Сделай новый поиск.", show_alert=True)
            return
        if value < 0 or value >= len(session.results):
            await callback_query.answer("Неверный номер трека.", show_alert=True)
            return

        track = session.results[value]
        await self._download_and_send_track(callback_query.message, track)

    async def _send_results_page(
        self,
        message,
        context: ContextTypes.DEFAULT_TYPE,
        session_id: int,
        session: SearchSession,
        page: int,
    ) -> None:
        total_pages = max(1, (len(session.results) + self._config.page_size - 1) // self._config.page_size)
        safe_page = max(0, min(page, total_pages - 1))
        self._set_active_session(context, session_id, safe_page)
        await message.reply_text(
            build_results_text(session.query_label, len(session.results), page=safe_page, total_pages=total_pages),
            reply_markup=build_results_keyboard(self._config, session_id, session.results, safe_page),
        )
        await message.reply_text(
            "Выбор трека кнопками:",
            reply_markup=build_page_control_keyboard(self._config, session.results, safe_page),
        )

    async def _download_and_send_track(self, message, track: TrackOption) -> None:
        progress_message = await message.reply_text(f"⬇️ Скачиваю: {track.artist} - {track.title}\nПодготовка...")
        loop = asyncio.get_running_loop()
        progress_state = {"text": "Подготовка..."}

        def on_progress(text: str) -> None:
            progress_state["text"] = text

        download_future = loop.run_in_executor(
            None,
            self._download_service.download_audio,
            track.url,
            on_progress,
        )

        last_shown_progress = ""
        while not download_future.done():
            current_progress = str(progress_state.get("text", ""))
            if current_progress and current_progress != last_shown_progress:
                try:
                    await progress_message.edit_text(
                        f"⬇️ Скачиваю: {track.artist} - {track.title}\n{current_progress}"
                    )
                    last_shown_progress = current_progress
                except Exception as edit_error:
                    if "message is not modified" not in str(edit_error).lower():
                        LOGGER.warning("Ошибка обновления прогресса: %s", edit_error)
            await asyncio.sleep(0.8)

        file_path = await download_future
        if not file_path:
            await progress_message.edit_text(
                "❌ Не удалось скачать аудио. Возможно, трек ограничен или временно недоступен."
            )
            return

        await progress_message.edit_text("📤 Отправляю файл...")
        try:
            sent_as = await self._send_track_with_fallback(message, file_path, track)
            await progress_message.delete()
            if sent_as == "document":
                await message.reply_text("ℹ️ Трек отправлен как файл для совместимости.")
        except Exception as exc:
            await progress_message.edit_text(f"⚠️ Ошибка при отправке: {exc}")

    async def _send_track_with_fallback(self, message, file_path: Path, track: TrackOption) -> str:
        caption = f"✨ {track.artist} - {track.title}"
        try:
            with open(file_path, "rb") as audio:
                await message.reply_audio(
                    audio=audio,
                    caption=caption,
                    title=track.title,
                    performer=track.artist,
                )
            return "audio"
        except Exception as audio_error:
            LOGGER.warning("reply_audio error: %s", audio_error)

        with open(file_path, "rb") as audio_doc:
            await message.reply_document(
                document=audio_doc,
                caption=f"{caption}\n(отправлено как файл, т.к. аудио-режим недоступен)",
            )
        return "document"

    @staticmethod
    def _parse_callback_payload(payload: str) -> tuple[Optional[str], int, int]:
        parts = payload.split(":")
        if len(parts) != 3:
            return None, 0, 0
        action, session_id_raw, value_raw = parts
        if not session_id_raw.isdigit() or not value_raw.isdigit():
            return None, 0, 0
        return action, int(session_id_raw), int(value_raw)

    def _save_session(self, context: ContextTypes.DEFAULT_TYPE, session_id: int, session: SearchSession) -> None:
        session_key = str(session_id)
        user_sessions: dict[str, SearchSession] = context.user_data.setdefault(USER_SESSIONS_KEY, {})
        chat_sessions: dict[str, SearchSession] = context.chat_data.setdefault(CHAT_SESSIONS_KEY, {})

        user_sessions[session_key] = session
        chat_sessions[session_key] = session
        self._session_store[session_key] = session

        self._trim_sessions(user_sessions)
        self._trim_sessions(chat_sessions)
        self._trim_sessions(self._session_store)

    def _set_active_session(self, context: ContextTypes.DEFAULT_TYPE, session_id: int, page: int) -> None:
        context.user_data[ACTIVE_SESSION_ID_KEY] = session_id
        context.user_data[ACTIVE_PAGE_KEY] = page
        context.chat_data[ACTIVE_SESSION_ID_KEY] = session_id
        context.chat_data[ACTIVE_PAGE_KEY] = page

    @staticmethod
    def _get_active_session_state(context: ContextTypes.DEFAULT_TYPE) -> tuple[Optional[int], int]:
        session_id = context.user_data.get(ACTIVE_SESSION_ID_KEY)
        page = context.user_data.get(ACTIVE_PAGE_KEY, 0)

        if session_id is None:
            session_id = context.chat_data.get(ACTIVE_SESSION_ID_KEY)
            page = context.chat_data.get(ACTIVE_PAGE_KEY, 0)

        if session_id is None:
            return None, 0

        try:
            return int(session_id), int(page)
        except (TypeError, ValueError):
            return None, 0

    def _get_session(self, context: ContextTypes.DEFAULT_TYPE, session_id: int) -> Optional[SearchSession]:
        session_key = str(session_id)

        user_sessions: dict[str, SearchSession] = context.user_data.get(USER_SESSIONS_KEY, {})
        if session_key in user_sessions:
            return user_sessions[session_key]

        chat_sessions: dict[str, SearchSession] = context.chat_data.get(CHAT_SESSIONS_KEY, {})
        if session_key in chat_sessions:
            return chat_sessions[session_key]

        return self._session_store.get(session_key)

    @staticmethod
    def _trim_sessions(storage: dict[str, SearchSession], max_items: int = 20) -> None:
        while len(storage) > max_items:
            oldest_key = next(iter(storage.keys()))
            del storage[oldest_key]


def main() -> None:
    config = AppConfig()
    env = load_env_config()

    search_service = SearchService(config, env)
    download_service = DownloadService(config, env)
    download_service.cleanup_download_directory()

    controller = MusicBotController(config, search_service, download_service)
    app = ApplicationBuilder().token(env.telegram_token).build()
    app.add_handler(CommandHandler("start", controller.start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, controller.handle_text))
    app.add_handler(CallbackQueryHandler(controller.handle_callback))
    app.add_error_handler(controller.error_handler)

    LOGGER.info("🚀 Бот запущен (кнопочный режим)")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
