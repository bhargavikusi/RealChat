"""Terms of use and privacy notice shown at onboarding (D-015).

DRAFT for development only. It must be replaced with legally reviewed text before
launch (O-007). Changing the text requires a new TERMS_VERSION so users re-accept.

Only describe what the product actually does. The retention period named here must
match the admin setting (D-046); changing either means updating the other.
"""

TERMS_VERSION = "draft-8"

TERMS_TEXT = """\
DRAFT - not for launch

Welcome to RealChat. Before you start:

• You must be at least {minimum_age} years old.
• You chat anonymously with other people. Other users never see your Telegram name, \
username, or phone number. When you're matched, your chat partner sees your profile \
card: nickname, age, gender, country, and any city, bio, or photo you add.
• Messages pass through the RealChat bot. They are not end-to-end encrypted. \
Chat messages are stored so that reports of abuse can be checked. They are deleted \
automatically after 7 days, unless they are part of a report that is still open. \
Deleting them from RealChat doesn't remove them from your Telegram chat.
• Your chat partner can't forward or save what you send. Some Telegram apps also \
block screenshots of it, but not all of them do.
• To keep chats anonymous, contact cards and locations can't be sent, and messages \
or captions containing links, phone numbers, or @usernames are not delivered.
• Be respectful. Harassment, threats, hate, sexual content involving minors, spam, \
scams, and sharing other people's private information are not allowed.
• You can block or report a chat partner from their profile card or after a chat. \
Blocking or reporting someone you're chatting with ends the chat, and they are only \
told that the chat ended. Reporting also blocks them. Blocking doesn't stop you being \
matched again in a random chat.
• Accounts that break these rules may be restricted or banned.
• You can delete your account at any time from your profile (Advanced settings). \
Your profile is erased immediately. A minimal record (your Telegram user ID, account \
status, and dates) is kept so that restrictions can't be avoided by signing up again. \
Blocks and reports you made or received are kept too."""

TERMS_QUESTION = "Do you agree to these terms and the privacy notice?"


def terms_body(minimum_age: int) -> str:
    """The terms alone, for reading again later (e.g. from /help)."""
    return TERMS_TEXT.format(minimum_age=minimum_age)


def terms_text(minimum_age: int) -> str:
    """The terms followed by the acceptance question, as shown at onboarding."""
    return f"{terms_body(minimum_age)}\n\n{TERMS_QUESTION}"
