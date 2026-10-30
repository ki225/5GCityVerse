"""OpenAI Responses API text adapter for the on-prem backend."""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request

ALLOWED_MODEL = "gpt-5.5"


class OpenAIError(RuntimeError):
    """A configuration, transport, or generation failure without secret details."""


class StructuredOutputError(OpenAIError):
    """Bounded per-response evidence, without model text, identities or prompts."""
    def __init__(self,category,*,response_id=None,schema_sha256=None,field_path=None):
        codes={'refusal':'model_refused','incomplete':'model_response_incomplete',
            'schema':'model_response_schema_invalid','transport':'model_transport_unavailable'}
        if category not in codes:raise ValueError('Unknown structured model failure')
        self.category=category;self.code=codes[category]
        super().__init__(self.code)
        self.safe_evidence={'code':self.code,'category':category,'stage':'response'}
        if isinstance(response_id,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}',response_id):
            self.safe_evidence['response_id']=response_id
        if isinstance(schema_sha256,str) and re.fullmatch('[0-9a-f]{64}',schema_sha256):
            self.safe_evidence['schema_sha256']=schema_sha256
        if isinstance(field_path,str) and re.fullmatch(r'root(?:\.(?:field|item)\[[0-9]+\])*',field_path):
            self.safe_evidence['field_path']=field_path[:256]


