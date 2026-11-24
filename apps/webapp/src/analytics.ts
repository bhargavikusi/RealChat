// The analytics view's choices, kept by App so going back from an account opened
// from a dashboard returns to the same view. No chart code here: the charts are
// loaded only when Analytics is opened.

export type Period = "7d" | "30d" | "90d" | "12m";

export const PERIODS: Record<Period, string> = {
  "7d": "Last 7 days",
  "30d": "Last 30 days",
  "90d": "Last 90 days",
  "12m": "Last 12 months (by week)",
};

export type Section = "growth" | "moderation" | "revenue";

export const SECTIONS: Record<Section, string> = {
  growth: "Growth",
  moderation: "Moderation",
  revenue: "Revenue",
};

export interface AnalyticsChoice {
  section: Section;
  period: Period;
}

export const DEFAULT_ANALYTICS: AnalyticsChoice = {
  section: "growth",
  period: "30d",
};
