// Display text for the values the API returns. The rules themselves live on the server.

export const REASONS: Record<string, string> = {
  spam: "Spam or scam",
  harassment: "Harassment or hate",
  sexual: "Sexual content without consent",
  under_18: "Appears under 18",
  threats: "Threats or violence",
  other: "Other",
};

/** The User Agreement's rules (section 6.2), as the bot names them in notices. */
export const RULES: Record<string, string> = {
  spam: "Spam, scams, or advertising",
  harassment: "Harassment, hate, or bullying",
  sexual: "Sexual content without consent",
  under_18: "Under 18 (RealChat is for adults only)",
  threats: "Threats or violence",
  impersonation: "Impersonation or a false age or identity",
  private_info: "Sharing someone's private information",
  evasion: "Getting around a restriction or abusing rewards or payments",
  other: "Other breach of the User Agreement",
};

export const SUSPENSION_DAYS = [1, 3, 7, 30] as const;

export const ACTIONS: Record<string, string> = {
  warn: "Warning",
  suspend: "Suspension",
  ban: "Ban",
  lift: "Lifted",
};

export const KINDS: Record<string, string> = {
  photo: "Photo",
  video: "Video",
  animation: "GIF",
  audio: "Audio",
  voice: "Voice message",
  video_note: "Video message",
  sticker: "Sticker",
  document: "File",
  poll: "Poll",
  dice: "Dice",
  other: "Unsupported message",
};

/** Why an action was refused, by the server's reason. */
export const REFUSALS: Record<string, string> = {
  report_closed: "Someone already handled this report.",
  protected: "Only admins can act on moderators and admins.",
  banned: "This account is already banned.",
  not_allowed: "You can't take moderation actions right now.",
  admin_only: "Only admins can lift a ban.",
  not_restricted: "This account isn't restricted.",
};

export function when(iso: string): string {
  return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}