class OpenAIClient:
    API_URL = "https://api.openai.com/v1/responses"
    ALLOWED_MODEL = ALLOWED_MODEL

    def __init__(self, api_key: str, model: str, timeout: float = 45,
                 max_output_tokens: int = 2048, max_retries: int = 1,
                 retry_backoff_seconds: float = 1, reasoning_effort: str = "medium"):
        if not api_key.strip() or any(c in api_key for c in "\r\n"):
            raise OpenAIError("OPENAI_API_KEY is required and must be a single line")
        if model != self.ALLOWED_MODEL:
            raise OpenAIError("Only OPENAI_MODEL=gpt-5.5 is allowed")
        if not math.isfinite(timeout) or timeout <= 0:
            raise OpenAIError("OPENAI_TIMEOUT_SECONDS must be finite and greater than zero")
        if not isinstance(max_output_tokens, int) or max_output_tokens <= 0:
            raise OpenAIError("OPENAI_MAX_OUTPUT_TOKENS must be a positive integer")
        if not isinstance(max_retries, int) or max_retries < 0 or max_retries > 3:
            raise OpenAIError("OPENAI_MAX_RETRIES must be an integer from zero through three")
        if not math.isfinite(retry_backoff_seconds) or retry_backoff_seconds < 0 or retry_backoff_seconds > 10:
            raise OpenAIError("OPENAI_RETRY_BACKOFF_SECONDS must be finite from zero through ten")
        if reasoning_effort not in {"none", "low", "medium", "high", "xhigh"}:
            raise OpenAIError("Unsupported GPT-5.5 reasoning effort")
        self._api_key = api_key.strip()
        self.model = model
        self.timeout = timeout
        self.max_output_tokens = max_output_tokens
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self.reasoning_effort = reasoning_effort

    @classmethod
    def from_env(cls):
        if os.environ.get("AI_PROVIDER", "openai") != "openai":
            raise OpenAIError("This adapter requires AI_PROVIDER=openai")
        try:
            timeout = float(os.environ.get("OPENAI_TIMEOUT_SECONDS", "45"))
            tokens = int(os.environ.get("OPENAI_MAX_OUTPUT_TOKENS", "4096"))
            retries = int(os.environ.get("OPENAI_MAX_RETRIES", "1"))
            backoff = float(os.environ.get("OPENAI_RETRY_BACKOFF_SECONDS", "1"))
        except ValueError:
            raise OpenAIError("Invalid OpenAI timeout or output-token configuration") from None
        return cls(
            os.environ.get("OPENAI_API_KEY", ""),
            os.environ.get("OPENAI_MODEL", cls.ALLOWED_MODEL),
            timeout,
            tokens,
            retries,
            backoff,
        )

    def generate_text(self, prompt: str, system_instruction: str = "") -> str:
        return self.generate_text_with_metadata(prompt, system_instruction)[0]

    def generate_structured_with_metadata(self,prompt,system_instruction,*,name,schema):
        """Actual Responses strict JSON-schema request; never falls back to text."""
        from backend.onprem.intents.model_contracts import strict_schema,schema_hash,validate,SchemaMismatch
        strict_schema(schema);digest=schema_hash(schema)
        if not isinstance(prompt,str) or not prompt.strip() or not re.fullmatch('[A-Za-z0-9_-]{1,64}',name):
            raise OpenAIError('Invalid structured request')
        payload={'model':self.model,'store':False,'input':prompt,'instructions':system_instruction,
            'max_output_tokens':self.max_output_tokens,'reasoning':{'effort':self.reasoning_effort},
            'text':{'format':{'type':'json_schema','name':name,'strict':True,'schema':schema}}}
        request=urllib.request.Request(self.API_URL,data=json.dumps(payload,allow_nan=False).encode(),
            headers={'Authorization':'Bearer '+self._api_key,'Content-Type':'application/json'},method='POST')
        try:raw=self._send_with_retry(request)
        except Exception:raise StructuredOutputError('transport',schema_sha256=digest) from None
        response_id=None
        try:
            if len(raw)>2*1024*1024:raise ValueError
            body=json.loads(raw)
            response_id=body.get('id')
            if body.get('status')=='incomplete':
                raise StructuredOutputError('incomplete',response_id=response_id,schema_sha256=digest)
            if any(part.get('type')=='refusal' for item in body.get('output',[]) if isinstance(item,dict)
                   for part in item.get('content',[]) if isinstance(part,dict)):
                raise StructuredOutputError('refusal',response_id=response_id,schema_sha256=digest)
            if body.get('status')!='completed' or not isinstance(response_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',response_id):
                raise ValueError
            texts=[part['text'] for item in body.get('output',[]) if item.get('type')=='message'
                for part in item.get('content',[]) if part.get('type')=='output_text' and isinstance(part.get('text'),str)]
            if len(texts)!=1:raise ValueError
            value=json.loads(texts[0],parse_constant=lambda _:(_ for _ in ()).throw(ValueError()))
            validate(value,schema)
        except StructuredOutputError:raise
        except SchemaMismatch as error:
            raise StructuredOutputError('schema',response_id=response_id,schema_sha256=digest,field_path=error.path) from None
        except Exception:
            raise StructuredOutputError('schema',response_id=response_id,schema_sha256=digest) from None
        return value,{'response_id':response_id,'output_format':'json_schema','strict':True,'schema_sha256':digest}

    def generate_text_with_metadata(self, prompt: str, system_instruction: str = "") -> tuple[str, dict]:
        """Return this response's bounded identity without mutable shared state."""
        if not isinstance(prompt, str) or not prompt.strip():
            raise OpenAIError("A non-empty text prompt is required")
        payload = {"model": self.model, "input": prompt, "max_output_tokens": self.max_output_tokens,
                   "reasoning": {"effort": self.reasoning_effort}}
        if system_instruction:
            payload["instructions"] = system_instruction
        request = urllib.request.Request(
            self.API_URL,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        raw = self._send_with_retry(request)
        if len(raw) > 2 * 1024 * 1024:
            raise OpenAIError("OpenAI response exceeded the supported size")
        try:
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError
            if body.get("status") not in {None, "completed"}:
                raise OpenAIError("OpenAI did not return a complete answer")
            response_id = body.get("id")
            metadata = {}
            if isinstance(response_id, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', response_id):
                metadata = {'response_id': response_id}
            text = "".join(
                part["text"]
                for item in body.get("output", [])
                if isinstance(item, dict) and item.get("type") == "message"
                for part in item.get("content", [])
                if isinstance(part, dict) and part.get("type") == "output_text"
                and isinstance(part.get("text"), str)
            )
        except (ValueError, TypeError, KeyError, AttributeError):
            raise OpenAIError("OpenAI returned an invalid response") from None
        if not text.strip():
            raise OpenAIError("OpenAI returned no answer text")
        return text, metadata

    def _send_with_retry(self, request: urllib.request.Request) -> bytes:
        for attempt in range(self.max_retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return response.read(2 * 1024 * 1024 + 1)
            except urllib.error.HTTPError as exc:
                retryable = exc.code in {408, 409, 429} or 500 <= exc.code <= 599
                if not retryable or attempt >= self.max_retries:
                    raise OpenAIError(f"OpenAI API returned HTTP {exc.code}") from None
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt >= self.max_retries:
                    raise OpenAIError("OpenAI API connection failed or timed out") from None
            if self.retry_backoff_seconds:
                time.sleep(self.retry_backoff_seconds * (attempt + 1))
        raise OpenAIError("OpenAI API connection failed or timed out")


def main():
    parser = argparse.ArgumentParser(description="Call OpenAI using server-side environment credentials")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--prompt")
    source.add_argument("--prompt-file", type=Path)
    args = parser.parse_args()
    try:
        prompt = args.prompt if args.prompt_file is None else args.prompt_file.read_text(encoding="utf-8")
        print(OpenAIClient.from_env().generate_text(prompt))
    except (OpenAIError, OSError) as exc:
        print(f"OpenAI request failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
