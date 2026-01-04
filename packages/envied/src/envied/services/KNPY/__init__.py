import base64
import json
import re
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from typing import List, Optional

import click
import jwt
from langcodes import Language

from envied.core.constants import AnyTrack
from envied.core.credential import Credential
from envied.core.manifests import DASH
from envied.core.search_result import SearchResult
from envied.core.service import Service
from envied.core.titles import Episode, Movie, Movies, Series, Title_T, Titles_T
from envied.core.tracks import Subtitle, Tracks


class KNPY(Service):
    """
    Service code for Kanopy (kanopy.com).
    Version: 1.0.0

    Auth: Credential (username + password)
    Security: FHD@L3

    Handles both Movies and Series (Playlists).
    Detects and stops for movies that require tickets.
    Caching included
    """

    # Updated regex to match the new URL structure with library subdomain and path
    TITLE_RE = r"^https?://(?:www\.)?kanopy\.com/.+/(?P<id>\d+)$"
    GEOFENCE = ()
    NO_SUBTITLES = False

    @staticmethod
    @click.command(name="KNPY", short_help="https://kanopy.com")
    @click.argument("title", type=str)
    @click.pass_context
    def cli(ctx, **kwargs):
        return KNPY(ctx, **kwargs)

    def __init__(self, ctx, title: str):
        super().__init__(ctx)
        if not self.config:
            raise ValueError("KNPY configuration not found. Ensure config.yaml exists.")
        
        self.cdm = ctx.obj.cdm

        match = re.match(self.TITLE_RE, title)
        if match:
            self.content_id = match.group("id")
        else:
            self.content_id = None
            self.search_query = title
        
        self.API_VERSION = self.config["client"]["api_version"]
        self.USER_AGENT = self.config["client"]["user_agent"]
        self.WIDEVINE_UA = self.config["client"]["widevine_ua"]

        self.session.headers.update({
            "x-version": self.API_VERSION,
            "user-agent": self.USER_AGENT
        })

        self._jwt = None
        self._visitor_id = None
        self._user_id = None
        self._domain_id = None
        self.widevine_license_url = None

    def authenticate(self, cookies: Optional[CookieJar] = None, credential: Optional[Credential] = None) -> None:
        if not credential or not credential.username or not credential.password:
            raise ValueError("Kanopy requires email and password for authentication.")

        cache = self.cache.get("auth_token")
        
        if cache and not cache.expired:
            cached_data = cache.data
            valid_token = None

            if isinstance(cached_data, dict) and "token" in cached_data:
                if cached_data.get("username") == credential.username:
                    valid_token = cached_data["token"]
                    self.log.info("Using cached authentication token")
                else:
                    self.log.info(f"Cached token belongs to '{cached_data.get('username')}', but logging in as '{credential.username}'. Re-authenticating.")
            
            elif isinstance(cached_data, str):
                self.log.info("Found legacy cached token format. Re-authenticating to ensure correct user.")
            
            if valid_token:
                self._jwt = valid_token
                self.session.headers.update({"authorization": f"Bearer {self._jwt}"})

                if not self._user_id or not self._domain_id or not self._visitor_id:
                    try:
                        decoded_jwt = jwt.decode(self._jwt, options={"verify_signature": False})
                        self._user_id = decoded_jwt["data"]["uid"]
                        self._visitor_id = decoded_jwt["data"]["visitor_id"]
                        self.log.info(f"Extracted user_id and visitor_id from cached token.")
                        self._fetch_user_details()
                        return 
                    except (KeyError, jwt.DecodeError) as e:
                        self.log.error(f"Could not decode cached token: {e}. Re-authenticating.")
                        
        self.log.info("Performing handshake to get visitor token...")
        r = self.session.get(self.config["endpoints"]["handshake"])
        r.raise_for_status()
        handshake_data = r.json()
        self._visitor_id = handshake_data["visitorId"]
        initial_jwt = handshake_data["jwt"]

        self.log.info(f"Logging in as {credential.username}...")
        login_payload = {
            "credentialType": "email",
            "emailUser": {
                "email": credential.username,
                "password": credential.password
            }
        }
        r = self.session.post(
            self.config["endpoints"]["login"],
            json=login_payload,
            headers={"authorization": f"Bearer {initial_jwt}"}
        )
        r.raise_for_status()
        login_data = r.json()
        self._jwt = login_data["jwt"]
        self._user_id = login_data["userId"]
        
        self.session.headers.update({"authorization": f"Bearer {self._jwt}"})
        self.log.info(f"Successfully authenticated as {credential.username}")

        self._fetch_user_details()

        try:
            decoded_jwt = jwt.decode(self._jwt, options={"verify_signature": False})
            exp_timestamp = decoded_jwt.get("exp")
            
            cache_payload = {
                "token": self._jwt,
                "username": credential.username
            }

            if exp_timestamp:
                expiration_in_seconds = int(exp_timestamp - datetime.now(timezone.utc).timestamp())
                self.log.info(f"Caching token for {expiration_in_seconds / 60:.2f} minutes.")
                cache.set(data=cache_payload, expiration=expiration_in_seconds)
            else:
                self.log.warning("JWT has no 'exp' claim, caching for 1 hour as a fallback.")
                cache.set(data=cache_payload, expiration=3600)
        except Exception as e:
            self.log.error(f"Failed to decode JWT for caching: {e}. Caching for 1 hour as a fallback.")
            cache.set(
                data={"token": self._jwt, "username": credential.username}, 
                expiration=3600
            )

    def _fetch_user_details(self):
        self.log.info("Fetching user library memberships...")
        r = self.session.get(self.config["endpoints"]["memberships"].format(user_id=self._user_id))
        r.raise_for_status()
        memberships = r.json()

        for membership in memberships.get("list", []):
            if membership.get("status") == "active" and membership.get("isDefault", False):
                self._domain_id = str(membership["domainId"])
                self.log.info(f"Using default library domain: {membership.get('sitename', 'Unknown')} (ID: {self._domain_id})")
                return
        
        if memberships.get("list"):
            self._domain_id = str(memberships["list"][0]["domainId"])
            self.log.warning(f"No default library found. Using first active domain: {self._domain_id}")
        else:
            raise ValueError("No active library memberships found for this user.")

    def get_titles(self) -> Titles_T:
        if not self.content_id:
            raise ValueError("A content ID is required to get titles. Use a URL or run a search first.")
        if not self._domain_id:
            raise ValueError("Domain ID not set. Authentication may have failed.")

        r = self.session.get(self.config["endpoints"]["video_info"].format(video_id=self.content_id, domain_id=self._domain_id))
        r.raise_for_status()
        content_data = r.json()
        
        content_type = content_data.get("type")
        
        def parse_lang(data):
            try:
                langs = data.get("languages", [])
                if langs and isinstance(langs, list) and len(langs) > 0:
                    return Language.find(langs[0]) 
            except:
                pass
            return Language.get("en")

        if content_type == "video":
            video_data = content_data["video"]
            movie = Movie(
                id_=str(video_data["videoId"]),
                service=self.__class__,
                name=video_data["title"],
                year=video_data.get("productionYear"),
                description=video_data.get("descriptionHtml", ""),
                language=parse_lang(video_data),
                data=video_data,
            )
            return Movies([movie])

        elif content_type == "playlist":
            playlist_data = content_data["playlist"]
            series_title = playlist_data["title"]
            series_year = playlist_data.get("productionYear")
            
            season_match = re.search(r'(?:Season|S)\s*(\d+)', series_title, re.IGNORECASE)
            season_num = int(season_match.group(1)) if season_match else 1

            r = self.session.get(self.config["endpoints"]["video_items"].format(video_id=self.content_id, domain_id=self._domain_id))
            r.raise_for_status()
            items_data = r.json()

            episodes = []
            for i, item in enumerate(items_data.get("list", [])):
                if item.get("type") != "video":
                    continue
                
                video_data = item["video"]
                ep_num = i + 1
                
                ep_title = video_data.get("title", "")
                ep_match = re.search(r'Ep(?:isode)?\.?\s*(\d+)', ep_title, re.IGNORECASE)
                if ep_match:
                    ep_num = int(ep_match.group(1))

                episodes.append(
                    Episode(
                        id_=str(video_data["videoId"]),
                        service=self.__class__,
                        title=series_title,
                        season=season_num,
                        number=ep_num,
                        name=video_data["title"],
                        description=video_data.get("descriptionHtml", ""),
                        year=video_data.get("productionYear", series_year),
                        language=parse_lang(video_data),
                        data=video_data,
                    )
                )
            
            series = Series(episodes)
            series.name = series_title
            series.description = playlist_data.get("descriptionHtml", "")
            series.year = series_year
            return series
        
        else:
            raise ValueError(f"Unsupported content type: {content_type}")

    def get_tracks(self, title: Title_T) -> Tracks:
        play_payload = {
            "videoId": int(title.id),
            "domainId": int(self._domain_id),
            "userId": int(self._user_id),
            "visitorId": self._visitor_id
        }

        self.session.headers.setdefault("authorization", f"Bearer {self._jwt}")
        self.session.headers.setdefault("x-version", self.API_VERSION)
        self.session.headers.setdefault("user-agent", self.USER_AGENT)

        r = self.session.post(self.config["endpoints"]["plays"], json=play_payload)
        response_json = None
        try:
            response_json = r.json()
        except Exception:
            pass

        # Handle known errors gracefully
        if r.status_code == 403:
            if response_json and response_json.get("errorSubcode") == "playRegionRestricted":
                self.log.error("Kanopy reports: This video is not available in your country.")
                raise PermissionError(
                    "Playback blocked by region restriction. Try connecting through a supported country or verify your library’s access region."
                )
            else:
                self.log.error(f"Access forbidden (HTTP 403). Response: {response_json}")
                raise PermissionError("Kanopy denied access to this video. It may require a different library membership or authentication.")
        
        # Raise for any other HTTP errors
        r.raise_for_status()
        play_data = response_json or r.json()

        manifest_url = None
        for manifest in play_data.get("manifests", []):
            if manifest["manifestType"] == "dash":
                url = manifest["url"]
                manifest_url = f"https://kanopy.com{url}" if url.startswith("/") else url
                drm_type = manifest.get("drmType")
                if drm_type == "kanopyDrm":
                    play_id = play_data.get("playId")
                    self.widevine_license_url = self.config["endpoints"]["widevine_license"].format(license_id=f"{play_id}-0")
                elif drm_type == "studioDrm":
                    license_id = manifest.get("drmLicenseID", f"{play_data.get('playId')}-1")
                    self.widevine_license_url = self.config["endpoints"]["widevine_license"].format(license_id=license_id)
                else:
                    self.log.warning(f"Unknown drmType: {drm_type}")
                    self.widevine_license_url = None
                break

        if not manifest_url:
            raise ValueError("Could not find a DASH manifest for this title.")
        if not self.widevine_license_url:
            raise ValueError("Could not construct Widevine license URL.")

        self.log.info(f"Fetching DASH manifest from: {manifest_url}")
        r = self.session.get(manifest_url)
        r.raise_for_status()

        # Refresh headers for manifest parsing
        self.session.headers.clear()
        self.session.headers.update({
            "User-Agent": self.WIDEVINE_UA,
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
        })

        tracks = DASH.from_text(r.text, url=manifest_url).to_tracks(language=title.language)
        for caption_data in play_data.get("captions", []):
            lang = caption_data.get("language", "en")
            for file_info in caption_data.get("files", []):
                if file_info.get("type") == "webvtt":
                    tracks.add(Subtitle(
                        id_=f"caption-{lang}",
                        url=file_info["url"],
                        codec=Subtitle.Codec.WebVTT,
                        language=Language.get(lang)
                    ))
                    break
                    
        return tracks


    def get_widevine_license(self, *, challenge: bytes, title: Title_T, track: AnyTrack) -> bytes:
        if not self.widevine_license_url:
            raise ValueError("Widevine license URL was not set. Call get_tracks first.")
  
        license_headers = {
            "Content-Type": "application/octet-stream",
            "User-Agent": self.WIDEVINE_UA,
            "Authorization": f"Bearer {self._jwt}", 
            "X-Version": self.API_VERSION
        }

        r = self.session.post(
            self.widevine_license_url,
            data=challenge,
            headers=license_headers  
        )
        r.raise_for_status()
        return r.content

    # def search(self) -> List[SearchResult]:
    #     if not hasattr(self, 'search_query'):
    #         self.log.error("Search query not set. Cannot search.")
    #         return []
            
    #     self.log.info(f"Searching for '{self.search_query}'...")
    #     params = {
    #         "query": self.search_query,
    #         "sort": "relevance",
    #         "domainId": self._domain_id,
    #         "page": 0,
    #         "perPage": 20
    #     }
    #     r = self.session.get(self.config["endpoints"]["search"], params=params)
    #     r.raise_for_status()
    #     search_data = r.json()

    #     results = []
    #     for item in search_data.get("list", []):
    #         item_type = item.get("type")
    #         if item_type not in ["playlist", "video"]:
    #             continue

    #         video_id = item.get("videoId")
    #         title = item.get("title", "No Title")
    #         label = "Series" if item_type == "playlist" else "Movie"
            
    #         results.append(
    #             SearchResult(
    #                 id_=str(video_id),
    #                 title=title,
    #                 description="",
    #                 label=label,
    #                 url=f"https://www.kanopy.com/watch/{video_id}" 
    #             )
    #         )
    #     return results

    def get_chapters(self, title: Title_T) -> list:
        return []
