"""百炼 OpenAI 兼容调用；不记录提示词、图片、API Key 或 NAS 信息。"""
from __future__ import annotations

import base64
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
from io import BytesIO
from importlib.resources import files
import json
import logging
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

_TEST_TOKENS = 256
_SEARCH_TOKENS = {"query": 2048, "rank": 3072}
_OMNI_TEXT_MODEL = "qwen3.8-omni-flash"
_PROVIDER_ERRORS = {
    "AccessDenied": "账号无调用权限",
    "InvalidApiKey": "API Key 无效",
    "invalid_api_key": "API Key 无效",
    "InvalidParameter": "请求参数不符合模型要求",
    "invalid_request_error": "请求参数不符合模型要求",
    "model_not_found": "模型不存在或未开通",
    "ModelNotFound": "模型不存在或未开通",
    "DataInspectionFailed": "请求未通过模型内容检查",
    "Arrearage": "服务账户欠费",
    "Throttling": "服务限流",
    "rate_limit_exceeded": "服务限流",
}


_search_usage: ContextVar[list[dict[str, object]] | None] = ContextVar("search_usage", default=None)


@contextmanager
def collect_search_usage() -> Iterator[list[dict[str, object]]]:
    calls: list[dict[str, object]] = []
    token = _search_usage.set(calls)
    try:
        yield calls
    finally:
        _search_usage.reset(token)


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
    with Image.new("RGB", (128, 128), "blue") as image:
        image.save(stream, format="JPEG")
    return stream.getvalue()


def _usage(body: object) -> dict[str, object]:
    candidate = body.get("usage", {}) if isinstance(body, dict) else {}
    return candidate if isinstance(candidate, dict) else {}


def _text_budget(model: str, tokens: int) -> dict[str, object]:
    if model.strip().casefold() in ("qwen4-max", "qwen4-max-preview"):
        return {"enable_thinking": False, "max_completion_tokens": tokens}
    return {"max_tokens": tokens}


def _provider_hint(body: object) -> str:
    if not isinstance(body, dict):
        return ""
    error = body.get("error")
    fields = error if isinstance(error, dict) else body
    for field in ("code", "type"):
        code = fields.get(field)
        if isinstance(code, str) and code in _PROVIDER_ERRORS:
            return f"（{code}，{_PROVIDER_ERRORS[code]}）"
    return ""


def _check_model_error(body: dict[str, object], *, diagnostics: bool) -> None:
    if body.get("error") is not None or body.get("success") is False or body.get("code") not in (
        None, "", 0, "0", 200, "200", "OK", "Success"
    ):
        raise _AttemptError("AI模型返回错误" + (_provider_hint(body) if diagnostics else ""), usage=_usage(body))


def _text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part["text"] for part in content
                       if isinstance(part, dict) and part.get("type", "text") in ("text", "output_text")
                       and isinstance(part.get("text"), str))
    return ""


def _finish(reason: object, usage: dict[str, object]) -> None:
    if reason == "length":
        raise _AttemptError("AI输出截断（length），请检查输出 Token 预算", usage=usage)
    if reason == "content_filter":
        raise _AttemptError("AI模型返回错误：回答未通过内容审核", usage=usage)
    if reason != "stop":
        raise _AttemptError("AI响应格式不兼容：缺少正常结束的文本回答", retryable=True, usage=usage)


def _final_answer(text: str, has_reasoning: bool, reason: object, usage: dict[str, object]) -> _Result:
    _finish(reason, usage)
    if text.strip() and not text.lstrip().lower().startswith("<think>"):
        return _Result(text, usage)
    message = "AI未返回最终回答：仅收到思考结果" if has_reasoning else "AI响应格式不兼容：未返回最终文本"
    raise _AttemptError(message, retryable=True, usage=usage)


def _sse_events(lines: Iterator[str]) -> Iterator[str]:
    parts: list[str] = []
    for line in lines:
        if line.startswith("data:"):
            parts.append(line[5:].removeprefix(" "))
        elif not line and parts:
            yield "\n".join(parts)
            parts.clear()
    if parts:
        yield "\n".join(parts)


