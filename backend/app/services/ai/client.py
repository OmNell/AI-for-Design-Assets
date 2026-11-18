"""百炼 OpenAI 兼容调用；不记录提示词、图片、API Key 或 NAS 信息。"""
from __future__ import annotations

import base64
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
from io import BytesIO
import json
import logging
import re
import threading
import time

import httpx
from PIL import Image, UnidentifiedImageError

from app.config import AiConfig
from app.db.database import Database
from app.db.models import AiCall
from app.services.crypto import CryptoError, SecretCipher
from .parsing import FieldDefinition
from .prompts import build_prompt

logger = logging.getLogger("nas_workbench")


class AiCallError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class _AttemptError(Exception):
    def __init__(self, message: str, retryable: bool = False, usage: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable
        self.usage = usage or {}


@dataclass(frozen=True)
class _Result:
    value: str
    usage: dict[str, object]


class _CallLimiter:
    """所有配置代际共享并发计数；设置变更只约束之后取得的名额。"""
    def __init__(self) -> None:
        self.condition = threading.Condition()
        self.limit = 3
        self.active = 0

    def configure(self, limit: int) -> None:
        with self.condition:
            self.limit = limit
            self.condition.notify_all()

    @contextmanager
    def slot(self) -> Iterator[None]:
        with self.condition:
            self.condition.wait_for(lambda: self.active < self.limit)
            self.active += 1
        try:
            yield
        finally:
            with self.condition:
                self.active -= 1
                self.condition.notify_all()


_LIMITER = _CallLimiter()


def validate_review_image(image: bytes) -> None:
    try:
        with Image.open(BytesIO(image), formats=("JPEG",)) as decoded:
            if max(decoded.size) > 1024 or decoded.getexif():
                raise AiCallError("只允许发送长边不超过1024且无EXIF的JPEG送审图")
            decoded.verify()
    except AiCallError:
        raise
    except (OSError, UnidentifiedImageError, ValueError, SyntaxError):
        raise AiCallError("送审图不是可解码的JPEG") from None


def _test_image() -> bytes:
    stream = BytesIO()
    with Image.new("RGB", (1, 1), "white") as image:
        image.save(stream, format="JPEG")
    return stream.getvalue()


def _usage(body: object) -> dict[str, object]:
    candidate = body.get("usage", {}) if isinstance(body, dict) else {}
    return candidate if isinstance(candidate, dict) else {}


class AiClient:
    def __init__(self, config: AiConfig, database: Database, cipher: SecretCipher,
                 transport: httpx.BaseTransport | None = None, retry_delay_seconds: float = 0.1) -> None:
        self.config = config.model_copy(deep=True)
        self.database = database
        self.cipher = cipher
        self.retry_delay_seconds = retry_delay_seconds
        self.http = httpx.Client(timeout=config.timeout_s, transport=transport, follow_redirects=False, trust_env=False)
        self._secret = ""
        _LIMITER.configure(config.concurrency)

    def close(self) -> None:
        self.http.close()
        self._secret = ""

    def activate_limit(self) -> None:
        _LIMITER.configure(self.config.concurrency)

    def _key(self) -> str:
        encrypted = self.config.api_key_enc.get_secret_value()
        if not encrypted:
            raise _AttemptError("尚未设置API Key")
        try:
            key = self.cipher.decrypt(encrypted)
        except CryptoError:
            raise _AttemptError("API Key 解密失败，请重新设置") from None
        if not key:
            raise _AttemptError("尚未设置API Key")
        self._secret = key
        return key

    def _safe_error(self, message: str, payload: dict[str, object]) -> str:
        if self._secret:
            message = message.replace(self._secret, "[凭据已隐藏]")
        # 厂商错误可能回显输入，仍不能把完整提示词/图像写入日志或数据库。
        messages = payload.get("messages", [])
        if isinstance(messages, list):
            for item in messages:
                content = item.get("content") if isinstance(item, dict) else None
                if isinstance(content, str) and content:
                    message = message.replace(content, "[提示词已隐藏]")
                elif isinstance(content, list):
                    for part in content:
                        if not isinstance(part, dict):
                            continue
                        text = part.get("text")
                        if isinstance(text, str) and text:
                            message = message.replace(text, "[提示词已隐藏]")
                        url = part.get("image_url")
                        if isinstance(url, dict) and isinstance(url.get("url"), str):
                            message = message.replace(url["url"], "[图片内容已隐藏]")
        message = re.sub(r"data:image/[^;\s]+;base64,[A-Za-z0-9+/=]+", "[图片内容已隐藏]", message)
        message = re.sub(r"[A-Za-z0-9+/]{128,}={0,2}", "[长数据已隐藏]", message)
        return message[:4000]

    def _post(self, endpoint: str, model: str, payload: dict[str, object], *, diagnostics: bool = False) -> object:
        if not model.strip():
            raise _AttemptError("模型名尚未配置")
        key = self._key()
        try:
            base = httpx.URL(self.config.base_url)
            if base.scheme != "https" or not base.host or base.username or base.password or base.query or base.fragment:
                raise ValueError("接口地址格式不正确")
        except (ValueError, httpx.InvalidURL):
            raise _AttemptError("AI接口地址格式不正确") from None
        try:
            response = self.http.post(str(base).rstrip("/") + endpoint, json=payload,
                                      headers={"Authorization": f"Bearer {key}"})
        except httpx.TimeoutException:
            raise _AttemptError("AI请求超时", retryable=True) from None
        except httpx.HTTPError:
            raise _AttemptError("无法连接AI接口", retryable=True) from None
        try:
            body: object = response.json()
        except ValueError:
            body = None
        if not 200 <= response.status_code < 300:
            detail: object = body.get("error", body) if isinstance(body, dict) else response.text
            if isinstance(detail, dict):
                detail = detail.get("message", json.dumps(detail, ensure_ascii=False))
            # 业务请求的厂商错误可能只回显姓名/字段提示等输入片段，不将其落库。
            # 只有不含业务数据的管理员最小连接测试保留脱敏原文。
            message = self._safe_error(str(detail), payload) if diagnostics and detail else f"AI接口返回HTTP {response.status_code}"
            raise _AttemptError(message, response.status_code in (408, 429) or response.status_code >= 500,
                                _usage(body))
        if body is None:
            raise _AttemptError("AI接口响应不是有效JSON", retryable=True)
        return body

    def _chat(self, model: str, payload: dict[str, object], *, diagnostics: bool = False) -> _Result:
        body = self._post("/chat/completions", model, payload, diagnostics=diagnostics)
        choices = body.get("choices") if isinstance(body, dict) else None
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            message = choices[0].get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, str) and content.strip():
                return _Result(content, _usage(body))
            if isinstance(content, list):
                text = "".join(part["text"] for part in content if isinstance(part, dict) and isinstance(part.get("text"), str))
                if text.strip():
                    return _Result(text, _usage(body))
        raise _AttemptError("AI接口未返回文本内容", retryable=True, usage=_usage(body))

    def _embedding_test(self, model: str, payload: dict[str, object]) -> _Result:
        body = self._post("/embeddings", model, payload, diagnostics=True)
        data = body.get("data") if isinstance(body, dict) else None
        embedding = data[0].get("embedding") if isinstance(data, list) and data and isinstance(data[0], dict) else None
        if not isinstance(embedding, list) or not embedding or not all(type(value) in (int, float) for value in embedding):
            raise _AttemptError("向量模型未返回有效测试结果", retryable=True, usage=_usage(body))
        return _Result("连接成功", _usage(body))

    def _record(self, model: str, kind: str, milliseconds: int, usage: dict[str, object], success: bool,
                error: str | None, batch_id: int | None, product_id: int | None) -> None:
        def count(name: str, fallback: str = "") -> int:
            value = usage.get(name, usage.get(fallback, 0))
            return value if type(value) is int and value >= 0 else 0
        prompt_tokens = count("prompt_tokens", "input_tokens")
        completion_tokens = count("completion_tokens", "output_tokens")
        total_tokens = count("total_tokens") or prompt_tokens + completion_tokens
        with self.database.session() as session:
            session.add(AiCall(model=model, kind=kind, batch_id=batch_id, batch_product_id=product_id,
                duration_ms=milliseconds, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                total_tokens=total_tokens, success=success, error=error))
        logged_model = model.replace(self._secret, "[凭据已隐藏]") if self._secret else model
        logger.info("AI调用：模型=%s，用途=%s，耗时=%d毫秒，输入=%d，输出=%d，总计=%d，结果=%s",
                    logged_model, kind, milliseconds, prompt_tokens, completion_tokens, total_tokens, "成功" if success else "失败")

    def _run(self, model: str, kind: str, action: Callable[[], _Result], batch_id: int | None = None,
             product_id: int | None = None, *, retries: int | None = None) -> str:
        budget = self.config.retries if retries is None else retries
        for attempt in range(budget + 1):
            failure: _AttemptError | None = None
            outcome = _Result("", {})
            with _LIMITER.slot():
                start = time.perf_counter()
                try:
                    outcome = action()
                except _AttemptError as error:
                    failure = error
                elapsed = max(round((time.perf_counter() - start) * 1000), 0)
                self._record(model, kind, elapsed, failure.usage if failure else outcome.usage,
                             failure is None, failure.message if failure else None, batch_id, product_id)
            if failure is None:
                return outcome.value
            if not failure.retryable or attempt >= budget:
                raise AiCallError(failure.message)
            if self.retry_delay_seconds:
                time.sleep(self.retry_delay_seconds * 2 ** attempt)
        raise AiCallError("AI调用失败")

    def generate(self, image: bytes, fields: list[FieldDefinition], items: list[dict[str, object]] | None = None,
                 filename: str = "", batch_id: int | None = None, product_id: int | None = None) -> str:
        validate_review_image(image)
        prompt = build_prompt(fields, items)
        # filename 专用于假模型，不得进入真实请求。
        content = [{"type": "text", "text": prompt},
                   {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(image).decode("ascii")}}]
        model = self.config.model_vision
        payload: dict[str, object] = {"model": model, "messages": [{"role": "user", "content": content}]}
        return self._run(model, "set" if items is not None else "item", lambda: self._chat(model, payload), batch_id, product_id)

    def test_models(self) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for kind, model in (("vision", self.config.model_vision), ("text", self.config.model_text), ("embed", self.config.model_embed)):
            payload: dict[str, object] = {"model": model}
            if kind == "embed":
                payload["input"] = "连接测试"
                action = lambda model=model, payload=payload: self._embedding_test(model, payload)
            else:
                content: object = "仅回复好"
                if kind == "vision":
                    content = [{"type": "text", "text": "仅回复好"}, {"type": "image_url", "image_url": {
                        "url": "data:image/jpeg;base64," + base64.b64encode(_test_image()).decode("ascii")}}]
                payload.update(messages=[{"role": "user", "content": content}], max_tokens=8)
                action = lambda model=model, payload=payload: self._chat(model, payload, diagnostics=True)
            try:
                self._run(model, f"{kind}_test", action, retries=0)
                rows.append({"kind": kind, "model": model, "success": True, "error": None})
            except AiCallError as error:
                rows.append({"kind": kind, "model": model, "success": False, "error": error.message})
        return rows


