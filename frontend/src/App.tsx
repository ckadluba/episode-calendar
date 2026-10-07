import { useEffect, useMemo, useState, type CSSProperties } from "react";

type Series = { id: string; title: string; platform: string; platform_id: string; description: string | null };
export type Release = { release_type: string; release_at: string; url: string | null; preview: boolean };
type Episode = {
  id: string; series_id: string; season_number: number | null; number: number | null;
  title: string; description: string | null; platform: string; platform_id: string; releases: Release[];
};
type CalendarItem = { episode: Episode; release: Release };

const API_URL = (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, "") ?? "http://localhost:8000";
const TIMEZONE = "Europe/Vienna";
const CACHE_PREFIX = "episode-calendar-cache-v2";
const PREVIEW_PREFERENCES_KEY = "episode-calendar-preview-preferences";
const PREVIEW_UNAVAILABLE_PLATFORMS = new Set(["bbc_iplayer", "channel4", "ardmediathek", "stv", "amazon_prime_de", "amazon_prime_uk"]);

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

type CalendarWeek = "last" | "current" | "next";
type PreviewPreferences = Record<string, boolean>;

function weekDays(week: CalendarWeek) {
  const now = new Date();
  const day = (now.getDay() + 6) % 7;
  const weekOffset = week === "last" ? -7 : week === "next" ? 7 : 0;
  const monday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - day + weekOffset);
  return Array.from({ length: 7 }, (_, offset) => {
    const date = new Date(monday); date.setDate(monday.getDate() + offset); return date;
  });
}

const dateKey = (date: Date) => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
const dayLabel = new Intl.DateTimeFormat("de-AT", { weekday: "short", day: "numeric", month: "short" });
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

export function episodeStartLabel(episode: Pick<Episode, "season_number" | "number">) {
  if (episode.number !== 1) return null;
  return episode.season_number === null || episode.season_number === 1 ? "Neue Serie" : "Neue Staffel";
}

export function calendarItems(episodes: Episode[]): CalendarItem[] {
  return episodes.flatMap((episode) => episode.releases.map((release) => ({ episode, release })));
}

export function selectCalendarRelease(
  episode: Episode,
  includePreview: boolean,
  from?: Date,
  to?: Date,
): Release | null {
  // Select one canonical release for the episode before applying the calendar
  // window. Otherwise the same episode can appear once as a preview and again
  // on its later regular/TV release date in another calendar week.
  const releases = episode.releases;
  const catalogReleases = releases.filter((release) => release.release_type === "streaming");
  const regularCatalogReleases = catalogReleases.filter((release) => !release.preview);
  const tvReleases = releases.filter((release) => release.release_type === "tv_broadcast");
  const previewReleases = catalogReleases.filter((release) => release.preview);
  const newest = (items: Release[]) => items.reduce<Release | null>(
    (latest, release) => !latest || Date.parse(release.release_at) > Date.parse(latest.release_at)
      ? release
      : latest,
    null,
  );

  const tv = newest(tvReleases);
  const regular = newest(regularCatalogReleases);
  // A regular streaming date after the linear premiere is a catalog artifact
  // (RTL+ catalog times are prereleases that precede their EPG airing) and must
  // not displace the premiere in the calendar.
  const canonical = tv && regular && Date.parse(regular.release_at) > Date.parse(tv.release_at)
    ? tv
    : (regular ?? tv);
  const selected = (includePreview ? newest(previewReleases) : null) ?? canonical;
  if (!selected) return null;

  const releaseAt = Date.parse(selected.release_at);
  return (from === undefined || releaseAt >= from.getTime())
    && (to === undefined || releaseAt < to.getTime())
    ? selected
    : null;
}

function compareNullableNumbers(left: number | null, right: number | null) {
  if (left === right) return 0;
  if (left === null) return 1;
  if (right === null) return -1;
  return left - right;
}

export function sortCalendarItems(items: CalendarItem[], seriesById: ReadonlyMap<string, Series>) {
  return [...items].sort((left, right) => {
    const byTime = Date.parse(left.release.release_at) - Date.parse(right.release.release_at);
    if (byTime !== 0) return byTime;

    const leftSeries = seriesById.get(left.episode.series_id)?.title ?? "Unbekannte Serie";
    const rightSeries = seriesById.get(right.episode.series_id)?.title ?? "Unbekannte Serie";
    const bySeries = leftSeries.localeCompare(rightSeries, "de");
    if (bySeries !== 0) return bySeries;

    const bySeason = compareNullableNumbers(left.episode.season_number, right.episode.season_number);
    if (bySeason !== 0) return bySeason;
    return compareNullableNumbers(left.episode.number, right.episode.number);
  });
}

