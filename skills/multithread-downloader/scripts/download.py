#!/usr/bin/env python3
"""HTTP(S) segmented downloader. Python 3.9+, standard library only."""
import argparse
import concurrent.futures
import contextlib
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import ssl
import stat
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

VERSION = "1.0.0"
BUFFER = 256 * 1024
USER_AGENT = "multithread-downloader/" + VERSION
RANGE_RE = re.compile(r"bytes (\d+)-(\d+)/(\d+)")
HASH_RE = re.compile(r"[0-9a-f]{64}")
RESERVED_HEADERS = {
    "range", "if-range", "if-match", "if-none-match", "if-modified-since",
    "if-unmodified-since", "accept-encoding", "host", "content-length",
    "transfer-encoding", "connection", "proxy-authorization", "expect",
}


class DownloadError(Exception):
    def __init__(self, message, code=3, retryable=False, http_status=None):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.http_status = http_status


def digest_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def log(message):
    print(message, file=sys.stderr, flush=True)


def check_cancel(stop):
    if stop.is_set():
        raise DownloadError("下载已中断；保留临时文件。", 130)


def require_regular(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode):
        raise DownloadError("工作文件不是普通文件，拒绝读取或覆盖。", 6)
    return True


def ensure_directory(path):
    if path.is_symlink():
        raise DownloadError("临时目录不能是符号链接。", 6)
    path.mkdir(mode=0o700, exist_ok=True)
    if not stat.S_ISDIR(path.lstat().st_mode):
        raise DownloadError("临时目录路径不是目录。", 6)


def open_private(path, truncate=True):
    require_regular(path)
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    if truncate:
        flags |= os.O_TRUNC
    fd = os.open(str(path), flags, 0o600)
    return os.fdopen(fd, "wb")


def save_json(path, value):
    temporary = path.with_name(path.name + ".tmp")
    with open_private(temporary) as stream:
        stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        stream.flush()
        os.fsync(stream.fileno())
    require_regular(path)
    os.replace(str(temporary), str(path))


def hash_file(path, stop):
    if not require_regular(path):
        raise DownloadError("待校验的文件不存在。", 4)
    result = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while True:
            check_cancel(stop)
            block = stream.read(BUFFER)
            if not block:
                break
            result.update(block)
            size += len(block)
    return size, result.hexdigest()


class OutputLock:
    """Keep the inode: unlinking a locked file creates a second-lock race."""
    def __init__(self, path):
        self.path = path
        self.stream = None

    def __enter__(self):
        require_regular(self.path)
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        self.stream = os.fdopen(fd, "r+b", buffering=0)
        try:
            if os.name == "nt":
                import msvcrt
                if self.path.stat().st_size == 0:
                    self.stream.write(b"\0")
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            raise DownloadError("相同输出路径已有下载任务运行，未启动第二个任务。", 5) from None
        return self

    def __exit__(self, *_):
        if self.stream:
            # Closing releases OS locks, including on process termination.
            self.stream.close()


def validate_url(url):
    if any(ord(char) < 33 or ord(char) == 127 for char in url):
        raise DownloadError("URL 含空白或控制字符，请先进行 URL 编码。", 2)
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
        del port
        if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
            raise ValueError
        if parsed.username is not None or parsed.password is not None:
            raise DownloadError("不接受 URL 内嵌用户名或密码，请通过请求头文件提供鉴权。", 2)
        if parsed.fragment:
            raise DownloadError("直接下载 URL 不应包含 fragment（# 后面的内容）。", 2)
    except ValueError:
        raise DownloadError("仅接受合法 HTTP/HTTPS URL。", 2) from None
    return url


def origin(url):
    value = urllib.parse.urlsplit(url)
    return value.scheme.lower(), value.hostname.lower(), value.port or (443 if value.scheme == "https" else 80)


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        if origin(req.full_url)[0] == "https" and origin(newurl)[0] == "http":
            raise DownloadError("拒绝 HTTPS 降级到 HTTP 的重定向。")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and origin(req.full_url) != origin(newurl):
            # Drop ALL user-supplied headers on cross-origin redirects, not only
            # conventional Authorization/Cookie names (custom API keys exist).
            internal = {"range", "if-range", "if-match", "accept-encoding"}
            redirected.headers = {k: v for k, v in redirected.headers.items() if k.lower() in internal}
            redirected.unredirected_hdrs = {}
            redirected.add_header("User-Agent", USER_AGENT)
        return redirected


