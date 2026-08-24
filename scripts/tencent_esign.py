#!/usr/bin/env python3
"""Minimal Tencent E-Sign hand-off client for the LazyMind contract skill.

Only four operations are exposed: check credentials, upload one PDF, create a
Tencent E-Sign mini-program preparation URL, or perform upload and preparation
in one hand-off. A token may come from the current chat through ``--token`` or
from ``ESIGN_TOKEN``. The script does not sign, stamp, approve, cancel, or
persist credentials.

Adapted from Tencent's MIT-licensed ``tencent_esign.py`` distributed with the
Contract Legal Expert skill. See ``LICENSE.tencent-esign`` in this skill.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, NoReturn, Optional, Tuple


API_URL = "https://appgw.ess.tencent.cn/plugin/openapi/"
UPLOAD_URL = "https://file.ess.tencent.cn/upload/"
API_VERSION = "2020-11-11"
SKILL_VERSION = "v1.0.0"
TOKEN_ENV = "ESIGN_TOKEN"
REQUEST_TIMEOUT_SECONDS = 22
MAX_PDF_BYTES = 10 * 1024 * 1024
RESOURCE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")


class EsignClientError(Exception):
    """Structured error that can retain upload context across a hand-off."""

    def __init__(self, message: str, *, code: str, exit_code: int = 1, **details: Any):
        super().__init__(message)
        self.message = message
        self.code = code
        self.exit_code = exit_code
        self.details = details

    def add_details(self, **details: Any) -> "EsignClientError":
        for key, value in details.items():
            self.details.setdefault(key, value)
        return self


def emit(payload: Dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def stop(message: str, *, code: str, exit_code: int = 1, **details: Any) -> NoReturn:
    raise EsignClientError(message, code=code, exit_code=exit_code, **details)


def emit_error(error: EsignClientError) -> None:
    payload: Dict[str, Any] = {
        "success": False,
        "error": {"code": error.code, "message": error.message},
    }
    if error.details:
        payload["error"]["details"] = error.details
    emit(payload)


def get_token(explicit_token: Optional[str] = None) -> str:
    token = str(explicit_token or "").strip()
    return token or os.environ.get(TOKEN_ENV, "").strip()


def require_token(explicit_token: Optional[str] = None) -> str:
    token = get_token(explicit_token)
    if not token:
        stop(
            "腾讯电子签凭证未提供。请在当前聊天中粘贴 Token，或由部署管理员设置 ESIGN_TOKEN。",
            code="credential_missing",
            exit_code=2,
        )
    return token


def parse_response(raw: bytes, status: int) -> Dict[str, Any]:
    text = raw.decode("utf-8", errors="replace") if raw else ""
    if not text.strip():
        if 200 <= status < 300:
            return {"Response": {}}
        return {
            "Response": {
                "Error": {
                    "Code": "HTTPError",
                    "Message": f"HTTP {status} returned an empty response",
                }
            }
        }
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {
            "Response": {
                "Error": {
                    "Code": "InvalidResponse",
                    "Message": f"HTTP {status} returned non-JSON content",
                }
            }
        }
    if isinstance(parsed, dict):
        return parsed
    return {
        "Response": {
            "Error": {
                "Code": "InvalidResponse",
                "Message": "Tencent E-Sign returned a non-object JSON response",
            }
        }
    }


def post(url: str, body: bytes, headers: Dict[str, str]) -> Tuple[int, Dict[str, Any]]:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            status = int(getattr(response, "status", 200))
            return status, parse_response(response.read(), status)
    except urllib.error.HTTPError as exc:
        return int(exc.code), parse_response(exc.read(), int(exc.code))
    except urllib.error.URLError as exc:
        reason = str(getattr(exc, "reason", exc))
        stop(
            "无法连接腾讯电子签服务。",
            code="network_error",
            reason=reason,
        )
    except TimeoutError:
        stop(
            "连接腾讯电子签服务超时。",
            code="network_timeout",
            timeout_seconds=REQUEST_TIMEOUT_SECONDS,
        )


def ensure_success(status: int, result: Dict[str, Any]) -> Dict[str, Any]:
    response = result.get("Response")
    error = response.get("Error") if isinstance(response, dict) else None
    if status < 200 or status >= 300 or isinstance(error, dict):
        error_code = "HTTPError"
        error_message = f"腾讯电子签请求失败（HTTP {status}）。"
        request_id = ""
        if isinstance(response, dict):
            request_id = str(response.get("RequestId") or "")
        if isinstance(error, dict):
            error_code = str(error.get("Code") or error_code)
            error_message = str(error.get("Message") or error_message)
        stop(
            error_message,
            code=error_code,
            http_status=status,
            request_id=request_id,
        )
    return result


def call_api(
    action: str,
    params: Dict[str, Any],
    token: Optional[str] = None,
) -> Dict[str, Any]:
    if action != "CreateMiniAppPrepareFlow":
        stop("接口不在电子签白名单中。", code="action_not_allowed", action=action)
    token = require_token(token)
    payload = dict(params)
    payload["Action"] = action
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    status, result = post(
        API_URL,
        body,
        {
            "Authorization": token,
            "Content-Type": "application/json",
            "X-Skill-Version": SKILL_VERSION,
            "X-Tc-Action": action,
            "X-Tc-Version": API_VERSION,
        },
    )
    return ensure_success(status, result)


def validate_pdf(file_path: str) -> Path:
    path = Path(file_path).expanduser().resolve()
    if not path.is_file():
        stop("待上传文件不存在。", code="file_missing", path=str(path))
    if path.suffix.lower() != ".pdf":
        stop("电子签转交当前只接受 PDF。", code="file_type_not_allowed", path=str(path))
    try:
        size = path.stat().st_size
    except OSError as exc:
        stop("无法读取待上传文件信息。", code="file_stat_failed", reason=str(exc))
    if size <= 0:
        stop("PDF 文件为空。", code="file_empty", path=str(path))
    if size > MAX_PDF_BYTES:
        stop(
            "PDF 超过当前 LazyMind 电子签转交的大小限制。",
            code="file_too_large",
            size_bytes=size,
            max_bytes=MAX_PDF_BYTES,
        )
    try:
        with path.open("rb") as handle:
            magic = handle.read(5)
    except OSError as exc:
        stop("无法读取 PDF。", code="file_read_failed", reason=str(exc))
    if magic != b"%PDF-":
        stop("文件扩展名为 PDF，但文件内容不是有效 PDF。", code="invalid_pdf")
    return path


def multipart_pdf(path: Path) -> Tuple[bytes, str]:
    boundary = "----LazyMindEsign" + uuid.uuid4().hex
    safe_name = path.name.replace('"', "_").replace("\r", "_").replace("\n", "_")
    with path.open("rb") as handle:
        pdf = handle.read()
    chunks = [
        f"--{boundary}\r\n".encode("ascii"),
        b'Content-Disposition: form-data; name="business_type"\r\n\r\n',
        b"DOCUMENT\r\n",
        f"--{boundary}\r\n".encode("ascii"),
        (
            'Content-Disposition: form-data; name="file"; '
            f'filename="{safe_name}"\r\n'
        ).encode("utf-8"),
        b"Content-Type: application/pdf\r\n\r\n",
        pdf,
        b"\r\n",
        f"--{boundary}--\r\n".encode("ascii"),
    ]
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def upload_pdf(file_path: str, token: Optional[str] = None) -> Dict[str, Any]:
    token = require_token(token)
    path = validate_pdf(file_path)
    try:
        body, content_type = multipart_pdf(path)
    except OSError as exc:
        stop("读取 PDF 失败。", code="file_read_failed", reason=str(exc))
    status, result = post(
        UPLOAD_URL,
        body,
        {
            "AccessToken": token,
            "Content-Type": content_type,
        },
    )
    result = ensure_success(status, result)
    response = result.get("Response")
    resource_id = response.get("ResourceId") if isinstance(response, dict) else None
    if not resource_id:
        stop(
            "文件上传响应缺少 ResourceId，未生成电子签资源。",
            code="resource_id_missing",
            http_status=status,
        )
    return result


def prepare_link(
    resource_id: str,
    flow_name: str,
    token: Optional[str] = None,
) -> Dict[str, Any]:
    resource_id = resource_id.strip()
    flow_name = flow_name.strip()
    if not RESOURCE_ID_RE.fullmatch(resource_id):
        stop("ResourceId 格式无效。", code="invalid_resource_id")
    if not flow_name or len(flow_name) > 200 or any(ord(ch) < 32 for ch in flow_name):
        stop("合同文件名为空、过长或含控制字符。", code="invalid_flow_name")
    result = call_api(
        "CreateMiniAppPrepareFlow",
        {"ResourceId": resource_id, "FlowName": flow_name},
        token=token,
    )
    response = result.get("Response")
    long_url = response.get("LongUrl") if isinstance(response, dict) else None
    if not isinstance(long_url, str) or not long_url.startswith("https://"):
        stop(
            "电子签响应未返回有效的 HTTPS 发起链接。",
            code="prepare_url_missing",
        )
    return result


def handoff_pdf(
    file_path: str,
    flow_name: str,
    token: Optional[str] = None,
) -> Dict[str, Any]:
    """Upload a PDF and create its preparation URL in one process."""
    resolved_token = require_token(token)
    upload_result = upload_pdf(file_path, token=resolved_token)
    upload_response = upload_result.get("Response")
    resource_id = upload_response.get("ResourceId") if isinstance(upload_response, dict) else None
    if not isinstance(resource_id, str) or not resource_id:
        stop("文件上传响应缺少 ResourceId。", code="resource_id_missing")

    try:
        prepare_result = prepare_link(resource_id, flow_name, token=resolved_token)
    except EsignClientError as exc:
        raise exc.add_details(stage="prepare", resource_id=resource_id)

    prepare_response = prepare_result.get("Response")
    long_url = prepare_response.get("LongUrl") if isinstance(prepare_response, dict) else None
    return {
        "success": True,
        "Response": {
            "ResourceId": resource_id,
            "LongUrl": long_url,
            "UploadRequestId": (
                upload_response.get("RequestId") if isinstance(upload_response, dict) else None
            ),
            "PrepareRequestId": (
                prepare_response.get("RequestId") if isinstance(prepare_response, dict) else None
            ),
        },
    }


def add_token_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--token",
        help="用户在当前聊天中提供的腾讯电子签 Token；省略时读取 ESIGN_TOKEN",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LazyMind 腾讯电子签人工确认式转交工具")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("version", help="显示脚本版本")
    auth_check = subparsers.add_parser("auth-check", help="检查当前调用或部署环境是否提供凭证")
    add_token_argument(auth_check)

    upload = subparsers.add_parser("upload", help="上传一份已确认的 PDF")
    upload.add_argument("--file", required=True, help="PDF 的真实绝对路径")
    add_token_argument(upload)

    prepare = subparsers.add_parser("prepare", help="生成腾讯电子签小程序发起入口")
    prepare.add_argument("--resource-id", required=True, help="上传返回的 ResourceId")
    prepare.add_argument("--flow-name", required=True, help="合同原始文件名")
    add_token_argument(prepare)

    handoff = subparsers.add_parser("handoff", help="一次完成 PDF 上传和发起入口生成")
    handoff.add_argument("--file", required=True, help="PDF 的真实绝对路径")
    handoff.add_argument("--flow-name", required=True, help="合同原始文件名")
    add_token_argument(handoff)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "version":
        emit(
            {
                "success": True,
                "name": "lazymind-tencent-esign-handoff",
                "version": "1.1.0",
                "operations": ["auth-check", "upload", "prepare", "handoff"],
            }
        )
        return
    if args.command == "auth-check":
        if not get_token(args.token):
            stop(
                "腾讯电子签凭证未提供。请在当前聊天中粘贴 Token，或由部署管理员设置 ESIGN_TOKEN。",
                code="credential_missing",
                exit_code=2,
            )
        emit({"success": True, "credential": "configured"})
        return
    if args.command == "upload":
        emit(upload_pdf(args.file, token=args.token))
        return
    if args.command == "prepare":
        emit(prepare_link(args.resource_id, args.flow_name, token=args.token))
        return
    if args.command == "handoff":
        emit(handoff_pdf(args.file, args.flow_name, token=args.token))
        return
    stop("未知命令。", code="unknown_command")


if __name__ == "__main__":
    try:
        main()
    except EsignClientError as exc:
        emit_error(exc)
        raise SystemExit(exc.exit_code)
    except KeyboardInterrupt:
        error = EsignClientError("操作已取消。", code="cancelled", exit_code=130)
        emit_error(error)
        raise SystemExit(error.exit_code)
