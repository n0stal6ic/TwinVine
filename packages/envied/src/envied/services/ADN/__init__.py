import base64
import json
import re
import time
import uuid
import subprocess
import tempfile
import shutil
from typing import Generator, Optional, Union, List, Any
from pathlib import Path
from functools import partial

import click
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from langcodes import Language

from envied.core import binaries
from envied.core.config import config
from envied.core.manifests import HLS
from envied.core.search_result import SearchResult
from envied.core.service import Service
from envied.core.session import session
from envied.core.titles import Episode, Series
from envied.core.tracks import Tracks, Chapters, Chapter
from envied.core.tracks.audio import Audio
from envied.core.tracks.video import Video
from envied.core.tracks.audio import Audio
from envied.core.tracks.subtitle import Subtitle


class VideoNoAudio(Video):
    """
    Video track qui enlève automatiquement l'audio après téléchargement.
    Nécessaire car ADN fournit des streams HLS avec audio muxé.
    """
    
    def download(self, session, prepare_drm, max_workers=None, progress=None, *, cdm=None):
        """Override : télécharge puis demuxe pour enlever l'audio."""
        import logging
        log = logging.getLogger('ADN.VideoNoAudio')
        
        # Téléchargement normal
        super().download(session, prepare_drm, max_workers, progress, cdm=cdm)
        
        # Si pas de path, échec du téléchargement
        if not self.path or not self.path.exists():
            return
        
        # Vérifier FFmpeg disponible
        if not binaries.FFMPEG:
            log.warning("FFmpeg not found, cannot remove audio from video")
            return
        
        # Demuxer : enlever l'audio
        if progress:
            progress(downloaded="Removing audio")
        
        original_path = self.path
        noaudio_path = original_path.with_stem(f"{original_path.stem}_noaudio")
        
        try:
            log.debug(f"Removing audio from {original_path.name}")
            
            result = subprocess.run(
                [
                    binaries.FFMPEG,
                    '-i', str(original_path),
                    '-vcodec', 'copy',  # Copie vidéo sans réencodage
                    '-an',              # Enlève l'audio
                    '-y',
                    str(noaudio_path)
                ],
                capture_output=True,
                text=True,
                timeout=120
            )
            
            if result.returncode != 0:
                log.error(f"FFmpeg demux failed: {result.stderr}")
                noaudio_path.unlink(missing_ok=True)
                return
            
            if not noaudio_path.exists() or noaudio_path.stat().st_size < 1000:
                log.error("Demuxed video is empty or too small")
                noaudio_path.unlink(missing_ok=True)
                return
            
            # Remplacer le fichier original
            log.debug(f"Video demuxed successfully: {noaudio_path.stat().st_size} bytes")
            original_path.unlink()
            noaudio_path.rename(original_path)
            
            if progress:
                progress(downloaded="Downloaded")
                
        except subprocess.TimeoutExpired:
            log.error("FFmpeg demux timeout")
            noaudio_path.unlink(missing_ok=True)
        except Exception as e:
            log.error(f"Failed to demux video: {e}")
            noaudio_path.unlink(missing_ok=True)


class AudioExtracted(Audio):
    """
    Audio track déjà extrait d'un flux HLS muxé.
    Override download() pour copier le fichier au lieu de télécharger.
    """
    
    def __init__(self, *args, extracted_path: Path, **kwargs):
        # URL vide pour éviter que curl essaie de télécharger
        super().__init__(*args, url="", **kwargs)
        self.extracted_path = extracted_path
    
    def download(self, session, prepare_drm, max_workers=None, progress=None, *, cdm=None):
        """Override : copie le fichier extrait au lieu de télécharger."""
        if not self.extracted_path or not self.extracted_path.exists():
            if progress:
                progress(downloaded="[red]FAILED")
            raise ValueError(f"Extracted audio file not found: {self.extracted_path}")
        
        # Créer le path de destination (même logique que Track.download)
        track_type = self.__class__.__name__
        save_path = config.directories.temp / f"{track_type}_{self.id}.m4a"
        
        if progress:
            progress(downloaded="Copying", total=100, completed=0)
        
        # Copier le fichier extrait vers le path final
        config.directories.temp.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.extracted_path, save_path)
        
        self.path = save_path
        
        if progress:
            progress(downloaded="Downloaded", completed=100)


