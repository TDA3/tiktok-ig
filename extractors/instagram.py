"""
Instagram extractor — ported from Cobalt's instagram.js

Extraction chain (tries in order):
1. Mobile API via oembed → media_id → /api/v1/media/{id}/info/
2. Embed page parsing (/p/{id}/embed/captioned/)
3. GraphQL API (PolarisPostActionLoadPostQueryQuery)

All methods work without login for public posts.
"""

import re
import json
import http.cookiejar
from typing import Optional
from urllib.parse import quote

from .base import BaseExtractor, VideoResult, GENERIC_USER_AGENT

MOBILE_HEADERS = {
    "x-ig-app-locale": "en_US",
    "x-ig-device-locale": "en_US",
    "x-ig-mapped-locale": "en_US",
    "user-agent": (
        "Instagram 275.0.0.27.98 Android (33/13; 280dpi; 720x1423; "
        "Xiaomi; Redmi 7; onclite; qcom; en_US; 458229237)"
    ),
    "accept-language": "en-US",
    "x-fb-http-engine": "Liger",
    "x-fb-client-ip": "True",
    "x-fb-server-cluster": "True",
    "content-length": "0",
}

COMMON_HEADERS = {
    "user-agent": GENERIC_USER_AGENT,
    "sec-gpc": "1",
    "sec-fetch-site": "same-origin",
    "x-ig-app-id": "936619743392459",
}

EMBED_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Cache-Control": "max-age=0",
    "Dnt": "1",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "User-Agent": GENERIC_USER_AGENT,
}

# URL patterns
POST_PATTERN = re.compile(
    r'instagram\.com/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)'
)
STORY_PATTERN = re.compile(
    r'instagram\.com/stories/([^/]+)/(\d+)'
)
SHARE_PATTERN = re.compile(
    r'instagram\.com/share/([A-Za-z0-9_-]+)'
)


