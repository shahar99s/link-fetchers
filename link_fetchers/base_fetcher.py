from __future__ import annotations

import json
import os
from collections.abc import Callable, Sequence
from datetime import datetime
from functools import wraps
from typing import Any

from httporchestrator import ConditionalStep, Flow, RequestStep
from loguru import logger

from link_fetchers.tls_client import CurlImpersonatingClient
from link_fetchers.utils import Mode, resolve_filename, should_download, status_is


class BaseFetcher:
    NAME = None
    BASE_URL = None
    steps = []
    IMPERSONATE = None
    _FETCHER_BASE_KWARGS = {
        "headers",
        "cookies",
        "impersonate",
        "log_details",
        "mode",
        "save_path",
        "retry_times",
        "retry_interval",
    }
    _FETCHER_BASE_KWARGS_ATTR = "_fetcher_base_kwargs"

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)

        original_init = cls.__init__
        if getattr(original_init, "_fetcher_base_kwargs_wrapped", False):
            return

        @wraps(original_init)
        def wrapped_init(self, *args, **kwargs):
            base_kwargs = {}
            for key in BaseFetcher._FETCHER_BASE_KWARGS:
                if key in kwargs:
                    base_kwargs[key] = kwargs.pop(key)
            setattr(self, BaseFetcher._FETCHER_BASE_KWARGS_ATTR, base_kwargs)
            try:
                original_init(self, *args, **kwargs)
            finally:
                if hasattr(self, BaseFetcher._FETCHER_BASE_KWARGS_ATTR):
                    delattr(self, BaseFetcher._FETCHER_BASE_KWARGS_ATTR)

        wrapped_init._fetcher_base_kwargs_wrapped = True
        cls.__init__ = wrapped_init

    def constructor_base_kwargs(self) -> dict:
        return dict(getattr(self, self._FETCHER_BASE_KWARGS_ATTR, {}) or {})

    def initial_headers(self) -> dict[str, str]:
        base_kwargs = self.constructor_base_kwargs()
        headers = dict(base_kwargs.get("headers") or {})
        cookies = base_kwargs.get("cookies")
        if cookies:
            headers["Cookie"] = self._merge_cookie_header(
                headers.get("Cookie", ""),
                self._normalize_cookies(cookies),
            )
        return headers

    def _normalize_cookies(self, cookies: dict[str, str] | str) -> dict[str, str]:
        if isinstance(cookies, str):
            return self._parse_cookie_header(cookies)
        return dict(cookies or {})

    def _parse_cookie_header(self, cookie_header: str) -> dict[str, str]:
        cookie_map: dict[str, str] = {}
        for pair in (cookie_header or "").split(";"):
            if not pair.strip():
                continue
            if "=" in pair:
                key, value = pair.split("=", 1)
                cookie_map[key.strip()] = value.strip()
            else:
                cookie_map[pair.strip()] = ""
        return cookie_map

    def _format_cookie_header(self, cookies: dict[str, str]) -> str:
        return "; ".join(f"{key}={value}" for key, value in cookies.items())

    def _merge_cookie_header(
        self, existing_cookie_header: str, cookies: dict[str, str]
    ) -> str:
        existing = self._parse_cookie_header(existing_cookie_header or "")
        existing.update(cookies)
        return self._format_cookie_header(existing)

    def log_fetch_snapshot(self, summary: dict, details: dict) -> None:
        self.log_json("fetch snapshot", {"summary": summary, "details": details})

    def log_json(self, label: str, payload: dict):
        logger.info(
            "[{}] {}\n{}",
            self.NAME,
            label,
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
        )

    def transform_body(self, body: bytes) -> bytes:
        return body

    def steps_for_mode(
        self,
        mode: Mode,
        info_steps: Sequence,
        fetch_steps: Sequence | None = None,
    ) -> list:
        steps = list(info_steps)
        if mode == Mode.INFO or fetch_steps is None:
            return steps
        return [*steps, *list(fetch_steps)]

    def build_info_steps(self) -> list:
        return list(getattr(type(self), "steps", []))

    def build_fetch_steps(self) -> list:
        return []

    def build_steps(self, mode: Mode) -> list:
        steps = self.steps_for_mode(
            mode, self.build_info_steps(), self.build_fetch_steps()
        )
        if self.retry_times > 0:
            steps = self._apply_retry(steps)
        return steps

    def _apply_retry(self, steps: list) -> list:
        from dataclasses import replace

        result = []
        for step in steps:
            if isinstance(step, RequestStep):
                step = step.retry(self.retry_times, self.retry_interval)
            elif isinstance(step, ConditionalStep) and isinstance(
                step.step, RequestStep
            ):
                step = replace(
                    step, step=step.step.retry(self.retry_times, self.retry_interval)
                )
            result.append(step)
        return result

    def extract_by_key_or_cb(
        self, key_or_cb: str | Callable[[dict], Any], variables: dict
    ) -> Any:
        if callable(key_or_cb):
            return key_or_cb(variables)
        return variables[key_or_cb]

    def should_fetch(
        self,
        variables: dict,
        *,
        downloads_count_key: str = "downloads_count",
        downloads_count: int | None = None,
        when: Callable[[dict], bool] | None = None,
    ) -> bool:
        count = (
            downloads_count
            if downloads_count is not None
            else variables.get(downloads_count_key)
        )
        return should_download(self.mode, count) and (
            when is None or bool(when(variables))
        )

    def download_step(
        self,
        *,
        name: str = "download",
        url_key: str | Callable[[dict], Any] = "direct_link",
        filename_key: str | Callable[[dict], Any] = "filename",
        headers: dict | None = None,
        capture_filename: str | None = None,
        downloads_count: int | None = None,
        when: Callable[[dict], bool] | None = None,
    ) -> ConditionalStep:
        request = (
            RequestStep(name)
            .get(lambda variables: self.extract_by_key_or_cb(url_key, variables))
            .headers(**(self.headers if headers is None else headers))
            .after(
                lambda response, variables: self.save_file(
                    response, self.extract_by_key_or_cb(filename_key, variables)
                )
            )
            .check(status_is(200), "expected 200 response")
        )
        if capture_filename:
            request = request.capture(
                capture_filename,
                lambda response, variables: resolve_filename(
                    response.headers,
                    self.extract_by_key_or_cb(filename_key, variables),
                ),
            )
        return request.when(
            lambda variables: self.should_fetch(
                variables, downloads_count=downloads_count, when=when
            )
        )

    def resolve_save_path(self, filename: str) -> str:
        resolved_name = os.path.basename(filename)
        os.makedirs(self.save_path, exist_ok=True)
        return os.path.join(self.save_path, resolved_name)

    def save_file(self, response: object, fallback_filename: str) -> dict:
        if response.status_code != 200:
            logger.warning(
                "[{}] download failed with HTTP {} - skipping save",
                self.NAME,
                response.status_code,
            )
            return {}

        resolved_name = os.path.basename(
            resolve_filename(response.headers, fallback_filename)
        )
        payload = self.transform_body(response.body)

        path = self.resolve_save_path(resolved_name)
        with open(path, "wb") as file_handle:
            file_handle.write(payload)

        logger.success(
            "[{}] downloaded file saved to {} ({} bytes)",
            self.NAME,
            path,
            len(payload),
        )
        return {"local_file_path": path}

    def __init__(
        self,
        *,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | str | None = None,
        mode: Mode | None = None,
        log_details: bool | None = None,
        save_path: str | os.PathLike | None = None,
        retry_times: int = 3,
        retry_interval: float = 2.0,
    ):
        base_kwargs = self.constructor_base_kwargs()
        if cookies is None:
            cookies = base_kwargs.get("cookies")
        if headers is None:
            headers = base_kwargs.get("headers")
        if mode is None:
            mode = base_kwargs.get("mode", Mode.FETCH)
        if log_details is None:
            log_details = base_kwargs.get("log_details", False)
        if save_path is None:
            save_path = base_kwargs.get("save_path")
        self.retry_times: int = base_kwargs.get("retry_times", retry_times)
        self.retry_interval: float = base_kwargs.get("retry_interval", retry_interval)
        self.save_path = os.path.abspath(os.fspath(save_path or os.getcwd()))
        self.mode = mode
        self.impersonate = base_kwargs.get("impersonate", self.IMPERSONATE)
        self.cookies = self._normalize_cookies(cookies) if cookies else {}
        if not hasattr(self, "headers"):
            self.headers = dict(headers or {})
        if self.cookies:
            self.headers["Cookie"] = self._merge_cookie_header(
                self.headers.get("Cookie", ""),
                self.cookies,
            )
        self.flow = Flow(
            name=self.NAME,
            base_url=self.BASE_URL or "",
            steps=tuple(self.build_steps(mode)),
            log_details=log_details,
            add_request_id=False,
        ).with_artifact_dir(self.save_path)
        self.steps = list(self.flow.steps)

    def variables(self, variables: dict) -> "BaseFetcher":
        self.flow = self.flow.state(dict(variables or {}))
        return self

    def export(self, export: list[str]) -> "BaseFetcher":
        self.flow = self.flow.export(list(export))
        return self

    def _log_case_id(self) -> str:
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        name = (self.NAME or "fetcher").lower().replace(" ", "-")
        mode = self.mode.name.lower() if self.mode else "unknown"
        return f"{ts}_{name}_{mode}"

    def run(self, param: dict | None = None):
        self.flow = self.flow.with_steps(tuple(self.steps))
        client = self.build_http_client()
        case_id = self._log_case_id()
        if client is None:
            return self.flow.run(inputs=param, case_id=case_id)
        try:
            return self.flow.run(inputs=param, client=client, case_id=case_id)
        finally:
            client.close()

    def build_http_client(self):
        if not self.impersonate:
            return None
        return CurlImpersonatingClient(impersonate=self.impersonate)