def _stream_answer(response: httpx.Response, *, diagnostics: bool) -> _Result:
    chunks: list[str] = []
    has_reasoning = False
    reason: object = None
    usage: dict[str, object] = {}
    for event in _sse_events(response.iter_lines()):
        if event.strip() == "[DONE]":
            break
        try:
            frame = json.loads(event)
        except ValueError:
            raise _AttemptError("AI响应格式不兼容：流式事件不是有效 JSON", retryable=True, usage=usage) from None
        if not isinstance(frame, dict):
            raise _AttemptError("AI响应格式不兼容：流式事件需要 JSON 对象", retryable=True, usage=usage)
        _check_model_error(frame, diagnostics=diagnostics)
        usage = _usage(frame) or usage
        choices = frame.get("choices")
        if not isinstance(choices, list) or (choices and not isinstance(choices[0], dict)):
            raise _AttemptError("AI响应格式不兼容：缺少流式对话结果", retryable=True, usage=usage)
        if not choices:
            continue
        choice = choices[0]
        delta = choice.get("delta")
        if isinstance(delta, dict):
            if delta.get("refusal"):
                raise _AttemptError("AI模型返回错误：模型拒绝回答", usage=usage)
            chunks.append(_text(delta.get("content")))
            has_reasoning |= bool(delta.get("reasoning_content"))
        if choice.get("finish_reason") is not None:
            reason = choice["finish_reason"]
    return _final_answer("".join(chunks), has_reasoning, reason, usage)


