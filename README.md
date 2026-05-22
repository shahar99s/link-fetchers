# link-fetchers

A Python library for fetching file metadata and downloads from popular file-sharing services. Automatically detects the provider from a URL and routes to the correct fetcher.
Based on `httporchestrator` engine: https://github.com/shahar99s/httporchestrator.py

## Supported Providers

| Provider | Example URL |
|---|---|
| 4Shared | `https://www.4shared.com/s/...` |
| ChatGPT | `https://chatgpt.com/c/...` / `https://chat.openai.com/share/...` |
| Dropbox Transfer | `https://www.dropbox.com/t/...` |
| Filemail | `https://www.filemail.com/d/...` |
| Limewire | `https://limewire.com/d/...` |
| MediaFire | `https://www.mediafire.com/file/...` |
| Mega | `https://mega.nz/file/...` |
| SendAnywhere | `https://send-anywhere.com/web/downloads/...` |
| SendGB | `https://sendgb.com/...` |
| FromSmash | `https://fromsmash.com/...` |
| TeraBox | `https://1024terabox.com/s/...` |
| TransferNow | `https://www.transfernow.net/dl/...` |
| TransferXL | `https://transferxl.com/download/...` |
| WeTransfer | `https://wetransfer.com/downloads/...` / `https://we.tl/...` |

## Installation

```bash
pip install .
```

Dependencies are managed with [Poetry](https://python-poetry.org/):

```bash
poetry install
```

## Quick Start

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

| Argument | Type | Description |
|---|---|---|
| `mode` | `Mode` | Operation mode (default: `Mode.FETCH`) |
| `headers` | `dict` | Extra HTTP headers to include in requests |
| `cookies` | `dict \| str` | Cookies to send (merged into `Cookie` header) |
| `impersonate` | `str \| dict` | TLS fingerprint to impersonate (e.g. `"chrome124"`) |
| `save_path` | `str \| Path` | Directory to save downloaded files |
| `log_details` | `bool` | Enable verbose JSON request/response logging |

### Impersonation

Pass a browser string or a detailed dict to bypass TLS-based bot detection:

```python
fetcher = create_fetcher(
    url,
    impersonate={
        "browser": "Chrome",
        "version": "131",
        "os": "Windows",
        "os_version": "11",
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
    ├── chatgpt_fetcher.py
    ├── dropbox_transfer_fetcher.py
    ├── filemail_fetcher.py
    ├── fourshared_fetcher.py
    ├── limewire_fetcher.py
    ├── mediafire_fetcher.py
    ├── mega_fetcher.py
    ├── sendanywhere_fetcher.py
    ├── sendgb_fetcher.py
    ├── smash_fetcher.py
    ├── terabox_fetcher.py
    ├── transfernow_fetcher.py
    ├── transferxl_fetcher.py
    └── wetransfer_fetcher.py

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
5. Define your `steps` list using `httporchestrator` request steps.

The registry discovers and registers it automatically — no other changes needed.

```python
from link_fetchers.base_fetcher import BaseFetcher

class MyProviderFetcher(BaseFetcher):
    NAME = "MyProvider"
    BASE_URL = "https://myprovider.com"

    @classmethod
    def is_relevant_url(cls, url: str) -> bool:
        return "myprovider.com" in url

    # define self.steps in __init__ ...
```

## Running Tests

```bash
pytest
```

## License

Apache 2.0 — see [LICENSE](LICENSE).
