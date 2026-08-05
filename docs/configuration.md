# Configuration and Versioning

## Canonical configuration

YAML is accepted only as a seed, development, test, export, or import template. Production execution reads a validated immutable configuration snapshot from PostgreSQL. A strategy run stores the complete merged snapshot, not only references to source profiles.

## Generic seed templates

The committed files in `config_templates/` are generic LayerPatchSchema seeds.
They are intentionally non-operational: market data is mock-only, recommendation
settings prohibit order, broker, and position mutation, and scoring/sizing remain
disabled until a reviewed configuration-to-engine binding is introduced. They
never contain real symbols, holdings, schedules, costs, notification recipients,
secrets, or live credentials. The unit suite parses each YAML file and validates
its permitted layer scope; it also parses the strategy seed with the constrained
rule DSL so schema changes cannot silently drift from the templates.

Each snapshot stores:

~~~text
config_version       monotonic version for its target resource
content_hash         SHA-256 of canonical format version and merged payload
created_at           timezone-aware timestamp
created_by           user or system principal
parent_versions      ordered references, hashes, and hash-format versions of contributing layers
merged_payload       validated fully resolved configuration
scope and owner_id   ownership of the snapshot target
runtime_expires_at   required for a runtime override; otherwise null
~~~

The content hash is calculated only after final validation and canonical serialization. Before a layer can contribute, its sparse patch is normalized with `mode="python"`, `by_alias=True`, and `exclude_unset=True`; its versioned canonical hash must match the supplied layer hash. Delete directives therefore enter parent evidence only as the stable `$delete` alias. A run stores snapshot ID, content hash, parent versions, and the embedded merged payload so a decision remains reproducible after later settings change.

## Schema separation

Every input layer uses a LayerPatchSchema. It accepts only keys permitted at that layer; every business field is optional because a layer is intentionally sparse. Its typed patch values may express a supplied value, an explicit null for nullable fields, or a delete directive for optional inherited map fields. Unknown fields and patch operations that are unauthorized for a field fail immediately.

After all patches merge, the result is parsed by a separate ResolvedConfigurationSchema. It has required final fields, final cross-field validation, and no delete directives. This is the only schema that produces a configuration snapshot. A patch can never be mistaken for an executable configuration.

## Resolution order

~~~text
SYSTEM < MARKET < STRATEGY < USER < PORTFOLIO < ROTATION_PLAN < RUNTIME_OVERRIDE
~~~

Every LayerPatchSchema is validated before merging and the final result is validated by ResolvedConfigurationSchema. A resolver receives AccessContext and rejects a parent layer that is inaccessible to the caller.

## Deterministic merge rules

1. A missing patch key inherits the lower-priority key.
2. Map plus map merges recursively.
3. Scalar values replace lower-priority scalars.
4. A list replaces the lower-priority list by default. A schema field may explicitly declare keyed-list merging and the unique key; only such fields merge by key.
5. A literal null is an explicit empty value and replaces the inherited value only if the final schema permits null. It never means inherit.
6. Deleting an inherited optional map key requires the explicit typed delete directive. The directive is rejected for required fields and is absent from the resolved payload.
7. A non-null value with a type incompatible with the inherited value is a ConfigurationMergeError. Lists never merge with maps or scalars.
8. Runtime overrides are command-local LayerPatchSchema values, require a positive expiry no later than the run deadline, are never persisted as reusable profiles, and are discarded after the run. Their maximum TTL is a system safety limit.

The schema documents merge behavior field by field. In particular, rules, scoring factors, stages, market sessions, and candidate memberships are lists and replace unless their declared merge strategy says otherwise. This prevents accidental combination of independently valid but incompatible strategies.

## Canonical snapshot serialization

Canonicalization is versioned and runs over the validated ResolvedConfigurationSchema Python value, never untyped YAML. The payload is UTF-8 JSON with no insignificant whitespace, NFC-normalized strings and map keys, and map keys sorted by normalized UTF-8 bytes. Duplicate map keys created by NFC normalization are rejected. Lists retain declared order because list order is semantic.

Values normalize as follows:

| Value | Canonical representation |
| --- | --- |
| Decimal | finite base-10 value with trailing fractional zeroes removed, no exponent, and negative zero rendered as 0 |
| UUID | lower-case hyphenated RFC 4122 string |
| Enum | its declared stable string value, never its Python name or representation |
| datetime | aware value converted to UTC and formatted with exactly six fractional digits and Z suffix |
| date | ISO-8601 calendar date |
| integer / Boolean / null | JSON primitive; Boolean is never treated as an integer |
| string | NFC-normalized JSON string encoded without ASCII escaping |

Floats, NaN, positive/negative infinity, sets, bytes, arbitrary objects, and naive datetimes are rejected at canonicalization. Thus numerically equal Decimal values such as 1.0 and 1.00 hash identically. The hash input is UTF-8 bytes of canonical format version, one zero-byte delimiter, and canonical JSON; SHA-256 of those bytes is content_hash. Parent evidence carries the exact hash format version. A resolver rejects an unsupported layer format rather than silently treating it as the current format. A canonicalization change requires a new format version and never silently re-hashes historical snapshots.

## Configuration ownership and errors

SYSTEM templates are read-only to ordinary users. USER profiles are available only to their owner and may be referenced by that user's portfolios. PORTFOLIO overrides are visible only through their portfolio. Invalid YAML, unknown or unauthorized patch fields, malformed patch operations, expired runtime overrides, cyclic parent references, unavailable parents, canonicalization failures, and merge/type errors fail the run before provider access. The failure is an auditable non-action result, never a fallback to defaults that were not explicitly resolved.
