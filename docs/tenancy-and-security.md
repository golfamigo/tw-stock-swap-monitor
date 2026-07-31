# Tenancy and Security

## Authorization rule

Every application command and repository query receives AccessContext:

~~~text
actor_user_id
is_administrator
request_id
authentication_method
~~~

Access to a USER record requires matching actor_user_id or administrator capability. Access to a PORTFOLIO record first resolves the owning portfolio, then verifies its user ID. SYSTEM references are readable only where the caller is otherwise authorized to use the selected market, but ordinary users cannot create, update, or delete them. Child resources are never fetched by bare ID without this check.

## Repository contract

Repository Protocols accept either a typed owner reference or AccessContext plus resource ID. They return NotFoundForActor for both absence and non-ownership, avoiding an identifier enumeration oracle. Write methods re-check ownership and permitted reference scopes transactionally. An in-memory implementation uses the same rule so unit tests exercise authorization before SQLAlchemy exists.

## Configuration scope controls

SYSTEM templates are administrator-maintained. USER profiles may only be selected by the owning user. PORTFOLIO settings require the portfolio owner. The resolver verifies each parent configuration before merging. A plan cannot refer to a profile owned by another user, even if its UUID is known.

## Administrative dry-run

The minimal POST admin/run-once adapter is guarded by an explicit administrative-token dependency. The token comes only from ADMIN_API_TOKEN and is absent from logs and responses. It requires a rotation_plan_id and dry_run=true. The service creates an audit record and calls the same coordinator as a scheduler. It is intentionally not anonymous in production configuration and cannot update positions.

## Data handling

Secrets are environment or platform secret references only. Logging uses correlation IDs and resource IDs but redacts credentials, full notification destinations, and unnecessary personal data. External provider and LLM input are Pydantic-validated before entering the domain. No adapter may make a trading order; no broker credentials are modeled.

## Security tests

Tests must prove that two users cannot read or mutate each other's portfolios, positions, groups, plans, runs, or private profiles; that both can reference a global Instrument but neither can mutate it; and that protected positions cannot enter a sale path. The same test matrix runs against in-memory repositories and the SQLAlchemy repository when persistence integration begins.
