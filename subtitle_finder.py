from pathlib import Path
from difflib import SequenceMatcher
from ctypes import wintypes

import base64
import ctypes
import io
import json
import os
import queue
import re
import subprocess
import threading
import time
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from tkinter import filedialog, messagebox, ttk


APP_NAME = "SubDub"
APP_VERSION = "1.7.0"
USER_AGENT = f"{APP_NAME}/{APP_VERSION}"

LANGUAGE = "en"
SUBDL_LANGUAGE = "EN"

VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".avi", ".mov", ".wmv", ".m4v",
    ".webm", ".mpg", ".mpeg", ".ts"
}

SUBTITLE_EXTENSIONS = {
    ".srt", ".ass", ".ssa", ".vtt", ".sub"
}

IGNORE_VIDEO_NAMES = {
    "rarbg", "rarbg.com", "sample", "sample-video", "trailer"
}

APPDATA = Path(os.environ.get("APPDATA", str(Path.home())))
SETTINGS_DIR = APPDATA / APP_NAME
SETTINGS_FILE = SETTINGS_DIR / "settings.json"

# -----------------------------
# Theme
# -----------------------------

BG = "#070A0F"
TOPBAR = "#090D14"
PANEL = "#101620"
PANEL_ALT = "#141C28"
CARD = "#121A26"
CARD_HOVER = "#1A2637"
ENTRY_BG = "#090E15"
TREE_BG = "#090D13"
BORDER = "#253247"
BORDER_SOFT = "#192435"

TEXT = "#F5F8FC"
TEXT_SECONDARY = "#A9B5C5"
TEXT_MUTED = "#657387"

ACCENT = "#6C63FF"
ACCENT_HOVER = "#817AFF"
ACCENT_DARK = "#4F47D7"

CYAN = "#4FD9FF"
BLUE = "#4E91FF"
GREEN = "#3AD29F"
ORANGE = "#FFAD4D"
RED = "#F26574"
WHITE = "#FFFFFF"

DISABLED_BG = "#181D25"
DISABLED_FG = "#667080"


# ============================================================
# DPAPI
# ============================================================

class DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def _bytes_to_blob(data: bytes):
    buffer = ctypes.create_string_buffer(data)
    blob = DATA_BLOB(
        len(data),
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)),
    )
    return blob, buffer


def protect_secret(value: str) -> str:
    if not value:
        return ""

    raw = value.encode("utf-8")

    if os.name != "nt":
        return base64.b64encode(raw).decode("ascii")

    input_blob, _buffer = _bytes_to_blob(raw)
    output_blob = DATA_BLOB()

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    result = crypt32.CryptProtectData(
        ctypes.byref(input_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(output_blob),
    )

    if not result:
        raise ctypes.WinError()

    try:
        encrypted = ctypes.string_at(
            output_blob.pbData,
            output_blob.cbData,
        )
    finally:
        kernel32.LocalFree(output_blob.pbData)

    return base64.b64encode(encrypted).decode("ascii")


def unprotect_secret(value: str) -> str:
    if not value:
        return ""

    try:
        raw = base64.b64decode(value)
    except Exception:
        return ""

    if os.name != "nt":
        try:
            return raw.decode("utf-8")
        except Exception:
            return ""

    input_blob, _buffer = _bytes_to_blob(raw)
    output_blob = DATA_BLOB()

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    result = crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(output_blob),
    )

    if not result:
        return ""

    try:
        decrypted = ctypes.string_at(
            output_blob.pbData,
            output_blob.cbData,
        )
    finally:
        kernel32.LocalFree(output_blob.pbData)

    try:
        return decrypted.decode("utf-8")
    except Exception:
        return ""


# ============================================================
# Settings
# ============================================================

DEFAULT_SETTINGS = {
    "last_folder": "",
    "opensubtitles_enabled": True,
    "subdl_enabled": False,
    "provider_order": "OpenSubtitles first",
    "opensubtitles_username": "",
    "opensubtitles_password": "",
    "opensubtitles_api_key": "",
    "subdl_api_key": "",
}


def load_settings():
    result = DEFAULT_SETTINGS.copy()

    if not SETTINGS_FILE.exists():
        return result

    try:
        data = json.loads(
            SETTINGS_FILE.read_text(encoding="utf-8")
        )
    except Exception:
        return result

    result["last_folder"] = data.get("last_folder", "")
    result["opensubtitles_enabled"] = bool(
        data.get("opensubtitles_enabled", True)
    )
    result["subdl_enabled"] = bool(
        data.get("subdl_enabled", False)
    )
    result["provider_order"] = data.get(
        "provider_order",
        "OpenSubtitles first",
    )
    result["opensubtitles_username"] = data.get(
        "opensubtitles_username",
        "",
    )
    result["opensubtitles_password"] = unprotect_secret(
        data.get("opensubtitles_password", "")
    )
    result["opensubtitles_api_key"] = unprotect_secret(
        data.get("opensubtitles_api_key", "")
    )
    result["subdl_api_key"] = unprotect_secret(
        data.get("subdl_api_key", "")
    )

    return result


def save_settings(settings):
    SETTINGS_DIR.mkdir(parents=True, exist_ok=True)

    data = {
        "last_folder": settings["last_folder"],
        "opensubtitles_enabled": bool(
            settings["opensubtitles_enabled"]
        ),
        "subdl_enabled": bool(
            settings["subdl_enabled"]
        ),
        "provider_order": settings["provider_order"],
        "opensubtitles_username":
            settings["opensubtitles_username"],
        "opensubtitles_password": protect_secret(
            settings["opensubtitles_password"]
        ),
        "opensubtitles_api_key": protect_secret(
            settings["opensubtitles_api_key"]
        ),
        "subdl_api_key": protect_secret(
            settings["subdl_api_key"]
        ),
    }

    SETTINGS_FILE.write_text(
        json.dumps(data, indent=2),
        encoding="utf-8",
    )


# ============================================================
# HTTP
# ============================================================

def http_json(
    url,
    method="GET",
    headers=None,
    data=None,
    timeout=30,
):
    headers = headers or {}
    body = None

    if data is not None:
        body = json.dumps(data).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=body,
        headers=headers,
        method=method,
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=timeout,
        ) as response:
            raw = response.read()
            if not raw:
                return {}
            return json.loads(raw.decode("utf-8"))

    except urllib.error.HTTPError as error:
        response_body = error.read().decode(
            "utf-8",
            errors="replace",
        )
        raise RuntimeError(
            f"HTTP {error.code}: {response_body}"
        ) from error

    except urllib.error.URLError as error:
        raise RuntimeError(
            f"Network error: {error}"
        ) from error


def http_bytes(
    url,
    headers=None,
    timeout=60,
):
    headers = headers or {}

    request = urllib.request.Request(
        url,
        headers=headers,
        method="GET",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=timeout,
        ) as response:
            return response.read()

    except urllib.error.HTTPError as error:
        raise RuntimeError(
            f"HTTP {error.code}"
        ) from error

    except urllib.error.URLError as error:
        raise RuntimeError(
            f"Network error: {error}"
        ) from error


# ============================================================
# Media parsing
# ============================================================

RELEASE_JUNK_PATTERNS = [
    r"\b2160p\b",
    r"\b1080p\b",
    r"\b720p\b",
    r"\b576p\b",
    r"\b480p\b",
    r"\b4k\b",
    r"\buhd\b",
    r"\bbluray\b",
    r"\bbrrip\b",
    r"\bbdrip\b",
    r"\bwebrip\b",
    r"\bweb[\s.-]?dl\b",
    r"\bhdrip\b",
    r"\bdvdrip\b",
    r"\bhdtv\b",
    r"\bremastered\b",
    r"\bremaster\b",
    r"\buncut\b",
    r"\bunrated\b",
    r"\bextended\b",
    r"\bx264\b",
    r"\bx265\b",
    r"\bh\.?264\b",
    r"\bh\.?265\b",
    r"\bhevc\b",
    r"\bav1\b",
    r"\baac\b",
    r"\bac3\b",
    r"\b10bit\b",
    r"\byify\b",
    r"\byts\b",
    r"\brarbg\b",
    r"\bbone\b",
    r"\bgalaxyrg\b",
    r"\bshaanig\b",
]


def normalize_release_name(text):
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def release_similarity(
    video_name,
    subtitle_release,
):
    if not subtitle_release:
        return 0.0

    return SequenceMatcher(
        None,
        normalize_release_name(video_name),
        normalize_release_name(subtitle_release),
    ).ratio()


def clean_release_text(text):
    value = text.replace(".", " ").replace("_", " ")

    value = re.sub(
        r"\[[^\]]+\]",
        " ",
        value,
    )

    for pattern in RELEASE_JUNK_PATTERNS:
        value = re.sub(
            pattern,
            " ",
            value,
            flags=re.IGNORECASE,
        )

    value = re.sub(
        r"\s+",
        " ",
        value,
    )

    return value.strip(" -._")


def extract_year(text):
    matches = re.findall(
        r"\b(19\d{2}|20\d{2})\b",
        text,
    )

    if matches:
        return int(matches[-1])

    return None


def clean_movie_title(text):
    value = text.replace(".", " ").replace("_", " ")

    year_match = re.search(
        r"\b(19\d{2}|20\d{2})\b",
        value,
    )

    if year_match:
        value = value[:year_match.start()]

    return clean_release_text(value)


def clean_series_title(text):
    value = text.replace(".", " ").replace("_", " ")

    # Remove TV episode markers and everything after them.
    patterns = [
        r"(?i)\bS\d{1,2}\s*[-._ ]*E\d{1,3}\b",
        r"(?i)\b\d{1,2}\s*x\s*\d{1,3}\b",
        (
            r"(?i)\bSeason\s*0*(\d{1,2})"
            r"\s*(?:[-._ ]+)?Episode\s*0*(\d{1,3})\b"
        ),
    ]

    earliest = None

    for pattern in patterns:
        match = re.search(pattern, value)
        if match:
            if earliest is None or match.start() < earliest:
                earliest = match.start()

    if earliest is not None:
        value = value[:earliest]

    value = re.sub(
        r"(?i)\bSeason\s*0*\d{1,2}\b",
        " ",
        value,
    )

    return clean_release_text(value)


def extract_episode_title(text):
    patterns = [
        (
            r"(?i)\bS\d{1,2}\s*[-._ ]*E\d{1,3}\b"
            r"[\s._-]*(.*)$"
        ),
        (
            r"(?i)\b\d{1,2}\s*x\s*\d{1,3}\b"
            r"[\s._-]*(.*)$"
        ),
        (
            r"(?i)\bSeason\s*0*\d{1,2}\s*"
            r"(?:[-._ ]+)?Episode\s*0*\d{1,3}\b"
            r"[\s._-]*(.*)$"
        ),
    ]

    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            title = clean_release_text(
                match.group(1)
            )
            return title

    return ""


def parse_tv_from_filename(video):
    stem = video.stem

    # S01E23
    match = re.search(
        r"(?i)\bS0*(\d{1,2})\s*[-._ ]*E0*(\d{1,3})\b",
        stem,
    )

    if match:
        season = int(match.group(1))
        episode = int(match.group(2))

        series = clean_series_title(
            stem[:match.start()]
        )

        if not series:
            series = clean_series_title(
                video.parent.name
            )

        return {
            "kind": "tv",
            "series": series,
            "season": season,
            "episode": episode,
            "episode_title": extract_episode_title(stem),
            "year": extract_year(series),
            "source_pattern": "SxxExx",
        }

    # 1x23
    match = re.search(
        r"(?i)\b0*(\d{1,2})\s*x\s*0*(\d{1,3})\b",
        stem,
    )

    if match:
        season = int(match.group(1))
        episode = int(match.group(2))

        series = clean_series_title(
            stem[:match.start()]
        )

        if not series:
            series = clean_series_title(
                video.parent.name
            )

        return {
            "kind": "tv",
            "series": series,
            "season": season,
            "episode": episode,
            "episode_title": extract_episode_title(stem),
            "year": extract_year(series),
            "source_pattern": "NxNN",
        }

    # Season 01 Episode 23
    match = re.search(
        (
            r"(?i)\bSeason\s*0*(\d{1,2})"
            r"\s*(?:[-._ ]+)?Episode\s*0*(\d{1,3})\b"
        ),
        stem,
    )

    if match:
        season = int(match.group(1))
        episode = int(match.group(2))

        series = clean_series_title(
            stem[:match.start()]
        )

        if not series:
            series = clean_series_title(
                video.parent.name
            )

        return {
            "kind": "tv",
            "series": series,
            "season": season,
            "episode": episode,
            "episode_title": extract_episode_title(stem),
            "year": extract_year(series),
            "source_pattern": "Season Episode",
        }

    return None


