"""Injected ML seam and explicit deterministic engineering stub; no real model."""

from typing import Protocol

from factored_bck.conversation_contract import AdapterContext, AdapterInfo


class MLAdapter(Protocol):
    info: AdapterInfo

    async def propose(self, context: AdapterContext) -> dict:
        """Return one untrusted proposal; honor cancellation and the host's deadline.

        Implementations must use nonblocking I/O and bounded provider deadlines. They
        receive no tokens, principal, database connection or executable capabilities.
        """
        ...


class DeterministicStub:
    """Exact engineering commands, not an intent classifier or ML quality baseline."""

    info = AdapterInfo(provider="deterministic-engineering-stub", version="1", mode="stub")

    async def propose(self, context):
        text = context.messages[-1].text
        if text == "/cards":
            return {"kind": "tool_request", "name": "get_cards", "arguments": {}}
        if text.startswith("/pause "):
            return {
                "kind": "tool_request",
                "name": "pause_card",
                "arguments": {"product_id": text.removeprefix("/pause ")},
            }
        if text == "/clarify":
            return {
                "kind": "clarification",
                "question": "¿Pausa temporal o pérdida/robo?"
                if context.language == "es"
                else "Pausa temporária ou perda/roubo?",
            }
        if text == "/handoff":
            return {
                "kind": "human_handoff",
                "triage": {
                    "reason": "card_support",
                    "severity": "low",
                    "required_specialty": None,
                    "minimum_experience": "Junior",
                    "language": context.language,
                    "summary": "Engineering stub requests human review; no bank action asserted.",
                },
            }
        return {
            "kind": "answer",
            "text": "Modo de prueba: este mensaje no ejecuta acciones bancarias."
            if context.language == "es"
            else "Modo de teste: esta mensagem não executa ações bancárias.",
        }
