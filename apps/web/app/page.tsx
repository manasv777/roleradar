'use client';

import React from 'react';
import { ListingsTable } from '@/components/listings-table';
import {
  getCurrentRun,
  getTaxonomy,
  isRunActive,
  listListings,
  listTerms,
  startRun,
  updateListing,
  type Domain,
  type Listing,
  type RunState,
  type Term,
} from '@/lib/api';

// A bucket holds hundreds of listings. Fetching them all is what made most of
// them unreachable in the tool this replaced.
const PAGE_SIZE = 50;
const POLL_MS = 5000;

export default function Home() {
  const [listings, setListings] = React.useState<Listing[]>([]);
  const [total, setTotal] = React.useState(0);
  const [terms, setTerms] = React.useState<Term[]>([]);
  const [domains, setDomains] = React.useState<Domain[]>([]);
  const [run, setRun] = React.useState<RunState | null>(null);

  // Filters live in the URL. Without this the page gave no way to confirm what
  // was actually filtered: leaving Role on "Any" while picking a field shows
  // that field's internships, which reads exactly like the filter being ignored.
  const initial =
    typeof window === 'undefined'
      ? new URLSearchParams()
      : new URLSearchParams(window.location.search);
  const [term, setTerm] = React.useState(initial.get('term') ?? '');
  const [roleType, setRoleType] = React.useState(initial.get('role') ?? '');
  const [specialty, setSpecialty] = React.useState(initial.get('field') ?? '');
  const [page, setPage] = React.useState(0);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => {
    Promise.all([listTerms(), getTaxonomy()])
      .then(([t, tax]) => {
        setTerms(t);
        setDomains(tax.domains);
      })
      .catch((e) => setError(String(e)));
  }, []);

  const load = React.useCallback(async () => {
    setLoading(true);
    try {
      const data = await listListings({
        term_id: term || undefined,
        role_type: roleType || undefined,
        specialty: specialty || undefined,
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
      });
      setListings(data.listings);
      setTotal(data.total);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [term, roleType, specialty, page]);

  React.useEffect(() => {
    void load();
  }, [load]);

  // Changing what is listed invalidates the page number.
  React.useEffect(() => {
    setPage(0);
  }, [term, roleType, specialty]);

  React.useEffect(() => {
    if (!isRunActive(run)) return;
    const id = setInterval(async () => {
      try {
        const next = await getCurrentRun();
        setRun(next);
        if (next && !isRunActive(next)) void load();
      } catch {
        /* a transient poll failure is not worth surfacing */
      }
    }, POLL_MS);
    return () => clearInterval(id);
  }, [run, load]);

  React.useEffect(() => {
    const params = new URLSearchParams();
    if (term) params.set('term', term);
    if (roleType) params.set('role', roleType);
    if (specialty) params.set('field', specialty);
    if (page) params.set('page', String(page + 1));
    const query = params.toString();
    window.history.replaceState(null, '', query ? `?${query}` : window.location.pathname);
  }, [term, roleType, specialty, page]);

  const termLabels = React.useMemo(
    () => new Map(terms.map((t) => [t.id, t.label])),
    [terms]
  );
  const specialtyLabels = React.useMemo(() => {
    const map = new Map<string, string>();
    for (const d of domains) for (const s of d.specialties) map.set(s.key, s.label);
    return map;
  }, [domains]);

  const activeFilters: Array<[string, string]> = [
    ...(roleType
      ? ([['Role', roleType === 'full_time' ? 'Full-time' : roleType.replace('_', ' ')]] as Array<[string, string]>)
      : []),
    ...(specialty
      ? ([['Field', specialtyLabels.get(specialty) ?? specialty]] as Array<[string, string]>)
      : []),
    ...(term ? ([['Term', termLabels.get(term) ?? term]] as Array<[string, string]>) : []),
  ];

  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const pageNumbers = React.useMemo(() => {
    const wanted = new Set<number>([0, pageCount - 1]);
    for (let i = page - 2; i <= page + 2; i += 1) {
      if (i >= 0 && i < pageCount) wanted.add(i);
    }
    const out: Array<number | null> = [];
    let previous: number | null = null;
    for (const n of [...wanted].sort((a, b) => a - b)) {
      if (previous !== null && n - previous > 1) out.push(null);
      out.push(n);
      previous = n;
    }
    return out;
  }, [page, pageCount]);

  const dismiss = async (id: string) => {
    setListings((prev) => prev.filter((l) => l.listing_id !== id));
    try {
      await updateListing(id, { dismissed: true });
    } catch {
      void load();
    }
  };

  return (
    <main className="wrap">
      <header className="masthead">
        <h1>roleradar</h1>
        <p className="mono">
          {total.toLocaleString()} listings in your fields · {terms.length} terms in range
        </p>
      </header>

      <div className="filters">
        <label>
          <span className="mono">Term</span>
          <select value={term} onChange={(e) => setTerm(e.target.value)}>
            <option value="">All terms</option>
            {terms.map((t) => (
              <option key={t.id} value={t.id}>{t.label}</option>
            ))}
          </select>
        </label>

        <label>
          <span className="mono">Role</span>
          <select value={roleType} onChange={(e) => setRoleType(e.target.value)}>
            <option value="">Any</option>
            <option value="internship">Internship</option>
            <option value="new_grad">New grad</option>
            <option value="full_time">Full-time</option>
          </select>
        </label>

        <label>
          <span className="mono">Field</span>
          <select value={specialty} onChange={(e) => setSpecialty(e.target.value)}>
            <option value="">All fields</option>
            {domains.map((d) => (
              <optgroup key={d.id} label={d.label}>
                {d.specialties.map((s) => (
                  <option key={s.key} value={s.key}>{s.label}</option>
                ))}
              </optgroup>
            ))}
          </select>
        </label>
      </div>

      <div className="filters" style={{ borderBottom: 'none', paddingBottom: 4 }}>
        {activeFilters.length === 0 ? (
          <span className="mono">
            Showing everything — no filters. Pick Role <strong>Full-time</strong> to
            exclude internships.
          </span>
        ) : (
          <>
            <span className="mono">Showing</span>
            {activeFilters.map(([label, value]) => (
              <span className="chip mono" key={label}>
                {label}: {value}
              </span>
            ))}
            <span className="mono">· {total.toLocaleString()} match{total === 1 ? '' : 'es'}</span>
            <button
              className="mono"
              onClick={() => {
                setRoleType('');
                setSpecialty('');
                setTerm('');
              }}
            >
              Clear
            </button>
          </>
        )}
      </div>

      {error && <div className="banner"><strong>Could not load listings.</strong> {error}</div>}

      {loading ? (
        <p className="mono" style={{ padding: '32px 0' }}>Loading…</p>
      ) : (
        <>
          <ListingsTable listings={listings} terms={terms} onDismiss={dismiss} />
          {pageCount > 1 && (
            <nav className="pager" aria-label="Listing pages">
              <span className="count mono">
                {page * PAGE_SIZE + 1}–{Math.min((page + 1) * PAGE_SIZE, total)} of {total}
              </span>
              <button onClick={() => setPage((n) => Math.max(0, n - 1))} disabled={page === 0}>
                Prev
              </button>
              {pageNumbers.map((n, i) =>
                n === null ? (
                  <span key={`gap${i}`} className="mono">…</span>
                ) : (
                  <button
                    key={n}
                    onClick={() => setPage(n)}
                    aria-current={n === page ? 'page' : undefined}
                    className={n === page ? 'active' : ''}
                  >
                    {n + 1}
                  </button>
                )
              )}
              <button
                onClick={() => setPage((n) => Math.min(pageCount - 1, n + 1))}
                disabled={page >= pageCount - 1}
              >
                Next
              </button>
            </nav>
          )}
        </>
      )}

      <div className="runbar">
        <button
          onClick={async () => {
            try {
              setRun(await startRun());
            } catch (e) {
              setError(e instanceof Error ? e.message : String(e));
            }
          }}
          disabled={isRunActive(run)}
        >
          {isRunActive(run) ? 'Scouting…' : 'Run scout'}
        </button>
        <span className="mono">
          {run ? `${run.phase} · ${run.listings_new} new` : 'idle'}
        </span>
        {run && run.sources_error > 0 && (
          <span className="chip danger mono">{run.sources_error} source error(s)</span>
        )}
      </div>
    </main>
  );
}