def parse_tv_from_folders(video):
    parent = video.parent
    parent_name = parent.name

    season_match = re.search(
        r"(?i)\bSeason[\s._-]*0*(\d{1,2})\b",
        parent_name,
    )

    if not season_match:
        season_match = re.fullmatch(
            r"(?i)S0*(\d{1,2})",
            parent_name.strip(),
        )

    if not season_match:
        return None

    season = int(season_match.group(1))

    # Common folder layout:
    # Show Name\Season 01\01 - Pilot.mkv
    episode_match = re.match(
        r"^\s*0*(\d{1,3})(?:\s*[-._ ]+)(.*)$",
        video.stem,
    )

    if not episode_match:
        episode_match = re.search(
            r"(?i)\bEpisode[\s._-]*0*(\d{1,3})\b",
            video.stem,
        )

        if episode_match:
            episode = int(
                episode_match.group(1)
            )
            episode_title = re.sub(
                r"(?i)^.*?\bEpisode[\s._-]*0*\d{1,3}\b[\s._-]*",
                "",
                video.stem,
            )
        else:
            return None
    else:
        episode = int(
            episode_match.group(1)
        )
        episode_title = episode_match.group(2)

    series = clean_series_title(
        parent.parent.name
    )

    if not series:
        return None

    return {
        "kind": "tv",
        "series": series,
        "season": season,
        "episode": episode,
        "episode_title": clean_release_text(
            episode_title
        ),
        "year": extract_year(series),
        "source_pattern": "Season folder",
    }


def parse_media_identity(video):
    tv = parse_tv_from_filename(video)

    if tv:
        return tv

    tv = parse_tv_from_folders(video)

    if tv:
        return tv

    title = clean_movie_title(
        video.stem
    )

    year = extract_year(
        video.stem
    )

    return {
        "kind": "movie",
        "title": title,
        "year": year,
    }


def tv_display(identity):
    title = identity.get(
        "series",
        "Unknown series",
    )

    season = identity.get(
        "season",
        0,
    )

    episode = identity.get(
        "episode",
        0,
    )

    episode_title = identity.get(
        "episode_title",
        "",
    )

    result = (
        f"{title} · "
        f"S{season:02d}E{episode:02d}"
    )

    if episode_title:
        result += f" · {episode_title}"

    return result


# ============================================================
# Providers
# ============================================================

class SubtitleProvider:
    name = "Provider"

    def login(self):
        return

    def search(
        self,
        video,
        identity,
    ):
        raise NotImplementedError

    def download(
        self,
        result,
        destination,
    ):
        raise NotImplementedError


