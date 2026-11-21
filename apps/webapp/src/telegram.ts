// The parts of Telegram's Mini App object (window.Telegram.WebApp) the panel uses.
// See https://core.telegram.org/bots/webapps#initializing-mini-apps.

interface BackButton {
  show(): void;
  hide(): void;
  onClick(callback: () => void): void;
  offClick(callback: () => void): void;
}

interface WebApp {
  /** The signed launch data, sent to the server with every request. */
  initData: string;
  colorScheme: "light" | "dark";
  BackButton?: BackButton;
  ready(): void;
  expand(): void;
  showConfirm?(message: string, callback: (confirmed: boolean) => void): void;
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

/** Ask before an action: Telegram's own confirmation popup, or the browser's outside
 * Telegram. */
export function confirmAction(message: string): Promise<boolean> {
  const app = webApp();
  if (app?.showConfirm) {
    const show = app.showConfirm.bind(app);
    return new Promise((resolve) => show(message, resolve));
  }
  return Promise.resolve(window.confirm(message));
}
