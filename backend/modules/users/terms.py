"""Terms of use and privacy notice shown at onboarding (D-015).

DRAFT for development only. It must be replaced with legally reviewed text before
launch (O-007). Changing the text requires a new TERMS_VERSION so users re-accept.
"""

TERMS_VERSION = "draft-1"

TERMS_TEXT = """\
DRAFT - not for launch

Welcome to RealChat. Before you start:

• You must be at least {minimum_age} years old.
• You chat anonymously with other people. Other users see only your nickname and \
the profile details you choose to add — never your Telegram name, username, or phone number.
• Messages pass through the RealChat bot. They are not end-to-end encrypted. \
Conversations may be kept for a limited time to investigate reports and abuse.
• Be respectful. Harassment, threats, hate, sexual content involving minors, spam, \
scams, and sharing other people's private information are not allowed.
• You can block or report anyone, and you can delete your account at any time.
• Accounts that break these rules may be restricted or banned.

Do you agree to these terms and the privacy notice?"""


def terms_text(minimum_age: int) -> str:
    return TERMS_TEXT.format(minimum_age=minimum_age)
