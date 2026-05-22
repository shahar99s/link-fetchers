import httpx
import pytest
from httporchestrator import Response

from link_fetchers.fetcher_registry import create_fetcher
from link_fetchers.fetchers.chatgpt_fetcher import ChatGPTFetcher
from link_fetchers.fetchers.dropbox_transfer_fetcher import DropboxTransferFetcher
from link_fetchers.fetchers.filemail_fetcher import FilemailFetcher
from link_fetchers.fetchers.limewire_fetcher import decode_turbo_stream
from link_fetchers.fetchers.mediafire_fetcher import MediaFireFetcher
from link_fetchers.fetchers.sendgb_fetcher import SendgbFetcher
from link_fetchers.fetchers.smash_fetcher import FromSmashFetcher
from link_fetchers.fetchers.terabox_fetcher import TeraBoxFetcher
from link_fetchers.utils import Mode, cookies_from_response, merge_cookie_header


def test_create_fetcher_builds_sendanywhere_tracking_url():
    fetcher = create_fetcher(
        "https://mandrillapp.com/track/click/30564474/sendanywhe.re?p=eyJzIjoieElrVFZ2M05LcUZHMm1PcklCZlZjZXBxLU1FIiwidiI6MiwicCI6IntcInVcIjozMDU2NDQ3NCxcInZcIjoyLFwidXJsXCI6XCJodHRwOlxcXC9cXFwvc2VuZGFueXdoZS5yZVxcXC9LVDJBNVFER1wiLFwiaWRcIjpcImQyMDQ5Y2QxOTc2ZTQyMTM4MDMzNzJlYWQwOWU3MjU1XCIsXCJ1cmxfaWRzXCI6W1wiMWY1NmQ1NmNlMmNiMWRmNjRmOGM2YjZiMTBjMTk2ZmYzYmNkOTMzYVwiXSxcIm1zZ190c1wiOjE3NzUxNjM0MjV9In0",
        mode=Mode.INFO,
    )

    assert fetcher.NAME == "SendAnywhere"


def test_create_fetcher_builds_transfernow_with_sender_secret():
    fetcher = create_fetcher(
        "https://www.transfernow.net/dl/202603120kavmEMg/yBLpPYkJ",
        mode=Mode.INFO,
        sender_secret="sender-secret",
    )

    assert fetcher.NAME == "TransferNow"
    assert any(step.name == "load transfer stats" for step in fetcher.steps)


def test_limewire_turbo_decoder_keeps_literal_list_values():
    decoded = decode_turbo_stream(
        [
            [1, "literal", True, None],
            {"_2": "direct-value"},
            "name",
        ]
    )

    assert decoded == [{"name": "direct-value"}, "literal", True, None]


def test_dropbox_builds_direct_download_link():
    fetcher = DropboxTransferFetcher(
        "https://www.dropbox.com/l/scl/AAEMf-awt4SqTt9TW9i1N27WY1Vm-717f_0",
        mode=Mode.INFO,
    )

    assert (
        fetcher.direct_link
        == "https://www.dropbox.com/l/scl/AAEMf-awt4SqTt9TW9i1N27WY1Vm-717f_0?dl=1"
    )


def test_dropbox_extracts_metadata_from_download_response():
    fetcher = DropboxTransferFetcher(
        "https://www.dropbox.com/t/AbCdEfGhIjKlMnOp", mode=Mode.INFO
    )
    response = Response(
        httpx.Response(
            206,
            headers={
                "Content-Type": "application/pdf",
                "Content-Length": "1234",
                "Content-Disposition": 'attachment; filename="report.pdf"',
            },
            request=httpx.Request(
                "GET", "https://www.dropbox.com/t/AbCdEfGhIjKlMnOp?dl=1"
            ),
        )
    )

    state = fetcher.extract_metadata(response)

    assert state["metadata"]["filename"] == "report.pdf"
    assert state["metadata"]["content_type"] == "application/pdf"
    assert state["metadata"]["size"] == 1234
    assert state["metadata"]["state"] == "available"


def test_dropbox_marks_preview_only_for_html_page():
    fetcher = DropboxTransferFetcher(
        "https://www.dropbox.com/t/AbCdEfGhIjKlMnOp", mode=Mode.INFO
    )
    response = Response(
        httpx.Response(
            200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            request=httpx.Request(
                "GET",
                "https://www.dropbox.com/scl/fi/demo123/report.pdf?dl=0&r=token&sm=1",
            ),
        )
    )

    state = fetcher.extract_metadata(response)

    assert state["metadata"]["filename"] == "report.pdf"
    assert state["metadata"]["state"] == "preview_only"
    assert state["metadata"]["download_url"] is None


