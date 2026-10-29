"""Terms of use and privacy notice shown at onboarding (D-015).

DRAFT for development only. It must be replaced with legally reviewed text before
launch (O-007). Changing the text requires a new TERMS_VERSION so users re-accept.

Only describe what the product actually does: blocking and reporting are added
here when those features ship, and the retention period once it is decided (O-002).
"""

TERMS_VERSION = "draft-5"

TERMS_TEXT = """\
DRAFT - not for launch

Welcome to RealChat. Before you start:

• You must be at least {minimum_age} years old.
• You chat anonymously with other people. Other users never see your Telegram name, \
username, or phone number. When you're matched, your chat partner sees your profile \
card: nickname, age, gender, country, and any city, bio, or photo you add.
• Messages pass through the RealChat bot. They are not end-to-end encrypted. \
Chat messages are stored for a limited time so that reports of abuse can be \
checked, then deleted automatically.
• Your chat partner can't forward or save what you send. Some Telegram apps also \
block screenshots of it, but not all of them do.
• To keep chats anonymous, contact cards, locations, links, phone numbers, and \
@usernames can't be sent.
• Be respectful. Harassment, threats, hate, sexual content involving minors, spam, \
scams, and sharing other people's private information are not allowed.
• Accounts that break these rules may be restricted or banned.
• You can delete your account at any time from your profile (Advanced settings). \
Your profile is erased immediately. A minimal record (your Telegram user ID, account \
status, and dates) is kept so that restrictions can't be avoided by signing up again."""

TERMS_QUESTION = "Do you agree to these terms and the privacy notice?"


def terms_body(minimum_age: int) -> str:
    """The terms alone, for reading again later (e.g. from /help)."""
    return TERMS_TEXT.format(minimum_age=minimum_age)


def terms_text(minimum_age: int) -> str:
    """The terms followed by the acceptance question, as shown at onboarding."""
    return f"{terms_body(minimum_age)}\n\n{TERMS_QUESTION}"