def load_headers(path):
    if path is None:
        return {}
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise DownloadError("无法读取请求头 JSON 文件；应为字符串到字符串的对象。", 2) from None
    if not isinstance(value, dict):
        raise DownloadError("请求头 JSON 必须是对象。", 2)
    result = {}
    for key, item in value.items():
        if not isinstance(key, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key):
            raise DownloadError("请求头名称无效。", 2)
        if not isinstance(item, str) or any(ord(c) < 32 or ord(c) == 127 for c in item):
            raise DownloadError("请求头值必须为不含控制字符的字符串。", 2)
        try:
            item.encode("latin-1")
        except UnicodeEncodeError:
            raise DownloadError("请求头值必须可按 Latin-1 编码；请使用服务端要求的编码形式。", 2) from None
        key = key.lower()
        if key in RESERVED_HEADERS or key in result:
            raise DownloadError("请求头含下载器保留字段或大小写重复字段。", 2)
        result[key] = item
    return result


class Client:
    def __init__(self, url, headers, timeout, retries, stop):
        self.url, self.headers = url, headers
        self.timeout, self.retries, self.stop = timeout, retries, stop
        # Explicitly honor CA environment settings, including macOS Python builds
        # whose LibreSSL defaults do not load SSL_CERT_FILE automatically.
        self.tls_context = ssl.create_default_context(
            cafile=os.environ.get("SSL_CERT_FILE") or None,
            capath=os.environ.get("SSL_CERT_DIR") or None,
        )

    def open(self, extra):
        check_cancel(self.stop)
        headers = {"User-Agent": USER_AGENT, **self.headers, "Accept-Encoding": "identity", **extra}
        request = urllib.request.Request(self.url, headers=headers, method="GET")
        # An opener per call avoids shared mutable redirect/handler state.
        opener = urllib.request.build_opener(SafeRedirect(), urllib.request.HTTPSHandler(context=self.tls_context))
        try:
            return opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as error:
            code = error.code
            error.close()
            if code == 412:
                raise DownloadError("远端资源版本已改变，保留分片；请确认后使用 --restart。", 5) from None
            raise DownloadError("服务器返回 HTTP %d。" % code, 3,
                                code in (408, 429, 500, 502, 503, 504), http_status=code) from None
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
            raise DownloadError("网络连接、TLS 或响应读取失败。", 3, True) from None

    def retry(self, operation, label):
        for attempt in range(self.retries + 1):
            check_cancel(self.stop)
            try:
                return operation()
            except DownloadError as error:
                if not error.retryable or attempt == self.retries:
                    raise
                delay = min(2 ** attempt, 60)
                log("%s失败，%d 秒后重试（%d/%d）。" % (label, delay, attempt + 1, self.retries))
                if self.stop.wait(delay):
                    check_cancel(self.stop)
            except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as error:
                # Filesystem errors must not be retried as networking failures.
                if isinstance(error, OSError) and not isinstance(error, (TimeoutError, ConnectionError, urllib.error.URLError)):
                    raise DownloadError("文件读写或网络 I/O 失败，保留临时文件。", 6) from None
                if attempt == self.retries:
                    raise DownloadError("网络读取失败或响应提前结束，重试次数已用尽。", 3) from None
                delay = min(2 ** attempt, 60)
                log("%s响应中断，%d 秒后重试（%d/%d）。" % (label, delay, attempt + 1, self.retries))
                if self.stop.wait(delay):
                    check_cancel(self.stop)


def content_length(response):
    values = response.headers.get_all("Content-Length", [])
    if not values:
        return None
    if len(values) != 1 or not re.fullmatch(r"\d+", values[0].strip()):
        raise DownloadError("服务端 Content-Length 不合法或重复。", 4)
    if response.headers.get("Transfer-Encoding"):
        raise DownloadError("响应同时包含 Content-Length 和 Transfer-Encoding，拒绝含糊长度。", 4)
    return int(values[0])


