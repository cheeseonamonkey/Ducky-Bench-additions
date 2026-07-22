#!/usr/bin/env python3
"""Config-first, reproducible candidate runs for Ducky Bench SVG tasks.

This calls an OpenAI-compatible Chat Completions API. It intentionally does not
submit votes or modify the public Ducky Bench leaderboard.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import mimetypes
import os
import re
import shutil
import sys
import time
import tomllib
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlparse
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
USER_AGENT = "ducky-bench-additions/0.1 (+https://github.com/cheeseonamonkey/Ducky-Bench-additions)"
IGNORED_IMAGE_ALTS = {"ducky bench", "model a", "model b"}


class ConfigError(ValueError):
    pass


class RunError(RuntimeError):
    pass


@dataclass(frozen=True)
class Model:
    label: str
    model_id: str
    request: dict[str, Any]
    endpoint: str | None = None
    api_key_env: str | None = None


@dataclass(frozen=True)
class Reference:
    test_id: str
    name: str
    page_url: str
    source_url: str
    local_path: Path
    mime_type: str


class ImageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.images: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "img":
            return
        item = {key.lower(): value or "" for key, value in attrs}
        if item.get("src"):
            self.images.append(item)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def safe_name(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "item"


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_json(path: Path, value: Any) -> None:
    write_text(path, json.dumps(value, indent=2, sort_keys=True, default=str) + "\n")


def load_dotenv(path: Path) -> None:
    """Load simple KEY=VALUE lines without overriding actual environment values."""
    if not path.exists():
        return
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            raise ConfigError(f"{path}:{number}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise ConfigError(f"{path}:{number}: missing key")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def load_config(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"Config not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {path}: {exc}") from exc


def selected_models(config: dict[str, Any], requested: list[str]) -> list[Model]:
    raw_models = config.get("models", [])
    if not isinstance(raw_models, list):
        raise ConfigError("models must be a list of [[models]] blocks")

    models: list[Model] = []
    for index, raw in enumerate(raw_models, start=1):
        if not isinstance(raw, dict):
            raise ConfigError(f"models[{index}] must be a table")
        if not raw.get("enabled", False):
            continue
        label = str(raw.get("label", "")).strip()
        model_id = str(raw.get("id", "")).strip()
        request = raw.get("request", {})
        if not label or not model_id or model_id == "provider/model-id":
            raise ConfigError(f"models[{index}] needs a real label and id before enabling it")
        if not isinstance(request, dict):
            raise ConfigError(f"models[{index}].request must be an inline table")
        endpoint = str(raw.get("endpoint", "")).strip() or None
        api_key_env = str(raw.get("api_key_env", "")).strip() or None
        models.append(Model(label, model_id, dict(request), endpoint, api_key_env))

    if not requested:
        return models
    wanted = set(requested)
    picked = [model for model in models if model.label in wanted or model.model_id in wanted]
    found = {item.label for item in picked} | {item.model_id for item in picked}
    missing = wanted - found
    if missing:
        raise ConfigError(f"No enabled model matches: {', '.join(sorted(missing))}")
    return picked


def test_ids(config: dict[str, Any], requested: list[str]) -> list[str]:
    benchmark = config.get("benchmark")
    if not isinstance(benchmark, dict):
        raise ConfigError("Missing [benchmark] table")
    available = [str(value) for value in benchmark.get("test_ids", [])]
    if not available:
        raise ConfigError("benchmark.test_ids cannot be empty")
    if not requested:
        return available
    unknown = set(requested) - set(available)
    if unknown:
        raise ConfigError(f"Unknown test id(s): {', '.join(sorted(unknown))}")
    return requested


def fetch_bytes(url: str, timeout_seconds: int) -> tuple[bytes, str]:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            return response.read(), response.headers.get_content_type() or "application/octet-stream"
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RunError(f"GET {url} failed with HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RunError(f"GET {url} failed: {exc.reason}") from exc


def vote_url(vote_page: str, test_id: str) -> str:
    separator = "&" if "?" in vote_page else "?"
    return f"{vote_page}{separator}test={quote(test_id)}"


def image_extension(source_url: str, mime_type: str) -> str:
    suffix = Path(urlparse(source_url).path).suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"}:
        return suffix
    return mimetypes.guess_extension(mime_type) or ".bin"


def collect_reference(
    benchmark: dict[str, Any],
    test_id: str,
    run_dir: Path,
    timeout_seconds: int,
) -> Reference:
    page_url = vote_url(str(benchmark["vote_page"]), test_id)
    page_bytes, _ = fetch_bytes(page_url, timeout_seconds)
    parser = ImageParser()
    parser.feed(page_bytes.decode("utf-8", errors="replace"))

    candidates = [
        image
        for image in parser.images
        if image.get("alt", "").strip().lower() not in IGNORED_IMAGE_ALTS
    ]
    if not candidates:
        raise RunError(
            f"Could not find the reference image on {page_url}. "
            "The public benchmark page layout may have changed."
        )
    image = candidates[-1]
    name = image.get("alt", f"test-{test_id}").strip() or f"test-{test_id}"
    source_url = urljoin(page_url, image["src"])
    binary, mime_type = fetch_bytes(source_url, timeout_seconds)
    extension = image_extension(source_url, mime_type)
    local_path = run_dir / "references" / f"test-{test_id}-{safe_name(name)}{extension}"
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_bytes(binary)
    if mime_type == "application/octet-stream":
        mime_type = mimetypes.guess_type(local_path.name)[0] or mime_type
    return Reference(test_id, name, page_url, source_url, local_path, mime_type)


def as_data_url(path: Path, mime_type: str) -> str:
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{data}"


def api_headers(endpoint: str, api_key: str) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
    if urlparse(endpoint).netloc.endswith("openrouter.ai"):
        headers.update(
            {
                "HTTP-Referer": "https://github.com/cheeseonamonkey/Ducky-Bench-additions",
                "X-Title": "Ducky Bench additions",
            }
        )
    return headers


def response_text(response: dict[str, Any]) -> str:
    try:
        content = response["choices"][0]["message"].get("content")
    except (KeyError, IndexError, TypeError) as exc:
        raise RunError("API response did not contain choices[0].message.content") from exc
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        if parts:
            return "\n".join(parts)
    raise RunError("API response content was not text")


def extract_svg(text: str) -> str:
    match = re.search(r"<svg\b[^>]*>.*?</svg\s*>", text, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        raise RunError("Model response did not contain a complete SVG document")
    return match.group(0).strip()


def validate_svg(svg: str) -> None:
    try:
        root = ET.fromstring(svg)
    except ET.ParseError as exc:
        raise RunError(f"SVG is not valid XML: {exc}") from exc
    if root.tag.rsplit("}", 1)[-1].lower() != "svg":
        raise RunError("Root element is not SVG")
    forbidden = {"image", "foreignobject"}
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1].lower() in forbidden:
            raise RunError("SVG contains a forbidden raster or external element")
    if re.search(r"\b(?:href|xlink:href)\s*=\s*['\"](?:https?:|data:)", svg, re.IGNORECASE):
        raise RunError("SVG contains an external or embedded asset reference")


def request_svg(
    endpoint: str,
    api_key: str,
    model: Model,
    reference: Reference,
    prompt: str,
    defaults: dict[str, Any],
    image_detail: str | None,
    timeout_seconds: int,
    retry_attempts: int,
) -> tuple[dict[str, Any], str, float | None]:
    image_url: dict[str, Any] = {"url": as_data_url(reference.local_path, reference.mime_type)}
    if image_detail:
        image_url["detail"] = image_detail
    payload: dict[str, Any] = {
        "model": model.model_id,
        "messages": [
            {
                "role": "system",
                "content": "You are a careful SVG illustrator. Follow the output restrictions exactly.",
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": image_url},
                ],
            },
        ],
    }
    payload.update(defaults)
    payload.update(model.request)
    body = json.dumps(payload).encode("utf-8")

    last_error: Exception | None = None
    for attempt in range(retry_attempts + 1):
        request = Request(endpoint, data=body, headers=api_headers(endpoint, api_key), method="POST")
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                result = json.loads(response.read().decode("utf-8"))
            text = response_text(result)
            cost_raw = result.get("usage", {}).get("cost")
            try:
                cost = float(cost_raw) if cost_raw is not None else None
            except (TypeError, ValueError):
                cost = None
            return result, extract_svg(text), cost
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:800]
            last_error = RunError(f"API HTTP {exc.code}: {detail}")
            retryable = exc.code == 429 or 500 <= exc.code < 600
        except (URLError, TimeoutError, json.JSONDecodeError, RunError) as exc:
            last_error = exc
            retryable = isinstance(exc, (URLError, TimeoutError))
        if not retryable or attempt == retry_attempts:
            break
        time.sleep(min(2**attempt, 8))
    raise RunError(f"Model request failed: {last_error}") from last_error


def relative_to_run(path: Path, run_dir: Path) -> str:
    return path.relative_to(run_dir).as_posix()


def write_review_html(run_dir: Path, manifest: dict[str, Any]) -> None:
    rows: list[str] = []
    for artifact in manifest["artifacts"]:
        if artifact["status"] != "ok":
            continue
        rows.append(
            """
            <section>
              <h2>{label} — {test_name}</h2>
              <div class="comparison">
                <figure><figcaption>Reference</figcaption><img src="{reference}" alt="Reference image"></figure>
                <figure><figcaption>Generated SVG</figcaption><img src="{svg}" alt="Generated SVG"></figure>
              </div>
            </section>
            """.format(
                label=html.escape(artifact["model_label"]),
                test_name=html.escape(artifact["reference_name"]),
                reference=html.escape(artifact["reference_file"]),
                svg=html.escape(artifact["svg_file"]),
            )
        )
    document = """<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ducky Bench candidate review</title>
