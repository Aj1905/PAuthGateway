"""Intercepting-proxy adapter: bridge captured HTTP(S) traffic to the Gateway.

This is the request-handling CORE of the Mode 2 inference/tool proxy (S22): the
part that reads an intercepted request, applies gateway enforcement, and either
forwards it upstream or blocks it -- the "intercept -> inspect -> forward or
reject" mechanism.

The TLS termination + networking shell (a ``mitmproxy`` addon, a CA the client
trusts, or a base-URL swap so the client connects to the proxy directly) is kept
OUT of this module so the enforcement logic is testable without real sockets.
That shell feeds parsed requests here and sends ``response``/``block_response``
back on the wire. The shell is deployment/infra work.

Two request kinds, matching INGRESS_DESIGN's two channels:

* **Inference request** (model API): carries the clean user prompt on the first
  turn. The proxy CAPTURES it (establishes the gateway plan) and ALWAYS forwards
  -- inference is not the enforcement point; blocking it would stop the agent
  from reasoning. "Capture is not enforcement."
* **Tool / SaaS request**: an outbound action. The proxy runs gateway
  enforcement; it forwards iff permitted, otherwise returns a value-free block
  response (S16). Because the gateway executes the tool via its SuiteSpec
  runner (the real-SaaS client in production), "permit -> execute" already IS
  the forward.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Callable

from gateway.runtime.gateway import CallResult, Gateway, SubmissionResult


@dataclasses.dataclass
class InferenceResult:
    forwarded: bool
    prompt: str | None
    submission: SubmissionResult | None
    response: Any


@dataclasses.dataclass
class ToolProxyResult:
    forward: bool
    permit: bool
    return_value: Any | None = None
    agent_reason: str | None = None
    block_response: dict | None = None


class InterceptingProxy:
    """Adapter from intercepted requests to gateway enforcement.

    ``model_upstream(request) -> response`` actually sends an inference request
    onward to the real model API (mocked in tests). ``submit`` overrides how a
    captured prompt is turned into a plan (defaults to the recognizer path);
    deployments pass the freeform/auto planner here.
    """

    def __init__(
        self,
        gateway: Gateway,
        model_upstream: Callable[[dict], Any],
        *,
        submit: Callable[[str], SubmissionResult] | None = None,
    ) -> None:
        self._gw = gateway
        self._model_upstream = model_upstream
        self._submit = submit or gateway.submit_user_prompt

    # -- Inference channel: capture the clean prompt, always forward. ----------
    @staticmethod
    def capture_prompt(messages: list[dict]) -> str | None:
        """Extract the first user turn's text from an Anthropic-style array."""
        for message in messages:
            if message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                texts = [
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                ]
                if texts:
                    return "\n".join(t for t in texts if t)
        return None

    def handle_inference(self, request: dict) -> InferenceResult:
        """Capture the prompt (establish the plan) and forward the model call."""
        prompt = self.capture_prompt(request.get("messages", []))
        submission = self._submit(prompt) if prompt is not None else None
        # Inference is never blocked here -- enforcement happens at the tool
        # channel. A rejected plan simply means the tool channel will
        # default-deny later.
        response = self._model_upstream(request)
        return InferenceResult(
            forwarded=True, prompt=prompt, submission=submission, response=response
        )

    # -- Tool channel: authorize; forward iff permitted. ----------------------
    def handle_tool(self, tool: str, args: list[Any]) -> ToolProxyResult:
        """Enforce an outbound tool/SaaS action. Forward (=execute) iff permitted."""
        result: CallResult = self._gw.handle_tool_call(tool, args)
        if result.permit:
            return ToolProxyResult(
                forward=True, permit=True, return_value=result.return_value
            )
        # Blocked: the wire response carries only the VALUE-FREE agent reason
        # (never the internal reason, which may quote an operand value, S16).
        return ToolProxyResult(
            forward=False,
            permit=False,
            agent_reason=result.agent_reason,
            block_response={
                "status": 403,
                "error": {"type": "pauth_denied", "message": result.agent_reason},
            },
        )