def check_encoding(response):
    if response.headers.get("Content-Encoding", "identity").strip().lower() != "identity":
        raise DownloadError("服务器未遵守 identity 编码要求，拒绝按压缩表示拼接文件。", 4)


def strong_etag(response):
    value = response.headers.get("ETag")
    if value and re.fullmatch(r'"[\x21\x23-\x7e\x80-\xff]*"', value):
        return value
    return None


def read_network(response, length):
    try:
        return response.read(length)
    except (OSError, http.client.HTTPException):
        raise DownloadError("网络响应读取中断。", 3, True) from None


def probe(client):
    def attempt():
        try:
            response = client.open({"Range": "bytes=0-0"})
        except DownloadError as error:
            if error.http_status != 416:
                raise
            # A zero-length resource legitimately rejects bytes=0-0.
            response = client.open({})
        with response:
            check_encoding(response)
            length = content_length(response)
            status = response.status
            if status == 206:
                match = RANGE_RE.fullmatch(response.headers.get("Content-Range", ""))
                if not match or tuple(map(int, match.groups()[:2])) != (0, 0) or int(match[3]) < 1:
                    raise DownloadError("探测响应的 Content-Range 不正确。", 4)
                if length not in (None, 1):
                    raise DownloadError("探测响应长度不正确。", 4)
                if len(read_network(response, 2)) != 1:
                    raise DownloadError("探测响应正文长度不正确。", 4, True)
                total, ranges = int(match[3]), True
            elif status == 200:
                total, ranges = length, False
                # Do not consume a potentially enormous body just to probe it.
            else:
                raise DownloadError("服务器未返回可用的文件响应（HTTP %d）。" % status)
            etag = strong_etag(response)
            return {"size": total, "ranges": ranges, "etag": etag,
                    "etag_hash": digest_text(etag) if etag else None,
                    "resolved_url_hash": digest_text(response.geturl())}
    return client.retry(attempt, "资源探测")


def same_response(response, remote):
    check_encoding(response)
    if digest_text(response.geturl()) != remote["resolved_url_hash"]:
        raise DownloadError("重定向目标发生变化，停止以避免混用资源。", 5)
    if remote["etag"] is not None and strong_etag(response) != remote["etag"]:
        raise DownloadError("资源 ETag 改变或消失，停止以避免混用不同版本。", 5)


def conditional_headers(remote):
    return {"If-Match": remote["etag"]} if remote["etag"] else {}


def stream_to_file(response, path, expected, stop):
    size, digest = 0, hashlib.sha256()
    with open_private(path) as stream:
        while True:
            check_cancel(stop)
            # Read one extra byte on the last chunk when framing permits it.
            amount = BUFFER if expected is None else min(BUFFER, expected - size + 1)
            block = read_network(response, amount)
            if not block:
                break
            size += len(block)
            if expected is not None and size > expected:
                raise DownloadError("响应正文超出预期分片长度。", 4)
            stream.write(block)
            digest.update(block)
        stream.flush()
        os.fsync(stream.fileno())
    if expected is not None and size != expected:
        raise DownloadError("响应提前结束，分片长度不足。", 4, True)
    return {"size": size, "sha256": digest.hexdigest()}


def part_name(index):
    return "part-%08d.bin" % index


def load_manifest(path):
    if not require_regular(path):
        raise DownloadError("临时任务缺少有效记录；确认后使用 --restart 保留旧任务并重建。", 5)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("completed"), dict):
            raise ValueError
        return value
    except (ValueError, UnicodeError):
        raise DownloadError("续传记录损坏；确认后使用 --restart 保留旧任务并重建。", 5) from None


