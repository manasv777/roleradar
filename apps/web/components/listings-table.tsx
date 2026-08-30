'use client';

import React from 'react';
import type { Listing, Term } from '@/lib/api';

function Confidence({ listing }: { listing: Listing }) {
  const value = listing.classify_confidence;
  const rank = value >= 0.8 ? 'high' : value >= 0.5 ? 'mid' : 'low';
  return (
    <span
      className={`dot ${rank}`}
      /* The reason trace is the whole point: a classification the user cannot
         inspect is one they cannot trust. */
      title={listing.classify_reasons.join('\n') || 'no reasons recorded'}
    />
  );
}

function Fit({ listing }: { listing: Listing }) {
  const advice = listing.advice;
  if (!advice || advice.fit_ratio === undefined) return <span className="mono">—</span>;
  const matched = advice.matched?.length ?? 0;
  const total = matched + (advice.missing?.length ?? 0);
  if (!total) return <span className="mono">—</span>;
  return (
    <span className="mono" title={advice.verdict ?? ''}>
      {matched}/{total} met
    </span>
  );
}

function Deadline({ listing }: { listing: Listing }) {
  if (!listing.deadline) {
    return <span className="mono" title="No deadline published — treat as rolling">rolling</span>;
  }
  const days = Math.ceil(
    (new Date(listing.deadline).getTime() - Date.now()) / 86_400_000
  );
  const tone = days < 3 ? 'danger' : days < 14 ? 'warn' : 'muted';
  return (
    <span className={`chip ${tone} mono`} title={`source: ${listing.deadline_source}`}>
      {listing.deadline}
    </span>
  );
}

export function ListingsTable({
  listings,
  terms,
  onDismiss,
}: {
  listings: Listing[];
  terms: Term[];
  onDismiss: (id: string) => void;
}) {
  const termLabel = React.useMemo(() => {
    const map = new Map(terms.map((t) => [t.id, t.label]));
    return (id: string | null) => (id ? map.get(id) ?? id : '—');
  }, [terms]);

  if (listings.length === 0) {
    return (
      <div className="empty">
        <p>No listings match these filters.</p>
        <p className="mono">Widen your fields, or run a scout.</p>
      </div>
    );
  }

  return (
    <table>
      <thead>
        <tr>
          <th style={{ width: 18 }} />
          <th>Role</th>
          <th style={{ width: 150 }}>Fields</th>
          <th style={{ width: 120 }}>Term</th>
          <th style={{ width: 90 }}>Fit</th>
          <th style={{ width: 110 }}>Deadline</th>
          <th style={{ width: 120 }}>Apply</th>
        </tr>
      </thead>
      <tbody>
        {listings.map((listing) => (
          <tr key={listing.listing_id}>
            <td><Confidence listing={listing} /></td>
            <td>
              <span className="role">
                {listing.title}
                {!listing.active && (
                  <span
                    className="chip danger mono"
                    title="Closed upstream — kept because it was seen open"
                  >
                    closed
                  </span>
                )}
                {listing.needs_verification && (
                  <span
                    className="chip warn mono"
                    title="Work-authorization read is inferred, never published. Confirm with the employer."
                  >
                    verify
                  </span>
                )}
              </span>
              <span className="meta mono">
                {listing.company_url ? (
                  <a href={listing.company_url} target="_blank" rel="noopener noreferrer">
                    {listing.company}
                  </a>
                ) : (
                  listing.company
                )}
                {listing.locations[0] ? ` · ${listing.locations[0]}` : ''}
                {listing.is_remote ? ' · remote' : ''}
              </span>
            </td>
            <td>
              {listing.specialties.length === 0 ? (
                <span className="chip muted mono">unlabelled</span>
              ) : (
                listing.specialties.map((s) => (
                  <span key={s} className="chip mono">{s.split('/')[1]}</span>
                ))
              )}
            </td>
            <td className="mono">
              {termLabel(listing.term_id)}
              <span className="meta mono">{listing.role_type.replace('_', ' ')}</span>
            </td>
            <td><Fit listing={listing} /></td>
            <td><Deadline listing={listing} /></td>
            <td>
              <a className="mono" href={listing.apply_url} target="_blank" rel="noopener noreferrer">
                apply →
              </a>
              <br />
              <button className="mono" style={{ marginTop: 4 }} onClick={() => onDismiss(listing.listing_id)}>
                dismiss
              </button>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
