# Configuration and Versioning

## Canonical configuration

YAML is accepted only as a seed, development, test, export, or import template. Production execution reads a validated immutable configuration snapshot from PostgreSQL. A strategy run stores the complete merged snapshot, not only references to source profiles.

Each snapshot stores:

~~~text
config_version       monotonic version for its target resource
content_hash         SHA-256 of canonical JSON merged_payload
created_at           timezone-aware timestamp
created_by           user or system principal
parent_versions      ordered references and hashes of contributing layers
merged_payload       validated fully resolved configuration
scope and owner_id   ownership of the snapshot target
runtime_expires_at   required for a runtime override; otherwise null
~~~

The content hash is calculated only after validation and canonical JSON serialization with deterministic key ordering. A run stores snapshot ID, content hash, parent versions, and the embedded merged payload so a decision remains reproducible after later settings change.

## Resolution order

~~~text
SYSTEM < MARKET < STRATEGY < USER < PORTFOLIO < ROTATION_PLAN < RUNTIME_OVERRIDE
~~~

Every layer is parsed by the same Pydantic schema before merging and the final payload is validated again. A layer may only contribute fields it owns by schema; it cannot inject arbitrary data.

## Deterministic merge rules

1. A missing key inherits the lower-priority key.
2. Map plus map merges recursively.
3. Scalar values replace lower-priority scalars.
4. A list replaces the lower-priority list by default. A schema field may explicitly declare keyed-list merging and the unique key; only such fields merge by key.
5. A literal null is an explicit empty value and replaces the inherited value only if the target schema permits null. It never means inherit.
6. Deleting an inherited optional map key requires the explicit delete directive represented in the wire schema as an object containing only a delete marker. The directive is rejected for required fields and is absent from the resolved payload.
7. A non-null value with a type incompatible with the inherited value is a ConfigurationMergeError. Lists never merge with maps or scalars.
8. Runtime overrides are command-local, require a positive expiry no later than the run deadline, are never persisted as a reusable profile, and are discarded after the run. Their maximum TTL is a system safety limit.

The schema documents merge behavior field by field. In particular, rules, scoring factors, stages, market sessions, and candidate memberships are lists and replace unless their declared merge strategy says otherwise. This prevents accidental combination of independently valid but incompatible strategies.

## Configuration ownership

SYSTEM templates are read-only to ordinary users. USER profiles are available only to their owner and may be referenced by that user's portfolios. PORTFOLIO overrides are visible only through their portfolio. A resolver receives AccessContext and fails closed when any parent layer is inaccessible.

## Error handling

Invalid YAML, unknown fields, non-JSON-serializable values, expired runtime overrides, cyclic parent references, bad hash data, unavailable parent versions, and merge/type errors all fail the run before provider access. The resulting failure is an auditable non-action result, never a fallback to defaults that were not explicitly resolved.