def test_dropbox_marks_invite_style_links_as_recipient_gated():
    fetcher = DropboxTransferFetcher(
        "https://www.dropbox.com/l/scl/AAEMf-awt4SqTt9TW9i1N27WY1Vm-717f_0",
        mode=Mode.INFO,
    )
    response = Response(
        httpx.Response(
            200,
            text="<html>invite-only</html>",
            request=httpx.Request(
                "GET",
                "https://www.dropbox.com/scl/fi/demo123/report.pdf?dl=0&r=token&sm=1",
            ),
        )
    )

    state = fetcher.capture_shared_link_context(response)

    assert state["probe_required"] is False
    assert state["available"] is False
    assert state["metadata"]["state"] == "recipient_gated"
    assert state["metadata"]["provider_state"] == "shared_link_access_denied"


def test_smash_only_blocks_download_notifications():
    fetcher = FromSmashFetcher("https://fromsmash.com/abcDEF123", mode=Mode.INFO)
    response = Response(
        httpx.Response(
            200,
            json={
                "transfer": {
                    "title": "demo",
                    "filesNumber": 1,
                    "download": "https://download.example/file",
                    "size": 123,
                    "notification": {"email": {"enabled": True}},
                }
            },
            request=httpx.Request("GET", "https://example.com"),
        )
    )

    state = fetcher.extract_transfer_state(
        response, {"target": "public-id", "region": "eu"}
    )

    assert state["transfer_metadata"]["has_download_notification"] is False
    assert state["transfer_metadata"]["notification_safe"] is True


def test_sendgb_builds_better_fallback_filename():
    fetcher = SendgbFetcher("https://sendgb.com/g4D2eAoOamH", mode=Mode.INFO)

    assert fetcher.build_fallback_filename(
        {"filename": "Quarterly Report.pdf", "file": "opaque-token"}
    ) == ("Quarterly Report.pdf")
    assert (
        fetcher.build_fallback_filename({"file": "opaque-token"})
        == "sendgb-g4D2eAoOamH.bin"
    )


def test_sendgb_marks_expired_transfer_as_unavailable():
    fetcher = SendgbFetcher("https://sendgb.com/g4D2eAoOamH", mode=Mode.INFO)
    response = Response(
        httpx.Response(
            200,
            text=(
                "<html><body>"
                "<div>There are currently no files available for download. "
                "This transfer has expired and has been permanently removed from our servers."
                "</div>"
                "</body></html>"
            ),
            request=httpx.Request(
                "GET", "https://www.sendgb.com/upload/?utm_source=g4D2eAoOamH"
            ),
        )
    )

    state = fetcher.extract_page_state(response)

    assert state["available"] is False
    assert state["is_deleted"] is True
    assert state["private_id"] is None
    assert state["secret_code"] is None
    assert state["fallback_filename"] == "sendgb-g4D2eAoOamH.bin"


def test_filemail_download_step_checks_for_expired_file():
    fetcher = FilemailFetcher(
        "https://www.filemail.com/d/ifyvssdfbjbnzni", mode=Mode.FETCH
    )

    download_step = fetcher.build_fetch_steps()[0].step

    assert (
        download_step.assertions[0].fn(
            None,
            {
                "metadata": {"is_expired": True},
                "downloads_count": 1,
                "direct_link": "https://example.com/download",
                "filename": "demo.txt",
            },
        )
        is False
    )
    assert download_step.assertions[0].message == "Error: Filemail file expired"


def test_mediafire_extracts_copy_direct_link_from_list_payload():
    fetcher = MediaFireFetcher(
        "https://www.mediafire.com/file/5rv03j13foves42/demo/file",
        mode=Mode.FETCH,
        email="user@example.com",
        password="secret",
    )
    response = Response(
        httpx.Response(
            200,
            json={
                "response": {
                    "links": [
                        {
                            "direct_download": "https://download1481.mediafire.com/demo/report.pdf",
                        }
                    ]
                }
            },
            request=httpx.Request(
                "POST", "https://www.mediafire.com/api/1.5/file/get_links.php"
            ),
        )
    )

    assert (
        fetcher.extract_copy_direct_link(response)
        == "https://download1481.mediafire.com/demo/report.pdf"
    )


def test_mediafire_builds_login_signature():
    fetcher = MediaFireFetcher(
        "https://www.mediafire.com/file/5rv03j13foves42/demo/file",
        mode=Mode.FETCH,
        email="user@example.com",
        password="secret",
        app_id="42511",
    )

    assert fetcher.build_login_signature() == "37e995232c842e407d6555a42f6baaccec75c8ba"


def test_mediafire_builds_authenticated_api_body():
    fetcher = MediaFireFetcher(
        "https://www.mediafire.com/file/5rv03j13foves42/demo/file",
        mode=Mode.FETCH,
        email="user@example.com",
        password="secret",
        app_id="42511",
    )

    body = fetcher.build_authenticated_api_body(
        "/api/1.5/file/copy.php",
        {
            "session_token": "session-token",
            "session_time": "1775819183.2458",
            "session_secret_key": "633664053",
        },
        quick_key="5rv03j13foves42",
        folder_key="myfiles",
    )

    assert body == (
        "folder_key=myfiles&quick_key=5rv03j13foves42&response_format=json&session_token=session-token"
        "&signature=aa3ca0ef8332114bdc8d33a5110f3662"
    )


