# Infrastructure and deployment notes

Status: initial direction confirmed with Infrastructure on August 27, 2026.
This document records the current deployment constraints and open decisions. It
is not a statement that the production process has been approved or deployed.

## Confirmed direction

- The City's practical option for unattended Python execution is currently an
  IT-managed Windows server or VM. Someone in IT would connect to the server,
  install the approved dependencies, and configure Windows Task Scheduler.
- The code should be maintained in a City of Raleigh GitHub repository.
  PlanDev can push reviewed updates, while IT can clone an approved revision to
  the server and update the scheduled deployment when needed.
- The repository should be the source of truth for code and configuration
  templates. Credentials, logs, generated routes, and daily data snapshots must
  remain outside version control.
- The job must minimize EnerGov traffic, enforce request limits, and preserve a
  usable fallback when EnerGov, the GIS service, or the publishing step fails.
- No remote repository, initial commit, or production schedule has been created
  yet.

## Proposed minimal workflow

```
Windows Task Scheduler
        -> Python retrieval and validation
        -> EnerGov WebAPI search
        -> Routing service
        -> Daily JSON route snapshot
        -> Static inspector route page
```

The expected production run is once each business day shortly after the 3:00
p.m. inspection-request cutoff. The job determines the next business day,
retrieves that day's scheduled inspections and the rollover-source population,
groups the work locally by inspector and address, creates the route sequence,
and atomically writes one JSON snapshot for the route date. A separate command
builds the static HTML page from that snapshot. The initial workflow is
read-only and does not write changes back to EnerGov.

## API safeguards

- Authenticate once and reuse the same session for the run.
- Retrieve the default criteria and setup metadata once per run, or cache stable
  setup data later if the EnerGov owner approves it.
- Use the three server-filtered, paginated searches required by the current
  POC: rollover-date scheduled, route-date scheduled, and route-date requested.
  Retrieve all relevant inspectors together when practical and group them
  locally instead of repeating the same searches per inspector.
- Use lightweight search results for routing. Individual
  `GET /api/inspections/{id}` requests are exception-only enrichment and are
  disabled in the low-request workflow.
- Keep explicit page-size and maximum-record limits. Record the request count,
  records returned, duration, and any throttling or retry responses in each run.
- Use short, bounded retries with backoff only for transient failures. Do not
  repeatedly restart a large retrieval or issue unbounded parallel requests.
- Validate unexpectedly empty or unusually large results before publishing.

## Output and delivery

### Simple daily web view

The POC now writes a daily JSON route snapshot and generates a static site from
it. Daily JSON archives remain ignored. The builder writes the latest snapshot
to `route-data.js` at the repository root so the committed GitHub Pages site can
load it. Publishing this file exposes its route data through the site and Git
history, so access and retention approval is required before production use.

### Power Automate from a cached snapshot

The scheduled job could place its result in a location Power Automate can read,
after which a flow could distribute inspector-specific messages. This may be a
workable interim solution, but the approved storage location and permissions
must be established first.

### Email from the scheduled job

Direct email remains possible only if IT provides a supported unattended
authentication method. No Azure application registration is currently
available, so Outlook or Microsoft Graph automation cannot be assumed. Email
credentials or tokens must never be stored in the repository or embedded in
the script.

## Failure and fallback behavior

- Write snapshots atomically so a failed run cannot replace a valid snapshot
  with a partial file.
- Preserve the last successful snapshot with its generation time and route
  date. Any web view must clearly identify stale data rather than presenting it
  as current.
- If the GIS route service fails after valid inspection data is retrieved, use
  the existing alphabetical-address method and label the result as a fallback
  route.
- If EnerGov authentication, retrieval, or validation fails, do not publish a
  new route as successful. Preserve the prior snapshot, return a failing process
  exit code, and produce a concise operational log for support.
- The day-old database can support development and historical replay. Whether
  it is acceptable as an explicitly labeled emergency production fallback is a
  business decision; it must not silently replace live data.
- Scheduled reruns must be safe and replace only the snapshot for the same route
  date after successful validation.

## Windows deployment expectations

- Run the task under a least-privilege service identity approved by IT.
- Store EnerGov and GIS credentials using an approved machine-level secret
  mechanism or protected environment variables, not files tracked by Git.
- Pin the Python version and dependency versions used by the scheduled task.
- Set an explicit working directory, timeout, retry policy, log location, and
  nonzero failure exit behavior in Task Scheduler.
- Deploy a reviewed commit or release. Avoid automatically pulling the latest
  branch at the start of every scheduled run so production can be reproduced or
  rolled back.
- Give PlanDev access to the repository, run logs, deployed revision, and output
  validation needed to maintain the business rules collaboratively.

## Decisions still needed

- Windows server/VM owner, service identity, and support contact.
- City GitHub repository location, access, review, and deployment process.
- Approved secret storage and credential-rotation process.
- Confirmed EnerGov request limits, page size, retry guidance, and support path.
- GIS service endpoint, authentication, limits, and fallback expectations.
- Snapshot storage, retention, access controls, and stale-data behavior.
- Delivery choice: web view, Power Automate, direct email, or a phased
  combination.
- Monitoring and notification method when a scheduled run fails.
