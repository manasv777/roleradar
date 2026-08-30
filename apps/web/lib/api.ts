/**
 * API client.
 *
 * Note what is absent: a union type of term ids. The original had one, which
 * meant the client shipped with a hardcoded list of seasons that silently went
 * stale. Terms are plain strings and their labels come from `/api/v1/terms`.
 */

const BASE = '/api/v1';

export type RoleType = 'internship' | 'new_grad' | 'unknown';

export interface Term {
  id: string;
  label: string;
  season: string;
  year: number;
}

export interface Specialty {
  id: string;
  key: string;
  label: string;
  skills: string[];
}

export interface Domain {
  id: string;
  label: string;
  specialties: Specialty[];
}

export interface Taxonomy {
  version: number;
  domains: Domain[];
}

export interface Advice {
  fit_ratio?: number;
  matched?: string[];
  missing?: string[];
  verdict?: string;
  logistics?: string[];
  priority?: number;
  urgency?: string;
}

export interface Listing {
  listing_id: string;
  source_id: string;
  company: string;
  title: string;
  company_url: string | null;
  apply_url: string;
  locations: string[];
  is_remote: boolean;
  term_id: string | null;
  role_type: RoleType;
  domains: string[];
  specialties: string[];
  classify_confidence: number;
  classify_reasons: string[];
  work_auth_score: number | null;
  needs_verification: boolean;
  deadline: string | null;
  deadline_source: string;
  ats_vendor: string | null;
  advice: Advice | null;
  active: boolean;
  dismissed: boolean;
  first_seen_at: string | null;
}

export interface ListingPage {
  listings: Listing[];
  total: number;
  counts_by_term: Record<string, number>;
}

export interface Source {
  source_id: string;
  url: string;
  enabled: boolean;
  last_checked_at: string | null;
  last_success_at: string | null;
  last_status: string | null;
  last_error: string | null;
  record_count: number;
  note: string;
}

export interface RunState {
  run_id: string;
  phase: string;
  started_at: string;
  finished_at: string | null;
  sources_ok: number;
  sources_error: number;
  listings_new: number;
  enriched_ok: number;
  notifications_sent: number;
  errors: string[];
}

export interface ListingQuery {
  term_id?: string;
  role_type?: string;
  domain?: string;
  specialty?: string;
  min_confidence?: number;
  include_inactive?: boolean;
  limit?: number;
  offset?: number;
}

async function get<T>(path: string): Promise<T> {
  const response = await fetch(`${BASE}${path}`, { cache: 'no-store' });
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return (await response.json()) as T;
}

export async function listListings(query: ListingQuery = {}): Promise<ListingPage> {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== '' && value !== false) {
      params.set(key, String(value));
    }
  }
  return get<ListingPage>(`/listings?${params.toString()}`);
}

export const listTerms = () => get<Term[]>('/terms');
export const getTaxonomy = () => get<Taxonomy>('/taxonomy');
export const listSources = () => get<Source[]>('/sources');
export const getCurrentRun = () => get<RunState | null>('/runs/current');

export async function startRun(): Promise<RunState> {
  const response = await fetch(`${BASE}/runs`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{}',
  });
  if (response.status === 409) throw new Error('A scout run is already in flight.');
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return (await response.json()) as RunState;
}

export async function updateListing(
  id: string,
  patch: { dismissed?: boolean; deadline?: string }
): Promise<Listing> {
  const response = await fetch(`${BASE}/listings/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(patch),
  });
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return (await response.json()) as Listing;
}

export const ACTIVE_PHASES = new Set([
  'queued',
  'fetching',
  'classifying',
  'enriching',
  'advising',
  'reporting',
]);

export const isRunActive = (run: RunState | null) =>
  run !== null && ACTIVE_PHASES.has(run.phase) && !run.finished_at;
