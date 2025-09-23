#!/usr/bin/env python3
"""
Quick debug script to test SSE endpoint directly
"""

import asyncio
import traceback

import httpx


async def test_sse_endpoint() -> None:
    async with httpx.AsyncClient() as client:
        try:
            # Test payload similar to what the frontend sends
            payload = {
                "prompt": "What do you see in this image?",
                "conversation_id": "debug_test_conv",
                "interface_type": "web",
                "profile_id": "default_assistant",
                "attachments": [
                    {
                        "type": "image",
                        "content": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==",
                    }
                ],
            }

            print("Sending request to SSE endpoint...")
            async with client.stream(
                "POST",
                "http://localhost:8000/api/v1/chat/send_message_stream",
                json=payload,
                headers={"Accept": "text/event-stream"},
                timeout=30.0,
            ) as response:
                print(f"Response status: {response.status_code}")
                print(f"Response headers: {response.headers}")

                if response.status_code != 200:
                    print(f"Error response: {await response.aread()}")
                    return

                print("Reading SSE stream...")
                async for chunk in response.aiter_text():
                    print(f"Received chunk: {repr(chunk)}")
                    if chunk.strip():
                        print(f"Parsed chunk: {chunk.strip()}")

        except Exception as e:
            print(f"Error: {e}")
            traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(test_sse_endpoint())
