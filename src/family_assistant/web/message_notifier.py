"""Message notification system for live chat updates.

This module provides the MessageNotifier class which manages asyncio.Queue
instances for SSE (Server-Sent Events) connections, enabling real-time message
delivery to web clients.

The design follows the same pattern as the task queue's notification system
(see storage/tasks.py), using asyncio.Queue per listener with tickle notifications
(boolean signals rather than message data).
"""

import asyncio
import logging

logger = logging.getLogger(__name__)


class MessageNotifier:
    """Manages notification queues for live message updates.

    This class maintains a registry of asyncio.Queue instances, one per SSE connection,
    organized by (conversation_id, interface_type). When a message is created, the
    notify() method is called to "tickle" all registered listeners, which then query
    the database for new messages.

    The notification pattern uses tickles (boolean signals) rather than sending
    actual message data, keeping the database as the single source of truth.

    Example usage:
        # In SSE endpoint
        notifier = request.app.state.message_notifier
        queue = await notifier.register(conversation_id, 'web')
        try:
            while True:
                await asyncio.wait_for(queue.get(), timeout=5.0)
                # Query database for new messages
        finally:
            await notifier.unregister(conversation_id, 'web', queue)

        # In message creation (on_commit callback)
        def notify_listeners():
            notifier.notify(conversation_id, 'web')
    """

    def __init__(self) -> None:
        """Initialize the message notifier.

        Creates empty registries for tracking listeners.
        Uses asyncio.Lock to ensure thread-safe access to the listener registry.
        """
        # Registry maps conversation keys to lists of listener queues
        self._listeners: dict[tuple[str, str], list[asyncio.Queue[bool]]] = {}
        self._lock = asyncio.Lock()

    async def register(
        self, conv_id: str, interface_type: str = "web", max_queue_size: int = 10
    ) -> asyncio.Queue[bool]:
        """Register a new SSE listener for a conversation.

        Creates a new asyncio.Queue for this listener and adds it to the registry.
        The queue will receive boolean "tickle" signals when new messages arrive.

        Args:
            conv_id: The conversation ID to listen to
            interface_type: The interface type (default 'web')
            max_queue_size: Maximum number of queued notifications (default 10)

        Returns:
            An asyncio.Queue that will receive True when messages arrive
        """
        async with self._lock:
            key = (conv_id, interface_type)
            queue: asyncio.Queue[bool] = asyncio.Queue(maxsize=max_queue_size)

            if key not in self._listeners:
                self._listeners[key] = []

            self._listeners[key].append(queue)

            listener_count = len(self._listeners[key])
            logger.debug(
                f"Registered listener for {key}, total listeners: {listener_count}"
            )

            return queue

    async def unregister(
        self, conv_id: str, interface_type: str, queue: asyncio.Queue[bool]
    ) -> None:
        """Unregister an SSE listener when the connection closes.

        Removes the queue from the registry and cleans up empty conversation entries.

        Args:
            conv_id: The conversation ID
            interface_type: The interface type
            queue: The queue instance to remove
        """
        async with self._lock:
            key = (conv_id, interface_type)

            if key in self._listeners:
                try:
                    self._listeners[key].remove(queue)
                    logger.debug(
                        f"Unregistered listener for {key}, "
                        f"remaining: {len(self._listeners[key])}"
                    )
                except ValueError:
                    # Queue already removed, ignore
                    pass

                # Clean up empty conversation entries
                if not self._listeners[key]:
                    del self._listeners[key]
                    logger.debug(f"No more listeners for {key}, removed from registry")

    def notify(self, conv_id: str, interface_type: str) -> None:
        """Tickle all listeners for a conversation.

        Sends a boolean True to each registered queue for this conversation.
        This is a non-blocking operation - if a queue is full (slow listener),
        the tickle is skipped and the listener will catch up via timeout polling.

        This method is called from database on_commit() callbacks and must be
        synchronous (not async).

        IMPORTANT: This method uses asyncio.Queue.put_nowait() which is NOT
        thread-safe. It MUST be called from the same thread running the asyncio
        event loop. In practice, this is guaranteed because on_commit() callbacks
        run in the same event loop as the database transaction.

        Args:
            conv_id: The conversation ID that has new messages
            interface_type: The interface type
        """
        key = (conv_id, interface_type)

        # Get snapshot of listeners without holding the lock
        # (notify is called from sync context, can't await lock)
        listeners = self._listeners.get(key, []).copy()

        if not listeners:
            logger.debug(f"No listeners for {key}, skipping notification")
            return

        logger.debug(f"Notifying {len(listeners)} listener(s) for {key}")

        for queue in listeners:
            try:
                # Non-blocking put - if queue is full, skip this tickle
                queue.put_nowait(True)
            except asyncio.QueueFull:
                # Listener is slow/stuck - they'll catch up via timeout poll
                logger.warning(
                    f"Listener queue full for {key}, "
                    f"skipping tickle (listener will catch up via polling)"
                )
            except Exception as e:
                # Log but don't fail - notification is best-effort
                logger.error(f"Error notifying listener for {key}: {e}")

    def get_listener_count(self, conv_id: str, interface_type: str) -> int:
        """Get the number of active listeners for a conversation.

        Useful for debugging and metrics.

        Args:
            conv_id: The conversation ID
            interface_type: The interface type

        Returns:
            Number of active SSE connections for this conversation
        """
        key = (conv_id, interface_type)
        return len(self._listeners.get(key, []))

    def get_total_listeners(self) -> int:
        """Get the total number of active listeners across all conversations.

        Returns:
            Total number of active SSE connections
        """
        return sum(len(queues) for queues in self._listeners.values())

    def get_conversation_keys(self) -> list[tuple[str, str]]:
        """Get all conversation keys that have active listeners.

        Useful for debugging and monitoring.

        Returns:
            List of (conversation_id, interface_type) tuples
        """
        return list(self._listeners.keys())
