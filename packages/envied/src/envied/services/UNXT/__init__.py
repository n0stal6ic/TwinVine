from __future__ import annotations

import click
import re
import sys
import time
import uuid

from click import Context
from collections.abc import Generator
from datetime import datetime
from http.cookiejar import CookieJar
from langcodes import Language
from pathlib import Path
from requests import Request
from typing import Any, Optional, Union, List, Literal
from urllib.parse import urlparse, parse_qs, urlencode

from envied.core.cdm.detect import is_playready_cdm
from envied.core.constants import AnyTrack
from envied.core.credential import Credential
from envied.core.manifests import DASH, ISM
from envied.core.search_result import SearchResult
from envied.core.service import Service
from envied.core.titles import Title_T, Titles_T, Episode, Movie, Movies, Series
from envied.core.tracks import Chapters, Tracks, Video, Audio, Subtitle, Attachment, Chapter
from envied.core.utilities import get_ip_info


class UNXT(Service):
    """
    Service code for U-NEXT Streaming Service (https://video.unext.jp).\n
    Version: 26.03.01

    Author: Made by CodeName393 with Special Thanks to narakama\n
    Authorization: Credentials\n
    Security: UHD@L3/SL2000, FHD@L3/SL2000
    """

    ALIASES = ("UNXT", "UNEXT", "U-NEXT")
    GEOFENCE = ("JP",)
    TITLE_RE = (r"https?://(?:[\w-]+\.)?unext\.jp/.*?(?:/|td=)(?P<id>SID[0-9]+)",)

    @staticmethod
    @click.command(name="U-NEXT", short_help="https://video.unext.jp", help=__doc__)
    @click.argument("title", type=str)
    @click.option(
        "-t",
        "--type",
        type=click.Choice(["sub", "dub"], case_sensitive=False),
        default="sub",
        help="Prefer subtitle or dubbing version for content type if available.",
    )
    @click.pass_context
    def cli(ctx: Context, **kwargs: Any) -> UNXT:
        return UNXT(ctx, **kwargs)

    def __init__(self, ctx: Context, title: str, type: Literal["sub", "dub"]):
        self.title = title
        super().__init__(ctx)

        self.title_id = self.title
        for pattern in self.TITLE_RE:
            match = re.match(pattern, self.title)
            if match:
                self.title_id = match.group("id")
                break

        self.dub_type = type
        self.cdm = ctx.obj.cdm

        self.device_id = None
        self.account_tokens = {}
        self.active_session = {}
        self.playback_data = {}

        self.log.info("Preparing...")

        ip_info = get_ip_info(self.session)
        country_key = None
        possible_keys = ["countryCode", "country", "country_code", "country-code"]
        for key in possible_keys:
            if key in ip_info:
                country_key = key
                break
        if country_key:
            region = str(ip_info[country_key]).upper()
            self.log.info(f" + IP Region: {region}")
        else:
            self.log.warning(f" - The region could not be determined from IP information: {ip_info}")
            region = "US"
            self.log.info(f" + IP Region: {region} (By Default)")
        if region != "JP":
            self.log.error("  - It is not currently available in the country.", exc_info=False)
            sys.exit(1)

        self.session.headers.update(
            {
                "User-Agent": self.config["device"]["user_agent"],
                "Content-Type": "application/json; charset=utf-8",
                "Accept-Encoding": "gzip",
            }
        )

    def authenticate(self, cookies: Optional[CookieJar] = None, credential: Optional[Credential] = None) -> None:
        super().authenticate(cookies, credential)
        self.credentials = credential
        if not credential:
            raise EnvironmentError("Service requires Credentials for Authentication.")

        self.log.info("Logging into U-NEXT...")
        self._login()

        self.log.debug(self.active_session)
        self.log.info(f" + Account ID: {self.active_session['cuid']}")
        self.log.info(f" + Platform ID: {self.active_session['pfid']}")
        self.log.info(f" + Subscribed: {self.active_session['contractStatusCode'] == 'ACTIVE'}")

    def _login(self) -> None:
        cache = self.cache.get(f"tokens_{self.credentials.sha1}")

        if cache:
            try:
                self.log.info(" + Using cached tokens...")
                self.account_tokens = cache.data.get("tokens", {})
                self.device_id = cache.data.get("device_id")

                required_keys = ["userToken", "userTokenExpire", "securityToken"]
                if not self.account_tokens or not all(k in self.account_tokens for k in required_keys) or not self.device_id:
                    raise ValueError("Invalid token data in cache")

            except (KeyError, ValueError, TypeError) as e:
                self.log.warning(f" - Cached token data is invalid or corrupted ({e}). Getting new tokens...")
                self._perform_full_login()

            try:
                self._refresh()
            except Exception as e:
                self.log.warning(f" - Failed to refresh token ({e}). Getting new tokens...")
                self._perform_full_login()

        else:
            self.log.info(" + Getting new tokens...")
            self._perform_full_login()

        self.log.info(" + Fetching session data...")
        self.active_session = self._get_account_info()
        self.log.info("Session data setup successfully.")

    def _perform_full_login(self) -> None:
        if not self.device_id:
            self.device_id = str(uuid.uuid4())

        user_info = self._login_with_password(self.credentials.username, self.credentials.password)
        token_data = {
            "userToken": user_info["userToken"],
            "userTokenExpire": user_info["userTokenExpire"],
            "securityToken": user_info["securityToken"],
        }
        self._apply_new_tokens(token_data)

    def _refresh(self) -> None:
        cache = self.cache.get(f"tokens_{self.credentials.sha1}")
        if not cache.expired:
            self.log.debug(f" + Token is valid until: {datetime.fromtimestamp(cache.expiration.timestamp()).strftime('%Y-%m-%d %H:%M:%S')}")
            return

        self.log.warning(" + Token expired. Refreshing...")
        try:
            user_info = self._refresh_token()
            token_data = {
                "userToken": user_info["userToken"],
                "userTokenExpire": user_info["userTokenExpire"],
                "securityToken": user_info["securityToken"],
            }
            self._apply_new_tokens(token_data)

        except Exception as e:
            self.log.error(f"Refresh failed: {e}.", exc_info=False)
            raise e

    def _apply_new_tokens(self, token_data: dict) -> None:
        self.account_tokens = token_data

        expire_time = token_data["userTokenExpire"]
        valid_duration = max(0, expire_time - int(time.time()))

        cache_data = {
            "tokens": self.account_tokens,
            "device_id": self.device_id,
        }
        cache = self.cache.get(f"tokens_{self.credentials.sha1}")
        cache.set(cache_data, valid_duration - 60)
        self.log.debug(f" + New Token is valid until: {datetime.fromtimestamp(cache.expiration.timestamp()).strftime('%Y-%m-%d %H:%M:%S')}")

    def search(self) -> Generator[SearchResult, None, None]:
        data = self._get_search(self.title)
        if not data["title_list"]:
            return
        for result in data["title_list"]:
            year = result["production_year"]
            if not year:
                year = result["since_year"]

            yield SearchResult(
                id_=result["title_code"],
                title=result["title_name"],
                description=result["catchphrase"],
                label=year,
                url=f"https://video.unext.jp/title/{result['title_code']}",
            )

    def get_titles(self) -> Titles_T:
        stage_data = self._get_stage_data(self.title_id)

        content_type = stage_data["media_type_code"]
        self.log.debug(f" + Content Type: {content_type.upper()}")

        if content_type in ["MOVIE", "VIDEO", "OV"]:
            year = stage_data["production_year"]
            if not year:
                year = stage_data["since_year"]

            movie_data = self._get_movie_data(self.title_id)
            return Movies(
                [
                    Movie(
                        id_=movie_data["episode"]["episode_code"],
                        service=self.__class__,
                        name=stage_data["title_name"],
                        description=stage_data["story"],
                        year=year,
                        language=Language.get("en"),
                        data=movie_data["episode"],
                    )
                ]
            )

        elif content_type == "TV":
            return Series(self._get_series(stage_data))

        else:
            self.log.error(f" - Unsupported content type: {content_type}", exc_info=False)
            sys.exit(1)

    def _get_series(self, stage_data: dict) -> Series:
        title_relation = self._get_title_relation(self.title_id)

        all_seasons = {}
        special_keywords = ["メイキング", "making", "special", "特典", "映像"]

        current_year = stage_data["production_year"]
        if not current_year:
            current_year = stage_data["since_year"]

        title_name_main = stage_data["title_name"]
        is_special_main = any(k in title_name_main.lower() for k in special_keywords)

        raw_order_main = int(stage_data["series_in_order"])
        if is_special_main:
            order_main = 0
        else:
            order_main = raw_order_main if raw_order_main > 0 else 1

        series_name = stage_data["series_name"]
        if not series_name:
            series_name = title_name_main

        all_seasons[self.title_id] = {
            "order": order_main,
            "name": series_name,
            "year": current_year,
        }

        if "relation" in title_relation:
            for rel_group in title_relation["relation"]:
                if rel_group["groupcode"] != stage_data["series_code"]:
                    continue

                for item in rel_group["grouplist"]:
                    if item["media_type_code"] != "TV":
                        continue

                    item_year = item["production_year"]
                    if not item_year:
                        item_year = item["since_year"]

                    title_name_sub = item["title_name"]
                    is_special_sub = any(k in title_name_sub.lower() for k in special_keywords)

                    raw_order_sub = int(item["series_in_order"])
                    if is_special_sub:
                        order_sub = 0
                    else:
                        order_sub = raw_order_sub if raw_order_sub > 0 else 1

                    all_seasons[item["title_code"]] = {
                        "order": order_sub,
                        "name": item["series_name"],
                        "year": item_year,
                    }

        sorted_season_ids = sorted(all_seasons.keys(), key=lambda x: all_seasons[x]["order"])

        episodes: List[Episode] = []
        regular_season_idx = 1
        special_episode_idx = 1

        for season_id in sorted_season_ids:
            season_info = all_seasons[season_id]

            if season_info["order"] == 0:
                current_season_number = 0
            else:
                current_season_number = regular_season_idx
                regular_season_idx += 1

            episode_list_data = self._get_episode_list(season_id)
            for idx, episode in enumerate(episode_list_data["items"], start=1):
                if current_season_number == 0:
                    episode_number = special_episode_idx
                    special_episode_idx += 1
                else:
                    episode_number = idx

                episodes.append(
                    Episode(
                        id_=episode["episode_code"],
                        service=self.__class__,
                        title=season_info["name"],
                        season=current_season_number,
                        number=episode_number,
                        name=episode["episode_name"],
                        description=episode["introduction"],
                        year=season_info["year"],
                        language=Language.get("en"),
                        data=episode,
                    )
                )

        return episodes

    def get_tracks(self, title: Title_T) -> Tracks:
        self._refresh()  # Safe Access

        play_mode = "caption"
        if title.data["has_dub"] and self.dub_type == "dub":
            play_mode = "dub"
            title.language = Language.get("ja")

        fetched_ranges = set()

        def _fetch_variant(title: Title_T, codec: Optional[Video.Codec], range_: Video.Range) -> Tracks:
            if codec == Video.Codec.AVC and range_ in (Video.Range.DV, Video.Range.HDR10, Video.Range.HDR10P):
                return Tracks()
            nonlocal fetched_ranges

            video_ranges = ["SDR"]
            if range_ == Video.Range.DV:
                video_ranges = ["VISION"]
            elif range_ in (Video.Range.HDR10, Video.Range.HDR10P):
                video_ranges = ["HDR10"]

            range_key = video_ranges[0]
            if range_key in fetched_ranges:
                return Tracks()
            fetched_ranges.add(range_key)

            self.log.debug(f"Fetching {range_.name} manifest...")
            return self._fetch_manifest_tracks(title, video_ranges, play_mode)

        tracks = self._get_tracks_for_variants(title, _fetch_variant)

        if thumb := title.data.get("thumbnail"):
            url = f"https://{thumb['standard']}"
            description = Path(url).stem
            tracks.add(Attachment.from_url(url=url, name="thumbnail", description=description, session=self.session))

        for track in tracks:
            if isinstance(track, (Audio, Subtitle)):
                track.name = track.language.display_name()
                track.name += " [Original]" if track.is_original_lang else ""

        for audio in tracks.audio:
            if audio.codec == Audio.Codec.EC3 and audio.bitrate > 448_000:
                audio.joc = 16  # U-NEXT Bug...

        return tracks

    def _fetch_manifest_tracks(self, title: Title_T, video_ranges: List[str], play_mode: str) -> Tracks:
        self.playback_data[title.id] = self._get_playlist(title.id, video_ranges, play_mode)

        if (res_code := self.playback_data[title.id]["result_status"]) != 200:
            self.log.debug(f" - Get stream error({res_code})")
            return Tracks()

        movie_profile = self.playback_data[title.id]["url_info"][0]["movie_profile"]
        target_drm_key = "playready" if is_playready_cdm(self.cdm) else "widevine"
        priority_protocols = ["dash", "smooth"]

        selected_protocol = next(
            (proto for proto in priority_protocols if proto in movie_profile and target_drm_key in movie_profile[proto].get("license_url_list", {})),
            None,
        )

        if not selected_protocol:
            self.log.debug(f" - Unable to handle with invalid DRM type. {target_drm_key} info not found or license_url is empty in response.")
            return Tracks()

        self.playback_data[title.id]["protocol"] = selected_protocol
        base_url = movie_profile[selected_protocol].get("playlist_url")
        u = urlparse(base_url)
        query = parse_qs(u.query)
        query["play_token"] = self.playback_data[title.id]["play_token"]
        new_query = urlencode(query, doseq=True)
        manifest_url = u._replace(query=new_query).geturl()

        self.log.debug(f" + Manifest URL: {manifest_url}")
        if ".mpd/" in manifest_url:
            tracks = DASH.from_url(url=manifest_url, session=self.session).to_tracks(title.language)
        elif ".ism/manifest" in manifest_url:
            tracks = ISM.from_url(url=manifest_url, session=self.session).to_tracks(title.language)
        else:
            self.log.debug(" - Manifest type cannot be handled.")
            return Tracks()

        for video in tracks.videos:
            if video.codec == Video.Codec.HEVC and "HDR10" in video_ranges:
                video.range = Video.Range.HDR10  # U-NEXT Bug...

        return tracks

    def get_chapters(self, title: Titles_T) -> Chapters:
        url_info = self.playback_data[title.id]["url_info"][0]
        endroll_start_position = float(url_info["endroll_start_position"])
        movie_parts_list = url_info["movie_parts_position_list"]

        pre_chapter = []

        has_intro = False
        has_credits = False

        if movie_parts_list:
            for part in movie_parts_list:
                part_type = part.get("movie_parts_type")
                start = float(part.get("from", 0))
                end = float(part.get("to", 0))

                chapter_name = "Scene"
                if part_type == "Opening":
                    if not has_intro:
                        chapter_name = "Intro"
                        has_intro = True
                elif part_type == "Ending":
                    if not has_credits:
                        chapter_name = "Credits"
                        has_credits = True

                pre_chapter.append((chapter_name, start))

                if end > start:
                    pre_chapter.append(("Scene", end))

        if endroll_start_position > 0:
            is_duplicate = any(abs(t - endroll_start_position) < 1.0 for _, t in pre_chapter)

            if not is_duplicate:
                if has_credits:
                    pre_chapter.append(("Scene", endroll_start_position))
                else:
                    pre_chapter.append(("Credits", endroll_start_position))

        pre_chapter.sort(key=lambda x: x[1])

        if not pre_chapter or pre_chapter[0][1] > 0:
            pre_chapter.insert(0, ("Scene", 0.0))

        if len(pre_chapter) == 1 and pre_chapter[0] == ("Scene", 0):
            return []

        chapters: List[Chapter] = []
        for i, (chapter_title, time_sec) in enumerate(pre_chapter):
            chapters.append(
                Chapter(
                    timestamp=float(time_sec),
                    name=chapter_title if chapter_title != "Scene" else None,
                )
            )

        return chapters

    def get_widevine_license(self, *, challenge: bytes, title: Title_T, track: AnyTrack) -> Optional[Union[bytes, str]]:
        protocol = self.playback_data[title.id]["protocol"]
        license_url = self.playback_data[title.id]["url_info"][0]["movie_profile"][protocol]["license_url_list"]["widevine"]
        params = {"play_token": self.playback_data[title.id]["play_token"]}
        headers = {"Content-Type": "application/octet-stream"}
        res = self.session.post(license_url, params=params, headers=headers, data=challenge)
        res.raise_for_status()
        return res.content

    def get_playready_license(self, *, challenge: bytes, title: Title_T, track: AnyTrack) -> Optional[Union[bytes, str]]:
        protocol = self.playback_data[title.id]["protocol"]
        license_url = self.playback_data[title.id]["url_info"][0]["movie_profile"][protocol]["license_url_list"]["playready"]
        params = {"play_token": self.playback_data[title.id]["play_token"]}
        headers = {
            "Accept": "application/xml",
            "Content-Type": "text/xml; charset=utf-8",
        }
        res = self.session.post(license_url, params=params, headers=headers, data=challenge)
        res.raise_for_status()
        return res.content

    def _get_search(self, title: str) -> dict:
        endpoint = self.config["endpoints"]["search"]
        data_payload = {
            "searchString": title,
            "numParPage": 32,
            "pageNo": 1,
            "order": "recommend",
            "film_rating_code": "R15",
            "need_fulldata": 1,
        }
        payload = self._build_payload(
            data_payload=data_payload,
            user_token=self.account_tokens["userToken"],
        )
        data = self._request("POST", endpoint, payload=payload)
        return data["data"]

    def _get_stage_data(self, title_id: str) -> dict:
        endpoint = self.config["endpoints"]["stage"]
        data_payload = {"title_code": title_id}
        payload = self._build_payload(
            data_payload=data_payload,
            user_token=self.account_tokens["userToken"],
        )
        data = self._request("POST", endpoint, payload=payload)
        return data["data"]

    def _get_movie_data(self, title_id: str) -> dict:
        endpoint = self.config["endpoints"]["movie"]
        data_payload = {"title_code": title_id}
        payload = self._build_payload(
            data_payload=data_payload,
            user_token=self.account_tokens["userToken"],
        )
        data = self._request("POST", endpoint, payload=payload)
        return data["data"]

    def _get_title_relation(self, title_id: str) -> dict:
        endpoint = self.config["endpoints"]["relation"]
        data_payload = {"title_code": title_id}
        payload = self._build_payload(
            data_payload=data_payload,
            user_token=self.account_tokens["userToken"],
        )
        data = self._request("POST", endpoint, payload=payload)
        return data["data"]

    def _get_episode_list(self, season_id: str) -> dict:
        endpoint = self.config["endpoints"]["episode"]
        data_payload = {
            "title_code": season_id,
            "page_size": 1000,
            "page_number": 1,
        }
        payload = self._build_payload(
            data_payload=data_payload,
            user_token=self.account_tokens["userToken"],
        )
        data = self._request("POST", endpoint, payload=payload)
        return data["data"]

    def _get_playlist(self, title_id: str, video_ranges: List[str], play_mode: str) -> dict:
        data_payload = {
            "code": title_id,
            "bitrate_low": 192,
            "play_type": 2,
            "play_mode": play_mode,
            "keyonly_flg": 0,
            "validation_flg": 0,
            "codec": ["H264", "H265"],
            "dynamic_range_list": video_ranges,
            "audio_type_list": ["ac-3", "ec-3", "mp4a"],
        }
        endpoint = self.config["endpoints"]["playlist"]
        payload = self._build_payload(
            data_payload=data_payload,
            user_token=self.account_tokens["userToken"],
        )
        data = self._request("POST", endpoint, payload=payload)
        return data["data"]

    def _login_with_password(self, username: str, password: str) -> dict:
        endpoint = self.config["endpoints"]["auth"]
        data_payload = {
            "loginId": username,
            "password": password,
        }
        payload = self._build_payload(data_payload=data_payload)
        data = self._request("POST", endpoint, payload=payload)
        return data["common"]["userInfo"]

    def _get_account_info(self) -> dict:
        endpoint = self.config["endpoints"]["account"]
        payload = self._build_payload(
            data_payload={},
            user_token=self.account_tokens["userToken"],
            security_token=self.account_tokens["securityToken"],
        )
        data = self._request("POST", endpoint, payload=payload)
        return data["common"]["userInfo"]

    def _refresh_token(self) -> dict:
        endpoint = self.config["endpoints"]["auth"]
        data_payload = {"securityToken": self.account_tokens["securityToken"]}
        payload = self._build_payload(
            data_payload=data_payload,
            user_token=self.account_tokens["userToken"],
        )
        data = self._request("POST", endpoint, payload=payload)
        return data["common"]["userInfo"]

    def _build_payload(self, data_payload: dict, user_token: str = "", security_token: str = None) -> dict:
        user_info = {
            "userToken": user_token,
            "service_name": "unext",
        }
        if security_token:
            user_info["securityToken"] = security_token
        device_info = {
            "deviceType": "980",
            "appVersion": "1",
            "deviceUuid": self.device_id,
        }
        payload = {
            "common": {
                "userInfo": user_info,
                "deviceInfo": device_info,
            },
            "data": data_payload,
        }
        return payload

    def _request(self, method: str, endpoint: str, params: dict = None, headers: dict = None, payload: dict = None) -> Any[dict | str]:
        _headers = self.session.headers.copy()
        if headers:
            _headers.update(headers)

        req = Request(method, endpoint, headers=_headers, params=params, json=payload)
        prepped = self.session.prepare_request(req)

        try:
            res = self.session.send(prepped)
            res.raise_for_status()
            data = res.json()
            if error_code := data["common"]["result"]["errorCode"]:
                raise ConnectionError(f"{error_code}: {data['common']['result']['errorMessage']}")
            return data
        except Exception as e:
            self.log.error(f"API Request failed: {e}", exc_info=False)
            sys.exit(1)
