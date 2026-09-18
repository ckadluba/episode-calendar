import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";

type Series = { id: string; title: string; platform: string; description: string | null };
type Release = { release_type: string; release_at: string; url: string | null };
type Episode = {
  id: string; series_id: string; season_number: number | null; number: number | null;
  title: string; description: string | null; platform: string; releases: Release[];
};

const API_URL = (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, "") ?? "http://localhost:8000";
const TIMEZONE = "Europe/Vienna";
const CACHE_PREFIX = "episode-calendar-cache-v1";

type CachedValue<T> = { timestamp: number; data: T };

function readLocal<T>(key: string, fallback: T): T {
  try {
    const value = localStorage.getItem(key);
    if (value === null) return fallback;
    try {
      return JSON.parse(value) as T;
    } catch {
      // Preferences created by older versions were stored as plain strings.
      return value as T;
    }
  } catch {
    return fallback;
  }
}

function readCache<T>(key: string): CachedValue<T> | null {
  try {
    const value = JSON.parse(localStorage.getItem(key) ?? "null") as CachedValue<T> | null;
    return value && Array.isArray(value.data) && typeof value.timestamp === "number" ? value : null;
  } catch {
    return null;
  }
}

function writeCache<T>(key: string, data: T) {
  try {
    localStorage.setItem(key, JSON.stringify({ timestamp: Date.now(), data }));
  } catch {
    // Caching is best effort (for example, private browsing may disable storage).
  }
}

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

const SERIES_COLORS = [
  "#f0c674", "#78c6e8", "#d98bd8", "#8bd49c", "#f28f8f", "#b6a0f5", "#f5a96b", "#74d4c3",
];

/** Returns a stable, repeatable color for a series without storing extra state. */
export function seriesColor(seriesId: string) {
  let hash = 0;
  for (const character of seriesId) hash = (hash * 31 + character.charCodeAt(0)) | 0;
  return SERIES_COLORS[Math.abs(hash) % SERIES_COLORS.length];
}

