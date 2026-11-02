"""Owned stdio app-server connection; adapts events to the existing exec renderer.

No sockets, auth copies, automatic approval grants, or model-call retries.
"""
import asyncio
import json
import re


class RpcError(RuntimeError):
    """A definite rejection, as opposed to an ambiguous lost acknowledgement."""


class CodexAppTransport:
    def __init__(self, proc, read_line, *, timeout=30):
        self.proc = proc
        self.read_line = read_line
        self.timeout = timeout
        self.events = asyncio.StreamReader(limit=1024 * 1024)
        self.pending = {}
        self.sequence = 0
        self.thread_id = None
        self.turn_id = None
        self.finished = False
        self.closed = False
        self.events_finished = False
        self.stage = "not initialized"
        self.usage = None
        self.usage_base = None
        self.turn_messages = []
        self.reader_task = None

    def emit(self, event):
        if not self.closed and not self.events_finished:
            self.events.feed_data((json.dumps(event, ensure_ascii=False) + "\n").encode())

    def finish_events(self):
        """EOF is terminal even while unread events remain in the buffer."""
        if not self.events_finished:
            self.events_finished = True
            self.events.feed_eof()

    async def send(self, message):
        if self.closed:
            raise ConnectionError("Codex App Server connection closed")
        self.proc.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode())
        await self.proc.stdin.drain()

    async def rpc(self, method, params):
        if self.closed or self.events_finished:
            raise ConnectionError("Codex App Server connection already closed")
        self.stage = method
        self.sequence += 1
        request_id = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.send({"id": request_id, "method": method, "params": params})
            return await asyncio.wait_for(future, self.timeout)
        finally:
            self.pending.pop(request_id, None)
            if not future.done():
                future.cancel()

    @staticmethod
    def inputs(prompt, images):
        return [{"type": "text", "text": prompt}] + [
            {"type": "localImage", "path": path} for path in (images or [])
        ]

    async def start(self, prompt, images, *, resume_id, cwd, model, effort, ephemeral=False):
        self.reader_task = asyncio.create_task(self._read())
        await self.rpc("initialize", {
            "clientInfo": {"name": "discord_codex_remote", "title": "Discord Codex Remote", "version": "2.0.0"},
        })
        await self.send({"method": "initialized", "params": {}})
        params = {"cwd": cwd, "approvalPolicy": "never", "sandbox": "danger-full-access"}
        if model:
            params["model"] = model
        if ephemeral and not resume_id:
            params["ephemeral"] = True
        method = "thread/resume" if resume_id else "thread/start"
        if resume_id:
            params["threadId"] = resume_id
            # Discord renders only the new turn. Do not serialize years of old
            # items into one RPC response; this does not clear model context.
            params["excludeTurns"] = True
        reply = await self.rpc(method, params)
        self.thread_id = reply["thread"]["id"]
        self.emit({"type": "thread.started", "thread_id": self.thread_id})
        reply = await self.rpc("turn/start", {
            "threadId": self.thread_id, "input": self.inputs(prompt, images), "effort": effort,
        })
        # A tiny turn can complete before the start acknowledgement arrives.
        if not self.finished:
            self.turn_id = reply["turn"]["id"]

    async def steer(self, prompt, images):
        if not self.turn_id or self.finished or self.closed:
            return False
        await self.rpc("turn/steer", {
            "threadId": self.thread_id, "expectedTurnId": self.turn_id,
            "input": self.inputs(prompt, images),
        })
        return True

    async def interrupt(self):
        if self.turn_id and not self.finished and not self.closed:
            await self.rpc("turn/interrupt", {"threadId": self.thread_id, "turnId": self.turn_id})

    def _notification(self, method, params):
        # Ignore unrelated thread/turn notifications (including bootstrap MCP events).
        if params.get("threadId") and params["threadId"] != self.thread_id:
            return
        if method == "turn/started":
            self.turn_id = params["turn"]["id"]
            self.emit({"type": "turn.started"})
        elif method == "thread/tokenUsage/updated":
            total = params["tokenUsage"]["total"]
            last = params["tokenUsage"]["last"]
            if self.usage_base is None:
                self.usage_base = {key: total.get(key, 0) - last.get(key, 0) for key in total}
            fields = {"inputTokens": "input_tokens", "cachedInputTokens": "cached_input_tokens",
                      "outputTokens": "output_tokens", "reasoningOutputTokens": "reasoning_output_tokens"}
            self.usage = {target: max(0, total.get(key, 0) - self.usage_base.get(key, 0))
                          for key, target in fields.items()}
        elif method in ("item/started", "item/completed"):
            item = dict(params.get("item") or {})
            kind = item.get("type", "")
            item["type"] = re.sub(r"(?<!^)(?=[A-Z])", "_", kind).lower()
            if kind == "agentMessage" and method == "item/completed":
                self.turn_messages.append(item)
            self.emit({"type": method.replace("/", "."), "item": item})
        elif method == "item/agentMessage/delta":
            # Render only a short public assistant preview, never reasoning deltas.
            self.emit({"type": "item.updated", "item": {
                "id": params.get("itemId"), "type": "agent_message", "text": params.get("delta", ""),
            }})
        elif method == "error":
            if not params.get("willRetry"):
                self.emit({"type": "error", "message": str(params.get("error", {}))})
        elif method == "turn/completed":
            self.finished = True
            self.turn_id = None
            turn = params["turn"]
            if turn.get("status") == "completed":
                # Prefer final_answer over later commentary when phases are available.
                finals = [m for m in self.turn_messages if m.get("phase") == "final_answer"]
                if finals:
                    self.emit({"type": "item.completed", "item": finals[-1]})
                self.emit({"type": "turn.completed", "usage": self.usage})
            else:
                self.emit({"type": "turn.failed", "error": turn.get("error") or {"message": turn.get("status")}})
                self.finish_events()

    async def _read(self):
        failure = None
        try:
            while True:
                raw = await self.read_line(self.proc.stdout)
                if not raw:
                    failure = ConnectionError(f"Codex App Server stdout closed during {self.stage}")
                    break
                try:
                    message = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if "id" in message and "method" not in message:
                    future = self.pending.get(message["id"])
                    if future and not future.done():
                        if "error" in message:
                            future.set_exception(RpcError(str(message["error"])))
                        else:
                            future.set_result(message.get("result", {}))
                elif "method" in message and "id" in message:
                    # Fail closed. Never invent answers or expand approval policy.
                    await self.send({"id": message["id"], "error": {
                        "code": -32601, "message": "Discord bridge cannot answer this approval/input request",
                    }})
                    self.emit({"type": "error", "message": "지원하지 않는 승인/응답 요청: " + message["method"]})
                elif "method" in message:
                    self._notification(message["method"], message.get("params") or {})
        except asyncio.CancelledError:
            raise
        except Exception as error:
            failure = ConnectionError(f"Codex App Server read failed during {self.stage}: {type(error).__name__}: {error}")
            self.emit({"type": "error", "message": str(error)})
        finally:
            self.finished = True
            self.turn_id = None
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(failure or ConnectionError("Codex App Server connection closed"))
            self.finish_events()

    async def close(self):
        self.closed = True
        if self.reader_task:
            self.reader_task.cancel()
            await asyncio.gather(self.reader_task, return_exceptions=True)
        self.finish_events()
        if self.proc.stdin:
            self.proc.stdin.close()