def test_mediafire_extracts_copy_direct_link_from_nested_payload():
    fetcher = MediaFireFetcher(
        "https://www.mediafire.com/file/5rv03j13foves42/demo/file",
        mode=Mode.FETCH,
        email="user@example.com",
        password="secret",
    )
    response = Response(
        httpx.Response(
            200,
            json={
                "response": {
                    "links": {
                        "item": {
                            "downloads": {
                                "normal_download": "//download1481.mediafire.com/demo/report.pdf",
                            }
                        }
                    }
                }
            },
            request=httpx.Request(
                "POST", "https://www.mediafire.com/api/1.5/file/get_links.php"
            ),
        )
    )

    assert (
        fetcher.extract_copy_direct_link(response)
        == "https://download1481.mediafire.com/demo/report.pdf"
    )


def test_mediafire_extracts_copy_quick_key_from_new_quickkeys():
    fetcher = MediaFireFetcher(
        "https://www.mediafire.com/file/5rv03j13foves42/demo/file",
        mode=Mode.FETCH,
        email="user@example.com",
        password="secret",
    )
    response = Response(
        httpx.Response(
            200,
            json={"response": {"new_quickkeys": ["3xfqnizexp1kxvq"], "new_key": "yes"}},
            request=httpx.Request(
                "POST", "https://www.mediafire.com/api/1.5/file/copy.php"
            ),
        )
    )

    assert fetcher.extract_copy_quick_key(response) == "3xfqnizexp1kxvq"


def test_mediafire_regenerates_secret_key_when_api_requests_new_key():
    fetcher = MediaFireFetcher(
        "https://www.mediafire.com/file/5rv03j13foves42/demo/file",
        mode=Mode.FETCH,
        email="user@example.com",
        password="secret",
    )
    response = Response(
        httpx.Response(
            200,
            json={"response": {"new_key": "yes"}},
            request=httpx.Request(
                "POST", "https://www.mediafire.com/api/1.5/file/copy.php"
            ),
        )
    )

    state = fetcher.update_authenticated_session_state(
        response, {"session_secret_key": "1131879819"}
    )

    assert state == {"session_secret_key": "1093972807"}


def test_create_fetcher_returns_real_terabox_fetcher_instance():
    fetcher = create_fetcher(
        "https://1024terabox.com/s/1LJTcFCQ5haHb838XjlghcA",
        mode=Mode.INFO,
    )

    assert isinstance(fetcher, TeraBoxFetcher)
    prepare_hook = fetcher.steps[0].before_hooks[0]
    assert prepare_hook({}) == {"auth_cookie": ""}


def test_terabox_uses_cookie_header_when_provided():
    fetcher = create_fetcher(
        "https://1024terabox.com/s/1LJTcFCQ5haHb838XjlghcA",
        mode=Mode.INFO,
        headers={"Cookie": "session=abc123; ndus=cookie-value"},
    )

    prepare_hook = fetcher.steps[0].before_hooks[0]

    assert prepare_hook({}) == {"auth_cookie": "session=abc123; ndus=cookie-value"}


def test_terabox_captures_and_merges_response_cookies():
    response = Response(
        httpx.Response(
            200,
            headers=[
                ("Set-Cookie", "lang=en; Path=/"),
                ("Set-Cookie", "shareUpdateRandom=69; Path=/; HttpOnly"),
            ],
            request=httpx.Request(
                "GET",
                "https://www.1024tera.com/sharing/link?surl=LJTcFCQ5haHb838XjlghcA",
            ),
        )
    )

    cookies = cookies_from_response(response)
    merged = merge_cookie_header("", cookies)

    assert "lang=en" in merged
    assert "shareUpdateRandom=69" in merged


def test_terabox_extracts_js_token_from_share_page():
    fetcher = TeraBoxFetcher(
        "https://1024terabox.com/s/1LJTcFCQ5haHb838XjlghcA", mode=Mode.INFO
    )
    response = Response(
        httpx.Response(
            200,
            text='<html><script>window.jsToken = "ABC123TOKEN";</script></html>',
            request=httpx.Request(
                "GET",
                "https://www.1024tera.com/sharing/link?surl=LJTcFCQ5haHb838XjlghcA",
            ),
        )
    )

    assert fetcher.extract_js_token(response.text) == "ABC123TOKEN"