class SubtitleEmbedded(Subtitle):
    """
    Subtitle avec contenu embarqué (data URI).
    Override download() pour écrire le contenu directement.
    """
    
    def __init__(self, *args, embedded_content: str, **kwargs):
        # URL vide pour éviter que curl essaie de télécharger
        super().__init__(*args, url="", **kwargs)
        self.embedded_content = embedded_content
    
    def download(self, session, prepare_drm, max_workers=None, progress=None, *, cdm=None):
        """Override : écrit le contenu embarqué au lieu de télécharger."""
        if not self.embedded_content:
            if progress:
                progress(downloaded="[red]FAILED")
            raise ValueError("No embedded content in subtitle")
        
        # Créer le path de destination
        track_type = "Subtitle"
        save_path = config.directories.temp / f"{track_type}_{self.id}.{self.codec.extension}"
        
        if progress:
            progress(downloaded="Writing", total=100, completed=0)
        
        # Ã‰crire le contenu
        config.directories.temp.mkdir(parents=True, exist_ok=True)
        save_path.write_text(self.embedded_content, encoding='utf-8')
        
        self.path = save_path
        
        if progress:
            progress(downloaded="Downloaded", completed=100)


class ADN(Service):
    """
    Service code for Animation Digital Network (ADN).

    \b
    Version: 3.2.1 (FINAL - Full multi-audio/subtitle support with custom Track classes)
    Authorization: Credentials
    Robustness:
        Video: Clear HLS (Highest Quality)
        Audio: Pre-extracted from muxed streams with AudioExtracted class
        Subs: AES-128 Encrypted JSON -> ASS format with SubtitleEmbedded class
    
    Technical Solution:
    - ADN provides HLS streams with muxed video+audio (not separable)
    - AudioExtracted: Extracts audio in get_tracks(), copies during download()
    - SubtitleEmbedded: Decrypts and converts to ASS, writes during download()
    - Result: MKV with 1 video + multiple audio tracks + subtitles
    
    Custom Track Classes:
    - AudioExtracted: Bypasses curl file:// limitation with direct file copy
    - SubtitleEmbedded: Bypasses requests data: limitation with direct write
    Made by: guilara_tv
    """

    

    RSA_PUBLIC_KEY = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQCbQrCJBRmaXM4gJidDmcpWDssg
