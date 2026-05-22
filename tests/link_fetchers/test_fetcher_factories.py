import os

import pytest

from link_fetchers.fetcher_registry import create_fetcher, find_relevant_fetcher_class
from link_fetchers.utils import Mode

FETCHER_CASES = [
    ("https://sendgb.com/g4D2eAoOamH", "SendgbFetcher", "SendGB"),
    (
        "https://mega.nz/file/cH51DYDR#qH7QOfRcM-7N9riZWdSjsRq5VDTLfIhThx1capgVA30",
        "MegaFetcher",
        "Mega",
    ),
    ("https://wetransfer.com/downloads/TID/SEC123", "WeTransferFetcher", "WeTransfer"),
    ("https://we.tl/t-mQ7BfOv3WD", "WeTransferFetcher", "WeTransfer"),
    ("https://www.filemail.com/d/ifyvssdfbjbnzni", "FilemailFetcher", "Filemail"),
    ("https://fromsmash.com/abcDEF123", "FromSmashFetcher", "FromSmash"),
    (
        "https://www.dropbox.com/t/AbCdEfGhIjKlMnOp",
        "DropboxTransferFetcher",
        "DropboxTransfer",
    ),
    (
        "https://www.transfernow.net/dl/202603120kavmEMg/yBLpPYkJ",
        "TransferNowFetcher",
        "TransferNow",
    ),
    (
        "https://transferxl.com/download/08abc123def456ghi789jkl012mno345",
        "TransferXLFetcher",
        "TransferXL",
    ),
    (
        "https://send-anywhere.com/web/downloads/KT2A5QDG",
        "SendAnywhereFetcher",
        "SendAnywhere",
    ),
    (
        "https://www.mediafire.com/file/5rv03j13foves42/demo/file",
        "MediaFireFetcher",
        "MediaFire",
    ),
    ("https://1024terabox.com/s/1LJTcFCQ5haHb838XjlghcA", "TeraBoxFetcher", "TeraBox"),
    ("https://limewire.com/d/KJ6Qa#Onk5j8PVz5", "LimewireFetcher", "Limewire"),
    ("https://www.4shared.com/s/fkEvKTDlWge", "FourSharedFetcher", "4shared"),
    ("https://gofile.io/d/SXwLDt", "GoFileFetcher", "GoFile"),
    (
        "https://wormhole.app/W00DZv#gDZGGoKuKsWb4uqdBfgBsA",
        "WormholeFetcher",
        "Wormhole",
    ),
    ("https://trbt.cc/v8y7cq32fay2.html", "TurbobitFetcher", "Turbobit"),
    ("https://turbobit.net/v8y7cq32fay2.html", "TurbobitFetcher", "Turbobit"),
]


@pytest.mark.parametrize(
    ("url", "expected_class_name", "_expected_name"), FETCHER_CASES
)
def test_find_relevant_fetcher_class(url, expected_class_name, _expected_name):
    fetcher_class = find_relevant_fetcher_class(url)

    assert fetcher_class is not None
    assert fetcher_class.__name__ == expected_class_name


def test_create_fetcher_returns_named_fetcher():
    fetcher = create_fetcher(
        "https://mega.nz/file/cH51DYDR#qH7QOfRcM-7N9riZWdSjsRq5VDTLfIhThx1capgVA30",
        mode=Mode.INFO,
    )

    assert fetcher.NAME == "Mega"
    assert fetcher.steps[0].name == "get file metadata"


def test_create_fetcher_forwards_base_save_path(tmp_path):
    fetcher = create_fetcher(
        "https://mega.nz/file/cH51DYDR#qH7QOfRcM-7N9riZWdSjsRq5VDTLfIhThx1capgVA30",
        mode=Mode.INFO,
        save_path=tmp_path,
    )

    assert fetcher.save_path == os.path.abspath(tmp_path)
    assert fetcher.flow.artifact_dir == os.path.abspath(tmp_path)


def test_create_fetcher_raises_for_unknown():
    with pytest.raises(ValueError, match="No supported provider"):
        create_fetcher("https://example.com/unknown")


@pytest.mark.parametrize(
    ("url", "_expected_class_name", "expected_name"), FETCHER_CASES
)
def test_create_fetcher_builds_info_mode_fetchers(
    url, _expected_class_name, expected_name
):
    fetcher = create_fetcher(url, mode=Mode.INFO)

    assert fetcher.NAME == expected_name
    assert fetcher.steps


@pytest.mark.parametrize(
    ("url", "_expected_class_name", "expected_name"), FETCHER_CASES
)
def test_create_fetcher_builds_fetch_mode_fetchers(
    url, _expected_class_name, expected_name
):
    fetcher = create_fetcher(url, mode=Mode.FETCH)

    assert fetcher.NAME == expected_name
    assert fetcher.steps