def test_terabox_extracts_metadata_and_sharedownload_state_from_shorturlinfo():
    fetcher = TeraBoxFetcher(
        "https://1024terabox.com/s/1LJTcFCQ5haHb838XjlghcA", mode=Mode.INFO
    )
    response = Response(
        httpx.Response(
            200,
            json={
                "errno": 0,
                "country": "IL",
                "ctime": 1774375470,
                "expiredtype": 0,
                "fcount": 1,
                "head_url": "https://example.com/avatar.png",
                "list": [
                    {
                        "category": "3",
                        "fs_id": "626495027274991",
                        "isdir": "0",
                        "md5": "a83986a40a1204945d237644c7dc5a9c",
                        "server_ctime": "1774375461",
                        "server_filename": "report.png",
                        "size": "3181849",
                        "thumbs": {"icon": "https://example.com/icon.png"},
                    }
                ],
                "randsk": "s4FUDKrogUOBR%2BHRNQ7ZkKA9XCGOgwaZaVh%2FmNsfQSE%3D",
                "share_username": "demo-user",
                "shareid": 14526303475,
                "sign": "eb2c43f7898e4bda0dd94f30e3c9049d",
                "timestamp": 1775685905,
                "uk": 4400508661606,
                "uk_str": "4400508661606",
            },
            request=httpx.Request("GET", "https://www.1024tera.com/api/shorturlinfo"),
        )
    )

    state = fetcher.extract_metadata_state(
        response,
        {"shorturl": "1LJTcFCQ5haHb838XjlghcA", "surl": "LJTcFCQ5haHb838XjlghcA"},
    )

    assert state["available"] is True
    assert state["filename"] == "report.png"
    assert state["metadata"]["size_bytes"] == 3181849
    assert state["download_request_state"]["share_id"] == 14526303475
    assert state["download_request_state"]["fid_list"] == "[626495027274991]"
    assert state["download_request_state"]["extra"] == (
        '{"sekey":"s4FUDKrogUOBR+HRNQ7ZkKA9XCGOgwaZaVh/mNsfQSE="}'
    )


def test_terabox_marks_native_sharedownload_as_ready():
    fetcher = TeraBoxFetcher(
        "https://1024terabox.com/s/1LJTcFCQ5haHb838XjlghcA", mode=Mode.FETCH
    )
    response = Response(
        httpx.Response(
            200,
            json={
                "errno": 0,
                "server_time": 1775685905,
                "list": [
                    {"dlink": "https://d.1024tera.com/file/demo.png?dstime=1775685905"}
                ],
            },
            request=httpx.Request("GET", "https://www.1024tera.com/api/sharedownload"),
        )
    )

    state = fetcher.extract_download_state(
        response,
        {
            "shorturl": "1LJTcFCQ5haHb838XjlghcA",
            "filename": "report.png",
            "metadata": {"filename": "report.png"},
        },
        source="legacy",
    )

    assert state["download_status"]["can_download"] is True
    assert state["download_status"]["reason"] == "legacy_ready"
    assert state["metadata"]["download_state"] == "legacy_ready"


def test_terabox_marks_native_sharedownload_as_expired():
    fetcher = TeraBoxFetcher(
        "https://1024terabox.com/s/1LJTcFCQ5haHb838XjlghcA", mode=Mode.FETCH
    )
    response = Response(
        httpx.Response(
            200,
            json={
                "errno": 0,
                "server_time": 1775685905,
                "list": [
                    {"dlink": "https://d.1024tera.com/file/demo.png?dstime=1775682304"}
                ],
            },
            request=httpx.Request("GET", "https://www.1024tera.com/api/sharedownload"),
        )
    )

    state = fetcher.extract_download_state(
        response,
        {
            "shorturl": "1LJTcFCQ5haHb838XjlghcA",
            "filename": "report.png",
            "metadata": {"filename": "report.png"},
        },
        source="legacy",
    )

    assert state["download_status"]["can_download"] is False
    assert state["download_status"]["reason"] == "legacy_ready_but_stale"
    assert state["metadata"]["download_state"] == "legacy_ready_but_stale"


def test_terabox_uses_browser_preview_for_image_when_dlink_requires_verify():
    fetcher = TeraBoxFetcher(
        "https://1024terabox.com/s/1LJTcFCQ5haHb838XjlghcA", mode=Mode.FETCH
    )
    response = Response(
        httpx.Response(
            200,
            json={"errno": 400310, "errmsg": "need verify_v2", "request_id": 123},
            request=httpx.Request("POST", "https://www.1024tera.com/share/download"),
        )
    )

    state = fetcher.extract_download_state(
        response,
        {
            "shorturl": "1LJTcFCQ5haHb838XjlghcA",
            "filename": "report.jpg",
            "metadata": {
                "filename": "report.jpg",
                "category": "3",
                "preview_download_url": "https://data.1024tera.com/thumbnail/demo?size=c850_u580",
            },
        },
        source="guarded",
    )

    assert state["download_status"]["can_download"] is True
    assert state["download_status"]["reason"] == "guarded_browser_preview"
    assert state["download_status"]["browser_preview_fallback"] is True
    assert state["direct_link"].endswith("size=c850_u580")
    assert state["metadata"]["download_is_preview"] is True