<style>
body { max-width: 1200px; margin: 2rem auto; padding: 0 1rem; color: #19212b; font: 16px/1.45 system-ui, sans-serif; }
h1 { margin-bottom: .2rem; } .comparison { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 1rem; }
figure { margin: 0; padding: 1rem; border: 1px solid #d6dbe1; border-radius: .6rem; background: #fff; }
figcaption { font-weight: 650; margin-bottom: .7rem; } img { display: block; width: 100%; max-height: 500px; object-fit: contain; background: #f4f6f8; }
@media (max-width: 700px) { .comparison { grid-template-columns: 1fr; } }
</style>
<h1>Ducky Bench candidate review</h1>
<p>Generated locally from the recorded config and public reference images. This is not an official leaderboard score.</p>
""" + "\n".join(rows)
    write_text(run_dir / "review.html", document)


def write_bundle(run_dir: Path) -> Path:
    destination = run_dir / "submission-bundle.zip"
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(run_dir.rglob("*")):
            if path.is_file() and path != destination:
                archive.write(path, path.relative_to(run_dir))
    return destination


def plan(models: list[Model], ids: list[str], config: dict[str, Any]) -> None:
    print(f"Tests: {', '.join(ids)}")
    print(f"Enabled models: {len(models)}")
    for model in models:
        print(f"  - {model.label}: {model.model_id}")
    print(f"Planned requests: {len(models) * len(ids)}")
    cap = config.get("run", {}).get("max_total_cost_usd")
    if cap is not None:
        print(f"Reported-cost cap: USD {float(cap):.2f}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "models.toml", help="TOML config path")
    parser.add_argument("--execute", action="store_true", help="Make API calls; otherwise print a no-cost plan")
    parser.add_argument("--run-name", help="Artifact folder name; defaults to a UTC timestamp")
    parser.add_argument("--resume", action="store_true", help="Reuse valid existing SVGs within --run-name")
    parser.add_argument("--model", action="append", default=[], help="Exact enabled model label or id; repeatable")
    parser.add_argument("--test", action="append", default=[], help="Test id from models.toml; repeatable")
    args = parser.parse_args(argv)

    try:
        load_dotenv(ROOT / ".env")
        config = load_config(args.config)
        run = config.get("run")
        benchmark = config.get("benchmark")
        if not isinstance(run, dict) or not isinstance(benchmark, dict):
            raise ConfigError("Config needs [run] and [benchmark] tables")
        models = selected_models(config, args.model)
        ids = test_ids(config, args.test)
        plan(models, ids, config)

        if not args.execute:
            if not models:
                print("\nNo models are enabled yet. Edit models.toml, then re-run.")
            else:
                print("\nDry run only. Add --execute to make API calls.")
            return 0
        if not models:
            raise ConfigError("Enable at least one model in models.toml before --execute")

        endpoint = str(run.get("endpoint", "")).strip()
        key_env = str(run.get("api_key_env", "")).strip()
        prompt = str(benchmark.get("prompt", "")).strip()
        defaults = run.get("request", {})
        if not endpoint or not key_env or not prompt or not isinstance(defaults, dict):
            raise ConfigError("run.endpoint, run.api_key_env, benchmark.prompt, and run.request are required")
        timeout_seconds = int(run.get("timeout_seconds", 180))
        retry_attempts = int(run.get("retry_attempts", 2))
        image_detail = str(run.get("image_detail", "")).strip() or None
        cost_cap = float(run.get("max_total_cost_usd", 0))

        run_name = args.run_name or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_dir = ROOT / "runs" / safe_name(run_name)
        if run_dir.exists() and not args.resume:
            raise ConfigError(f"{run_dir} already exists; use another --run-name or add --resume")
        run_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(args.config, run_dir / "models.toml")

        references = {
            test_id: collect_reference(benchmark, test_id, run_dir, timeout_seconds) for test_id in ids
        }
        manifest: dict[str, Any] = {
            "schema_version": 1,
            "started_at": utc_now(),
            "run_name": run_name,
            "benchmark": {
                "vote_page": benchmark["vote_page"],
                "prompt": prompt,
                "tests": [
                    {
                        "test_id": ref.test_id,
                        "name": ref.name,
                        "page_url": ref.page_url,
                        "source_url": ref.source_url,
                        "local_file": relative_to_run(ref.local_path, run_dir),
                    }
                    for ref in references.values()
                ],
            },
            "models": [asdict(model) for model in models],
            "artifacts": [],
            "reported_total_cost_usd": 0.0,
        }
        write_json(run_dir / "manifest.json", manifest)

        reported_cost = 0.0
        errors = 0
        stopped = False
        for model_index, model in enumerate(models, start=1):
            model_folder = f"{model_index:02d}-{safe_name(model.label)}"
            model_endpoint = model.endpoint or endpoint
            model_key_env = model.api_key_env or key_env
            api_key = os.environ.get(model_key_env, "").strip()
            if not api_key:
                raise ConfigError(f"{model.label}: missing API key in environment variable {model_key_env}")

            for test_id, reference in references.items():
                if cost_cap > 0 and reported_cost >= cost_cap:
                    print(f"Cost cap reached (USD {reported_cost:.4f}); stopping before {model.label} / test {test_id}.")
                    stopped = True
                    break
                svg_path = run_dir / "outputs" / model_folder / f"test-{test_id}.svg"
                response_path = run_dir / "responses" / model_folder / f"test-{test_id}.json"
                artifact: dict[str, Any] = {
                    "model_label": model.label,
                    "model_id": model.model_id,
                    "test_id": test_id,
                    "reference_name": reference.name,
                    "reference_file": relative_to_run(reference.local_path, run_dir),
                    "reference_url": reference.source_url,
                    "svg_file": relative_to_run(svg_path, run_dir),
                }
                if args.resume and svg_path.exists():
                    try:
                        validate_svg(svg_path.read_text(encoding="utf-8"))
                    except RunError:
                        pass
                    else:
                        artifact["status"] = "reused"
                        manifest["artifacts"].append(artifact)
                        write_json(run_dir / "manifest.json", manifest)
                        print(f"Reused {model.label} / {reference.name}")
                        continue

                print(f"Running {model.label} / {reference.name} ...")
                try:
                    response, svg, cost = request_svg(
                        endpoint=model_endpoint,
                        api_key=api_key,
                        model=model,
                        reference=reference,
                        prompt=prompt,
                        defaults=dict(defaults),
                        image_detail=image_detail,
                        timeout_seconds=timeout_seconds,
                        retry_attempts=retry_attempts,
                    )
                    validate_svg(svg)
                    write_text(svg_path, svg + "\n")
                    write_json(response_path, response)
                    artifact.update(
                        {
                            "status": "ok",
                            "response_file": relative_to_run(response_path, run_dir),
                            "reported_cost_usd": cost,
                        }
                    )
                    if cost is not None:
                        reported_cost += cost
                        manifest["reported_total_cost_usd"] = reported_cost
                except (RunError, ConfigError) as exc:
                    errors += 1
                    artifact.update({"status": "error", "error": str(exc)})
                    print(f"  ERROR: {exc}", file=sys.stderr)
                manifest["artifacts"].append(artifact)
                write_json(run_dir / "manifest.json", manifest)
            if stopped:
                break

        manifest["finished_at"] = utc_now()
        write_json(run_dir / "manifest.json", manifest)
        write_review_html(run_dir, manifest)
        bundle = write_bundle(run_dir)
        print(f"\nArtifacts: {run_dir}")
        print(f"Review:    {run_dir / 'review.html'}")
        print(f"Bundle:    {bundle}")
        if errors:
            print(f"Completed with {errors} failed request(s). See manifest.json.", file=sys.stderr)
            return 1
        return 0
    except (ConfigError, RunError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