class FakeAiClient(AiClient):
    """固定的本地假模型，只在内存用文件名选择测试场景。"""
    def __init__(self, config: AiConfig, database: Database, cipher: SecretCipher,
                 transport: httpx.BaseTransport | None = None, retry_delay_seconds: float = 0) -> None:
        super().__init__(config, database, cipher, transport, retry_delay_seconds)

    def generate(self, image: bytes, fields: list[FieldDefinition], items: list[dict[str, object]] | None = None,
                 filename: str = "", batch_id: int | None = None, product_id: int | None = None) -> str:
        validate_review_image(image)
        name = filename.casefold()
        def action() -> _Result:
            if "调用失败" in name or "fail" in name:
                raise _AttemptError("假模型调用失败", retryable=True)
            choices = ("保温杯", "雨伞", "茶具套装")
            category = choices[int(hashlib.sha256(filename.encode()).hexdigest()[:8], 16) % len(choices)]
            defaults = {"category": category, "material": "陶瓷、胡桃木", "color": "中国红、哑光黑", "style": "极简、国潮",
                "festival": "通用", "audience": "企业客户、员工", "usage_scene": "商务社交、日常居家", "size": "约20cm高",
                "craft": "烫金、激光雕刻", "material_name": "双层玻璃杯", "structure": "杯身加杯盖"}
            tags = {field["key"]: defaults.get(field["key"], "演示" + field["label"]) for field in fields}
            if fields and ("无法判断" in name or "uncertain" in name):
                tags[fields[0]["key"]] = "无法判断"
            relations = [item["index"] for item in items or [] if type(item.get("index")) is int]
            if "空关系" in name or "empty_relation" in name:
                relations = []
            result = {"tags": tags, "free_text": "具有清晰外观的商品，适合商务社交和日常居家。",
                "multi_object": "一图多品" in name or "multi" in name, "relation_items": relations}
            return _Result(json.dumps(result, ensure_ascii=False), {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
        return self._run(self.config.model_vision or "fake-vision", "set" if items is not None else "item", action, batch_id, product_id)

    def test_models(self) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for kind, configured in (("vision", self.config.model_vision), ("text", self.config.model_text), ("embed", self.config.model_embed)):
            model = configured or f"fake-{kind}"
            try:
                def action() -> _Result:
                    if "fail" in model.casefold() or "失败" in model:
                        raise _AttemptError("假模型连接测试失败")
                    return _Result("连接成功", {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
                self._run(model, f"{kind}_test", action, retries=0)
                rows.append({"kind": kind, "model": model, "success": True, "error": None})
            except AiCallError as error:
                rows.append({"kind": kind, "model": model, "success": False, "error": error.message})
        return rows
