"""Conversations between matched users (D-027).

PostgreSQL is the source of truth for who is talking to whom. A user can be in at
most one open conversation, which a partial unique index enforces even under
concurrent matching.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.database.engine import Base

# Why a conversation ended. More reasons are added with blocking.
# "unreachable": a message could not be delivered because the partner blocked the bot
# or their account is gone. "idle": nobody wrote for the configured time (D-039).
END_REASONS = ("user_ended", "unreachable", "idle")
# What a relayed message was. Text and captions are stored in `text`.
MESSAGE_KINDS = (
    "text",
    "photo",
    "video",
    "animation",
    "audio",
    "voice",
    "video_note",
    "sticker",
    "document",
    "poll",
    "dice",
    "other",
)


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        CheckConstraint(
            f"end_reason IN ({', '.join(repr(r) for r in END_REASONS)})", name="end_reason"
        ),
        CheckConstraint("(ended_at IS NULL) = (end_reason IS NULL)", name="ended_pair"),
        # Serves the worker's search for quiet open chats.
        Index(
            "ix_conversations_open_last_activity_at",
            "last_activity_at",
            postgresql_where=text("ended_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # When a message was last relayed (or the chat started), for the idle timeout.
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # When both users were warned that the quiet chat will end; cleared by any message.
    idle_warned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_reason: Mapped[str | None] = mapped_column(String(16))
    # Who ended it, when a user did. NULL for automatic endings.
    ended_by_user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"))


class ConversationParticipant(Base):
    """One row per user per conversation. `left_at` is set when the conversation ends."""

    __tablename__ = "conversation_participants"
    __table_args__ = (
        # At most one open conversation per user: the final guard against double matches.
        Index(
            "uq_conversation_participants_open_user",
            "user_id",
            unique=True,
            postgresql_where=text("left_at IS NULL"),
        ),
        # Serves "conversations this user was in", e.g. for repeat-match avoidance.
        Index("ix_conversation_participants_user_id", "user_id", "conversation_id"),
    )

    conversation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), primary_key=True)
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ChatMessage(Base):
    """A message relayed between chat partners, kept for moderation (D-028).

    `text` holds the original text or caption and is never overwritten; `edited_text`
    holds the latest edit (D-038, D-043). Content (`text`, `edited_text`, `file_id`) is
    deleted after the retention period; the row itself maps the sender's message to the
    partner's copy, so replies and edits can be mirrored.
    """

    __tablename__ = "chat_messages"
    __table_args__ = (
        CheckConstraint(f"kind IN ({', '.join(repr(k) for k in MESSAGE_KINDS)})", name="kind"),
        CheckConstraint("(edited_text IS NULL) OR (edited_at IS NOT NULL)", name="edited_pair"),
        # One row per message the sender sent: a redelivered update is relayed once.
        UniqueConstraint("sender_user_id", "sender_message_id"),
        # Serves "the message this reply points to" and "the copy of this message".
        Index("ix_chat_messages_conversation_recipient", "conversation_id", "recipient_message_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("conversations.id", ondelete="CASCADE")
    )
    sender_user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    # Message ids are per chat: the sender's original, and the copy in the partner's chat.
    # The row is written before the copy is sent, so a redelivered update is never
    # relayed twice; `recipient_message_id` is filled in once the copy exists, and stays
    # NULL if the bot stopped in between (the message was then possibly not delivered).
    sender_message_id: Mapped[int] = mapped_column(BigInteger)
    recipient_message_id: Mapped[int | None] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(16))
    text: Mapped[str | None] = mapped_column(Text)
    edited_text: Mapped[str | None] = mapped_column(Text)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # A Telegram file identifier only; files are never downloaded.
    file_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
