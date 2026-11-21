// The parts of Telegram's Mini App object (window.Telegram.WebApp) the panel uses.
// See https://core.telegram.org/bots/webapps#initializing-mini-apps.

interface WebApp {
  /** The signed launch data, sent to the server with every request. */
  initData: string;
  colorScheme: "light" | "dark";
  ready(): void;
  expand(): void;
}

declare global {
  interface Window {
    Telegram?: { WebApp?: WebApp };
  }
}

/** Telegram's Mini App object, or undefined outside Telegram. */
export function webApp(): WebApp | undefined {
  return window.Telegram?.WebApp;
}
