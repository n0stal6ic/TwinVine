from __future__ import annotations

import re
import sys
import uuid
from collections.abc import Generator
from http.cookiejar import CookieJar
from typing import Any, Optional, Union

import click
from click import Context
from requests import Request

from envied.core.downloaders import n_m3u8dl_re
from envied.core.credential import Credential
from envied.core.manifests import HLS
from envied.core.search_result import SearchResult
from envied.core.service import Service
from envied.core.titles import Episode, Movie, Movies, Series
from envied.core.tracks import Chapters, Tracks, Video, Hybrid
from envied.core.utils.collections import as_list

from . import queries


class DSNP(Service):
    """
    \b
    Service code for DisneyPlus streaming service (https://www.disneyplus.com).

    \b
    Authorization: Credentials
    Robustness:
        Widevine:
            L1: 2160p, 1080p
            L3: 720p
        PlayReady:
            SL3: 2160p, 1080p

    \b
    Tips:
        - Input should be only the entity ID for both series and movies:
            MOVIE: entity-99e15d53-926e-4074-b9f4-6524d10c8bed
            SERIES: entity-30429ad6-dd12-41bf-924e-19131fa66bb5
        - Use the --lang LANG_RANGE option to request non-english tracks
        - CDM level dictates playback quality (L3 == 720p, L1 == 1080p, 2160p)

    \b
    Notes:
        - On first run, the program will look for the first account profile that doesn't
          have kids mode or pin protection enabled. If none are found, the program will exit.
        - The profile will be cached and re-used until cache is cleared.

    """

    @staticmethod
    @click.command(name="DSNP", short_help="https://www.disneyplus.com", help=__doc__)
    @click.argument("title", type=str)
    @click.option(
        "-m", "--movie", is_flag=True, default=False, help="Title is a Movie."
    )
    @click.pass_context
    def cli(ctx: Context, **kwargs: Any) -> DSNP:
        return DSNP(ctx, **kwargs)

    def __init__(self, ctx: Context, title: str, movie):
        self.title = title
        self.movie = movie
        super().__init__(ctx)
        self.cdm = ctx.obj.cdm

        vcodec = ctx.parent.params.get("vcodec")
        range = ctx.parent.params.get("range_")

        self.range = range[0].name if range else "SDR"
        self.vcodec = "H.265" if vcodec and vcodec == Video.Codec.HEVC else "H.264"
        if self.range != "SDR" and self.vcodec != "H.265":
            self.vcodec = "H.265"

    def authenticate(self, cookies: Optional[CookieJar] = None, credential: Optional[Credential] = None) -> None:
        super().authenticate(cookies, credential)
        if not credential:
            raise EnvironmentError("Service requires Credentials for Authentication.")

        # Use exact headers from working Vinetrimmer implementation to avoid geoblocking
        self.session.headers.update({
            "Accept-Language": "en-US,en;q=0.5",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Origin": "https://www.disneyplus.com",
            "x-bamsdk-platform": "javascript/windows/chrome",
            "x-bamsdk-version": "28.0",
            "x-bamsdk-client-id": "disney-svod",
            "Accept-Encoding": "gzip",
        })
        self.session.headers.update({"x-bamsdk-transaction-id": str(uuid.uuid4())})
        self.prd_config = self.session.get(self.config["CONFIG_URL"]).json()

        self._cache = self.cache.get(f"tokens_{credential.sha1}")
        if self._cache:
            self.log.info(" + Refreshing Tokens")
            profile = self.refresh_token(self._cache.data["token"]["refreshToken"])
            self._cache.set(profile, expiration=profile["token"]["expiresIn"] - 30)
            token = self._cache.data["token"]["accessToken"]
            self.session.headers.update({"Authorization": "Bearer {}".format(token)})
            self.active_session = self.account()["activeSession"]
        else:
            self.log.info(" + Setting up new profile...")
            token = self.register_device()
            status = self.check_email(credential.username, token)
            if status.lower() == "register":
                raise ValueError("Account is not registered. Please register first.")
            elif status.lower() == "otp":
                self.log.error(" - Account requires passcode for login.")
                sys.exit(1)

            else:
                tokens = self.login(credential.username, credential.password, token)
                self.session.headers.update({"Authorization": "Bearer {}".format(tokens["accessToken"])})
                account = self.account()
                profile_id = next(
                    (
                        x.get("id")
                        for x in account["account"]["profiles"]
                        if not x["attributes"]["kidsModeEnabled"]
                        and not x["attributes"]["parentalControls"]["isPinProtected"]
                    ),
                    None,
                )
                if not profile_id:
                    self.log.error(
                        " - Missing profile - you need at least one profile with kids mode and pin protection disabled"
                    )
                    sys.exit(1)

                set_profile = self.switch_profile(profile_id)
                profile = self.refresh_token(set_profile["token"]["refreshToken"])
                self._cache.set(profile, expiration=profile["token"]["expiresIn"] - 30)
                token = self._cache.data["token"]["accessToken"]
                self.session.headers.update({"Authorization": "Bearer {}".format(token)})
                self.active_session = self.account()["activeSession"]

            self.log.info(" + Acquired tokens...")

    def search(self) -> Generator[SearchResult, None, None]:
        params = {
            "query": self.title,
        }
        endpoint = self.href(
            self.prd_config["services"]["explore"]["client"]["endpoints"]["search"]["href"],
            version=self.config["EXPLORE_VERSION"],
        )
        data = self._request("GET", endpoint, params=params)["data"]["page"]
        if not data.get("containers"):
            return

        results = data["containers"][0]["items"]

        for result in results:
            entity = "entity-" + result.get("id")
            yield SearchResult(
                id_=entity,
                title=result["visuals"].get("title"),
                description=result["visuals"]["description"].get("brief"),
                label=result["visuals"]["metastringParts"].get("releaseYearRange", {}).get("startYear"),
                url=f"https://www.disneyplus.com/browse/{entity}",
            )

    def get_titles(self) -> Union[Movies, Series]:
        # Use Vinetrimmer logic - handle both entity IDs and other formats
        if not "entity" in self.title:
            # Convert to entity ID like Vinetrimmer does
            try:
                deeplinkId_response = self.session.get(
                    url='https://disney.api.edge.bamgrid.com/explore/v1.3/deeplink',
                    params={
                        'refId': self.title,
                        'refIdType': 'encodedFamilyId'  # Try movie first
                    }
                )
                if deeplinkId_response.status_code == 200:
                    deeplinkId = deeplinkId_response.json()
                    self.title = deeplinkId["data"]["deeplink"]["actions"][0]["deeplinkId"]
                else:
                    # Try with encodedSeriesId
                    deeplinkId_response = self.session.get(
                        url='https://disney.api.edge.bamgrid.com/explore/v1.3/deeplink',
                        params={
                            'refId': self.title,
                            'refIdType': 'encodedSeriesId'
                        }
                    )
                    if deeplinkId_response.status_code == 200:
                        deeplinkId = deeplinkId_response.json()
                        self.title = deeplinkId["data"]["deeplink"]["actions"][0]["deeplinkId"]
            except Exception:
                # If all fails, assume it's already an entity ID
                pass

        content = self.get_deeplink(self.title)
        
        # Handle deeplink response structure - determine if series or movie from infoBlock
        if "data" in content and "deeplink" in content["data"]:
            actions = content["data"]["deeplink"]["actions"]
            if actions and len(actions) > 0:
                action = actions[0]
                # Check the infoBlock to determine content type like Vinetrimmer does
                info_block = action.get("infoBlock", "")
                if "urn:ds:cmp:eva:series" in info_block:
                    _type = "series"
                elif "urn:ds:cmp:eva:movie" in info_block or "urn:ds:cmp:eva:film" in info_block:
                    _type = "movie"
                else:
                    # Fallback: assume series for browse type
                    _type = "series" if action.get("type") == "browse" else "movie"
            else:
                raise ValueError("No actions found in deeplink response")
        else:
            raise ValueError("Invalid deeplink response structure")

        if _type == "movie" or self.movie:
            movie = self._movie(self.title)
            return Movies(movie)

        elif _type == "series":
            episodes = self._show(self.title)
            return Series(episodes)
        
        else:
            raise ValueError(f"Unknown content type: {_type}")

    def get_tracks(self, title: Union[Movie, Episode]) -> Tracks:
        resource_id = title.data.get("resourceId")
    
        # ===============================
        # TOKEN REFRESH
        # ===============================
        if self._cache:
            refresh_token = self._cache.data["token"]["refreshToken"]
            fresh_token = self.refresh_token(refresh_token)
            self._cache.set(fresh_token, expiration=fresh_token["token"]["expiresIn"] - 30)
    
            # Update session header
            token = fresh_token["token"]["accessToken"]
            self.session.headers.update({"Authorization": f"Bearer {token}"})
    
        # ===============================
        # INTERNAL MANIFEST FETCHER
        # ===============================
        def get_manifest_for_scenario(scenario_name, quality=None):
            original_headers = dict(self.session.headers)
    
            # Vinetrimmer-style headers
            self.session.headers.update({
                'x-dss-feature-filtering': 'true',
                'x-application-version': '1.1.2',
                'x-bamsdk-client-id': 'disney-svod',
                'x-bamsdk-platform': 'javascript/windows/chrome',
                'x-bamsdk-version': '28.0'
            })
    
            # Default resolution (Vinetrimmer-style)
            if quality is None:
                quality = '1280' if hasattr(self, 'cdm') and self.cdm.security_level == 3 else '1920'
    
            json_data = {
                'playback': {
                    'attributes': {
                        'resolution': {
                            'max': [quality],
                        },
                        'protocol': 'HTTPS',
                        'assetInsertionStrategy': 'SGAI',
                        'playbackInitiationContext': 'ONLINE',
                        'frameRates': [60],
                    },
                },
                'playbackId': resource_id,
            }
    
            try:
                res = self.session.post(
                    f'https://disney.playback.edge.bamgrid.com/v7/playback/{scenario_name}',
                    json=json_data
                )
                if res.status_code == 200:
                    data = res.json()
                    manifest_url = data["stream"]["sources"][0]['complete']['url']
                    return HLS.from_url(url=manifest_url, session=self.session).to_tracks(language="en-US")
                return None
            finally:
                self.session.headers.clear()
                self.session.headers.update(original_headers)
    
        # ==========================================================
        # =============== HYBRID RANGE (DV + HDR10) =================
        # ==========================================================
        if self.range == "HYBRID":
            self.log.info("HYBRID mode — fetching HDR10 + DV manifests")
    
            all_tracks = Tracks()
    
            # -------- Fetch HDR10 ------------
            self.log.info("Fetching HDR10 tracks")
            self.range = Video.Range.HDR10
            hdr_scenario = 'tv-drm-cbcs-h265-hdr10'
            hdr_tracks = get_manifest_for_scenario(hdr_scenario, '2160')
            if hdr_tracks:
                all_tracks.add(hdr_tracks, warn_only=True)
    
            # -------- Fetch DV ---------------
            self.log.info("Fetching DV tracks")
            self.range = Video.Range.DV
            dv_scenario = 'tv-drm-cbcs-h265-dovi'
            dv_tracks = get_manifest_for_scenario(dv_scenario, '2160')
            if dv_tracks:
                all_tracks.add(dv_tracks, warn_only=True)
    
            # Restore range
            self.range = "HYBRID"
    
            tracks = all_tracks
            self.log.info("HYBRID fetch complete — merge will occur after download")
    
        # ==========================================================
        # =============== NON-HYBRID MODES =========================
        # ==========================================================
        else:
            if self.vcodec == "H.265" and self.range == 'HDR10':
                scenario = 'tv-drm-cbcs-h265-hdr10'
                quality = '2160'
            elif self.vcodec == "H.265" and self.range == 'DV':
                scenario = 'tv-drm-cbcs-h265-dovi'
                quality = '2160'
            else:
                scenario = 'tv-drm-cbcs'
                quality = '1920'
    
            tracks = get_manifest_for_scenario(scenario, quality)
            if not tracks:
                raise ValueError("Failed to fetch DSNP manifest")
    
        # =====================================================
        # Fetch ATMOS/H265 secondary manifest (like Vinetrimmer)
        # =====================================================
        atmos_tracks = get_manifest_for_scenario('tv-drm-ctr-h265-atmos')
        if atmos_tracks:
            tracks.videos.extend(atmos_tracks.videos)
            tracks.audio.extend(atmos_tracks.audio)
            tracks.subtitles.extend(atmos_tracks.subtitles)
    
        # =====================================================
        # AUDIO BITRATE FIX
        # =====================================================
        for audio in tracks.audio:
            bitrate = re.search(
                r"(?<=r/composite_)\d+|\d+(?=_complete.m3u8)",
                as_list(audio.url)[0],
            )
            audio.bitrate = int(bitrate.group()) * 1000
            if audio.bitrate == 1000_000:  # DSNP lies about Atmos
                audio.bitrate = 768_000
    
        # =====================================================
        # FINAL CONFIG
        # =====================================================
        for track in tracks:
            if track not in tracks.attachments:
                track.downloader = n_m3u8dl_re
                track.needs_repack = True
    
        return tracks

    def get_chapters(self, title: Union[Movie, Episode]) -> Chapters:
        return Chapters()

    def get_widevine_service_certificate(self, **_: Any) -> str:
        return None

    def get_widevine_license(self, *, challenge: bytes, title, track) -> bytes:
        """Get Widevine license for Disney+ content - Adapted from working Vinetrimmer implementation"""
        # Force token refresh like Vinetrimmer does for license calls
        if self._cache:
            refresh_token = self._cache.data["token"]["refreshToken"]
            fresh_token = self.refresh_token(refresh_token)
            self._cache.set(fresh_token, expiration=fresh_token["token"]["expiresIn"] - 30)
            token = fresh_token["token"]["accessToken"]
        else:
            return None
            
        headers = {
            "Authorization": f'Bearer {token}',
            "Content-Type": "application/octet-stream",
        }
        r = self.session.post(url=self.config["LICENSE"], headers=headers, data=challenge)
        if r.status_code != 200:
            raise ConnectionError(r.text)
        return r.content

    def get_playready_license(self, *, challenge: bytes, title, track) -> Optional[bytes]:
        """Get PlayReady license for Disney+ content - Adapted from working Vinetrimmer implementation"""
        # Refresh token if needed  
        token = self._cache.data["token"]["accessToken"] if self._cache else None
        if not token:
            return None
            
        r = self.session.post(
            url='https://disney.playback.edge.bamgrid.com/playready/v1/obtain-license.asmx',
            headers={'authorization': f'Bearer {token}'},
            data=challenge
        )
        
        if r.status_code != 200:
            raise ConnectionError(r.text)
        return r.text.encode()

    # Service specific functions

    def _show(self, title: str) -> Episode:
        page = self.get_page(title)
        container = next(x for x in page["containers"] if x.get("type") == "episodes")
        season_ids = [x.get("id") for x in container["seasons"] if x.get("type") == "season"]

        episodes = []
        for season in season_ids:
            # Use direct Disney+ API URL like Vinetrimmer does
            endpoint = f'https://disney.api.edge.bamgrid.com/explore/v1.3/season/{season}'
            params = {'limit': 80}
            
            response = self.session.get(endpoint, params=params)
            if response.status_code == 200:
                data = response.json()["data"]["season"]["items"]
                episodes.extend(data)
            else:
                self.log.warning(f"Failed to get season {season}: {response.status_code}")

        return [
            Episode(
                id_=episode.get("id"),
                service=self.__class__,
                title=episode["visuals"].get("title"),
                year=episode["visuals"]["metastringParts"].get("releaseYearRange", {}).get("startYear"),
                season=int(episode["visuals"].get("seasonNumber", 0)),
                number=int(episode["visuals"].get("episodeNumber", 0)),
                name=episode["visuals"].get("episodeTitle"),
                data=next(x for x in episode["actions"] if x.get("type") == "playback"),
            )
            for episode in episodes
            if episode.get("type") == "view"
        ]

    def _movie(self, title: str) -> Movie:
        movie = self.get_page(title)

        return [
            Movie(
                id_=movie.get("id"),
                service=self.__class__,
                name=movie["visuals"].get("title"),
                year=movie["visuals"]["metastringParts"].get("releaseYearRange", {}).get("startYear"),
                data=next(x for x in movie["actions"] if x.get("type") == "playback"),
            )
        ]

    def _request(
        self,
        method: str,
        endpoint: str,
        params: dict = None,
        headers: dict = None,
        payload: dict = None,
    ) -> Any[dict | str]:
        _headers = headers if headers else self.session.headers
        prep = self.session.prepare_request(Request(method, endpoint, headers=_headers, params=params, json=payload))
        response = self.session.send(prep)
        
        # Check for geoblocking
        if response.status_code == 404 and 'x-dss-edge' in response.headers:
            edge_error = response.headers.get('x-dss-edge', '')
            if 'location.invalid' in edge_error:
                raise ConnectionError("Disney+ content is geoblocked in your region. Please use a VPN to US/supported region.")
        
        try:
            data = response.json()
            if data.get("errors"):
                code = data["errors"][0]["extensions"].get("code")

                if "token.service.unauthorized.client" in code:
                    raise ConnectionError("Unauthorized Client/IP: " + code)
                if "idp.error.identity.bad-credentials" in code:
                    raise ConnectionError("Bad Credentials: " + code)
                else:
                    raise ConnectionError(data["errors"])
            return data

        except Exception as e:
            if response.status_code == 404:
                raise ConnectionError(f"Disney+ content not found or geoblocked. Status: {response.status_code}")
            raise ConnectionError(f"Request failed. Status: {response.status_code}, Content: {response.content}")

    def get_page(self, title):
        # Use direct Disney+ API URL like Vinetrimmer does - no need for SDK endpoints
        params = {
            "disableSmartFocus": True,
            "enhancedContainersLimit": 12,
            "limit": 24,
        }
        
        # Use exact Vinetrimmer URL pattern
        endpoint = f'https://disney.api.edge.bamgrid.com/explore/v1.4/page/{title}'
        
        response = self.session.get(endpoint, params=params)
        if response.status_code == 200:
            return response.json()["data"]["page"]
        else:
            raise ConnectionError(f"Failed to get page data. Status: {response.status_code}")

    def get_video(self, content_id: str) -> dict:
        # Use Vinetrimmer's approach - get manifest directly using playback API
        # Add special headers like Vinetrimmer does to avoid geoblocking
        original_headers = dict(self.session.headers)
        self.session.headers.update({
            'x-dss-feature-filtering': 'true',
            'x-application-version': '1.1.2',
            'x-bamsdk-client-id': 'disney-svod',
            'x-bamsdk-platform': 'javascript/windows/chrome',
            'x-bamsdk-version': '28.0'
        })
        
        try:
            # Use playback API like Vinetrimmer with exact configuration
            # Use exact Vinetrimmer configuration - L3 CAN get 1080p if done correctly
            json_data = {
                'playback': {
                    'attributes': {
                        'resolution': {
                            'max': ['1920'],  # Same as Vinetrimmer - no artificial L3 limitation
                        },
                        'protocol': 'HTTPS',
                        'assetInsertionStrategy': 'SGAI',
                        'playbackInitiationContext': 'ONLINE',
                        'frameRates': [60],
                    },
                },
                'playbackId': content_id,
            }

            # Use exact Vinetrimmer playback URL and scenario
            manifest_response = self.session.post(
                'https://disney.playback.edge.bamgrid.com/v7/playback/tv-drm-ctr', 
                json=json_data
            )
            
            if manifest_response.status_code == 200:
                manifest_data = manifest_response.json()
                # Return in expected format
                return {
                    "video": {
                        "mediaMetadata": {
                            "playbackUrls": [{"url": manifest_data["stream"]["sources"][0]['complete']['url']}]
                        }
                    }
                }
            else:
                # Fallback: try original method but with new headers
                endpoint = self.href(
                    self.prd_config["services"]["content"]["client"]["endpoints"]["getDmcVideo"]["href"], 
                    contentId=content_id
                )
                data = self._request("GET", endpoint)["data"]["DmcVideo"]
                return data
        except Exception as e:
            # Final fallback
            endpoint = self.href(
                self.prd_config["services"]["content"]["client"]["endpoints"]["getDmcVideo"]["href"], 
                contentId=content_id
            )
            data = self._request("GET", endpoint)["data"]["DmcVideo"]
            return data
                
        finally:
            # Restore original headers
            self.session.headers.clear()
            self.session.headers.update(original_headers)

    def get_deeplink(self, ref_id: str) -> str:
        # Use direct Disney+ API URL like Vinetrimmer does
        params = {
            "refId": ref_id,
            "refIdType": "deeplinkId",
        }
        endpoint = "https://disney.api.edge.bamgrid.com/explore/v1.3/deeplink"
        
        response = self.session.get(endpoint, params=params)
        if response.status_code == 200:
            return response.json()
        else:
            raise ConnectionError(f"Failed to get deeplink. Status: {response.status_code}")

    def series_bundle(self, series_id: str) -> dict:
        endpoint = self.href(
            self.prd_config["services"]["content"]["client"]["endpoints"]["getDmcSeriesBundle"]["href"],
            encodedSeriesId=series_id,
        )

        return self.session.get(endpoint).json()["data"]["DmcSeriesBundle"]

    def refresh_token(self, refresh_token: str):
        payload = {
            "operationName": "refreshToken",
            "variables": {
                "input": {
                    "refreshToken": refresh_token,
                },
            },
            "query": queries.REFRESH_TOKEN,
        }

        endpoint = self.prd_config["services"]["orchestration"]["client"]["endpoints"]["refreshToken"]["href"]
        data = self._request("POST", endpoint, payload=payload, headers={"authorization": self.config["API_KEY"]})
        return data["extensions"]["sdk"]

    def _refresh(self):
        if not self._cache.expired:
            return self._cache.data["token"]["accessToken"]

        profile = self.refresh_token(self._cache.data["token"]["refreshToken"])
        self._cache.set(profile, expiration=profile["token"]["expiresIn"] - 30)
        return self._cache.data["token"]["accessToken"]

    def register_device(self) -> dict:
        payload = {
            "variables": {
                "registerDevice": {
                    "applicationRuntime": self.config.get("BAM_APPLICATION_RUNTIME", "android"),
                    "attributes": {
                        "operatingSystem": "Android",
                        "operatingSystemVersion": "8.1.0",
                    },
                    "deviceFamily": self.config.get("BAM_FAMILY", "browser"),  # Use Vinetrimmer family
                    "deviceLanguage": "en",
                    "deviceProfile": self.config.get("BAM_PROFILE", "tv"),
                }
            },
            "query": queries.REGISTER_DEVICE,
        }

        endpoint = self.prd_config["services"]["orchestration"]["client"]["endpoints"]["registerDevice"]["href"]
        data = self._request("POST", endpoint, payload=payload, headers={"authorization": self.config["API_KEY"]})
        return data["extensions"]["sdk"]["token"]["accessToken"]

    def login(self, email: str, password: str, token: str) -> dict:
        payload = {
            "operationName": "loginTv",
            "variables": {
                "input": {
                    "email": email,
                    "password": password,
                },
            },
            "query": queries.LOGIN,
        }

        endpoint = self.prd_config["services"]["orchestration"]["client"]["endpoints"]["query"]["href"]
        data = self._request("POST", endpoint, payload=payload, headers={"authorization": token})
        return data["extensions"]["sdk"]["token"]

    def href(self, href, **kwargs) -> str:
        _args = {
            "apiVersion": "{apiVersion}",
            "region": self.active_session["location"]["countryCode"],
            "impliedMaturityRating": 1850,
            "kidsModeEnabled": "false",
            "appLanguage": "en-US",
            "partner": "disney",
        }
        _args.update(**kwargs)

        href = href.format(**_args)

        # [3.0, 3.1, 3.2, 5.0, 3.3, 5.1, 6.0, 5.2, 6.1]
        api_version = "6.1"
        if "/search/" in href:
            api_version = "5.1"

        return href.format(apiVersion=api_version)

    def check_email(self, email: str, token: str) -> str:
        payload = {
            "operationName": "Check",
            "variables": {
                "email": email,
            },
            "query": queries.CHECK_EMAIL,
        }

        endpoint = self.prd_config["services"]["orchestration"]["client"]["endpoints"]["query"]["href"]
        data = self._request("POST", endpoint, payload=payload, headers={"authorization": token})
        return data["data"]["check"]["operations"][0]

    def account(self) -> dict:
        endpoint = self.prd_config["services"]["orchestration"]["client"]["endpoints"]["query"]["href"]

        payload = {
            "operationName": "EntitledGraphMeQuery",
            "variables": {},
            "query": queries.ENTITLEMENTS,
        }

        data = self._request("POST", endpoint, payload=payload)
        return data["data"]["me"]

    def switch_profile(self, profile_id: str) -> dict:
        payload = {
            "operationName": "switchProfile",
            "variables": {
                "input": {
                    "profileId": profile_id,
                },
            },
            "query": queries.SWITCH_PROFILE,
        }

        endpoint = self.prd_config["services"]["orchestration"]["client"]["endpoints"]["query"]["href"]
        data = self._request("POST", endpoint, payload=payload)
        return data["extensions"]["sdk"]