def prepare_state(active, args, remote, request_hash, mode):
    identity = {"version": 1, "request_hash": request_hash, "size": remote["size"],
                "etag_hash": remote["etag_hash"], "resolved_url_hash": remote["resolved_url_hash"],
                "expected_sha256": args.sha256, "chunk_size": args.chunk_size, "mode": mode}
    manifest_path = active / "manifest.json"
    if active.exists():
        ensure_directory(active)
        state = load_manifest(manifest_path)
        if any(state.get(key) != value for key, value in identity.items()):
            raise DownloadError("旧任务与当前 URL、请求头、资源版本或参数不一致；请确认后使用 --restart。", 5)
        if mode == "parallel" and not remote["etag"] and not args.sha256:
            raise DownloadError("没有可靠版本标识或可信 SHA-256，不能复用旧分片。", 5)
        if mode == "single":
            state["completed"] = {}
            log("单线程模式重新下载，不复用中断的文件。")
    else:
        ensure_directory(active)
        state = {**identity, "completed": {}}
    save_json(manifest_path, state)
    return state


def check_completed(active, state, count, total, chunk_size, stop):
    valid = {}
    for index in range(count):
        check_cancel(stop)
        record = state["completed"].get(str(index))
        if not isinstance(record, dict):
            continue
        expected = min(chunk_size, total - index * chunk_size)
        path = active / part_name(index)
        if not require_regular(path):
            continue
        if path.stat().st_size != expected:
            continue
        size, sha = hash_file(path, stop)
        if size == record.get("size") == expected and sha == record.get("sha256"):
            valid[str(index)] = record
    discarded = len(state["completed"]) - len(valid)
    if discarded:
        log("发现 %d 个不可复用的分片记录，将重新下载对应范围。" % discarded)
    state["completed"] = valid
    save_json(active / "manifest.json", state)
    return len(valid)


def download_parts(client, active, state, remote, args):
    total = remote["size"]
    count = (total + args.chunk_size - 1) // args.chunk_size
    if count > 1000000:
        raise DownloadError("分片超过一百万个，请增大 --chunk-size。", 2)
    resumed = check_completed(active, state, count, total, args.chunk_size, client.stop)
    pending = iter(index for index in range(count) if str(index) not in state["completed"])
    mutex = threading.Lock()
    errors = []
    progress_at = [0.0]
    progress_bytes = [sum(record["size"] for record in state["completed"].values())]
    threads = min(args.threads, max(count - resumed, 1))
    log("分片模式：%d 个分片，最多 %d 个线程，复用 %d 个已验证分片。" % (count, threads, resumed))

    def fetch(index):
        start = index * args.chunk_size
        end = min(start + args.chunk_size, total) - 1
        headers = {**conditional_headers(remote), "Range": "bytes=%d-%d" % (start, end)}
        if remote["etag"]:
            headers["If-Range"] = remote["etag"]
        with client.open(headers) as response:
            same_response(response, remote)
            if response.status != 206:
                raise DownloadError("分片请求不再返回 206，保留现场；请确认后 --restart 重新探测。", 5)
            match = RANGE_RE.fullmatch(response.headers.get("Content-Range", ""))
            if not match or tuple(map(int, match.groups())) != (start, end, total):
                raise DownloadError("分片 Content-Range 与请求范围不符。", 4)
            length = content_length(response)
            if length is not None and length != end - start + 1:
                raise DownloadError("分片 Content-Length 不正确。", 4)
            temporary = active / (part_name(index) + ".partial")
            record = stream_to_file(response, temporary, end - start + 1, client.stop)
        target = active / part_name(index)
        require_regular(target)
        os.replace(str(temporary), str(target))
        return record

    def worker():
        try:
            while not client.stop.is_set():
                with mutex:
                    index = next(pending, None)
                if index is None:
                    return
                record = client.retry(lambda: fetch(index), "分片 %d" % index)
                with mutex:
                    state["completed"][str(index)] = record
                    save_json(active / "manifest.json", state)
                    progress_bytes[0] += record["size"]
                    now = time.monotonic()
                    if now - progress_at[0] >= 1 or progress_bytes[0] == total:
                        log("已完成 %d/%d 字节（%.1f%%）。" % (progress_bytes[0], total, 100 * progress_bytes[0] / total))
                        progress_at[0] = now
        except BaseException as error:
            with mutex:
                errors.append(error)
            client.stop.set()

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=threads)
    futures = [pool.submit(worker) for _ in range(threads)]
    try:
        for future in futures:
            future.result()
    except BaseException:
        client.stop.set()
        raise
    finally:
        pool.shutdown(wait=True)
    if errors:
        raise errors[0]
    check_cancel(client.stop)
    # Full second pass immediately before merging, not just trusting filenames.
    for index in range(count):
        expected = min(args.chunk_size, total - index * args.chunk_size)
        size, sha = hash_file(active / part_name(index), client.stop)
        record = state["completed"].get(str(index), {})
        if size != expected or size != record.get("size") or sha != record.get("sha256"):
            raise DownloadError("合并前分片校验失败，保留分片，不生成正式文件。", 4)
    return count, resumed, threads


