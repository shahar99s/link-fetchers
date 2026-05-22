from __future__ import annotations

import pytest

from link_fetchers.utils import (
    Mode,
    format_size,
    format_timestamp,
    merge_cookie_header,
    parse_cookie_header,
    resolve_filename,
    should_download,
    status_is,
    variable_is,
    variable_truthy,
)


class TestFormatSize:
    def test_bytes(self):
        assert format_size(512) == "512 B"

    def test_kilobytes(self):
        assert format_size(1024) == "1.0 KB"

    def test_megabytes(self):
        assert format_size(1024 * 1024) == "1.0 MB"

    def test_gigabytes(self):
        assert format_size(1024**3) == "1.0 GB"

    def test_numeric_string(self):
        assert format_size("2048") == "2.0 KB"

    def test_non_numeric_string_returns_none(self):
        assert format_size("large") is None

    def test_none_returns_none(self):
        assert format_size(None) is None

    def test_list_returns_none(self):
        assert format_size([1024]) is None

    def test_float(self):
        assert format_size(1536.0) == "1.5 KB"


class TestFormatTimestamp:
    def test_seconds_epoch(self):
        result = format_timestamp(0)
        assert result == "1970-01-01 00:00:00 UTC"

    def test_milliseconds_epoch(self):
        result = format_timestamp(1_500_000_000_000)
        assert "2017" in result

    def test_iso_string_passthrough(self):
        assert format_timestamp("2024-01-15") == "2024-01-15"

    def test_none_returns_none(self):
        assert format_timestamp(None) is None

    def test_list_returns_none(self):
        assert format_timestamp([1234]) is None


class TestShouldDownload:
    def test_force_fetch_always_true(self):
        assert should_download(Mode.FORCE_FETCH, None) is True
        assert should_download(Mode.FORCE_FETCH, 0) is True
        assert should_download(Mode.FORCE_FETCH, 5) is True

    def test_info_mode_always_false(self):
        assert should_download(Mode.INFO, 5) is False
        assert should_download(Mode.INFO, None) is False

    def test_fetch_mode_none_count_false(self):
        assert should_download(Mode.FETCH, None) is False

    def test_fetch_mode_zero_count_false(self):
        assert should_download(Mode.FETCH, 0) is False

    def test_fetch_mode_negative_count_false(self):
        assert should_download(Mode.FETCH, -1) is False

    def test_fetch_mode_positive_count_true(self):
        assert should_download(Mode.FETCH, 1) is True
        assert should_download(Mode.FETCH, 99) is True


class TestParseCookieHeader:
    def test_single_cookie(self):
        assert parse_cookie_header("foo=bar") == {"foo": "bar"}

    def test_multiple_cookies(self):
        result = parse_cookie_header("a=1; b=2; c=3")
        assert result == {"a": "1", "b": "2", "c": "3"}

    def test_empty_string(self):
        assert parse_cookie_header("") == {}

    def test_none_string(self):
        assert parse_cookie_header(None) == {}

    def test_value_with_equals(self):
        assert parse_cookie_header("token=abc=def") == {"token": "abc=def"}

    def test_skips_keyless_pairs(self):
        assert parse_cookie_header("noequalssign") == {}

    def test_strips_whitespace(self):
        assert parse_cookie_header("  foo = bar ; baz = qux ") == {
            "foo": "bar",
            "baz": "qux",
        }


class TestMergeCookieHeader:
    def test_two_headers_merged(self):
        result = merge_cookie_header("a=1", "b=2")
        assert "a=1" in result
        assert "b=2" in result

    def test_later_value_wins(self):
        result = merge_cookie_header("a=old", "a=new")
        assert "a=new" in result
        assert "a=old" not in result

    def test_empty_first_header(self):
        assert merge_cookie_header("", "x=1") == "x=1"

    def test_all_empty(self):
        assert merge_cookie_header("", "") == ""

    def test_single_header(self):
        assert merge_cookie_header("k=v") == "k=v"


class TestResolveFilename:
    def test_no_disposition_returns_fallback(self):
        assert resolve_filename({}, "fallback.bin") == "fallback.bin"

    def test_basic_filename(self):
        assert (
            resolve_filename(
                {"Content-Disposition": 'attachment; filename="report.pdf"'}, "f"
            )
            == "report.pdf"
        )

    def test_filename_star_takes_precedence(self):
        headers = {
            "Content-Disposition": "attachment; filename=\"old.pdf\"; filename*=UTF-8''new%20file.pdf"
        }
        assert resolve_filename(headers, "f") == "new file.pdf"

    def test_case_insensitive_header_key(self):
        assert (
            resolve_filename(
                {"content-disposition": 'attachment; filename="x.txt"'}, "f"
            )
            == "x.txt"
        )

    def test_url_encoded_filename(self):
        assert (
            resolve_filename(
                {"Content-Disposition": 'attachment; filename="my%20file.zip"'}, "f"
            )
            == "my file.zip"
        )


class TestPredicates:
    def test_status_is_match(self):
        class R:
            status_code = 200

        assert status_is(200)(R(), {}) is True

    def test_status_is_no_match(self):
        class R:
            status_code = 404

        assert status_is(200)(R(), {}) is False

    def test_variable_is_match(self):
        assert variable_is("state", "available")(None, {"state": "available"}) is True

    def test_variable_is_no_match(self):
        assert variable_is("state", "available")(None, {"state": "deleted"}) is False

    def test_variable_is_missing_key(self):
        assert variable_is("state", "available")(None, {}) is False

    def test_variable_truthy_true(self):
        assert variable_truthy("link")(None, {"link": "https://x.com"}) is True

    def test_variable_truthy_false(self):
        assert variable_truthy("link")(None, {"link": ""}) is False

    def test_variable_truthy_missing(self):
        assert variable_truthy("link")(None, {}) is False
