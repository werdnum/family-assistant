"""Unit tests for MessageNotifier class."""

import asyncio
import logging
import time

import pytest

from family_assistant.web.message_notifier import MessageNotifier


class TestMessageNotifier:
    """Test MessageNotifier notification queue management."""

    @pytest.mark.asyncio
    @pytest.mark.no_db
    async def test_register_creates_queue(self) -> None:
        """Test that register() creates a queue and adds it to the registry."""
        notifier = MessageNotifier()

        queue = await notifier.register("conv1", "web")

        assert isinstance(queue, asyncio.Queue)
        assert queue.maxsize == 10  # Default max_queue_size
        assert notifier.get_listener_count("conv1", "web") == 1

    @pytest.mark.asyncio
    @pytest.mark.no_db
    async def test_register_custom_queue_size(self) -> None:
        """Test that register() respects custom max_queue_size."""
        notifier = MessageNotifier()

        queue = await notifier.register("conv1", "web", max_queue_size=5)

        assert queue.maxsize == 5

    @pytest.mark.asyncio
    @pytest.mark.no_db
    async def test_register_multiple_listeners_same_conversation(self) -> None:
        """Test that multiple listeners can register for the same conversation."""
        notifier = MessageNotifier()

        queue1 = await notifier.register("conv1", "web")
        queue2 = await notifier.register("conv1", "web")
        queue3 = await notifier.register("conv1", "web")

        assert queue1 is not queue2
        assert queue2 is not queue3
        assert notifier.get_listener_count("conv1", "web") == 3

    @pytest.mark.asyncio
    @pytest.mark.no_db
    async def test_register_different_conversations(self) -> None:
        """Test that different conversations have separate listener lists."""
        notifier = MessageNotifier()

        await notifier.register("conv1", "web")
        await notifier.register("conv2", "web")
        await notifier.register("conv1", "telegram")

        assert notifier.get_listener_count("conv1", "web") == 1
        assert notifier.get_listener_count("conv2", "web") == 1
        assert notifier.get_listener_count("conv1", "telegram") == 1
        assert notifier.get_total_listeners() == 3

    @pytest.mark.asyncio
    @pytest.mark.no_db
    async def test_unregister_removes_queue(self) -> None:
        """Test that unregister() removes queue from registry."""
        notifier = MessageNotifier()

        queue = await notifier.register("conv1", "web")
        assert notifier.get_listener_count("conv1", "web") == 1

        await notifier.unregister("conv1", "web", queue)
        assert notifier.get_listener_count("conv1", "web") == 0

    @pytest.mark.asyncio
    @pytest.mark.no_db
    async def test_unregister_cleans_up_empty_entries(self) -> None:
        """Test that unregister() removes conversation key when last listener is removed."""
        notifier = MessageNotifier()

        queue = await notifier.register("conv1", "web")
        await notifier.unregister("conv1", "web", queue)

        # Should not have the key in the registry anymore
        assert ("conv1", "web") not in notifier.get_conversation_keys()

    @pytest.mark.asyncio
    @pytest.mark.no_db
    async def test_unregister_keeps_other_listeners(self) -> None:
        """Test that unregister() only removes the specified queue."""
        notifier = MessageNotifier()

        queue1 = await notifier.register("conv1", "web")
        queue2 = await notifier.register("conv1", "web")
        queue3 = await notifier.register("conv1", "web")

        await notifier.unregister("conv1", "web", queue2)

        assert notifier.get_listener_count("conv1", "web") == 2
        # Verify the right queue was removed by checking notifications
        notifier.notify("conv1", "web")
        assert queue1.qsize() == 1
        assert queue2.qsize() == 0  # Should not have received notification
        assert queue3.qsize() == 1

    @pytest.mark.asyncio
    @pytest.mark.no_db
    async def test_unregister_nonexistent_queue(self) -> None:
        """Test that unregister() handles nonexistent queue gracefully."""
        notifier = MessageNotifier()

        await notifier.register("conv1", "web")
        queue2 = asyncio.Queue()  # Not registered

        # Should not raise error
        await notifier.unregister("conv1", "web", queue2)

        # Original queue should still be there
        assert notifier.get_listener_count("conv1", "web") == 1

    @pytest.mark.asyncio
    @pytest.mark.no_db
    async def test_unregister_nonexistent_conversation(self) -> None:
        """Test that unregister() handles nonexistent conversation gracefully."""
        notifier = MessageNotifier()

        queue = asyncio.Queue()

        # Should not raise error
        await notifier.unregister("nonexistent", "web", queue)

    @pytest.mark.asyncio
    @pytest.mark.no_db
    async def test_notify_tickles_all_listeners(self) -> None:
        """Test that notify() sends True to all registered listeners."""
        notifier = MessageNotifier()

        queue1 = await notifier.register("conv1", "web")
        queue2 = await notifier.register("conv1", "web")
        queue3 = await notifier.register("conv1", "web")

        notifier.notify("conv1", "web")

        # All queues should have received a tickle
        assert await queue1.get() is True
        assert await queue2.get() is True
        assert await queue3.get() is True

    @pytest.mark.asyncio
    @pytest.mark.no_db
    async def test_notify_only_notifies_matching_conversation(self) -> None:
        """Test that notify() only notifies listeners for the specific conversation."""
        notifier = MessageNotifier()

        queue1 = await notifier.register("conv1", "web")
        queue2 = await notifier.register("conv2", "web")
        queue3 = await notifier.register("conv1", "telegram")

        notifier.notify("conv1", "web")

        # Only conv1/web should have received notification
        assert queue1.qsize() == 1
        assert queue2.qsize() == 0
        assert queue3.qsize() == 0

    @pytest.mark.asyncio
    @pytest.mark.no_db
    async def test_notify_with_no_listeners(self) -> None:
        """Test that notify() with no listeners doesn't raise error."""
        notifier = MessageNotifier()

        # Should not raise error
        notifier.notify("nonexistent", "web")

    @pytest.mark.asyncio
    @pytest.mark.no_db
    async def test_notify_with_full_queue_logs_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that notify() logs warning when queue is full but doesn't crash."""
        notifier = MessageNotifier()

        # Register with small queue size
        queue = await notifier.register("conv1", "web", max_queue_size=2)

        # Fill the queue
        notifier.notify("conv1", "web")
        notifier.notify("conv1", "web")

        # Queue should be full now
        assert queue.qsize() == 2

        # Next notification should log warning but not crash
        with caplog.at_level(logging.WARNING):
            notifier.notify("conv1", "web")

        assert "Listener queue full" in caplog.text
        assert "skipping tickle" in caplog.text
        assert queue.qsize() == 2  # Queue should still be full

    @pytest.mark.asyncio
    @pytest.mark.no_db
    async def test_notify_multiple_times_queues_notifications(self) -> None:
        """Test that multiple notify() calls queue up notifications."""
        notifier = MessageNotifier()

        queue = await notifier.register("conv1", "web", max_queue_size=5)

        # Send multiple notifications
        notifier.notify("conv1", "web")
        notifier.notify("conv1", "web")
        notifier.notify("conv1", "web")

        assert queue.qsize() == 3

        # All should be True
        assert await queue.get() is True
        assert await queue.get() is True
        assert await queue.get() is True

    @pytest.mark.asyncio
    @pytest.mark.no_db
    async def test_get_listener_count_returns_correct_count(self) -> None:
        """Test that get_listener_count() returns accurate count."""
        notifier = MessageNotifier()

        assert notifier.get_listener_count("conv1", "web") == 0

        await notifier.register("conv1", "web")
        assert notifier.get_listener_count("conv1", "web") == 1

        await notifier.register("conv1", "web")
        assert notifier.get_listener_count("conv1", "web") == 2

        await notifier.register("conv1", "web")
        assert notifier.get_listener_count("conv1", "web") == 3

    @pytest.mark.asyncio
    @pytest.mark.no_db
    async def test_get_listener_count_different_conversations(self) -> None:
        """Test that get_listener_count() is scoped to specific conversation."""
        notifier = MessageNotifier()

        await notifier.register("conv1", "web")
        await notifier.register("conv1", "web")
        await notifier.register("conv2", "web")

        assert notifier.get_listener_count("conv1", "web") == 2
        assert notifier.get_listener_count("conv2", "web") == 1
        assert notifier.get_listener_count("conv3", "web") == 0

    @pytest.mark.asyncio
    @pytest.mark.no_db
    async def test_get_total_listeners_returns_sum(self) -> None:
        """Test that get_total_listeners() returns total across all conversations."""
        notifier = MessageNotifier()

        assert notifier.get_total_listeners() == 0

        await notifier.register("conv1", "web")
        await notifier.register("conv1", "web")
        await notifier.register("conv2", "web")
        await notifier.register("conv1", "telegram")

        assert notifier.get_total_listeners() == 4

    @pytest.mark.asyncio
    @pytest.mark.no_db
    async def test_get_conversation_keys_returns_active_conversations(self) -> None:
        """Test that get_conversation_keys() returns list of conversations with listeners."""
        notifier = MessageNotifier()

        assert notifier.get_conversation_keys() == []

        await notifier.register("conv1", "web")
        await notifier.register("conv2", "web")
        await notifier.register("conv1", "telegram")

        keys = notifier.get_conversation_keys()
        assert len(keys) == 3
        assert ("conv1", "web") in keys
        assert ("conv2", "web") in keys
        assert ("conv1", "telegram") in keys

    @pytest.mark.asyncio
    @pytest.mark.no_db
    async def test_get_conversation_keys_after_cleanup(self) -> None:
        """Test that get_conversation_keys() doesn't include cleaned up conversations."""
        notifier = MessageNotifier()

        queue1 = await notifier.register("conv1", "web")
        await notifier.register("conv2", "web")

        # Remove the only listener for conv1
        await notifier.unregister("conv1", "web", queue1)

        keys = notifier.get_conversation_keys()
        assert len(keys) == 1
        assert ("conv1", "web") not in keys
        assert ("conv2", "web") in keys

    @pytest.mark.asyncio
    @pytest.mark.no_db
    async def test_concurrent_register_and_unregister(self) -> None:
        """Test that concurrent register/unregister operations are thread-safe."""
        notifier = MessageNotifier()

        async def register_and_unregister(conv_id: str) -> None:
            queue = await notifier.register(conv_id, "web")
            await asyncio.sleep(0.01)  # Simulate some work
            await notifier.unregister(conv_id, "web", queue)

        # Run multiple concurrent operations
        tasks = [register_and_unregister(f"conv{i}") for i in range(10)]
        await asyncio.gather(*tasks)

        # All should be cleaned up
        assert notifier.get_total_listeners() == 0
        assert notifier.get_conversation_keys() == []

    @pytest.mark.asyncio
    @pytest.mark.no_db
    async def test_concurrent_notify_and_register(self) -> None:
        """Test that concurrent notify and register operations don't crash."""
        notifier = MessageNotifier()

        async def register_listener() -> None:
            await notifier.register("conv1", "web")

        async def send_notifications() -> None:
            for _ in range(10):
                notifier.notify("conv1", "web")
                await asyncio.sleep(0.001)

        # Run register and notify concurrently
        await asyncio.gather(
            register_listener(),
            register_listener(),
            send_notifications(),
        )

        # Should have registered 2 listeners
        assert notifier.get_listener_count("conv1", "web") == 2

    @pytest.mark.asyncio
    @pytest.mark.no_db
    async def test_notify_is_non_blocking(self) -> None:
        """Test that notify() is a non-blocking synchronous operation."""
        notifier = MessageNotifier()

        await notifier.register("conv1", "web")

        # notify() should return immediately
        start = time.time()
        notifier.notify("conv1", "web")
        elapsed = time.time() - start

        # Should complete in microseconds, not milliseconds
        assert elapsed < 0.01

    @pytest.mark.asyncio
    @pytest.mark.no_db
    async def test_notify_continues_after_exception_in_one_listener(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test that notify() continues notifying other listeners even if one fails."""
        notifier = MessageNotifier()

        queue1 = await notifier.register("conv1", "web")
        queue2 = await notifier.register("conv1", "web")

        # Break queue1 by replacing put_nowait with something that raises
        def broken_put(item: bool) -> None:
            raise RuntimeError("Simulated error")

        queue1.put_nowait = broken_put  # type: ignore[method-assign]

        # Notify should log error but continue
        with caplog.at_level(logging.ERROR):
            notifier.notify("conv1", "web")

        # queue2 should still receive the notification
        assert queue2.qsize() == 1
        assert await queue2.get() is True

        # Should have logged the error
        assert "Error notifying listener" in caplog.text

    @pytest.mark.asyncio
    @pytest.mark.no_db
    async def test_listener_isolation(self) -> None:
        """Test that listeners for different conversations are isolated."""
        notifier = MessageNotifier()

        web1 = await notifier.register("conv1", "web")
        web2 = await notifier.register("conv2", "web")
        telegram1 = await notifier.register("conv1", "telegram")

        # Notify each combination
        notifier.notify("conv1", "web")
        notifier.notify("conv2", "web")
        notifier.notify("conv1", "telegram")

        # Each should have exactly one notification
        assert web1.qsize() == 1
        assert web2.qsize() == 1
        assert telegram1.qsize() == 1

        # Verify they got the right notifications
        assert await web1.get() is True
        assert await web2.get() is True
        assert await telegram1.get() is True

        # All queues should be empty now
        assert web1.qsize() == 0
        assert web2.qsize() == 0
        assert telegram1.qsize() == 0