numHinCLHAgS4buMtdH7dEGGEUfBofLzoEdt1jqcrCDT6YNhM0aFCqbLOPFtx9cg
/X2G/G5bPVu8cuFM0L+ehp8s6izK1kjx3OOPH/kWzvstM5tkqgJkNyNEvHdeJl6
KhS+IFEqwvZqgbBpKuwIDAQAB
-----END PUBLIC KEY-----"""

    TITLE_RE = r"^(?:https?://(?:www\.)?animationdigitalnetwork\.com/video/[^/]+/)?(?P<id>\d+)"

    @staticmethod
    def get_session():
        return session("okhttp4")

    @staticmethod
    @click.command(
        name="ADN", 
        short_help="Téléchargement depuis Animation Digital Network",
        help=(
            "Télécharge des séries ou films depuis ADN.\n\n"
            "TITLE : L'URL de la série ou son ID (ex: 1125).\n\n"
            "SYSTÈME DE SÉLECTION :\n"
            "  - Simple :  '-e 1-5' (épisodes 1 à 5)\n"
            "  - Saisons : '-e S2' ou '-e S02'  (toute la saison 2) ou '-e S2E1-12'\n"
            "  - Mixte :   '-e 1,3,S2E5' ou '-e 1,3,S02E05'\n"
            "  - Bonus :   '-e NC1,OAV1'"
        )
    )
    @click.argument("title", type=str, required=True)
    @click.option(
        "-e", "--episode", "select", type=str,
        help="Sélection : numéros, plages (5-10), saisons (S1, S2) ou combiné (S1E5)."
    )
    @click.option(
        "--but", is_flag=True,
        help="Inverse la sélection : télécharge tout SAUF les épisodes spécifiés avec -e."
    )
    @click.option(
        "--all", "all_eps", is_flag=True,
        help="Ignore toutes les restrictions et télécharge l'intégralité de la série."
    )
    @click.pass_context
    def cli(ctx, **kwargs) -> "ADN":
        return ADN(ctx, **kwargs)

    def __init__(self, ctx, title: str, select: Optional[str] = None, but: bool = False, all_eps: bool = False):
        self.title = title
        self.select_str = select
        self.but = but
        self.all_eps = all_eps
        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.token_expiration: Optional[int] = None
        
        super().__init__(ctx)

        self.locale = self.config.get("params", {}).get("locale", "fr")
        self.session.headers.update(self.config.get("headers", {}))
        self.session.headers["x-target-distribution"] = self.locale


    @staticmethod
    def _timecode_to_ms(tc: str) -> int:
        """Convert HH:MM:SS timecode to milliseconds."""
        parts = tc.split(':')
        hours = int(parts[0])
        minutes = int(parts[1])
        seconds = int(parts[2])
        return (hours * 3600 + minutes * 60 + seconds) * 1000

    @property
    def auth_header(self) -> dict:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "X-Access-Token": self.access_token
        }

    def ensure_authenticated(self) -> None:
        """Vérifie le token et rafraÃ®chit si nécessaire."""
        current_time = int(time.time())

        if self.access_token and self.token_expiration and current_time < (self.token_expiration - 60):
            return

        cache_key = f"adn_auth_{self.credential.sha1 if self.credential else 'default'}"
        cached = self.cache.get(cache_key)

        if cached and not cached.expired:
            self.access_token = cached.data["access_token"]
            self.refresh_token = cached.data["refresh_token"]
            self.token_expiration = cached.data["token_expiration"]
            self.session.headers.update(self.auth_header)
            self.log.debug("Loaded authentication from cache")
        else:
            self.authenticate(credential=self.credential)

    def authenticate(self, cookies=None, credential=None) -> None:
        super().authenticate(cookies, credential)

        if self.refresh_token:
            try:
                self._do_refresh()
                return
            except Exception:
                self.log.warning("Refresh failed, proceeding to full login")

        if not credential:
            raise ValueError("Credentials required for ADN")

        response = self.session.post(
            url=self.config["endpoints"]["login"],
            json={
                "username": credential.username,
                "password": credential.password,
                "source": "Web"
            }
        )

        if response.status_code != 200:
            self.log.error(f"Login failed: {response.status_code} - {response.text}")
            response.raise_for_status()

        self._save_tokens(response.json())

    def _do_refresh(self):
        response = self.session.post(
            url=self.config["endpoints"]["refresh"],
            json={"refreshToken": self.refresh_token},
            headers=self.auth_header
        )
        if response.status_code != 200:
            raise ValueError("Token refresh failed")
        self._save_tokens(response.json())

    def _save_tokens(self, data: dict):
        self.access_token = data["accessToken"]
        self.refresh_token = data["refreshToken"]
        expires_in = data.get("expires_in", 3600)
        self.token_expiration = int(time.time()) + expires_in
        self.session.headers.update(self.auth_header)

    def _parse_select(self, ep_id: str, short_number: str, season_num: int) -> bool:
            """Retourne True si l'épisode doit être inclus."""
            if self.all_eps or not self.select_str:
                return True

            # Préparation des identifiants possibles pour cet épisode
            # On teste : "30353" (id), "1" (numéro), "S02E01" (format complet), "S02" (saison entière)
            candidates = [
                str(ep_id),
                str(short_number).lstrip("0"),
                f"S{season_num:02d}E{int(short_number):02d}" if str(short_number).isdigit() else "",
                f"S{season_num:02d}"
            ]
            
            parts = re.split(r'[ ,]+', self.select_str.strip().upper())
            selection: set[str] = set()

            for part in parts:
                if '-' in part:
                    start_p, end_p = part.split('-', 1)
                    # Gestion des plages S02E01-S02E04
                    m_start = re.match(r'^S(\d+)E(\d+)$', start_p)
                    m_end = re.match(r'^S(\d+)E(\d+)$', end_p)
                    
                    if m_start and m_end:
                        s_start, e_start = map(int, m_start.groups())
                        s_end, e_end = map(int, m_end.groups())
                        if s_start == s_end: # Même saison
                            for i in range(e_start, e_end + 1):
                                selection.add(f"S{s_start:02d}E{i:02d}")
                        continue
                    
                    # Plages classiques (1-10)
                    nums = re.findall(r'\d+', part)
                    if len(nums) >= 2:
                        for i in range(int(nums[0]), int(nums[1]) + 1):
                            selection.add(str(i))
                else:
                    selection.add(part.lstrip("0"))

            included = any(c in selection for c in candidates if c)
            return not included if self.but else included

    def get_titles(self) -> Series:
            """Récupère les épisodes avec le titre réel de la série."""
            show_id = self.parse_show_id(self.title)
            
            # 1. Récupérer d'abord les infos globales du show pour avoir le titre propre
            show_url = self.config["endpoints"]["show"].format(show_id=show_id)
            show_res = self.session.get(show_url).json()
            
            # On extrait le titre de la série (ex: "Demon Slave")
            # C'est ce titre qui servira de nom au dossier unique
            series_title = show_res["videos"][0]["show"]["title"] if show_res.get("videos") else "ADN Show"

            # 2. Récupérer ensuite la structure par saisons
            url_seasons = self.config["endpoints"].get("seasons")
            if not url_seasons:
                url_seasons = "https://gw.api.animationdigitalnetwork.com/video/show/{show_id}/seasons?maxAgeCategory=18&order=asc"
                
            res = self.session.get(url_seasons.format(show_id=show_id)).json()

            if not res.get("seasons"):
                self.log.error(f"Aucune saison trouvée pour l'ID {show_id}")
                return Series([])

            episodes = []
            for season_data in res["seasons"]:
                s_val = str(season_data.get("season", "1"))
                season_num = int(s_val) if s_val.isdigit() else 1
                
                for vid in season_data.get("videos", []):
                    video_id = str(vid["id"])
                    
                    # Nettoyage du numéro d'épisode (on ne garde que les chiffres)
                    num_match = re.search(r'\d+', str(vid.get("number", "0")))
                    short_number = num_match.group() if num_match else "0"

                    # Logique de sélection (SxxEyy)
                    if not self._parse_select(video_id, short_number, season_num):
                        continue

                    # Création de l'épisode
                    episodes.append(Episode(
                        id_=video_id,
                        service=self.__class__,
                        title=series_title,     # Dossier : "Demon Slave"
                        season=season_num,      # Saison : 2
                        number=int(short_number),
                        name=vid.get("name") or "", # Nom : "La grande réunion..."
                        data=vid
                    ))

            episodes.sort(key=lambda x: (x.season, x.number))
            return Series(episodes)

    def get_tracks(self, title: Episode) -> Tracks:
        """
        Récupère les pistes en pré-extrayant les audios.
        Les audios sont extraits maintenant et seront copiés pendant download().
        """
        self.ensure_authenticated()
        vid_id = title.id

        # Configuration du lecteur
        config_url = self.config["endpoints"]["player_config"].format(video_id=vid_id)
        config_res = self.session.get(config_url).json()

        player_opts = config_res["player"]["options"]
        if not player_opts["user"]["hasAccess"]:
            raise PermissionError("No access to this video (Premium required?)")

        # Token du lecteur
        refresh_url = player_opts["user"].get("refreshTokenUrl") or self.config["endpoints"]["player_refresh"]
        token_res = self.session.post(
            refresh_url,
            headers={"X-Player-Refresh-Token": player_opts["user"]["refreshToken"]}
        ).json()

        player_token = token_res["token"]
        links_url = player_opts["video"].get("url") or self.config["endpoints"]["player_links"].format(video_id=vid_id)

        # Chiffrement RSA
        rand_key = uuid.uuid4().hex[:16]
        payload = json.dumps({"k": rand_key, "t": player_token}).encode('utf-8')

        public_key = serialization.load_pem_public_key(
            self.RSA_PUBLIC_KEY.encode('utf-8'),
            backend=default_backend()
        )

        encrypted = public_key.encrypt(payload, padding.PKCS1v15())
        auth_header_val = base64.b64encode(encrypted).decode('utf-8')

        # Récupération des liens
        links_res = self.session.get(
            links_url,
            params={"freeWithAds": "true", "adaptive": "true", "withMetadata": "true", "source": "Web"},
            headers={"X-Player-Token": auth_header_val}
        ).json()

        tracks = Tracks()
        streaming_links = links_res.get("links", {}).get("streaming", {})

        # Map des langues
        lang_map = {
            "vf": "fr",
            "vostf": "ja",
            "vde": "de",
            "vostde": "ja",
        }

        # Priorité: VOSTF (original) pour la vidéo principale
        priority_order = ["vostf", "vf", "vde", "vostde"]
        available_streams = {k: v for k, v in streaming_links.items() if k in lang_map}
        
        sorted_streams = sorted(
            available_streams.keys(),
            key=lambda x: priority_order.index(x) if x in priority_order else 999
        )

        if not sorted_streams:
            raise ValueError("No supported streams found")

        # Vidéo principale (VOSTF ou premier disponible)
        primary_stream = sorted_streams[0]
        primary_lang = lang_map[primary_stream]
        
        self.log.info(f"Primary video stream: {primary_stream} ({primary_lang})")
        
        video_track = self._get_video_track(
            streaming_links[primary_stream],
            primary_stream,
            primary_lang,
            is_original=(primary_stream in ["vostf", "vostde"])
        )
        
        if video_track:
            tracks.add(video_track)
            self.log.info(f"Video track added: {video_track.width}x{video_track.height}")

        # Extraire audios pour toutes les langues disponibles
        for stream_type in sorted_streams:
            audio_lang = lang_map[stream_type]
            is_original = stream_type in ["vostf", "vostde"]
            
            self.log.info(f"Processing audio for: {stream_type} ({audio_lang})")
            
            audio_track = self._extract_audio_track(
                streaming_links[stream_type],
                stream_type,
                audio_lang,
                is_original,
                title
            )
            
            if audio_track:
                tracks.add(audio_track, warn_only=True)
                self.log.info(f"Audio track added: {audio_lang}")

        # Stocker les données de chapitres pour get_chapters()
        if "video" in links_res:
            title.data["chapter_data"] = links_res["video"]
            self.log.debug(f"Stored chapter data: intro={links_res['video'].get('tcIntroStart')}, ending={links_res['video'].get('tcEndingStart')}")

        # Sous-titres
        self._process_subtitles(links_res, rand_key, title, tracks)

        if not tracks.videos:
            raise ValueError("No video tracks were successfully added")

        return tracks

    def _get_video_track(self, stream_data: dict, stream_type: str, lang: str, is_original: bool):
        """Récupère la piste vidéo principale (sans audio)."""
        try:
            m3u8_url = self._resolve_stream_url(stream_data, stream_type)
            if not m3u8_url:
                return None

            hls_manifest = HLS.from_url(url=m3u8_url, session=self.session)
            hls_tracks = hls_manifest.to_tracks(language=lang)

            if not hls_tracks.videos:
                self.log.warning(f"No video tracks found for {stream_type}")
                return None

            # Meilleure qualité
            best_video = max(
                hls_tracks.videos,
                key=lambda v: (v.height or 0, v.width or 0, v.bitrate or 0)
            )

            # Convertir en VideoNoAudio pour demuxer automatiquement
            video_no_audio = VideoNoAudio(
                id_=best_video.id,
                url=best_video.url,
                codec=best_video.codec,
                language=Language.get(lang),
                is_original_lang=is_original,
                bitrate=best_video.bitrate,
                descriptor=best_video.descriptor,
                width=best_video.width,
                height=best_video.height,
                fps=best_video.fps,
                range_=best_video.range,
                data=best_video.data,
            )
            
            video_no_audio.data["stream_type"] = stream_type
            
            return video_no_audio

        except Exception as e:
            self.log.error(f"Failed to get video track for {stream_type}: {e}")
            return None

    def _extract_audio_track(self, stream_data: dict, stream_type: str, lang: str, is_original: bool, title: Episode):
        """
        Extrait l'audio et retourne un AudioExtracted.
        L'audio est extrait MAINTENANT et sera copié pendant download().
        """
        if not binaries.FFMPEG:
            self.log.warning("FFmpeg not found, cannot extract audio")
            return None

        try:
            m3u8_url = self._resolve_stream_url(stream_data, stream_type)
            if not m3u8_url:
                return None

            # Créer un répertoire temp pour ADN dans le temp d'Unshackle
            adn_temp = config.directories.temp / "adn_audio_extracts"
            adn_temp.mkdir(parents=True, exist_ok=True)
            
            # Nom de fichier unique basé sur video_id + langue
            audio_filename = f"audio_{title.id}_{stream_type}.m4a"
            audio_path = adn_temp / audio_filename

            # Si déjÃ  extrait, réutiliser
            if audio_path.exists() and audio_path.stat().st_size > 1000:
                self.log.debug(f"Reusing existing extracted audio: {audio_path}")
            else:

                # Extraire avec FFmpeg
                result = subprocess.run(
                    [
                        binaries.FFMPEG,
                        '-i', m3u8_url,
                        '-vn',
                        '-acodec', 'copy',
                        '-y',
                        str(audio_path)
                    ],
                    capture_output=True,
                    text=True,
                    timeout=300
                )

                if result.returncode != 0:
                    self.log.error(f"FFmpeg failed for {stream_type}: {result.stderr}")
                    audio_path.unlink(missing_ok=True)
                    return None

                if not audio_path.exists() or audio_path.stat().st_size < 1000:
                    self.log.error(f"Extracted audio is invalid for {stream_type}")
                    audio_path.unlink(missing_ok=True)
                    return None

            # Créer AudioExtracted avec le fichier pré-extrait
            audio_track = AudioExtracted(
                id_=f"audio-{stream_type}-{lang}",
                extracted_path=audio_path,
                codec=Audio.Codec.AAC,
                language=Language.get(lang),
                is_original_lang=is_original,
                bitrate=128000,
                channels=2.0,
            )
            
            return audio_track

        except subprocess.TimeoutExpired:
            self.log.error(f"FFmpeg timeout for {stream_type}")
            return None
        except Exception as e:
            self.log.error(f"Failed to extract audio for {stream_type}: {e}")
            return None

    def _resolve_stream_url(self, stream_data: dict, stream_type: str) -> Optional[str]:
        """Résout l'URL du stream."""
        preferred_keys = ["fhd", "hd", "auto", "sd", "mobile"]

        m3u8_url = None
        for key in preferred_keys:
            if key in stream_data and stream_data[key]:
                m3u8_url = stream_data[key]
                break

        if not m3u8_url:
            return None

        try:
            resp = self.session.get(m3u8_url, timeout=12)
            if resp.status_code != 200:
                return None

            content_type = resp.headers.get("Content-Type", "")
            resp_text = resp.text.strip()

            if "application/json" in content_type or resp_text.startswith("{"):
                try:
                    json_data = resp.json()
                    real_location = json_data.get("location")
                    if real_location:
                        return real_location
                except json.JSONDecodeError:
                    pass

            return m3u8_url

        except Exception as e:
            self.log.error(f"Failed to resolve URL for {stream_type}: {e}")
            return None

    def _process_subtitles(self, links_res: dict, rand_key: str, title: Episode, tracks: Tracks):
        """Traite les sous-titres."""
        subs_root = links_res.get("links", {}).get("subtitles", {})
        if "all" not in subs_root:
            self.log.debug("No subtitles available")
            return

        aes_key_bytes = bytes.fromhex(rand_key + '7fac1178830cfe0c')

        try:
            sub_loc_res = self.session.get(subs_root["all"]).json()
            encrypted_sub_res = self.session.get(sub_loc_res["location"]).text

            self.log.debug(f"Encrypted subtitle length: {len(encrypted_sub_res)}")

            iv_b64 = encrypted_sub_res[:24]
            payload_b64 = encrypted_sub_res[24:]

            iv = base64.b64decode(iv_b64)
            ciphertext = base64.b64decode(payload_b64)

            self.log.debug(f"IV length: {len(iv)}, Ciphertext length: {len(ciphertext)}")

            cipher = Cipher(algorithms.AES(aes_key_bytes), modes.CBC(iv), backend=default_backend())
            decryptor = cipher.decryptor()
            decrypted_padded = decryptor.update(ciphertext) + decryptor.finalize()

            # TOUJOURS retirer le padding PKCS7 (Python ne le fait pas automatiquement)
            pad_len = decrypted_padded[-1]
            if not (1 <= pad_len <= 16):
                self.log.error(f"Invalid PKCS7 padding length: {pad_len}")
                return
            
            # Vérifier que tous les bytes de padding ont la même valeur
            padding = decrypted_padded[-pad_len:]
            if not all(b == pad_len for b in padding):
                self.log.error(f"Invalid PKCS7 padding bytes")
                return
            
            decrypted_json = decrypted_padded[:-pad_len].decode('utf-8')
            self.log.debug(f"Decrypted JSON length: {len(decrypted_json)}")

            
            subs_data = json.loads(decrypted_json)

            
            if not isinstance(subs_data, dict):
                self.log.error(f"subs_data is not a dict! Type: {type(subs_data)}")
                return
            
            if len(subs_data) == 0:
                self.log.warning("subs_data is empty!")
                return
            
            # Debug chaque clé
            for key in subs_data.keys():
                value = subs_data[key]
                if isinstance(value, list) and len(value) > 0:
                    self.log.debug(f"    First item type: {type(value[0])}")
                    self.log.debug(f"    First item keys: {value[0].keys() if isinstance(value[0], dict) else 'NOT A DICT'}")
                    self.log.debug(f"    First item sample: {str(value[0])[:200]}")
            processed_langs = set()
            
            for sub_lang_key, cues in subs_data.items():
                
                if not isinstance(cues, list):
                    self.log.warning(f"Cues for {sub_lang_key} is not a list! Type: {type(cues)}")
                    continue
                
                if len(cues) == 0:
                    self.log.debug(f"No subtitles for {sub_lang_key} (normal for dubbed versions)")
                    continue
                
                self.log.debug(f"  Cues count: {len(cues)}")
                self.log.debug(f"  First cue: {cues[0]}")
                
                if "vf" in sub_lang_key.lower() or "vostf" in sub_lang_key.lower():
                    target_lang = "fr"
                elif "vde" in sub_lang_key.lower() or "vostde" in sub_lang_key.lower():
                    target_lang = "de"
                else:
                    self.log.debug(f"Skipping subtitle language: {sub_lang_key}")
                    continue

                if target_lang in processed_langs:
                    self.log.debug(f"Already processed {target_lang}, skipping")
                    continue
                
                processed_langs.add(target_lang)

                # Convertir en ASS
                ass_content = self._json_to_ass(cues, title.title, title.number)
                
                # Vérifier si le fichier ASS a du contenu
                event_count = ass_content.count("Dialogue:")
                self.log.debug(f"Generated ASS with {event_count} dialogue events")
                
                if event_count == 0:
                    self.log.warning(f"ASS file has no dialogue events!")
                    self.log.warning(f"First cue was: {cues[0] if cues else 'EMPTY LIST'}")
                
                # Créer SubtitleEmbedded avec le contenu ASS directement
                subtitle = SubtitleEmbedded(
                    id_=f"sub-{target_lang}-{sub_lang_key}",
                    embedded_content=ass_content,  # Contenu ASS directement
                    codec=Subtitle.Codec.SubStationAlphav4,
                    language=Language.get(target_lang),
                    forced=False,
                    sdh=False,
                )
                
                tracks.add(subtitle, warn_only=True)
                self.log.info(f"Subtitle added: {target_lang} ({event_count} events)")

        except json.JSONDecodeError as e:
            self.log.error(f"Failed to decode JSON: {e}")
            self.log.error(f"Decrypted data (first 500 chars): {decrypted_json[:500] if 'decrypted_json' in locals() else 'NOT DECRYPTED'}")
        except Exception as e:
            self.log.error(f"Failed to process subtitles: {e}")
            import traceback
            self.log.debug(traceback.format_exc())

    def get_chapters(self, title: Episode) -> Chapters:
        """
        Crée les chapitres à partir des timecodes ADN.
        - Si tcIntroStart existe:
            - Si tcIntroStart != "00:00:00": ajouter "Prologue" à 00:00:00
            - Ajouter "Opening" à tcIntroStart
            - Ajouter "Episode" à tcIntroEnd
        - Sinon: ajouter "Episode" à 00:00:00
        - Si tcEndingStart existe:
            - Ajouter "Ending Start" à tcEndingStart
            - Ajouter "Ending End" à tcEndingEnd
        """
        chapters = Chapters()
        
        # Récupérer les données de chapitres stockées dans get_tracks()
        chapter_data = title.data.get("chapter_data", {})
        if not chapter_data:
            self.log.debug("No chapter data available")
            return chapters
        
        tc_intro_start = chapter_data.get("tcIntroStart")
        tc_intro_end = chapter_data.get("tcIntroEnd")
        tc_ending_start = chapter_data.get("tcEndingStart")
        tc_ending_end = chapter_data.get("tcEndingEnd")
        
        self.log.debug(f"Chapter timecodes: intro={tc_intro_start}->{tc_intro_end}, ending={tc_ending_start}->{tc_ending_end}")
        
        try:
            if tc_intro_start:
                # Si l'intro ne commence pas à 00:00:00, ajouter un prologue
                if tc_intro_start != "00:00:00":
                    chapters.add(Chapter(
                        timestamp=0,
                        name="Prologue"
                    ))
                    self.log.debug("Added Prologue chapter at 00:00:00")
                
                # Opening
                chapters.add(Chapter(
                    timestamp=self._timecode_to_ms(tc_intro_start),
                    name="Opening"
                ))
                self.log.debug(f"Added Opening chapter at {tc_intro_start}")
                
                # Episode (après l'intro)
                if tc_intro_end:
                    chapters.add(Chapter(
                        timestamp=self._timecode_to_ms(tc_intro_end),
                        name="Episode"
                    ))
                    self.log.debug(f"Added Episode chapter at {tc_intro_end}")
            else:
                # Pas d'intro, épisode commence à 00:00:00
                chapters.add(Chapter(
                    timestamp=0,
                    name="Episode"
                ))
                self.log.debug("Added Episode chapter at 00:00:00 (no intro)")
            
            # Ending
            if tc_ending_start:
                chapters.add(Chapter(
                    timestamp=self._timecode_to_ms(tc_ending_start),
                    name="Ending Start"
                ))
                self.log.debug(f"Added Ending Start chapter at {tc_ending_start}")
                
                if tc_ending_end:
                    chapters.add(Chapter(
                        timestamp=self._timecode_to_ms(tc_ending_end),
                        name="Ending End"
                    ))
                    self.log.debug(f"Added Ending End chapter at {tc_ending_end}")
            
            self.log.info(f"✓ Created {len(chapters)} chapters")
            
        except Exception as e:
            self.log.error(f"Failed to create chapters: {e}")
            import traceback
            self.log.debug(traceback.format_exc())
        
        return chapters

    def search(self) -> Generator[SearchResult, None, None]:
        res = self.session.get(
            self.config["endpoints"]["search"],
            params={"search": self.title, "limit": 20, "offset": 0}
        ).json()

        for show in res.get("shows", []):
            yield SearchResult(
                id_=str(show["id"]),
                title=show["title"],
                label=show["type"],
                description=show.get("summary", "")[:300],
                url=f"https://animationdigitalnetwork.com/video/{show['id']}",
                image=show.get("image")
            )

    def parse_show_id(self, input_str: str) -> str:
        if input_str.isdigit():
            return input_str
        match = re.match(self.TITLE_RE, input_str)
        if match:
            return match.group("id")
        raise ValueError(f"Invalid ADN Show ID/URL: {input_str}")

    def _json_to_ass(self, cues: List[dict], title: str, ep_num: Union[int, str]) -> str:
        """Convertit les sous-titres JSON en ASS."""
        header = """[Script Info]
ScriptType: v4.00+
WrapStyle: 0
PlayResX: 1280
PlayResY: 720
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,50,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,1.95,0,2,0,0,70,0

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
        events = []
        pos_align_map = {"start": 1, "end": 3}
        line_align_map = {"middle": 8, "end": 4}

        def format_time(seconds: float) -> str:
            """Format exact d'adn : HH:MM:SS.CC (centisecondes sur 2 chiffres)"""
            secs = int(seconds)
            centiseconds = round((seconds - secs) * 100)
            
            hours = secs // 3600
            minutes = (secs % 3600) // 60
            remaining_seconds = secs % 60
            
            # Padding sur 2 chiffres pour TOUT (hours inclus)
            return f"{hours:02d}:{minutes:02d}:{remaining_seconds:02d}.{centiseconds:02d}"

        for cue in cues:
            start_time = cue.get("startTime", 0)
            end_time = cue.get("endTime", 0)
            text = cue.get("text", "")
            
            # Skip si texte vide
            if not text or not text.strip():
                continue

            # Nettoyage EXACT du code adn
            text = text.replace(' \\N', '\\N')  # remove space before \\N at end
            if text.endswith('\\N'):
                text = text[:-2]  # remove \\N at end
            text = text.replace('\r', '')
            text = text.replace('\n', '\\N')
            text = re.sub(r'\\N +', r'\\N', text)  # \\N followed by spaces
            text = re.sub(r' +\\N', r'\\N', text)  # spaces followed by \\N
            text = re.sub(r'(\\N)+', r'\\N', text)  # multiple \\N
            text = re.sub(r'<b[^>]*>([^<]*)</b>', r'{\\b1}\1{\\b0}', text)
            text = re.sub(r'<i[^>]*>([^<]*)</i>', r'{\\i1}\1{\\i0}', text)
            text = re.sub(r'<u[^>]*>([^<]*)</u>', r'{\\u1}\1{\\u0}', text)
            text = text.replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&')
            text = re.sub(r'<[^>]>', '', text)  # remove any remaining single tags
            if text.endswith('\\N'):
                text = text[:-2]
            text = text.rstrip()  # remove trailing spaces
            
            # Skip après nettoyage si vide
            if not text.strip():
                continue

            p_align = pos_align_map.get(cue.get("positionAlign"), 2)
            l_align = line_align_map.get(cue.get("lineAlign", ""), 0)
            align_val = p_align + l_align

            start = format_time(start_time)
            end = format_time(end_time)
            
            style_mod = f"{{\\a{align_val}}}" if align_val != 2 else ""
            events.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{style_mod}{text}")

        self.log.debug(f"Converted {len(events)} subtitle events from {len(cues)} cues")
        
        if not events:
            self.log.warning(f"No subtitle events generated - all cues were empty or invalid (total cues: {len(cues)})")
        
        return header + "\n".join(events)