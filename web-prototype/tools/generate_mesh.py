#!/usr/bin/env python3
"""Generate and download text-to-3D or Tripo multiview meshes.

No third-party Python packages are required. Credentials are read from the
project's .env file or the current process environment.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any


# web-prototype/ (this file is web-prototype/tools/generate_mesh.py); .env lives one level up
PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PROJECT_ROOT.parent
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "visual" / "assets" / "generated"
MESHY_BASE_URL = "https://api.meshy.ai"
TRIPO_BASE_URL = "https://openapi.tripo3d.ai/v3"


class ApiError(RuntimeError):
    """An API request failed or returned an invalid response."""


def load_dotenv(path: Path) -> None:
    """Load a small, dependency-free subset of dotenv syntax."""
    if not path.exists():
        return

    for line_number, raw_line in enumerate(path.read_text().splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ApiError(f"Invalid .env entry on line {line_number}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'\"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def api_key(provider: str) -> str:
    variable = "MESHY_API_KEY" if provider == "meshy" else "TRIPO_API_KEY"
    value = os.environ.get(variable, "").strip()
    if not value:
        raise ApiError(f"{variable} is missing; add it to {REPO_ROOT / '.env'}")
    return value


def request_json(
    url: str,
    *,
    token: str,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    timeout: float = 60,
) -> dict[str, Any]:
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "smash-weights-mesh-cli/1.0",
    }
    if payload is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=payload, headers=headers, method=method)

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw_response = response.read()
    except urllib.error.HTTPError as error:
        raw_error = error.read().decode("utf-8", errors="replace")
        try:
            error_data = json.loads(raw_error)
            detail = (
                error_data.get("message")
                or error_data.get("detail")
                or error_data.get("suggestion")
                or raw_error
            )
        except json.JSONDecodeError:
            detail = raw_error or error.reason
        raise ApiError(f"API returned HTTP {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise ApiError(f"Could not reach API: {error.reason}") from error

    try:
        result = json.loads(raw_response)
    except json.JSONDecodeError as error:
        raise ApiError("API returned a non-JSON response") from error
    if not isinstance(result, dict):
        raise ApiError("API returned an unexpected response shape")
    return result


def upload_tripo_image(path: Path) -> str:
    """Upload one local reference image and return its short-lived Tripo token."""
    path = path.expanduser().resolve()
    if not path.is_file():
        raise ApiError(f"Reference image does not exist: {path}")
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if media_type not in {"image/png", "image/jpeg", "image/webp"}:
        raise ApiError(f"Tripo does not accept this reference type: {path.suffix}")

    boundary = f"----smash-weights-{uuid.uuid4().hex}"
    body = b"".join(
        (
            f"--{boundary}\r\n".encode(),
            (
                f'Content-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
            ).encode(),
            f"Content-Type: {media_type}\r\n\r\n".encode(),
            path.read_bytes(),
            f"\r\n--{boundary}--\r\n".encode(),
        )
    )
    request = urllib.request.Request(
        f"{TRIPO_BASE_URL}/files",
        data=body,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key('tripo')}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "smash-weights-mesh-cli/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            result = json.loads(response.read())
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise ApiError(f"Tripo upload returned HTTP {error.code}: {detail}") from error
    except (urllib.error.URLError, json.JSONDecodeError) as error:
        raise ApiError(f"Could not upload Tripo reference image: {error}") from error
    if not isinstance(result, dict):
        raise ApiError("Tripo upload returned an unexpected response")
    ensure_tripo_success(result)
    token = result.get("data", {}).get("file_token")
    if not isinstance(token, str) or not token:
        raise ApiError("Tripo upload did not return an image token")
    return token


def check_credentials(provider: str) -> dict[str, Any]:
    token = api_key(provider)
    if provider == "meshy":
        response = request_json(f"{MESHY_BASE_URL}/openapi/v1/balance", token=token)
        return {"balance": response.get("balance")}

    response = request_json(f"{TRIPO_BASE_URL}/account/balance", token=token)
    ensure_tripo_success(response)
    data = response.get("data", {})
    return {"balance": data.get("balance"), "frozen": data.get("frozen")}


def ensure_tripo_success(response: dict[str, Any]) -> None:
    code = response.get("code")
    if code == 0:
        return
    message = response.get("message") or "Unknown Tripo API error"
    suggestion = response.get("suggestion")
    if suggestion:
        message = f"{message} ({suggestion})"
    raise ApiError(f"Tripo API error {code}: {message}")


def create_meshy_preview(args: argparse.Namespace) -> str:
    body: dict[str, Any] = {
        "mode": "preview",
        "prompt": args.prompt,
        "target_formats": ["glb"],
    }
    if args.target_polycount is not None:
        body.update(
            {
                "should_remesh": True,
                "target_polycount": args.target_polycount,
                "topology": "triangle",
            }
        )
    response = request_json(
        f"{MESHY_BASE_URL}/openapi/v2/text-to-3d",
        token=api_key("meshy"),
        method="POST",
        body=body,
    )
    task_id = response.get("result")
    if not isinstance(task_id, str) or not task_id:
        raise ApiError("Meshy did not return a task ID")
    return task_id


def create_meshy_refine(preview_task_id: str, args: argparse.Namespace) -> str:
    body: dict[str, Any] = {
        "mode": "refine",
        "preview_task_id": preview_task_id,
        "enable_pbr": True,
        "target_formats": ["glb"],
    }
    if args.texture_prompt:
        body["texture_prompt"] = args.texture_prompt
    response = request_json(
        f"{MESHY_BASE_URL}/openapi/v2/text-to-3d",
        token=api_key("meshy"),
        method="POST",
        body=body,
    )
    task_id = response.get("result")
    if not isinstance(task_id, str) or not task_id:
        raise ApiError("Meshy did not return a refine task ID")
    return task_id


def get_meshy_task(task_id: str) -> dict[str, Any]:
    return request_json(
        f"{MESHY_BASE_URL}/openapi/v2/text-to-3d/{task_id}",
        token=api_key("meshy"),
    )


def create_tripo_task(args: argparse.Namespace) -> str:
    body: dict[str, Any] = {
        "model": args.model_version or "v2.5-20250123",
        "prompt": args.prompt,
        "texture": args.textured,
        "pbr": args.textured,
    }
    if args.negative_prompt:
        body["negative_prompt"] = args.negative_prompt
    if args.target_polycount is not None:
        body["face_limit"] = args.target_polycount
    response = request_json(
        f"{TRIPO_BASE_URL}/generation/text-to-model",
        token=api_key("tripo"),
        method="POST",
        body=body,
    )
    ensure_tripo_success(response)
    task_id = response.get("data", {}).get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise ApiError("Tripo did not return a task ID")
    return task_id


def create_tripo_multiview_task(args: argparse.Namespace, tokens: list[str]) -> str:
    body: dict[str, Any] = {
        "inputs": tokens,
        "texture": args.textured,
        "pbr": args.textured,
    }
    if not (args.model_version or "").startswith("P1-"):
        body["export_uv"] = args.textured
    if args.target_polycount is not None:
        body["face_limit"] = args.target_polycount
        if not (args.model_version or "").startswith("P1-"):
            body["smart_low_poly"] = True
    if args.model_version:
        body["model"] = args.model_version

    response = request_json(
        f"{TRIPO_BASE_URL}/generation/multiview-to-model",
        token=api_key("tripo"),
        method="POST",
        body=body,
    )
    ensure_tripo_success(response)
    task_id = response.get("data", {}).get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise ApiError("Tripo did not return a multiview task ID")
    return task_id


def create_tripo_image_task(args: argparse.Namespace, token: str) -> str:
    body: dict[str, Any] = {
        "input": token,
        "texture": args.textured,
        "pbr": args.textured,
    }
    if not (args.model_version or "").startswith("P1-"):
        body["export_uv"] = args.textured
    if args.target_polycount is not None:
        body["face_limit"] = args.target_polycount
        if not (args.model_version or "").startswith("P1-"):
            body["smart_low_poly"] = True
    if args.model_version:
        body["model"] = args.model_version

    response = request_json(
        f"{TRIPO_BASE_URL}/generation/image-to-model",
        token=api_key("tripo"),
        method="POST",
        body=body,
    )
    ensure_tripo_success(response)
    task_id = response.get("data", {}).get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise ApiError("Tripo did not return an image-to-model task ID")
    return task_id


def get_tripo_task(task_id: str) -> dict[str, Any]:
    response = request_json(
        f"{TRIPO_BASE_URL}/tasks/{task_id}", token=api_key("tripo")
    )
    ensure_tripo_success(response)
    data = response.get("data")
    if not isinstance(data, dict):
        raise ApiError("Tripo returned an invalid task response")
    return data


def task_snapshot(provider: str, task: dict[str, Any]) -> tuple[str, int | None]:
    status = str(task.get("status", "unknown"))
    progress = task.get("progress")
    return status, progress if isinstance(progress, int) else None


def wait_for_task(
    provider: str,
    task_id: str,
    *,
    timeout: float,
    poll_interval: float,
) -> dict[str, Any]:
    getter = get_meshy_task if provider == "meshy" else get_tripo_task
    success_status = "SUCCEEDED" if provider == "meshy" else "success"
    active_statuses = (
        {"PENDING", "IN_PROGRESS"}
        if provider == "meshy"
        else {"queued", "running"}
    )
    deadline = time.monotonic() + timeout
    last_snapshot: tuple[str, int | None] | None = None

    while True:
        task = getter(task_id)
        status, progress = task_snapshot(provider, task)
        snapshot = (status, progress)
        if snapshot != last_snapshot:
            progress_text = f" ({progress}%)" if progress is not None else ""
            print(f"  {status}{progress_text}", flush=True)
            last_snapshot = snapshot

        if status == success_status:
            return task
        if status not in active_statuses:
            error = task.get("error_message") or task.get("task_error") or task.get("error_msg") or "No error detail"
            if isinstance(error, dict):
                error = error.get("message") or json.dumps(error)
            raise ApiError(f"{provider.title()} task {task_id} ended as {status}: {error}")
        if time.monotonic() >= deadline:
            raise ApiError(
                f"Timed out waiting for task {task_id}; rerun the status command later"
            )
        time.sleep(poll_interval)


def task_model_url(provider: str, task: dict[str, Any], textured: bool) -> str:
    if provider == "meshy":
        url = task.get("model_urls", {}).get("glb")
    else:
        output = task.get("output", {})
        candidates = (
            ("model_url", "pbr_model", "model", "base_model")
            if textured
            else ("model_url", "base_model", "model", "pbr_model")
        )
        url = next((output.get(name) for name in candidates if output.get(name)), None)
    if not isinstance(url, str) or not url:
        raise ApiError(f"{provider.title()} task succeeded but returned no model URL")
    return url


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return (slug[:48].rstrip("-") or "mesh")


def output_path(args: argparse.Namespace, task_id: str) -> Path:
    output_dir = args.output_dir.expanduser()
    if not output_dir.is_absolute():
        output_dir = PROJECT_ROOT / output_dir
    prompt = getattr(args, "prompt", "multiview")
    name = args.name or f"{args.provider}-{slugify(prompt)}-{task_id[:8]}"
    if not name.lower().endswith(".glb"):
        name += ".glb"
    return output_dir / Path(name).name


def download_model(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with urllib.request.urlopen(url, timeout=180) as response:
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=f".{destination.name}.", dir=destination.parent, delete=False
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                shutil.copyfileobj(response, temporary_file)
        os.replace(temporary_path, destination)
        destination.chmod(0o644)
    except (urllib.error.URLError, OSError) as error:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise ApiError(f"Could not download model: {error}") from error


def command_check(args: argparse.Namespace) -> int:
    providers = (args.provider,) if args.provider else ("meshy", "tripo")
    failures = 0
    for provider in providers:
        try:
            details = check_credentials(provider)
            balance = details.get("balance")
            extra = f"; balance: {balance}" if balance is not None else ""
            if details.get("frozen"):
                extra += f"; frozen: {details['frozen']}"
            print(f"{provider.title()}: connected{extra}")
        except ApiError as error:
            failures += 1
            print(f"{provider.title()}: {error}", file=sys.stderr)
    return 1 if failures else 0


def command_status(args: argparse.Namespace) -> int:
    task = (
        get_meshy_task(args.task_id)
        if args.provider == "meshy"
        else get_tripo_task(args.task_id)
    )
    status, progress = task_snapshot(args.provider, task)
    progress_text = f" ({progress}%)" if progress is not None else ""
    print(f"{args.provider.title()} task {args.task_id}: {status}{progress_text}")
    return 0


def command_generate(args: argparse.Namespace) -> int:
    print(f"Submitting {args.provider.title()} text-to-3D task...")
    task_id = create_meshy_preview(args) if args.provider == "meshy" else create_tripo_task(args)
    print(f"Task ID: {task_id}")
    if args.no_wait:
        print(
            "Check later with: "
            f"python3 tools/generate_mesh.py status --provider {args.provider} --task-id {task_id}"
        )
        return 0

    task = wait_for_task(
        args.provider,
        task_id,
        timeout=args.timeout,
        poll_interval=args.poll_interval,
    )

    if args.provider == "meshy" and args.textured:
        print("Submitting Meshy texture/refine task...")
        task_id = create_meshy_refine(task_id, args)
        print(f"Refine task ID: {task_id}")
        task = wait_for_task(
            "meshy",
            task_id,
            timeout=args.timeout,
            poll_interval=args.poll_interval,
        )

    destination = output_path(args, task_id)
    print(f"Downloading {destination.name}...")
    download_model(task_model_url(args.provider, task, args.textured), destination)
    try:
        display_path = destination.relative_to(PROJECT_ROOT)
    except ValueError:
        display_path = destination
    print(f"Saved: {display_path}")
    return 0


def command_generate_multiview(args: argparse.Namespace) -> int:
    views = (args.front, args.left, args.back, args.right)
    labels = ("front", "left", "back", "right")
    uploaded: dict[Path, str] = {}
    tokens: list[str] = []
    for label, path in zip(labels, views, strict=True):
        resolved = path.expanduser().resolve()
        if resolved not in uploaded:
            print(f"Uploading {label} reference: {path.name}...", flush=True)
            uploaded[resolved] = upload_tripo_image(resolved)
        else:
            print(f"Reusing symmetric {label} reference: {path.name}...", flush=True)
        tokens.append(uploaded[resolved])

    print("Submitting Tripo multiview-to-3D task...", flush=True)
    task_id = create_tripo_multiview_task(args, tokens)
    print(f"Task ID: {task_id}")
    if args.no_wait:
        print(
            "Check later with: "
            f"python3 tools/generate_mesh.py status --provider tripo --task-id {task_id}"
        )
        return 0

    task = wait_for_task(
        "tripo", task_id, timeout=args.timeout, poll_interval=args.poll_interval
    )
    destination = output_path(args, task_id)
    print(f"Downloading {destination.name}...")
    download_model(task_model_url("tripo", task, args.textured), destination)
    try:
        display_path = destination.relative_to(PROJECT_ROOT)
    except ValueError:
        display_path = destination
    print(f"Saved: {display_path}")
    return 0


def command_generate_image(args: argparse.Namespace) -> int:
    print(f"Uploading image reference: {args.image.name}...", flush=True)
    token = upload_tripo_image(args.image)
    print("Submitting Tripo image-to-3D task...", flush=True)
    task_id = create_tripo_image_task(args, token)
    print(f"Task ID: {task_id}")
    if args.no_wait:
        print(
            "Check later with: "
            f"python3 tools/generate_mesh.py status --provider tripo --task-id {task_id}"
        )
        return 0

    task = wait_for_task(
        "tripo", task_id, timeout=args.timeout, poll_interval=args.poll_interval
    )
    destination = output_path(args, task_id)
    print(f"Downloading {destination.name}...")
    download_model(task_model_url("tripo", task, args.textured), destination)
    try:
        display_path = destination.relative_to(PROJECT_ROOT)
    except ValueError:
        display_path = destination
    print(f"Saved: {display_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate text or multiview GLB meshes with Meshy or Tripo.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    check_parser = subparsers.add_parser(
        "check", help="validate credentials without spending generation credits"
    )
    check_parser.add_argument("--provider", choices=("meshy", "tripo"))
    check_parser.set_defaults(handler=command_check)

    status_parser = subparsers.add_parser("status", help="check an existing task")
    status_parser.add_argument("--provider", choices=("meshy", "tripo"), required=True)
    status_parser.add_argument("--task-id", required=True)
    status_parser.set_defaults(handler=command_status)

    generate_parser = subparsers.add_parser(
        "generate", help="submit, poll, and download a text-to-3D task"
    )
    generate_parser.add_argument("--provider", choices=("meshy", "tripo"), required=True)
    generate_parser.add_argument("--prompt", required=True, help="description of the model")
    generate_parser.add_argument(
        "--negative-prompt", help="Tripo-only features to avoid in the output"
    )
    generate_parser.add_argument(
        "--textured", action="store_true", help="also generate textures and PBR maps"
    )
    generate_parser.add_argument(
        "--texture-prompt", help="Meshy-only guidance for the texture/refine stage"
    )
    generate_parser.add_argument(
        "--target-polycount", type=int, help="requested face count"
    )
    generate_parser.add_argument(
        "--model-version", help="Tripo model version; defaults to v2.5-20250123"
    )
    generate_parser.add_argument(
        "--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="download directory"
    )
    generate_parser.add_argument("--name", help="output filename (GLB)")
    generate_parser.add_argument(
        "--no-wait", action="store_true", help="submit the task and return immediately"
    )
    generate_parser.add_argument(
        "--timeout", type=float, default=1200, help="maximum polling time in seconds"
    )
    generate_parser.add_argument(
        "--poll-interval", type=float, default=5, help="seconds between status requests"
    )
    generate_parser.set_defaults(handler=command_generate)

    image_parser = subparsers.add_parser(
        "generate-image", help="generate a Tripo GLB from one image reference"
    )
    image_parser.add_argument("--image", type=Path, required=True)
    image_parser.add_argument(
        "--textured", action="store_true", help="also generate textures and PBR maps"
    )
    image_parser.add_argument(
        "--target-polycount", type=int, help="requested face count"
    )
    image_parser.add_argument(
        "--model-version", default="P1-20260311", help="Tripo model version"
    )
    image_parser.add_argument(
        "--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="download directory"
    )
    image_parser.add_argument("--name", help="output filename (GLB)")
    image_parser.add_argument(
        "--no-wait", action="store_true", help="submit the task and return immediately"
    )
    image_parser.add_argument(
        "--timeout", type=float, default=1200, help="maximum polling time in seconds"
    )
    image_parser.add_argument(
        "--poll-interval", type=float, default=5, help="seconds between status requests"
    )
    image_parser.set_defaults(provider="tripo", handler=command_generate_image)

    multiview_parser = subparsers.add_parser(
        "generate-multiview",
        help="generate a Tripo GLB from ordered front/left/back/right references",
    )
    for view in ("front", "left", "back", "right"):
        multiview_parser.add_argument(f"--{view}", type=Path, required=True)
    multiview_parser.add_argument(
        "--textured", action="store_true", help="also generate textures and PBR maps"
    )
    multiview_parser.add_argument(
        "--target-polycount", type=int, help="requested face count"
    )
    multiview_parser.add_argument(
        "--model-version", default="P1-20260311", help="Tripo model version"
    )
    multiview_parser.add_argument(
        "--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="download directory"
    )
    multiview_parser.add_argument("--name", help="output filename (GLB)")
    multiview_parser.add_argument(
        "--no-wait", action="store_true", help="submit the task and return immediately"
    )
    multiview_parser.add_argument(
        "--timeout", type=float, default=1200, help="maximum polling time in seconds"
    )
    multiview_parser.add_argument(
        "--poll-interval", type=float, default=5, help="seconds between status requests"
    )
    multiview_parser.set_defaults(provider="tripo", handler=command_generate_multiview)
    return parser


def validate_args(args: argparse.Namespace) -> None:
    if args.command not in {"generate", "generate-image", "generate-multiview"}:
        return
    if args.command in {"generate-image", "generate-multiview"}:
        if args.target_polycount is not None and args.target_polycount <= 0:
            raise ApiError("--target-polycount must be greater than zero")
        if args.poll_interval <= 0 or args.timeout <= 0:
            raise ApiError("--poll-interval and --timeout must be greater than zero")
        return
    if args.provider != "tripo" and (args.negative_prompt or args.model_version):
        raise ApiError("--negative-prompt and --model-version are Tripo-only options")
    if args.provider != "meshy" and args.texture_prompt:
        raise ApiError("--texture-prompt is a Meshy-only option")
    if args.texture_prompt and not args.textured:
        raise ApiError("--texture-prompt requires --textured")
    prompt_limit = 600 if args.provider == "meshy" else 1024
    if len(args.prompt) > prompt_limit:
        raise ApiError(f"Prompt exceeds {args.provider.title()}'s {prompt_limit}-character limit")
    if args.negative_prompt and len(args.negative_prompt) > 255:
        raise ApiError("Tripo negative prompt exceeds the 255-character limit")
    if args.target_polycount is not None and args.target_polycount <= 0:
        raise ApiError("--target-polycount must be greater than zero")
    if args.poll_interval <= 0 or args.timeout <= 0:
        raise ApiError("--poll-interval and --timeout must be greater than zero")


def main() -> int:
    try:
        load_dotenv(REPO_ROOT / ".env")
        args = build_parser().parse_args()
        validate_args(args)
        return args.handler(args)
    except KeyboardInterrupt:
        print("Interrupted; the remote task may still be running.", file=sys.stderr)
        return 130
    except ApiError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
