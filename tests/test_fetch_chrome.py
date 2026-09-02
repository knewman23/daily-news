"""The Chrome backend: mapping Instagram's web payload onto fetch.py's contract.

Every fixture here mirrors a response captured live on 2026-09-02 from
`PolarisProfilePostsQuery` (root field
`xdt_api__v1__feed__user_timeline_graphql_connection`) while the instaloader
backend was blocked. The shapes -- `code`, `taken_at`, `media_type`,
`caption.text`, `video_versions`, `image_versions2.candidates`,
`carousel_media`, `page_info` -- are the verified ones, not invented.
"""

from datetime import datetime, timezone

import pytest

from src import fetch, fetch_chrome
from src.config import FetchConfig


CFG = FetchConfig(session_user="krys.newman", backend="chrome")

# media_type in the live payload: 1 image, 2 video, 8 carousel.
TAKEN_AT = 1788368640          # 2026-09-02 17:04:00 UTC


def image_node(code="AAA", taken_at=TAKEN_AT, caption="a caption"):
    return {
        "pk": "1",
        "code": code,
        "taken_at": taken_at,
        "media_type": 1,
        "caption": {"text": caption} if caption is not None else None,
        "image_versions2": {"candidates": [
            {"url": f"https://cdn.test/{code}_full.jpg", "width": 1206, "height": 1608},
            {"url": f"https://cdn.test/{code}_small.jpg", "width": 320, "height": 427},
        ]},
        "video_versions": None,
        "carousel_media": None,
    }


def video_node(code="VVV", taken_at=TAKEN_AT):
    node = image_node(code, taken_at)
    node["media_type"] = 2
    node["video_versions"] = [
        {"url": f"https://cdn.test/{code}_720.mp4", "width": 720, "height": 1280},
        {"url": f"https://cdn.test/{code}_480.mp4", "width": 480, "height": 854},
    ]
    return node


def carousel_node(code="CCC", taken_at=TAKEN_AT, slide_types=(1, 1, 2)):
    node = image_node(code, taken_at)
    node["media_type"] = 8
    node["carousel_media"] = [
        {
            "pk": f"{code}-{i}",
            "media_type": kind,
            "image_versions2": {"candidates": [
                {"url": f"https://cdn.test/{code}_slide{i}.jpg",
                 "width": 1206, "height": 1605},
            ]},
            "video_versions": (
                [{"url": f"https://cdn.test/{code}_slide{i}.mp4"}] if kind == 2 else None
            ),
        }
        for i, kind in enumerate(slide_types, start=1)
    ]
    return node


def page(nodes, has_next=False, cursor=None):
    """The full wire payload, wrapper included -- what the browser hands back."""
    return {
        "data": {
            fetch_chrome.POSTS_ROOT_FIELD: {
                "edges": [{"node": n} for n in nodes],
                "page_info": {"has_next_page": has_next, "end_cursor": cursor},
            }
        }
    }


class FakeBrowser:
    """Stands in for the CDP-driven browser. Returns one page per call."""

    def __init__(self, pages, blob=b"bytes"):
        self.pages = list(pages)
        self.blob = blob
        self.requests = []
        self.fetched = []

    def profile_posts(self, handle, cursor=None):
        self.requests.append((handle, cursor))
        return self.pages[len(self.requests) - 1]

    def get_bytes(self, url):
        self.fetched.append(url)
        return self.blob


def client(pages, **kw):
    return fetch_chrome.ChromeClient(CFG, browser=FakeBrowser(pages, **kw))


# --- the attribute surface fetch.py depends on -----------------------------


def test_a_post_exposes_everything_the_walk_reads():
    """fetch.fetch_handle touches exactly these; a missing one breaks the walk."""
    post = next(iter(client([page([image_node()])]).posts_for("carolinegleich")))

    for attr in ("shortcode", "date_utc", "is_video", "caption",
                 "video_url", "url", "typename"):
        assert hasattr(post, attr), attr
    assert callable(post.get_sidecar_nodes)


