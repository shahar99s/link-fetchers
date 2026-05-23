# link-fetchers

A Python library for fetching file metadata and downloads from popular file-sharing services. Automatically detects the provider from a URL and routes to the correct fetcher.

Based on `httporchestrator` engine: https://github.com/shahar99s/httporchestrator.py

## Supported Providers

| Provider | Example URL | Notes |
|---|---|---|
| 4Shared | `https://www.4shared.com/s/...` | |
| Box | `https://app.box.com/file/...` | OAuth2 required for private files; see [Box](#box) |
| Box (shared link) | `https://app.box.com/s/...` | No auth required |
| ChatGPT | `https://chatgpt.com/c/...` / `https://chat.openai.com/share/...` | |
| Dropbox Transfer | `https://www.dropbox.com/t/...` | |
| Filemail | `https://www.filemail.com/d/...` | |
| GoFile | `https://gofile.io/d/...` | Password-protected files supported |
| Limewire | `https://limewire.com/d/...` | |
| MediaFire | `https://www.mediafire.com/file/...` | |
| Mega | `https://mega.nz/file/...` | |
| SendAnywhere | `https://send-anywhere.com/web/downloads/...` | |
| SendGB | `https://sendgb.com/...` | |
| FromSmash | `https://fromsmash.com/...` | |
| TeraBox | `https://1024terabox.com/s/...` | |
| TransferNow | `https://www.transfernow.net/dl/...` | |
| TransferXL | `https://transferxl.com/download/...` | |
| Turbobit | `https://turbobit.net/...` / `https://trbt.cc/...` | Free download; captcha gate may apply |
| WeTransfer | `https://wetransfer.com/downloads/...` / `https://we.tl/...` | |
| Wormhole | `https://wormhole.app/...#KEY` | End-to-end encrypted; key must be in URL fragment |

## Installation

```bash
pip install .
```

Dependencies are managed with [Poetry](https://python-poetry.org/):

```bash
poetry install
```

## CLI

```
link-fetchers <url> [options]
```

| Option | Default | Description |
|---|---|---|
| `--mode info\|fetch\|force-fetch` | `fetch` | Operation mode |
| `--save-path DIR` | current dir | Directory to save downloaded files |
| `--log-details` | off | Enable verbose JSON request/response logging |
| `--retry-times N` | `3` | Number of retry attempts for transient errors |
| `--retry-interval SECS` | `2.0` | Seconds to wait between retries |
| `--impersonate TARGET` | | TLS fingerprint to impersonate (e.g. `chrome131`) |
| `--opt KEY=VALUE` | | Provider-specific option, may be repeated |

**Examples:**

```bash
# Fetch metadata only
link-fetchers "https://gofile.io/d/SXwLDt" --mode info

# Download to a specific directory
link-fetchers "https://wetransfer.com/downloads/..." --save-path ./downloads

# Password-protected GoFile link
link-fetchers "https://gofile.io/d/SXwLDt" --opt password=secret

# Box private file with OAuth2 credentials
link-fetchers "https://app.box.com/file/123" --opt client_id=ID --opt client_secret=SECRET
```

## Quick Start (Python API)

```python
from link_fetchers import create_fetcher, Mode

fetcher = create_fetcher(
    "https://www.4shared.com/s/fkEvKTDlWge",
    mode=Mode.FETCH,
)
fetcher.run()
```

## Modes

| Mode | Description |
|---|---|
| `Mode.INFO` | Fetch file metadata only (name, size, type) — no download |
| `Mode.FETCH` | Download the file if a direct link is available |
| `Mode.FORCE_FETCH` | Download regardless of availability signals |

## Options

All fetchers accept these keyword arguments:

| Argument | Type | Default | Description |
|---|---|---|---|
| `mode` | `Mode` | `Mode.FETCH` | Operation mode |
| `headers` | `dict` | | Extra HTTP headers to include in requests |
| `cookies` | `dict \| str` | | Cookies to send (merged into `Cookie` header) |
| `impersonate` | `str \| dict` | | TLS fingerprint to impersonate (e.g. `"chrome124"`) |
| `save_path` | `str \| Path` | cwd | Directory to save downloaded files |
| `log_details` | `bool` | `False` | Enable verbose JSON request/response logging |
| `retry_times` | `int` | `3` | Retry attempts for transient errors (`0` to disable) |
| `retry_interval` | `float` | `2.0` | Seconds between retries |

### Impersonation

Pass a browser string or a detailed dict to bypass TLS-based bot detection:

```python
fetcher = create_fetcher(
    url,
    impersonate={
        "browser": "Chrome",
        "version": "131",
        "os": "Windows",
    },
)
```

Shorthand string form: `impersonate="chrome124"`.

## Examples

### Fetch metadata only

```python
from link_fetchers import create_fetcher, Mode

fetcher = create_fetcher("https://mega.nz/file/cH51DYDR#...", mode=Mode.INFO)
result = fetcher.run()
print(result.summary)
```

### Download a file to a specific directory

```python
from link_fetchers import create_fetcher, Mode
from pathlib import Path

fetcher = create_fetcher(
    "https://wetransfer.com/downloads/TID/SEC123",
    mode=Mode.FETCH,
    save_path=Path("./downloads"),
)
fetcher.run()
```

### MediaFire with authenticated copy

Some providers support optional credentials for unlocking additional access:

```python
fetcher = create_fetcher(
    "https://www.mediafire.com/file/abc123/file",
    mode=Mode.FETCH,
    email="user@example.com",
    password="secret",
)
fetcher.run()
```

## Project Structure

```
link_fetchers/
├── __init__.py              # Public API: BaseFetcher, create_fetcher, Mode
├── base_fetcher.py          # Base class for all fetchers
├── fetcher_registry.py      # Auto-discovery factory
├── utils.py                 # Shared utilities and enums
├── tls_client.py            # curl-cffi TLS impersonation client
└── fetchers/                # Provider implementations
    ├── box_fetcher.py
    ├── chatgpt_fetcher.py
    ├── dropbox_transfer_fetcher.py
    ├── filemail_fetcher.py
    ├── fourshared_fetcher.py
    ├── gofile_fetcher.py
    ├── limewire_fetcher.py
    ├── mediafire_fetcher.py
    ├── mega_fetcher.py
    ├── sendanywhere_fetcher.py
    ├── sendgb_fetcher.py
    ├── smash_fetcher.py
    ├── terabox_fetcher.py
    ├── transfernow_fetcher.py
    ├── transferxl_fetcher.py
    ├── turbobit_fetcher.py
    ├── wetransfer_fetcher.py
    └── wormhole_fetcher.py

tests/
└── link_fetchers/
    ├── test_fetcher_factories.py   # Registry and URL-matching tests
    ├── test_fetchers.py            # Per-provider unit tests
    ├── test_runner_state.py        # BaseFetcher lifecycle tests
    └── test_utils.py               # Utility function tests
```

## Adding a New Provider

1. Create `link_fetchers/fetchers/myprovider_fetcher.py`.
2. Define a class ending with `Fetcher` that extends `BaseFetcher`.
3. Implement `is_relevant_url(url)` as a `@classmethod`.
4. Set `NAME` and `BASE_URL` class attributes.
5. Implement `build_info_steps()` and optionally `build_fetch_steps()` using `httporchestrator` request steps.

The registry discovers and registers it automatically — no other changes needed.

```python
from link_fetchers.base_fetcher import BaseFetcher

class MyProviderFetcher(BaseFetcher):
    NAME = "MyProvider"
    BASE_URL = "https://myprovider.com"

    @classmethod
    def is_relevant_url(cls, url: str) -> bool:
        return "myprovider.com" in url

    def build_info_steps(self):
        ...
```

## Running Tests

```bash
pytest
```

## License

Apache 2.0 — see [LICENSE](LICENSE).
