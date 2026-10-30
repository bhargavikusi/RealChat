"""Conversations between matched users (D-027).

PostgreSQL is the source of truth for who is talking to whom. A user can be in at
most one open conversation, which a partial unique index enforces even under
concurrent matching.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from backend.database.engine import Base

# Why a conversation ended. "unreachable": a message could not be delivered because the
# partner blocked the bot or their account is gone. "idle": nobody wrote for the
# configured time (D-039). "blocked" and "reported": a user blocked or reported their
# partner (D-045).
END_REASONS = ("user_ended", "unreachable", "idle", "blocked", "reported")
# Endings caused by one of the users, who is recorded in `ended_by_user_id`.
USER_END_REASONS = ("user_ended", "blocked", "reported")
# Kinds sent as view-once (tap to view) in Private mode (D-048).
VIEW_ONCE_KINDS = ("photo", "video", "voice", "video_note")
# Why Private mode ended (D-047): a user turned it off, the chat ended, or it reached
# its time limit.
PRIVATE_END_REASONS = ("turned_off", "chat_ended", "time_limit")
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
    holds the latest edit (D-038, D-043). The row maps the sender's message to the
    partner's copy, so replies and edits can be mirrored, and the whole row is deleted
    after the retention period (D-046), unless its conversation has an open report.
    """

    __tablename__ = "chat_messages"
    __table_args__ = (
        CheckConstraint(f"kind IN ({', '.join(repr(k) for k in MESSAGE_KINDS)})", name="kind"),
        CheckConstraint("(edited_text IS NULL) OR (edited_at IS NOT NULL)", name="edited_pair"),
        CheckConstraint("viewed_at IS NULL OR view_once", name="viewed_once"),
        CheckConstraint(
            "viewed_message_id IS NULL OR viewed_at IS NOT NULL", name="viewed_message_pair"
        ),
        # One row per message the sender sent: a redelivered update is relayed once.
        UniqueConstraint("sender_user_id", "sender_message_id"),
        # Serves "the message this reply points to" and "the copy of this message".
        Index("ix_chat_messages_conversation_recipient", "conversation_id", "recipient_message_id"),
        # Serves the retention cleanup, which deletes the oldest messages first.
        Index("ix_chat_messages_created_at", "created_at"),
        # Serves "the messages sent in this Private mode session".
        Index(
            "ix_chat_messages_private_session_id",
            "private_session_id",
            postgresql_where=text("private_session_id IS NOT NULL"),
        ),
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
    # The Private mode session it was sent in, if any (D-047).
    private_session_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("private_sessions.id")
    )
    # View-once media (D-048): the partner got a tap-to-view placeholder, whose id is
    # `recipient_message_id`. `viewed_at` is when it was opened (once), and
    # `viewed_message_id` the copy then sent to the partner. `media_seconds` is the
    # length of a video or voice message, added to the view timer.
    view_once: Mapped[bool] = mapped_column(Boolean, server_default="false")
    media_seconds: Mapped[int | None] = mapped_column(Integer)
    viewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    viewed_message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PrivateSession(Base):
    """A period of Private mode in a conversation (D-031, D-047).

    Messages sent while it is active are deleted from both users' Telegram chats once it
    ends. At most one session per conversation is active at a time.
    """

    __tablename__ = "private_sessions"
    __table_args__ = (
        CheckConstraint(
            f"end_reason IN ({', '.join(repr(r) for r in PRIVATE_END_REASONS)})",
            name="end_reason",
        ),
        CheckConstraint("(ended_at IS NULL) = (end_reason IS NULL)", name="ended_pair"),
        Index(
            "uq_private_sessions_active_conversation",
            "conversation_id",
            unique=True,
            postgresql_where=text("ended_at IS NULL"),
        ),
        # Serves the worker's search for active sessions to end.
        Index(
            "ix_private_sessions_active_started_at",
            "started_at",
            postgresql_where=text("ended_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("conversations.id", ondelete="CASCADE")
    )
    started_by_user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_reason: Mapped[str | None] = mapped_column(String(16))
    ended_by_user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id"))


class TelegramDeletion(Base):
    """A message to delete from a user's Telegram chat (D-047).

    Holds only Telegram identifiers, no content, so it is independent of the retention of
    stored messages. An entry is removed once the message is deleted, turns out to be
    gone already, or can no longer be deleted (Telegram allows it for 48 hours).
    """

    __tablename__ = "telegram_deletions"
    __table_args__ = (
        UniqueConstraint("chat_id", "message_id"),
        CheckConstraint("not_before < expires_at", name="window"),
        # Serves the worker's search for entries that are due.
        Index("ix_telegram_deletions_not_before", "not_before"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    # A private chat's id, which is the user's Telegram id.
    chat_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(BigInteger)
    # Not tried before this time: when queued for most messages, 24 hours after sending
    # for dice (Telegram refuses earlier), and pushed back while a worker is trying it.
    not_before: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # 48 hours after the message was sent: after this Telegram refuses to delete it.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