def test_an_image_post_maps_onto_the_instaloader_shape():
    post = next(iter(client([page([image_node("AAA")])]).posts_for("h")))

    assert post.shortcode == "AAA"
    assert post.is_video is False
    assert post.caption == "a caption"
    assert post.url == "https://cdn.test/AAA_full.jpg"   # widest candidate, first
    assert post.typename == "GraphImage"


def test_a_video_post_takes_the_first_and_largest_rendition():
    post = next(iter(client([page([video_node("VVV")])]).posts_for("h")))

    assert post.is_video is True
    assert post.typename == "GraphVideo"
    assert post.video_url == "https://cdn.test/VVV_720.mp4"


def test_a_carousel_reports_itself_as_a_sidecar():
    """fetch.slide_urls branches on this exact string."""
    post = next(iter(client([page([carousel_node("CCC")])]).posts_for("h")))

    assert post.typename == "GraphSidecar"
    assert len(post.get_sidecar_nodes()) == 3


def test_timestamps_come_back_as_aware_utc():
    post = next(iter(client([page([image_node(taken_at=TAKEN_AT)])]).posts_for("h")))

    assert post.date_utc == datetime(2026, 9, 2, 17, 4, tzinfo=timezone.utc)
    # fetch._utc accepts either, but an aware value cannot be misread as local.
    assert fetch._utc(post.date_utc) == post.date_utc


def test_a_post_with_no_caption_is_empty_rather_than_a_crash():
    """Reels and image posts routinely carry `caption: null`."""
    post = next(iter(client([page([image_node(caption=None)])]).posts_for("h")))

    assert post.caption == ""


# --- slide_urls parity with the instaloader backend ------------------------


def test_slide_urls_skips_video_slides_inside_a_carousel():
    """Matches InstaloaderClient: a video slide's thumbnail is not its content."""
    c = client([page([carousel_node("CCC", slide_types=(1, 2, 1))])])
    post = next(iter(c.posts_for("h")))

    assert c.slide_urls(post) == [
        "https://cdn.test/CCC_slide1.jpg",
        "https://cdn.test/CCC_slide3.jpg",
    ]


def test_slide_urls_of_a_single_image_is_just_its_own_url():
    c = client([page([image_node("AAA")])])
    post = next(iter(c.posts_for("h")))

    assert c.slide_urls(post) == ["https://cdn.test/AAA_full.jpg"]


# --- pagination is lazy ----------------------------------------------------


def test_only_one_page_is_requested_when_the_walk_stops_early():
    """The saving over a profile walk: a handle with nothing new costs one load.

    fetch_handle abandons the iterator once it has seen enough consecutive old
    posts, so a second page must not be fetched speculatively.
    """
    browser = FakeBrowser([page([image_node("A")], has_next=True, cursor="c1"),
                           page([image_node("B")])])
    c = fetch_chrome.ChromeClient(CFG, browser=browser)

    first = next(iter(c.posts_for("h")))

    assert first.shortcode == "A"
    assert browser.requests == [("h", None)]


def test_the_next_page_is_requested_with_the_cursor_when_iteration_continues():
    browser = FakeBrowser([page([image_node("A")], has_next=True, cursor="c1"),
                           page([image_node("B")])])
    c = fetch_chrome.ChromeClient(CFG, browser=browser)

    codes = [p.shortcode for p in c.posts_for("h")]

    assert codes == ["A", "B"]
    assert browser.requests == [("h", None), ("h", "c1")]


def test_paging_stops_when_a_cursor_is_promised_but_missing():
    """has_next_page without an end_cursor would otherwise loop forever."""
    browser = FakeBrowser([page([image_node("A")], has_next=True, cursor=None)])
    c = fetch_chrome.ChromeClient(CFG, browser=browser)

    assert [p.shortcode for p in c.posts_for("h")] == ["A"]
    assert len(browser.requests) == 1


# --- downloads -------------------------------------------------------------


