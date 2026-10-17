# -*- coding: utf-8 -*-
"""
Publishes a generated Allure HTML report (from running one of testLoke's
own pytest test files via the Script Runner) to a Cloudflare R2 bucket
over its S3-compatible API, and returns a public https://pub-xxxx.r2.dev/...
(or custom domain) URL - same mechanism mobile_agent's own cf_r2.py uses
for its Test Scripts tab, ported here since this is a separate project/
repo with its own venv and can't just import that module.

Needs CLOUDFLARE_ACCOUNT_ID, CLOUDFLARE_R2_ACCESS_KEY_ID,
CLOUDFLARE_R2_SECRET_ACCESS_KEY, CLOUDFLARE_R2_BUCKET and
CLOUDFLARE_R2_PUBLIC_URL (the bucket's r2.dev public-access URL, or a
custom domain mapped to it) - see .env.example. Also needs the bucket's
"Public Access" enabled in the R2 dashboard, or uploads will succeed but
the returned URL will 403.
"""
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import time
from typing import Callable, List, Optional


def is_configured() -> bool:
    return bool(
        os.environ.get("CLOUDFLARE_ACCOUNT_ID")
        and os.environ.get("CLOUDFLARE_R2_ACCESS_KEY_ID")
        and os.environ.get("CLOUDFLARE_R2_SECRET_ACCESS_KEY")
        and os.environ.get("CLOUDFLARE_R2_BUCKET")
        and os.environ.get("CLOUDFLARE_R2_PUBLIC_URL")
    )


def _client():
    import boto3
    from botocore.config import Config

    account_id = os.environ["CLOUDFLARE_ACCOUNT_ID"]
    return boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["CLOUDFLARE_R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["CLOUDFLARE_R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(connect_timeout=5, read_timeout=15, retries={"max_attempts": 2}),
    )


def _public_url(key: str) -> str:
    base = os.environ["CLOUDFLARE_R2_PUBLIC_URL"].rstrip("/")
    return f"{base}/{key}"


def _upload_dir(local_dir: str, key_prefix: str, log_fn: Callable[[str], None]) -> None:
    client = _client()
    bucket = os.environ["CLOUDFLARE_R2_BUCKET"]
    uploaded = 0
    for root, _dirs, files in os.walk(local_dir):
        for name in files:
            path = os.path.join(root, name)
            rel = os.path.relpath(path, local_dir).replace(os.sep, "/")
            key = f"{key_prefix}/{rel}"
            content_type, _ = mimetypes.guess_type(name)
            extra_args = {"ContentType": content_type} if content_type else {}
            client.upload_file(path, bucket, key, ExtraArgs=extra_args)
            uploaded += 1
    log_fn(f"Uploaded {uploaded} file(s) to r2://{bucket}/{key_prefix}/")


_SECRET_NAME_PATTERN = re.compile(r"(TOKEN|SECRET|PASSWORD|PASSWD|_KEY|URI|WEBHOOK)", re.IGNORECASE)
_BINARY_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".ttf", ".eot"}


def _report_secrets(project_root: Optional[str]) -> List[str]:
    """Same reasoning as mobile_agent's allure_publish.py: the pytest run
    that produced these Allure results executed with this process's own
    environment PLUS whatever testLoke's own .env supplies via its own
    load_dotenv() call - either could print a secret straight into a
    generated attachment (e.g. a connection string logged by a helper),
    so both need covering before this ever gets uploaded."""
    from dotenv import dotenv_values

    values = dict(os.environ)
    if project_root:
        values.update(dotenv_values(os.path.join(project_root, ".env")))
    return [v for k, v in values.items() if v and len(v) >= 6 and _SECRET_NAME_PATTERN.search(k)]


def _scrub_report_files(report_dir: str, secrets: List[str], log_fn: Callable[[str], None]) -> None:
    if not secrets:
        return
    scrubbed_files = 0
    for root, _dirs, files in os.walk(report_dir):
        for name in files:
            if os.path.splitext(name)[1].lower() in _BINARY_EXTENSIONS:
                continue
            path = os.path.join(root, name)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
            except (UnicodeDecodeError, OSError):
                continue
            new_content = content
            for secret in secrets:
                new_content = new_content.replace(secret, "***")
            if new_content != content:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(new_content)
                scrubbed_files += 1
    if scrubbed_files:
        log_fn(f"Scrubbed secret-looking values out of {scrubbed_files} report file(s) before publishing.")


def publish_html_report(subdir: str, filename: str, html_content: str) -> str:
    """Uploads one self-contained HTML string to <bucket>/<subdir>/<filename>
    and returns its public URL - used for a standalone-script run (no
    pytest/Allure results to bundle, just a plain log wrapped in HTML),
    unlike publish_allure_report's multi-file upload."""
    if not is_configured():
        raise RuntimeError("Cloudflare R2 is not configured - see CLOUDFLARE_R2_* in .env")
    client = _client()
    bucket = os.environ["CLOUDFLARE_R2_BUCKET"]
    key = f"{subdir}/{filename}"
    client.put_object(
        Bucket=bucket, Key=key,
        Body=html_content.encode("utf-8"),
        ContentType="text/html; charset=utf-8",
    )
    return _public_url(key)


def publish_allure_report(allure_results_dir: str, project_root: Optional[str],
                           log_fn: Callable[[str], None]) -> str:
    """
    Runs `allure generate` on allure_results_dir into a fresh temp dir,
    scrubs secret-looking values out of it, uploads every file to
    <bucket>/testloke-reports/<timestamp>/, and returns the public URL to
    that report's index.html.
    """
    if not is_configured():
        raise RuntimeError("Cloudflare R2 is not configured - see CLOUDFLARE_R2_* in .env")
    if shutil.which("allure") is None:
        raise RuntimeError('"allure" CLI not found on PATH - install it (e.g. "brew install allure")')
    if not os.path.isdir(allure_results_dir) or not os.listdir(allure_results_dir):
        raise RuntimeError(f"no Allure results found at {allure_results_dir} - was --alluredir actually written?")

    timestamp = time.strftime("%Y-%m-%d_%H-%M-%S", time.gmtime())
    with tempfile.TemporaryDirectory(prefix="api_testgen_r2_allure_") as tmp:
        report_dir = os.path.join(tmp, "allure-report")
        result = subprocess.run(
            ["allure", "generate", allure_results_dir, "-o", report_dir, "--clean"],
            capture_output=True, text=True,
        )
        for line in (result.stdout + result.stderr).splitlines():
            log_fn(line)
        if result.returncode != 0:
            raise RuntimeError(f"allure generate failed (exit {result.returncode})")

        secrets = _report_secrets(project_root)
        _scrub_report_files(report_dir, secrets, log_fn)

        key_prefix = f"testloke-reports/{timestamp}"
        _upload_dir(report_dir, key_prefix, log_fn)

    url = _public_url(f"{key_prefix}/index.html")
    log_fn(f"Published Allure report to Cloudflare R2: {url}")
    return url
