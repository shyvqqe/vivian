from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import discord
import httpx

from app import file_scans
from core import virustotal


BODY = b"attachment fixture"
DIGEST = hashlib.sha256(BODY).hexdigest()
STATS = {"malicious": 4, "suspicious": 1, "undetected": 70, "type-unsupported": 3}


def provider_response(**attributes):
    return httpx.Response(200, json={"data": {"id": DIGEST, "attributes": attributes}})


def attachment(**changes):
    data = dict(id=50, size=len(BODY), filename="fixture.exe", ephemeral=False,
                url="https://cdn.discordapp.com/attachments/10/50/fixture.exe?ex=aaa&hm=bbb")
    return SimpleNamespace(**(data | changes))


def message_for(item, *, channel_type=discord.ChannelType.text, public=True):
    everyone = object()
    member = object()

    def permissions_for(role):
        return discord.Permissions(view_channel=public if role is everyone else True,
                                   send_messages=True, send_messages_in_threads=True, embed_links=True)

    return SimpleNamespace(
        id=90, attachments=[item], guild=SimpleNamespace(me=member, default_role=everyone),
        channel=SimpleNamespace(id=10, type=channel_type, permissions_for=permissions_for),
    )


class VirusTotalClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_lookup_deduplicates_same_digest_and_never_uploads_known_file(self):
        requests = []
        contacted = asyncio.Event()

        async def transport(request):
            requests.append(request)
            contacted.set()
            await asyncio.sleep(0)
            return provider_response(last_analysis_stats=STATS)

        scanner = virustotal.VirusTotal("test-key", per_minute=4, per_day=500)
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            first, second = await asyncio.gather(
                scanner.inspect(client, DIGEST, BODY, may_upload=lambda: True),
                scanner.inspect(client, DIGEST, BODY, may_upload=lambda: True),
            )
            replay = await scanner.inspect(client, DIGEST, BODY, may_upload=lambda: True)
        self.assertTrue(contacted.is_set())
        self.assertEqual(first, second)
        self.assertEqual(first, replay)
        self.assertEqual((first.malicious, first.suspicious, first.examined, first.skipped), (4, 1, 75, 3))
        self.assertEqual(first.url, f"https://www.virustotal.com/gui/file/{DIGEST}")
        self.assertEqual([(r.method, r.url.path) for r in requests], [("GET", f"/api/v3/files/{DIGEST}")])
        self.assertEqual(requests[0].headers["x-apikey"], "test-key")

    async def test_unknown_hash_respects_upload_switch_and_cache(self):
        requests = []

        def transport(request):
            requests.append(request.method)
            return httpx.Response(404)

        scanner = virustotal.VirusTotal("test-key", per_minute=4, per_day=500)
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            first = await scanner.inspect(client, DIGEST, BODY, may_upload=lambda: False)
            second = await scanner.inspect(client, DIGEST, BODY, may_upload=lambda: False)
        self.assertEqual(first.status, "unknown")
        self.assertIsNone(first.url)
        self.assertEqual(first, second)
        self.assertEqual(requests, ["GET"])

    async def test_uploads_unknown_file_and_retrieves_only_completed_analysis(self):
        requests = []
        analysis_id = "ok/a=="

        def transport(request):
            requests.append((request.method, request.url.raw_path))
            if len(requests) == 1:
                return httpx.Response(404)
            if request.method == "POST":
                self.assertIn(BODY, request.content)
                self.assertIn(b'filename="attachment.exe"', request.content)
                return httpx.Response(200, json={"data": {"id": analysis_id}})
            status = "in-progress" if len(requests) == 3 else "completed"
            return httpx.Response(200, json={"data": {"id": analysis_id, "attributes": {
                "status": status, "stats": STATS,
            }}, "meta": {"file_info": {"sha256": DIGEST}}})

        scanner = virustotal.VirusTotal("test-key", per_minute=4, per_day=500)
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            pending = await scanner.inspect(client, DIGEST, BODY, may_upload=lambda: True, suffix=".exe")
            self.assertEqual(pending.status, "pending")
            self.assertEqual(pending.malicious, 0)
            with patch.object(virustotal, "_POLL_DELAYS", (0, 0)):
                completed = await scanner.follow(client, pending)
            replay = await scanner.inspect(client, DIGEST, BODY, may_upload=lambda: True)
        self.assertEqual(completed.status, "complete")
        self.assertEqual(completed, replay)
        self.assertEqual(requests, [
            ("GET", f"/api/v3/files/{DIGEST}".encode()), ("POST", b"/api/v3/files"),
            ("GET", b"/api/v3/analyses/ok%2Fa%3D%3D"), ("GET", b"/api/v3/analyses/ok%2Fa%3D%3D"),
        ])

    async def test_quota_limits_stop_new_calls(self):
        other = "f" * 64
        for status_code, per_day in ((429, 500), (200, 1)):
            with self.subTest(status_code=status_code):
                requests = []

                def transport(request):
                    requests.append(request)
                    if status_code == 429:
                        return httpx.Response(429, headers={"Retry-After": "30"})
                    return provider_response(last_analysis_stats=STATS)

                scanner = virustotal.VirusTotal("test-key", per_minute=4, per_day=per_day)
                async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
                    await scanner.inspect(client, DIGEST, BODY, may_upload=lambda: True)
                    blocked = await scanner.inspect(client, other, BODY, may_upload=lambda: True)
                self.assertEqual(blocked.status, "unavailable")
                self.assertIn("лимит", blocked.message)
                self.assertEqual(len(requests), 1)

    async def test_does_not_attribute_another_files_stats_to_attachment(self):
        def transport(_):
            return httpx.Response(200, json={"data": {"id": "f" * 64, "attributes": {
                "last_analysis_stats": STATS,
            }}})

        scanner = virustotal.VirusTotal("test-key", per_minute=4, per_day=500)
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            report = await scanner.inspect(client, DIGEST, BODY, may_upload=lambda: True)
        self.assertEqual(report.status, "unavailable")
        self.assertIsNone(report.url)


class DiscordAttachmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_streams_exact_original_file_from_discord_with_no_secrets(self):
        item = attachment()

        def transport(request):
            self.assertEqual(str(request.url), item.url)
            self.assertNotIn("x-apikey", request.headers)
            return httpx.Response(200, stream=httpx.ByteStream(BODY))

        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            checked = await file_scans._attachment_bytes(client, message_for(item), item, 1024)
        self.assertEqual(checked, (DIGEST, BODY))

    async def test_rejects_untrusted_urls_and_oversized_files_before_request(self):
        bad = [
            attachment(url="https://cdn.discordapp.com.evil.test/attachments/10/50/file.exe"),
            attachment(url="https://cdn.discordapp.com/attachments/11/50/file.exe"),
            attachment(url="https://cdn.discordapp.com/attachments/10/51/file.exe"),
            attachment(size=2048),
        ]

        def transport(_):
            self.fail("No request should have been made")

        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            for item in bad:
                with self.subTest(item=item), self.assertRaises(file_scans._DownloadError):
                    await file_scans._attachment_bytes(client, message_for(item), item, 1024)

    async def test_rejects_redirects_and_stream_larger_than_attachment_metadata(self):
        responses = [
            httpx.Response(302, headers={"Location": "https://example.com/malicious.exe"}),
            httpx.Response(200, stream=httpx.ByteStream(BODY + b"a")),
        ]
        item = attachment()

        def transport(request):
            self.assertEqual(str(request.url), item.url)
            return responses.pop(0)

        async with httpx.AsyncClient(transport=httpx.MockTransport(transport), follow_redirects=True) as client:
            for _ in range(2):
                with self.assertRaises(file_scans._DownloadError):
                    await file_scans._attachment_bytes(client, message_for(item), item, 1024)
        self.assertEqual(responses, [])

    async def test_background_report_is_delivered_and_updated(self):
        item = attachment(filename="alert.exe")
        message = message_for(item)
        edit = AsyncMock()
        message.channel.get_partial_message = lambda message_id: SimpleNamespace(id=message_id, edit=edit)
        settings = file_scans._Settings(True, None, 1024)
        pending = virustotal.FileReport("pending", DIGEST, "a", "Анализ в процессе")
        finished = virustotal.FileReport("complete", DIGEST, malicious=4, examined=75, skipped=3)
        service = SimpleNamespace(inspect=AsyncMock(return_value=pending), follow=AsyncMock(return_value=finished))
        scans = file_scans._FileScans(settings, service)
        with (patch.object(file_scans, "_attachment_bytes", AsyncMock(return_value=(DIGEST, BODY))),
              patch.object(file_scans.helpers, "_send_message_once", AsyncMock(return_value={"id": "81"})) as send):
            scans.enqueue(message)
            service.inspect.assert_not_called()
            async with asyncio.timeout(5):
                await scans.queue.join()
                await asyncio.gather(*tuple(scans.followups | scans.workers))
        service.inspect.assert_awaited_once()
        self.assertTrue(service.inspect.await_args.kwargs["may_upload"]())
        self.assertTrue(send.await_args.kwargs["components_v2"])
        self.assertIn("Анализ в процессе", send.await_args.kwargs["embed"].description)
        view = edit.await_args.kwargs["view"]
        self.assertIsInstance(view, discord.ui.LayoutView)
        text = "\n".join(item.content for item in view.walk_children() if isinstance(item, discord.ui.TextDisplay))
        self.assertIn("Опасно", text)
        self.assertIn("4/75", text)

    def test_only_public_channels_pass_scope_filter(self):
        item = attachment()
        settings = file_scans._Settings(False, None, 1024)
        good = message_for(item)
        private = message_for(item, channel_type=discord.ChannelType.private_thread)
        private_roles = message_for(item, public=False)
        dm = SimpleNamespace(guild=None, channel=SimpleNamespace(type=discord.ChannelType.private))
        self.assertIsNotNone(file_scans._eligible_channel(good, settings))
        for blocked in (private, private_roles, dm):
            self.assertIsNone(file_scans._eligible_channel(blocked, settings))
        settings = file_scans._Settings(False, frozenset({99}), 1024)
        self.assertIsNone(file_scans._eligible_channel(good, settings))

    def test_empty_and_zero_detection_reports_have_separate_verdicts(self):
        unsupported = virustotal.FileReport("complete", DIGEST, skipped=71)
        clean = virustotal.FileReport("complete", DIGEST, examined=71)
        payload = file_scans._report_payload([("archive.zip", unsupported), ("setup.exe", clean)],
                                              omitted=0, embed_links=False)
        self.assertIn("Недостаточно результатов", payload["content"])
        self.assertIn("Срабатываний нет", payload["content"])
        self.assertIn("не гарантирует безопасность", payload["content"])

    def test_queued_executables_have_priority_and_attachment_count_is_bounded(self):
        photo = message_for(attachment(filename="photo.jpeg"))
        upload = message_for(attachment(filename="setup.ExE"))
        upload.attachments = [attachment(filename=f"photo{i}.jpeg") for i in range(4)] + upload.attachments
        scans = file_scans._FileScans(file_scans._Settings(False, None, 1024), service=None)
        with patch.object(scans, "_start_worker"):
            scans.enqueue(photo)
            scans.enqueue(upload)
        _, _, _, message, selected, omitted = scans.queue.get_nowait()
        self.assertIs(message, upload)
        self.assertEqual(selected[0].filename, "setup.ExE")
        self.assertEqual(len(selected), 3)
        self.assertEqual(omitted, 2)


if __name__ == "__main__":
    unittest.main()