function SeriesFilterPage({ series, selectedIds, previewPreferences, onSave, onDiscard }: {
  series: Series[];
  selectedIds: string[];
  previewPreferences: PreviewPreferences;
  onSave: (ids: string[], previewPreferences: PreviewPreferences) => void;
  onDiscard: () => void;
}) {
  const [draftIds, setDraftIds] = useState(selectedIds);
  const [draftPreviewPreferences, setDraftPreviewPreferences] = useState(previewPreferences);
  const [query, setQuery] = useState("");
  const normalizedQuery = query.trim().toLocaleLowerCase("de");
  const groups = useMemo(() => {
    const visible = series
      .filter((item) => item.title.toLocaleLowerCase("de").includes(normalizedQuery))
      .sort((left, right) => left.title.localeCompare(right.title, "de"));
    return [...new Set(visible.map((item) => item.platform))]
      .sort((left, right) => left.localeCompare(right, "de"))
      .map((platform) => {
        const platformSeries = visible.filter((item) => item.platform === platform);
        return { platform, platformId: platformSeries[0].platform_id, series: platformSeries };
      });
  }, [normalizedQuery, series]);

  const toggle = (id: string) => setDraftIds((current) => current.includes(id)
    ? current.filter((item) => item !== id)
    : [...current, id]);

  return <main className="app-shell filter-page">
    <header className="hero"><p className="eyebrow">EPISODE CALENDAR</p><h1>Serien filtern</h1><p className="subtitle">Wähle aus, welche Serien im Kalender erscheinen.</p></header>
    <div className="filter-actions filter-actions-top">
      <button className="secondary-button" type="button" onClick={() => setDraftIds(series.map((item) => item.id))}>Alle auswählen</button>
      <button className="secondary-button" type="button" onClick={() => setDraftIds([])}>Keine auswählen</button>
    </div>
    <label className="filter-search">Serien durchsuchen
      <input type="search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="z. B. Celebrity" />
    </label>
    <section className="series-groups" aria-label="Serienauswahl">
      {groups.length === 0 && <p className="empty">Keine Serien gefunden.</p>}
      {groups.map((group) => <section className="series-group" key={group.platform}>
        <div className="provider-filter-header"><h2><label className="provider-filter-option">
          <input
            type="checkbox"
            aria-label={`Alle Serien von ${group.platform} auswählen`}
            checked={group.series.every((item) => draftIds.includes(item.id))}
            ref={(element) => {
              if (element) {
                element.indeterminate = group.series.some((item) => draftIds.includes(item.id))
                  && !group.series.every((item) => draftIds.includes(item.id));
              }
            }}
            onChange={() => {
              const groupIds = group.series.map((item) => item.id);
              const allSelected = groupIds.every((id) => draftIds.includes(id));
              setDraftIds((current) => allSelected
                ? current.filter((id) => !groupIds.includes(id))
                : [...new Set([...current, ...groupIds])]);
            }}
          />
          {group.platform}
        </label></h2>{!PREVIEW_UNAVAILABLE_PLATFORMS.has(group.platformId) && <label className="preview-filter-option">
            <input
              type="checkbox"
              aria-label={`Vorab-Releases für ${group.platform} anzeigen`}
              checked={draftPreviewPreferences[group.platformId] !== false}
              onChange={() => setDraftPreviewPreferences((current) => ({ ...current, [group.platformId]: current[group.platformId] === false }))}
            />
            Vorab-Releases anzeigen
          </label>}</div>
        {group.series.map((item) => <label className="series-filter-option" key={item.id}>
          <input type="checkbox" checked={draftIds.includes(item.id)} onChange={() => toggle(item.id)} />
          <span className="series-dot" style={{ backgroundColor: seriesColor(item.id) }} aria-hidden="true" />
          <span>{item.title}</span>
        </label>)}
      </section>)}
    </section>
    <div className="filter-actions">
      <button className="secondary-button" type="button" onClick={onDiscard}>Verwerfen</button>
      <button className="primary-button" type="button" onClick={() => onSave(draftIds, draftPreviewPreferences)}>Speichern</button>
    </div>
  </main>;
}

