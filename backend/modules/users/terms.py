"""Terms of use and privacy notice shown at onboarding (D-015).

DRAFT for development only. It must be replaced with legally reviewed text before
launch (O-007). Changing the text requires a new TERMS_VERSION so users re-accept.

Only describe what the product actually does. Periods that admins can change, such as
message retention, are not given as numbers, so the text stays true when they change
(D-046).
"""

TERMS_VERSION = "draft-18"

TERMS_TEXT = """\
DRAFT - not for launch

Welcome to RealChat. Before you start:

• You must be at least {minimum_age} years old.
• You chat anonymously with other people. Other users never see your Telegram name, \
username, or phone number. When you're matched, your chat partner sees your profile \
card: nickname, age, gender, country, and any city, bio, or photo you add. People \
you've chatted with can still see your profile card for a while after the chat. \
Your profile card shows your RealChat ID, and anyone who has it can open your \
profile card. A new ID is given if you delete your account and come back. \
When you view someone's profile card, they can see that you visited, and they may be \
told. When you like someone's profile, they can see that you liked it while the like \
stands, and they may be told.
• Messages pass through the RealChat bot. They are not end-to-end encrypted. \
Chat messages are stored so that reports of abuse can be checked. They are deleted \
automatically after a set period, unless they are part of a report that is still open. \
Deleting them from RealChat doesn't remove them from your Telegram chat.
• You can send a direct message to someone from their profile card, for coins. They \
see your nickname and RealChat ID with it, and you're told when they open it. Telegram \
apps that honour RealChat's protection don't let them forward or save it. Direct \
messages are stored so they can be opened, and deleted automatically after the same \
set period as chat messages.
• You can send someone a chat request from their profile card, for coins, when they \
were recently active in RealChat. They see your nickname and RealChat ID. The coins \
are taken once the request is delivered and aren't returned if it's declined, expires, \
or you don't switch to them when they accept. If either of you is in a chat when it's \
accepted, that chat ends.
• You can pay coins for one alert the next time someone uses RealChat. Unless you \
turn this off in Advanced settings, others can set such an alert about you; you're \
not told who. It's based on your use of RealChat, never your Telegram online status. \
The coins come back if the alert can't be sent within a set period, but not if you \
cancel it.
• While a daily reward is offered, RealChat may remind you to claim it, at most once a \
day: about the time you usually do, or at a set local hour in your country. You can \
turn reminders off in Advanced settings.
• Telegram apps that honour RealChat's protection don't let your chat partner forward \
or save what you send. Some Telegram apps also block screenshots of it, but not all of \
them do.
• To keep chats anonymous, contact cards and locations can't be sent, and messages \
or captions containing links, phone numbers, or @usernames are not delivered.
• Be respectful. Harassment, threats, hate, sexual content involving minors, spam, \
scams, and sharing other people's private information are not allowed.
• You can block or report a chat partner from their profile card or after a chat, \
and anyone whose profile card you opened, or who opened yours, from their card. \
Blocking or reporting someone you're chatting with ends the chat, and they are only \
told that the chat ended. Reporting also blocks them. Blocking doesn't stop you being \
matched again in a random chat, or being told when they visit or like your profile. \
Neither of you can send the other direct messages or chat requests while either has \
blocked the other.
• Coins are a virtual in-app currency. They have no cash value and can't be exchanged \
for money or transferred to other users.
• Accounts that break these rules may be restricted or banned.
• You can delete your account at any time from your profile (Advanced settings). \
Your profile is erased immediately, and so are your coins and your invite link; a \
new account doesn't get one-time rewards, like the welcome bonus, again. A minimal \
record (your Telegram user ID, account status, and dates) is kept so that \
restrictions can't be avoided by signing up again. Blocks and reports you made or \
received are kept too."""

TERMS_QUESTION = "Do you agree to these terms and the privacy notice?"


def terms_body(minimum_age: int) -> str:
    """The terms alone, for reading again later (e.g. from /help)."""
    return TERMS_TEXT.format(minimum_age=minimum_age)


def terms_text(minimum_age: int) -> str:
    """The terms followed by the acceptance question, as shown at onboarding."""
    return f"{terms_body(minimum_age)}\n\n{TERMS_QUESTION}"
