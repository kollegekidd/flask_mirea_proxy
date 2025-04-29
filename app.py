import logging
import os
from urllib.parse import urlparse, urljoin

import requests
from flask import Flask, request, Response, stream_with_context

TARGET_URL = os.environ.get("TARGET_URL", "https://f.mirea.ru")

ENABLE_URL_REWRITING = os.environ.get("ENABLE_URL_REWRITING", "true").lower() == "true"

REWRITABLE_CONTENT_TYPES = [
    "text/html",
    "text/css",
    "application/javascript",
    "application/x-javascript",
    "application/json",
    "application/xml",
    "text/xml",
    "image/svg+xml",
]

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

target_parts = urlparse(TARGET_URL)
if not target_parts.scheme or not target_parts.netloc:
    raise ValueError("Invalid TARGET_URL. Please include scheme (http/https) and domain.")

TARGET_SCHEME = target_parts.scheme
TARGET_NETLOC = target_parts.netloc
TARGET_BASE = f"{TARGET_SCHEME}://{TARGET_NETLOC}"


def get_proxy_base_url():
    proto = request.headers.get("X-Forwarded-Proto", request.scheme)
    host = request.headers.get("X-Forwarded-Host", request.host)
    return f"{proto}://{host}"


def rewrite_content(content, proxy_base, target_base, target_netloc):
    if isinstance(content, bytes):
        try:
            content = content.decode()
        except UnicodeDecodeError:
            logging.warning("Could not decode content as UTF-8 for rewriting.")
            return content
    content = content.replace(target_base, proxy_base)
    content = content.replace(f"//{target_netloc}", f"//{urlparse(proxy_base).netloc}")

    # TODO: Add more robust rewriting using regex or parsing\
    return content.encode()


@app.route('/', defaults={'path': ''}, methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS', 'HEAD'])
@app.route('/<path:path>', methods=['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'OPTIONS', 'HEAD'])
def proxy(path):
    proxy_base = get_proxy_base_url()
    target_path_url = urljoin(TARGET_BASE + "/", path)
    if request.query_string:
        target_path_url += "?" + request.query_string.decode()

    logging.info(f"Proxying request for {request.method} {path} -> {target_path_url}")

    excluded_headers = [
        'host', 'connection', 'content-length', 'content-encoding',
        'transfer-encoding', 'keep-alive'
    ]
    headers = {key: value for key, value in request.headers if key.lower() not in excluded_headers}
    headers['Host'] = TARGET_NETLOC
    headers['X-Forwarded-For'] = request.remote_addr
    headers['X-Forwarded-Proto'] = request.scheme
    if 'Accept-Encoding' in request.headers:
        headers['Accept-Encoding'] = request.headers['Accept-Encoding']

    try:
        target_resp = requests.request(
            method=request.method,
            url=target_path_url,
            headers=headers,
            data=request.get_data(),
            cookies=request.cookies,
            stream=True,
            allow_redirects=False,
            timeout=30
        )
    except requests.exceptions.RequestException as e:
        logging.error(f"Error connecting to target: {e}")
        return "Proxy Error: Could not connect to target server.", 502

    resp_headers = []
    should_rewrite = False
    content_type_header = target_resp.headers.get('Content-Type', '').lower()

    for name, value in target_resp.raw.headers.items():
        name_lower = name.lower()
        if name_lower in ['transfer-encoding', 'connection', 'content-encoding', 'content-length']:
            continue

        if name_lower == 'location' and ENABLE_URL_REWRITING:
            original_location = value
            if original_location.startswith(TARGET_BASE):
                rewritten_location = original_location.replace(TARGET_BASE, proxy_base, 1)
                resp_headers.append((name, rewritten_location))
                logging.info(f"Rewriting Location: {original_location} -> {rewritten_location}")
            elif original_location.startswith("/"):
                rewritten_location = urljoin(proxy_base + "/", original_location.lstrip('/'))
                resp_headers.append((name, rewritten_location))
                logging.info(f"Rewriting relative Location: {original_location} -> {rewritten_location}")
            else:
                resp_headers.append((name, value))
            continue

        if name_lower == 'set-cookie' and ENABLE_URL_REWRITING:
            try:
                proxy_netloc = urlparse(proxy_base).netloc
                target_domain_only = TARGET_NETLOC.split(':')[0]
                proxy_domain_only = proxy_netloc.split(':')[0]

                cookie_val = value.replace(f"domain={target_domain_only}", f"domain={proxy_domain_only}")
                resp_headers.append((name, cookie_val))
                logging.debug(f"Attempted rewrite Set-Cookie: {value} -> {cookie_val}")
            except Exception as e:
                logging.warning(f"Failed to rewrite Set-Cookie header '{value}': {e}")
                resp_headers.append((name, value))
            continue

        resp_headers.append((name, value))
    content_type_base = content_type_header.split(';')[0].strip()
    if ENABLE_URL_REWRITING and content_type_base in REWRITABLE_CONTENT_TYPES:
        should_rewrite = True
        logging.debug(f"Content type {content_type_base} marked for rewriting.")

    def generate_content():
        if should_rewrite:
            try:
                original_content = target_resp.content
                logging.info(f"Read {len(original_content)} bytes for rewriting ({content_type_base})")
                rewritten = rewrite_content(original_content, proxy_base, TARGET_BASE, TARGET_NETLOC)
                yield rewritten
            except Exception as exc:
                logging.error(f"Error during content rewriting: {exc}")
                try:
                    yield target_resp.content
                except Exception as read_err:
                    logging.error(f"Error reading original content after rewrite failed: {read_err}")
                    yield b""
        else:
            try:
                for chunk in target_resp.iter_content(chunk_size=8192):
                    yield chunk
            except Exception as exc:
                logging.error(f"Error streaming content: {exc}")

    response = Response(stream_with_context(generate_content()),
                        status=target_resp.status_code,
                        headers=resp_headers)

    return response


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)  # Set debug=False for production testing