def test_terabox_reports_anonymous_download_block_when_verify_and_legacy_are_gated():
    fetcher = TeraBoxFetcher(
        "https://1024terabox.com/s/1LJTcFCQ5haHb838XjlghcA", mode=Mode.FETCH
    )
    vars = {
        "filename": "installer.exe",
        "download_status": {"can_download": False, "reason": "legacy_ready_but_stale"},
        "metadata": {
            "filename": "installer.exe",
            "download_attempts": {
                "guarded": {
                    "errno": 400310,
                    "errmsg": "need verify_v2",
                    "reason": "guarded_errno_400310",
                }
            },
        },
    }

    with pytest.raises(ValueError, match="requires verify_v2"):
        fetcher.ensure_download_link_is_usable(vars)
    assert (
        fetcher.classify_provider_download_state(
            source="legacy",
            errno=0,
            reason="legacy_ready_but_stale",
            can_download=False,
            using_preview_fallback=False,
            attempts=vars["metadata"]["download_attempts"],
        )
        == "anonymous_download_blocked"
    )


def test_create_fetcher_builds_chatgpt_fetcher():
    fetcher = create_fetcher(
        "https://chat.openai.com/share/abc123",
        mode=Mode.INFO,
    )

    assert fetcher.NAME == "ChatGPT"


def test_create_fetcher_builds_chatgpt_share_alias_fetcher():
    fetcher = create_fetcher(
        "https://chatgpt.com/c/abc123",
        mode=Mode.INFO,
    )

    assert fetcher.NAME == "ChatGPT"
    assert fetcher.link == "https://chatgpt.com/c/abc123"
    assert fetcher.is_share_url is True
    assert fetcher.account_mode is False


def test_chatgpt_conversation_url_skips_share_api_step():
    fetcher = ChatGPTFetcher("https://chatgpt.com/c/abc123", mode=Mode.INFO)

    assert [step.name for step in fetcher.steps] == [
        "load auth session",
        "load conversation data",
        "load chat page",
    ]


def test_chatgpt_public_share_url_uses_share_api_step():
    fetcher = ChatGPTFetcher("https://chatgpt.com/share/abc123", mode=Mode.INFO)

    assert [step.name for step in fetcher.steps] == [
        "load share data",
        "load chat page",
    ]


def test_create_fetcher_includes_cookie_header():
    fetcher = create_fetcher(
        "https://chat.openai.com/share/abc123",
        mode=Mode.INFO,
        cookies={"session": "abc123"},
    )

    assert fetcher.headers["Cookie"] == "session=abc123"


def test_create_fetcher_merges_cookie_header_with_string():
    fetcher = create_fetcher(
        "https://chat.openai.com/share/abc123",
        mode=Mode.INFO,
        cookies="session=abc123; theme=dark",
    )

    assert "session=abc123" in fetcher.headers["Cookie"]
    assert "theme=dark" in fetcher.headers["Cookie"]


def test_chatgpt_extracts_conversation_payload_from_share_html():
    fetcher = ChatGPTFetcher("https://chat.openai.com/share/abc123", mode=Mode.INFO)
    html = (
        '<script id="__NEXT_DATA__" type="application/json">'
        '{"props":{"pageProps":{"share":{"title":"Demo Chat","conversation":'
        '{"title":"Demo Chat","messages":[{"author":{"role":"user","name":"Alice"},'
        '"content":["Hello"]},{"author":{"role":"assistant","name":"ChatGPT"},'
        '"content":["Hi there"]}]}}}}}</script>'
    )

    state = fetcher.parse_share_page(html)

    assert state["conversation_payload"]["title"] == "Demo Chat"
    assert len(state["conversation_payload"]["messages"]) == 2
    assert state["filename"] == "Demo-Chat.md"


def test_chatgpt_parses_share_api_response():
    fetcher = ChatGPTFetcher("https://chat.openai.com/c/abc123", mode=Mode.INFO)
    response = Response(
        httpx.Response(
            200,
            json={
                "conversation": {
                    "title": "Demo Chat",
                    "messages": [
                        {
                            "author": {"role": "user", "name": "Alice"},
                            "content": ["Hello"],
                        },
                        {
                            "author": {"role": "assistant", "name": "ChatGPT"},
                            "content": ["Hi there"],
                        },
                    ],
                }
            },
            request=httpx.Request(
                "GET", "https://chat.openai.com/backend-api/share/abc123"
            ),
        )
    )

    state = fetcher.parse_share_api_response(response)

    assert state["conversation_payload"]["title"] == "Demo Chat"
    assert len(state["conversation_payload"]["messages"]) == 2
    assert state["filename"] == "Demo-Chat.md"