def merge_parts(active, count, state, stop):
    candidate = active / "merged.tmp"
    aggregate = hashlib.sha256()
    size = 0
    with open_private(candidate) as output:
        for index in range(count):
            piece_hash = hashlib.sha256()
            piece_size = 0
            path = active / part_name(index)
            require_regular(path)
            with path.open("rb") as stream:
                while True:
                    check_cancel(stop)
                    block = stream.read(BUFFER)
                    if not block:
                        break
                    output.write(block)
                    aggregate.update(block)
                    piece_hash.update(block)
                    piece_size += len(block)
                    size += len(block)
            record = state["completed"][str(index)]
            if piece_size != record["size"] or piece_hash.hexdigest() != record["sha256"]:
                raise DownloadError("合并期间分片被修改，保留临时文件。", 4)
        output.flush()
        os.fsync(output.fileno())
    return candidate, size, aggregate.hexdigest()


def download_single(client, active, state, remote):
    def attempt():
        with client.open(conditional_headers(remote)) as response:
            same_response(response, remote)
            if response.status != 200:
                raise DownloadError("单线程请求没有返回完整文件（200）。", 4)
            length = content_length(response)
            if remote["size"] is not None and length is not None and length != remote["size"]:
                raise DownloadError("单线程响应的总大小发生变化。", 5)
            expected = remote["size"] if remote["size"] is not None else length
            record = stream_to_file(response, active / "single.partial", expected, client.stop)
            return record, expected
    log("已降级为单线程完整下载。")
    record, expected = client.retry(attempt, "单线程下载")
    target = active / part_name(0)
    require_regular(target)
    os.replace(str(active / "single.partial"), str(target))
    state["completed"] = {"0": record}
    save_json(active / "manifest.json", state)
    size, sha = hash_file(target, client.stop)
    if size != record["size"] or sha != record["sha256"]:
        raise DownloadError("单线程临时文件校验失败。", 4)
    return target, size, sha, expected


def verify_candidate(candidate, assembled_size, assembled_hash, expected_size, expected_sha, stop):
    log("正在重新读取合并文件，检查最终大小与 SHA-256。")
    size, sha = hash_file(candidate, stop)
    if size != assembled_size or sha != assembled_hash:
        raise DownloadError("合并文件与已验证分片不一致，保留全部临时文件。", 4)
    if expected_size is not None and size != expected_size:
        raise DownloadError("最终文件大小与远端预期大小不一致，保留分片。", 4)
    if expected_sha is not None and sha != expected_sha:
        raise DownloadError("最终 SHA-256 与可信预期值不一致，保留分片。", 4)
    if expected_size is None and expected_sha is None:
        raise DownloadError("无法确认完整性：既无预期总大小，也无可信 SHA-256；已保留临时文件。", 4)
    return size, sha


@contextlib.contextmanager
def publication_guard():
    """Do not report interrupted after committing a verified output file."""
    saved = {}
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGINT, signal.SIGTERM):
            saved[signum] = signal.getsignal(signum)
            signal.signal(signum, signal.SIG_IGN)
    try:
        yield
    finally:
        for signum, handler in saved.items():
            signal.signal(signum, handler)


