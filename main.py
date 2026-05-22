from link_fetchers.fetcher_registry import create_fetcher
from link_fetchers.utils import Mode

if __name__ == "__main__":
    mode = Mode.FETCH  # or Mode.INFO / Mode.FETCH / Mode.FORCE_FETCH

    runner = create_fetcher(
        "https://www.4shared.com/s/fkEvKTDlWge",
        impersonate={
            "browser": "Chrome",
            "version": "131",
            "os": "Windows",
            "os_version": "11",
        },
        mode=mode,
    )
    runner.run()
