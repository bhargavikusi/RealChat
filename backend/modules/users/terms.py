"""Terms of use and privacy notice shown at onboarding (D-015).

DRAFT for development only. It must be replaced with legally reviewed text before
launch (O-007). Changing the text requires a new TERMS_VERSION so users re-accept.

Only describe what the product actually does: blocking, reporting, and account
deletion are added here when those features ship, and the retention period once
it is decided (O-002).
"""

TERMS_VERSION = "draft-2"

TERMS_TEXT = """\
DRAFT - not for launch

Welcome to RealChat. Before you start:

• You must be at least {minimum_age} years old.
• You chat anonymously with other people. Other users see only your nickname and \
the profile details you choose to add — never your Telegram name, username, or phone number.
• Messages pass through the RealChat bot. They are not end-to-end encrypted. \
Conversations may be kept to investigate reports and abuse; how long is not decided yet.
• Be respectful. Harassment, threats, hate, sexual content involving minors, spam, \
scams, and sharing other people's private information are not allowed.
• Accounts that break these rules may be restricted or banned."""

TERMS_QUESTION = "Do you agree to these terms and the privacy notice?"


def terms_body(minimum_age: int) -> str:
    """The terms alone, for reading again later (e.g. from /help)."""
    return TERMS_TEXT.format(minimum_age=minimum_age)


def terms_text(minimum_age: int) -> str:
    """The terms followed by the acceptance question, as shown at onboarding."""
    return f"{terms_body(minimum_age)}\n\n{TERMS_QUESTION}"
