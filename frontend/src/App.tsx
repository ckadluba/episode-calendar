import { useEffect, useMemo, useState } from "react";

type Series = { id: string; title: string; platform: string; description: string | null };
type Release = { release_type: string; release_at: string; url: string | null };
type Episode = {
  id: string; series_id: string; season_number: number | null; number: number | null;
  title: string; description: string | null; platform: string; releases: Release[];
};

const API_URL = (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, "") ?? "http://localhost:8000";
const TIMEZONE = "Europe/Vienna";

function weekDays(week: "current" | "next") {
  const now = new Date();
  const day = (now.getDay() + 6) % 7;
  const monday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - day + (week === "next" ? 7 : 0));
  return Array.from({ length: 7 }, (_, offset) => {
    const date = new Date(monday); date.setDate(monday.getDate() + offset); return date;
  });
}

const dateKey = (date: Date) => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
const dayLabel = new Intl.DateTimeFormat("de-AT", { weekday: "long", day: "numeric", month: "short" });
const timeLabel = new Intl.DateTimeFormat("de-AT", { hour: "2-digit", minute: "2-digit" });

export function App() {
  const [week, setWeek] = useState<"current" | "next">("current");
  const [series, setSeries] = useState<Series[]>([]);
  const [episodes, setEpisodes] = useState<Episode[]>([]);
  const [platform, setPlatform] = useState(localStorage.getItem("episode-calendar-platform") ?? "all");
  const [seriesId, setSeriesId] = useState(localStorage.getItem("episode-calendar-series") ?? "all");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => { localStorage.setItem("episode-calendar-platform", platform); }, [platform]);
  useEffect(() => { localStorage.setItem("episode-calendar-series", seriesId); }, [seriesId]);
  useEffect(() => {
    const load = async () => {
      setLoading(true); setError(null);
      try {
        const [seriesResponse, episodeResponse] = await Promise.all([
          fetch(`${API_URL}/api/v1/series`).then((response) => response.json()),
          fetch(`${API_URL}/api/v1/episodes/${week}-week?timezone=${encodeURIComponent(TIMEZONE)}`).then((response) => response.json()),
        ]);
        if (!Array.isArray(seriesResponse) || !Array.isArray(episodeResponse)) throw new Error("Ungültige API-Antwort");
        setSeries(seriesResponse); setEpisodes(episodeResponse);
      } catch (cause) { setError(cause instanceof Error ? cause.message : "Die API ist nicht erreichbar."); }
      finally { setLoading(false); }
    };
    void load();
  }, [week]);

  const platforms = useMemo(() => [...new Set(series.map((item) => item.platform))].sort(), [series]);
  const filtered = episodes.filter((episode) => (platform === "all" || episode.platform === platform) && (seriesId === "all" || episode.series_id === seriesId));
  const byDay = new Map<string, Episode[]>();
  filtered.forEach((episode) => {
    const release = episode.releases.find((item) => item.release_type === "streaming") ?? episode.releases[0];
    if (!release) return;
    const key = dateKey(new Date(release.release_at));
    byDay.set(key, [...(byDay.get(key) ?? []), episode]);
  });
  const seriesMap = new Map(series.map((item) => [item.id, item]));
  const days = weekDays(week);

 return <main className="app-shell">
    <header className="hero"><p className="eyebrow">EPISODE CALENDAR</p><h1>Was läuft diese Woche?</h1><p className="subtitle">Neue Episoden deiner Serien auf einen Blick.</p></header>
    <nav className="week-switch" aria-label="Woche"><button className={week === "current" ? "active" : ""} onClick={() => setWeek("current")}>Diese Woche</button><button className={week === "next" ? "active" : ""} onClick={() => setWeek("next")}>Nächste Woche</button></nav>
    <section className="filters" aria-label="Filter"><label>Plattform<select value={platform} onChange={(event) => { setPlatform(event.target.value); setSeriesId("all"); }}><option value="all">Alle Plattformen</option>{platforms.map((item) => <option key={item} value={item}>{item}</option>)}</select></label><label>Serie<select value={seriesId} onChange={(event) => setSeriesId(event.target.value)}><option value="all">Alle Serien</option>{series.filter((item) => platform === "all" || item.platform === platform).map((item) => <option key={item.id} value={item.id}>{item.title}</option>)}</select></label></section>
    {loading && <p className="status">Kalender wird geladen …</p>}
    {error && <p className="status error">{error}<br /><small>Prüfe, ob die API unter {API_URL} läuft.</small></p>}
    {!loading && !error && <section className="calendar">{days.map((day) => { const items = byDay.get(dateKey(day)) ?? []; return <article className="day" key={dateKey(day)}><h2>{dayLabel.format(day)}</h2>{items.length === 0 ? <p className="empty">Keine Episoden</p> : items.map((episode) => { const release = episode.releases.find((item) => item.release_type === "streaming") ?? episode.releases[0]; const show = seriesMap.get(episode.series_id); const content = <><div className="episode-time">{release && timeLabel.format(new Date(release.release_at))}</div><div><h3>{show?.title ?? "Unbekannte Serie"}</h3><p>{episode.season_number ? `S${episode.season_number} · ` : ""}{episode.number ? `E${episode.number} · ` : ""}{episode.title}</p><span className={`badge ${episode.platform}`}>{episode.platform}</span></div></>; return release?.url ? <a className="episode episode-link" href={release.url} target="_blank" rel="noreferrer" key={episode.id}>{content}</a> : <div className="episode" key={episode.id}>{content}</div>; })}</article>; })}</section>}
    {!loading && !error && filtered.length === 0 && <p className="status">Für diese Filter wurden keine Episoden gefunden.</p>}
  </main>;
}
