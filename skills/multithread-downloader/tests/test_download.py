"""Behavior tests with real local HTTP servers; no external services needed."""
import hashlib
import http.server
import importlib.util
import json
import os
from pathlib import Path
import signal
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "download.py"
spec = importlib.util.spec_from_file_location("downloader", SCRIPT)
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)
PAYLOAD = bytes(range(256)) * 3072
SHA = hashlib.sha256(PAYLOAD).hexdigest()
CHUNK = 65536


class Fixture:
    def __init__(self, **options):
        self.data = options.pop("data", PAYLOAD)
        self.etag = options.pop("etag", '"v1"')
        self.options = options
        self.requests = []
        self.active = self.peak = 0
        self.lock = threading.Lock()
        self.failures = options.pop("failures", {})
        self.failure_used = set()
        self.server = None
        self.thread = None

    def __enter__(self):
        fixture = self
        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"
            def log_message(self, *_):
                pass

            def do_GET(self):
                try:
                    self.respond()
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def respond(self):
                opt = fixture.options
                range_header = self.headers.get("Range")
                with fixture.lock:
                    fixture.requests.append({"range": range_header, "path": self.path, "headers": dict(self.headers)})
                if opt.get("redirect") and self.path.startswith("/redirect"):
                    self.send_response(302)
                    self.send_header("Location", opt["redirect"])
                    self.end_headers()
                    return
                if opt.get("auth") and self.headers.get("Authorization") != opt["auth"]:
                    self.send_response(401)
                    self.end_headers()
                    return
                if self.headers.get("If-Match") and self.headers["If-Match"] != fixture.etag:
                    self.send_response(412)
                    self.end_headers()
                    return
                if not fixture.data and range_header:
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */0")
                    self.end_headers()
                    return
                ranged = bool(range_header and not opt.get("ignore_range"))
                start, end = 0, len(fixture.data) - 1
                if ranged:
                    start, end = map(int, range_header[6:].split("-"))
                probe = range_header == "bytes=0-0"
                if not probe and start in fixture.failures:
                    failure = fixture.failures[start]
                    with fixture.lock:
                        used = start in fixture.failure_used
                        fixture.failure_used.add(start)
                    if failure == "always" or (failure == "once" and not used):
                        self.send_response(503)
                        self.end_headers()
                        return
                if not probe and opt.get("switch_etag"):
                    fixture.etag = '"v2"'
                if not probe and opt.get("ignore_after_probe"):
                    ranged = False
                    start, end = 0, len(fixture.data) - 1
                body = fixture.data[start:end + 1]
                self.send_response(206 if ranged else 200)
                if fixture.etag is not None:
                    self.send_header("ETag", fixture.etag)
                if opt.get("encoding"):
                    self.send_header("Content-Encoding", opt["encoding"])
                if ranged:
                    content_start = start + (1 if opt.get("bad_range") and not probe else 0)
                    self.send_header("Content-Range", "bytes %d-%d/%d" % (content_start, end, len(fixture.data)))
                if not opt.get("unknown_length"):
                    reported = len(body)
                    if opt.get("bad_length") and not probe:
                        reported += 1
                    self.send_header("Content-Length", str(reported))
                self.end_headers()
                if not probe:
                    with fixture.lock:
                        fixture.active += 1
                        fixture.peak = max(fixture.peak, fixture.active)
                try:
                    if not probe:
                        time.sleep(opt.get("delay", 0))
                    if opt.get("short_body") and not probe:
                        body = body[:len(body) // 2]
                    if not probe and opt.get("drop_once") and start not in fixture.failure_used:
                        fixture.failure_used.add(start)
                        body = body[:len(body) // 2]
                    self.wfile.write(body)
                    self.wfile.flush()
                finally:
                    if not probe:
                        with fixture.lock:
                            fixture.active -= 1
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        if self.options.get("tls_context"):
            self.server.socket = self.options["tls_context"].wrap_socket(self.server.socket, server_side=True)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        scheme = "https" if self.options.get("tls_context") else "http"
        self.url = "%s://127.0.0.1:%d/file" % (scheme, self.server.server_port)
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class DownloaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.output = self.path / "result.bin"
        self.root = self.path / ".result.bin.download"
        self.active = self.root / "active"

    def tearDown(self):
        self.temp.cleanup()

    def command(self, url, *extra):
        return [sys.executable, str(SCRIPT), url, "-o", str(self.output), "--chunk-size", str(CHUNK),
                "--retries", "0", "--timeout", "3", "--json", *extra]

    def invoke(self, url, *extra, expected=0):
        process = subprocess.run(self.command(url, *extra), capture_output=True, text=True, encoding="utf-8", timeout=25)
        self.assertEqual(process.returncode, expected, process.stdout + process.stderr)
        return json.loads(process.stdout), process

    def check_success(self, result, data=PAYLOAD):
        self.assertEqual(self.output.read_bytes(), data)
        self.assertEqual(result["bytes"], len(data))
        self.assertEqual(result["sha256"], hashlib.sha256(data).hexdigest())
        self.assertTrue(result["temporary_parts_cleaned"])
        self.assertFalse(self.active.exists())
        self.assertEqual([p.name for p in self.root.iterdir()], [".lock"])

    def test_parallel_default_eight_real_concurrent_requests(self):
        with Fixture(delay=0.06) as server:
            result, _ = self.invoke(server.url, "--sha256", SHA)
            self.check_success(result)
            self.assertEqual(result["threads"], 8)
            self.assertEqual(result["verification"], "sha256")
            self.assertGreater(server.peak, 1)
            self.assertLessEqual(server.peak, 8)
            actual = sorted(r["range"] for r in server.requests if r["range"] != "bytes=0-0")
            expected = sorted("bytes=%d-%d" % (i, min(i + CHUNK, len(PAYLOAD)) - 1) for i in range(0, len(PAYLOAD), CHUNK))
            self.assertEqual(actual, expected)

    def test_default_chunk_size_64_mib_actual_ranges(self):
        data = bytes(range(256)) * (256 * 1024) + b"tail"
        with Fixture(data=data) as server:
            command = self.command(server.url)
            option_index = command.index("--chunk-size")
            del command[option_index:option_index + 2]
            process = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=25)
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
            result = json.loads(process.stdout)
            self.check_success(result, data)
            self.assertEqual(result["parts"], 2)
            self.assertEqual(result["threads"], 2)
            ranges = [r["range"] for r in server.requests if r["range"] != "bytes=0-0"]
            self.assertCountEqual(ranges, ["bytes=0-67108863", "bytes=67108864-67108867"])

    def test_basic_integrity_without_trusted_hash(self):
        with Fixture() as server:
            result, _ = self.invoke(server.url)
            self.check_success(result)
            self.assertEqual(result["verification"], "size-and-structure")

    def test_small_file_uses_only_one_thread(self):
        with Fixture(data=b"small") as server:
            result, _ = self.invoke(server.url)
            self.check_success(result, b"small")
            self.assertEqual(result["threads"], 1)

    def test_last_short_chunk(self):
        data = PAYLOAD + b"tail"
        with Fixture(data=data) as server:
            result, _ = self.invoke(server.url)
            self.check_success(result, data)
            self.assertEqual(result["parts"], 13)

    def test_no_range_falls_back(self):
        with Fixture(ignore_range=True) as server:
            result, _ = self.invoke(server.url)
            self.check_success(result)
            self.assertEqual(result["mode"], "single")

    def test_no_strong_validator_falls_back(self):
        with Fixture(etag=None) as server:
            result, _ = self.invoke(server.url)
            self.check_success(result)
            self.assertEqual(result["mode"], "single")

    def test_weak_etag_not_used_for_parallel_identity(self):
        with Fixture(etag='W/"v1"') as server:
            result, _ = self.invoke(server.url)
            self.check_success(result)
            self.assertEqual(result["mode"], "single")

    def test_trusted_hash_allows_parallel_without_etag(self):
        with Fixture(etag=None) as server:
            result, _ = self.invoke(server.url, "--sha256", SHA)
            self.check_success(result)
            self.assertEqual(result["mode"], "parallel")

    def test_empty_file_416_probe(self):
        with Fixture(data=b"") as server:
            result, _ = self.invoke(server.url)
            self.check_success(result, b"")

    def test_unknown_length_without_hash_retains_unverified_file(self):
        with Fixture(ignore_range=True, unknown_length=True) as server:
            result, _ = self.invoke(server.url, expected=4)
            self.assertFalse(self.output.exists())
            self.assertEqual((self.active / d.part_name(0)).read_bytes(), PAYLOAD)
            self.assertEqual(result["status"], "failed")

    def test_unknown_length_with_hash_can_complete(self):
        with Fixture(ignore_range=True, unknown_length=True) as server:
            result, _ = self.invoke(server.url, "--sha256", SHA)
            self.check_success(result)

    def test_hash_mismatch_does_not_publish_or_cleanup(self):
        with Fixture() as server:
            self.invoke(server.url, "--sha256", "0" * 64, expected=4)
            self.assertFalse(self.output.exists())
            self.assertEqual(len(list(self.active.glob("part-*.bin"))), 12)
            self.assertTrue((self.active / "merged.tmp").is_file())

    def test_existing_file_not_overwritten_or_even_requested(self):
        self.output.write_bytes(b"original")
        with Fixture() as server:
            self.invoke(server.url, expected=5)
            self.assertEqual(self.output.read_bytes(), b"original")
            self.assertEqual(server.requests, [])

    def test_overwrite_preserves_original_on_hash_mismatch(self):
        self.output.write_bytes(b"original")
        with Fixture() as server:
            self.invoke(server.url, "--overwrite", "--sha256", "0" * 64, expected=4)
            self.assertEqual(self.output.read_bytes(), b"original")
            self.assertTrue(self.active.exists())

    def test_verified_overwrite(self):
        self.output.write_bytes(b"original")
        with Fixture() as server:
            result, _ = self.invoke(server.url, "--overwrite", "--sha256", SHA)
            self.check_success(result)

    def test_bad_range_never_merged(self):
        with Fixture(bad_range=True) as server:
            self.invoke(server.url, expected=4)
            self.assertFalse(self.output.exists())
            self.assertFalse((self.active / "merged.tmp").exists())

    def test_bad_length_never_merged(self):
        with Fixture(bad_length=True) as server:
            self.invoke(server.url, expected=4)
            self.assertFalse(self.output.exists())

    def test_server_ignoring_ranges_after_probe_stops(self):
        with Fixture(ignore_after_probe=True) as server:
            self.invoke(server.url, expected=5)
            self.assertFalse(self.output.exists())

    def test_version_switch_mid_download_stops(self):
        with Fixture(switch_etag=True) as server:
            self.invoke(server.url, expected=5)
            self.assertFalse(self.output.exists())

    def test_encoded_response_rejected(self):
        with Fixture(encoding="gzip") as server:
            self.invoke(server.url, expected=4)
            self.assertFalse(self.output.exists())

    def test_short_body_keeps_partial_and_no_output(self):
        with Fixture(short_body=True) as server:
            self.invoke(server.url, expected=4)
            self.assertFalse(self.output.exists())
            self.assertGreater(len(list(self.active.glob("*.partial"))), 0)

    def test_only_failed_piece_retried(self):
        with Fixture(failures={CHUNK: "once"}) as server:
            result, _ = self.invoke(server.url, "--retries", "1")
            self.check_success(result)
            ranges = [r["range"] for r in server.requests]
            self.assertEqual(ranges.count("bytes=%d-%d" % (CHUNK, 2 * CHUNK - 1)), 2)
            self.assertEqual(ranges.count("bytes=0-%d" % (CHUNK - 1)), 1)

    def test_retry_limit_is_initial_plus_three(self):
        client = d.Client("http://localhost/file", {}, 1, 3, threading.Event())
        operation = mock.Mock(side_effect=d.DownloadError("temporary", 3, True))
        with mock.patch.object(client.stop, "wait", return_value=False) as wait:
            with self.assertRaises(d.DownloadError):
                client.retry(operation, "test")
        self.assertEqual(operation.call_count, 4)
        self.assertEqual([c.args[0] for c in wait.call_args_list], [1, 2, 4])

    def make_interrupted(self, server):
        server.failures = {CHUNK * 3: "always"}
        self.invoke(server.url, "--threads", "1", expected=3)
        state = json.loads((self.active / "manifest.json").read_text())
        self.assertEqual(len(state["completed"]), 3)
        server.failures = {}
        server.requests.clear()
        return state

    def test_resume_reuses_only_verified_completed_pieces(self):
        with Fixture() as server:
            self.make_interrupted(server)
            result, _ = self.invoke(server.url)
            self.check_success(result)
            self.assertEqual(result["resumed_parts"], 3)
            ranges = [r["range"] for r in server.requests]
            for i in range(3):
                self.assertNotIn("bytes=%d-%d" % (i * CHUNK, (i + 1) * CHUNK - 1), ranges)

    def test_same_size_corrupt_resume_piece_redownloaded(self):
        with Fixture() as server:
            self.make_interrupted(server)
            (self.active / d.part_name(1)).write_bytes(b"X" * CHUNK)
            result, _ = self.invoke(server.url)
            self.check_success(result)
            self.assertEqual(result["resumed_parts"], 2)
            self.assertIn("bytes=%d-%d" % (CHUNK, 2 * CHUNK - 1), [r["range"] for r in server.requests])

    def test_truncated_resume_piece_redownloaded(self):
        with Fixture() as server:
            self.make_interrupted(server)
            (self.active / d.part_name(1)).write_bytes(b"tiny")
            result, _ = self.invoke(server.url)
            self.check_success(result)
            self.assertEqual(result["resumed_parts"], 2)

    def test_changed_remote_resume_refused_and_restart_preserves_old(self):
        with Fixture() as server:
            self.make_interrupted(server)
            saved = (self.active / d.part_name(0)).read_bytes()
            server.etag = '"v2"'
            self.invoke(server.url, expected=5)
            self.assertEqual((self.active / d.part_name(0)).read_bytes(), saved)
            result, _ = self.invoke(server.url, "--restart")
            self.assertEqual(self.output.read_bytes(), PAYLOAD)
            retained = list(self.root.glob("retained-*"))
            self.assertEqual(len(retained), 1)
            self.assertEqual((retained[0] / d.part_name(0)).read_bytes(), saved)
            self.assertTrue(result["temporary_parts_cleaned"])

    def test_changed_chunk_size_refused(self):
        with Fixture() as server:
            self.make_interrupted(server)
            self.invoke(server.url, "--chunk-size", "32KiB", expected=5)
            self.assertFalse(self.output.exists())

    def test_corrupt_manifest_refused(self):
        with Fixture() as server:
            self.make_interrupted(server)
            (self.active / "manifest.json").write_text("bad json")
            self.invoke(server.url, expected=5)
            self.assertFalse(self.output.exists())

    def test_secrets_absent_from_logs_and_manifests(self):
        token = "Bearer unique-secret-123"
        signature = "private-signature-456"
        headers = self.path / "headers.json"
        headers.write_text(json.dumps({"Authorization": token, "Cookie": "private-cookie-789"}))
        with Fixture(auth=token, failures={CHUNK: "always"}) as server:
            result, process = self.invoke(server.url + "?token=" + signature, "--headers-file", str(headers), "--threads", "1", expected=3)
            persisted = (self.active / "manifest.json").read_text()
            for secret in (token, signature, "private-cookie-789"):
                self.assertNotIn(secret, process.stdout + process.stderr + persisted)
            self.assertEqual(server.requests[0]["headers"]["Authorization"], token)

    def test_cross_origin_redirect_strips_custom_and_standard_credentials(self):
        headers = self.path / "headers.json"
        headers.write_text(json.dumps({"Authorization": "Bearer abc", "Cookie": "session=xyz", "X-Api-Key": "secret", "Referer": "private"}))
        with Fixture() as destination:
            with Fixture(redirect=destination.url) as source:
                url = source.url.replace("/file", "/redirect")
                result, _ = self.invoke(url, "--headers-file", str(headers))
                self.check_success(result)
                for request in destination.requests:
                    for key in ("Authorization", "Cookie", "X-Api-Key", "Referer"):
                        self.assertNotIn(key, request["headers"])
                self.assertEqual(source.requests[0]["headers"]["Authorization"], "Bearer abc")

    def test_same_origin_redirect_preserves_credentials(self):
        token = "Bearer same-origin"
        headers = self.path / "headers.json"
        headers.write_text(json.dumps({"Authorization": token}))
        with Fixture(auth=token) as server:
            server.options["redirect"] = server.url
            result, _ = self.invoke(server.url.replace("/file", "/redirect"), "--headers-file", str(headers))
            self.check_success(result)
            self.assertTrue(all(r["headers"].get("Authorization") == token for r in server.requests))

    def test_https_downgrade_redirect_refused(self):
        request = d.urllib.request.Request("https://example.test/file")
        with self.assertRaises(d.DownloadError):
            d.SafeRedirect().redirect_request(request, None, 302, "", {}, "http://example.test/file")

    def test_url_file_download(self):
        with Fixture() as server:
            urlfile = self.path / "url.txt"
            urlfile.write_text(server.url + "?signature=hidden")
            command = self.command(server.url)[0:2] + self.command(server.url)[3:] + ["--url-file", str(urlfile)]
            process = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=10)
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
            self.check_success(json.loads(process.stdout))

    def test_reserved_request_header_rejected(self):
        headers = self.path / "headers.json"
        headers.write_text(json.dumps({"Range": "bytes=10-20"}))
        with Fixture() as server:
            self.invoke(server.url, "--headers-file", str(headers), expected=2)
            self.assertEqual(server.requests, [])

    def test_target_symlink_not_followed(self):
        other = self.path / "other"
        other.write_bytes(b"original")
        try:
            self.output.symlink_to(other)
        except OSError:
            self.skipTest("symlink not available")
        with Fixture() as server:
            self.invoke(server.url, "--overwrite", expected=6)
            self.assertEqual(other.read_bytes(), b"original")

    def test_active_directory_symlink_not_followed(self):
        other = self.path / "outside"
        other.mkdir()
        self.root.mkdir()
        try:
            self.active.symlink_to(other, target_is_directory=True)
        except OSError:
            self.skipTest("symlink not available")
        with Fixture() as server:
            self.invoke(server.url, "--restart", expected=6)
            self.assertEqual(list(other.iterdir()), [])

    def test_concurrent_same_target_rejected(self):
        self.root.mkdir()
        with Fixture() as server, d.OutputLock(self.root / ".lock"):
            self.invoke(server.url, expected=5)
            self.assertEqual(server.requests, [])

    def test_atomic_no_clobber_publish_race(self):
        candidate = self.path / "candidate"
        candidate.write_bytes(b"new")
        self.output.write_bytes(b"created during download")
        with self.assertRaises(d.DownloadError):
            d.publish(candidate, self.output, False)
        self.assertEqual(self.output.read_bytes(), b"created during download")
        self.assertEqual(candidate.read_bytes(), b"new")

    def test_failed_publish_keeps_original_and_candidate(self):
        candidate = self.path / "candidate"
        candidate.write_bytes(b"new")
        self.output.write_bytes(b"original")
        with mock.patch.object(d.os, "replace", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                d.publish(candidate, self.output, True)
        self.assertEqual(self.output.read_bytes(), b"original")
        self.assertEqual(candidate.read_bytes(), b"new")

    def test_merge_detects_same_size_corruption(self):
        self.active.mkdir(parents=True)
        (self.active / d.part_name(0)).write_bytes(b"bad")
        state = {"completed": {"0": {"size": 3, "sha256": hashlib.sha256(b"old").hexdigest()}}}
        with self.assertRaises(d.DownloadError):
            d.merge_parts(self.active, 1, state, threading.Event())
        self.assertFalse(self.output.exists())
        self.assertTrue((self.active / d.part_name(0)).exists())

    def test_final_disk_reread_detects_corruption(self):
        candidate = self.path / "candidate"
        candidate.write_bytes(b"bad")
        with self.assertRaises(d.DownloadError):
            d.verify_candidate(candidate, 3, hashlib.sha256(b"old").hexdigest(), 3, None, threading.Event())
        self.assertTrue(candidate.exists())

    def test_cleanup_error_reports_verified_published_file(self):
        with Fixture() as server:
            args = d.parse_args(self.command(server.url)[2:])
            with mock.patch.object(d.shutil, "rmtree", side_effect=OSError("permission")):
                result = d.run(args, threading.Event())
            self.assertEqual(self.output.read_bytes(), PAYLOAD)
            self.assertFalse(result["temporary_parts_cleaned"])
            self.assertEqual(result["status"], "complete-with-cleanup-warning")
            self.assertTrue(self.active.exists())

    @unittest.skipIf(os.name == "nt", "POSIX signal behavior; Windows tested separately")
    def test_sigint_keeps_checkpoint_and_resumes(self):
        data = PAYLOAD * 4
        with Fixture(data=data, delay=0.08) as server:
            process = subprocess.Popen(self.command(server.url, "--threads", "1"), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
            try:
                deadline = time.monotonic() + 6
                while time.monotonic() < deadline:
                    try:
                        state = json.loads((self.active / "manifest.json").read_text())
                        if len(state["completed"]) >= 2:
                            break
                    except (OSError, ValueError):
                        pass
                    time.sleep(0.02)
                else:
                    self.fail("checkpoint not written in time")
                process.send_signal(signal.SIGINT)
                stdout, stderr = process.communicate(timeout=6)
                self.assertEqual(process.returncode, 130, stdout + stderr)
                self.assertFalse(self.output.exists())
                self.assertTrue(self.active.exists())
                server.options["delay"] = 0
                result, _ = self.invoke(server.url)
                self.check_success(result, data)
                self.assertGreaterEqual(result["resumed_parts"], 2)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

    def tls_fixture(self):
        executable = shutil.which("openssl")
        if executable is None:
            self.skipTest("OpenSSL executable needed only to generate ephemeral test certificates")
        key, cert = self.path / "test-key.pem", self.path / "test-cert.pem"
        config = self.path / "openssl.cnf"
        config.write_text("[req]\ndistinguished_name=dn\nx509_extensions=v3\nprompt=no\n[dn]\nCN=127.0.0.1\n[v3]\nsubjectAltName=IP:127.0.0.1\nbasicConstraints=critical,CA:TRUE\nkeyUsage=digitalSignature,keyEncipherment,keyCertSign\nextendedKeyUsage=serverAuth\n")
        subprocess.run([executable, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                        "-out", str(cert), "-days", "1", "-config", str(config)],
                       check=True, capture_output=True, timeout=15)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert), str(key))
        return context, cert

    def test_https_validated_certificate_parallel_download(self):
        context, cert = self.tls_fixture()
        with Fixture(tls_context=context) as server:
            with mock.patch.dict(os.environ, {"SSL_CERT_FILE": str(cert)}):
                result, _ = self.invoke(server.url, "--sha256", SHA)
            self.check_success(result)
            self.assertEqual(result["mode"], "parallel")

    def test_https_untrusted_certificate_rejected(self):
        context, _ = self.tls_fixture()
        with Fixture(tls_context=context) as server:
            self.invoke(server.url, expected=3)
            self.assertFalse(self.output.exists())
            self.assertEqual(server.requests, [])

    def test_truncated_body_retried_from_start_of_piece(self):
        data = PAYLOAD[:CHUNK * 2]
        with Fixture(data=data, drop_once=True) as server:
            result, _ = self.invoke(server.url, "--retries", "1")
            self.check_success(result, data)
            ranges = [r["range"] for r in server.requests]
            self.assertEqual(ranges.count("bytes=0-%d" % (CHUNK - 1)), 2)
            self.assertEqual(ranges.count("bytes=%d-%d" % (CHUNK, CHUNK * 2 - 1)), 2)

    def test_permission_failure_does_not_publish_or_delete_parts(self):
        with Fixture() as server:
            args = d.parse_args(self.command(server.url)[2:])
            original = d.open_private
            def disk_full(path, truncate=True):
                if path.name == "merged.tmp":
                    raise OSError("disk full")
                return original(path, truncate)
            with mock.patch.object(d, "open_private", side_effect=disk_full):
                with self.assertRaises(OSError):
                    d.run(args, threading.Event())
            self.assertFalse(self.output.exists())
            self.assertEqual(len(list(self.active.glob("part-*.bin"))), 12)

    def test_utf8_json_even_with_legacy_output_encoding(self):
        with Fixture(data=b"small") as server:
            with mock.patch.dict(os.environ, {"PYTHONIOENCODING": "ascii"}):
                result, _ = self.invoke(server.url)
            self.check_success(result, b"small")
            self.assertIn("大小与分片结构", result["note"])

    def test_invalid_thread_count_rejected(self):
        process = subprocess.run(self.command("http://localhost/file", "--threads", "33"), capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(process.returncode, 2)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