class InstagramExtractor(BaseExtractor):

    def _extract_post_id(self, url: str) -> Optional[str]:
        m = POST_PATTERN.search(url)
        return m.group(1) if m else None

    def _extract_share_id(self, url: str) -> Optional[str]:
        m = SHARE_PATTERN.search(url)
        return m.group(1) if m else None

    # ------ Mobile API ------

    async def _get_media_id(self, post_id: str) -> Optional[str]:
        """Get numeric media_id from oembed endpoint."""
        oembed_url = f"https://i.instagram.com/api/v1/oembed/?url=https://www.instagram.com/p/{post_id}/"
        data = await self.fetch_json(oembed_url, headers=MOBILE_HEADERS)
        return data.get("media_id") if data else None

    async def _request_mobile_api(self, media_id: str) -> Optional[dict]:
        """Fetch media info from Instagram mobile API."""
        url = f"https://i.instagram.com/api/v1/media/{media_id}/info/"
        data = await self.fetch_json(url, headers=MOBILE_HEADERS)
        if data and data.get("items"):
            return data["items"][0]
        return None

    # ------ Embed parsing ------

    async def _request_embed(self, post_id: str) -> Optional[dict]:
        """Parse the embed page for video/image URLs."""
        url = f"https://www.instagram.com/p/{post_id}/embed/captioned/"
        html = await self.fetch(url, headers=EMBED_HEADERS)
        if not html:
            return None
        try:
            # Extract the init data JSON
            match = re.search(r'"init",\[\],\[(.*?)\]\],', html)
            if not match:
                return None
            embed_data = json.loads(match.group(1))
            if embed_data and embed_data.get("contextJSON"):
                return json.loads(embed_data["contextJSON"])
        except Exception as e:
            self.logger.debug(f"Embed parse error: {e}")
        return None

    # ------ GraphQL API ------

    async def _request_gql(self, post_id: str) -> Optional[dict]:
        """Query Instagram's GraphQL API for post data."""
        try:
            # First fetch the post page to get tokens
            page_url = f"https://www.instagram.com/p/{post_id}/"
            html = await self.fetch(page_url, headers=EMBED_HEADERS)
            if not html:
                return None

            # Extract LSD token
            lsd_match = re.search(r'"LSD",\[\],({.*?}),\d+\]', html)
            lsd = "unknown"
            if lsd_match:
                try:
                    lsd = json.loads(lsd_match.group(1)).get("token", "unknown")
                except Exception:
                    pass

            # Extract CSRF
            csrf_match = re.search(r'"csrf_token":"([^"]+)"', html)
            csrf = csrf_match.group(1) if csrf_match else ""

            headers = {
                **EMBED_HEADERS,
                "x-ig-app-id": "936619743392459",
                "X-FB-LSD": lsd,
                "X-CSRFToken": csrf,
                "content-type": "application/x-www-form-urlencoded",
                "X-FB-Friendly-Name": "PolarisPostActionLoadPostQueryQuery",
            }

            body = {
                "__a": "1",
                "__d": "www",
                "lsd": lsd,
                "fb_api_caller_class": "RelayModern",
                "fb_api_req_friendly_name": "PolarisPostActionLoadPostQueryQuery",
                "variables": json.dumps({
                    "shortcode": post_id,
                    "fetch_tagged_user_count": None,
                    "hoisted_comment_id": None,
                    "hoisted_reply_id": None,
                }),
                "server_timestamps": "true",
                "doc_id": "8845758582119845",
            }

            data = await self.fetch_json(
                "https://www.instagram.com/graphql/query",
                headers=headers,
                method="POST",
                data=body,
            )
            if data and data.get("data"):
                return {"gql_data": data["data"]}
        except Exception as e:
            self.logger.debug(f"GQL error: {e}")
        return None

    # ------ Result extraction ------

    def _extract_from_mobile_api(self, data: dict, post_id: str) -> Optional[VideoResult]:
        """Extract video/photo from mobile API response (new format)."""
        # Carousel
        carousel = data.get("carousel_media")
        if carousel:
            # Return first video from carousel
            for item in carousel:
                if item.get("video_versions"):
                    best = max(item["video_versions"], key=lambda v: v.get("width", 0) * v.get("height", 0))
                    return VideoResult(
                        url=best["url"],
                        filename=f"instagram_{post_id}.mp4",
                    )
            # No video in carousel — return first image
            for item in carousel:
                if item.get("image_versions2", {}).get("candidates"):
                    return VideoResult(
                        url=item["image_versions2"]["candidates"][0]["url"],
                        filename=f"instagram_{post_id}.jpg",
                        is_photo=True,
                    )
            return None

        # Single video
        if data.get("video_versions"):
            best = max(data["video_versions"], key=lambda v: v.get("width", 0) * v.get("height", 0))
            return VideoResult(
                url=best["url"],
                filename=f"instagram_{post_id}.mp4",
            )

        # Single image
        candidates = data.get("image_versions2", {}).get("candidates")
        if candidates:
            return VideoResult(
                url=candidates[0]["url"],
                filename=f"instagram_{post_id}.jpg",
                is_photo=True,
            )

        return None

    def _extract_from_gql(self, data: dict, post_id: str) -> Optional[VideoResult]:
        """Extract from GraphQL response (old format)."""
        shortcode_media = (
            data.get("gql_data", {}).get("shortcode_media")
            or data.get("gql_data", {}).get("xdt_shortcode_media")
        )
        if not shortcode_media:
            return None

        # Sidecar (carousel)
        sidecar = shortcode_media.get("edge_sidecar_to_children")
        if sidecar:
            for edge in sidecar.get("edges", []):
                node = edge.get("node", {})
                if node.get("is_video") and node.get("video_url"):
                    return VideoResult(
                        url=node["video_url"],
                        filename=f"instagram_{post_id}.mp4",
                    )
            # Fallback to first image
            for edge in sidecar.get("edges", []):
                node = edge.get("node", {})
                if node.get("display_url"):
                    return VideoResult(
                        url=node["display_url"],
                        filename=f"instagram_{post_id}.jpg",
                        is_photo=True,
                    )
            return None

        # Single video
        if shortcode_media.get("video_url"):
            return VideoResult(
                url=shortcode_media["video_url"],
                filename=f"instagram_{post_id}.mp4",
            )

        # Single image
        if shortcode_media.get("display_url"):
            return VideoResult(
                url=shortcode_media["display_url"],
                filename=f"instagram_{post_id}.jpg",
                is_photo=True,
            )

        return None

    def _extract_from_embed(self, data: dict, post_id: str) -> Optional[VideoResult]:
        """Extract from embed page data."""
        if not data:
            return None

        # Look for video_url in the context data
        video_url = data.get("video_url")
        if video_url:
            return VideoResult(
                url=video_url,
                filename=f"instagram_{post_id}.mp4",
            )

        # Look for display_url (image)
        display_url = data.get("display_url")
        if display_url:
            return VideoResult(
                url=display_url,
                filename=f"instagram_{post_id}.jpg",
                is_photo=True,
            )

        return None

    # ------ Main extract ------

    async def extract(self, url: str) -> Optional[VideoResult]:
        # Handle share links — resolve redirect first
        share_id = self._extract_share_id(url)
        if share_id:
            resolved = await self.resolve_redirect(
                f"https://www.instagram.com/share/{share_id}/",
                headers={"User-Agent": "curl/7.88.1"},
            )
            if resolved:
                url = resolved

        post_id = self._extract_post_id(url)
        if not post_id:
            self.logger.debug(f"Could not extract post ID from {url}")
            return None

        # Strategy 1: Mobile API
        try:
            media_id = await self._get_media_id(post_id)
            if media_id:
                data = await self._request_mobile_api(media_id)
                if data:
                    result = self._extract_from_mobile_api(data, post_id)
                    if result:
                        self.logger.info(f"Instagram: extracted via mobile API")
                        return result
        except Exception as e:
            self.logger.debug(f"Mobile API failed: {e}")

        # Strategy 2: Embed page
        try:
            embed_data = await self._request_embed(post_id)
            if embed_data:
                result = self._extract_from_embed(embed_data, post_id)
                if result:
                    self.logger.info(f"Instagram: extracted via embed")
                    return result
        except Exception as e:
            self.logger.debug(f"Embed failed: {e}")

        # Strategy 3: GraphQL
        try:
            gql_data = await self._request_gql(post_id)
            if gql_data:
                result = self._extract_from_gql(gql_data, post_id)
                if result:
                    self.logger.info(f"Instagram: extracted via GQL")
                    return result
        except Exception as e:
            self.logger.debug(f"GQL failed: {e}")

        self.logger.warning(f"Instagram: all methods failed for {post_id}")
        return None

    # ------ Profile scraping ------

    def _load_instagram_cookies(self) -> str:
        """
        Load Instagram cookies from cookies.txt (Netscape format) and return
        a Cookie header string.  Returns empty string when cookies are unavailable.
        """
        from config import COOKIES_FILE, COOKIES_ENABLED
        if not COOKIES_ENABLED:
            return ""
        try:
            jar = http.cookiejar.MozillaCookieJar()
            jar.load(COOKIES_FILE, ignore_discard=True, ignore_expires=True)
            cookies = {
                c.name: c.value
                for c in jar
                if c.domain == "instagram.com" or c.domain.endswith(".instagram.com")
            }
            return "; ".join(f"{k}={v}" for k, v in cookies.items())
        except Exception as e:
            self.logger.debug(f"Failed to load Instagram cookies: {e}")
            return ""

    async def extract_profile(self, username: str) -> list:
        """
        Scrape all post URLs from an Instagram profile.

        Strategy:
        1. Fetch the profile page to obtain the numeric user_id, LSD token,
           and CSRF token.
        2. Fall back to the mobile web_profile_info API if HTML parsing fails.
        3. Paginate through posts using the Instagram GraphQL endpoint with the
           ``edge_owner_to_timeline_media`` query.

        Uses cookies from cookies.txt when available for private/authenticated
        content.

        Returns list of dicts: [{"url": "...", "shortcode": "..."}, ...]
        """
        from config import MAX_PROFILE_DOWNLOADS

        cookie_header = self._load_instagram_cookies()

        page_headers = dict(EMBED_HEADERS)
        if cookie_header:
            page_headers["Cookie"] = cookie_header

        profile_url = f"https://www.instagram.com/{quote(username)}/"

        # Step 1: Fetch profile page to get user_id and GQL tokens
        html = await self.fetch(profile_url, headers=page_headers)

        user_id = None
        lsd = "unknown"
        csrf = ""

        if html:
            # Try several patterns that Instagram embeds in page scripts
            for pattern in [
                r'"owner":\{"id":"(\d+)"',
                r'"userId":"(\d+)"',
                r'"id":"(\d+)","username":"' + re.escape(username),
                r'"profileId":"(\d+)"',
                r'"user_id":"(\d+)"',
            ]:
                m = re.search(pattern, html)
                if m:
                    user_id = m.group(1)
                    break

            lsd_match = re.search(r'"LSD",\[\],(\{.*?\}),\d+\]', html)
            if lsd_match:
                try:
                    lsd = json.loads(lsd_match.group(1)).get("token", "unknown")
                except Exception:
                    pass

            csrf_match = re.search(r'"csrf_token":"([^"]+)"', html)
            if csrf_match:
                csrf = csrf_match.group(1)

        # Step 2: Fall back to mobile web_profile_info API for user_id
        if not user_id:
            mobile_headers = dict(MOBILE_HEADERS)
            if cookie_header:
                mobile_headers["Cookie"] = cookie_header
            api_url = (
                f"https://i.instagram.com/api/v1/users/web_profile_info/"
                f"?username={quote(username)}"
            )
            data = await self.fetch_json(api_url, headers=mobile_headers)
            if data:
                user_id = (
                    data.get("data", {})
                    .get("user", {})
                    .get("id")
                )

        if not user_id:
            self.logger.warning(f"Instagram profile: could not get user_id for @{username}")
            return []

        # Step 3: Paginate through posts via GraphQL
        posts = []
        after_cursor = None

        gql_headers = {
            **EMBED_HEADERS,
            "x-ig-app-id": "936619743392459",
            "X-FB-LSD": lsd,
            "X-CSRFToken": csrf,
            "content-type": "application/x-www-form-urlencoded",
            "X-FB-Friendly-Name": "PolarisProfilePostsQuery",
        }
        if cookie_header:
            gql_headers["Cookie"] = cookie_header

        while len(posts) < MAX_PROFILE_DOWNLOADS:
            variables: dict = {"id": user_id, "first": 12}
            if after_cursor:
                variables["after"] = after_cursor

            body = {
                "__a": "1",
                "__d": "www",
                "lsd": lsd,
                "fb_api_caller_class": "RelayModern",
                "fb_api_req_friendly_name": "PolarisProfilePostsQuery",
                "variables": json.dumps(variables),
                "server_timestamps": "true",
                "doc_id": "17888483320059182",
            }

            try:
                data = await self.fetch_json(
                    "https://www.instagram.com/graphql/query",
                    headers=gql_headers,
                    method="POST",
                    data=body,
                )
            except Exception as e:
                self.logger.debug(f"Instagram profile GQL error: {e}")
                break

            if not data:
                break

            # Navigate the response — structure varies slightly between accounts
            user_node = (
                data.get("data", {}).get("user")
                or data.get("data", {})
            )
            timeline = (
                user_node.get("edge_owner_to_timeline_media")
                or user_node.get("edge_felix_video_timeline")
            )

            if not timeline:
                break

            for edge in timeline.get("edges", []):
                node = edge.get("node", {})
                shortcode = node.get("shortcode")
                if shortcode:
                    posts.append({
                        "url": f"https://www.instagram.com/p/{shortcode}/",
                        "shortcode": shortcode,
                    })

            page_info = timeline.get("page_info", {})
            if not page_info.get("has_next_page"):
                break
            after_cursor = page_info.get("end_cursor")
            if not after_cursor:
                break

        self.logger.info(f"Instagram profile @{username}: found {len(posts)} posts")
        return posts