def test_chatgpt_parses_conversation_mapping_response():
    fetcher = ChatGPTFetcher("https://chatgpt.com/c/abc123", mode=Mode.INFO)
    response = Response(
        httpx.Response(
            200,
            json={
                "title": "Mapped Chat",
                "current_node": "node-2",
                "mapping": {
                    "root": {
                        "id": "root",
                        "message": None,
                        "parent": None,
                        "children": ["node-1"],
                    },
                    "node-1": {
                        "id": "node-1",
                        "parent": "root",
                        "children": ["node-2"],
                        "message": {
                            "author": {"role": "user", "name": "Alice"},
                            "content": {"parts": ["Hello"]},
                        },
                    },
                    "node-2": {
                        "id": "node-2",
                        "parent": "node-1",
                        "children": [],
                        "message": {
                            "author": {"role": "assistant", "name": "ChatGPT"},
                            "content": {"parts": ["Hi there"]},
                        },
                    },
                },
            },
            request=httpx.Request(
                "GET", "https://chatgpt.com/backend-api/conversation/abc123"
            ),
        )
    )

    state = fetcher.parse_conversation_api_response(response)

    assert state["conversation_payload"]["title"] == "Mapped Chat"
    assert [
        message["author"]["role"]
        for message in state["conversation_payload"]["messages"]
    ] == [
        "user",
        "assistant",
    ]
    assert state["filename"] == "Mapped-Chat.md"


def test_chatgpt_key_search_handles_cyclic_page_data():
    fetcher = ChatGPTFetcher("https://chatgpt.com/c/abc123", mode=Mode.INFO)
    page_data = {"title": "Cycle"}
    page_data["self"] = page_data

    assert fetcher._find_key(page_data, "missing") is None
    assert fetcher._find_key(page_data, "title") == "Cycle"


def test_create_fetcher_builds_chatgpt_account_fetcher():
    fetcher = create_fetcher(
        "https://chat.openai.com",
        mode=Mode.FETCH,
        email="alice@example.com",
        password="secret",
    )

    assert fetcher.NAME == "ChatGPT"
    assert fetcher.has_credentials is True
    assert fetcher.account_mode is True
    assert any(step.name == "login" for step in fetcher.steps)
    assert any(step.name == "list conversations" for step in fetcher.steps)


def test_create_fetcher_builds_chatgpt_home_fetcher_without_credentials():
    fetcher = create_fetcher(
        "https://chatgpt.com",
        mode=Mode.INFO,
        cookies={"session": "abc123"},
    )

    assert fetcher.NAME == "ChatGPT"
    assert fetcher.link == "https://chatgpt.com"
    assert fetcher.account_mode is True
    assert fetcher.has_credentials is False
    assert [step.name for step in fetcher.steps] == [
        "load auth session",
        "list conversations",
    ]


def test_chatgpt_account_builds_login_payload():
    fetcher = ChatGPTFetcher(
        "https://chat.openai.com",
        mode=Mode.FETCH,
        email="alice@example.com",
        password="secret",
    )

    assert fetcher.build_login_payload() == {
        "email": "alice@example.com",
        "password": "secret",
    }


def test_chatgpt_account_parses_conversations_response():
    fetcher = ChatGPTFetcher(
        "https://chat.openai.com",
        mode=Mode.INFO,
        email="alice@example.com",
        password="secret",
    )
    response = Response(
        httpx.Response(
            200,
            json={
                "items": [
                    {"id": "1", "title": "First Conversation", "create_time": 123},
                    {"id": "2", "title": "Second Conversation", "create_time": 456},
                ]
            },
            request=httpx.Request(
                "GET", "https://chat.openai.com/backend-api/conversations?limit=200"
            ),
        )
    )

    conversations = fetcher.parse_conversations(response)

    assert len(conversations) == 2
    assert conversations[0]["title"] == "First Conversation"


def test_chatgpt_account_saves_conversation_filenames_with_ids():
    fetcher = ChatGPTFetcher("https://chatgpt.com", mode=Mode.FETCH)

    filename = fetcher.build_account_conversation_filename(
        {"title": "Repeated Title"},
        "abc1234567890",
    )

    assert filename == "Repeated-Title-bc1234567890.md"


def test_chatgpt_account_fetch_writes_each_conversation(tmp_path):
    fetcher = ChatGPTFetcher("https://chatgpt.com", mode=Mode.FETCH, save_path=tmp_path)
    response = Response(
        httpx.Response(
            200,
            json={
                "title": "Fetched Chat",
                "mapping": {
                    "root": {"message": None, "parent": None},
                    "node-1": {
                        "parent": "root",
                        "message": {
                            "author": {"role": "user", "name": "Alice"},
                            "content": {"parts": ["Hello"]},
                        },
                    },
                },
            },
            request=httpx.Request(
                "GET", "https://chatgpt.com/backend-api/conversation/abc123"
            ),
        )
    )
    vars = {
        "conversations": [{"id": "abc123", "title": "Fallback"}],
        "conversation_fetch_index": 0,
        "conversation_files": [],
        "conversation_fetch_failures": [],
    }

    parsed = fetcher.parse_account_conversation_response(response, vars)
    vars.update(parsed)
    state = fetcher.save_account_conversation_response(vars)

    saved_path = tmp_path / "Fetched-Chat-abc123.md"
    assert state["conversation_files"] == [str(saved_path)]
    assert state["conversation_fetch_failures"] == []
    assert "Hello" in saved_path.read_text(encoding="utf-8")