export function App() {
  const [week, setWeek] = useState<CalendarWeek>(() => readLocal<CalendarWeek>("episode-calendar-week", "current"));
  const [series, setSeries] = useState<Series[]>([]);
  const [episodes, setEpisodes] = useState<Episode[]>([]);
  const [selectedSeriesIds, setSelectedSeriesIds] = useState<string[]>([]);
  const [previewPreferences, setPreviewPreferences] = useState<PreviewPreferences>({});
  const [seriesSelectionInitialized, setSeriesSelectionInitialized] = useState(false);
  const [knownSeriesIds, setKnownSeriesIds] = useState<string[]>([]);
  const [filterOpen, setFilterOpen] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [hideCandidate, setHideCandidate] = useState<{ seriesId: string; title: string } | null>(null);

  useEffect(() => {
    if (!hideCandidate) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setHideCandidate(null);
    };
    document.addEventListener("keydown", closeOnEscape);
    return () => document.removeEventListener("keydown", closeOnEscape);
  }, [hideCandidate]);

  useEffect(() => { localStorage.setItem("episode-calendar-week", week); }, [week]);
  useEffect(() => {
    if (!series.length || seriesSelectionInitialized) return;
    const saved = readLocal<unknown>("episode-calendar-series-selection", null);
    const currentIds = series.map((item) => item.id);
    if (Array.isArray(saved)) {
      setSelectedSeriesIds(saved.filter((id): id is string => typeof id === "string" && currentIds.includes(id)));
    } else if (saved && typeof saved === "object" && "selected" in saved && "known" in saved
      && Array.isArray(saved.selected) && Array.isArray(saved.known)) {
      const selected = saved.selected.filter((id): id is string => typeof id === "string" && currentIds.includes(id));
      const known = saved.known.filter((id): id is string => typeof id === "string");
      setSelectedSeriesIds([...selected, ...currentIds.filter((id) => !known.includes(id))]);
    } else {
      setSelectedSeriesIds(currentIds);
    }
    setKnownSeriesIds(currentIds);
    setSeriesSelectionInitialized(true);
  }, [series, seriesSelectionInitialized]);
  useEffect(() => {
    if (!seriesSelectionInitialized) return;
    const currentIds = series.map((item) => item.id);
    const newIds = currentIds.filter((id) => !knownSeriesIds.includes(id));
    if (newIds.length) setSelectedSeriesIds((current) => [...current, ...newIds]);
    if (newIds.length) setKnownSeriesIds(currentIds);
  }, [knownSeriesIds, series, seriesSelectionInitialized]);
  useEffect(() => {
    if (seriesSelectionInitialized) {
      localStorage.setItem("episode-calendar-series-selection", JSON.stringify({ selected: selectedSeriesIds, known: knownSeriesIds }));
    }
  }, [knownSeriesIds, selectedSeriesIds, seriesSelectionInitialized]);
  useEffect(() => {
    if (!series.length) return;
    const saved = readLocal<unknown>(PREVIEW_PREFERENCES_KEY, {});
    const stored = saved && typeof saved === "object" ? saved as Record<string, unknown> : {};
    const platformIds = [...new Set(series.map((item) => item.platform_id))];
    setPreviewPreferences(Object.fromEntries(platformIds.map((platformId) => [
      platformId,
      stored[platformId] !== false,
    ])));
  }, [series]);
  useEffect(() => {
    if (Object.keys(previewPreferences).length) {
      localStorage.setItem(PREVIEW_PREFERENCES_KEY, JSON.stringify(previewPreferences));
    }
  }, [previewPreferences]);
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
          fetch(`${API_URL}/api/v1/episodes/${week}-week?timezone=${encodeURIComponent(TIMEZONE)}&includePreviews=all&includeReleaseHistory=true`).then(async (response) => {
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

  const days = weekDays(week);
  const todayKey = dateKey(new Date());
  const weekStart = days[0];
  const weekEnd = new Date(days[days.length - 1]);
  weekEnd.setDate(weekEnd.getDate() + 1);
  const filtered = episodes
    .filter((episode) => selectedSeriesIds.includes(episode.series_id))
    .flatMap((episode) => {
      const release = selectCalendarRelease(
        episode,
        previewPreferences[episode.platform_id] !== false,
        weekStart,
        weekEnd,
      );
      return release ? [{ episode, release }] : [];
    });
  const byDay = new Map<string, CalendarItem[]>();
  filtered.forEach((item) => {
    const key = dateKey(new Date(item.release.release_at));
    byDay.set(key, [...(byDay.get(key) ?? []), item]);
  });
  const seriesMap = new Map(series.map((item) => [item.id, item]));
  const hideSeries = (seriesId: string) => setSelectedSeriesIds((current) => current.filter((id) => id !== seriesId));
  if (filterOpen) return <SeriesFilterPage
    series={series}
    selectedIds={selectedSeriesIds}
    previewPreferences={previewPreferences}
    onSave={(ids, preferences) => { setSelectedSeriesIds(ids); setPreviewPreferences(preferences); setFilterOpen(false); }}
    onDiscard={() => setFilterOpen(false)}
  />;

  return <main className="app-shell">
    <header className="hero"><p className="eyebrow">EPISODE CALENDAR</p><h1>{week === "last" ? "Was lief letzte Woche?" : week === "next" ? "Was läuft nächste Woche?" : "Was läuft diese Woche?"}</h1><p className="subtitle">Neue Episoden deiner Serien auf einen Blick. Zukünftige Daten können unvollständig sein.</p></header>
    <nav className="week-switch" aria-label="Woche"><button className={week === "last" ? "active" : ""} onClick={() => setWeek("last")}>Letzte Woche</button><button className={week === "current" ? "active" : ""} onClick={() => setWeek("current")}>Diese Woche</button><button className={week === "next" ? "active" : ""} onClick={() => setWeek("next")}>Nächste Woche</button></nav>
    <section className="filters" aria-label="Filter"><button className="filter-button" type="button" disabled={!seriesSelectionInitialized} onClick={() => setFilterOpen(true)}>Filter</button><span>{selectedSeriesIds.length} von {series.length} Serien ausgewählt</span></section>
    {loading && <p className="status">Kalender wird geladen …</p>}
    {error && <p className="status error">{error}<br /><small>Prüfe, ob die API unter {API_URL} läuft.</small></p>}
    {!loading && !error && <section className="calendar">{days.map((day) => { const items = sortCalendarItems(byDay.get(dateKey(day)) ?? [], seriesMap); return <article className={dateKey(day) === todayKey ? "day day-today" : "day"} key={dateKey(day)}><h2>{dayLabel.format(day)}</h2>{items.length === 0 ? <p className="empty">Keine Episoden</p> : items.map(({ episode, release }) => { const show = seriesMap.get(episode.series_id); const color = seriesColor(episode.series_id); const colorStyle = { "--series-color": color } as CSSProperties; const startLabel = episodeStartLabel(episode); const className = `episode${startLabel ? " episode-new" : ""}`; const content = <><div className="episode-header"><div className="episode-time">{timeLabel.format(new Date(release.release_at))}</div><span className={`badge ${episode.platform_id}`}>{episode.platform}</span></div><div><h3><span className="series-dot" style={{ backgroundColor: color }} aria-hidden="true" />{show?.title ?? "Unbekannte Serie"}</h3><p>{episode.season_number ? `S${episode.season_number} · ` : ""}{episode.number ? `E${episode.number} · ` : ""}{episode.title}</p><div className="episode-badges">{startLabel && <span className="new-badge">{startLabel}</span>}{release.preview && <span className="preview-badge">Vorab</span>}</div></div></>; return <article className={className} style={colorStyle} key={`${episode.id}-${release.release_at}`}>{release.url ? <a className="episode-link" href={release.url} target="_blank" rel="noreferrer">{content}</a> : content}<button className="episode-hide" type="button" onClick={() => setHideCandidate({ seriesId: episode.series_id, title: show?.title ?? episode.platform })}>Serie ausblenden</button></article>; })}</article>; })}</section>}
    {!loading && !error && filtered.length === 0 && <p className="status">Für diese Filter wurden keine Episoden gefunden.</p>}
    <footer className="app-footer">Episode Calendar is open source under the <a href="https://www.apache.org/licenses/LICENSE-2.0" target="_blank" rel="noreferrer">Apache 2.0 license</a>. Created by <a href="https://github.com/ckadluba" target="_blank" rel="noreferrer">Christian Kadluba</a>. <a href="https://github.com/ckadluba/episode-calendar" target="_blank" rel="noreferrer">View the source on GitHub</a>.</footer>
    {hideCandidate && <div className="confirm-backdrop" onClick={() => setHideCandidate(null)}>
      <div className="confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="hide-series-title" onClick={(event) => event.stopPropagation()}>
        <h2 id="hide-series-title">{hideCandidate.title}</h2>
        <p>Willst du alle Folgen dieser Serie verstecken? Du kannst diese Einstellung jederzeit in der Filter Seite wieder ändern.</p>
        <div className="confirm-actions">
          <button className="secondary-button" type="button" autoFocus onClick={() => setHideCandidate(null)}>Nein</button>
          <button className="primary-button" type="button" onClick={() => { hideSeries(hideCandidate.seriesId); setHideCandidate(null); }}>Ja</button>
        </div>
      </div>
    </div>}
  </main>;
}
