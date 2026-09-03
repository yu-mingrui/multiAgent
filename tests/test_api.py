import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from langgraph.checkpoint.redis import RedisSaver
from langchain_core.messages import HumanMessage

import Director
from api import app


class DirectorApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_director_uses_user_and_session_for_thread_id(self):
        with patch("api.graph.invoke") as mock_invoke:
            mock_invoke.return_value = {"messages": [type("Message", (), {"content": "ok"})()]}

            response = self.client.post(
                "/api/director",
                json={"query": "你好", "user_id": "u-1", "session_id": "s-1"},
            )

            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertEqual(body["user_id"], "u-1")
            self.assertEqual(body["session_id"], "s-1")
            self.assertEqual(body["thread_id"], "user:u-1:session:s-1")
            self.assertEqual(mock_invoke.call_args.args[1]["configurable"]["thread_id"], "user:u-1:session:s-1")

    def test_director_legacy_request_without_identity_still_works(self):
        with patch("api.graph.invoke") as mock_invoke:
            mock_invoke.return_value = {"messages": [type("Message", (), {"content": "legacy"})()]}

            response = self.client.post("/api/director", json={"query": "测试"})

            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertEqual(body["query"], "测试")
            self.assertEqual(body["response"], "legacy")
            self.assertIsNone(body["user_id"])
            self.assertIsNone(body["session_id"])
            self.assertTrue(body["thread_id"])

    def test_director_reads_user_and_session_from_headers(self):
        with patch("api.graph.invoke") as mock_invoke:
            mock_invoke.return_value = {"messages": [type("Message", (), {"content": "header-ok"})()]}

            response = self.client.post(
                "/api/director",
                json={"query": "你好"},
                headers={"X-User-Id": "u-2", "X-Session-Id": "s-2"},
            )

            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertEqual(body["user_id"], "u-2")
            self.assertEqual(body["session_id"], "s-2")
            self.assertEqual(body["thread_id"], "user:u-2:session:s-2")
            self.assertEqual(mock_invoke.call_args.args[1]["configurable"]["thread_id"], "user:u-2:session:s-2")

    def test_cors_preflight_is_allowed(self):
        response = self.client.options(
            "/api/director",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "X-User-Id, X-Session-Id, Content-Type",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("*", response.headers.get("access-control-allow-origin", ""))
        self.assertIn("POST", response.headers.get("access-control-allow-methods", ""))

    def test_director_returns_graceful_error_when_model_request_fails(self):
        with patch("api.graph.invoke", side_effect=Exception("Access denied, please make sure your account is in good standing.")):
            response = self.client.post(
                "/api/director",
                json={"query": "你好", "user_id": "u-3", "session_id": "s-3"},
            )

            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertEqual(body["user_id"], "u-3")
            self.assertEqual(body["session_id"], "s-3")
            self.assertIn("Access denied", body["response"])

    def test_supervisor_reclassifies_latest_question_in_multiturn_session(self):
        mock_llm = unittest.mock.Mock()
        mock_llm.invoke.return_value = type("Resp", (), {"content": "couplet"})()

        with patch.object(Director.local_setting, "qwen", mock_llm):
            state = {
                "messages": [HumanMessage("讲个笑话"), HumanMessage("上联：金榜题名时")],
                "type": "joke",
            }
            with patch("Director.get_stream_writer", return_value=lambda event: None):
                result = Director.supervisor_node(state)
            self.assertEqual(result["type"], "couplet")

    def test_graph_uses_redis_checkpoint_for_cross_process_session(self):
        self.assertIsInstance(Director.graph.checkpointer, RedisSaver)


if __name__ == "__main__":
    unittest.main()