def test_chatgpt_writes_conversation_file(tmp_path):
    fetcher = ChatGPTFetcher(
        "https://chat.openai.com/share/abc123",
        mode=Mode.FETCH,
        save_path=tmp_path,
    )
    conversation_payload = {
        "title": "Demo Chat",
        "messages": [
            {"author": {"role": "user", "name": "Alice"}, "content": ["Hello"]}
        ],
    }

    result = fetcher.write_conversation_file(conversation_payload, "Demo-Chat.md")
    saved_text = (tmp_path / "Demo-Chat.md").read_text(encoding="utf-8")

    assert (tmp_path / "Demo-Chat.md").exists()
    assert result["local_file_path"].endswith("Demo-Chat.md")
    assert "Alice" in saved_text
    assert "Hello" in saved_text


# ── GoFile tests ─────────────────────────────────────────────────────────────

from link_fetchers.fetchers.gofile_fetcher import GoFileFetcher


def test_gofile_extracts_content_id_from_url():
    fetcher = GoFileFetcher("https://gofile.io/d/SXwLDt", mode=Mode.INFO)

    assert fetcher.content_id == "SXwLDt"


def test_gofile_content_url_without_password():
    fetcher = GoFileFetcher("https://gofile.io/d/SXwLDt", mode=Mode.INFO)

    assert (
        fetcher._content_url()
        == "/contents/SXwLDt?cache=true&sortField=createTime&sortDirection=1"
    )


def test_gofile_content_url_with_password():
    import hashlib

    fetcher = GoFileFetcher(
        "https://gofile.io/d/SXwLDt", mode=Mode.INFO, password="secret"
    )
    expected_hash = hashlib.sha256(b"secret").hexdigest()

    assert (
        fetcher._content_url()
        == f"/contents/SXwLDt?cache=true&sortField=createTime&sortDirection=1&password={expected_hash}"
    )


def test_gofile_extracts_primary_file_from_children():
    fetcher = GoFileFetcher("https://gofile.io/d/SXwLDt", mode=Mode.INFO)
    response = Response(
        httpx.Response(
            200,
            json={
                "status": "ok",
                "data": {
                    "id": "SXwLDt",
                    "type": "folder",
                    "name": "My Folder",
                    "children": {
                        "abc123": {
                            "id": "abc123",
                            "type": "file",
                            "name": "report.pdf",
                            "mimetype": "application/pdf",
                            "size": 123456,
                            "link": "https://cdn1.gofile.io/download/abc123/report.pdf",
                            "createTime": 1716000000,
                        }
                    },
                },
            },
            request=httpx.Request("GET", "https://api.gofile.io/contents/SXwLDt"),
        )
    )

    state = fetcher._extract_content_state(response)

    assert state["available"] is True
    assert state["filename"] == "report.pdf"
    assert state["direct_link"] == "https://cdn1.gofile.io/download/abc123/report.pdf"
    assert state["metadata"]["size"] == 123456
    assert state["metadata"]["file_count"] == 1


def test_gofile_unavailable_when_no_children():
    fetcher = GoFileFetcher("https://gofile.io/d/SXwLDt", mode=Mode.INFO)
    response = Response(
        httpx.Response(
            200,
            json={
                "status": "ok",
                "data": {"id": "SXwLDt", "type": "folder", "children": {}},
            },
            request=httpx.Request("GET", "https://api.gofile.io/contents/SXwLDt"),
        )
    )

    state = fetcher._extract_content_state(response)

    assert state["available"] is False


# ── Wormhole tests ───────────────────────────────────────────────────────────

from link_fetchers.fetchers.wormhole_fetcher import (
    WormholeFetcher,
    _bencode_decode,
    _extract_torrent_fields,
)


def test_wormhole_parses_room_id_and_key_from_url():
    fetcher = WormholeFetcher(
        "https://wormhole.app/W00DZv#gDZGGoKuKsWb4uqdBfgBsA",
        mode=Mode.INFO,
    )

    assert fetcher.room_id == "W00DZv"
    assert len(fetcher.main_key) == 16


def test_wormhole_rejects_url_without_fragment():
    with pytest.raises(ValueError, match="Invalid Wormhole URL"):
        WormholeFetcher("https://wormhole.app/W00DZv", mode=Mode.INFO)


def test_wormhole_auth_header_is_deterministic():
    fetcher = WormholeFetcher(
        "https://wormhole.app/W00DZv#gDZGGoKuKsWb4uqdBfgBsA",
        mode=Mode.INFO,
    )
    header1 = fetcher._auth_header("b+sjNKtTVwrLr6JafWSLXw==")
    header2 = fetcher._auth_header("b+sjNKtTVwrLr6JafWSLXw==")

    assert header1.startswith("Bearer sync-v1 ")
    assert header1 == header2