def _embedding(body: dict[str, object]) -> list[float]:
    data = body.get("data")
    vector = data[0].get("embedding") if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict) else None
    if (not isinstance(vector, list) or not 1 <= len(vector) <= 8192
            or any(type(value) not in (float, int) or not (-1e30 < value < 1e30) for value in vector)
            or not any(vector)):
        raise _AttemptError("AI响应格式不兼容：向量模型未返回有效向量", retryable=True, usage=_usage(body))
    return vector


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

    @contextmanager
    def _request(self, endpoint: str, model: str, payload: dict[str, object]) -> Iterator[httpx.Response]:
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
            with self.http.stream("POST", str(base).rstrip("/") + endpoint, json=payload,
                                  headers={"Authorization": f"Bearer {key}"}) as response:
                yield response
        except httpx.ConnectTimeout:
            raise _AttemptError("AI连接超时，未能建立连接", retryable=True) from None
        except httpx.ConnectError:
            raise _AttemptError("无法建立AI连接，请检查网络或TLS证书", retryable=True) from None
        except httpx.TimeoutException:
            raise _AttemptError("AI请求超时", retryable=True) from None
        except httpx.DecodingError:
            raise _AttemptError("AI响应格式不兼容：无法解码响应", retryable=True) from None
        except httpx.HTTPError:
            raise _AttemptError("AI HTTP 请求失败：请求或响应传输中断", retryable=True) from None

    def _response_json(self, response: httpx.Response, *, diagnostics: bool = False) -> dict[str, object]:
        response.read()
        try:
            body: object = response.json()
        except ValueError:
            body = None
        if not response.is_success:
            message = f"AI接口返回HTTP {response.status_code}"
            if diagnostics:
                message += _provider_hint(body)
            raise _AttemptError(message, response.status_code in (408, 429) or response.status_code >= 500,
                                _usage(body))
        if not isinstance(body, dict):
            raise _AttemptError("AI响应格式不兼容：需要 JSON 对象", retryable=True)
        _check_model_error(body, diagnostics=diagnostics)
        return body

    def _post(self, endpoint: str, model: str, payload: dict[str, object], *, diagnostics: bool = False) -> dict[str, object]:
        with self._request(endpoint, model, payload) as response:
            return self._response_json(response, diagnostics=diagnostics)

    def _chat(self, model: str, payload: dict[str, object], *, diagnostics: bool = False) -> _Result:
        if model.strip().casefold() == _OMNI_TEXT_MODEL:
            payload = {**payload, "stream": True, "modalities": ["text"], "stream_options": {"include_usage": True}}
        with self._request("/chat/completions", model, payload) as response:
            if response.is_success and payload.get("stream") is True and response.headers.get(
                "content-type", ""
            ).split(";", 1)[0].strip().lower() == "text/event-stream":
                return _stream_answer(response, diagnostics=diagnostics)
            body = self._response_json(response, diagnostics=diagnostics)
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise _AttemptError("AI响应格式不兼容：缺少对话结果", retryable=True, usage=_usage(body))
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, dict):
            _finish(choice.get("finish_reason"), _usage(body))
            raise _AttemptError("AI响应格式不兼容：缺少对话消息", retryable=True, usage=_usage(body))
        if message.get("refusal"):
            raise _AttemptError("AI模型返回错误：模型拒绝回答", usage=_usage(body))
        return _final_answer(_text(message.get("content")), bool(message.get("reasoning_content")),
                             choice.get("finish_reason", "stop"), _usage(body))

    def _embedding_test(self, model: str, payload: dict[str, object]) -> _Result:
        body = self._post("/embeddings", model, payload, diagnostics=True)
        _embedding(body)
        if "model" in body and body["model"] != model:
            raise _AttemptError("向量响应的模型与配置不一致", usage=_usage(body))
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
        calls = _search_usage.get()
        if calls is not None:
            calls.append({"kind": kind, "model": model, "input_tokens": prompt_tokens,
                          "output_tokens": completion_tokens, "success": success})
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

    def embed(self, text: str, batch_id: int | None) -> list[float]:
        model = self.config.model_embed.strip()

        def action() -> _Result:
            body = self._post("/embeddings", model, {"model": model, "input": text})
            vector = _embedding(body)
            if body.get("model") != model:
                raise _AttemptError("向量响应的模型与配置不一致")
            return _Result(json.dumps(vector), _usage(body))

        return json.loads(self._run(model, "embed", action, batch_id))

    def search_chat(self, task: str, data: dict[str, object]) -> str:
        path = {"query": "query_understanding.txt", "rank": "ranking.txt"}[task]
        instruction = files("app").joinpath("prompts", path).read_text(encoding="utf-8")
        model = self.config.model_text
        payload = {"model": model, "temperature": 0, **_text_budget(model, _SEARCH_TOKENS[task]), "messages": [
            {"role": "system", "content": instruction},
            {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
        ]}
        return self._run(model, "search_" + task, lambda: self._chat(model, payload))

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
                    content = [{"type": "text", "text": "请用一句话回答图片的主色。"}, {"type": "image_url", "image_url": {
                        "url": "data:image/jpeg;base64," + base64.b64encode(_test_image()).decode("ascii")}}]
                payload.update(_text_budget(model, _TEST_TOKENS))
                if model.strip().casefold() == _OMNI_TEXT_MODEL:
                    payload["reasoning_effort"] = "none"
                payload["messages"] = [{"role": "user", "content": content}]
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
                "festival": "", "audience": "企业客户、员工", "usage_scene": "商务社交、日常居家", "size": "约20cm高",
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

    def embed(self, text: str, batch_id: int | None) -> list[float]:
        model = self.config.model_embed.strip() or "fake-embed"
        def action() -> _Result:
            if "向量失败" in text:
                raise _AttemptError("假向量模型调用失败", retryable=True)
            vector = [byte / 127.5 - 1 for byte in hashlib.sha256(text.encode("utf-8")).digest()]
            return _Result(json.dumps(vector), {"prompt_tokens": 1, "total_tokens": 1})
        return json.loads(self._run(model, "embed", action, batch_id))

    def search_chat(self, task: str, data: dict[str, object]) -> str:
        model = self.config.model_text or "fake-text"

        def action() -> _Result:
            query = str(data["query"])
            if f"假{task}失败" in query or "fail" in model.lower():
                raise _AttemptError("假文本模型调用失败", retryable=False)
            if task == "rank":
                candidates = data["candidates"]
                answer = {"items": [{"index": row["index"], "reason": "匹配已确认的商品属性"} for row in candidates[:10]]}
            else:
                # 假模型只供开发验收，正式字段值由人工填写并由真实模型解释。
                fields = {}
                remaining = query
                for key, variants in {"festival": ["春节", "过年", "新年"], "audience": ["商务客户", "企业客户", "送领导"],
                    "color": ["红色", "中国红", "红"], "material": ["陶瓷", "瓷器"],
                    "style": ["极简", "简约"], "usage_scene": ["商务社交", "日常居家"]}.items():
                    if any(term in query for term in variants):
                        fields[key] = variants
                        for term in variants:
                            remaining = remaining.replace(term, " ")
                answer = {"fields": fields, "keywords": [term for term in remaining.split() if len(term) >= 2][:12], "exclude": []}
            return _Result(json.dumps(answer, ensure_ascii=False), {"prompt_tokens": 8, "completion_tokens": 8})

        return self._run(model, "search_" + task, action)

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
