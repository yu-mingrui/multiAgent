import unittest
import json
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from langgraph.checkpoint.redis import AsyncRedisSaver
from langchain_core.messages import AIMessage, HumanMessage

import Director
import api
from api import app


async def async_stream(items):
    for item in items:
        yield item


class DirectorApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def read_events(self, response):
        events = []
        for block in response.text.strip().split("\n\n"):
            lines = block.splitlines()
            event = next(line.removeprefix("event: ") for line in lines if line.startswith("event: "))
            data = next(line.removeprefix("data: ") for line in lines if line.startswith("data: "))
            events.append((event, json.loads(data)))
        return events

    def test_director_uses_user_and_session_for_thread_id(self):
        checkpoint = type("Checkpoint", (), {"values": {"messages": []}})()
        with patch("api.graph.aget_state", new_callable=AsyncMock, return_value=checkpoint), patch(
            "api.graph.astream", return_value=async_stream([{"messages": [AIMessage("ok")]}])
        ) as mock_stream:

            response = self.client.post(
                "/api/director",
                json={"query": "你好", "user_id": "u-1", "session_id": "s-1"},
            )

            self.assertEqual(response.status_code, 200)
            events = self.read_events(response)
            self.assertEqual(response.headers["content-type"], "text/event-stream; charset=utf-8")
            self.assertEqual([event[0] for event in events], ["answer", "done"])
            self.assertEqual(events[0][1]["delta"], "ok")
            self.assertEqual(mock_stream.call_args.args[1]["configurable"]["thread_id"], "user:u-1:session:s-1")

    def test_director_legacy_request_without_identity_still_works(self):
        checkpoint = type("Checkpoint", (), {"values": {"messages": []}})()
        with patch("api.graph.aget_state", new_callable=AsyncMock, return_value=checkpoint), patch(
            "api.graph.astream", return_value=async_stream([{"messages": [AIMessage("legacy")]}])
        ):

            response = self.client.post("/api/director", json={"query": "测试"})

            self.assertEqual(response.status_code, 200)
            events = self.read_events(response)
            self.assertEqual(events[0][1]["delta"], "legacy")
            self.assertEqual(events[1][0], "done")

    def test_director_reads_user_and_session_from_headers(self):
        checkpoint = type("Checkpoint", (), {"values": {"messages": []}})()
        with patch("api.graph.aget_state", new_callable=AsyncMock, return_value=checkpoint), patch(
            "api.graph.astream", return_value=async_stream([{"messages": [AIMessage("header-ok")]}])
        ) as mock_stream:

            response = self.client.post(
                "/api/director",
                json={"query": "你好"},
                headers={"X-User-Id": "u-2", "X-Session-Id": "s-2"},
            )

            self.assertEqual(response.status_code, 200)
            events = self.read_events(response)
            self.assertEqual(events[0][1]["delta"], "header-ok")
            self.assertEqual(mock_stream.call_args.args[1]["configurable"]["thread_id"], "user:u-2:session:s-2")

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

    def test_lists_persisted_sessions_for_user(self):
        store = type("Store", (), {"hvals": AsyncMock(return_value=[json.dumps({
            "session_id": "s-1",
            "title": "讲个笑话",
            "preview": "这是一个笑话",
            "updated_at": "2026-09-17T10:00:00",
        }, ensure_ascii=False)])})()
        with patch.object(api, "session_store", store):
            response = self.client.get("/api/sessions", params={"user_id": "u-1"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["sessions"][0]["session_id"], "s-1")
        store.hvals.assert_awaited_once_with("director:sessions:u-1")

    def test_gets_messages_for_persisted_session(self):
        checkpoint = type("Checkpoint", (), {"values": {
            "messages": [HumanMessage("讲个笑话"), AIMessage("程序员为什么戴眼镜？因为看代码不清。")]
        }})()
        with patch.object(api.graph, "aget_state", new_callable=AsyncMock, return_value=checkpoint) as get_state:
            response = self.client.get("/api/sessions/s-1", params={"user_id": "u-1"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["messages"], [
            {"role": "user", "content": "讲个笑话"},
            {"role": "assistant", "content": "程序员为什么戴眼镜？因为看代码不清。"},
        ])
        self.assertEqual(get_state.call_args.args[0]["configurable"]["thread_id"], "user:u-1:session:s-1")

    def test_deletes_session_checkpoint_and_index(self):
        store = type("Store", (), {"hdel": AsyncMock()})()
        with patch.object(api, "session_store", store), patch.object(
            api.checkpointer, "adelete_thread", new_callable=AsyncMock
        ) as delete_thread:
            response = self.client.delete("/api/sessions/s-1", params={"user_id": "u-1"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"deleted": True, "session_id": "s-1"})
        delete_thread.assert_awaited_once_with("user:u-1:session:s-1")
        store.hdel.assert_awaited_once_with("director:sessions:u-1", "s-1")

    def test_session_title_stays_first_question(self):
        store = type("Store", (), {
            "hget": AsyncMock(return_value=json.dumps({"title": "第一次提问"}, ensure_ascii=False)),
            "hset": AsyncMock(),
        })()
        with patch.object(api, "session_store", store):
            import asyncio
            asyncio.run(api.save_session_metadata("u-1", "s-1", "第二次追问", "最新回答"))

        metadata = json.loads(store.hset.call_args.args[2])
        self.assertEqual(metadata["title"], "第一次提问")
        self.assertEqual(metadata["preview"], "最新回答")

    def test_director_returns_graceful_error_when_model_request_fails(self):
        checkpoint = type("Checkpoint", (), {"values": {"messages": []}})()
        with patch("api.graph.aget_state", new_callable=AsyncMock, return_value=checkpoint), patch(
            "api.graph.astream", side_effect=Exception("Access denied, please make sure your account is in good standing.")
        ):
            response = self.client.post(
                "/api/director",
                json={"query": "你好", "user_id": "u-3", "session_id": "s-3"},
            )

            self.assertEqual(response.status_code, 200)
            event, body = self.read_events(response)[-1]
            self.assertEqual(event, "error")
            self.assertEqual(body["error"], "model_request_failed")
            self.assertEqual(body["message"], "模型暂时无法回答，请稍后重试。")

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

    def test_supervisor_reuses_couplet_for_retry_feedback(self):
        mock_llm = unittest.mock.Mock()
        mock_llm.invoke.return_value = type("Resp", (), {"content": "couplet"})()

        with patch.object(Director.local_setting, "qwen", mock_llm):
            state = {
                "messages": [
                    HumanMessage("上联：春风得意马蹄疾"),
                    Director.AIMessage("下联：一日看尽长安花"),
                    HumanMessage("这个不好，再对一个"),
                ],
                "type": "couplet",
            }
            with patch("Director.get_stream_writer", return_value=lambda event: None):
                result = Director.supervisor_node(state)

        self.assertEqual(result["type"], "couplet")
        prompt = mock_llm.invoke.call_args.args[0][1]["content"]
        self.assertIn("上联：春风得意马蹄疾", prompt)
        self.assertIn("这个不好，再对一个", prompt)

    def test_supervisor_reuses_couplet_after_completed_turn(self):
        mock_llm = unittest.mock.Mock()
        mock_llm.invoke.return_value = type("Resp", (), {"content": "couplet"})()

        with patch.object(Director.local_setting, "qwen", mock_llm):
            state = {
                "messages": [
                    HumanMessage("上联：春风得意马蹄疾"),
                    Director.AIMessage("下联：一日看尽长安花"),
                    HumanMessage("再换一个"),
                ],
                "type": Director.END,
            }
            with patch("Director.get_stream_writer", return_value=lambda event: None):
                result = Director.supervisor_node(state)

        self.assertEqual(result["type"], "couplet")
        self.assertTrue(mock_llm.invoke.called)

    def test_supervisor_reuses_travel_for_transport_followup(self):
        mock_llm = unittest.mock.Mock()
        mock_llm.invoke.return_value = type("Resp", (), {"content": "travel"})()

        with patch.object(Director.local_setting, "qwen", mock_llm):
            state = {
                "messages": [
                    HumanMessage("从北京到郑州的坐车路线"),
                    Director.AIMessage("可以乘坐高铁前往"),
                    HumanMessage("有大巴吗"),
                ],
                "type": Director.END,
            }
            with patch("Director.get_stream_writer", return_value=lambda event: None):
                result = Director.supervisor_node(state)

        self.assertEqual(result["type"], "travel")
        prompt = mock_llm.invoke.call_args.args[0][1]["content"]
        self.assertIn("从北京到郑州的坐车路线", prompt)
        self.assertIn("有大巴吗", prompt)

    def test_supervisor_updates_summary_after_long_conversation(self):
        mock_llm = unittest.mock.Mock()
        mock_llm.invoke.return_value = type("Resp", (), {"content": "用户正在规划北京到郑州的出行。"})()
        messages = []
        for index in range(7):
            messages.extend([HumanMessage(f"问题{index}"), Director.AIMessage(f"回答{index}")])

        with patch.object(Director.local_setting, "qwen", mock_llm):
            state = {"messages": messages, "type": "travel"}
            with patch("Director.get_stream_writer", return_value=lambda event: None):
                result = Director.supervisor_node(state)

        self.assertEqual(result["type"], Director.END)
        self.assertEqual(result["conversation_summary"], "用户正在规划北京到郑州的出行。")
        self.assertIn("问题0", mock_llm.invoke.call_args.args[0][1]["content"])

    def test_graph_uses_redis_checkpoint_for_cross_process_session(self):
        self.assertIsInstance(Director.graph.checkpointer, AsyncRedisSaver)


if __name__ == "__main__":
    unittest.main()