def test_wormhole_bencode_decode_dict():
    data = b"d3:fooi42e3:bar3:baze"
    result, _ = _bencode_decode(data)

    assert result[b"foo"] == 42
    assert result[b"bar"] == b"baz"


def test_wormhole_bencode_decode_list():
    data = b"li1ei2ei3ee"
    result, _ = _bencode_decode(data)

    assert result == [1, 2, 3]


def test_wormhole_extract_torrent_fields_single_file():
    download_url = "https://example.com/dl"
    url_len = len(download_url)
    data = (
        b"d"
        b"4:infod"
        b"4:name8:file.zip"
        b"6:lengthi1000e"
        b"e"
        b"8:url-list" + f"{url_len}:{download_url}".encode() + b"e"
    )
    torrent, _ = _bencode_decode(data)
    fields = _extract_torrent_fields(torrent)

    assert fields["name"] == "file.zip"
    assert fields["size"] == 1000
    assert fields["downloadUrl"] == download_url


# ── Turbobit tests ───────────────────────────────────────────────────────────

from link_fetchers.fetchers.turbobit_fetcher import TurbobitFetcher


def test_turbobit_extracts_file_id_from_trbt_url():
    fetcher = TurbobitFetcher("https://trbt.cc/v8y7cq32fay2.html", mode=Mode.INFO)

    assert fetcher.file_id == "v8y7cq32fay2"


def test_turbobit_extracts_file_id_from_turbobit_url():
    fetcher = TurbobitFetcher("https://turbobit.net/v8y7cq32fay2.html", mode=Mode.INFO)

    assert fetcher.file_id == "v8y7cq32fay2"


def test_turbobit_extract_info_state_parses_json():
    fetcher = TurbobitFetcher("https://turbobit.net/v8y7cq32fay2.html", mode=Mode.INFO)

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "file": {"id": "v8y7cq32fay2", "name": "report.pdf", "size": 512000},
                "premiumOnlyDownload": False,
            }

    result = fetcher._extract_info_state(FakeResponse())

    assert result["available"] is True
    assert result["filename"] == "report.pdf"
    assert result["metadata"]["size"] == 512000
    assert result["metadata"]["premium_only"] is False


def test_turbobit_extract_info_state_handles_404():
    fetcher = TurbobitFetcher("https://turbobit.net/v8y7cq32fay2.html", mode=Mode.INFO)

    class FakeResponse:
        status_code = 404

        def json(self):
            return {}

    result = fetcher._extract_info_state(FakeResponse())

    assert result["available"] is False
    assert result["metadata"]["state"] == "not_found"


def test_turbobit_extract_download_state_parses_download_url():
    fetcher = TurbobitFetcher("https://turbobit.net/v8y7cq32fay2.html", mode=Mode.INFO)

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "downloadUrl": "https://turbobit.net/download/redirect/token/file.jpg"
            }

    result = fetcher._extract_download_state(FakeResponse())

    assert result["download_available"] is True
    assert (
        result["direct_link"] == "https://turbobit.net/download/redirect/token/file.jpg"
    )


# ── Box tests ─────────────────────────────────────────────────────────────────

from link_fetchers.fetchers.box_fetcher import BoxFetcher


def test_box_detects_file_url():
    assert BoxFetcher.is_relevant_url(
        "https://app.box.com/file/2240429889078?box_source=legacy-notify_existing_collab_file"
    )


def test_box_detects_shared_url():
    assert BoxFetcher.is_relevant_url("https://app.box.com/s/abc123xyz")


def test_box_does_not_match_terabox():
    assert not BoxFetcher.is_relevant_url("https://1024terabox.com/s/abc")


def test_box_extracts_file_id():
    fetcher = BoxFetcher(
        "https://app.box.com/file/2240429889078", mode=Mode.INFO, access_token="tok"
    )

    assert fetcher.link_type == "file"
    assert fetcher.file_id == "2240429889078"


def test_box_extracts_shared_code():
    fetcher = BoxFetcher("https://app.box.com/s/abc123xyz", mode=Mode.INFO)

    assert fetcher.link_type == "shared"
    assert fetcher.shared_link_url == "https://app.box.com/s/abc123xyz"


def test_box_extract_state_parses_json():
    fetcher = BoxFetcher(
        "https://app.box.com/file/2240429889078", mode=Mode.INFO, access_token="tok"
    )

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "id": "2240429889078",
                "name": "report.pdf",
                "size": 512000,
                "sha1": "abc123",
                "created_at": "2024-01-01T00:00:00-08:00",
                "modified_at": "2024-06-01T00:00:00-07:00",
            }

    result = fetcher._extract_state(FakeResponse())

    assert result["available"] is True
    assert result["filename"] == "report.pdf"
    assert result["metadata"]["size"] == 512000
    assert result["file_id"] == "2240429889078"