function SeriesPicker({ series, selectedIds, onChange }: { series: Series[]; selectedIds: string[]; onChange: (ids: string[]) => void }) {
  const [open, setOpen] = useState(false);
  const pickerRef = useRef<HTMLDivElement>(null);
  const sortedSeries = [...series].sort((left, right) => left.title.localeCompare(right.title, "de"));
  const allSelected = sortedSeries.length > 0 && sortedSeries.every((item) => selectedIds.includes(item.id));
  const toggle = (id: string) => onChange(selectedIds.includes(id) ? selectedIds.filter((item) => item !== id) : [...selectedIds, id]);

  useEffect(() => {
    if (!open) return;
    const close = (event: MouseEvent) => {
      if (!pickerRef.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);

  return <div className="series-picker" ref={pickerRef}>
    <button className="series-picker-button" type="button" aria-haspopup="listbox" aria-expanded={open} onClick={() => setOpen((current) => !current)}>
      {allSelected ? "Alle Serien" : `${selectedIds.length} Serien ausgewählt`}<span className="series-picker-chevron" aria-hidden="true">⌄</span>
    </button>
    {open && <div className="series-picker-menu" role="listbox" aria-label="Serie">
      <button className="series-picker-option series-picker-all" type="button" role="option" aria-selected={allSelected} onClick={() => onChange(allSelected ? [] : sortedSeries.map((item) => item.id))}>Alle Serien</button>
      {sortedSeries.map((item) => <label className="series-picker-option" role="option" aria-selected={selectedIds.includes(item.id)} key={item.id}>
        <input type="checkbox" checked={selectedIds.includes(item.id)} onChange={() => toggle(item.id)} />
        <span className="series-dot" style={{ backgroundColor: seriesColor(item.id) }} aria-hidden="true" />{item.title} <span className="series-platform">({item.platform})</span>
      </label>)}
    </div>}
  </div>;
}

export function App() {
  const [week, setWeek] = useState<"current" | "next">(() => readLocal<"current" | "next">("episode-calendar-week", "current"));
  const [series, setSeries] = useState<Series[]>([]);
  const [episodes, setEpisodes] = useState<Episode[]>([]);
  const [selectedSeriesIds, setSelectedSeriesIds] = useState<string[]>([]);
  const [seriesSelectionInitialized, setSeriesSelectionInitialized] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => { localStorage.setItem("episode-calendar-week", week); }, [week]);
  useEffect(() => {
    if (!series.length || seriesSelectionInitialized) return;
    const saved = readLocal<unknown>("episode-calendar-series-selection", null);
    setSelectedSeriesIds(Array.isArray(saved) ? saved.filter((id): id is string => typeof id === "string" && series.some((item) => item.id === id)) : series.map((item) => item.id));
    setSeriesSelectionInitialized(true);
  }, [series, seriesSelectionInitialized]);
  useEffect(() => { if (seriesSelectionInitialized) localStorage.setItem("episode-calendar-series-selection", JSON.stringify(selectedSeriesIds)); }, [selectedSeriesIds, seriesSelectionInitialized]);
  useEffect(() => {
    const seriesKey = `${CACHE_PREFIX}:${API_URL}:series`;
    const episodesKey = `${CACHE_PREFIX}:${API_URL}:episodes:${week}:${TIMEZONE}`;
    const cachedSeries = readCache<Series[]>(seriesKey);
    const cachedEpisodes = readCache<Episode[]>(episodesKey);
    if (cachedSeries) setSeries(cachedSeries.data);
    if (cachedEpisodes) setEpisodes(cachedEpisodes.data);

    const load = async () => {
      setLoading(!cachedSeries || !cachedEpisodes); setError(null);
      try {
        const [seriesResponse, episodeResponse] = await Promise.all([
          fetch(`${API_URL}/api/v1/series`).then(async (response) => {
            if (!response.ok) throw new Error(`Serien konnten nicht geladen werden (${response.status}).`);
            return response.json();
          }),
          fetch(`${API_URL}/api/v1/episodes/${week}-week?timezone=${encodeURIComponent(TIMEZONE)}`).then(async (response) => {
            if (!response.ok) throw new Error(`Episoden konnten nicht geladen werden (${response.status}).`);
            return response.json();
          }),
        ]);
        if (!Array.isArray(seriesResponse) || !Array.isArray(episodeResponse)) throw new Error("Ungültige API-Antwort");
        writeCache(seriesKey, seriesResponse);
        writeCache(episodesKey, episodeResponse);
        setSeries(seriesResponse); setEpisodes(episodeResponse);
      } catch (cause) {
        if (!cachedSeries || !cachedEpisodes) setError(cause instanceof Error ? cause.message : "Die API ist nicht erreichbar.");
      }
      finally { setLoading(false); }
    };
    void load();
  }, [week]);

  const filtered = episodes.filter((episode) => selectedSeriesIds.includes(episode.series_id));
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
    <section className="filters" aria-label="Filter"><label>Serien<SeriesPicker series={series} selectedIds={selectedSeriesIds} onChange={setSelectedSeriesIds} /></label></section>
    {loading && <p className="status">Kalender wird geladen …</p>}
    {error && <p className="status error">{error}<br /><small>Prüfe, ob die API unter {API_URL} läuft.</small></p>}
    {!loading && !error && <section className="calendar">{days.map((day) => { const items = byDay.get(dateKey(day)) ?? []; return <article className="day" key={dateKey(day)}><h2>{dayLabel.format(day)}</h2>{items.length === 0 ? <p className="empty">Keine Episoden</p> : items.map((episode) => { const release = episode.releases.find((item) => item.release_type === "streaming") ?? episode.releases[0]; const show = seriesMap.get(episode.series_id); const color = seriesColor(episode.series_id); const colorStyle = { "--series-color": color } as CSSProperties; const content = <><div className="episode-time">{release && timeLabel.format(new Date(release.release_at))}</div><div><h3><span className="series-dot" style={{ backgroundColor: color }} aria-hidden="true" />{show?.title ?? "Unbekannte Serie"}</h3><p>{episode.season_number ? `S${episode.season_number} · ` : ""}{episode.number ? `E${episode.number} · ` : ""}{episode.title}</p><span className={`badge ${episode.platform}`}>{episode.platform}</span></div></>; return release?.url ? <a className="episode episode-link" style={colorStyle} href={release.url} target="_blank" rel="noreferrer" key={episode.id}>{content}</a> : <div className="episode" style={colorStyle} key={episode.id}>{content}</div>; })}</article>; })}</section>}
    {!loading && !error && filtered.length === 0 && <p className="status">Für diese Filter wurden keine Episoden gefunden.</p>}
  </main>;
}
