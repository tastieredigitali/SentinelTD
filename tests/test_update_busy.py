"""Aggiornamento partito sul sito ma senza esito: niente secondo tentativo da REST.

Il 28/10/2026 l'aggiornamento del core di un sito e' rimasto appeso sul download da wordpress.org.
Sentinel aspettava 180 secondi, poi riprovava da REST mentre il sito stava ancora aggiornando:
WordPress rispondeva "[locked]" e il motivo vero (PHP fermato da php-fpm a 300 secondi) spariva.
Ora: si aspetta piu' del limite del server, e se la richiesta e' arrivata a PHP senza esito
(nessuna risposta, 502/504) si riporta questo, senza secondo tentativo e senza altri aggiornamenti
su quel sito nello stesso giro."""
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

import test_screenshot_schedule as fixtures
from app import connectors, worker
from app.models import Site


def site():
    return Site(id=993, name="Prova", url="https://prova.example", cms="wp", token="t", backup_mode="")


class UpdateOneTests(unittest.IsolatedAsyncioTestCase):
    async def update(self, ajax):
        calls = []

        def handler(request: httpx.Request):
            path = request.url.path
            calls.append("ajax" if path.endswith("admin-ajax.php") else "rest")
            if path.endswith("admin-ajax.php"):
                return ajax(request)
            return httpx.Response(200, json={"ok": False, "error": "Attualmente è in corso un altro aggiornamento. [locked]"})

        real = httpx.AsyncClient
        with patch.object(worker.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)), \
             patch("app.settings_store.get_operational_settings", AsyncMock(return_value={"pre_update_backup": True})):
            res = await worker._update_one(site(), "core", "wordpress", "7.1.4")
        return res, calls

    async def test_no_answer_in_time_is_not_retried_from_rest(self):
        def ajax(request):
            raise httpx.ReadTimeout("timed out", request=request)
        res, calls = await self.update(ajax)
        self.assertEqual(calls, ["ajax"])                       # niente REST: troverebbe "[locked]"
        self.assertFalse(res["ok"])
        self.assertTrue(res["busy"])
        self.assertIn("nessuna risposta dal sito in 330 secondi", res["error"])
        self.assertIn("15 minuti", res["error"])               # core: WordPress lo tiene bloccato
        self.assertNotIn("locked", res["error"])

    async def test_server_stopped_php_is_not_retried_from_rest(self):
        res, calls = await self.update(lambda request: httpx.Response(504, text="Gateway Time-out"))
        self.assertEqual(calls, ["ajax"])
        self.assertTrue(res["busy"])
        self.assertIn("HTTP 504", res["error"])

    async def test_old_connector_still_falls_back_to_rest(self):
        # admin-ajax risponde "0" (connettore vecchio): la richiesta non ha fatto niente, REST va bene
        res, calls = await self.update(lambda request: httpx.Response(200, text="0"))
        self.assertEqual(calls, ["ajax", "rest"])
        self.assertFalse(res.get("busy", False))

    async def test_unreachable_backend_still_falls_back_to_rest(self):
        def ajax(request):
            raise httpx.ConnectError("refused", request=request)
        res, calls = await self.update(ajax)
        self.assertEqual(calls, ["ajax", "rest"])

    async def test_both_attempts_are_reported_when_the_second_fails_too(self):
        # il backend risponde 503, REST risponde "[locked]": nell'errore ci sono tutti e due
        res, calls = await self.update(lambda request: httpx.Response(503, text="Service Unavailable"))
        self.assertEqual(calls, ["ajax", "rest"])
        self.assertIn("[locked]", res["error"])
        self.assertIn("Primo tentativo, dal backend del sito (admin-ajax): HTTP 503", res["error"])

    async def test_redirect_and_garbage_from_the_backend_are_named(self):
        res, _c = await self.update(lambda request: httpx.Response(302, headers={"location": "https://prova.example/login"}))
        self.assertIn("reindirizzamento (HTTP 302) verso prova.example", res["error"])
        res, _c = await self.update(lambda request: httpx.Response(200, text="<html>Errore   critico</html>"))
        self.assertIn("risposta non valida (HTTP 200): Errore critico", res["error"])

    async def test_old_connector_adds_no_note(self):
        res, _c = await self.update(lambda request: httpx.Response(200, text="0"))
        self.assertNotIn("Primo tentativo", res["error"])

    async def update_405(self, current):
        """Backend e REST rispondono 405; alla rilettura il sito dichiara `current`."""
        calls = []

        def handler(request: httpx.Request):
            calls.append("ajax" if request.url.path.endswith("admin-ajax.php") else "rest")
            return httpx.Response(405, headers={"server": "Apache"},
                                  text="<html><style>b{}</style><h1>Method Not Allowed</h1><p>Richiesta   rifiutata</p></html>")

        async def status(s, force=False, **kw):
            return {"cms": "wp", "core": {"current": "7.1.5"},
                    "extensions": [{"type": "plugin", "slug": "code-snippets", "current": current},
                                   {"type": "theme", "slug": "code-snippets", "current": "4.1.1"}]}

        real = httpx.AsyncClient
        with patch.object(worker.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw)), \
             patch("app.settings_store.get_operational_settings", AsyncMock(return_value={"pre_update_backup": True})), \
             patch.object(connectors, "fetch_status", status), patch.object(worker, "RECHECK_PAUSE", 0):
            res = await worker._update_one(site(), "plugin", "code-snippets", "4.1.1")
        return res, calls

    async def test_http_error_but_the_site_is_at_the_expected_version(self):
        # 26/11/2026: il connettore aveva aggiornato, il server ha risposto 405 a tutte e due
        res, calls = await self.update_405("4.1.1")
        self.assertEqual(calls, ["ajax", "rest"])
        self.assertTrue(res["ok"])
        self.assertEqual(res["new"], "4.1.1")
        self.assertEqual(res["confirmed"], "HTTP 405")

    async def test_http_error_and_the_site_is_still_at_the_old_version(self):
        res, _c = await self.update_405("4.1.0")      # il tema omonimo alla 4.1.1 non conta
        self.assertFalse(res["ok"])
        self.assertIn("il server del sito ha risposto HTTP 405 Method Not Allowed (server: Apache): "
                      "Method Not Allowed Richiesta rifiutata", res["error"])
        self.assertIn("Primo tentativo, dal backend del sito (admin-ajax): HTTP 405", res["error"])
        self.assertNotIn("mozilla", res["error"])

    def test_waits_longer_than_the_server_limit(self):
        self.assertGreater(worker.UPDATE_WAIT, 300)


class UpdateLoopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await fixtures.SchedulerTests.asyncSetUp(self)

    async def asyncTearDown(self):
        await fixtures.SchedulerTests.asyncTearDown(self)

    async def test_nothing_else_on_the_site_after_an_update_without_outcome(self):
        sid = await fixtures.SchedulerTests.add_site(self, status="ok", auto_update=True, cms="wp")
        status = {"cms": "wp", "core": {"current": "7.1.3", "latest": "7.1.3", "update": False},
                  "extensions": [
                      {"type": "plugin", "name": "Rank Math", "slug": "seo-by-rank-math",
                       "current": "1.0.270", "new": "1.0.271", "update": True},
                      {"type": "plugin", "name": "Yoast SEO", "slug": "wordpress-seo",
                       "current": "28.6", "new": "28.7", "update": True},
                  ]}

        async def fetch(s, force=False, **kw):
            return status

        busy = worker._busy_result(site(), "plugin", "nessuna risposta dal sito in 330 secondi")
        upd = AsyncMock(return_value=busy)
        with patch.object(connectors, "fetch_status", fetch), patch.object(worker, "_update_one", upd), \
             patch.object(worker, "_visual_shot", AsyncMock(return_value=None)), \
             patch.object(worker, "notify_dispatch", AsyncMock()), patch.object(worker.asyncio, "sleep", AsyncMock()):
            await worker._update_site({"redis": self.redis}, sid, False, {})
        self.assertEqual(upd.await_count, 1)                   # il secondo plugin resta al prossimo giro
        cycle = [json.loads(x) for x in await self.redis.lrange("tg:cycle:report", 0, -1)]
        self.assertEqual([f["name"] for f in cycle[0]["failed"]], ["Rank Math"])

    async def test_update_confirmed_by_rereading_is_a_success_with_a_note(self):
        sid = await fixtures.SchedulerTests.add_site(self, status="ok", auto_update=True, cms="wp")
        status = {"cms": "wp", "core": {"current": "7.1.3", "latest": "7.1.3", "update": False},
                  "extensions": [{"type": "plugin", "name": "Code Snippets", "slug": "code-snippets",
                                  "current": "4.1.0", "new": "4.1.1", "update": True}]}

        async def fetch(s, force=False, **kw):
            return status

        res = {"ok": True, "error": "", "new": "4.1.1", "noop": False, "message": "", "manual": False,
               "reason": "", "backup": "", "backup_error": "", "confirmed": "HTTP 405"}
        notify = AsyncMock()
        with patch.object(connectors, "fetch_status", fetch), patch.object(worker, "_update_one", AsyncMock(return_value=res)), \
             patch.object(worker, "_visual_shot", AsyncMock(return_value=None)), \
             patch.object(worker, "notify_dispatch", notify), patch.object(worker.asyncio, "sleep", AsyncMock()):
            await worker._update_site({"redis": self.redis}, sid, False, {})
        cycle = [json.loads(x) for x in await self.redis.lrange("tg:cycle:report", 0, -1)]
        self.assertEqual(cycle[0].get("failed") or [], [])
        self.assertIn("Code Snippets (esito non arrivato, HTTP 405: confermato rileggendo il sito)", json.dumps(cycle[0], ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
