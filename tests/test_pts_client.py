import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from services import pts_client


class PtsGraphqlRetryTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _client_with_responses(*responses):
        client = AsyncMock()
        client.post = AsyncMock(side_effect=responses)
        context = AsyncMock()
        context.__aenter__.return_value = client
        context.__aexit__.return_value = None
        return context, client

    async def test_retries_gateway_deadline_error(self):
        timed_out = MagicMock(status_code=200)
        timed_out.json.return_value = {
            "errors": [{
                "message": 'Post "http://pipeline.babysitter:3030/query": '
                           "context deadline exceeded "
                           "(Client.Timeout exceeded while awaiting headers)"
            }]
        }
        success = MagicMock(status_code=200)
        success.json.return_value = {"data": {"ok": True}}
        context, client = self._client_with_responses(timed_out, success)

        with patch.object(pts_client.httpx, "AsyncClient", return_value=context), \
             patch.object(pts_client, "_rate_limit", new_callable=AsyncMock), \
             patch.object(pts_client.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            result = await pts_client.pts_graphql_query("{ ok }", max_retries=1)

        self.assertEqual(result, {"ok": True})
        self.assertEqual(client.post.await_count, 2)
        sleep.assert_awaited_once_with(5)

    async def test_does_not_retry_business_graphql_error(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "errors": [{"message": "validation failed: invalid order id"}]
        }
        context, client = self._client_with_responses(response)

        with patch.object(pts_client.httpx, "AsyncClient", return_value=context), \
             patch.object(pts_client, "_rate_limit", new_callable=AsyncMock), \
             patch.object(pts_client.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            with self.assertRaisesRegex(RuntimeError, "validation failed"):
                await pts_client.pts_graphql_query("{ bad }", max_retries=3)

        self.assertEqual(client.post.await_count, 1)
        sleep.assert_not_awaited()

    async def test_retries_http_503(self):
        unavailable = MagicMock(status_code=503, text="upstream unavailable")
        success = MagicMock(status_code=200)
        success.json.return_value = {"data": {"ok": True}}
        context, client = self._client_with_responses(unavailable, success)

        with patch.object(pts_client.httpx, "AsyncClient", return_value=context), \
             patch.object(pts_client, "_rate_limit", new_callable=AsyncMock), \
             patch.object(pts_client.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            result = await pts_client.pts_graphql_query("{ ok }", max_retries=1)

        self.assertEqual(result, {"ok": True})
        self.assertEqual(client.post.await_count, 2)
        sleep.assert_awaited_once_with(5)
