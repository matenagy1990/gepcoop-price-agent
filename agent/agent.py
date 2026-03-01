import anthropic
import json
import os
from typing import Callable, Awaitable
from dotenv import load_dotenv
from .tools import lookup_mapping, fetch_supplier_price

load_dotenv()

ProgressCb = Callable[[dict], Awaitable[None]] | None

TOOLS = [
    {
        "name": "lookup_mapping",
        "description": (
            "Translate a Gép-Coop internal part number to the supplier ID, "
            "supplier part number, and supplier URL. Must be called first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "internal_part_no": {
                    "type": "string",
                    "description": "The Gép-Coop internal part number (e.g. 934128ZN)",
                }
            },
            "required": ["internal_part_no"],
        },
    },
    {
        "name": "fetch_supplier_price",
        "description": (
            "Open the supplier website, search for the part, and return the "
            "current price (normalised to per db) and stock level. "
            "Call this after lookup_mapping."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "supplier_id": {
                    "type": "string",
                    "description": "Supplier identifier from lookup_mapping (e.g. csavarda, irontrade)",
                },
                "supplier_part_no": {
                    "type": "string",
                    "description": "Supplier part number from lookup_mapping",
                },
            },
            "required": ["supplier_id", "supplier_part_no"],
        },
    },
]

SYSTEM_PROMPT = """You are a procurement assistant for Gép-Coop.
Your job is to look up current prices and stock levels from supplier websites.

Steps:
1. Call lookup_mapping with the internal part number.
2. Call fetch_supplier_price with the supplier_id and supplier_part_no from step 1.
3. Return a clear, concise summary of the result in English.

Rules:
- Never guess or invent prices or stock levels.
- If a tool returns an error, report it clearly in plain language.
- Do not call a tool more than once for the same request.
"""


async def run_agent(internal_part_no: str, on_progress: ProgressCb = None) -> dict:
    """
    Run the Claude agent for a single part number query.
    Emits progress events via on_progress callback if provided.
    Returns a dict with the full result or an error.
    """
    async def emit(event: dict):
        if on_progress:
            await on_progress(event)

    client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    messages = [
        {
            "role": "user",
            "content": f"Look up the current price and stock for internal part number: {internal_part_no}",
        }
    ]

    collected: dict = {}

    while True:
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            final_text = next(
                (b.text for b in response.content if hasattr(b, "text")), ""
            )
            result = {"internal_part_no": internal_part_no, "message": final_text}
            if "lookup_mapping" in collected:
                result.update(
                    {
                        "supplier_id": collected["lookup_mapping"].get("supplier_id", ""),
                        "supplier_part_no": collected["lookup_mapping"].get("supplier_part_no", ""),
                    }
                )
            if "fetch_supplier_price" in collected:
                pd = collected["fetch_supplier_price"]
                result.update(
                    {
                        "price_per_db":   pd.get("price_per_db"),
                        "price_raw":      pd.get("price_raw"),
                        "price_unit_qty": pd.get("price_unit_qty"),
                        "currency":       pd.get("currency", "HUF"),
                        "unit":           pd.get("unit", "db"),
                        "stock":          pd.get("stock"),
                        "queried_at":     pd.get("queried_at"),
                    }
                )
            return result

        if response.stop_reason == "tool_use":
            tool_blocks = [b for b in response.content if b.type == "tool_use"]
            messages.append({"role": "assistant", "content": [b.model_dump() for b in response.content]})

            tool_results = []
            for block in tool_blocks:
                try:
                    if block.name == "lookup_mapping":
                        await emit({
                            "step": "mapping", "status": "running",
                            "msg": f"Looking up part number '{block.input['internal_part_no']}' in mapping table…"
                        })
                        result = lookup_mapping(block.input["internal_part_no"])
                        await emit({
                            "step": "mapping", "status": "done",
                            "msg": f"Found → supplier: {result['supplier_id']}, part: {result['supplier_part_no']}",
                        })

                    elif block.name == "fetch_supplier_price":
                        supplier_id = block.input["supplier_id"]
                        await emit({
                            "step": "browser", "status": "running",
                            "msg": f"Opening {supplier_id} website…",
                            "supplier": supplier_id,
                        })
                        result = await fetch_supplier_price(
                            supplier_id,
                            block.input["supplier_part_no"],
                            on_progress=on_progress,
                        )
                        await emit({
                            "step": "browser", "status": "done",
                            "msg": "Price and stock retrieved successfully.",
                        })

                    else:
                        result = {"error": f"Unknown tool: {block.name}"}

                    collected[block.name] = result

                except Exception as exc:
                    result = {"error": str(exc)}
                    collected[block.name] = result
                    step = "mapping" if block.name == "lookup_mapping" else "browser"
                    await emit({"step": step, "status": "error", "msg": str(exc)})

                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )

            messages.append({"role": "user", "content": tool_results})
            continue

        break

    return {
        "internal_part_no": internal_part_no,
        "error": "Agent finished unexpectedly.",
    }
