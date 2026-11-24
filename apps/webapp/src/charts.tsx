import { Bar, BarChart, CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

// Provisional colours, until the design pass.
export const COLORS = {
  primary: "#2481cc",
  second: "#34a853",
  third: "#f29900",
  fourth: "#a142f4",
  fifth: "#d93025",
  sixth: "#707579",
};

/** One bucket of a chart: its first India day, and its numbers by key. */
export type Row = { start: string; [key: string]: number | string };

export interface Series {
  key: string;
  label: string;
  color: string;
}

function label(start: string, weekly: boolean): string {
  const day = new Date(`${start}T00:00:00`);
  const text = day.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
  return weekly ? `Week of ${text}` : text;
}

/** A chart with a "Show data" table under it, for screen readers and exact numbers.
 * Bars are stacked when there is more than one series. */
export function Chart({
  title,
  data,
  series,
  weekly,
  bars = false,
}: {
  title: string;
  data: Row[];
  series: Series[];
  weekly: boolean;
  bars?: boolean;
}) {
  const rows: Row[] = data.map((bucket) => ({ ...bucket, label: label(bucket.start, weekly) }));
  const axes = (
    <>
      <CartesianGrid strokeDasharray="3 3" />
      <XAxis dataKey="label" fontSize={11} minTickGap={16} />
      <YAxis allowDecimals={false} fontSize={11} width={40} />
      <Tooltip />
      {series.length > 1 && <Legend />}
    </>
  );
  return (
    <section className="chart" aria-label={title}>
      <h3>{title}</h3>
      <div aria-hidden="true">
        <ResponsiveContainer width="100%" height={220}>
          {bars ? (
            <BarChart data={rows}>
              {axes}
              {series.map((item) => (
                <Bar key={item.key} dataKey={item.key} name={item.label} fill={item.color} stackId="all" />
              ))}
            </BarChart>
          ) : (
            <LineChart data={rows}>
              {axes}
              {series.map((item) => (
                <Line
                  key={item.key}
                  type="monotone"
                  dataKey={item.key}
                  name={item.label}
                  stroke={item.color}
                  dot={false}
                  isAnimationActive={false}
                />
              ))}
            </LineChart>
          )}
        </ResponsiveContainer>
      </div>
      <details>
        <summary>Show data</summary>
        <table className="data">
          <thead>
            <tr>
              <th scope="col">{weekly ? "Week" : "Day"}</th>
              {series.map((item) => (
                <th key={item.key} scope="col">
                  {item.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.start}>
                <th scope="row">{row.label}</th>
                {series.map((item) => (
                  <td key={item.key}>{row[item.key] ?? 0}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </section>
  );
}