def publish(candidate, target, overwrite):
    require_regular(target)
    if overwrite:
        os.replace(str(candidate), str(target))
    else:
        # Atomic no-clobber publication, even if another process creates target
        # after our initial check. Refuse unsupported filesystems instead of
        # falling back to an unsafe check-then-rename or half-written copy.
        try:
            os.link(str(candidate), str(target))
        except FileExistsError:
            raise DownloadError("目标文件在下载期间出现，未覆盖；已保留临时文件。", 5) from None
        except OSError:
            raise DownloadError("文件系统不支持安全的无覆盖发布，保留临时文件；可改用支持硬链接的磁盘。", 6) from None
    if os.name != "nt":
        try:
            fd = os.open(str(target.parent), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            log("提示：文件已发布，但文件系统不支持目录 fsync。")


def run(args, stop):
    url = args.url
    if args.url_file:
        try:
            url = Path(args.url_file).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            raise DownloadError("无法读取 URL 文件。", 2) from None
    validate_url(url)
    headers = load_headers(args.headers_file)
    request_hash = digest_text(json.dumps({"url": url, "headers": headers}, sort_keys=True))
    requested = Path(args.output).expanduser().absolute()
    requested.parent.mkdir(parents=True, exist_ok=True)
    target = requested.parent.resolve() / requested.name
    root = target.parent / ("." + target.name + ".download")
    ensure_directory(root)
    active = root / "active"
    with OutputLock(root / ".lock"):
        if require_regular(target) and not args.overwrite:
            raise DownloadError("目标文件已存在；未覆盖。请更换输出路径或显式指定 --overwrite。", 5)
        if active.is_symlink():
            raise DownloadError("临时任务目录不能是符号链接。", 6)
        client = Client(url, headers, args.timeout, args.retries, stop)
        remote = probe(client)
        mode = "parallel" if remote["ranges"] and (remote["etag"] or args.sha256) else "single"
        if remote["ranges"] and mode == "single":
            log("服务端虽支持分片，但没有强 ETag 或可信 SHA-256；为避免混合版本改用单线程。")
        if args.restart and active.exists():
            ensure_directory(active)
            retained = root / ("retained-" + uuid.uuid4().hex)
            os.rename(str(active), str(retained))
            log("旧任务已保留到 retained 目录，新任务不复用旧分片。")
        state = prepare_state(active, args, remote, request_hash, mode)
        if mode == "parallel":
            count, resumed, threads = download_parts(client, active, state, remote, args)
            log("所有分片检查通过，按字节范围顺序合并。")
            candidate, size, sha = merge_parts(active, count, state, stop)
            expected_size = remote["size"]
        else:
            candidate, size, sha, expected_size = download_single(client, active, state, remote)
            count, resumed, threads = 1, 0, 1
        size, sha = verify_candidate(candidate, size, sha, expected_size, args.sha256, stop)
        check_cancel(stop)
        with publication_guard():
            publish(candidate, target, args.overwrite)
            # Publishing is the commit point. Complete cleanup before honoring
            # further interrupts; no half-published file is ever presented.
            cleaned = True
            try:
                shutil.rmtree(str(active))
            except OSError:
                cleaned = False
                log("文件已验证并发布，但临时文件清理不完整；可手动检查临时目录。")
        verification = "sha256" if args.sha256 else "size-and-structure"
        note = "可信 SHA-256 校验通过。" if args.sha256 else "大小与分片结构校验通过，未验证内容哈希；计算所得 SHA-256 仅作记录。"
        return {"status": "complete" if cleaned else "complete-with-cleanup-warning",
                "output": str(target), "bytes": size, "sha256": sha, "verification": verification,
                "mode": mode, "threads": threads, "parts": count, "resumed_parts": resumed,
                "temporary_parts_cleaned": cleaned, "temporary_directory": str(active),
                "lock_file": str(root / ".lock"), "note": note}


def parse_size(value):
    match = re.fullmatch(r"(\d+)\s*(B|KiB|MiB|GiB)?", value, re.I)
    if not match:
        raise argparse.ArgumentTypeError("分片大小示例：16777216、16MiB、64MiB。")
    amount = int(match[1]) * {"b": 1, "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3}[(match[2] or "B").lower()]
    if amount < 1 or amount > 1024 ** 3:
        raise argparse.ArgumentTypeError("分片大小范围为 1 字节到 1 GiB。")
    return amount


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="多线程 HTTP/HTTPS 下载、分片续传、验证、合并及安全清理（Python 3.9+）。")
    parser.add_argument("url", nargs="?", help="HTTP/HTTPS 直接文件链接；敏感签名链接优先用 --url-file")
    parser.add_argument("--url-file", help="从 UTF-8 文件读取 URL，避免出现在命令行参数中")
    parser.add_argument("-o", "--output", required=True, help="正式输出文件路径，不自动使用服务端文件名")
    parser.add_argument("--threads", type=int, default=8, help="下载线程数 1～32，默认 8")
    parser.add_argument("--chunk-size", type=parse_size, default=64 * 1024 ** 2, help="默认 64MiB")
    parser.add_argument("--retries", type=int, default=3, help="失败后额外重试次数 0～10，默认 3，等待 1/2/4 秒")
    parser.add_argument("--timeout", type=float, default=30, help="单次网络阻塞超时秒数，默认 30；不是任务总时限")
    parser.add_argument("--sha256", help="可信来源提供的 64 位十六进制 SHA-256")
    parser.add_argument("--headers-file", help="自定义请求头 JSON 文件，不会写入日志或续传记录")
    parser.add_argument("--overwrite", action="store_true", help="仅在新文件验证通过后原子替换原文件")
    parser.add_argument("--restart", action="store_true", help="保留旧 active 为 retained 目录，开启全新下载")
    parser.add_argument("--json", action="store_true", help="stdout 输出单个 JSON 结果；进度写 stderr")
    parser.add_argument("--version", action="version", version=VERSION)
    args = parser.parse_args(argv)
    if bool(args.url) == bool(args.url_file):
        parser.error("必须且只能提供 URL 或 --url-file。")
    if not 1 <= args.threads <= 32:
        parser.error("--threads 范围为 1～32。")
    if not 0 <= args.retries <= 10:
        parser.error("--retries 范围为 0～10。")
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 3600:
        parser.error("--timeout 必须大于 0 且不超过 3600 秒。")
    if args.sha256:
        args.sha256 = args.sha256.lower()
        if not HASH_RE.fullmatch(args.sha256):
            parser.error("--sha256 必须是 64 位十六进制。")
    return args


def main(argv=None):
    # Deterministic UTF-8 for redirected output, including Windows legacy locales.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    args = parse_args(argv)
    stop = threading.Event()
    old_term = None
    if threading.current_thread() is threading.main_thread():
        old_term = signal.getsignal(signal.SIGTERM)
        def interrupt(_signum, _frame):
            stop.set()
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, interrupt)
    try:
        result = run(args, stop)
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print("下载完成：%s\n大小：%d 字节\nSHA-256：%s\n%s\n临时分片：%s" % (
                result["output"], result["bytes"], result["sha256"], result["note"],
                "已清理（保留无凭据锁文件）" if result["temporary_parts_cleaned"] else "清理不完整，请检查"))
        return 0
    except KeyboardInterrupt:
        stop.set()
        error = DownloadError("下载已中断；完成且验证通过的分片已保留，可重跑同一命令恢复。", 130)
    except DownloadError as caught:
        error = caught
    except OSError:
        error = DownloadError("本地文件操作失败（请检查空间、权限和路径）；保留已有数据。", 6)
    except Exception:
        # Do not dump exceptions containing request URLs, headers or response bodies.
        error = DownloadError("遇到未预期错误，已停止并保留临时文件；未输出敏感请求上下文。", 6)
    finally:
        if old_term is not None:
            signal.signal(signal.SIGTERM, old_term)
    if args.json:
        print(json.dumps({"status": "failed", "exit_code": error.code, "error": str(error)}, ensure_ascii=False))
    else:
        log("下载失败：" + str(error))
    return error.code


if __name__ == "__main__":
    sys.exit(main())