class OpenSubtitlesProvider(
    SubtitleProvider
):
    name = "OpenSubtitles"

    def __init__(
        self,
        username,
        password,
        api_key,
    ):
        self.username = username
        self.password = password
        self.api_key = api_key
        self.token = None
        self.base_url = (
            "https://api.opensubtitles.com"
        )


    def headers(
        self,
        authenticated=False,
    ):
        result = {
            "Api-Key": self.api_key,
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        if authenticated and self.token:
            result[
                "Authorization"
            ] = f"Bearer {self.token}"

        return result


    def login(self):
        result = http_json(
            self.base_url
            + "/api/v1/login",
            method="POST",
            headers=self.headers(),
            data={
                "username": self.username,
                "password": self.password,
            },
        )

        self.token = result.get(
            "token"
        )

        if not self.token:
            raise RuntimeError(
                "OpenSubtitles did not return "
                "an authentication token."
            )


    def request_subtitles(
        self,
        parameters,
    ):
        url = (
            self.base_url
            + "/api/v1/subtitles?"
            + urllib.parse.urlencode(
                parameters
            )
        )

        result = http_json(
            url,
            headers=self.headers(
                authenticated=True
            ),
        )

        return result.get(
            "data",
            [],
        )


    def search_movie(
        self,
        video,
        identity,
    ):
        title = identity.get(
            "title",
            "",
        )

        year = identity.get(
            "year"
        )

        if not title:
            return None

        params = {
            "query": title,
            "languages": LANGUAGE,
            "type": "movie",
            "order_by": "download_count",
            "order_direction": "desc",
        }

        if year:
            params["year"] = str(
                year
            )

        results = self.request_subtitles(
            params
        )

        if not results and year:
            params.pop(
                "year",
                None,
            )
            results = self.request_subtitles(
                params
            )

        return self.choose_result(
            video,
            identity,
            results,
        )


    def search_tv(
        self,
        video,
        identity,
    ):
        series = identity.get(
            "series",
            "",
        )

        season = identity.get(
            "season"
        )

        episode = identity.get(
            "episode"
        )

        if (
            not series
            or season is None
            or episode is None
        ):
            return None

        # Explicit type=episode remains useful even though the API
        # can infer series searches from season/episode metadata.
        params = {
            "query": series,
            "languages": LANGUAGE,
            "type": "episode",
            "season_number": str(
                season
            ),
            "episode_number": str(
                episode
            ),
            "order_by": "download_count",
            "order_direction": "desc",
        }

        results = self.request_subtitles(
            params
        )

        if not results:
            # Conservative fallback without type. OpenSubtitles
            # currently detects series searches from season/episode.
            params.pop(
                "type",
                None,
            )

            results = self.request_subtitles(
                params
            )

        return self.choose_result(
            video,
            identity,
            results,
        )


    def choose_result(
        self,
        video,
        identity,
        results,
    ):
        matches = []

        for item in results:
            attributes = item.get(
                "attributes",
                {},
            )

            files = attributes.get(
                "files",
                [],
            )

            if not files:
                continue

            file_id = files[0].get(
                "file_id"
            )

            if not file_id:
                continue

            feature = attributes.get(
                "feature_details",
                {},
            )

            release = attributes.get(
                "release",
                "",
            )

            score = release_similarity(
                video.stem,
                release,
            )

            if identity["kind"] == "movie":
                wanted_year = identity.get(
                    "year"
                )
                found_year = feature.get(
                    "year"
                )

                if (
                    wanted_year
                    and found_year == wanted_year
                ):
                    score += 1.0

                display = feature.get(
                    "title",
                    identity.get(
                        "title",
                        "",
                    ),
                )

            else:
                wanted_season = identity.get(
                    "season"
                )
                wanted_episode = identity.get(
                    "episode"
                )

                found_season = (
                    feature.get(
                        "season_number"
                    )
                    or feature.get(
                        "season"
                    )
                )

                found_episode = (
                    feature.get(
                        "episode_number"
                    )
                    or feature.get(
                        "episode"
                    )
                )

                if (
                    found_season is not None
                    and int(found_season)
                    == wanted_season
                ):
                    score += 1.0

                if (
                    found_episode is not None
                    and int(found_episode)
                    == wanted_episode
                ):
                    score += 2.0

                display = tv_display(
                    identity
                )

            matches.append(
                {
                    "provider": self.name,
                    "title": display,
                    "release": release,
                    "file_id": file_id,
                    "score": score,
                    "media_kind":
                        identity["kind"],
                }
            )

        if not matches:
            return None

        matches.sort(
            key=lambda item:
                item["score"],
            reverse=True,
        )

        return matches[0]


    def search(
        self,
        video,
        identity,
    ):
        if identity["kind"] == "tv":
            return self.search_tv(
                video,
                identity,
            )

        return self.search_movie(
            video,
            identity,
        )


    def download(
        self,
        result,
        destination,
    ):
        response = http_json(
            self.base_url
            + "/api/v1/download",
            method="POST",
            headers=self.headers(
                authenticated=True
            ),
            data={
                "file_id":
                    result["file_id"]
            },
        )

        link = response.get(
            "link"
        )

        if not link:
            raise RuntimeError(
                "OpenSubtitles returned "
                "no download URL."
            )

        data = http_bytes(
            link,
            headers={
                "User-Agent":
                    USER_AGENT
            },
        )

        if not data:
            raise RuntimeError(
                "Downloaded subtitle "
                "was empty."
            )

        destination.write_bytes(
            data
        )


class SubDLProvider(
    SubtitleProvider
):
    name = "SubDL"

    def __init__(
        self,
        api_key,
    ):
        self.api_key = api_key


    def login(self):
        if not self.api_key:
            raise RuntimeError(
                "SubDL API key is missing."
            )


    def request_subtitles(
        self,
        parameters,
    ):
        parameters = dict(
            parameters
        )

        parameters[
            "api_key"
        ] = self.api_key

        parameters[
            "client"
        ] = "custom_integration"

        url = (
            "https://api.subdl.com"
            "/api/v1/subtitles?"
            + urllib.parse.urlencode(
                parameters
            )
        )

        result = http_json(
            url,
            headers={
                "Accept":
                    "application/json",
                "User-Agent":
                    USER_AGENT,
            },
        )

        if result.get(
            "status"
        ) is False:
            error = result.get(
                "error"
            )
            if error:
                raise RuntimeError(
                    error
                )
            return {}

        return result


    def search_movie(
        self,
        video,
        identity,
    ):
        title = identity.get(
            "title",
            "",
        )

        if not title:
            return None

        params = {
            "film_name": title,
            "type": "movie",
            "languages": SUBDL_LANGUAGE,
            "subs_per_page": "30",
            "releases": "1",
            "unpack": "1",
        }

        year = identity.get(
            "year"
        )

        if year:
            params["year"] = str(
                year
            )

        data = self.request_subtitles(
            params
        )

        if (
            not data.get(
                "subtitles"
            )
            and year
        ):
            params.pop(
                "year",
                None,
            )
            data = self.request_subtitles(
                params
            )

        return self.choose_result(
            video,
            identity,
            data,
        )


    def search_tv(
        self,
        video,
        identity,
    ):
        series = identity.get(
            "series",
            "",
        )

        season = identity.get(
            "season"
        )

        episode = identity.get(
            "episode"
        )

        if (
            not series
            or season is None
            or episode is None
        ):
            return None

        params = {
            "film_name": series,
            "file_name": video.name,
            "type": "tv",
            "season_number": str(
                season
            ),
            "episode_number": str(
                episode
            ),
            "languages": SUBDL_LANGUAGE,
            "subs_per_page": "30",
            "releases": "1",
            "unpack": "1",
        }

        data = self.request_subtitles(
            params
        )

        if not data.get(
            "subtitles"
        ):
            # Some collections behave better without file_name.
            params.pop(
                "file_name",
                None,
            )

            data = self.request_subtitles(
                params
            )

        return self.choose_result(
            video,
            identity,
            data,
        )


    def choose_result(
        self,
        video,
        identity,
        data,
    ):
        subtitles = data.get(
            "subtitles",
            [],
        )

        results = data.get(
            "results",
            [],
        )

        if not subtitles:
            return None

        movie_or_show_name = ""

        if results:
            movie_or_show_name = (
                results[0].get(
                    "name"
                )
                or ""
            )

        candidates = []

        for subtitle in subtitles:
            release = (
                subtitle.get(
                    "release_name"
                )
                or subtitle.get(
                    "name"
                )
                or ""
            )

            score = release_similarity(
                video.stem,
                release,
            )

            chosen_url = subtitle.get(
                "url"
            )

            chosen_name = subtitle.get(
                "name",
                "",
            )

            direct_raw = False

            if identity["kind"] == "tv":
                wanted_season = identity[
                    "season"
                ]
                wanted_episode = identity[
                    "episode"
                ]

                subtitle_season = subtitle.get(
                    "season"
                )
                subtitle_episode = subtitle.get(
                    "episode"
                )

                if (
                    subtitle_season is not None
                    and int(subtitle_season)
                    == wanted_season
                ):
                    score += 1.0

                if (
                    subtitle_episode is not None
                    and int(subtitle_episode)
                    == wanted_episode
                ):
                    score += 2.0

                best_unpack = None

                for unpacked in subtitle.get(
                    "unpack_files",
                    [],
                ):
                    language = (
                        unpacked.get(
                            "language"
                        )
                        or ""
                    ).upper()

                    if (
                        language
                        and language
                        != SUBDL_LANGUAGE
                    ):
                        continue

                    unpack_season = unpacked.get(
                        "season"
                    )

                    unpack_episode = unpacked.get(
                        "episode"
                    )

                    if (
                        unpack_season is not None
                        and int(unpack_season)
                        != wanted_season
                    ):
                        continue

                    if (
                        unpack_episode is not None
                        and int(unpack_episode)
                        != wanted_episode
                    ):
                        continue

                    # If season/episode metadata is missing,
                    # inspect the unpacked filename as a fallback.
                    if (
                        unpack_episode is None
                        or unpack_season is None
                    ):
                        name = (
                            unpacked.get(
                                "release_name"
                            )
                            or unpacked.get(
                                "name"
                            )
                            or ""
                        )

                        parsed = parse_tv_from_filename(
                            Path(name)
                        )

                        if parsed:
                            if (
                                parsed.get(
                                    "season"
                                )
                                != wanted_season
                                or parsed.get(
                                    "episode"
                                )
                                != wanted_episode
                            ):
                                continue

                    unpack_release = (
                        unpacked.get(
                            "release_name"
                        )
                        or unpacked.get(
                            "name"
                        )
                        or ""
                    )

                    unpack_score = (
                        release_similarity(
                            video.stem,
                            unpack_release,
                        )
                    )

                    if (
                        best_unpack is None
                        or unpack_score
                        > best_unpack["score"]
                    ):
                        best_unpack = {
                            "url":
                                unpacked.get(
                                    "url"
                                ),
                            "name":
                                unpacked.get(
                                    "name",
                                    "",
                                ),
                            "release":
                                unpack_release,
                            "score":
                                unpack_score,
                        }

                if best_unpack:
                    chosen_url = best_unpack[
                        "url"
                    ]
                    chosen_name = best_unpack[
                        "name"
                    ]
                    release = best_unpack[
                        "release"
                    ]
                    score += (
                        best_unpack[
                            "score"
                        ]
                        + 3.0
                    )
                    direct_raw = True

                else:
                    # Never blindly select a full-season archive
                    # when we cannot identify the requested episode.
                    is_full_season = bool(
                        subtitle.get(
                            "full_season"
                        )
                    )

                    episode_from = subtitle.get(
                        "episode_from"
                    )

                    episode_end = subtitle.get(
                        "episode_end"
                    )

                    if (
                        is_full_season
                        or (
                            episode_from is not None
                            and episode_end is not None
                            and int(episode_from)
                            != int(episode_end)
                        )
                    ):
                        continue

                    # Normal single-episode subtitle must match
                    # episode metadata if metadata is present.
                    if (
                        subtitle_episode is not None
                        and int(subtitle_episode)
                        != wanted_episode
                    ):
                        continue

                    if (
                        subtitle_season is not None
                        and int(subtitle_season)
                        != wanted_season
                    ):
                        continue

                display = tv_display(
                    identity
                )

            else:
                year = identity.get(
                    "year"
                )

                if (
                    year
                    and results
                    and results[0].get(
                        "year"
                    ) == year
                ):
                    score += 1.0

                display = (
                    movie_or_show_name
                    or identity.get(
                        "title",
                        ""
                    )
                )

            if not chosen_url:
                continue

            candidates.append(
                {
                    "provider": self.name,
                    "title": display,
                    "release": release,
                    "url": chosen_url,
                    "filename": chosen_name,
                    "direct_raw": direct_raw,
                    "score": score,
                    "media_kind":
                        identity["kind"],
                }
            )

        if not candidates:
            return None

        candidates.sort(
            key=lambda item:
                item["score"],
            reverse=True,
        )

        return candidates[0]


    def search(
        self,
        video,
        identity,
    ):
        if identity["kind"] == "tv":
            return self.search_tv(
                video,
                identity,
            )

        return self.search_movie(
            video,
            identity,
        )


    def make_download_url(
        self,
        url,
    ):
        if not url:
            return None

        if (
            url.startswith(
                "http://"
            )
            or url.startswith(
                "https://"
            )
        ):
            return url

        if not url.startswith(
            "/"
        ):
            url = "/" + url

        return (
            "https://dl.subdl.com"
            + url
        )


    def select_zip_subtitle(
        self,
        data,
        identity,
    ):
        try:
            archive = zipfile.ZipFile(
                io.BytesIO(
                    data
                )
            )
        except zipfile.BadZipFile as error:
            raise RuntimeError(
                "SubDL returned an "
                "invalid ZIP archive."
            ) from error

        choices = []

        for name in archive.namelist():
            if (
                Path(name).suffix.lower()
                not in SUBTITLE_EXTENSIONS
            ):
                continue

            score = 0.0

            if identity["kind"] == "tv":
                parsed = parse_tv_from_filename(
                    Path(name)
                )

                if parsed:
                    if (
                        parsed.get(
                            "season"
                        )
                        == identity.get(
                            "season"
                        )
                        and parsed.get(
                            "episode"
                        )
                        == identity.get(
                            "episode"
                        )
                    ):
                        score += 10.0
                    else:
                        continue

                # Episode number fallback for archives that use
                # simple names such as "23 - Roland.srt".
                episode = identity.get(
                    "episode"
                )

                if re.search(
                    rf"(?<!\d)0*{episode}(?!\d)",
                    Path(name).stem,
                ):
                    score += 3.0

            score += release_similarity(
                Path(name).stem,
                identity.get(
                    "series",
                    identity.get(
                        "title",
                        "",
                    ),
                ),
            )

            if Path(name).suffix.lower() == ".srt":
                score += 0.25

            choices.append(
                (
                    score,
                    name,
                )
            )

        if not choices:
            raise RuntimeError(
                "No usable subtitle file "
                "was found in the archive."
            )

        choices.sort(
            key=lambda item:
                item[0],
            reverse=True,
        )

        return archive.read(
            choices[0][1]
        )


    def download(
        self,
        result,
        destination,
    ):
        url = self.make_download_url(
            result.get(
                "url"
            )
        )

        if not url:
            raise RuntimeError(
                "SubDL returned no "
                "download URL."
            )

        data = http_bytes(
            url,
            headers={
                "User-Agent":
                    USER_AGENT
            },
        )

        if not data:
            raise RuntimeError(
                "SubDL returned an "
                "empty download."
            )

        if data.startswith(
            b"PK"
        ):
            identity = result.get(
                "_identity",
                {
                    "kind": "movie",
                },
            )

            data = self.select_zip_subtitle(
                data,
                identity,
            )

        destination.write_bytes(
            data
        )


# ============================================================
# UI helpers
# ============================================================

class NeonProgress(tk.Canvas):
    def __init__(
        self,
        parent,
        height=7,
    ):
        super().__init__(
            parent,
            height=height,
            bg="#19202B",
            highlightthickness=0,
            borderwidth=0,
        )

        self.value = 0.0

        self.bind(
            "<Configure>",
            self.redraw,
        )


    def set(
        self,
        value,
    ):
        try:
            value = float(value)
        except Exception:
            value = 0.0

        self.value = max(
            0.0,
            min(
                100.0,
                value,
            ),
        )

        self.redraw()


    def redraw(
        self,
        event=None,
    ):
        self.delete("all")

        width = self.winfo_width()
        height = self.winfo_height()

        if width <= 1 or height <= 1:
            return

        self.create_rectangle(
            0,
            0,
            width,
            height,
            fill="#19202B",
            outline="",
        )

        fill_width = int(
            width
            * self.value
            / 100.0
        )

        if fill_width > 0:
            self.create_rectangle(
                0,
                0,
                fill_width,
                height,
                fill=ACCENT,
                outline="",
            )


# ============================================================
# Application
# ============================================================

class SubDubApp:

    def __init__(
        self,
        root,
    ):
        self.root = root
        self.events = queue.Queue()
        self.cancel_event = threading.Event()

        self.running = False
        self.save_job = None

        self.video_records = []
        self.with_subs_records = []
        self.missing_records = []
        self.download_records = []
        self.failed_records = []

        settings = load_settings()

        self.folder_var = tk.StringVar(
            value=settings[
                "last_folder"
            ]
        )

        self.opensub_enabled_var = tk.BooleanVar(
            value=settings[
                "opensubtitles_enabled"
            ]
        )

        self.subdl_enabled_var = tk.BooleanVar(
            value=settings[
                "subdl_enabled"
            ]
        )

        self.provider_order_var = tk.StringVar(
            value=settings[
                "provider_order"
            ]
        )

        self.opensub_username_var = tk.StringVar(
            value=settings[
                "opensubtitles_username"
            ]
        )

        self.opensub_password_var = tk.StringVar(
            value=settings[
                "opensubtitles_password"
            ]
        )

        self.opensub_api_var = tk.StringVar(
            value=settings[
                "opensubtitles_api_key"
            ]
        )

        self.subdl_api_var = tk.StringVar(
            value=settings[
                "subdl_api_key"
            ]
        )

        self.show_credentials_var = tk.BooleanVar(
            value=False
        )

        self.status_var = tk.StringVar(
            value="READY"
        )

        self.current_file_var = tk.StringVar(
            value=(
                "Choose your media library "
                "to begin."
            )
        )

        self.total_var = tk.StringVar(
            value="0"
        )

        self.have_var = tk.StringVar(
            value="0"
        )

        self.missing_var = tk.StringVar(
            value="0"
        )

        self.downloaded_var = tk.StringVar(
            value="0"
        )

        self.failed_var = tk.StringVar(
            value="0"
        )

        self.provider_tab = "opensubtitles"

        self.setup_window()
        self.setup_tree_style()
        self.build_ui()
        self.install_auto_save()

        self.root.after(
            75,
            self.process_events,
        )

        self.root.protocol(
            "WM_DELETE_WINDOW",
            self.on_close,
        )


    # --------------------------------------------------------
    # Window / style
    # --------------------------------------------------------

    def setup_window(self):
        self.root.title(
            f"{APP_NAME} {APP_VERSION}"
        )

        self.root.geometry(
            "1400x900"
        )

        self.root.minsize(
            1100,
            720,
        )

        self.root.configure(
            bg=BG
        )

        if os.name == "nt":
            try:
                self.root.state(
                    "zoomed"
                )
            except Exception:
                pass


    def setup_tree_style(self):
        style = ttk.Style()

        try:
            style.theme_use(
                "clam"
            )
        except Exception:
            pass

        style.configure(
            "SubDub.Treeview",
            background=TREE_BG,
            fieldbackground=TREE_BG,
            foreground=TEXT,
            rowheight=30,
            borderwidth=0,
            relief="flat",
            font=(
                "Segoe UI",
                9,
            ),
        )

        style.map(
            "SubDub.Treeview",
            background=[
                (
                    "selected",
                    ACCENT_DARK,
                )
            ],
            foreground=[
                (
                    "selected",
                    WHITE,
                )
            ],
        )

        style.configure(
            "SubDub.Treeview.Heading",
            background=PANEL_ALT,
            foreground=TEXT,
            borderwidth=0,
            relief="flat",
            font=(
                "Segoe UI",
                9,
                "bold",
            ),
            padding=8,
        )

        style.map(
            "SubDub.Treeview.Heading",
            background=[
                (
                    "active",
                    CARD_HOVER,
                )
            ],
            foreground=[
                (
                    "active",
                    WHITE,
                )
            ],
        )


    # --------------------------------------------------------
    # Controls
    # --------------------------------------------------------

    def make_button(
        self,
        parent,
        text,
        command,
        bg,
        hover,
        fg=WHITE,
        padx=18,
        pady=10,
        font_size=10,
    ):
        button = tk.Button(
            parent,
            text=text,
            command=command,
            bg=bg,
            fg=fg,
            activebackground=hover,
            activeforeground=fg,
            disabledforeground=DISABLED_FG,
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            cursor="hand2",
            font=(
                "Segoe UI",
                font_size,
                "bold",
            ),
            padx=padx,
            pady=pady,
        )

        button._normal_bg = bg
        button._normal_fg = fg

        return button


    def set_button_enabled(
        self,
        button,
        enabled,
    ):
        if enabled:
            button.configure(
                state="normal",
                bg=button._normal_bg,
                fg=button._normal_fg,
                cursor="hand2",
            )
        else:
            button.configure(
                state="disabled",
                bg=DISABLED_BG,
                fg=DISABLED_FG,
                cursor="arrow",
            )


    def make_entry(
        self,
        parent,
        variable,
        show=None,
    ):
        return tk.Entry(
            parent,
            textvariable=variable,
            show=show,
            bg=ENTRY_BG,
            fg=TEXT,
            insertbackground=TEXT,
            selectbackground=ACCENT,
            selectforeground=WHITE,
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
            font=(
                "Segoe UI",
                10,
            ),
        )


    def make_check(
        self,
        parent,
        text,
        variable,
        command=None,
    ):
        return tk.Checkbutton(
            parent,
            text=text,
            variable=variable,
            command=command,
            bg=PANEL,
            fg=TEXT_SECONDARY,
            activebackground=PANEL,
            activeforeground=TEXT,
            selectcolor=ENTRY_BG,
            disabledforeground=TEXT_MUTED,
            highlightthickness=0,
            borderwidth=0,
            font=(
                "Segoe UI",
                10,
            ),
        )


    def make_dropdown(
        self,
        parent,
        variable,
        values,
    ):
        dropdown = tk.OptionMenu(
            parent,
            variable,
            *values,
        )

        dropdown.configure(
            bg=ENTRY_BG,
            fg=TEXT,
            activebackground=CARD_HOVER,
            activeforeground=WHITE,
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=BORDER,
            font=(
                "Segoe UI",
                10,
            ),
            anchor="w",
        )

        dropdown["menu"].configure(
            bg=PANEL_ALT,
            fg=TEXT,
            activebackground=ACCENT,
            activeforeground=WHITE,
            borderwidth=0,
            font=(
                "Segoe UI",
                10,
            ),
        )

        return dropdown


    # --------------------------------------------------------
    # Main UI
    # --------------------------------------------------------

    def build_ui(self):
        root_panel = tk.Frame(
            self.root,
            bg=BG,
        )

        root_panel.pack(
            fill="both",
            expand=True,
        )

        self.build_topbar(
            root_panel
        )

        content = tk.Frame(
            root_panel,
            bg=BG,
        )

        content.pack(
            fill="both",
            expand=True,
            padx=20,
            pady=(0, 20),
        )

        content.grid_rowconfigure(
            0,
            weight=1,
        )

        content.grid_columnconfigure(
            0,
            weight=1,
        )

        main = tk.Frame(
            content,
            bg=BG,
        )

        main.grid(
            row=0,
            column=0,
            sticky="nsew",
            padx=(0, 16),
        )

        main.grid_columnconfigure(
            0,
            weight=1,
        )

        main.grid_rowconfigure(
            4,
            weight=1,
        )

        sidebar = tk.Frame(
            content,
            bg=PANEL,
            width=340,
            highlightthickness=1,
            highlightbackground=BORDER,
        )

        sidebar.grid(
            row=0,
            column=1,
            sticky="ns",
        )

        sidebar.grid_propagate(
            False
        )

        self.build_library_bar(
            main
        )
        self.build_stats(
            main
        )
        self.build_status(
            main
        )
        self.build_progress(
            main
        )
        self.build_activity(
            main
        )
        self.build_sidebar(
            sidebar
        )


    def build_topbar(
        self,
        parent,
    ):
        top = tk.Frame(
            parent,
            bg=TOPBAR,
            height=82,
        )

        top.pack(
            fill="x"
        )

        top.pack_propagate(
            False
        )

        tk.Frame(
            top,
            bg=ACCENT,
            width=4,
        ).pack(
            side="left",
            fill="y",
        )

        brand = tk.Frame(
            top,
            bg=TOPBAR,
        )

        brand.pack(
            side="left",
            padx=20,
            pady=13,
        )

        title_line = tk.Frame(
            brand,
            bg=TOPBAR,
        )

        title_line.pack(
            anchor="w"
        )

        tk.Label(
            title_line,
            text="SUBDUB",
            bg=TOPBAR,
            fg=TEXT,
            font=(
                "Segoe UI",
                24,
                "bold",
            ),
        ).pack(
            side="left"
        )

        tk.Label(
            title_line,
            text=(
                "  /  SUBTITLE INTELLIGENCE"
            ),
            bg=TOPBAR,
            fg=CYAN,
            font=(
                "Segoe UI",
                9,
                "bold",
            ),
        ).pack(
            side="left",
            pady=(8, 0),
        )

        tk.Label(
            brand,
            text=(
                "Movies + TV · multi-source subtitle "
                "management"
            ),
            bg=TOPBAR,
            fg=TEXT_SECONDARY,
            font=(
                "Segoe UI",
                9,
            ),
        ).pack(
            anchor="w"
        )

        right = tk.Frame(
            top,
            bg=TOPBAR,
        )

        right.pack(
            side="right",
            padx=22,
        )

        tk.Label(
            right,
            text="FREE  •  OPEN SOURCE",
            bg=TOPBAR,
            fg=GREEN,
            font=(
                "Segoe UI",
                9,
                "bold",
            ),
        ).pack(
            anchor="e"
        )

        tk.Label(
            right,
            text=f"VERSION {APP_VERSION}",
            bg=TOPBAR,
            fg=TEXT_MUTED,
            font=(
                "Segoe UI",
                8,
            ),
        ).pack(
            anchor="e",
            pady=(3, 0),
        )


    def build_library_bar(
        self,
        parent,
    ):
        panel = tk.Frame(
            parent,
            bg=PANEL,
            highlightthickness=1,
            highlightbackground=BORDER,
        )

        panel.grid(
            row=0,
            column=0,
            sticky="ew",
            pady=(16, 0),
        )

        library_row = tk.Frame(
            panel,
            bg=PANEL,
        )

        library_row.pack(
            fill="x",
            padx=15,
            pady=(14, 10),
        )

        tk.Label(
            library_row,
            text="MEDIA LIBRARY",
            bg=PANEL,
            fg=TEXT_SECONDARY,
            font=(
                "Segoe UI",
                9,
                "bold",
            ),
        ).pack(
            side="left",
            padx=(0, 12),
        )

        self.folder_entry = (
            self.make_entry(
                library_row,
                self.folder_var,
            )
        )

        self.folder_entry.pack(
            side="left",
            fill="x",
            expand=True,
            ipady=7,
            padx=(0, 8),
        )

        self.browse_button = (
            self.make_button(
                library_row,
                "BROWSE",
                self.choose_folder,
                CARD,
                CARD_HOVER,
                padx=15,
                pady=8,
                font_size=9,
            )
        )

        self.browse_button.pack(
            side="left",
            padx=(0, 8),
        )

        self.open_folder_button = (
            self.make_button(
                library_row,
                "OPEN",
                self.open_folder,
                CARD,
                CARD_HOVER,
                padx=15,
                pady=8,
                font_size=9,
            )
        )

        self.open_folder_button.pack(
            side="left"
        )

        actions = tk.Frame(
            panel,
            bg=PANEL,
        )

        actions.pack(
            fill="x",
            padx=15,
            pady=(0, 14),
        )

        self.scan_button = (
            self.make_button(
                actions,
                "SCAN LIBRARY",
                self.start_scan,
                PANEL_ALT,
                CARD_HOVER,
            )
        )

        self.scan_button.pack(
            side="left",
            padx=(0, 10),
        )

        self.download_button = (
            self.make_button(
                actions,
                "SCAN + DOWNLOAD MISSING",
                self.start_download,
                ACCENT,
                ACCENT_HOVER,
            )
        )

        self.download_button.pack(
            side="left",
            padx=(0, 10),
        )

        self.cancel_button = (
            self.make_button(
                actions,
                "CANCEL",
                self.cancel,
                "#4A252C",
                "#63313A",
            )
        )

        self.cancel_button.pack(
            side="left"
        )

        self.set_button_enabled(
            self.cancel_button,
            False,
        )

        self.clear_button = (
            self.make_button(
                actions,
                "CLEAR ACTIVITY",
                self.clear_log,
                PANEL_ALT,
                CARD_HOVER,
                fg=TEXT_SECONDARY,
                padx=14,
                font_size=9,
            )
        )

        self.clear_button.pack(
            side="right"
        )


    def build_stats(
        self,
        parent,
    ):
        container = tk.Frame(
            parent,
            bg=BG,
        )

        container.grid(
            row=1,
            column=0,
            sticky="ew",
            pady=(14, 0),
        )

        for column in range(5):
            container.grid_columnconfigure(
                column,
                weight=1,
            )

        definitions = [
            (
                "VIDEOS",
                self.total_var,
                BLUE,
                "videos",
                "Movies + episodes",
            ),
            (
                "WITH SUBS",
                self.have_var,
                GREEN,
                "with_subs",
                "Embedded or external",
            ),
            (
                "MISSING",
                self.missing_var,
                ORANGE,
                "missing",
                "Need subtitles",
            ),
            (
                "DOWNLOADED",
                self.downloaded_var,
                ACCENT,
                "downloaded",
                "Added this run",
            ),
            (
                "FAILED",
                self.failed_var,
                RED,
                "failed",
                "Review problems",
            ),
        ]

        for index, definition in enumerate(
            definitions
        ):
            (
                title,
                variable,
                color,
                stat_type,
                description,
            ) = definition

            card = tk.Frame(
                container,
                bg=CARD,
                cursor="hand2",
                highlightthickness=1,
                highlightbackground=BORDER,
            )

            card.grid(
                row=0,
                column=index,
                sticky="ew",
                padx=(
                    0 if index == 0 else 5,
                    0 if index == 4 else 5,
                ),
            )

            strip = tk.Frame(
                card,
                bg=color,
                height=3,
            )

            strip.pack(
                fill="x"
            )

            inner = tk.Frame(
                card,
                bg=CARD,
                padx=14,
                pady=13,
            )

            inner.pack(
                fill="both",
                expand=True,
            )

            title_label = tk.Label(
                inner,
                text=title,
                bg=CARD,
                fg=TEXT_SECONDARY,
                font=(
                    "Segoe UI",
                    8,
                    "bold",
                ),
            )

            title_label.pack(
                anchor="w"
            )

            value_label = tk.Label(
                inner,
                textvariable=variable,
                bg=CARD,
                fg=TEXT,
                font=(
                    "Segoe UI",
                    25,
                    "bold",
                ),
            )

            value_label.pack(
                anchor="w",
                pady=(2, 0),
            )

            description_label = tk.Label(
                inner,
                text=description,
                bg=CARD,
                fg=TEXT_MUTED,
                font=(
                    "Segoe UI",
                    8,
                ),
            )

            description_label.pack(
                anchor="w",
                pady=(1, 6),
            )

            detail_label = tk.Label(
                inner,
                text="VIEW DETAILS  →",
                bg=CARD,
                fg=color,
                font=(
                    "Segoe UI",
                    8,
                    "bold",
                ),
            )

            detail_label.pack(
                anchor="w"
            )

            colored_widgets = [
                title_label,
                value_label,
                description_label,
                detail_label,
            ]

            def click_handler(
                event,
                kind=stat_type,
            ):
                self.show_stat_details(
                    kind
                )

            def enter_handler(
                event,
                frame=card,
                body=inner,
                labels=colored_widgets,
            ):
                frame.configure(
                    bg=CARD_HOVER
                )
                body.configure(
                    bg=CARD_HOVER
                )
                for label in labels:
                    label.configure(
                        bg=CARD_HOVER
                    )

            def leave_handler(
                event,
                frame=card,
                body=inner,
                labels=colored_widgets,
            ):
                frame.configure(
                    bg=CARD
                )
                body.configure(
                    bg=CARD
                )
                for label in labels:
                    label.configure(
                        bg=CARD
                    )

            for widget in [
                card,
                strip,
                inner,
                *colored_widgets,
            ]:
                widget.bind(
                    "<Button-1>",
                    click_handler,
                )
                widget.bind(
                    "<Enter>",
                    enter_handler,
                )
                widget.bind(
                    "<Leave>",
                    leave_handler,
                )


    def build_status(
        self,
        parent,
    ):
        panel = tk.Frame(
            parent,
            bg=PANEL,
            highlightthickness=1,
            highlightbackground=BORDER,
        )

        panel.grid(
            row=2,
            column=0,
            sticky="ew",
            pady=(14, 0),
        )

        tk.Frame(
            panel,
            bg=GREEN,
            width=4,
        ).pack(
            side="left",
            fill="y",
        )

        text_panel = tk.Frame(
            panel,
            bg=PANEL,
        )

        text_panel.pack(
            side="left",
            fill="x",
            expand=True,
            padx=14,
            pady=11,
        )

        tk.Label(
            text_panel,
            textvariable=self.status_var,
            bg=PANEL,
            fg=TEXT,
            font=(
                "Segoe UI",
                10,
                "bold",
            ),
        ).pack(
            anchor="w"
        )

        tk.Label(
            text_panel,
            textvariable=self.current_file_var,
            bg=PANEL,
            fg=TEXT_SECONDARY,
            font=(
                "Segoe UI",
                9,
            ),
        ).pack(
            anchor="w",
            pady=(3, 0),
        )


    def build_progress(
        self,
        parent,
    ):
        panel = tk.Frame(
            parent,
            bg=PANEL,
            highlightthickness=1,
            highlightbackground=BORDER,
        )

        panel.grid(
            row=3,
            column=0,
            sticky="ew",
            pady=(14, 0),
        )

        inside = tk.Frame(
            panel,
            bg=PANEL,
        )

        inside.pack(
            fill="x",
            padx=14,
            pady=12,
        )

        tk.Label(
            inside,
            text="LIBRARY SCAN",
            bg=PANEL,
            fg=TEXT_SECONDARY,
            font=(
                "Segoe UI",
                8,
                "bold",
            ),
        ).pack(
            anchor="w"
        )

        self.scan_progress = NeonProgress(
            inside,
            height=7,
        )

        self.scan_progress.pack(
            fill="x",
            pady=(6, 12),
        )

        tk.Label(
            inside,
            text="SUBTITLE DOWNLOADS",
            bg=PANEL,
            fg=TEXT_SECONDARY,
            font=(
                "Segoe UI",
                8,
                "bold",
            ),
        ).pack(
            anchor="w"
        )

        self.download_progress = NeonProgress(
            inside,
            height=7,
        )

        self.download_progress.pack(
            fill="x",
            pady=(6, 0),
        )


    def build_activity(
        self,
        parent,
    ):
        panel = tk.Frame(
            parent,
            bg=PANEL,
            highlightthickness=1,
            highlightbackground=BORDER,
        )

        panel.grid(
            row=4,
            column=0,
            sticky="nsew",
            pady=(14, 0),
        )

        header = tk.Frame(
            panel,
            bg=PANEL,
        )

        header.pack(
            fill="x",
            padx=14,
            pady=(11, 8),
        )

        tk.Label(
            header,
            text="ACTIVITY STREAM",
            bg=PANEL,
            fg=TEXT,
            font=(
                "Segoe UI",
                10,
                "bold",
            ),
        ).pack(
            side="left"
        )

        tk.Label(
            header,
            text="LIVE",
            bg=PANEL,
            fg=GREEN,
            font=(
                "Segoe UI",
                8,
                "bold",
            ),
        ).pack(
            side="right"
        )

        log_area = tk.Frame(
            panel,
            bg=TREE_BG,
        )

        log_area.pack(
            fill="both",
            expand=True,
            padx=1,
            pady=(0, 1),
        )

        self.log_text = tk.Text(
            log_area,
            bg=TREE_BG,
            fg="#D6DDEA",
            insertbackground=WHITE,
            selectbackground=ACCENT_DARK,
            selectforeground=WHITE,
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            font=(
                "Consolas",
                9,
            ),
            padx=13,
            pady=12,
            wrap="word",
        )

        scrollbar = ttk.Scrollbar(
            log_area,
            orient="vertical",
            command=self.log_text.yview,
        )

        self.log_text.configure(
            yscrollcommand=scrollbar.set
        )

        scrollbar.pack(
            side="right",
            fill="y",
        )

        self.log_text.pack(
            side="left",
            fill="both",
            expand=True,
        )


    def build_sidebar(
        self,
        parent,
    ):
        heading = tk.Frame(
            parent,
            bg=PANEL,
        )

        heading.pack(
            fill="x",
            padx=16,
            pady=(17, 0),
        )

        tk.Label(
            heading,
            text="SOURCE CONTROL",
            bg=PANEL,
            fg=TEXT,
            font=(
                "Segoe UI",
                12,
                "bold",
            ),
        ).pack(
            anchor="w"
        )

        tk.Label(
            heading,
            text=(
                "Movie + TV providers, priority "
                "and authentication."
            ),
            bg=PANEL,
            fg=TEXT_MUTED,
            font=(
                "Segoe UI",
                9,
            ),
            justify="left",
            wraplength=285,
        ).pack(
            anchor="w",
            pady=(4, 14),
        )

        provider_controls = tk.Frame(
            parent,
            bg=PANEL,
        )

        provider_controls.pack(
            fill="x",
            padx=16,
        )

        self.make_check(
            provider_controls,
            "Enable OpenSubtitles",
            self.opensub_enabled_var,
        ).pack(
            anchor="w",
            pady=3,
        )

        self.make_check(
            provider_controls,
            "Enable SubDL",
            self.subdl_enabled_var,
        ).pack(
            anchor="w",
            pady=3,
        )

        priority_panel = tk.Frame(
            parent,
            bg=PANEL,
        )

        priority_panel.pack(
            fill="x",
            padx=16,
            pady=(15, 0),
        )

        tk.Label(
            priority_panel,
            text="SEARCH PRIORITY",
            bg=PANEL,
            fg=TEXT_MUTED,
            font=(
                "Segoe UI",
                8,
                "bold",
            ),
        ).pack(
            anchor="w",
            pady=(0, 5),
        )

        self.provider_dropdown = (
            self.make_dropdown(
                priority_panel,
                self.provider_order_var,
                [
                    "OpenSubtitles first",
                    "SubDL first",
                ],
            )
        )

        self.provider_dropdown.pack(
            fill="x"
        )

        tk.Frame(
            parent,
            bg=BORDER,
            height=1,
        ).pack(
            fill="x",
            padx=16,
            pady=18,
        )

        tabs = tk.Frame(
            parent,
            bg=PANEL,
        )

        tabs.pack(
            fill="x",
            padx=16,
        )

        self.opensub_tab_button = (
            self.make_button(
                tabs,
                "OpenSubtitles",
                lambda:
                    self.show_provider_tab(
                        "opensubtitles"
                    ),
                ACCENT,
                ACCENT_HOVER,
                padx=10,
                pady=8,
                font_size=9,
            )
        )

        self.opensub_tab_button.pack(
            side="left",
            fill="x",
            expand=True,
            padx=(0, 4),
        )

        self.subdl_tab_button = (
            self.make_button(
                tabs,
                "SubDL",
                lambda:
                    self.show_provider_tab(
                        "subdl"
                    ),
                CARD,
                CARD_HOVER,
                fg=TEXT_SECONDARY,
                padx=10,
                pady=8,
                font_size=9,
            )
        )

        self.subdl_tab_button.pack(
            side="left",
            fill="x",
            expand=True,
            padx=(4, 0),
        )

        self.provider_content = tk.Frame(
            parent,
            bg=PANEL,
        )

        self.provider_content.pack(
            fill="both",
            expand=True,
            padx=16,
            pady=(14, 0),
        )

        self.opensub_panel = tk.Frame(
            self.provider_content,
            bg=PANEL,
        )

        self.subdl_panel = tk.Frame(
            self.provider_content,
            bg=PANEL,
        )

        self.build_opensub_panel(
            self.opensub_panel
        )

        self.build_subdl_panel(
            self.subdl_panel
        )

        self.show_provider_tab(
            "opensubtitles"
        )

        bottom = tk.Frame(
            parent,
            bg=PANEL,
        )

        bottom.pack(
            fill="x",
            padx=16,
            pady=(0, 17),
        )

        tk.Frame(
            bottom,
            bg=BORDER,
            height=1,
        ).pack(
            fill="x",
            pady=(0, 13),
        )

        self.show_credentials_check = (
            self.make_check(
                bottom,
                "Show credentials",
                self.show_credentials_var,
                command=self.toggle_credentials,
            )
        )

        self.show_credentials_check.pack(
            anchor="w"
        )

        tk.Label(
            bottom,
            text=(
                "SETTINGS AUTO-SAVED\n"
                "Credentials protected with "
                "Windows DPAPI"
            ),
            bg=PANEL,
            fg=GREEN,
            font=(
                "Segoe UI",
                8,
                "bold",
            ),
            justify="left",
        ).pack(
            anchor="w",
            pady=(12, 0),
        )


    def build_opensub_panel(
        self,
        parent,
    ):
        tk.Label(
            parent,
            text="OPENSUBTITLES ACCOUNT",
            bg=PANEL,
            fg=TEXT_SECONDARY,
            font=(
                "Segoe UI",
                8,
                "bold",
            ),
        ).pack(
            anchor="w",
            pady=(0, 12),
        )

        self.opensub_username_entry = (
            self.build_labeled_entry(
                parent,
                "USERNAME",
                self.opensub_username_var,
            )
        )

        self.opensub_password_entry = (
            self.build_labeled_entry(
                parent,
                "PASSWORD",
                self.opensub_password_var,
                show="*",
            )
        )

        self.opensub_api_entry = (
            self.build_labeled_entry(
                parent,
                "API KEY",
                self.opensub_api_var,
                show="*",
            )
        )

        note = tk.Frame(
            parent,
            bg=PANEL_ALT,
            highlightthickness=1,
            highlightbackground=BORDER_SOFT,
        )

        note.pack(
            fill="x",
            pady=(4, 0),
        )

        tk.Label(
            note,
            text="INFO",
            bg=PANEL_ALT,
            fg=CYAN,
            font=(
                "Segoe UI",
                8,
                "bold",
            ),
        ).pack(
            anchor="w",
            padx=10,
            pady=(9, 2),
        )

        tk.Label(
            note,
            text=(
                "Use the OpenSubtitles account "
                "username, not the email address."
            ),
            bg=PANEL_ALT,
            fg=TEXT_SECONDARY,
            font=(
                "Segoe UI",
                8,
            ),
            justify="left",
            wraplength=270,
        ).pack(
            anchor="w",
            padx=10,
            pady=(0, 9),
        )


    def build_subdl_panel(
        self,
        parent,
    ):
        tk.Label(
            parent,
            text="SUBDL ACCOUNT",
            bg=PANEL,
            fg=TEXT_SECONDARY,
            font=(
                "Segoe UI",
                8,
                "bold",
            ),
        ).pack(
            anchor="w",
            pady=(0, 12),
        )

        self.subdl_api_entry = (
            self.build_labeled_entry(
                parent,
                "API KEY",
                self.subdl_api_var,
                show="*",
            )
        )

        note = tk.Frame(
            parent,
            bg=PANEL_ALT,
            highlightthickness=1,
            highlightbackground=BORDER_SOFT,
        )

        note.pack(
            fill="x",
            pady=(4, 0),
        )

        tk.Label(
            note,
            text="TV SUPPORT",
            bg=PANEL_ALT,
            fg=CYAN,
            font=(
                "Segoe UI",
                8,
                "bold",
            ),
        ).pack(
            anchor="w",
            padx=10,
            pady=(9, 2),
        )

        tk.Label(
            note,
            text=(
                "TV searches send show title, "
                "season and episode metadata and "
                "use unpacked season-pack files "
                "when available."
            ),
            bg=PANEL_ALT,
            fg=TEXT_SECONDARY,
            font=(
                "Segoe UI",
                8,
            ),
            justify="left",
            wraplength=270,
        ).pack(
            anchor="w",
            padx=10,
            pady=(0, 9),
        )


    def build_labeled_entry(
        self,
        parent,
        title,
        variable,
        show=None,
    ):
        tk.Label(
            parent,
            text=title,
            bg=PANEL,
            fg=TEXT_MUTED,
            font=(
                "Segoe UI",
                8,
                "bold",
            ),
        ).pack(
            anchor="w",
            pady=(0, 4),
        )

        entry = self.make_entry(
            parent,
            variable,
            show,
        )

        entry.pack(
            fill="x",
            ipady=7,
            pady=(0, 12),
        )

        return entry


    def show_provider_tab(
        self,
        tab,
    ):
        self.provider_tab = tab

        self.opensub_panel.pack_forget()
        self.subdl_panel.pack_forget()

        if tab == "opensubtitles":
            self.opensub_panel.pack(
                fill="both",
                expand=True,
            )

            self.opensub_tab_button.configure(
                bg=ACCENT,
                fg=WHITE,
                activebackground=ACCENT_HOVER,
                activeforeground=WHITE,
            )

            self.opensub_tab_button._normal_bg = (
                ACCENT
            )
            self.opensub_tab_button._normal_fg = (
                WHITE
            )

            self.subdl_tab_button.configure(
                bg=CARD,
                fg=TEXT_SECONDARY,
                activebackground=CARD_HOVER,
                activeforeground=WHITE,
            )

            self.subdl_tab_button._normal_bg = (
                CARD
            )
            self.subdl_tab_button._normal_fg = (
                TEXT_SECONDARY
            )

        else:
            self.subdl_panel.pack(
                fill="both",
                expand=True,
            )

            self.subdl_tab_button.configure(
                bg=ACCENT,
                fg=WHITE,
                activebackground=ACCENT_HOVER,
                activeforeground=WHITE,
            )

            self.subdl_tab_button._normal_bg = (
                ACCENT
            )
            self.subdl_tab_button._normal_fg = (
                WHITE
            )

            self.opensub_tab_button.configure(
                bg=CARD,
                fg=TEXT_SECONDARY,
                activebackground=CARD_HOVER,
                activeforeground=WHITE,
            )

            self.opensub_tab_button._normal_bg = (
                CARD
            )
            self.opensub_tab_button._normal_fg = (
                TEXT_SECONDARY
            )


    # --------------------------------------------------------
    # Detail windows
    # --------------------------------------------------------

    def show_stat_details(
        self,
        stat_type,
    ):
        if stat_type == "videos":
            title = "ALL VIDEOS"
            accent = BLUE
            records = self.video_records
            columns = [
                (
                    "filename",
                    "VIDEO",
                    300,
                ),
                (
                    "media",
                    "MEDIA",
                    260,
                ),
                (
                    "status",
                    "STATUS",
                    150,
                ),
                (
                    "path",
                    "LOCATION",
                    500,
                ),
            ]

        elif stat_type == "with_subs":
            title = "VIDEOS WITH SUBTITLES"
            accent = GREEN
            records = self.with_subs_records
            columns = [
                (
                    "filename",
                    "VIDEO",
                    280,
                ),
                (
                    "media",
                    "MEDIA",
                    240,
                ),
                (
                    "subtitle_type",
                    "TYPE",
                    140,
                ),
                (
                    "details",
                    "SUBTITLE DETAILS",
                    400,
                ),
                (
                    "path",
                    "LOCATION",
                    400,
                ),
            ]

        elif stat_type == "missing":
            title = "MISSING SUBTITLES"
            accent = ORANGE
            records = self.missing_records
            columns = [
                (
                    "filename",
                    "VIDEO",
                    300,
                ),
                (
                    "media",
                    "DETECTED MEDIA",
                    320,
                ),
                (
                    "path",
                    "LOCATION",
                    600,
                ),
            ]

        elif stat_type == "downloaded":
            title = "DOWNLOADED SUBTITLES"
            accent = ACCENT
            records = self.download_records
            columns = [
                (
                    "filename",
                    "VIDEO",
                    260,
                ),
                (
                    "media",
                    "MEDIA",
                    270,
                ),
                (
                    "provider",
                    "SOURCE",
                    120,
                ),
                (
                    "match",
                    "MATCH",
                    280,
                ),
                (
                    "release",
                    "RELEASE",
                    330,
                ),
                (
                    "subtitle",
                    "SAVED SUBTITLE",
                    380,
                ),
            ]

        elif stat_type == "failed":
            title = "FAILED / NOT FOUND"
            accent = RED
            records = self.failed_records
            columns = [
                (
                    "filename",
                    "VIDEO",
                    280,
                ),
                (
                    "media",
                    "DETECTED MEDIA",
                    300,
                ),
                (
                    "reason",
                    "FAILURE DETAILS",
                    520,
                ),
                (
                    "path",
                    "LOCATION",
                    400,
                ),
            ]

        else:
            return

        window = tk.Toplevel(
            self.root
        )

        window.title(
            f"{APP_NAME} — {title}"
        )

        window.geometry(
            "1280x700"
        )

        window.minsize(
            850,
            500,
        )

        window.configure(
            bg=BG
        )

        header = tk.Frame(
            window,
            bg=TOPBAR,
            height=76,
        )

        header.pack(
            fill="x"
        )

        header.pack_propagate(
            False
        )

        tk.Frame(
            header,
            bg=accent,
            width=4,
        ).pack(
            side="left",
            fill="y",
        )

        header_text = tk.Frame(
            header,
            bg=TOPBAR,
        )

        header_text.pack(
            side="left",
            padx=18,
            pady=13,
        )

        tk.Label(
            header_text,
            text=title,
            bg=TOPBAR,
            fg=TEXT,
            font=(
                "Segoe UI",
                18,
                "bold",
            ),
        ).pack(
            anchor="w"
        )

        tk.Label(
            header_text,
            text=(
                f"{len(records)} ITEM"
                if len(records) == 1
                else
                f"{len(records)} ITEMS"
            ),
            bg=TOPBAR,
            fg=accent,
            font=(
                "Segoe UI",
                8,
                "bold",
            ),
        ).pack(
            anchor="w",
            pady=(3, 0),
        )

        body = tk.Frame(
            window,
            bg=BG,
        )

        body.pack(
            fill="both",
            expand=True,
            padx=18,
            pady=18,
        )

        table_panel = tk.Frame(
            body,
            bg=TREE_BG,
            highlightthickness=1,
            highlightbackground=BORDER,
        )

        table_panel.pack(
            fill="both",
            expand=True,
        )

        column_ids = [
            item[0]
            for item in columns
        ]

        tree = ttk.Treeview(
            table_panel,
            columns=column_ids,
            show="headings",
            style="SubDub.Treeview",
        )

        vertical = ttk.Scrollbar(
            table_panel,
            orient="vertical",
            command=tree.yview,
        )

        horizontal = ttk.Scrollbar(
            table_panel,
            orient="horizontal",
            command=tree.xview,
        )

        tree.configure(
            yscrollcommand=vertical.set,
            xscrollcommand=horizontal.set,
        )

        vertical.pack(
            side="right",
            fill="y",
        )

        horizontal.pack(
            side="bottom",
            fill="x",
        )

        tree.pack(
            side="left",
            fill="both",
            expand=True,
        )

        for (
            column_id,
            heading,
            width,
        ) in columns:
            tree.heading(
                column_id,
                text=heading,
                command=lambda c=column_id:
                    self.sort_treeview(
                        tree,
                        c,
                        False,
                    ),
            )

            tree.column(
                column_id,
                width=width,
                minwidth=100,
                stretch=True,
            )

        for index, record in enumerate(
            records
        ):
            values = [
                record.get(
                    column_id,
                    "",
                )
                for column_id
                in column_ids
            ]

            tree.insert(
                "",
                "end",
                iid=str(index),
                values=values,
            )

        def open_selected(
            event=None,
        ):
            selected = tree.selection()

            if not selected:
                return

            try:
                record = records[
                    int(
                        selected[0]
                    )
                ]
            except Exception:
                return

            target = (
                record.get(
                    "path"
                )
                or record.get(
                    "subtitle"
                )
            )

            if not target:
                return

            path = Path(
                target
            )

            folder = (
                path
                if path.is_dir()
                else path.parent
            )

            if (
                folder.exists()
                and os.name == "nt"
            ):
                os.startfile(
                    str(folder)
                )

        tree.bind(
            "<Double-1>",
            open_selected,
        )

        footer = tk.Frame(
            window,
            bg=BG,
        )

        footer.pack(
            fill="x",
            padx=18,
            pady=(0, 18),
        )

        tk.Label(
            footer,
            text=(
                "DOUBLE-CLICK A ROW TO OPEN "
                "ITS FOLDER"
            ),
            bg=BG,
            fg=TEXT_MUTED,
            font=(
                "Segoe UI",
                8,
                "bold",
            ),
        ).pack(
            side="left"
        )

        self.make_button(
            footer,
            "CLOSE",
            window.destroy,
            CARD,
            CARD_HOVER,
        ).pack(
            side="right"
        )


    def sort_treeview(
        self,
        tree,
        column,
        reverse,
    ):
        values = []

        for item in tree.get_children():
            value = tree.set(
                item,
                column,
            )

            values.append(
                (
                    value.lower(),
                    item,
                )
            )

        values.sort(
            reverse=reverse
        )

        for index, (
            _value,
            item,
        ) in enumerate(
            values
        ):
            tree.move(
                item,
                "",
                index,
            )

        tree.heading(
            column,
            command=lambda:
                self.sort_treeview(
                    tree,
                    column,
                    not reverse,
                ),
        )


    # --------------------------------------------------------
    # GUI helpers
    # --------------------------------------------------------

    def choose_folder(self):
        folder = filedialog.askdirectory(
            title="Choose Media Library"
        )

        if folder:
            self.folder_var.set(
                folder
            )

            self.current_file_var.set(
                (
                    "Media library selected. "
                    "Ready to scan."
                )
            )


    def open_folder(self):
        folder = (
            self.folder_var
            .get()
            .strip()
        )

        if not folder:
            return

        path = Path(
            folder
        )

        if (
            path.is_dir()
            and os.name == "nt"
        ):
            os.startfile(
                str(path)
            )


    def toggle_credentials(self):
        show = (
            ""
            if self.show_credentials_var.get()
            else "*"
        )

        self.opensub_password_entry.configure(
            show=show
        )

        self.opensub_api_entry.configure(
            show=show
        )

        self.subdl_api_entry.configure(
            show=show
        )


    def clear_log(self):
        self.log_text.delete(
            "1.0",
            "end",
        )


    def append_log(
        self,
        message,
    ):
        self.log_text.insert(
            "end",
            message + "\n",
        )

        self.log_text.see(
            "end"
        )


    # --------------------------------------------------------
    # Settings auto-save
    # --------------------------------------------------------

    def install_auto_save(self):
        variables = [
            self.folder_var,
            self.opensub_enabled_var,
            self.subdl_enabled_var,
            self.provider_order_var,
            self.opensub_username_var,
            self.opensub_password_var,
            self.opensub_api_var,
            self.subdl_api_var,
        ]

        for variable in variables:
            variable.trace_add(
                "write",
                self.schedule_settings_save,
            )


    def schedule_settings_save(
        self,
        *args,
    ):
        if self.save_job:
            try:
                self.root.after_cancel(
                    self.save_job
                )
            except Exception:
                pass

        self.save_job = self.root.after(
            500,
            self.persist_settings,
        )


    def persist_settings(self):
        self.save_job = None

        settings = {
            "last_folder":
                self.folder_var
                .get()
                .strip(),

            "opensubtitles_enabled":
                self.opensub_enabled_var
                .get(),

            "subdl_enabled":
                self.subdl_enabled_var
                .get(),

            "provider_order":
                self.provider_order_var
                .get(),

            "opensubtitles_username":
                self.opensub_username_var
                .get()
                .strip(),

            "opensubtitles_password":
                self.opensub_password_var
                .get(),

            "opensubtitles_api_key":
                self.opensub_api_var
                .get()
                .strip(),

            "subdl_api_key":
                self.subdl_api_var
                .get()
                .strip(),
        }

        try:
            save_settings(
                settings
            )
        except Exception as error:
            print(
                "Could not save settings:",
                error,
            )


    # --------------------------------------------------------
    # Event queue
    # --------------------------------------------------------

    def post(
        self,
        event_type,
        *data,
    ):
        self.events.put(
            (
                event_type,
                data,
            )
        )


    def process_events(self):
        try:
            while True:
                event_type, data = (
                    self.events.get_nowait()
                )

                if event_type == "log":
                    self.append_log(
                        data[0]
                    )

                elif event_type == "status":
                    self.status_var.set(
                        data[0]
                    )

                elif event_type == "file":
                    self.current_file_var.set(
                        data[0]
                    )

                elif event_type == "scan_progress":
                    self.scan_progress.set(
                        data[0]
                    )

                elif event_type == "download_progress":
                    self.download_progress.set(
                        data[0]
                    )

                elif event_type == "metrics":
                    (
                        total,
                        have,
                        missing,
                        downloaded,
                        failed,
                    ) = data

                    self.total_var.set(
                        str(total)
                    )

                    self.have_var.set(
                        str(have)
                    )

                    self.missing_var.set(
                        str(missing)
                    )

                    self.downloaded_var.set(
                        str(downloaded)
                    )

                    self.failed_var.set(
                        str(failed)
                    )

                elif event_type == "records":
                    (
                        record_type,
                        records,
                    ) = data

                    if record_type == "videos":
                        self.video_records = (
                            records
                        )

                    elif record_type == "with_subs":
                        self.with_subs_records = (
                            records
                        )

                    elif record_type == "missing":
                        self.missing_records = (
                            records
                        )

                    elif record_type == "downloaded":
                        self.download_records = (
                            records
                        )

                    elif record_type == "failed":
                        self.failed_records = (
                            records
                        )

                elif event_type == "error":
                    messagebox.showerror(
                        APP_NAME,
                        data[0],
                    )

                elif event_type == "finished":
                    self.set_running(
                        False
                    )

        except queue.Empty:
            pass

        self.root.after(
            75,
            self.process_events,
        )


    # --------------------------------------------------------
    # Validation
    # --------------------------------------------------------

    def validate_folder(self):
        folder = (
            self.folder_var
            .get()
            .strip()
        )

        if not folder:
            messagebox.showerror(
                APP_NAME,
                (
                    "Select a media library "
                    "before starting."
                ),
            )
            return None

        path = Path(
            folder
        )

        if not path.is_dir():
            messagebox.showerror(
                APP_NAME,
                (
                    "The selected media "
                    "folder does not exist."
                ),
            )
            return None

        return path


    def validate_download_settings(
        self,
    ):
        if not (
            self.opensub_enabled_var.get()
            or self.subdl_enabled_var.get()
        ):
            messagebox.showerror(
                APP_NAME,
                (
                    "Enable at least one "
                    "subtitle provider."
                ),
            )
            return False

        if self.opensub_enabled_var.get():
            username = (
                self.opensub_username_var
                .get()
                .strip()
            )

            password = (
                self.opensub_password_var
                .get()
            )

            api_key = (
                self.opensub_api_var
                .get()
                .strip()
            )

            if not username:
                messagebox.showerror(
                    APP_NAME,
                    (
                        "OpenSubtitles username "
                        "is required."
                    ),
                )
                return False

            if "@" in username:
                messagebox.showerror(
                    APP_NAME,
                    (
                        "OpenSubtitles requires "
                        "your username, not "
                        "your email address."
                    ),
                )
                return False

            if not password:
                messagebox.showerror(
                    APP_NAME,
                    (
                        "OpenSubtitles password "
                        "is required."
                    ),
                )
                return False

            if not api_key:
                messagebox.showerror(
                    APP_NAME,
                    (
                        "OpenSubtitles API key "
                        "is required."
                    ),
                )
                return False

        if self.subdl_enabled_var.get():
            if not (
                self.subdl_api_var
                .get()
                .strip()
            ):
                messagebox.showerror(
                    APP_NAME,
                    (
                        "SubDL API key "
                        "is required."
                    ),
                )
                return False

        return True


    # --------------------------------------------------------
    # Video discovery / subtitle detection
    # --------------------------------------------------------

    def should_ignore_video(
        self,
        path,
    ):
        name = (
            path.stem
            .lower()
            .strip()
        )

        if name in IGNORE_VIDEO_NAMES:
            return True

        if name.startswith(
            "rarbg"
        ):
            return True

        if re.search(
            r"(^|[._ -])sample([._ -]|$)",
            name,
        ):
            return True

        return False


    def find_videos(
        self,
        folder,
    ):
        videos = []

        for path in folder.rglob(
            "*"
        ):
            if self.cancel_event.is_set():
                break

            if not path.is_file():
                continue

            if (
                path.suffix.lower()
                not in VIDEO_EXTENSIONS
            ):
                continue

            if self.should_ignore_video(
                path
            ):
                continue

            videos.append(
                path
            )

        videos.sort(
            key=lambda path:
                str(path).lower()
        )

        return videos


    def find_external_subtitles(
        self,
        video,
    ):
        subtitles = []
        base = video.stem.lower()

        try:
            files = video.parent.iterdir()
        except OSError:
            return []

        for file in files:
            if not file.is_file():
                continue

            if (
                file.suffix.lower()
                not in SUBTITLE_EXTENSIONS
            ):
                continue

            subtitle_name = (
                file.stem.lower()
            )

            if (
                subtitle_name == base
                or subtitle_name.startswith(
                    base + "."
                )
            ):
                subtitles.append(
                    file
                )

        return subtitles


    def find_embedded_subtitles(
        self,
        video,
    ):
        command = [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_streams",
            "-select_streams",
            "s",
            str(video),
        ]

        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                creationflags=(
                    subprocess.CREATE_NO_WINDOW
                    if os.name == "nt"
                    else 0
                ),
            )

            if result.returncode != 0:
                return []

            if not result.stdout.strip():
                return []

            return json.loads(
                result.stdout
            ).get(
                "streams",
                [],
            )

        except Exception:
            return []


    def describe_embedded_subs(
        self,
        streams,
    ):
        details = []

        for stream in streams:
            tags = stream.get(
                "tags",
                {},
            )

            language = tags.get(
                "language",
                "unknown",
            )

            codec = stream.get(
                "codec_name",
                "unknown",
            )

            title = tags.get(
                "title",
                "",
            )

            description = (
                f"{language} / {codec}"
            )

            if title:
                description += (
                    f" / {title}"
                )

            details.append(
                description
            )

        return ", ".join(
            details
        )


    # --------------------------------------------------------
    # Providers
    # --------------------------------------------------------

    def get_config(self):
        return {
            "opensubtitles_enabled":
                self.opensub_enabled_var
                .get(),

            "subdl_enabled":
                self.subdl_enabled_var
                .get(),

            "provider_order":
                self.provider_order_var
                .get(),

            "opensubtitles_username":
                self.opensub_username_var
                .get()
                .strip(),

            "opensubtitles_password":
                self.opensub_password_var
                .get(),

            "opensubtitles_api_key":
                self.opensub_api_var
                .get()
                .strip(),

            "subdl_api_key":
                self.subdl_api_var
                .get()
                .strip(),
        }


    def build_providers(
        self,
        config,
    ):
        opensub = None
        subdl = None

        if config[
            "opensubtitles_enabled"
        ]:
            opensub = (
                OpenSubtitlesProvider(
                    config[
                        "opensubtitles_username"
                    ],
                    config[
                        "opensubtitles_password"
                    ],
                    config[
                        "opensubtitles_api_key"
                    ],
                )
            )

        if config[
            "subdl_enabled"
        ]:
            subdl = (
                SubDLProvider(
                    config[
                        "subdl_api_key"
                    ]
                )
            )

        providers = []

        if (
            config[
                "provider_order"
            ]
            == "SubDL first"
        ):
            if subdl:
                providers.append(
                    subdl
                )

            if opensub:
                providers.append(
                    opensub
                )

        else:
            if opensub:
                providers.append(
                    opensub
                )

            if subdl:
                providers.append(
                    subdl
                )

        return providers


    def connect_providers(
        self,
        providers,
    ):
        active = []

        for provider in providers:
            if self.cancel_event.is_set():
                break

            self.post(
                "log",
                (
                    f"Connecting to "
                    f"{provider.name}..."
                ),
            )

            try:
                provider.login()

                active.append(
                    provider
                )

                self.post(
                    "log",
                    (
                        f"{provider.name}: READY"
                    ),
                )

            except Exception as error:
                error_text = str(
                    error
                )

                self.post(
                    "log",
                    (
                        f"{provider.name}: "
                        f"FAILED — "
                        f"{error_text}"
                    ),
                )

        return active


    def describe_identity(
        self,
        identity,
    ):
        if identity[
            "kind"
        ] == "tv":
            return tv_display(
                identity
            )

        title = identity.get(
            "title",
            "Unknown movie",
        )

        year = identity.get(
            "year"
        )

        if year:
            return (
                f"{title} ({year})"
            )

        return title


    def download_for_video(
        self,
        video,
        providers,
    ):
        identity = parse_media_identity(
            video
        )

        media_display = (
            self.describe_identity(
                identity
            )
        )

        if identity["kind"] == "tv":
            self.post(
                "log",
                (
                    "    Detected TV: "
                    f"{media_display}"
                ),
            )
        else:
            self.post(
                "log",
                (
                    "    Detected movie: "
                    f"{media_display}"
                ),
            )

        provider_errors = []

        for provider in providers:
            if self.cancel_event.is_set():
                return (
                    False,
                    {
                        "filename":
                            video.name,
                        "media":
                            media_display,
                        "reason":
                            "Operation cancelled.",
                        "path":
                            str(video),
                    },
                )

            self.post(
                "log",
                (
                    f"    Searching "
                    f"{provider.name}..."
                ),
            )

            try:
                result = provider.search(
                    video,
                    identity,
                )

            except Exception as error:
                error_text = (
                    f"{provider.name}: "
                    f"{error}"
                )

                provider_errors.append(
                    error_text
                )

                self.post(
                    "log",
                    f"    {error_text}",
                )

                continue

            if not result:
                error_text = (
                    f"{provider.name}: "
                    "no match"
                )

                provider_errors.append(
                    error_text
                )

                self.post(
                    "log",
                    f"    {error_text}",
                )

                continue

            result["_identity"] = (
                identity
            )

            match = result.get(
                "title",
                media_display,
            )

            release = result.get(
                "release",
                "",
            )

            self.post(
                "log",
                (
                    f"    Match via "
                    f"{provider.name}: "
                    f"{match}"
                ),
            )

            if release:
                self.post(
                    "log",
                    (
                        f"    Release: "
                        f"{release}"
                    ),
                )

            destination = video.with_name(
                video.stem
                + ".en.srt"
            )

            try:
                provider.download(
                    result,
                    destination,
                )

            except Exception as error:
                error_text = (
                    f"{provider.name}: "
                    f"download failed — "
                    f"{error}"
                )

                provider_errors.append(
                    error_text
                )

                self.post(
                    "log",
                    f"    {error_text}",
                )

                continue

            self.post(
                "log",
                (
                    f"    DOWNLOADED via "
                    f"{provider.name}: "
                    f"{destination.name}"
                ),
            )

            return (
                True,
                {
                    "filename":
                        video.name,
                    "media":
                        media_display,
                    "provider":
                        provider.name,
                    "match":
                        match,
                    "release":
                        release,
                    "subtitle":
                        str(destination),
                    "path":
                        str(video),
                },
            )

        reason = "; ".join(
            provider_errors
        )

        if not reason:
            reason = (
                "No enabled provider "
                "could supply a subtitle."
            )

        return (
            False,
            {
                "filename":
                    video.name,
                "media":
                    media_display,
                "reason":
                    reason,
                "path":
                    str(video),
            },
        )


    # --------------------------------------------------------
    # Start / run state
    # --------------------------------------------------------

    def start_scan(self):
        folder = (
            self.validate_folder()
        )

        if folder is None:
            return

        self.persist_settings()

        self.begin_operation(
            folder,
            False,
            self.get_config(),
        )


    def start_download(self):
        folder = (
            self.validate_folder()
        )

        if folder is None:
            return

        if not (
            self.validate_download_settings()
        ):
            return

        self.persist_settings()

        self.begin_operation(
            folder,
            True,
            self.get_config(),
        )


    def begin_operation(
        self,
        folder,
        download,
        config,
    ):
        if self.running:
            return

        self.cancel_event.clear()

        self.video_records = []
        self.with_subs_records = []
        self.missing_records = []
        self.download_records = []
        self.failed_records = []

        self.clear_log()

        self.scan_progress.set(
            0
        )

        self.download_progress.set(
            0
        )

        self.total_var.set(
            "0"
        )

        self.have_var.set(
            "0"
        )

        self.missing_var.set(
            "0"
        )

        self.downloaded_var.set(
            "0"
        )

        self.failed_var.set(
            "0"
        )

        self.set_running(
            True
        )

        thread = threading.Thread(
            target=self.worker,
            args=(
                folder,
                download,
                config,
            ),
            daemon=True,
        )

        thread.start()


    def set_running(
        self,
        running,
    ):
        self.running = running

        self.set_button_enabled(
            self.scan_button,
            not running,
        )

        self.set_button_enabled(
            self.download_button,
            not running,
        )

        self.set_button_enabled(
            self.browse_button,
            not running,
        )

        self.set_button_enabled(
            self.cancel_button,
            running,
        )

        self.folder_entry.configure(
            state=(
                "disabled"
                if running
                else "normal"
            )
        )


    def cancel(self):
        if not self.running:
            return

        self.cancel_event.set()

        self.status_var.set(
            "CANCELLING"
        )

        self.current_file_var.set(
            (
                "Finishing the current "
                "operation..."
            )
        )


    # --------------------------------------------------------
    # Worker
    # --------------------------------------------------------

    def worker(
        self,
        folder,
        download,
        config,
    ):
        try:
            self.post(
                "status",
                "DISCOVERING MEDIA"
            )

            self.post(
                "file",
                str(folder)
            )

            self.post(
                "log",
                "=" * 78
            )

            self.post(
                "log",
                f"{APP_NAME} {APP_VERSION}"
            )

            self.post(
                "log",
                "=" * 78
            )

            videos = self.find_videos(
                folder
            )

            total = len(
                videos
            )

            if total == 0:
                self.post(
                    "status",
                    "NO VIDEOS FOUND"
                )

                self.post(
                    "file",
                    (
                        "No supported video files "
                        "were found."
                    ),
                )

                return

            have = []
            missing = []

            video_records = []
            with_subs_records = []
            missing_records = []

            self.post(
                "metrics",
                total,
                0,
                0,
                0,
                0,
            )

            for index, video in enumerate(
                videos,
                start=1,
            ):
                if self.cancel_event.is_set():
                    self.post(
                        "status",
                        "CANCELLED"
                    )
                    return

                identity = parse_media_identity(
                    video
                )

                media_display = (
                    self.describe_identity(
                        identity
                    )
                )

                self.post(
                    "status",
                    "SCANNING LIBRARY"
                )

                self.post(
                    "file",
                    (
                        f"{index} / {total}   "
                        f"{video.name}"
                    ),
                )

                self.post(
                    "scan_progress",
                    (
                        index
                        / total
                        * 100
                    ),
                )

                external = (
                    self.find_external_subtitles(
                        video
                    )
                )

                embedded = (
                    self.find_embedded_subtitles(
                        video
                    )
                )

                if (
                    external
                    or embedded
                ):
                    have.append(
                        video
                    )

                    if external and embedded:
                        subtitle_type = (
                            "External + Embedded"
                        )
                    elif external:
                        subtitle_type = (
                            "External"
                        )
                    else:
                        subtitle_type = (
                            "Embedded"
                        )

                    detail_parts = []

                    if external:
                        detail_parts.append(
                            (
                                "External: "
                                + ", ".join(
                                    subtitle.name
                                    for subtitle
                                    in external
                                )
                            )
                        )

                    if embedded:
                        detail_parts.append(
                            (
                                "Embedded: "
                                + self.describe_embedded_subs(
                                    embedded
                                )
                            )
                        )

                    with_subs_records.append(
                        {
                            "filename":
                                video.name,
                            "media":
                                media_display,
                            "subtitle_type":
                                subtitle_type,
                            "details":
                                " | ".join(
                                    detail_parts
                                ),
                            "path":
                                str(video),
                        }
                    )

                    video_records.append(
                        {
                            "filename":
                                video.name,
                            "media":
                                media_display,
                            "status":
                                "Has subtitles",
                            "path":
                                str(video),
                        }
                    )

                    label = "HAS SUBS"

                else:
                    missing.append(
                        video
                    )

                    missing_records.append(
                        {
                            "filename":
                                video.name,
                            "media":
                                media_display,
                            "path":
                                str(video),
                        }
                    )

                    video_records.append(
                        {
                            "filename":
                                video.name,
                            "media":
                                media_display,
                            "status":
                                "Missing subtitles",
                            "path":
                                str(video),
                        }
                    )

                    label = "MISSING"

                type_tag = (
                    "TV"
                    if identity[
                        "kind"
                    ] == "tv"
                    else "MOVIE"
                )

                self.post(
                    "log",
                    (
                        f"[{index:03}/{total:03}] "
                        f"{label:<9} "
                        f"[{type_tag}] "
                        f"{video.name}"
                    ),
                )

                self.post(
                    "metrics",
                    total,
                    len(have),
                    len(missing),
                    0,
                    0,
                )

            self.post(
                "records",
                "videos",
                video_records,
            )

            self.post(
                "records",
                "with_subs",
                with_subs_records,
            )

            self.post(
                "records",
                "missing",
                missing_records,
            )

            self.post(
                "scan_progress",
                100
            )

            self.post(
                "log",
                ""
            )

            self.post(
                "log",
                (
                    f"SCAN COMPLETE — "
                    f"{len(have)} WITH SUBTITLES / "
                    f"{len(missing)} MISSING"
                ),
            )

            if not download:
                self.post(
                    "status",
                    "SCAN COMPLETE"
                )

                self.post(
                    "file",
                    (
                        f"{len(missing)} video(s) "
                        "need subtitles. "
                        "Click a statistics card "
                        "for details."
                    ),
                )

                return

            if not missing:
                self.post(
                    "status",
                    "LIBRARY COMPLETE"
                )

                self.post(
                    "file",
                    (
                        "Every video already "
                        "has subtitles."
                    ),
                )

                return

            self.post(
                "status",
                "CONNECTING SOURCES"
            )

            providers = (
                self.build_providers(
                    config
                )
            )

            providers = (
                self.connect_providers(
                    providers
                )
            )

            if not providers:
                raise RuntimeError(
                    (
                        "None of the enabled "
                        "subtitle providers could "
                        "be initialized."
                    )
                )

            self.post(
                "log",
                ""
            )

            self.post(
                "log",
                (
                    "ACTIVE SOURCES: "
                    + ", ".join(
                        provider.name
                        for provider
                        in providers
                    )
                ),
            )

            downloaded = 0
            failed = 0

            download_records = []
            failed_records = []

            missing_total = len(
                missing
            )

            for index, video in enumerate(
                missing,
                start=1,
            ):
                if self.cancel_event.is_set():
                    self.post(
                        "status",
                        "CANCELLED"
                    )
                    return

                self.post(
                    "status",
                    "DOWNLOADING SUBTITLES"
                )

                self.post(
                    "file",
                    (
                        f"{index} / "
                        f"{missing_total}   "
                        f"{video.name}"
                    ),
                )

                self.post(
                    "download_progress",
                    (
                        index
                        / missing_total
                        * 100
                    ),
                )

                self.post(
                    "log",
                    ""
                )

                self.post(
                    "log",
                    (
                        f"[{index}/"
                        f"{missing_total}] "
                        f"{video.name}"
                    ),
                )

                (
                    success,
                    detail,
                ) = (
                    self.download_for_video(
                        video,
                        providers,
                    )
                )

                if success:
                    downloaded += 1

                    download_records.append(
                        detail
                    )
                else:
                    failed += 1

                    failed_records.append(
                        detail
                    )

                self.post(
                    "records",
                    "downloaded",
                    list(
                        download_records
                    ),
                )

                self.post(
                    "records",
                    "failed",
                    list(
                        failed_records
                    ),
                )

                self.post(
                    "metrics",
                    total,
                    len(have),
                    len(missing),
                    downloaded,
                    failed,
                )

                if index < missing_total:
                    time.sleep(
                        1
                    )

            self.post(
                "download_progress",
                100
            )

            self.post(
                "status",
                "COMPLETE"
            )

            self.post(
                "file",
                (
                    f"{downloaded} downloaded  •  "
                    f"{failed} failed  •  "
                    "Click a statistics card "
                    "for details."
                ),
            )

            self.post(
                "log",
                ""
            )

            self.post(
                "log",
                "=" * 78
            )

            self.post(
                "log",
                "SUBDUB COMPLETE"
            )

            self.post(
                "log",
                "=" * 78
            )

            self.post(
                "log",
                (
                    "Already had subtitles : "
                    f"{len(have)}"
                ),
            )

            self.post(
                "log",
                (
                    "Downloaded            : "
                    f"{downloaded}"
                ),
            )

            self.post(
                "log",
                (
                    "Failed / not found    : "
                    f"{failed}"
                ),
            )

        except Exception as error:
            error_text = str(
                error
            )

            self.post(
                "log",
                ""
            )

            self.post(
                "log",
                f"ERROR: {error_text}",
            )

            self.post(
                "status",
                "ERROR"
            )

            self.post(
                "file",
                error_text,
            )

            self.post(
                "error",
                error_text,
            )

        finally:
            self.post(
                "finished"
            )


    def on_close(self):
        try:
            self.persist_settings()
        except Exception:
            pass

        self.cancel_event.set()
        self.root.destroy()


def main():
    root = tk.Tk()
    SubDubApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()