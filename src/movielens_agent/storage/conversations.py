"""Persistent chat requests and invocation evidence; no network-held transactions."""
import json
from uuid import uuid4

from ..governance.config import canonical
from .tasks import TaskStore, now


class ConversationConflict(ValueError):
    pass


class ConversationBusy(ValueError):
    pass


class ConversationStore(TaskStore):
    def initialize(self):
        with self.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS chat_sessions (
                    session_id TEXT PRIMARY KEY, title TEXT NOT NULL,
                    active_task_id TEXT, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS chat_requests (
                    request_uid TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES chat_sessions,
                    request_id TEXT NOT NULL, payload TEXT NOT NULL, task_id TEXT,
                    status TEXT NOT NULL, response TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(session_id, request_id));
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_message
                    ON chat_requests(session_id) WHERE status='processing';
                CREATE TABLE IF NOT EXISTS chat_messages (
                    message_id TEXT PRIMARY KEY, request_uid TEXT NOT NULL REFERENCES chat_requests,
                    session_id TEXT NOT NULL REFERENCES chat_sessions,
                    role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS chat_tool_calls (
                    call_id TEXT PRIMARY KEY, request_uid TEXT NOT NULL REFERENCES chat_requests,
                    tool_name TEXT NOT NULL, request_key TEXT NOT NULL,
                    arguments TEXT NOT NULL, status TEXT NOT NULL, result TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS chat_model_calls (
                    call_id TEXT PRIMARY KEY, request_uid TEXT NOT NULL REFERENCES chat_requests,
                    round INTEGER NOT NULL, request TEXT NOT NULL, response TEXT,
                    status TEXT NOT NULL, error TEXT, duration_ms INTEGER,
                    created_at TEXT NOT NULL);
            """)

    def create_session(self, title="新会话", session_id=None):
        session_id = session_id or uuid4().hex
        with self.connect() as conn:
            conn.execute("INSERT OR IGNORE INTO chat_sessions VALUES (?,?,NULL,?)",
                         (session_id, title, now()))
        return self.session(session_id)

    def session(self, session_id):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM chat_sessions WHERE session_id=?", (session_id,)).fetchone()
        if row is None:
            raise KeyError("Conversation does not exist.")
        return dict(row)

    def begin(self, session_id, request_id, content, task_id=None, require_quality=False):
        payload = canonical({"content": content, "task_id": task_id, "require_quality": require_quality})
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            session = conn.execute("SELECT * FROM chat_sessions WHERE session_id=?", (session_id,)).fetchone()
            if session is None:
                raise KeyError("Conversation does not exist.")
            existing = conn.execute(
                "SELECT * FROM chat_requests WHERE session_id=? AND request_id=?",
                (session_id, request_id)).fetchone()
            if existing:
                if existing["payload"] != payload:
                    raise ConversationConflict("同一 request_id 已对应不同的消息或任务。")
                if existing["status"] == "processing":
                    raise ConversationBusy("这条消息正在处理，请等待或查询消息记录。")
                return dict(existing), False
            if conn.execute("SELECT 1 FROM chat_requests WHERE session_id=? AND status='processing'",
                            (session_id,)).fetchone():
                raise ConversationBusy("当前会话仍在处理上一条消息。")
            target = task_id or session["active_task_id"]
            if target and not conn.execute("SELECT 1 FROM tasks WHERE task_id=? AND session_id=?",
                                           (target, session_id)).fetchone():
                raise KeyError("Task is not visible in this conversation.")
            uid, stamp = uuid4().hex, now()
            conn.execute("INSERT INTO chat_requests VALUES (?,?,?,?,?,'processing',NULL,?,?)",
                         (uid, session_id, request_id, payload, target, stamp, stamp))
            conn.execute("INSERT INTO chat_messages VALUES (?,?,?,'user',?,?)",
                         (uid, uid, session_id, content, stamp))
            return dict(conn.execute("SELECT * FROM chat_requests WHERE request_uid=?", (uid,)).fetchone()), True

    def history(self, session_id, max_pairs=8):
        self.session(session_id)
        with self.connect() as conn:
            requests = conn.execute(
                "SELECT request_uid FROM chat_requests WHERE session_id=? AND status!='processing' "
                "ORDER BY created_at DESC LIMIT ?", (session_id, max_pairs)).fetchall()
            result = []
            for request in reversed(requests):
                rows = conn.execute("SELECT role,content FROM chat_messages WHERE request_uid=? "
                                    "ORDER BY created_at", (request[0],)).fetchall()
                result.extend(dict(row) for row in rows)
        return result

    def start_tool(self, request_uid, name, request_key, arguments):
        call_id = uuid4().hex
        with self.connect() as conn:
            conn.execute("INSERT INTO chat_tool_calls VALUES (?,?,?,?,?,'started',NULL,?,?)",
                         (call_id, request_uid, name, request_key, canonical(arguments), now(), now()))
        return call_id

    def finish_tool(self, call_id, result):
        with self.connect() as conn:
            conn.execute("UPDATE chat_tool_calls SET status=?,result=?,updated_at=? WHERE call_id=?",
                         (result["status"], canonical(result), now(), call_id))
            if result.get("task_ref"):
                conn.execute(
                    "UPDATE chat_sessions SET active_task_id=? WHERE session_id=("
                    "SELECT r.session_id FROM chat_requests r JOIN chat_tool_calls c "
                    "ON c.request_uid=r.request_uid WHERE c.call_id=?)",
                    (result["task_ref"]["task_id"], call_id))

    def start_model(self, request_uid, round_number, payload):
        call_id = uuid4().hex
        with self.connect() as conn:
            conn.execute("INSERT INTO chat_model_calls VALUES (?,?,?,?,NULL,'started',NULL,NULL,?)",
                         (call_id, request_uid, round_number, canonical(payload), now()))
        return call_id

    def finish_model(self, call_id, response=None, error=None, duration_ms=None):
        with self.connect() as conn:
            conn.execute("UPDATE chat_model_calls SET response=?,status=?,error=?,duration_ms=? WHERE call_id=?",
                         (canonical(response) if response else None, "failed" if error else "completed",
                          error, duration_ms, call_id))

    def finish(self, request_uid, content, error=None, origin=None):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            request = conn.execute("SELECT * FROM chat_requests WHERE request_uid=?", (request_uid,)).fetchone()
            if request["response"]:
                return json.loads(request["response"])
            calls = conn.execute("SELECT * FROM chat_tool_calls WHERE request_uid=? ORDER BY created_at",
                                 (request_uid,)).fetchall()
            task_ids = [request["task_id"]] if request["task_id"] else []
            evidence, trace = {}, []
            for call in calls:
                result = json.loads(call["result"]) if call["result"] else {}
                if result.get("task_ref"):
                    task_ids.append(result["task_ref"]["task_id"])
                for ref in result.get("evidence", []):
                    evidence[(ref["artifact_id"], ref["version"])] = ref
                trace.append({"call_id": call["call_id"], "name": call["tool_name"], "status": call["status"]})
            models = conn.execute("SELECT call_id,status,response,error,duration_ms FROM chat_model_calls "
                                  "WHERE request_uid=? ORDER BY round", (request_uid,)).fetchall()
            model_trace = []
            for model in models:
                result = json.loads(model["response"]) if model["response"] else {}
                model_trace.append({"call_id": model["call_id"], "status": model["status"],
                                    "error": model["error"], "duration_ms": model["duration_ms"],
                                    "attempts": result.get("attempts", [])})
            reply_id, stamp = uuid4().hex, now()
            response = {"request_id": request["request_id"], "message_id": reply_id,
                        "session_id": request["session_id"], "status": "failed" if error else "completed",
                        "content": content, "error": error,
                        "response_origin": origin or ("application_error" if error else "model"), "task_ids": list(dict.fromkeys(task_ids)),
                        "evidence": list(evidence.values()), "tool_calls": trace, "model_calls": model_trace}
            conn.execute("INSERT INTO chat_messages VALUES (?,?,?,'assistant',?,?)",
                         (reply_id, request_uid, request["session_id"], content, stamp))
            conn.execute("UPDATE chat_requests SET status=?,response=?,updated_at=? WHERE request_uid=?",
                         (response["status"], canonical(response), stamp, request_uid))
            return response

    def messages(self, session_id, offset=0, limit=100):
        self.session(session_id)
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT m.*,r.status,r.response,r.task_id FROM chat_messages m JOIN chat_requests r "
                "ON r.request_uid=m.request_uid WHERE m.session_id=? ORDER BY m.created_at LIMIT ? OFFSET ?",
                (session_id, limit, offset)).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            response = item.pop("response")
            item["metadata"] = json.loads(response) if response and item["role"] == "assistant" else {}
            items.append(item)
        return items

    def calls(self, session_id, message_id):
        with self.connect() as conn:
            row = conn.execute("SELECT request_uid FROM chat_messages WHERE message_id=? AND session_id=?",
                               (message_id, session_id)).fetchone()
            if not row:
                raise KeyError("Message is not visible in this conversation.")
            calls = conn.execute("SELECT * FROM chat_tool_calls WHERE request_uid=? ORDER BY created_at",
                                 (row[0],)).fetchall()
        return [dict(call) | {"arguments": json.loads(call["arguments"]),
                              "result": json.loads(call["result"]) if call["result"] else None} for call in calls]

    def model_calls(self, session_id, message_id):
        with self.connect() as conn:
            row = conn.execute("SELECT request_uid FROM chat_messages WHERE message_id=? AND session_id=?",
                               (message_id, session_id)).fetchone()
            if not row:
                raise KeyError("Message is not visible in this conversation.")
            calls = conn.execute("SELECT * FROM chat_model_calls WHERE request_uid=? ORDER BY round",
                                 (row[0],)).fetchall()
        return [dict(call) | {"request": json.loads(call["request"]),
                              "response": json.loads(call["response"]) if call["response"] else None}
                for call in calls]

    def recover_interrupted_messages(self):
        """Only the API process holding the database's server lock may call this."""
        with self.connect() as conn:
            pending = conn.execute("SELECT * FROM chat_requests WHERE status='processing'").fetchall()
        for request in pending:
            with self.connect() as conn:
                calls = conn.execute("SELECT * FROM chat_tool_calls WHERE request_uid=? AND status='started'",
                                     (request["request_uid"],)).fetchall()
            for call in calls:
                # A crash between task submission and result recording must not
                # hide an accepted task or cause it to be submitted again.
                with self.connect() as conn:
                    task = conn.execute("SELECT task_id FROM tasks WHERE session_id=? AND request_id=?",
                                        (request["session_id"], call["request_key"])).fetchone()
                if task:
                    self.finish_tool(call["call_id"], {"status": "accepted",
                        "task_ref": {"task_id": task[0]}, "evidence": []})
                else:
                    with self.connect() as conn:
                        conn.execute("UPDATE chat_tool_calls SET status='unknown',updated_at=? WHERE call_id=?",
                                     (now(), call["call_id"]))
            with self.connect() as conn:
                conn.execute("UPDATE chat_model_calls SET status='unknown',error=? "
                             "WHERE request_uid=? AND status='started'",
                             ("API process interrupted.", request["request_uid"]))
            self.finish(request["request_uid"], "服务重启中断了回答；已受理任务仍可查询，不会自动重提。",
                        {"code": "MESSAGE_INTERRUPTED", "message": "模型回答未完成。"})
