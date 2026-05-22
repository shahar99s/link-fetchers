from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

from httporchestrator import ConditionalStep, RepeatableStep, RequestStep, Response
from loguru import logger

from link_fetchers.base_fetcher import BaseFetcher
from link_fetchers.utils import Mode, status_is

_STREAM_RE = re.compile(r'streamController\.enqueue\("((?:[^"\\]|\\.)*)"\)')


def decode_react_router_stream(flat: list):
    cache: dict = {}

    def resolve(index):
        if not isinstance(index, int) or isinstance(index, bool):
            return index
        if index < 0:
            return None
        if index in cache:
            return cache[index]
        value = flat[index]
        if isinstance(value, dict):
            result: dict = {}
            cache[index] = result
            for key, value_index in value.items():
                actual_key = resolve(int(key.lstrip("_")))
                actual_value = resolve(value_index)
                if actual_key is not None:
                    result[actual_key] = actual_value
            return result
        if isinstance(value, list):
            if len(value) == 2 and value[0] == "D":
                return value[1]
            result_list: list = []
            cache[index] = result_list
            result_list.extend(resolve(item) for item in value)
            return result_list
        return value

    return resolve(0)


class ChatGPTFetcher(BaseFetcher):
    NAME = "ChatGPT"
    BASE_URL = "https://chatgpt.com"
    IMPERSONATE = "chrome120"
    ALIAS_HOSTS = {"chatgpt.com", "www.chatgpt.com"}
    SHARE_URL_PATTERN = re.compile(r"^/(?:share|c|chat)/[A-Za-z0-9_-]+")
    ACCOUNT_URLS = {"", "/", "/chat", "/chat/"}

    @classmethod
    def is_relevant_url(cls, url: str) -> bool:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        is_chat_host = (
            host == "chat.openai.com"
            or host.endswith(".openai.com")
            or host in cls.ALIAS_HOSTS
        )
        return is_chat_host and (
            bool(cls.SHARE_URL_PATTERN.match(parsed.path))
            or parsed.path in cls.ACCOUNT_URLS
        )

    def __init__(
        self,
        link: str,
        email: str = "",
        password: str = "",
        access_token: str = "",
        session_token: str = "",
    ):
        if not self.is_relevant_url(link):
            raise ValueError("Error: No valid ChatGPT URL provided")

        self.link = self._normalize_alias_host(link)
        self.email = email.strip()
        self.password = password.strip()
        self.access_token = self._strip_bearer(access_token)
        self.session_token = session_token.strip()
        self.has_credentials = bool(self.email and self.password)
        self.is_share_url = (
            self.SHARE_URL_PATTERN.match(urlparse(self.link).path) is not None
        )
        self.is_public_share_url = self._is_public_share_url(self.link)
        self.is_conversation_url = self._is_private_conversation_url()
        self.account_mode = self._is_account_url(self.link)
        self.conversation_id = self._extract_conversation_id(self.link)
        self.headers = self.initial_headers()

        super().__init__()

    def _normalize_alias_host(self, link: str) -> str:
        parsed = urlparse(link)
        host = (parsed.hostname or "").lower()
        if host in self.ALIAS_HOSTS:
            if not self.SHARE_URL_PATTERN.match(parsed.path):
                parsed = parsed._replace(scheme="https", netloc="chatgpt.com")
                return parsed.geturl()
        return link

    def build_info_steps(self) -> list:
        if self.account_mode:
            return self.build_account_info_steps()

        if self.is_public_share_url:
            return [
                RequestStep("load share data")
                .get(f"{self.BASE_URL}/backend-api/share/{self.conversation_id}")
                .headers(**self.headers)
                .after(lambda response, vars: self.parse_share_api_response(response))
                .after(
                    lambda response, vars: (
                        self.log_json(
                            "share fetch fallback",
                            {"status_code": response.status_code},
                        )
                        if not vars.get("conversation_payload")
                        else {}
                    )
                )
                .after(lambda response, vars: self.save_conversation_when_fetch(vars)),
                ConditionalStep(
                    RequestStep("load chat page")
                    .get(self.link)
                    .headers(**self.headers)
                    .after(
                        lambda response, vars: self.parse_share_page_response(response)
                    )
                    .after(
                        lambda response, vars: (
                            self.log_fetch_state(vars["conversation_payload"])
                            if vars.get("conversation_payload")
                            else {}
                        )
                    )
                    .after(
                        lambda response, vars: self.save_conversation_when_fetch(vars)
                    )
                    .check(
                        lambda response, vars: bool(vars.get("conversation_payload")),
                        self.build_unavailable_message(),
                    )
                ).run_when(lambda vars: not vars.get("conversation_payload")),
            ]

        if self.is_conversation_url:
            return [
                self.auth_session_step(),
                RequestStep("load conversation data")
                .get(f"{self.BASE_URL}/backend-api/conversation/{self.conversation_id}")
                .headers(**self.authenticated_headers())
                .after(
                    lambda response, vars: self.parse_conversation_api_response(
                        response
                    )
                )
                .after(lambda response, vars: self.save_conversation_when_fetch(vars)),
                ConditionalStep(
                    RequestStep("load chat page")
                    .get(self.link)
                    .headers(**self.headers)
                    .after(
                        lambda response, vars: self.parse_share_page_response(response)
                    )
                    .after(
                        lambda response, vars: (
                            self.log_fetch_state(vars["conversation_payload"])
                            if vars.get("conversation_payload")
                            else {}
                        )
                    )
                    .after(
                        lambda response, vars: self.save_conversation_when_fetch(vars)
                    )
                    .check(
                        lambda response, vars: bool(vars.get("conversation_payload")),
                        self.build_unavailable_message(),
                    )
                ).run_when(lambda vars: not vars.get("conversation_payload")),
            ]

        raise ValueError(
            "Error: ChatGPT URL does not point to a supported share page or account endpoint"
        )

    def build_fetch_steps(self) -> list:
        if self.account_mode:
            return []

        return []

    def build_account_info_steps(self) -> list:
        if self.has_credentials:
            return [
                RequestStep("login")
                .post("/api/auth/login")
                .headers(**self.headers)
                .json(lambda vars: self.build_login_payload())
                .after(lambda response, vars: self.extract_auth_state(response))
                .check(status_is(200), "expected 200 response"),
                RequestStep("list conversations")
                .get("/backend-api/conversations?limit=200")
                .headers(**self.headers)
                .after(
                    lambda response, vars: self.parse_account_conversations_response(
                        response
                    )
                )
                .after(lambda response, vars: self.save_account_when_fetch(vars))
                .check(status_is(200), "expected 200 response"),
            ]

        steps = [
            self.auth_session_step(),
            RepeatableStep(
                RequestStep("list conversations")
                .get(lambda vars: self.build_conversations_url(vars))
                .headers(**self.authenticated_headers())
                .after(
                    lambda response, vars: self.parse_conversations_page_response(
                        response, vars
                    )
                )
            ).run_while(lambda vars: vars.get("has_more_conversations", True)),
        ]
        if self.mode in {Mode.FETCH, Mode.FORCE_FETCH}:
            steps.append(
                RepeatableStep(
                    RequestStep("load conversation data")
                    .get(lambda vars: self.build_account_conversation_url(vars))
                    .headers(**self.authenticated_headers())
                    .after(
                        lambda response, vars: self.parse_account_conversation_response(
                            response, vars
                        )
                    )
                    .after(
                        lambda response, vars: self.save_account_conversation_response(
                            vars
                        )
                    )
                ).run_while(
                    lambda vars: (
                        vars.get("conversation_fetch_index", 0)
                        < len(vars.get("conversations", []))
                    )
                )
            )
        return steps

    def save_conversations_when_fetch(self, conversations: list[dict]) -> dict:
        if self.mode not in {Mode.FETCH, Mode.FORCE_FETCH}:
            return {}
        return self.write_account_conversations_file(
            conversations, self.build_account_filename()
        )

    def save_account_when_fetch(self, vars: dict) -> dict:
        conversations = vars.get("conversations")
        if not isinstance(conversations, list):
            return {}

        return self.save_conversations_when_fetch(conversations)

    def save_conversation_when_fetch(self, vars: dict) -> dict:
        if not vars.get("conversation_payload"):
            return {}
        return self.write_conversation_file(
            vars["conversation_payload"], vars["filename"]
        )

    def build_account_filename(self) -> str:
        if self.email:
            safe_email = self._sanitize_filename(self.email.split("@")[0])
        else:
            safe_email = "chatgpt-account"
        return f"{safe_email}-conversations.md"

    def auth_session_step(self) -> RequestStep:
        return (
            RequestStep("load auth session")
            .get(f"{self.BASE_URL}/api/auth/session")
            .headers(**self.headers)
            .after(lambda response, vars: self.parse_auth_session_response(response))
        )

    def authenticated_headers(self) -> dict:
        headers = dict(self.headers)
        headers["Authorization"] = lambda vars: self.authorization_header(vars)
        return headers

    def authorization_header(self, vars: dict) -> str:
        token = self._strip_bearer(vars.get("access_token") or self.access_token or "")
        return f"Bearer {token}" if token else ""

    def build_conversations_url(self, vars: dict) -> str:
        offset = vars.get("conversation_offset", 0)
        limit = vars.get("conversation_limit", 100)
        return f"{self.BASE_URL}/backend-api/conversations?offset={offset}&limit={limit}&order=updated"

    def build_account_conversation_url(self, vars: dict) -> str:
        conversation = self.current_account_conversation(vars)
        conversation_id = conversation.get("id") or conversation.get("conversation_id")
        return f"{self.BASE_URL}/backend-api/conversation/{conversation_id}"

    def current_account_conversation(self, vars: dict) -> dict:
        conversations = vars.get("conversations") or []
        index = vars.get("conversation_fetch_index", 0)
        if not isinstance(conversations, list) or index >= len(conversations):
            return {}
        conversation = conversations[index]
        return conversation if isinstance(conversation, dict) else {}

    def build_login_payload(self) -> dict:
        return {"email": self.email, "password": self.password}

    def extract_auth_state(self, response: Response) -> dict:
        if response.status_code != 200:
            raise ValueError("Error: ChatGPT login failed")
        return {}

    def parse_auth_session_response(self, response: Response) -> dict:
        if response.status_code != 200:
            return {}
        try:
            payload = response.json()
        except ValueError:
            return {}
        if not isinstance(payload, dict):
            return {}
        token = self._strip_bearer(payload.get("accessToken") or "")
        if not token:
            return {}
        self.access_token = token
        self.headers["Authorization"] = f"Bearer {token}"
        return {"access_token": token}

    def parse_conversations(self, response: Response) -> list[dict]:
        return self.parse_conversations_payload(response.json())

    def parse_conversations_payload(self, payload: Any) -> list[dict]:
        if isinstance(payload, dict) and isinstance(payload.get("items"), list):
            return payload["items"]
        if isinstance(payload, list):
            return payload
        raise ValueError("Error: Unexpected ChatGPT conversations response")

    def parse_account_conversations_response(self, response: Response) -> dict:
        conversations = self.parse_conversations(response)
        self.log_fetch_state(conversations)
        return {"conversations": conversations}

    def parse_conversations_page_response(self, response: Response, vars: dict) -> dict:
        if response.status_code != 200:
            raise ValueError(
                "Error: Unable to load ChatGPT conversations. Provide a valid Cookie header."
            )

        batch = self.parse_conversations(response)
        conversations = list(vars.get("conversations") or [])
        seen_ids = set(vars.get("conversation_ids") or [])
        new_items = []
        for conversation in batch:
            conversation_id = str(
                conversation.get("id") or conversation.get("conversation_id") or ""
            )
            if conversation_id and conversation_id in seen_ids:
                continue
            if conversation_id:
                seen_ids.add(conversation_id)
            new_items.append(conversation)

        conversations.extend(new_items)
        limit = vars.get("conversation_limit", 100)
        offset = vars.get("conversation_offset", 0)
        has_more = len(batch) >= limit and bool(new_items)
        updates = {
            "conversations": conversations,
            "conversation_ids": list(seen_ids),
            "conversation_offset": offset + limit,
            "conversation_limit": limit,
            "has_more_conversations": has_more,
            "conversations_count": len(conversations),
        }

        if not has_more:
            self.log_fetch_state(conversations)
            updates.update(
                self.save_account_when_fetch({"conversations": conversations})
            )
            if self.mode in {Mode.FETCH, Mode.FORCE_FETCH}:
                updates.update(
                    {
                        "conversation_fetch_index": 0,
                        "conversation_files": [],
                        "conversation_fetch_failures": [],
                    }
                )

        return updates

    def parse_account_conversation_response(
        self, response: Response, vars: dict
    ) -> dict:
        conversation = self.current_account_conversation(vars)
        state = self.parse_conversation_api_response(
            response, fallback_title=conversation.get("title")
        )
        return {"account_conversation_state": state}

    def save_account_conversation_response(self, vars: dict) -> dict:
        conversation = self.current_account_conversation(vars)
        conversation_id = str(
            conversation.get("id") or conversation.get("conversation_id") or ""
        )
        state = vars.get("account_conversation_state") or {}
        saved_paths = list(vars.get("conversation_files") or [])
        failed_ids = list(vars.get("conversation_fetch_failures") or [])

        if state.get("conversation_payload"):
            filename = self.build_account_conversation_filename(
                state["conversation_payload"],
                conversation_id,
            )
            result = self.write_conversation_file(
                state["conversation_payload"], filename
            )
            if result.get("local_file_path"):
                saved_paths.append(result["local_file_path"])
        elif conversation_id:
            failed_ids.append(conversation_id)

        return {
            "conversation_fetch_index": vars.get("conversation_fetch_index", 0) + 1,
            "conversation_files": saved_paths,
            "conversation_fetch_failures": failed_ids,
        }

    def write_account_conversations_file(
        self, conversations: list[dict], filename: str
    ) -> dict:
        content = self.format_conversations(conversations)
        path = self.resolve_save_path(filename)
        with open(path, "w", encoding="utf-8") as file_handle:
            file_handle.write(content)

        logger.success(
            "[{}] saved account conversations to {} ({} bytes)",
            self.NAME,
            path,
            len(content.encode("utf-8")),
        )
        return {"local_file_path": path}

    def format_conversations(self, conversations: list[dict]) -> str:
        lines = ["# ChatGPT Conversations", ""]
        lines.append(f"**Source URL:** {self.link}")
        lines.append("")
        lines.append(f"**Conversation count:** {len(conversations)}")
        lines.append("")

        for index, conversation in enumerate(conversations, start=1):
            summary = self.conversation_summary(conversation)
            title = summary["title"] or f"Conversation {index}"
            created_at = summary["created_at"]
            updated_at = summary["updated_at"]
            conversation_id = summary["id"]
            lines.extend(
                [
                    f"## {index}. {title}",
                    f"- ID: {conversation_id}",
                    f"- Created: {created_at}",
                    f"- Updated: {updated_at}",
                    "",
                ]
            )

            snippet = self._extract_message_text(
                conversation.get("snippet")
                or conversation.get("summary")
                or conversation.get("title")
                or ""
            )
            if snippet:
                lines.extend(["**Snippet:**", snippet, ""])

        if not conversations:
            lines.append("(no conversations found)")

        return "\n".join(lines)

    def log_fetch_state(self, conversation_payload: dict | list[dict]) -> dict:
        if self.account_mode:
            conversations = (
                conversation_payload if isinstance(conversation_payload, list) else []
            )
            conversation_summaries = [
                self.conversation_summary(conversation)
                for conversation in conversations
            ]
            self.log_fetch_snapshot(
                summary={
                    "provider": self.NAME,
                    "account": self.email or self.link,
                    "conversations_count": len(conversations),
                },
                details={"conversations": conversation_summaries},
            )
            return {}

        self.log_fetch_snapshot(
            summary={
                "provider": self.NAME,
                "conversation_id": self.conversation_id,
                "title": conversation_payload.get("title"),
                "messages_count": len(conversation_payload.get("messages", [])),
                "source_url": conversation_payload.get("source_url"),
            },
            details={"conversation": conversation_payload},
        )
        return {}

    def parse_share_page(self, html: str) -> dict:
        page_data = self._extract_json_from_html(html)
        conversation_payload = self._extract_conversation_payload(page_data)
        if not conversation_payload:
            raise ValueError(
                "Error: Unable to extract ChatGPT conversation from page HTML"
            )

        filename = self.build_filename(conversation_payload)
        return {
            "conversation_payload": conversation_payload,
            "filename": filename,
            "source_url": self.link,
        }

    def parse_share_page_response(self, response: Response) -> dict:
        html = response.text
        try:
            return self.parse_share_page(html)
        except ValueError as exc:
            return {"chatgpt_error": str(exc)}

    def parse_share_api_response(self, response: Response) -> dict:
        payload = None
        if response.status_code == 200:
            try:
                payload = response.json()
            except ValueError:
                payload = None

        if not isinstance(payload, dict):
            return {}

        conversation_payload = self._extract_conversation_payload(payload)
        if not conversation_payload:
            error_message = self._extract_chatgpt_error(payload)
            if error_message:
                return {"chatgpt_error": error_message}
            return {}

        filename = self.build_filename(conversation_payload)
        return {
            "conversation_payload": conversation_payload,
            "filename": filename,
            "source_url": self.link,
        }

    def parse_conversation_api_response(
        self, response: Response, fallback_title: str | None = None
    ) -> dict:
        payload = None
        if response.status_code == 200:
            try:
                payload = response.json()
            except ValueError:
                payload = None

        return self.parse_conversation_api_payload(
            payload, fallback_title=fallback_title
        )

    def parse_conversation_api_payload(
        self, payload: Any, fallback_title: str | None = None
    ) -> dict:
        if not isinstance(payload, dict):
            return {}

        conversation_payload = self._extract_conversation_payload(payload)
        if not conversation_payload:
            error_message = self._extract_chatgpt_error(payload)
            if error_message:
                return {"chatgpt_error": error_message}
            return {}
        if fallback_title and not conversation_payload.get("title"):
            conversation_payload["title"] = fallback_title

        filename = self.build_filename(conversation_payload)
        return {
            "conversation_payload": conversation_payload,
            "filename": filename,
            "source_url": self.link,
        }

    def write_conversation_file(
        self, conversation_payload: dict, filename: str
    ) -> dict:
        content = self.format_conversation(conversation_payload)
        path = self.resolve_save_path(filename)
        with open(path, "w", encoding="utf-8") as file_handle:
            file_handle.write(content)

        logger.success(
            "[{}] saved conversation transcript to {} ({} bytes)",
            self.NAME,
            path,
            len(content.encode("utf-8")),
        )
        return {"local_file_path": path}

    def build_filename(self, conversation_payload: dict) -> str:
        title = (
            conversation_payload.get("title")
            or self.conversation_id
            or "chatgpt-conversation"
        )
        sanitized_title = self._sanitize_filename(title)
        if not sanitized_title:
            sanitized_title = f"chatgpt-{self.conversation_id}"
        return f"{sanitized_title}.md"

    def build_account_conversation_filename(
        self, conversation_payload: dict, conversation_id: str
    ) -> str:
        title = (
            conversation_payload.get("title")
            or conversation_id
            or "chatgpt-conversation"
        )
        sanitized_title = self._sanitize_filename(title) or "chatgpt-conversation"
        safe_id = (
            self._sanitize_filename(conversation_id)[-12:] if conversation_id else ""
        )
        if safe_id:
            return f"{sanitized_title}-{safe_id}.md"
        return f"{sanitized_title}.md"

    def format_conversation(self, conversation_payload: dict) -> str:
        lines = ["# ChatGPT Conversation", ""]
        if conversation_payload.get("title"):
            lines.extend([f"**Title:** {conversation_payload['title']}", ""])
        lines.extend([f"**Source URL:** {self.link}", ""])
        lines.append("## Messages")
        lines.append("")

        for index, message in enumerate(
            conversation_payload.get("messages", []), start=1
        ):
            author = self._message_author(message)
            role = self._message_role(message)
            body = self._extract_message_text(
                message.get("content") or message.get("message") or message.get("text")
            )
            lines.extend(
                [
                    f"### Message {index}",
                    f"- Role: {role}",
                    f"- Author: {author}",
                    "",
                    body or "(no text)",
                    "",
                ]
            )

        if not conversation_payload.get("messages"):
            lines.append("(no messages found)")

        return "\n".join(lines)

    def conversation_summary(self, conversation: dict) -> dict:
        return {
            "id": conversation.get("id")
            or conversation.get("conversation_id")
            or "unknown",
            "title": conversation.get("title") or "",
            "created_at": conversation.get("create_time")
            or conversation.get("created_at"),
            "updated_at": conversation.get("update_time")
            or conversation.get("updated_at"),
        }

    def _extract_json_from_html(self, html: str) -> dict:
        patterns = [
            r"<script id=\"__NEXT_DATA__\" type=\"application/json\">(.+?)</script>",
            r"window\.__OPENAI_PRELOADED_STATE__\s*=\s*({.+?});",
            r"window\.__INITIAL_STATE__\s*=\s*({.+?});",
        ]
        for pattern in patterns:
            match = re.search(pattern, html, re.DOTALL)
            if not match:
                continue
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
        stream_data = self._extract_react_router_stream(html)
        if stream_data:
            return stream_data
        raise ValueError("Error: ChatGPT page JSON payload not found")

    def _extract_react_router_stream(self, html: str) -> dict | None:
        chunks = []
        for match in _STREAM_RE.finditer(html):
            try:
                chunks.append(json.loads('"' + match.group(1) + '"'))
            except json.JSONDecodeError:
                continue

        for chunk in chunks:
            try:
                decoded = decode_react_router_stream(json.loads(chunk))
            except (json.JSONDecodeError, ValueError, TypeError):
                continue
            if isinstance(decoded, dict):
                return decoded
        return None

    def _extract_conversation_payload(self, page_data: Any) -> dict | None:
        if not isinstance(page_data, dict):
            return None

        conversation = self._find_key(page_data, "conversation")
        if isinstance(conversation, dict) and conversation.get("messages"):
            title = conversation.get("title") or self._find_key(page_data, "title")
            return {
                "title": title,
                "messages": conversation.get("messages", []),
            }

        messages = self._find_key(page_data, "messages")
        if isinstance(messages, list):
            title = self._find_key(page_data, "title") or self.conversation_id
            return {"title": title, "messages": messages}

        mapping = self._find_key(page_data, "mapping")
        if isinstance(mapping, dict):
            messages = self._messages_from_mapping(
                mapping, self._find_key(page_data, "current_node")
            )
            if messages:
                title = self._find_key(page_data, "title") or self.conversation_id
                return {"title": title, "messages": messages}

        return None

    def _messages_from_mapping(
        self, mapping: dict, current_node: str | None = None
    ) -> list[dict]:
        if current_node and current_node in mapping:
            path = []
            node_id = current_node
            seen = set()
            while node_id and node_id in mapping and node_id not in seen:
                seen.add(node_id)
                node = mapping[node_id]
                message = node.get("message") if isinstance(node, dict) else None
                if isinstance(message, dict):
                    path.append(message)
                node_id = node.get("parent") if isinstance(node, dict) else None
            return list(reversed(path))

        messages = []
        for node in mapping.values():
            if not isinstance(node, dict):
                continue
            message = node.get("message")
            if isinstance(message, dict):
                messages.append(message)
        return sorted(messages, key=lambda message: message.get("create_time") or 0)

    def _find_key(self, obj: Any, key: str, seen: set[int] | None = None) -> Any:
        if seen is None:
            seen = set()
        if isinstance(obj, (dict, list)):
            obj_id = id(obj)
            if obj_id in seen:
                return None
            seen.add(obj_id)

        if isinstance(obj, dict):
            if key in obj:
                return obj[key]
            for value in obj.values():
                found = self._find_key(value, key, seen)
                if found is not None:
                    return found
        elif isinstance(obj, list):
            for item in obj:
                found = self._find_key(item, key, seen)
                if found is not None:
                    return found
        return None

    def _is_account_url(self, link: str) -> bool:
        parsed = urlparse(link)
        return parsed.path in self.ACCOUNT_URLS

    def _is_public_share_url(self, link: str) -> bool:
        parts = [part for part in urlparse(link).path.split("/") if part]
        return bool(parts and parts[0] == "share")

    def _is_private_conversation_url(self) -> bool:
        parts = [part for part in urlparse(self.link).path.split("/") if part]
        return bool(parts and parts[0] in {"c", "chat"})

    def _extract_message_text(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, dict):
            if "text" in value:
                return self._extract_message_text(value["text"])
            if "parts" in value:
                return self._extract_message_text(value["parts"])
            return " ".join(
                self._extract_message_text(v)
                for v in value.values()
                if self._extract_message_text(v)
            )
        if isinstance(value, list):
            return "".join(self._extract_message_text(item) for item in value)
        return ""

    def _message_author(self, message: dict) -> str:
        author = message.get("author") or {}
        if isinstance(author, dict):
            return author.get("name") or author.get("role") or "unknown"
        return str(author)

    def _message_role(self, message: dict) -> str:
        author = message.get("author") or {}
        if isinstance(author, dict) and author.get("role"):
            return author["role"]
        return message.get("role") or "unknown"

    def _sanitize_filename(self, name: str) -> str:
        sanitized = re.sub(r"[\\/:*?\"<>|]+", "-", name).strip()
        sanitized = re.sub(r"\s+", "-", sanitized)
        return sanitized[:200]

    def initial_headers(self) -> dict[str, str]:
        headers = super().initial_headers()
        if self.session_token:
            headers["Cookie"] = self._merge_cookie_header(
                headers.get("Cookie", ""),
                self._normalize_session_token(self.session_token),
            )
        if self.access_token:
            headers.setdefault("Authorization", f"Bearer {self.access_token}")
        if self.is_share_url or self.account_mode:
            headers.setdefault("Referer", f"{self.BASE_URL}/")
            headers.setdefault("Origin", self.BASE_URL)
        return headers

    def _extract_conversation_id(self, link: str) -> str:
        parsed = urlparse(link)
        parts = [part for part in parsed.path.split("/") if part]
        return parts[-1] if parts else "chatgpt"

    def _strip_bearer(self, value: str) -> str:
        value = (value or "").strip()
        if value.lower().startswith("bearer "):
            return value.split(None, 1)[1].strip()
        return value

    def _normalize_session_token(self, session_token: str) -> dict[str, str]:
        if "=" in session_token or ";" in session_token:
            return self._parse_cookie_header(session_token)
        return {"__Secure-next-auth.session-token": session_token}

    def _extract_chatgpt_error(self, payload: dict) -> str:
        detail = payload.get("detail")
        if isinstance(detail, dict):
            return (
                detail.get("message")
                or detail.get("description")
                or detail.get("code")
                or ""
            )
        if isinstance(detail, str):
            return detail
        meta = self._find_key(payload, "meta")
        if isinstance(meta, str):
            return meta
        return ""

    def build_unavailable_message(self) -> str:
        if self._is_private_conversation_url():
            return (
                "Error: ChatGPT conversation is private or inaccessible. "
                "Provide a ChatGPT Authorization bearer token, session_token, or a valid Cookie header."
            )
        return "Error: ChatGPT shared conversation could not be loaded"