def test_downloading_a_video_writes_the_bytes_the_browser_returned(tmp_path):
    c = client([page([video_node("VVV")])], blob=b"mp4-data")
    post = next(iter(c.posts_for("h")))
    dest = tmp_path / "nested" / "VVV.mp4"

    c.download(post, dest)

    assert dest.read_bytes() == b"mp4-data"


def test_downloading_an_image_writes_the_bytes_the_browser_returned(tmp_path):
    c = client([page([image_node()])], blob=b"jpg-data")
    dest = tmp_path / "nested" / "AAA.jpg"

    c.download_image("https://cdn.test/AAA_full.jpg", dest)

    assert dest.read_bytes() == b"jpg-data"


# --- failure paths ---------------------------------------------------------


def test_a_logged_out_browser_aborts_the_whole_run():
    """Verified live: logged out, Instagram serves the login interstitial and
    the posts connection is simply absent. No handle can succeed, so this has
    to abort rather than be recorded as one dead handle."""
    browser = FakeBrowser([{}])
    c = fetch_chrome.ChromeClient(CFG, browser=browser)

    with pytest.raises(fetch_chrome.ChromeUnavailable):
        list(c.posts_for("h"))


def test_being_logged_out_travels_the_existing_abort_path():
    """fetch_day only re-raises SessionExpired and ActionBlocked; anything else
    is swallowed as a per-handle failure and the run limps on pointlessly."""
    assert issubclass(fetch_chrome.ChromeUnavailable, fetch.SessionExpired)


def test_an_action_block_seen_through_the_browser_is_still_an_action_block():
    """If the browser path ever gets blocked too, it must not be retried."""
    browser = FakeBrowser([{"errors": [{"message": "feedback_required"}]}])
    c = fetch_chrome.ChromeClient(CFG, browser=browser)

    with pytest.raises(fetch.ActionBlocked):
        list(c.posts_for("h"))


# --- the timeout diagnostic ------------------------------------------------


def test_the_timeout_report_names_the_queries_that_did_arrive():
    """Two handles timed out on 2026-09-02 while a trace showed they issue the
    query fine. Without knowing what the page asked for instead, the next
    guess is as blind as the last one."""
    detail = fetch_chrome._timeout_detail(
        ["PolarisProfilePageContentQuery", "PolarisStoriesV3TrayContainerQuery",
         "PolarisProfilePageContentQuery"]
    )

    assert "PolarisProfilePageContentQuery" in detail
    assert "PolarisStoriesV3TrayContainerQuery" in detail
    assert detail.count("PolarisProfilePageContentQuery") == 1   # de-duplicated


def test_the_timeout_report_says_so_when_no_graphql_arrived_at_all():
    """A page that issued nothing is a different problem from one that issued
    everything except the posts query."""
    assert "no graphql" in fetch_chrome._timeout_detail([]).lower()


# --- the query name is not stable ------------------------------------------


def test_both_known_posts_query_names_are_recognised():
    """Observed live on 2026-09-02, same browser, same session, minutes apart:

        carolinegleich  ->  PolarisProfilePostsQuery
        aaronparnas     ->  PolarisProfilePostsTabContentQuery_connection

    Matching only the first name made two handles time out for 60s x 3 while
    the posts they wanted were on the wire the whole time. The route variant
    is not something we control, so accept either.
    """
    for name in ("PolarisProfilePostsQuery",
                 "PolarisProfilePostsTabContentQuery_connection"):
        assert fetch_chrome.is_posts_query(name), name


def test_an_unrelated_profile_query_is_not_mistaken_for_the_posts_query():
    """These arrive on the same endpoint during the same page load."""
    for name in ("PolarisProfilePageContentQuery",
                 "PolarisStoriesV3TrayContainerQuery",
                 "PolarisAPIGetFrCookieQuery",
                 "PolarisProfileNoteBubbleQuery",
                 ""):
        assert not fetch_chrome.is_posts_query(name), name

